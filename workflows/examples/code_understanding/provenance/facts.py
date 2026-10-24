"""Collect everything about a repository that can be established without a model.

This is the pre-LLM half of the provenance contract. It walks a checkout, reads
the git metadata, parses every dependency manifest it recognises, and runs Syft
over the tree, producing a single ``RepoFacts`` record. ``verify`` later checks
a finished index against that record.

Ordering matters when merging: a manifest parser knows the declared constraint
and the verbatim line, while Syft knows only a resolved version. Where both
describe the same package in the same file, the manifest record wins, because
losing ``>=2.1.8,<2.2.0`` in favour of a bare ``2.1.8`` would discard exactly
the information an upgrade decision needs.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

from . import declarations, imports, manifests
from .models import Package, RepoFacts
from .sbom import SyftNotAvailable, scan_directory

logger = logging.getLogger(__name__)

#: Directories never worth walking for manifests. Vendored trees are excluded
#: deliberately: a dependency's own requirements are not this repository's
#: declared dependencies, and including them inflates the package layer.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "node_modules",
    ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages",
    ".eggs", "build", "dist", ".idea", ".vscode",
})

CONTAINERFILE_NAMES = ("containerfile", "dockerfile")


def _git(root: Path, *args: str) -> str | None:
    """Run a read-only git command, returning None if it is not a repository."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,  # a non-zero exit means "not a repository", not a failure
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("git %s failed in %s: %s", args, root, exc)
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def git_facts(root: Path) -> dict[str, str | None]:
    """Read git identity straight from the checkout.

    These four fields are the ones AUGUR already injects into every file header,
    and they are pure fact: no model is involved in producing them, so any
    difference between these values and what appears in the finished graph is a
    defect rather than a judgement call.
    """
    remote = _git(root, "config", "--get", "remote.origin.url")
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return {
        "git_repo": remote,
        "git_slug": _slug_for(remote, branch),
        "git_commit": _git(root, "rev-parse", "HEAD"),
        "git_branch": branch,
    }


#: What ``git rev-parse --abbrev-ref HEAD`` returns on a detached checkout,
#: which is not a branch name and must not be fed to the slug helper.
DETACHED = "HEAD"


def _slug_for(remote: str | None, branch: str | None = None) -> str | None:
    """Derive the repository slug the way the rest of AUGUR derives it.

    The pipeline builds every slug with ``generate_slug_from_repo``, which
    yields ``owner-repo-branch``. Reimplementing that here would produce a slug
    that no indexing run ever used, and the verifier would then compare the
    graph against a name that was never in it, so the real helper is called.

    On a detached checkout there is no branch to supply and the helper's own
    default stands, which keeps this consistent with a pipeline run that was
    given no branch either.
    """
    if not remote:
        return None
    try:
        from utils.code_utils import generate_slug_from_repo

        if branch and branch != DETACHED:
            return generate_slug_from_repo(remote, branch)
        return generate_slug_from_repo(remote)
    except Exception:  # noqa: BLE001 - optional helper; any failure means fall back
        logger.debug("falling back to local slug derivation for %s", remote)
    tail = remote.rstrip("/").rsplit("/", 1)[-1]
    return tail.removesuffix(".git") or None


def _iter_files(root: Path):
    """Walk the tree, skipping directories that cannot hold our own manifests."""
    import os

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for filename in filenames:
            yield Path(dirpath) / filename


def _is_requirements(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(".txt") and ("requirement" in name or path.parent.name == "requirements")


def declared_packages(path: Path, root: Path) -> list[Package]:
    """The packages one manifest declares, or ``[]`` for a file no parser here reads.

    The per-file view of :func:`discover`, for a caller holding one path rather
    than a tree. ``setup.py`` is left to ``discover`` because it is a source
    file, not a config file, and its parser returns warnings this shape drops.
    """
    if _is_requirements(path):
        return manifests.parse_requirements(path, root)
    if path.name.lower() == "pyproject.toml":
        return manifests.parse_pyproject(path, root)
    return []


def discover(root: Path) -> tuple[list[Package], list, list[str]]:
    """Parse every dependency manifest in the tree.

    Returns declared packages, base images, and warnings describing anything a
    parser could see but could not resolve.
    """
    packages: list[Package] = []
    images: list = []
    warnings: list[str] = []

    for path in _iter_files(root):
        name = path.name.lower()
        try:
            if _is_requirements(path) or name == "pyproject.toml":
                packages.extend(declared_packages(path, root))
            elif name == "setup.py":
                found, notes = manifests.parse_setup_py(path, root)
                packages.extend(found)
                warnings.extend(notes)
            elif name.startswith(CONTAINERFILE_NAMES) or name.endswith(CONTAINERFILE_NAMES):
                images.extend(manifests.parse_containerfile(path, root))
        except Exception as exc:  # noqa: BLE001 - one bad manifest must not sink the scan
            logger.warning("failed parsing %s: %s", path, exc)
            warnings.append(f"{path.relative_to(root)}: parser raised {type(exc).__name__}: {exc}")

    return packages, images, warnings


def _holds_python(directory: Path) -> bool:
    """True if any Python file lives anywhere under ``directory``."""
    for _dirpath, dirnames, filenames in os.walk(directory):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        if any(f.endswith(".py") for f in filenames):
            return True
    return False


def own_modules(root: Path) -> list[str]:
    """Top-level importable names the repository defines itself.

    Knowing these names is what lets the verifier prove that an entity like
    ``celery.worker.utils.timeutils`` was misfiled as a third-party library,
    rather than leaving it to be spotted by eye.

    An ``__init__.py`` is the clean signal, but requiring one is too strict in
    two cases that both occur here: a PEP 420 namespace package has none, and a
    corpus assembled from a subset of a checkout can contain ``celery/worker``
    without ``celery/__init__.py``. Either way the directory holds this
    project's code, so any top-level directory containing Python counts.
    """
    names = []
    for child in sorted(root.iterdir()):
        if child.name in SKIP_DIRS or child.name.startswith("."):
            continue
        if not child.is_dir():
            continue
        if (child / "__init__.py").exists() or _holds_python(child):
            names.append(child.name)
    return names


#: Extensions counted as source for the file inventory. Deliberately narrow:
#: the inventory exists to adjudicate module entities, and a model does not
#: invent module entities for binary assets.
SOURCE_SUFFIXES = (
    ".py", ".pyx", ".pyi", ".java", ".go", ".rb", ".js", ".ts", ".tsx", ".jsx",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".rs", ".sh", ".txt", ".toml", ".cfg",
    ".yaml", ".yml", ".json", ".xml",
)


def source_files(root: Path, paths: Iterable[Path] | None = None) -> list[str]:
    """Every source path in the checkout, repository-relative and POSIX style.

    This is what lets the verifier separate a module entity that names a real
    but unindexed file from one that names nothing at all. The first is the
    model recalling the project from training data, the second is invention,
    and the corpus alone cannot tell them apart because it is only ever a
    subset of the repository.

    ``paths`` lets a caller that has already walked the tree hand the result
    over rather than pay for a second traversal.
    """
    found = []
    for path in _iter_files(root) if paths is None else paths:
        if path.name.lower().endswith(SOURCE_SUFFIXES):
            found.append(path.relative_to(root).as_posix())
    return sorted(found)


#: Directories a version-control system keeps inside a checkout, and the only
#: ones the full inventory leaves out. Unlike :data:`SKIP_DIRS`, a committed
#: ``build/`` or vendored tree stays in: the inventory sizes what a report may
#: legitimately name, and a file the repository tracks is nameable.
VCS_DIRS = frozenset({".git", ".hg", ".svn"})


def checkout_files(root: Path) -> list[str]:
    """Every file in the checkout, repository-relative and POSIX style.

    :func:`source_files` is narrow on purpose and this is complete on purpose.
    A migration plan that names ``Makefile`` or ``tox.ini`` names a real file,
    and checked against the narrow list it would read as invented, which is
    the partial-evidence failure the checkout walk exists to prevent. Captured
    here because the checkout is gone by the time the report is written.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in VCS_DIRS]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.is_file():
                found.append(path.relative_to(root).as_posix())
    return sorted(found)


def _merge(declared: list[Package], resolved: list[Package]) -> list[Package]:
    """Combine manifest and Syft records, preferring the declared constraint.

    Syft and the manifest parsers both read ``requirements.txt``, so a pinned
    requirement is found twice. The manifest record carries the constraint and
    the verbatim line, so it wins the collision.
    """
    merged: dict[tuple[str, str], Package] = {}
    for package in declared:
        merged.setdefault((package.key, package.source_file), package)
    for package in resolved:
        merged.setdefault((package.key, package.source_file), package)
    return list(merged.values())


def collect(
    root: str | Path,
    multi_repo: bool = False,
    sbom_path: Path | None = None,
    run_syft: bool = True,
    force: bool = False,
) -> RepoFacts:
    """Build the full ground-truth record for one repository checkout."""
    root = Path(root).resolve(strict=True)
    facts = RepoFacts(root=str(root), multi_repo=multi_repo, **git_facts(root))

    declared, images, warnings = discover(root)
    facts.base_images = images
    facts.warnings = warnings
    facts.own_modules = own_modules(root)

    # One walk feeds both inventories. The file list is the expensive part on a
    # large checkout, and the two readers want the same files.
    walked = list(_iter_files(root))
    facts.source_files = source_files(root, walked)
    facts.checkout_files = checkout_files(root)
    symbols, symbol_warnings = declarations.scan_files(root, walked)
    facts.declared_symbols = {key: sorted(names) for key, names in symbols.items()}
    facts.warnings.extend(symbol_warnings)

    imported, import_warnings = imports.scan_tree(root, SKIP_DIRS)
    facts.imported_names = sorted(imported)
    facts.warnings.extend(import_warnings)

    resolved: list[Package] = []
    if run_syft:
        try:
            resolved = scan_directory(root, sbom_path=sbom_path, force=force)
        except SyftNotAvailable as exc:
            # Visible, not silent: a missing SBOM changes what the verifier can
            # prove, so the gap is recorded rather than glossed over.
            facts.warnings.append(f"SBOM not generated: {exc}")
            logger.warning("%s", exc)

    facts.packages = _merge(declared, resolved)
    return facts


def write(facts: RepoFacts, path: Path) -> Path:
    """Persist facts as JSON so later pipeline stages can read them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(facts.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return path


def read(path: Path) -> RepoFacts:
    """Load facts previously written by :func:`write`."""
    from .models import BaseImage, Layer

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    facts = RepoFacts(
        root=data["root"],
        git_repo=data.get("git_repo"),
        git_slug=data.get("git_slug"),
        git_commit=data.get("git_commit"),
        git_branch=data.get("git_branch"),
        runtime=data.get("runtime"),
        multi_repo=data.get("multi_repo", False),
        own_modules=data.get("own_modules", []),
        imported_names=data.get("imported_names", []),
        source_files=data.get("source_files", []),
        checkout_files=data.get("checkout_files", []),
        declared_symbols=data.get("declared_symbols", {}),
        warnings=data.get("warnings", []),
    )
    facts.packages = [
        Package(
            name=p["name"],
            ecosystem=p["ecosystem"],
            layer=Layer(p["layer"]),
            source_file=p["source_file"],
            version=p.get("version"),
            constraint=p.get("constraint"),
            purl=p.get("purl"),
            declared_line=p.get("declared_line"),
        )
        for p in data.get("packages", [])
    ]
    facts.base_images = [BaseImage(**b) for b in data.get("base_images", [])]
    return facts
