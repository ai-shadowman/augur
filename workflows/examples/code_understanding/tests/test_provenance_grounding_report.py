"""The grounding report, which is the only record of what was corrected.

The corrected tables no longer show what they used to say, so a decision the
pass declined to make silently survives here or nowhere. That makes the
rendering a correctness concern rather than cosmetics: a correction nobody can
see is indistinguishable from a correction that did not happen.
"""

import unittest

from provenance.grounding_report import GroundingReport, render


class TestRender(unittest.TestCase):
    """Everything the reconciliation decided has to reach the pipeline log.

    A correction nobody can see is indistinguishable from a correction that
    did not happen, and these counters are how a run is reviewed after the
    fact.
    """

    REPORT = GroundingReport(
        entities_before=651,
        entities_after=623,
        merged=[("CELERY/WORKER/STATE.PY", ["CELERY.WORKER.STATE"])],
        created=["CELERY/APP/ABSTRACT.PY"],
        ungrounded_modules=["CELERY.CONCURRENCY.PROCESSES"],
        relative_resolved=[(".STATE", "CELERY.WORKER.STATE")],
        relative_unresolved=["..ABSTRACT"],
        symbols_refiled=[("CELERY.APP.BASE.TASK_ACCEPTED",
                          "CELERY.WORKER.STATE.TASK_ACCEPTED")],
        symbols_ambiguous=["CELERY.UTILS.RESET"],
        repository="CELERY-V2-5-5",
        repository_absorbed=["CELERY"],
    )

    def test_every_decision_appears_in_the_summary(self):
        text = render(self.REPORT)
        for expected in (
            "651 -> 623", "CELERY-V2-5-5", "CELERY",
            ".STATE -> CELERY.WORKER.STATE", "..ABSTRACT",
            "symbols refiled: 1", "CELERY.WORKER.STATE.TASK_ACCEPTED",
            "symbols left alone: 1", "CELERY.UTILS.RESET",
            "CELERY.CONCURRENCY.PROCESSES",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)

    def test_long_lists_are_truncated_rather_than_dumped(self):
        report = GroundingReport(
            symbols_refiled=[(f"A{i}", f"B{i}") for i in range(20)],
        )
        text = render(report, limit=3)
        self.assertIn("... and 17 more", text)
        self.assertNotIn("A19", text)

    def test_an_untouched_graph_says_so_without_empty_sections(self):
        text = render(GroundingReport())
        self.assertIn("repository:    (none)", text)
        for absent in ("merged:", "symbols refiled:", "relative imports"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, text)

