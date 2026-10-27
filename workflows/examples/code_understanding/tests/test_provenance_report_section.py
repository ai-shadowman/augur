"""The report's provenance section counts one table and calls no model.

Every figure is checked against a count taken straight from the
``edge_verdicts`` table it was built from: the four grounded arms under
``augur-test/runs`` and the table the cluster built inside GraphRAG. Frames
are built inside the tests, since ``conftest`` swaps pandas at import time.
"""

from __future__ import annotations

import os
import re
import socket
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from provenance import report_section
from provenance.edge_verdict import CONTRADICTED, OUT_OF_REACH, VERDICTS, WRITTEN

SANDBOX = Path(os.environ.get("AUGUR_SANDBOX", Path.home() / "Developer" / "augur-test"))
RUNS = SANDBOX / "runs"
TABLES = {
    "celery parser full": RUNS / "parser-full-120b-dedup" / "output",
    "celery parser full typed": RUNS / "parser-full-120b-typed-dedup" / "output",
    "commons-lang parser smoke": RUNS / "commons-lang-parser-smoke-120b-dedup" / "output",
    "jekyll parser smoke": RUNS / "jekyll-parser-smoke-120b-dedup" / "output",
    "cluster smoke": RUNS / "cluster-smoke-120b-drop" / "root" / "output",
    "cluster smoke, section run": RUNS / "cluster-smoke-120b-section" / "root" / "output",
}


def _no_network(*_args, **_kwargs):
    raise AssertionError("the provenance section opened a socket")


def _md_table(text: str, header: str) -> list[list[str]]:
    """The body rows of the markdown table whose header row starts ``header``."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(header))
    rows = []
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        rows.append([c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]])
    return rows


class TestSectionAgainstItsTable(unittest.TestCase):
    def test_every_count_matches_the_table(self):
        import pandas as pd

        found = {name: out for name, out in TABLES.items() if (out / report_section.TABLE_FILE).is_file()}
        if not found:
            self.skipTest("needs the grounded arms under augur-test/runs")
        for name, out in found.items():
            with self.subTest(arm=name):
                table = pd.read_parquet(out / report_section.TABLE_FILE)
                with mock.patch.object(socket.socket, "connect", _no_network):
                    text = report_section.section_for(out)

                model = table[table["verdict"] != WRITTEN]
                written = int((table["verdict"] == WRITTEN).sum())
                self.assertIn(f"The model wrote **{len(model)}** relationships", text)
                self.assertIn(f"AUGUR wrote {written} more", text)

                want = Counter(zip(model["type"], model["verdict"], strict=True))
                rows = _md_table(text, "| type |")
                self.assertEqual(rows[-1][0], "all")
                for row in rows[:-1]:
                    counts = dict(zip(VERDICTS, map(int, row[1:-1]), strict=True))
                    self.assertEqual(counts, {v: want[(row[0], v)] for v in VERDICTS}, row[0])
                    self.assertEqual(int(row[-1]), int((model["type"] == row[0]).sum()))
                self.assertEqual({r[0] for r in rows[:-1]}, set(model["type"]))
                self.assertEqual(
                    [int(c) for c in rows[-1][1:]],
                    [int((model["verdict"] == v).sum()) for v in VERDICTS] + [len(model)],
                )

                reach = model[model["verdict"] == OUT_OF_REACH]
                by_type = reach[reach["reason"] == report_section.UNCHECKED_TYPE]
                self.assertIn(f"{len(by_type)} relationships (", text)
                for kind, n in Counter(by_type["type"]).items():
                    self.assertIn(f"{kind} {n}", text)
                ungrounded = int((reach["reason"] == report_section.UNGROUNDED).sum())
                self.assertIn(f". {ungrounded} more have a checkable type", text)

                dropped = model[model["dropped"].astype(bool)]
                self.assertRegex(text, rf"\*\*Dropped from the graph: {len(dropped)}\b")
                kept = model[(model["verdict"] == CONTRADICTED) & ~model["dropped"].astype(bool)]
                listed = _md_table(text, "| source |") if len(dropped) or len(kept) else []
                if len(dropped):
                    self.assertEqual(
                        sorted((r[0], r[1], r[2], r[3]) for r in listed[: len(dropped)]),
                        sorted(zip(dropped["source"], dropped["type"], dropped["target"],
                                   dropped["reason"], strict=True)),
                    )
                if len(kept):
                    self.assertIn(f"**Contradicted but kept: {len(kept)}.**", text)
                else:
                    self.assertNotIn("Contradicted but kept", text)


class TestSectionEdges(unittest.TestCase):
    def test_without_a_table_the_section_says_nothing_was_checked(self):
        self.assertEqual(report_section.section_for(Path("/nonexistent/output")), report_section.NOT_RUN)

    def test_cells_are_escaped_and_missing_descriptions_blank(self):
        import pandas as pd

        table = pd.DataFrame([
            {"id": "1", "source": "A|B", "target": "C", "type": "IMPORTS", "subject": "source",
             "verdict": "contradicted", "reason": "no-edge", "dropped": True,
             "description": "A|B IMPORTS C\nacross two lines"},
            {"id": "2", "source": "D", "target": "E", "type": "INHERITS", "subject": "source",
             "verdict": "contradicted", "reason": "wrong-kind", "dropped": False,
             "description": float("nan")},
            {"id": "3", "source": "F", "target": "G", "type": "DEFINED_IN", "subject": "source",
             "verdict": "written", "reason": None, "dropped": False, "description": None},
        ])
        text = report_section.render(table)
        self.assertIn("| A\\|B | IMPORTS | C | no-edge | A\\|B IMPORTS C across two lines |", text)
        self.assertIn("| D | INHERITS | E | wrong-kind |  |", text)
        self.assertIn("The model wrote **2** relationships", text)
        self.assertIn("AUGUR wrote 1 more", text)
        self.assertNotIn("| nan", text)

    def test_a_table_without_descriptions_still_lists_rows(self):
        import pandas as pd

        table = pd.DataFrame([
            {"id": "1", "source": "A", "target": "B", "type": "IMPORTS", "subject": "source",
             "verdict": "contradicted", "reason": "same-file", "dropped": True},
            {"id": "2", "source": "C", "target": "D", "type": "IMPORTS", "subject": "source",
             "verdict": "confirmed", "reason": "exact", "dropped": False},
        ])
        self.assertIn("| A | IMPORTS | B | same-file |  |", report_section.render(table))

    @staticmethod
    def _row(**over):
        row = {"id": "1", "source": "A", "target": "B", "type": "IMPORTS", "subject": "source",
               "verdict": "confirmed", "reason": "exact", "dropped": False}
        return {**row, **over}

    def test_a_table_from_before_the_drop_renders_with_honest_prose(self):
        import pandas as pd

        table = pd.DataFrame([self._row(), self._row(id="2", verdict="contradicted", reason="no-edge")])
        text = report_section.render(table.drop(columns=["dropped"]))
        self.assertIn("**Dropped from the graph: 0.** This index was built before", text)
        self.assertIn("**Contradicted but kept: 1.** The source contradicts these as claimed", text)

    def test_missing_types_and_reasons_render_rather_than_crash(self):
        import pandas as pd

        table = pd.DataFrame([
            self._row(type=None, verdict="out-of-reach", reason="type"),
            self._row(id="2", type="USES", verdict="out-of-reach", reason="type"),
            self._row(id="3", verdict="contradicted", reason=None, dropped=True),
            self._row(id="4", verdict="contradicted", reason="no-edge", dropped=True),
        ])
        text = report_section.render(table)
        self.assertIn(f"| {report_section.NO_TYPE} | 0 | 0 | 1 | 0 | 1 |", text)
        self.assertIn("**Dropped from the graph: 2**", text)

    def test_a_nan_flag_is_not_a_drop(self):
        import pandas as pd

        table = pd.DataFrame([self._row(dropped=float("nan")),
                              self._row(id="2", verdict="contradicted", reason="wrong-kind")])
        table["dropped"] = table["dropped"].astype(object)
        text = report_section.render(table)
        self.assertIn("**Dropped from the graph: 0.**", text)
        self.assertIn("**Contradicted but kept: 1.**", text)

    def test_a_table_of_dropped_rows_alone_is_incomplete(self):
        import pandas as pd

        table = pd.DataFrame([self._row(verdict="contradicted", reason="no-edge", dropped=True)])
        self.assertEqual(report_section.render(table), report_section.INCOMPLETE)


if __name__ == "__main__":
    unittest.main()
