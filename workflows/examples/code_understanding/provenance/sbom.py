"""Syft wrapper producing an SBOM for the package and OS layers.

Syft is the only extractor in this package that covers more than one language,
and the only one that reaches the OS layer, which it catalogs from a built
container image rather than from source. It runs fully offline and emits a PURL
for every artifact, which gives the package layer a canonical identity that
does not depend on how a model chose to spell the name.

Two behaviours of Syft shape this module and are easy to get wrong:

1. A relative path is resolved against the process working directory *after*
   symlink expansion, so a relative path under a symlinked parent such as
   macOS's ``/tmp`` makes Syft walk the parent and catalog unrelated trees.
   Every path handed to Syft here is absolute and fully resolved.
2. The Python cataloger reports only exactly-pinned requirements, and it
   selects files by filename rather than by path. A range such as
   ``>=2.1.8,<2.2.0`` yields no artifact, and a file at
   ``requirements/py25.txt`` is never read at all because the name does not
   match ``*requirements*.txt``. Both limits bite at once on the common
   layout of a ``requirements/`` directory holding ranged pins: celery 2.5.5
   declares 45 dependencies across 11 such files and Syft reports zero.

Neither point is a defect in Syft, which is built to inventory what a tree
resolves to rather than what it asks for. It is why ``manifests`` exists and
why the two are merged rather than one being chosen. Syft carries the OS layer
and the ecosystems that pin by convention; the manifest parsers carry declared
intent.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .models import OS_ECOSYSTEMS, Layer, Package

logger = logging.getLogger(__name__)

SYFT_TIMEOUT_SECONDS = 900

INSTALL_HINT = (
    "syft is required for SBOM extraction and was not found on PATH. Install it with "
    "'brew install syft' (macOS), or see https://github.com/anchore/syft for Linux "
    "packages and the install script."
)


class SyftNotAvailable(RuntimeError):
    """Raised when the syft binary cannot be found or cannot be run."""


class SyftFailed(RuntimeError):
    """Raised when syft ran but exited non-zero."""


def syft_version() -> str:
    """Return the installed syft version, or raise if it is not usable."""
    binary = shutil.which("syft")
    if not binary:
        raise SyftNotAvailable(INSTALL_HINT)
    result = subprocess.run(
        [binary, "version", "-o", "json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,  # the returncode is inspected below so the error can name syft
    )
    if result.returncode != 0:
        raise SyftNotAvailable(f"syft is on PATH but failed to run: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout).get("version", "unknown")
    except json.JSONDecodeError:
        return result.stdout.strip() or "unknown"


def _run_syft(source: str) -> dict[str, Any]:
    binary = shutil.which("syft")
    if not binary:
        raise SyftNotAvailable(INSTALL_HINT)
    logger.info("running syft against %s", source)
    result = subprocess.run(
        [binary, "scan", source, "-o", "syft-json", "-q"],
        capture_output=True,
        text=True,
        timeout=SYFT_TIMEOUT_SECONDS,
        check=False,  # the returncode is inspected below so the error can carry stderr
    )
    if result.returncode != 0:
        raise SyftFailed(
            f"syft exited {result.returncode} scanning {source}: {result.stderr.strip()[:500]}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SyftFailed(f"syft produced output that is not JSON: {exc}") from exc


def _ecosystem_of(artifact: dict[str, Any]) -> str:
    """Prefer the PURL type, which is canonical, over syft's own type string."""
    purl = artifact.get("purl") or ""
    if purl.startswith("pkg:"):
        remainder = purl[4:]
        if "/" in remainder:
            return remainder.split("/", 1)[0].lower()
    return str(artifact.get("type") or "unknown").lower()


def artifacts_to_packages(document: dict[str, Any]) -> list[Package]:
    """Convert a syft-json document into Package records."""
    packages = []
    for artifact in document.get("artifacts", []) or []:
        name = artifact.get("name")
        if not name:
            continue
        ecosystem = _ecosystem_of(artifact)
        locations = [loc.get("path", "") for loc in artifact.get("locations", []) or []]
        packages.append(
            Package(
                name=name,
                ecosystem=ecosystem,
                layer=Layer.OS if ecosystem in OS_ECOSYSTEMS else Layer.PACKAGE,
                source_file=locations[0].lstrip("/") if locations else "(syft)",
                version=artifact.get("version") or None,
                constraint=None,  # syft resolves; it does not report intent
                purl=artifact.get("purl") or None,
            )
        )
    return packages


def scan_directory(root: Path, sbom_path: Path | None = None, force: bool = False) -> list[Package]:
    """Catalog a source tree.

    When ``sbom_path`` is given the raw document is written there and reused on
    later runs unless ``force`` is set, so re-running the pipeline does not
    repeat work that already succeeded.
    """
    if sbom_path and sbom_path.exists() and not force:
        logger.info("reusing SBOM at %s", sbom_path)
        return artifacts_to_packages(json.loads(sbom_path.read_text(encoding="utf-8")))

    # Resolve before handing the path over. See the module docstring.
    resolved = Path(root).resolve(strict=True)
    document = _run_syft(f"dir:{resolved}")

    if sbom_path:
        sbom_path.parent.mkdir(parents=True, exist_ok=True)
        sbom_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return artifacts_to_packages(document)


def scan_image(reference: str, sbom_path: Path | None = None, force: bool = False) -> list[Package]:
    """Catalog a container image, which is where the OS layer actually lives.

    This is the one part of extraction that needs network access, since the
    image has to be pulled. It is kept separate from ``scan_directory`` so a
    source-only run stays fully offline.
    """
    if sbom_path and sbom_path.exists() and not force:
        logger.info("reusing image SBOM at %s", sbom_path)
        return artifacts_to_packages(json.loads(sbom_path.read_text(encoding="utf-8")))

    document = _run_syft(reference)
    if sbom_path:
        sbom_path.parent.mkdir(parents=True, exist_ok=True)
        sbom_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return artifacts_to_packages(document)
