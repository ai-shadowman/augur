"""Which declared dependency versions fall inside a known vulnerability range.

The question a migration report used to put to a model, answered by a join.
Grype's usual route, an SBOM matched against its database, needs resolved
versions, and legacy code declares ranges: Syft reports zero artifacts on
celery 2.5.5, whose 45 declarations hold one pin. So this module skips the
SBOM and intersects each declared constraint with the affected ranges in
Grype's local database, read once as data through ``grype db search``.

Each declaration gets one verdict:

``all-affected``
    every version the constraint permits is inside some affected range.
``some-affected``
    the constraint admits at least one affected version.
``none-affected``
    no permitted version is in any advisory the database holds. With zero
    advisories for the name, that is "none known as of the build", not "safe".
``unbounded``
    a bare name. Any version is permitted, so no claim is made.
``declined``
    an ecosystem not covered, a constraint that cannot be read, or advisory
    ranges that cannot be read where they might have decided the verdict.

The records joined are the ones Grype's own Python matcher reads: GitHub
advisories and NVD's language-scoped records. NVD's CPE records are left out
because Grype leaves them out (``match.python.using-cpes`` is false by
default): a CPE product name is not scoped to an ecosystem. Advisories GitHub
has withdrawn (status ``rejected``) are left out for the same reason; joining
them reports packages affected by a vulnerability that was never real.

``health`` already carries a CVE list, from veripak's agent. This is the
deterministic counterpart: the same evidence produces the same verdicts, and
each one names the database build it was read from.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from . import ranges
from .models import Package, normalize_name

GRYPE_TIMEOUT_SECONDS = 300

#: facts ecosystem to Grype ecosystem. Absent ecosystems are declined, not guessed.
ECOSYSTEMS = {"pypi": "python"}

#: The namespaces Grype's own matcher reads for each ecosystem.
NAMESPACES = {"python": frozenset({"github:language:python", "nvd:language:python"})}

#: Advisory status meaning the advisory was withdrawn.
REJECTED = "rejected"

INSTALL_HINT = (
    "grype is required for the vulnerability join and was not found on PATH. Install it "
    "with 'brew install grype' (macOS), or see https://github.com/anchore/grype."
)


class GrypeNotAvailable(RuntimeError):
    """Raised when the grype binary or its database cannot be used."""


class Verdict(StrEnum):
    ALL_AFFECTED = "all-affected"
    SOME_AFFECTED = "some-affected"
    NONE_AFFECTED = "none-affected"
    UNBOUNDED = "unbounded"
    DECLINED = "declined"


AFFECTED = frozenset({Verdict.ALL_AFFECTED, Verdict.SOME_AFFECTED})


@dataclass(frozen=True)
class AffectedRange:
    """One affected range of one advisory, verbatim from the database.

    ``constraint`` is ``None`` when the record carries no range at all. Grype
    reads that as "every version"; here it is unreadable, so it can stop a
    ``none-affected`` claim but never make an affected one.
    """

    advisory: str
    constraint: str | None
    fix_state: str
    fixed: str | None = None
    aliases: tuple[str, ...] = ()
    severity: str | None = None


@dataclass(frozen=True)
class Database:
    built: str
    schema: str
    #: ``(grype ecosystem, PEP 503 name)`` to every range recorded for it.
    ranges: dict[tuple[str, str], tuple[AffectedRange, ...]]

    def lookup(self, ecosystem: str, name: str) -> tuple[AffectedRange, ...]:
        return self.ranges.get((ecosystem, normalize_name(name)), ())


@dataclass(frozen=True)
class Finding:
    package: Package
    verdict: Verdict
    db_built: str
    #: The ranges that intersect the declared constraint.
    matched: tuple[AffectedRange, ...] = ()
    #: Distinct advisories the database holds for this name, matched or not.
    advisories_known: int = 0
    unreadable: tuple[AffectedRange, ...] = ()
    reason: str | None = None
    purl: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["package"] = self.package.to_dict()
        d["verdict"] = self.verdict.value
        return d


def _purl(package: Package) -> str:
    if package.purl:
        return package.purl
    version = f"@{package.version}" if package.version else ""
    return f"pkg:{package.ecosystem}/{normalize_name(package.name)}{version}"


def _declared(package: Package) -> str | None:
    """The constraint to join, or ``None`` for a bare name.

    A resolved version with no constraint is the degenerate range and is
    joined as an exact pin.
    """
    if package.constraint:
        return package.constraint
    if package.version:
        return f"=={package.version}"
    return None


def assess(package: Package, database: Database) -> Finding:
    """One verdict for one declaration. Pure; never touches the network."""
    base = {"package": package, "db_built": database.built, "purl": _purl(package)}
    ecosystem = ECOSYSTEMS.get(package.ecosystem)
    if ecosystem is None:
        return Finding(
            verdict=Verdict.DECLINED,
            reason=f"ecosystem {package.ecosystem!r} is not covered by this join",
            **base,
        )

    recorded = database.lookup(ecosystem, package.name)
    base["advisories_known"] = len({r.advisory for r in recorded})
    constraint = _declared(package)
    if constraint is None:
        return Finding(verdict=Verdict.UNBOUNDED, **base)

    try:
        declared = ranges.parse(constraint)
    except ranges.UnsupportedSpecifier as exc:
        return Finding(verdict=Verdict.DECLINED, reason=str(exc), **base)
    if not declared:
        return Finding(
            verdict=Verdict.DECLINED,
            reason=f"constraint {constraint!r} admits no version",
            **base,
        )

    matched: list[tuple[AffectedRange, ranges.VersionSet]] = []
    unreadable: list[AffectedRange] = []
    for affected in recorded:
        if affected.constraint is None:
            unreadable.append(affected)
            continue
        try:
            versions = ranges.parse(affected.constraint)
        except ranges.UnsupportedSpecifier:
            unreadable.append(affected)
            continue
        if ranges.intersects(declared, versions):
            matched.append((affected, versions))
    base["unreadable"] = tuple(unreadable)
    base["matched"] = tuple(m for m, _ in matched)

    if not matched:
        if unreadable:
            # An unread range might be the one that intersects, so "none" would
            # be a claim this evidence cannot support.
            return Finding(
                verdict=Verdict.DECLINED,
                reason=f"{len(unreadable)} advisory range(s) could not be read",
                **base,
            )
        return Finding(verdict=Verdict.NONE_AFFECTED, **base)

    covered = ranges.contains(ranges.union(*(v for _, v in matched)), declared)
    verdict = Verdict.ALL_AFFECTED if covered else Verdict.SOME_AFFECTED
    return Finding(verdict=verdict, **base)


def assess_all(packages: Iterable[Package], database: Database) -> list[Finding]:
    """A finding for every package, in input order; none is dropped."""
    return [assess(p, database) for p in packages]


def parse_search(records: list[dict[str, Any]], ecosystem: str) -> dict:
    """Index ``grype db search -o json`` output by PEP 503 name.

    Only live records in the ecosystem's matcher namespaces are kept. A record
    there with no ``ranges`` is kept with a ``None`` constraint rather than
    dropped, since dropping it would read as "no advisory" downstream.
    """
    namespaces = NAMESPACES[ecosystem]
    index: dict[tuple[str, str], list[AffectedRange]] = defaultdict(list)
    for record in records:
        vulnerability = record["vulnerability"]
        if vulnerability.get("status") == REJECTED:
            continue
        for entry in record.get("packages", ()):
            if entry.get("namespace") not in namespaces:
                continue
            detail = entry.get("detail") or {}
            for affected in detail.get("ranges") or [{}]:
                fix = affected.get("fix") or {}
                index[(ecosystem, normalize_name(entry["package"]["name"]))].append(
                    AffectedRange(
                        advisory=vulnerability["id"],
                        constraint=(affected.get("version") or {}).get("constraint"),
                        fix_state=fix.get("state", "unknown"),
                        fixed=fix.get("version"),
                        aliases=tuple(detail.get("cves", ())),
                        severity=vulnerability.get("severity"),
                    )
                )
    return {key: tuple(value) for key, value in index.items()}


def _grype(binary: str, *args: str) -> Any:
    # Never fetch mid-run: a verdict must name the build it was read from, and
    # an auto-update between status and search would make that a lie.
    env = {**os.environ, "GRYPE_DB_AUTO_UPDATE": "false", "GRYPE_CHECK_FOR_APP_UPDATE": "false"}
    command = f"grype {' '.join(args)}"
    try:
        result = subprocess.run(
            [binary, *args, "-o", "json"],
            capture_output=True,
            text=True,
            timeout=GRYPE_TIMEOUT_SECONDS,
            env=env,
            check=False,  # inspected below so the error can name the command
        )
    except subprocess.TimeoutExpired as exc:
        raise GrypeNotAvailable(f"'{command}' timed out after {exc.timeout}s") from exc
    if result.returncode != 0:
        raise GrypeNotAvailable(f"'{command}' failed: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GrypeNotAvailable(f"'{command}' did not return JSON: {exc}") from exc


def load_database(ecosystems: Iterable[str] = ("python",)) -> Database:
    """Read every advisory for ``ecosystems`` out of Grype's local database."""
    binary = shutil.which("grype")
    if not binary:
        raise GrypeNotAvailable(INSTALL_HINT)
    status = _grype(binary, "db", "status")
    if not status.get("valid") or not status.get("built"):
        raise GrypeNotAvailable(
            f"grype's database at {status.get('path')} is missing or invalid; "
            "run 'grype db update' once to fetch it"
        )
    index: dict = {}
    for ecosystem in ecosystems:
        records = _grype(binary, "db", "search", "--ecosystem", ecosystem, "--limit", "0")
        index.update(parse_search(records, ecosystem))
    return Database(built=status["built"], schema=status.get("schemaVersion", ""), ranges=index)


def render(findings: list[Finding], database: Database) -> str:
    counts = defaultdict(int)
    for f in findings:
        counts[f.verdict] += 1
    lines = [
        f"grype database {database.schema}, built {database.built}",
        f"{len(findings)} declarations: "
        + ", ".join(f"{counts[v]} {v.value}" for v in Verdict),
    ]
    for f in findings:
        p = f.package
        where = f"{p.source_file}: {p.declared_line or p.name}"
        if f.verdict in AFFECTED:
            lines.append(f"  {f.verdict.value:14} {where}")
            for m in f.matched:
                fixed = f"fixed in {m.fixed}" if m.fixed else m.fix_state
                ids = ", ".join((m.advisory, *m.aliases))
                lines.append(f"      {ids}: affected {m.constraint}, {fixed}")
        elif f.verdict is Verdict.DECLINED:
            lines.append(f"  {f.verdict.value:14} {where} ({f.reason})")
        elif f.verdict is Verdict.NONE_AFFECTED and not f.advisories_known:
            lines.append(f"  {f.verdict.value:14} {where} (no advisories known as of {f.db_built})")
        else:
            n = f.advisories_known
            known = f"{n} {'advisory' if n == 1 else 'advisories'} known" if n else "none known"
            lines.append(f"  {f.verdict.value:14} {where} ({known})")
    return "\n".join(lines)
