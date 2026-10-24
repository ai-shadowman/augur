"""The deterministic half of the report's Known Vulnerabilities section.

``migration-report/3.txt`` keeps one question for the model, about weaknesses
in the code itself, which nothing here can answer. The two questions that have
a source leave the prompt and are answered here instead: which declared
dependency versions fall inside a known advisory range (:mod:`provenance.vulns`)
and which runtime components are past end of life (:mod:`provenance.eol`).

The block is appended after the model's answer rather than replacing it, and
it is never silently omitted. Whatever input is missing, the block says what
was not assessed and why, because an absent table reads as "nothing found".

It also checks the model's half. Any advisory identifier the answer names that
the join did not produce is listed as unverified, which is the exit code for
"no advisory ID in the report came from a model".
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from collections.abc import Iterable

from . import eol, vulns
from .models import Package

#: CVE, GitHub and PyPA advisory identifiers, as a model would write them.
ADVISORY_ID = re.compile(
    r"\b(CVE-\d{4}-\d{4,}|GHSA(?:-[23456789cfghjmpqrvwx]{4}){3}|PYSEC-\d{4}-\d+)\b",
    re.IGNORECASE,
)

#: Verdicts in the order a reader needs them: what is affected, then what
#: could not be judged, then what was judged clean.
_ORDER = (
    vulns.Verdict.ALL_AFFECTED, vulns.Verdict.SOME_AFFECTED, vulns.Verdict.DECLINED,
    vulns.Verdict.UNBOUNDED, vulns.Verdict.NONE_AFFECTED,
)


def _cell(text: object) -> str:
    return str(text).replace("|", r"\|").replace("\n", " ")


def _detail(finding: vulns.Finding) -> str:
    if finding.verdict in vulns.AFFECTED:
        parts = []
        for m in finding.matched:
            ids = m.advisory + (f" ({', '.join(m.aliases)})" if m.aliases else "")
            fixed = f"fixed in {m.fixed}" if m.fixed else m.fix_state
            parts.append(f"{ids}: affected `{m.constraint}`, {fixed}")
        return "; ".join(parts)
    if finding.verdict is vulns.Verdict.DECLINED:
        return finding.reason or ""
    n = finding.advisories_known
    known = f"{n} {'advisory' if n == 1 else 'advisories'} known"
    if finding.verdict is vulns.Verdict.UNBOUNDED:
        return ("no version constraint declared, so any version may install; "
                + (f"{known} for the package" if n else "no advisories known"))
    if not n:
        return "no advisories known"
    return f"{known}, none in the declared range"


def advisories(packages: list[Package] | None, database: vulns.Database | None) -> tuple[str, set[str]]:
    """The dependency table, and every identifier it cites."""
    lines = ["#### Declared dependencies against known advisories", ""]
    if packages is None:
        lines.append("Not assessed: no dependency facts reached this stage of the pipeline.")
        return "\n".join(lines), set()
    if database is None:
        lines.append("Not assessed: no advisory snapshot was available to this run.")
        return "\n".join(lines), set()

    findings = sorted(vulns.assess_all(packages, database), key=lambda f: _ORDER.index(f.verdict))
    cited = {i for f in findings for m in f.matched for i in (m.advisory, *m.aliases)}
    counts = Counter(f.verdict for f in findings)
    lines += [
        f"Joined against Grype's advisory database (schema {database.schema}, built "
        f"{database.built}) by intersecting each declared version range with the affected "
        "ranges on record. No identifier in this table came from a model.",
        "",
        f"{len(findings)} declarations: "
        + ", ".join(f"{counts[v]} {v.value}" for v in _ORDER) + ".",
    ]
    if findings:
        lines += ["", "| Declaration | Declared in | Verdict | Detail |", "|---|---|---|---|"]
        for f in findings:
            p = f.package
            lines.append(
                f"| `{_cell(p.declared_line or p.name)}` | `{_cell(p.source_file)}` "
                f"| {f.verdict.value} | {_cell(_detail(f))} |"
            )
    return "\n".join(lines), cited


def end_of_life(runtime: str | None, table: eol.Table | None, today: dt.date) -> str:
    lines = ["#### Runtime end-of-life", ""]
    parts = eol.components(runtime)
    if table is None:
        lines.append("Not assessed: the lifecycle table could not be read.")
        return "\n".join(lines)
    if not parts:
        lines.append("Not assessed: the document headers record no runtime.")
        return "\n".join(lines)
    lines += [
        f"Looked up in AUGUR's lifecycle table, retrieved {table.retrieved} from endoflife.date, "
        f"for the runtime the document headers record. Status as of {today.isoformat()}.",
        "",
        "| Component | Lifecycle | End of life | Status |",
        "|---|---|---|---|",
    ]
    for a in (table.assess(c, today) for c in parts):
        lines.append(
            f"| {_cell(a.component)} | {_cell(a.lifecycle or '')} "
            f"| {a.eol or ''} | {a.status.value} |"
        )
    return "\n".join(lines)


def unverified(answer: str, cited: Iterable[str]) -> list[str]:
    """Advisory identifiers the answer names that the join did not produce."""
    known = {c.upper() for c in cited}
    found = dict.fromkeys(m.upper() for m in ADVISORY_ID.findall(answer or ""))
    return [i for i in found if i not in known]


def render(answer: str, packages: list[Package] | None, database: vulns.Database | None,
           runtime: str | None, table: eol.Table | None, today: dt.date) -> str:
    """The block appended after the model's answer."""
    table_text, cited = advisories(packages, database)
    blocks = [table_text, end_of_life(runtime, table, today)]
    stray = unverified(answer, cited)
    if stray:
        blocks.append(
            f"_The answer above names {', '.join(stray)}, which the advisory join did not "
            "produce; treat it as unverified._"
        )
    return "\n\n".join(blocks)
