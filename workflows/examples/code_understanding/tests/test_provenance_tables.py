"""Folding duplicate edges that grounding's renames create.

graphrag 2.6.0's ``finalize_relationships`` drops duplicate ``(source,
target)`` rows without resetting the index, then assigns ``combined_degree``
from a freshly indexed Series. The labels misalign: tail rows get NaN, which
local search cannot read, and the rows between get another row's degree. A raw
extraction never has duplicates, so only grounding can trigger it.
"""

import unittest

from provenance.grounding_report import GroundingReport
from provenance.tables import merge_duplicate_edges


def edges(rows):
    import pandas as pd

    return pd.DataFrame(rows, columns=["source", "target", "description", "weight", "text_unit_ids"])


class TestMergeDuplicateEdges(unittest.TestCase):
    def test_a_renamed_pair_folds_into_one_row(self):
        """Verbatim shape from the celery 120b arm, after ``..utils`` resolved."""
        rows = [
            ("A", "B", "a CALLS ..utils.b", 8.0, ["u1"]),
            ("X", "Y", "x USES y", 5.0, ["u9"]),
            ("A", "B", "a CALLS celery.utils.b", 7.0, ["u2", "u1"]),
        ]
        report = GroundingReport()
        out = merge_duplicate_edges(edges(rows), report)

        self.assertEqual(report.duplicate_edges_merged, 1)
        self.assertEqual(list(zip(out["source"], out["target"], strict=True)), [("A", "B"), ("X", "Y")])
        merged = out.iloc[0]
        self.assertEqual(merged["description"], "a CALLS ..utils.b\na CALLS celery.utils.b")
        self.assertEqual(list(merged["text_unit_ids"]), ["u1", "u2"])
        self.assertEqual(merged["weight"], 15.0)
        self.assertEqual(list(out.index), [0, 1])

    def test_orientation_counts_as_finalize_relationships_does(self):
        rows = [("A", "B", "a CALLS b", 1.0, ["u1"]), ("B", "A", "b CALLS a", 1.0, ["u1"])]
        report = GroundingReport()
        out = merge_duplicate_edges(edges(rows), report)
        self.assertEqual(len(out), 2)
        self.assertEqual(report.duplicate_edges_merged, 0)

    def test_no_duplicates_returns_the_frame_untouched(self):
        frame = edges([("A", "B", "d", 1.0, ["u1"])])
        self.assertIs(merge_duplicate_edges(frame, GroundingReport()), frame)

    def test_the_finalize_step_no_longer_misaligns(self):
        """``drop_duplicates`` then assign from a 0..n-1 Series, as graphrag does."""
        import pandas as pd

        rows = [("A", "B", "1", 1.0, ["u1"]), ("A", "B", "2", 1.0, ["u2"]), ("C", "D", "3", 1.0, ["u3"])]
        out = merge_duplicate_edges(edges(rows), GroundingReport())
        final = out.drop_duplicates(subset=["source", "target"])
        final = final.assign(combined_degree=pd.Series(range(len(final))))
        self.assertFalse(final["combined_degree"].isna().any())


if __name__ == "__main__":
    unittest.main()
