"""Mark the model edges no parser checks, in the text the analysis model reads.

The report's provenance section (:mod:`provenance.report_section`) says how
much of the graph is checked, beside the answers. The community-report
prompt and local search read the relationship descriptions themselves, and
there every unchecked claim reads as fact. This appends :data:`TAG` on its
own line to each model row whose verdict is ``out-of-reach`` or
``unchecked``, so the claim and its status arrive together.

``confirmed`` rows, AUGUR's ``written`` rows and any kept ``contradicted``
row are left alone: the tag says no parser checks the edge, and for those
one did, or AUGUR wrote it. No prompt asset changes; the stock prompts read
the tagged data.

Off unless ``AUGUR_EDGE_TAG`` is set, so the untagged arm of the
measurement is the same code with the flag off. MANIFEST has the
registration under "Relationship layer Phase 7".

The tag line carries no relationship type token and no endpoint name, so
:func:`provenance.edge_types.type_of` and
:func:`provenance.edge_types.subject_of` read a tagged row exactly as they
read it untagged, and the verdict workflow after ``finalize_graph`` scores
it the same.
"""
from __future__ import annotations

import os
from typing import Any

TAG_ENV = "AUGUR_EDGE_TAG"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

TAG = "[AUGUR: no parser checks this edge]"

#: The verdicts that get the tag (``provenance.edge_verdict``), named here
#: rather than imported so the two modules do not import each other.
TAGGED_VERDICTS = ("out-of-reach", "unchecked")


def enabled() -> bool:
    """Whether grounding tags the unchecked model edges."""
    return os.getenv(TAG_ENV, "").strip().lower() in _TRUTHY


def _tagged(description: Any) -> str:
    # pandas holds a missing description as None or NaN; neither is text.
    text = description if isinstance(description, str) else ""
    if text.rstrip().endswith(TAG):
        return text
    return f"{text.rstrip()}\n{TAG}" if text.strip() else TAG


def tag(relationships: Any, verdicts: Any) -> tuple[Any, int]:
    """A copy of ``relationships`` with the unchecked rows' descriptions tagged.

    ``verdicts`` is aligned with ``relationships`` row for row (the
    ``verdict`` column of :func:`provenance.edge_verdict.assess`). Returns
    the frame and the number of rows tagged. A row already ending in the tag
    is not tagged twice, so a second grounding pass changes nothing.
    """
    marks = [v in TAGGED_VERDICTS for v in verdicts]
    if len(marks) != len(relationships):
        raise ValueError(
            f"edge tag: {len(marks)} verdict(s) for {len(relationships)} relationship row(s); "
            "the verdicts must be aligned with the rows they score"
        )
    out = relationships.copy()
    descriptions = list(out["description"])
    count = 0
    for i, mark in enumerate(marks):
        if mark:
            descriptions[i] = _tagged(descriptions[i])
            count += 1
    out["description"] = descriptions
    return out, count
