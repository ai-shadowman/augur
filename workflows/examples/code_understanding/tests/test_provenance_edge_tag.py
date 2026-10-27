"""The edge tag: unchecked model rows carry it, nothing else does, and it reads as nothing.

Runs ``edge_verdict.judge`` over the constructed checkout and cases of
``test_provenance_edge_verdict``, with ``AUGUR_EDGE_TAG`` on and off.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from provenance import edge_tag, edge_types, edge_verdict
from provenance.edge_evidence import EdgeEvidence
from tests.test_provenance_edge_verdict import CASES, _checkout

WRITTEN = ("PKG/A.PY", "PKG", "pkg/a.py BELONGS_TO pkg")


def _judge(frame, facts, flag: str | None):
    from provenance.grounding_report import GroundingReport

    report = GroundingReport()
    with patch.dict(os.environ, {edge_tag.TAG_ENV: flag or ""}):
        kept = edge_verdict.judge(frame, facts, report)
    return kept, report


class TestEdgeTag(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from types import SimpleNamespace

        import pandas as pd

        with tempfile.TemporaryDirectory() as tmp:
            _checkout(Path(tmp))
            cls.ev = EdgeEvidence.from_checkout(Path(tmp))
        cls.facts = SimpleNamespace(edge_evidence=cls.ev.to_dict())
        rows = [c[:3] for c in CASES] + [WRITTEN]
        cls.frame = pd.DataFrame(rows, columns=["source", "target", "description"])
        cls.untagged, cls.off_report = _judge(cls.frame, cls.facts, None)
        cls.tagged, cls.report = _judge(cls.frame, cls.facts, "1")

    def test_only_unchecked_model_rows_are_tagged(self):
        want = {c[2] for c in CASES if c[3] in edge_tag.TAGGED_VERDICTS}
        got = {d.removesuffix("\n" + edge_tag.TAG) for d in self.tagged["description"] if d.endswith(edge_tag.TAG)}
        self.assertEqual(got, want)
        self.assertEqual(self.report.edges_tagged, len(want))
        untouched = [c[2] for c in CASES if c[3] in ("confirmed", "contradicted")] + [WRITTEN[2]]
        kept = set(self.tagged["description"])
        for description in untouched:
            if description in set(self.untagged["description"]):
                with self.subTest(description=description):
                    self.assertIn(description, kept)

    def test_the_drop_is_the_same_with_the_tag(self):
        self.assertEqual(self.report.edges_dropped, self.off_report.edges_dropped)
        self.assertEqual(self.report.dropped_edges, self.off_report.dropped_edges)
        self.assertEqual(len(self.tagged), len(self.untagged))

    def test_off_by_default(self):
        self.assertEqual(list(self.untagged["description"]),
                         [d for d in self.frame["description"] if d in set(self.untagged["description"])])
        self.assertEqual(self.off_report.edges_tagged, 0)
        for value, on in (("", False), ("0", False), ("no", False), ("1", True), ("TRUE", True), (" on ", True)):
            with self.subTest(value=value), patch.dict(os.environ, {edge_tag.TAG_ENV: value}):
                self.assertIs(edge_tag.enabled(), on)

    def test_a_tagged_row_scores_as_it_did_untagged(self):
        # The verdict workflow after finalize_graph re-reads the tagged text.
        before = edge_verdict.assess(self.ev, self.untagged)
        after = edge_verdict.assess(self.ev, self.tagged)
        for column in (edge_types.TYPE_COLUMN, edge_types.SUBJECT_COLUMN,
                       edge_verdict.VERDICT_COLUMN, edge_verdict.REASON_COLUMN):
            with self.subTest(column=column):
                self.assertEqual(list(after[column]), list(before[column]))

    def test_tagging_twice_changes_nothing(self):
        verdicts = edge_verdict.assess(self.ev, self.tagged)[edge_verdict.VERDICT_COLUMN]
        again, count = edge_tag.tag(self.tagged, verdicts)
        self.assertEqual(list(again["description"]), list(self.tagged["description"]))
        self.assertEqual(count, self.report.edges_tagged)

    def test_empty_and_missing_descriptions(self):
        import pandas as pd

        frame = pd.DataFrame({"description": ["", None, "x\n"]})
        out, count = edge_tag.tag(frame, ["out-of-reach"] * 3)
        self.assertEqual(list(out["description"]), [edge_tag.TAG, edge_tag.TAG, f"x\n{edge_tag.TAG}"])
        self.assertEqual(count, 3)

    def test_misaligned_verdicts_raise(self):
        with self.assertRaisesRegex(ValueError, "aligned"):
            edge_tag.tag(self.frame, ["out-of-reach"])

    def test_the_report_says_how_many(self):
        from provenance import grounding_report

        self.assertIn(f"tagged:       {self.report.edges_tagged} unchecked row(s)",
                      grounding_report.render(self.report))
        self.assertNotIn("tagged:", grounding_report.render(self.off_report))
