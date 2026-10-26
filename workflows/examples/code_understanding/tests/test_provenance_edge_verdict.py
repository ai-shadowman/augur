"""The edge verdict: one constructed case per reason, and a pin on the celery arms.

The constructed checkout is small enough to read in full, so every expected
verdict below can be checked by eye against it. The pin needs the measured
runs under ``augur-test/runs`` and skips without them.
"""

from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from provenance import edge_verdict
from provenance.edge_evidence import EdgeEvidence

SANDBOX = Path(os.environ.get("AUGUR_SANDBOX", Path.home() / "Developer" / "augur-test"))
RUNS, CELERY = SANDBOX / "runs", SANDBOX / "candidates" / "celery"

#: ``pkg/a.py`` imports ``pkg.b`` and ``os``; ``pkg.e.Sub`` inherits
#: ``pkg.b.B``, which inherits ``pkg.c.C``;
#: ``pkg/c.py`` imports nothing internal; ``pkg/d.py`` has an import the
#: oracle cannot resolve (three dots from a two-deep package).
FILES = {
    "pkg/__init__.py": """
        from .c import C as Base

        VERSION = "1"
    """,
    # Imports the class from where it is declared; the model names the
    # package's re-export of it.
    "pkg/g.py": """
        from pkg.c import C
        from pkg import VERSION
    """,
    "pkg/h.py": """
        from os.path import join

        resource = None

        def f():
            from os import sep
            return sep
    """,
    "pkg/a.py": """
        import os
        from pkg import b

        class A:
            def run(self):
                pass

        class Helper:
            pass
    """,
    "pkg/b.py": """
        from pkg.c import C

        class B(C):
            pass
    """,
    "pkg/c.py": """
        class C:
            pass

        class E(C):
            pass
    """,
    "pkg/e.py": """
        from pkg.b import B

        class Sub(B):
            pass
    """,
    "pkg/r.rb": """
        require_relative "rmod/inner"
    """,
    "pkg/rmod.rb": """
        module Rmod
        end
    """,
    "pkg/rmod/inner.rb": """
        module Rmod
          class Inner
          end
        end
    """,
    # Ruby: StaticDrop's bare ``Drop`` is Jk::Drops::Drop by lexical
    # nesting, not the top-level Drop a benchmark script declares.
    "lib/jk/drops/drop.rb": """
        module Jk
          module Drops
            class Drop
            end
          end
        end
    """,
    "lib/jk/drops/static_drop.rb": """
        module Jk
          module Drops
            class StaticDrop < Drop
            end
          end
        end
    """,
    "bench/top.rb": """
        class Drop
        end
    """,
    "pkg/d.py": """
        from ...nowhere import thing

        class D:
            pass
    """,
}

#: ``(source, target, description, verdict, reason)``. The description names
#: the type and, through its subject, the direction.
CASES = [
    ("PKG/A.PY", "PKG/B.PY", "pkg/a.py IMPORTS pkg.b", "confirmed", "exact"),
    ("PKG.A.A.RUN", "PKG/B.PY", "pkg.a.A.run IMPORTS pkg.b", "confirmed", "lifted"),
    ("PKG/A.PY", "OS", "pkg/a.py IMPORTS os", "confirmed", "external"),
    ("PKG.B.B", "PKG.C.C", "pkg.b.B INHERITS pkg.c.C", "confirmed", "exact"),
    ("PKG.B.B", "PKG.C.C", "related through inheritance INHERITS", "confirmed", "exact, unoriented"),
    ("PKG.E.SUB", "PKG.C.C", "pkg.e.Sub INHERITS pkg.c.C", "confirmed", "transitive"),
    # Importing pkg.b runs pkg/__init__.py, so the package is imported too.
    ("PKG/B.PY", "PKG/__INIT__.PY", "pkg/b.py IMPORTS pkg", "confirmed", "ancestor"),
    ("PKG.E.SUB", "PKG/C.PY", "pkg.e.Sub INHERITS pkg.c", "confirmed", "container"),
    # Ruby's require loads one file, never the directory's namesake.
    ("PKG/R.RB", "PKG/RMOD.RB", "pkg/r.rb IMPORTS pkg/rmod.rb", "contradicted", "no-edge"),
    # A module-level binding, and a re-export chased to its declaring file.
    ("PKG/G.PY", "PKG.VERSION", "pkg/g.py IMPORTS pkg.VERSION", "confirmed", "lifted"),
    ("PKG/G.PY", "PKG.BASE", "pkg/g.py IMPORTS pkg.Base", "confirmed", "lifted"),
    # A bare name is looked up where the claimant uses it, then globally.
    ("JK::DROPS::STATICDROP", "DROP", "Jk::Drops::StaticDrop INHERITS Drop", "confirmed", "exact"),
    ("PKG/G.PY", "C", "pkg/g.py IMPORTS C", "confirmed", "lifted"),
    # An assignment's value is unknown, so a bare name never resolves to one.
    ("PKG/H.PY", "RESOURCE", "pkg/h.py IMPORTS resource", "out-of-reach", "ungrounded"),
    ("PKG/B.PY", "PKG/A.PY", "pkg/b.py IMPORTS pkg.a", "contradicted", "reversed"),
    ("PKG.A.A", "PKG/B.PY", "pkg.a.A INHERITS pkg.b", "contradicted", "wrong-kind"),
    ("PKG.A.A", "PKG.A.HELPER", "pkg.a.A IMPORTS pkg.a.Helper", "contradicted", "same-file"),
    ("PKG/A.PY", "PKG/C.PY", "pkg/a.py IMPORTS pkg.c", "contradicted", "no-edge"),
    ("PKG/D.PY", "PKG/C.PY", "pkg/d.py IMPORTS pkg.c", "unchecked", "no-edge"),
    ("PKG/A.PY", "PKG/B.PY", "pkg/a.py CALLS pkg.b", "out-of-reach", "type"),
    ("PKG/A.PY", "PKG/B.PY", "pkg/a.py talks to pkg.b", "out-of-reach", "type"),
    # Partial evidence: a plausible name inside a real package that nothing
    # declares and the claimant does not import is out of reach, never a
    # contradiction, since the oracle cannot tell invented from unseen.
    ("PKG/A.PY", "PKG.MISSING", "pkg/a.py IMPORTS pkg.missing", "out-of-reach", "ungrounded"),
    ("PKG/A.PY", "REQUESTS", "pkg/a.py IMPORTS requests", "out-of-reach", "ungrounded"),
]


def _checkout(root: Path) -> None:
    for path, body in FILES.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


class TestAncestorRule(unittest.TestCase):
    def test_every_edge_dialect_records_a_rule(self):
        from provenance.edges import RESOLVERS

        self.assertEqual(set(edge_verdict.ANCESTOR_IMPORTS), set(RESOLVERS))

    def test_an_unrecorded_dialect_raises(self):
        from types import SimpleNamespace
        from unittest import mock

        cobol = SimpleNamespace(name="cobol")
        with mock.patch.object(edge_verdict, "dialect_for", return_value=cobol), \
                self.assertRaisesRegex(ValueError, "cobol"):
            edge_verdict.imports_ancestors("x.cbl")


class TestImportBindings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        _checkout(Path(cls._tmp.name))
        cls.ev = EdgeEvidence.from_checkout(Path(cls._tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_where_each_bound_name_grounds(self):
        cases = [
            ("PKG.H.JOIN", {"pkg/h.py"}),  # bound from outside the checkout
            ("PKG.VERSION", {"pkg/__init__.py"}),  # a module-level assignment
            ("PKG.BASE", {"pkg/__init__.py", "pkg/c.py"}),  # binder and declarer
            ("PKG.H.SEP", set()),  # a function's import binds nothing in the module
        ]
        for title, files in cases:
            with self.subTest(title=title):
                self.assertEqual(self.ev.files(self.ev.ground(title)), files)

    def test_every_edge_dialect_records_a_rule(self):
        from provenance.edge_evidence import IMPORT_BINDS, SCOPES
        from provenance.edges import RESOLVERS

        self.assertEqual(set(IMPORT_BINDS), set(RESOLVERS))
        self.assertEqual(set(SCOPES), set(RESOLVERS))


class TestFactsCarryEvidence(unittest.TestCase):
    """The verdict runs where the checkout is gone, so facts must carry it intact."""

    def test_collect_write_read_merge(self):
        from provenance import facts
        from provenance.pipeline import merge_facts

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            _checkout(root)
            collected = facts.collect(root, run_syft=False)
            self.assertEqual(collected.edge_evidence, EdgeEvidence.from_checkout(root).to_dict())
            again = facts.read(facts.write(collected, Path(tmp) / "facts.json"))
            self.assertEqual(again.edge_evidence, collected.edge_evidence)
            merged = merge_facts([again, facts.read(Path(tmp) / "facts.json")], root=tmp)
            self.assertEqual(merged.edge_evidence, collected.edge_evidence)


class TestVerdictByReason(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        _checkout(Path(cls._tmp.name))
        cls.ev = EdgeEvidence.from_checkout(Path(cls._tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _frame(self, rows):
        import pandas as pd

        return pd.DataFrame(rows, columns=["source", "target", "description"])

    def test_each_reason(self):
        scored = edge_verdict.score(self.ev, self._frame([c[:3] for c in CASES]))
        for (source, target, description, want, reason), got, why in zip(
            CASES, scored["verdict"], scored["reason"], strict=True
        ):
            with self.subTest(description=description):
                self.assertEqual((got, why), (want, reason), f"{source} -> {target}")

    def test_split_accounts_for_every_row(self):
        scored = edge_verdict.score(self.ev, self._frame([c[:3] for c in CASES]))
        counts = edge_verdict.split(scored)
        self.assertEqual(sum(counts.values()), len(CASES))
        self.assertEqual(set(counts), set(edge_verdict.VERDICTS))

    def test_round_trip_through_facts_form(self):
        again = EdgeEvidence.from_dict(self.ev.to_dict())
        self.assertEqual(again.to_dict(), self.ev.to_dict())
        scored = edge_verdict.score(again, self._frame([c[:3] for c in CASES]))
        self.assertEqual(list(scored["verdict"]), [c[3] for c in CASES])

    def test_merge_is_a_union(self):
        part = self.ev.to_dict()
        merged = EdgeEvidence.merge([part, part, {}])
        self.assertEqual(merged, part)

    def test_report_accounts_for_every_row(self):
        from types import SimpleNamespace

        from provenance.grounding_report import GroundingReport, render

        rows = [c[:3] for c in CASES] + [("PKG/A.PY", "REPO", "pkg/a.py BELONGS_TO repo")]
        report = GroundingReport()
        edge_verdict.record(self._frame(rows), SimpleNamespace(edge_evidence=self.ev.to_dict()), report)
        self.assertEqual(sum(report.edge_verdicts.values()), len(CASES))
        self.assertEqual(report.edges_written, 1)
        self.assertEqual(sum(report.edge_contradictions.values()), report.edge_verdicts["contradicted"])
        text = render(report)
        self.assertIn(f"edge verdict:  {len(CASES)} model edge(s)", text)
        self.assertIn("1 written row(s) not judged", text)

    def test_without_evidence_the_report_says_it_did_not_run(self):
        from provenance.grounding_report import GroundingReport, render

        report = GroundingReport()
        edge_verdict.record(self._frame([c[:3] for c in CASES]), None, report)
        self.assertEqual(report.edge_verdicts, {})
        self.assertIn("edge verdict:  not run", render(report))

    def test_empty_evidence_checks_nothing(self):
        scored = edge_verdict.score(EdgeEvidence.from_dict(None), self._frame([c[:3] for c in CASES]))
        self.assertEqual(edge_verdict.precision(edge_verdict.split(scored)), None)


#: ``run -> (confirmed, contradicted, out-of-reach, unchecked)`` over the
#: model's rows of the grounded arm, as the harness reported them.
PINNED = {
    "parser-full-120b": (839, 3, 3104, 2),
    "parser-full-120b-typed": (866, 10, 2057, 1),
}


@unittest.skipUnless(CELERY.is_dir() and all((RUNS / f"{r}-dedup").is_dir() for r in PINNED),
                     "needs the celery checkout and the grounded celery arms under augur-test")
class TestCeleryArmsPin(unittest.TestCase):
    """Reproduces ``harness/edge_verdict.py`` on the two celery arms.

    The ``-dedup`` arms are rewritten by every grounding change, so a moved
    count here means rebuilding them with ``apply_provenance.py --refinalise``
    before reading it as a change in the verdict.
    """

    def test_counts(self):
        import pandas as pd

        ev = EdgeEvidence.from_checkout(CELERY)
        for run, want in PINNED.items():
            raw = pd.read_parquet(RUNS / run / "output" / "relationships.parquet")
            rel = pd.read_parquet(RUNS / f"{run}-dedup" / "output" / "relationships.parquet")
            rel = rel[rel["id"].isin(set(raw["id"]))].reset_index(drop=True)
            counts = edge_verdict.split(edge_verdict.score(ev, rel))
            with self.subTest(run=run):
                self.assertEqual(tuple(counts[v] for v in edge_verdict.VERDICTS), want, counts)

    def test_the_product_selects_the_same_model_rows_by_type(self):
        """The harness picks model rows by raw id; the product, before ids exist, by type."""
        from types import SimpleNamespace

        import pandas as pd

        from provenance.grounding_report import GroundingReport

        facts = SimpleNamespace(edge_evidence=EdgeEvidence.from_checkout(CELERY).to_dict())
        for run, want in PINNED.items():
            report = GroundingReport()
            rel = pd.read_parquet(RUNS / f"{run}-dedup" / "output" / "relationships.parquet")
            edge_verdict.record(rel, facts, report)
            with self.subTest(run=run):
                self.assertEqual(tuple(report.edge_verdicts[v] for v in edge_verdict.VERDICTS), want)


if __name__ == "__main__":
    unittest.main()
