"""The report's provenance section, stitched from ``output/edge_verdicts``.

The report should say how much of the graph it stands on has been checked,
and a model has nothing to add to that: every figure is a count over one
table. So this renders the table into markdown and makes no call. Every
count is derived from the table alone, never from ``GroundingReport``, so the
section has one source and a test can recompute it.

The table holds one row per finalised relationship plus one per row
grounding dropped (``dropped`` set). A row AUGUR wrote from a join has the
verdict ``written`` and is counted apart from the model's claims.

It is appended by ``DependencyAnalyzer.generate_migration_report`` beside
the metrics sections. Once ``feat/deterministic-first-cve`` merges, it
should move into a ``render:`` prompt (``DependencyAnalyzer._render_section``)
rather than stay a code-built append; report prompts are found by count, so
it was deliberately not given a numbered prompt file here.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from . import edge_types
from .edge_verdict import (
    CHECKABLE,
    CONFIRMED,
    CONTRADICTED,
    DROPPED_COLUMN,
    DROPPED_REASONS,
    OUT_OF_REACH,
    REASON_COLUMN,
    UNCHECKED,
    VERDICT_COLUMN,
    VERDICTS,
    WRITTEN,
)

TITLE = "## Provenance of the relationships"
TABLE_FILE = "edge_verdicts.parquet"

#: ``out-of-reach`` reasons, as ``edge_verdict`` writes them.
UNCHECKED_TYPE, UNGROUNDED = "type", "ungrounded"

#: Longest description quoted for a listed row.
DESCRIPTION_LIMIT = 160

NOT_RUN = (
    f"{TITLE}\n\n"
    "The edge verdict did not run for this index, so no relationship in the "
    "graph has been checked against the source. Every relationship in it is "
    "the extraction model's claim.\n"
)

INCOMPLETE = (
    f"{TITLE}\n\n"
    "This index's verdict table holds only the rows grounding dropped: the "
    "workflow that writes the rest did not run, so the counts cannot be "
    "reported. Treat every relationship in the graph as the extraction "
    "model's claim.\n"
)

NO_TYPE = "(none)"


def _cell(value: Any, limit: int | None = None) -> str:
    # A finalised row's description is reached through its id, so the table
    # holds NaN there; only a dropped row carries its own.
    text = "" if value is None or value != value else " ".join(str(value).split())
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text.replace("|", "\\|")


def _share(n: int, total: int) -> str:
    return f"{n / total:.1%}" if total else "n/a"


def _type_order(model: Any) -> list[str]:
    """Checkable types first, then the rest by row count, largest first."""
    counts = Counter(model[edge_types.TYPE_COLUMN])
    rest = sorted((t for t in counts if t not in CHECKABLE), key=lambda t: (-counts[t], t))
    return [t for t in CHECKABLE if t in counts] + rest


def _verdict_table(model: Any) -> list[str]:
    types = _type_order(model)
    pairs = Counter(zip(model[edge_types.TYPE_COLUMN], model[VERDICT_COLUMN], strict=True))
    lines = [
        "| type | " + " | ".join(VERDICTS) + " | total |",
        "|---|" + "---:|" * (len(VERDICTS) + 1),
    ]
    for t in types:
        row = [pairs[(t, v)] for v in VERDICTS]
        lines.append(f"| {t} | " + " | ".join(map(str, row)) + f" | {sum(row)} |")
    totals = [sum(pairs[(t, v)] for t in types) for v in VERDICTS]
    lines.append("| all | " + " | ".join(map(str, totals)) + f" | {sum(totals)} |")
    return lines


def _row_lines(rows: Any) -> list[str]:
    has_description = "description" in rows.columns
    lines = ["| source | type | target | reason | description |", "|---|---|---|---|---|"]
    for _, r in rows.iterrows():
        description = _cell(r["description"], DESCRIPTION_LIMIT) if has_description else ""
        lines.append(
            f"| {_cell(r['source'])} | {_cell(r[edge_types.TYPE_COLUMN])} | {_cell(r['target'])} "
            f"| {_cell(r[REASON_COLUMN])} | {description} |"
        )
    return lines


def _normalised(table: Any) -> tuple[Any, bool]:
    """The table with printable types and reasons and a real boolean flag.

    The flag is ``False`` throughout for a table written before the drop
    existed, which is the second value returned.
    """
    table = table.copy()
    table[edge_types.TYPE_COLUMN] = table[edge_types.TYPE_COLUMN].fillna(NO_TYPE).astype(str)
    table[REASON_COLUMN] = table[REASON_COLUMN].fillna("").astype(str)
    predates_drop = DROPPED_COLUMN not in table.columns
    flag = False if predates_drop else table[DROPPED_COLUMN].map(lambda v: v is True or v == True)  # noqa: E712
    table[DROPPED_COLUMN] = flag
    return table, predates_drop


def render(table: Any) -> str:
    """The section as markdown, or :data:`NOT_RUN` when there is no table."""
    if table is None:
        return NOT_RUN
    table, predates_drop = _normalised(table)
    if len(table) and table[DROPPED_COLUMN].all():
        return INCOMPLETE
    verdicts = table[VERDICT_COLUMN]
    model = table[verdicts != WRITTEN]
    written = int((verdicts == WRITTEN).sum())
    total = len(model)
    split = Counter(model[VERDICT_COLUMN])
    dropped = model[model[DROPPED_COLUMN]]
    kept = model[(model[VERDICT_COLUMN] == CONTRADICTED) & ~model[DROPPED_COLUMN]]
    out_of_reach = model[model[VERDICT_COLUMN] == OUT_OF_REACH]
    by_reason = Counter(out_of_reach[REASON_COLUMN])
    unchecked_types = Counter(out_of_reach.loc[out_of_reach[REASON_COLUMN] == UNCHECKED_TYPE, edge_types.TYPE_COLUMN])

    lines = [
        TITLE,
        "",
        "Built from `output/edge_verdicts` by counting, with no model call. Each "
        "relationship the extraction model wrote was compared with what a parser "
        "reads from the source, where a parser can read it: "
        + " and ".join(CHECKABLE) + " only.",
        "",
        f"The model wrote **{total}** relationships: {split[CONFIRMED]} confirmed "
        f"({_share(split[CONFIRMED], total)}), {split[CONTRADICTED]} contradicted, "
        f"{split[OUT_OF_REACH]} out of reach ({_share(split[OUT_OF_REACH], total)}) and "
        f"{split[UNCHECKED]} unchecked. AUGUR wrote {written} more from the source "
        f"itself ({', '.join(edge_types.AUGUR_WRITTEN)}), which are not claims and are "
        "not judged.",
        "",
        *_verdict_table(model),
        "",
        "**Out of reach means unchecked, not wrong.** "
        f"{by_reason[UNCHECKED_TYPE]} relationships ({_share(by_reason[UNCHECKED_TYPE], total)}) "
        "have a type no parser here checks"
        + (": " + ", ".join(f"{t} {n}" for t, n in sorted(unchecked_types.items(), key=lambda p: (-p[1], p[0])))
           if unchecked_types else "")
        + f". {by_reason[UNGROUNDED]} more have a checkable type but name an endpoint the "
        "checkout does not declare, such as a third-party module the file does not "
        "import. Read these as the model's claims.",
        "",
    ]
    if split[UNCHECKED]:
        lines += [
            f"**Unchecked** ({split[UNCHECKED]}) would have been contradictions, except "
            "that the parser could not resolve, or does not read, an edge beside them, "
            "so it did not say no.",
            "",
        ]
    if len(dropped):
        reasons = Counter(dropped[REASON_COLUMN])
        lines += [
            f"**Dropped from the graph: {len(dropped)}** ("
            + ", ".join(f"{n} {k}" for k, n in sorted(reasons.items()))
            + "). The source contradicts each outright: the pair is related the other "
            "way round (`reversed`), both ends are in one file (`same-file`), or the "
            "files are not related that way at all (`no-edge`).",
            "",
            *_row_lines(dropped),
            "",
        ]
    else:
        lines += [
            "**Dropped from the graph: 0.**"
            + (" This index was built before contradictions were dropped." if predates_drop else ""),
            "",
        ]
    if len(kept):
        outright = set(kept[REASON_COLUMN]) & set(DROPPED_REASONS)
        lines += [
            f"**Contradicted but kept: {len(kept)}.** "
            + ("The source contradicts these as claimed, and the index predates the drop, "
               "so they are still in the graph." if outright else
               "The source relates the pair or their files, just not as claimed, so the "
               "row stays in the graph."),
            "",
            *_row_lines(kept),
            "",
        ]
    return "\n".join(lines)


def section_for(output_dir: str | Path) -> str:
    """Render the section from an index's ``output/`` directory."""
    import pandas as pd

    path = Path(output_dir) / TABLE_FILE
    return render(pd.read_parquet(path) if path.is_file() else None)
