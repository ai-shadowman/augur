"""Parsers for dependencies as they were *declared*, not as they resolved.

Syft reports what a tree resolves to and skips unpinned ranges entirely, so a
project that says ``kombu>=2.1.8,<2.2.0`` produces no Syft artifact at all.
Migration analysis needs that constraint: the range is the statement of intent,
and the upgrade question is asked against it. These parsers recover it.

Every function here is pure with respect to the filesystem it reads. Nothing
executes project code, which matters because ``setup.py`` is arbitrary Python
and the repositories under analysis are old and untrusted.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

from .models import BaseImage, Layer, Package

logger = logging.getLogger(__name__)

#: Filenames that hold Python dependency declarations, matched case-insensitively.
REQUIREMENTS_GLOBS = ("requirements*.txt", "requirements/*.txt", "*-requirements.txt")

#: ``FROM <ref> [AS <stage>]`` with an optional ``--platform=`` flag in between.
FROM_RE = re.compile(
    r"^\s*FROM\s+(?:--[\w-]+=\S+\s+)*(?P<ref>\S+)(?:\s+AS\s+(?P<stage>\S+))?\s*$",
    re.IGNORECASE,
)


def _purl(ecosystem: str, name: str, version: str | None = None) -> str:
    """Build a package URL. Version is omitted when the declaration is a range."""
    base = f"pkg:{ecosystem}/{name}"
    return f"{base}@{version}" if version else base


def _exact_version(specifier: str | None) -> str | None:
    """Return the pinned version if a specifier pins exactly one, else None.

    ``==2.6.2.1`` resolves to a version. ``>=1.5,<2.0`` does not, and inventing
    one from the lower bound would be a fabrication, which is the behaviour this
    whole package exists to prevent.
    """
    if not specifier:
        return None
    parts = [p.strip() for p in specifier.split(",") if p.strip()]
    if len(parts) == 1 and parts[0].startswith("==") and "*" not in parts[0]:
        return parts[0][2:].strip()
    return None


def parse_requirements(path: Path, root: Path | None = None) -> list[Package]:
    """Parse one pip requirements file.

    Nested ``-r`` includes are deliberately not followed: discovery walks the
    whole tree and will reach the included file on its own, and following here
    as well would double-count every shared requirement.
    """
    from pip_requirements_parser import RequirementsFile

    rel = str(path.relative_to(root)) if root else str(path)
    try:
        parsed = RequirementsFile.from_file(str(path), include_nested=False)
    except Exception as exc:  # noqa: BLE001 - pip's parser raises anything it likes
        logger.warning("could not parse %s: %s", rel, exc)
        return []

    packages = []
    for req in parsed.requirements:
        if not req.name:
            continue  # a bare URL or editable install with no resolvable name
        specifier = str(req.specifier) if req.specifier else None
        line = req.requirement_line.line if req.requirement_line else None
        version = _exact_version(specifier)
        packages.append(
            Package(
                name=req.name,
                ecosystem="pypi",
                layer=Layer.PACKAGE,
                source_file=rel,
                version=version,
                constraint=specifier or None,
                purl=_purl("pypi", req.name, version),
                declared_line=line,
            )
        )
    return packages


def parse_pyproject(path: Path, root: Path | None = None) -> list[Package]:
    """Parse PEP 621 ``[project].dependencies`` and Poetry's dependency table."""
    import tomllib

    from packaging.requirements import Requirement

    rel = str(path.relative_to(root)) if root else str(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - malformed TOML or unreadable file; skip it
        logger.warning("could not parse %s: %s", rel, exc)
        return []

    packages: list[Package] = []

    def add(name: str, specifier: str | None, line: str | None) -> None:
        version = _exact_version(specifier)
        packages.append(
            Package(
                name=name,
                ecosystem="pypi",
                layer=Layer.PACKAGE,
                source_file=rel,
                version=version,
                constraint=specifier or None,
                purl=_purl("pypi", name, version),
                declared_line=line,
            )
        )

    for raw in data.get("project", {}).get("dependencies", []) or []:
        try:
            req = Requirement(raw)
        except Exception:  # noqa: BLE001 - packaging raises several unrelated types
            logger.warning("%s: unparseable dependency %r", rel, raw)
            continue
        add(req.name, str(req.specifier) or None, raw)

    # Poetry predates PEP 621 and is still common in older trees.
    poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {}) or {}
    for name, spec in poetry.items():
        if name.lower() == "python":
            continue  # the runtime, handled as its own layer
        if isinstance(spec, dict):
            spec = spec.get("version")
        if not isinstance(spec, str):
            spec = None
        add(name, spec, f"{name} = {spec!r}" if spec else name)

    return packages


def parse_setup_py(path: Path, root: Path | None = None) -> tuple[list[Package], list[str]]:
    """Recover ``install_requires`` from setup.py by reading the AST.

    Returns the packages it could prove and a list of warnings for the ones it
    could not. Many older projects compute ``install_requires`` at runtime by
    reading their requirements files, which no static parser can resolve;
    celery 2.5.5 is one of them. Saying so is more useful than reporting an
    empty dependency list as though it were a finding.
    """
    from packaging.requirements import Requirement

    rel = str(path.relative_to(root)) if root else str(path)
    warnings: list[str] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as exc:
        # Python 2 sources will not parse under a Python 3 ast. That is a real
        # limit for exactly the legacy repositories this pipeline targets.
        return [], [f"{rel}: could not parse as Python 3 ({exc.msg}); install_requires not recovered"]

    packages: list[Package] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_setup_call(node)):
            continue
        for kw in node.keywords:
            if kw.arg not in ("install_requires", "requires"):
                continue
            if not isinstance(kw.value, ast.List):
                warnings.append(
                    f"{rel}: install_requires is computed at runtime "
                    f"({type(kw.value).__name__}); dependencies not recoverable statically"
                )
                continue
            for element in kw.value.elts:
                if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
                    warnings.append(f"{rel}: non-literal entry in install_requires")
                    continue
                try:
                    req = Requirement(element.value)
                except Exception:  # noqa: BLE001 - packaging raises several unrelated types
                    warnings.append(f"{rel}: unparseable requirement {element.value!r}")
                    continue
                specifier = str(req.specifier) or None
                version = _exact_version(specifier)
                packages.append(
                    Package(
                        name=req.name,
                        ecosystem="pypi",
                        layer=Layer.PACKAGE,
                        source_file=rel,
                        version=version,
                        constraint=specifier,
                        purl=_purl("pypi", req.name, version),
                        declared_line=element.value,
                    )
                )
    return packages, warnings


def _is_setup_call(node: ast.Call) -> bool:
    """True for ``setup(...)`` and ``setuptools.setup(...)``."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "setup"
    return isinstance(func, ast.Attribute) and func.attr == "setup"


def parse_containerfile(path: Path, root: Path | None = None) -> list[BaseImage]:
    """Extract base image references from a Containerfile or Dockerfile.

    Syft catalogs the OS layer from a built image, not from the file that names
    it, so this is the static half: it records which images would have to be
    scanned to populate that layer. References to earlier build stages are
    skipped, since they are internal to the build rather than an OS baseline.
    """
    rel = str(path.relative_to(root)) if root else str(path)
    images: list[BaseImage] = []
    stages: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        logger.warning("could not read %s: %s", rel, exc)
        return []

    for number, line in enumerate(lines, start=1):
        match = FROM_RE.match(line)
        if not match:
            continue
        ref, stage = match.group("ref"), match.group("stage")
        if stage:
            stages.add(stage.lower())
        if ref.lower() in stages or ref.lower() == "scratch":
            continue  # an earlier stage in this same build, not a base image
        images.append(
            BaseImage(reference=ref, source_file=rel, stage=stage, line_number=number)
        )
    return images
