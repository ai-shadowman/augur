"""Row merging and edge repointing, which every grounding pass does the same way.

:mod:`provenance.graph` corrects the graph in four passes, and each one reaches
the same two operations: fold several rows that turned out to name one thing
into a single survivor, then point the relationship table at the titles that
survived. The rules differ between passes. The mechanics do not, and keeping
them here is what lets each pass read as the rule it applies rather than as the
pandas it applies it with.

Two shapes make this fiddlier than it looks. A column can be absent entirely,
because the stand-in tables a test builds carry only what the case needs and a
real extraction carries more. And a column that is present can hold NaN rather
than ``None``, because creating a row for an unextracted file widens the frame
and ``concat`` fills the new cells. Both spellings of absent are recognised
here once, instead of at every call site.
"""

from __future__ import annotations

from typing import Any

from .grounding_report import GroundingReport


def types_of(frame):
    """Lowercased, stripped type column, or empty strings if there is none."""
    if "type" not in frame:
        return frame.index.map(lambda _: "")
    return frame["type"].fillna("").astype(str).str.strip().str.lower()


def _missing(value) -> bool:
    """``None`` or a float NaN, which is what a widened column holds.

    Creating an entity for an unextracted file adds columns the extracted
    rows never had, and ``concat`` fills those with NaN. Both spellings of
    absent have to be recognised or the next merge raises on one of them.
    """
    return value is None or (isinstance(value, float) and value != value)


def _as_count(value) -> int:
    return 0 if _missing(value) else int(value or 0)


def _union_ids(values) -> list:
    """Union of several ``text_unit_ids`` cells, order-preserving."""
    seen: dict[Any, None] = {}
    for value in values:
        if _missing(value):
            continue
        try:
            items = list(value)
        except TypeError:
            items = [value]
        for item in items:
            seen.setdefault(item, None)
    return list(seen)


def _pick_description(frame, rows) -> str:
    """The longest description in the group.

    Merging is lossy by nature and any rule here is a judgement, so the rule is
    at least stated: longest wins, because the duplicate spellings are usually
    a full description beside a one-line stub. Every absorbed title is named in
    the report, so what was dropped stays recoverable from the pre-stitch table.
    """
    if "description" not in frame:
        return ""
    texts = [str(frame.at[i, "description"] or "") for i in rows]
    return max(texts, key=len) if texts else ""


def collapse(frame, rows, title: str, entity_type: str | None):
    """Fold ``rows`` into the first one, retitled, and drop the rest."""
    keep = rows[0]
    frame.at[keep, "title"] = title
    if entity_type is not None and "type" in frame:
        frame.at[keep, "type"] = entity_type
    if "description" in frame:
        frame.at[keep, "description"] = _pick_description(frame, rows)
    if "text_unit_ids" in frame:
        frame.at[keep, "text_unit_ids"] = _union_ids(
            frame.at[i, "text_unit_ids"] for i in rows
        )
    if "frequency" in frame:
        frame.at[keep, "frequency"] = sum(_as_count(frame.at[i, "frequency"]) for i in rows)
    return keep


def _flatten(renames: dict[str, str]) -> dict[str, str]:
    """Collapse rename chains so a single lookup lands on the surviving title.

    A relative name is renamed twice: the dot arithmetic turns ``.STATE`` into
    ``CELERY.WORKER.STATE``, and the merge then folds that into the header
    spelling ``CELERY/WORKER/STATE.PY``. One lookup would leave the edge
    pointing at the middle name, which by that point is a row that has been
    dropped.
    """
    flat: dict[str, str] = {}
    for start, first in renames.items():
        seen = {start}
        target = first
        while target not in seen and (nxt := renames.get(target)) is not None:
            seen.add(target)
            target = nxt
        flat[start] = target
    return flat


def rewrite_edges(relationships, renames: dict[str, str], report: GroundingReport):
    """Point every edge at the surviving titles and drop the self-loops that creates."""
    if relationships is None or not len(relationships) or not renames:
        return relationships
    renames = _flatten(renames)
    before_source = relationships["source"].astype(str)
    before_target = relationships["target"].astype(str)
    relationships["source"] = before_source.map(lambda t: renames.get(t, t))
    relationships["target"] = before_target.map(lambda t: renames.get(t, t))
    report.edges_rewritten += int(
        (relationships["source"] != before_source).sum()
        + (relationships["target"] != before_target).sum()
    )
    loops = relationships["source"] == relationships["target"]
    if loops.any():
        report.self_loops_removed += int(loops.sum())
        relationships = relationships[~loops].reset_index(drop=True)
    return relationships


def document_for(cell, text_unit_to_document: dict[str, str]) -> str | None:
    """The document a row's text units came from, if they agree on one.

    An entity merged across chunks of two different files has no single
    producing document, and guessing one would resolve the dots against the
    wrong package. Disagreement falls through to the corpus-wide fallback.
    """
    if _missing(cell) or not text_unit_to_document:
        return None
    try:
        units = list(cell)
    except TypeError:
        units = [cell]
    found = {d for u in units if (d := text_unit_to_document.get(str(u)))}
    return found.pop() if len(found) == 1 else None


def text_unit_documents(text_units) -> dict[str, str]:
    """Map each text unit id to the single document it belongs to."""
    if text_units is None or not len(text_units):
        return {}
    if "id" not in text_units or "document_ids" not in text_units:
        return {}
    mapping: dict[str, str] = {}
    for unit_id, document_ids in zip(
        text_units["id"].astype(str), text_units["document_ids"], strict=False
    ):
        if _missing(document_ids):
            continue
        try:
            ids = list(document_ids)
        except TypeError:
            ids = [document_ids]
        if len(ids) == 1:
            mapping[unit_id] = str(ids[0])
    return mapping
