"""Upstream health enrichment for the package layer, via veripak.

``facts`` answers "what does this project depend on". This module answers "and
is any of that still viable", which is the question a migration is actually
asking. It shells out to veripak for EOL status, current upstream version, CVE
exposure and replacement candidates.

Provenance is tracked per field rather than per package, because veripak mixes
two kinds of answer. The current version and the download check come from
registry APIs and are reproducible. The EOL assessment and the CVE list come
from an agent, and while veripak cross-checks CVE identifiers against OSV.dev
and NVD before admitting them, an agent-sourced field is not the same kind of
fact as a registry lookup. Callers that need determinism can ask for the
deterministic subset and get it; callers that want the full picture get the
rest clearly labelled.

Results are cached per package so an interrupted run resumes without repeating
network calls, and so the free-tier NVD rate limit is not spent twice on the
same question.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .models import Package, normalize_name

logger = logging.getLogger(__name__)

VERIPAK_TIMEOUT_SECONDS = 300

#: PURL ecosystem to the name veripak uses. Ecosystems absent from this map are
#: skipped rather than guessed at.
ECOSYSTEM_MAP = {
    "pypi": "python",
    "npm": "npm",
    "maven": "java",
    "golang": "go",
    "nuget": "nuget",
    "cargo": "rust",
    "composer": "php",
    "gem": "ruby",
}

#: Fields veripak derives from registry APIs, reproducible on any machine.
DETERMINISTIC_FIELDS = ("version", "download")

#: Fields veripak derives with agent assistance. Carried, but labelled.
ASSISTED_FIELDS = ("eol", "cves", "replacement")


class VeripakNotAvailable(RuntimeError):
    """Raised when the veripak binary cannot be found."""


def veripak_available() -> bool:
    return shutil.which("veripak") is not None


def _cache_path(cache_dir: Path, ecosystem: str, name: str, versions: tuple[str, ...]) -> Path:
    stamp = "-".join(versions) if versions else "any"
    safe = normalize_name(name)
    return cache_dir / ecosystem / f"{safe}@{stamp}.json"


def check_package(
    name: str,
    ecosystem: str = "python",
    versions: Iterable[str] = (),
    cache_dir: Path | None = None,
    skip_cves: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Audit one package, reusing a cached result when there is one."""
    versions = tuple(v for v in versions if v)
    cached = _cache_path(cache_dir, ecosystem, name, versions) if cache_dir else None
    if cached and cached.exists() and not force:
        return json.loads(cached.read_text(encoding="utf-8"))

    binary = shutil.which("veripak")
    if not binary:
        raise VeripakNotAvailable(
            "veripak is required for health enrichment and was not found on PATH. "
            "Install it with 'pip install veripak'."
        )

    command = [binary, "check", name, "-e", ecosystem, "--json", "--no-summary"]
    if versions:
        command += ["--versions", ",".join(versions)]
    if skip_cves:
        command.append("--no-cves")

    logger.info("veripak check %s (%s)", name, ecosystem)
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=VERIPAK_TIMEOUT_SECONDS,
        check=False,  # the returncode is inspected below so the error can name the package
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"veripak exited {result.returncode} for {name}: {result.stderr.strip()[:300]}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"veripak produced non-JSON output for {name}: {exc}") from exc

    if cached:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def summarize(payload: dict[str, Any]) -> dict[str, Any]:
    """Reduce a veripak record to the fields a migration decision turns on.

    ``unverified_claims`` surfaces veripak's own HITL flags. Those record
    identifiers its agent proposed and its verifier then rejected, which is
    exactly the kind of thing that should travel with a result rather than be
    discarded alongside it.
    """
    version = payload.get("version") or {}
    eol = payload.get("eol") or {}
    cves = payload.get("cves") or {}
    return {
        "package": payload.get("package"),
        "ecosystem": payload.get("ecosystem"),
        "versions_in_use": payload.get("versions_in_use") or [],
        "latest_upstream": version.get("version"),
        "latest_source": version.get("method"),
        "download_confirmed": (payload.get("download") or {}).get("confirmed"),
        "project_status": eol.get("project_status"),
        "eol": eol.get("eol"),
        "eol_date": eol.get("eol_date"),
        "eol_confidence": eol.get("confidence"),
        "cve_count": cves.get("total_count"),
        "cve_high_critical": cves.get("high_critical_count"),
        "replacement": (payload.get("replacement") or {}).get("name")
        if isinstance(payload.get("replacement"), dict)
        else payload.get("replacement"),
        "unverified_claims": [
            flag.get("reason") for flag in payload.get("hitl_flags") or []
        ],
        "field_provenance": {
            **dict.fromkeys(DETERMINISTIC_FIELDS, "registry"),
            **dict.fromkeys(ASSISTED_FIELDS, "agent-assisted"),
        },
    }


def enrich(
    packages: Iterable[Package],
    cache_dir: Path,
    skip_cves: bool = False,
    limit: int | None = None,
    pause_seconds: float = 0.0,
    force: bool = False,
) -> dict[str, dict[str, Any]]:
    """Audit the distinct distributions in a package list.

    Declarations are collapsed to distinct distributions first, since the same
    dependency declared in four requirements files is still one upstream
    project and should cost one audit rather than four. Failures are recorded
    against the package and do not stop the run, because a single unreachable
    registry should not cost the whole enrichment pass.
    """
    distinct: dict[tuple[str, str], set[str]] = {}
    for package in packages:
        ecosystem = ECOSYSTEM_MAP.get(package.ecosystem)
        if not ecosystem:
            continue
        key = (ecosystem, package.name)
        distinct.setdefault(key, set())
        if package.version:
            distinct[key].add(package.version)

    ordered = sorted(distinct.items())
    if limit is not None:
        ordered = ordered[:limit]

    results: dict[str, dict[str, Any]] = {}
    for index, ((ecosystem, name), versions) in enumerate(ordered):
        try:
            payload = check_package(
                name,
                ecosystem=ecosystem,
                versions=sorted(versions),
                cache_dir=cache_dir,
                skip_cves=skip_cves,
                force=force,
            )
            results[name] = summarize(payload)
        except Exception as exc:  # noqa: BLE001 - one bad package must not sink the audit
            logger.warning("health check failed for %s: %s", name, exc)
            results[name] = {"package": name, "ecosystem": ecosystem, "error": str(exc)}
        if pause_seconds and index + 1 < len(ordered):
            time.sleep(pause_seconds)
    return results
