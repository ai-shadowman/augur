"""What the grounding pass changed, and the summary a human reads.

Kept apart from :mod:`provenance.graph` so the passes there read as the rules
they apply. This module knows nothing about pandas and nothing about how a
correction was decided; it records decisions and formats them.

Every list here is a decision the pass declined to make silently. A merged
group names what it absorbed, an ungrounded module names itself rather than
disappearing, and an ambiguous symbol is reported instead of guessed. The
report is the only place those survive, because the corrected tables by
construction no longer show what they used to say.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class GroundingReport:
    """What the reconciliation changed, in a form a log or a test can read."""

    entities_before: int = 0
    entities_after: int = 0
    relationships_before: int = 0
    relationships_after: int = 0
    #: ``canonical title <- [absorbed titles]`` for each merged group.
    merged: list[tuple[str, list[str]]] = field(default_factory=list)
    #: Corpus files that had no entity at all and were created.
    created: list[str] = field(default_factory=list)
    #: Module-shaped entities naming no indexed file. Reported, not deleted:
    #: they carry relationships, and a name the corpus cannot confirm is not
    #: automatically a name that is wrong.
    ungrounded_modules: list[str] = field(default_factory=list)
    #: ``original title -> resolved spelling`` for each leading-dot name the
    #: dot arithmetic settled.
    relative_resolved: list[tuple[str, str]] = field(default_factory=list)
    #: Leading-dot names left alone, because no producing document was known
    #: and the corpus offered no single answer.
    relative_unresolved: list[str] = field(default_factory=list)
    #: ``wrong title -> corrected title`` for each class or function entity
    #: moved onto the indexed module that actually declares it.
    symbols_refiled: list[tuple[str, str]] = field(default_factory=list)
    #: Misattributed symbols left as the model wrote them because more than
    #: one indexed module declares the name. Reported rather than guessed.
    symbols_ambiguous: list[str] = field(default_factory=list)
    repository: str | None = None
    repository_absorbed: list[str] = field(default_factory=list)
    belongs_to_added: int = 0
    self_loops_removed: int = 0
    edges_rewritten: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _section(lines: list[str], heading: str, items, limit: int, show) -> None:
    """One truncated block, so a long list says how long rather than filling the log."""
    lines.append(heading)
    lines.extend(show(item) for item in items[:limit])
    if len(items) > limit:
        lines.append(f"  ... and {len(items) - limit} more")


def render(report: GroundingReport, limit: int = 15) -> str:
    """Human-readable summary, for a pipeline log or a terminal."""
    lines = [
        f"entities:      {report.entities_before} -> {report.entities_after}",
        f"relationships: {report.relationships_before} -> {report.relationships_after}",
        f"repository:    {report.repository or '(none)'}",
    ]
    if report.repository_absorbed:
        lines.append(f"  absorbed:    {', '.join(report.repository_absorbed)}")
    if report.merged:
        _section(
            lines,
            f"merged:        {len(report.merged)} duplicate group(s)",
            report.merged, limit,
            lambda pair: f"  {pair[0]} <- {', '.join(a for a in pair[1] if a)}",
        )
    if report.created:
        lines.append(f"created:       {len(report.created)} entity(ies) with no extraction")
        lines.extend(f"  {t}" for t in report.created[:limit])
    if report.relative_resolved:
        _section(
            lines,
            f"relative imports resolved: {len(report.relative_resolved)}",
            report.relative_resolved, limit,
            lambda pair: f"  {pair[0]} -> {pair[1]}",
        )
    if report.relative_unresolved:
        _section(
            lines,
            f"relative imports left alone: {len(report.relative_unresolved)} "
            "(no producing document and no unique corpus answer)",
            report.relative_unresolved, limit, lambda t: f"  {t}",
        )
    if report.symbols_refiled:
        _section(
            lines,
            f"symbols refiled: {len(report.symbols_refiled)} onto the indexed "
            "module that declares them",
            report.symbols_refiled, limit,
            lambda pair: f"  {pair[0]} -> {pair[1]}",
        )
    if report.symbols_ambiguous:
        _section(
            lines,
            f"symbols left alone: {len(report.symbols_ambiguous)} "
            "(several indexed modules declare the name)",
            report.symbols_ambiguous, limit, lambda t: f"  {t}",
        )
    if report.ungrounded_modules:
        _section(
            lines,
            f"ungrounded:    {len(report.ungrounded_modules)} module-shaped "
            "entity(ies) naming no indexed file",
            report.ungrounded_modules, limit, lambda t: f"  {t}",
        )
    lines.append(f"BELONGS_TO added: {report.belongs_to_added}")
    lines.append(f"self-loops dropped: {report.self_loops_removed}")
    lines.append(f"edge endpoints rewritten: {report.edges_rewritten}")
    return "\n".join(lines)
