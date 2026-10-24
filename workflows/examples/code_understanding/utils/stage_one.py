"""Whether stage one asks a model anything, read in one place.

Stage one makes no model call unless ``AUGUR_STAGE_ONE_PURPOSE`` is set. The
one answer left to ask for was ``Purpose:``, and Arm A (2026-10-24, celery
full, gpt-oss-120b) measured a header carrying it against the same header
without: worse symbol precision on two graders, 121 fewer declared symbols
after the join, and no gain outside the band registered in advance as not
counting. The prompt is kept rather than deleted because what it might help,
the descriptions a report is written from, was not graded. Figures in
``provenance/arms.py``.

It lives under ``utils`` rather than in ``pipelines.base.data_generation``
because the token accounting reads it too, and must not reconstruct usage
for calls that were never made.
"""

import os

PURPOSE_ENV = "AUGUR_STAGE_ONE_PURPOSE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def purpose_enabled() -> bool:
    """Whether stage one asks a model for each file's ``Purpose:`` line."""
    return os.getenv(PURPOSE_ENV, "").strip().lower() in _TRUTHY
