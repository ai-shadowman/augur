"""Sort every model edge into a four-way verdict against the edge oracle.

The relationship layer's counterpart of the symbol split. Each edge the model
wrote lands in exactly one bucket:

``confirmed``    the oracle states the claimed kind, claimant to object, at
                 the keys (``exact``), between their files (``lifted``), or
                 to an external target the claimant's file imports
                 (``external``).
``contradicted`` both endpoints ground and the oracle does not state it:
                 ``reversed``, ``wrong-kind``, ``same-file`` or ``no-edge``.
``out-of-reach`` an unchecked type, or an endpoint the checkout does not
                 declare and the claimant's file does not import. That is the
                 partial-evidence case, and it is never a contradiction.
``unchecked``    a would-be contradiction beside an unresolved oracle edge:
                 the oracle declined, so it did not say no.

Only ``IMPORTS`` and ``INHERITS`` are checkable, since only those have an
oracle. A row is oriented claimant to object by
:func:`provenance.edge_types.subject_of`; when that says ``unknown`` or
``both`` either orientation is accepted and the reason says ``unoriented``.

The rule was prototyped in ``augur-test/harness/edge_verdict.py`` and scored
there first; MANIFEST has it under "Relationship layer Phase 5
(harness-side)".
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from . import edge_types
from .corpus import dotted_key
from .edge_evidence import EdgeEvidence

CHECKABLE = ("IMPORTS", "INHERITS")
CONFIRMED, CONTRADICTED, OUT_OF_REACH, UNCHECKED = (
    "confirmed", "contradicted", "out-of-reach", "unchecked",
)
VERDICTS = (CONFIRMED, CONTRADICTED, OUT_OF_REACH, UNCHECKED)

#: The columns :func:`score` adds.
VERDICT_COLUMN, REASON_COLUMN = "verdict", "reason"


def _states(ev: EdgeEvidence, kind: str, a: str, b: str, fa: set, fb: set) -> str | None:
    """Where the oracle states ``kind`` from ``a`` to ``b``, or ``None``."""
    if kind in ev.pairs.get((a, b), ()):
        return "exact"
    if kind in {k for x in fa for y in fb for k in ev.internal.get((x, y), ())}:
        return "lifted"
    return None


def verdict(ev: EdgeEvidence, kind: str, source: str, target: str, subject: str) -> tuple[str, str]:
    """``(verdict, reason)`` for one edge of type ``kind``."""
    if kind not in CHECKABLE:
        return OUT_OF_REACH, "type"
    ks, kt = ev.ground(source), ev.ground(target)
    fs, ft = ev.files(ks), ev.files(kt)
    if subject == edge_types.TARGET:
        orders = [(kt, ks, ft, fs, source)]
    elif subject == edge_types.SOURCE:
        orders = [(ks, kt, fs, ft, target)]
    else:
        orders = [(ks, kt, fs, ft, target), (kt, ks, ft, fs, source)]
    unoriented = "" if len(orders) == 1 else ", unoriented"

    # One endpoint grounds: confirmed only if its file imports the other.
    for claimant, obj, fc, _, obj_title in orders:
        if claimant and not obj and kind in ev.external_kinds(fc, dotted_key(obj_title)):
            return CONFIRMED, "external" + unoriented
    if not (ks and kt):
        return OUT_OF_REACH, "ungrounded"

    for claimant, obj, fc, fo, _ in orders:
        if where := _states(ev, kind, claimant, obj, fc, fo):
            return CONFIRMED, where + unoriented
    near = any(ev.unresolved[f] for f in fs | ft)
    claimant, obj, fc, fo, _ = orders[0]
    if len(orders) == 1 and _states(ev, kind, obj, claimant, fo, fc):
        reason = "reversed"
    elif ev.pairs.get((ks, kt)) or ev.pairs.get((kt, ks)) or any(
        ev.internal.get((x, y)) or ev.internal.get((y, x)) for x in fs for y in ft
    ):
        reason = "wrong-kind"
    elif fs & ft:
        reason = "same-file"
    else:
        reason = "no-edge"
    return (UNCHECKED if near else CONTRADICTED), reason


def score(ev: EdgeEvidence, relationships: Any) -> Any:
    """A copy of ``relationships`` with ``type``, ``subject``, ``verdict`` and ``reason``.

    Takes and returns a pandas DataFrame without importing pandas, as
    :mod:`provenance.edge_types` does.
    """
    out = edge_types.with_subject(edge_types.with_type(relationships))
    found = [
        verdict(ev, k, s, t, d)
        for k, s, t, d in zip(out[edge_types.TYPE_COLUMN], out["source"], out["target"],
                              out[edge_types.SUBJECT_COLUMN], strict=True)
    ]
    out[VERDICT_COLUMN] = [v for v, _ in found]
    out[REASON_COLUMN] = [r for _, r in found]
    return out


def split(scored: Any) -> dict[str, int]:
    """Verdict counts, every verdict present."""
    counts = Counter(scored[VERDICT_COLUMN])
    return {v: counts[v] for v in VERDICTS}


def precision(counts: dict[str, int]) -> float | None:
    """confirmed / (confirmed + contradicted), or ``None`` with nothing checked."""
    checked = counts[CONFIRMED] + counts[CONTRADICTED]
    return counts[CONFIRMED] / checked if checked else None
