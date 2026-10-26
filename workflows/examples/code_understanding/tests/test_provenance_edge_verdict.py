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
    "pkg/__init__.py": "",
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

    def test_empty_evidence_checks_nothing(self):
        scored = edge_verdict.score(EdgeEvidence.from_dict(None), self._frame([c[:3] for c in CASES]))
        self.assertEqual(edge_verdict.precision(edge_verdict.split(scored)), None)


#: ``run -> (confirmed, contradicted, out-of-reach, unchecked)`` over the
#: model's rows of the grounded arm, as the harness reported them.
PINNED = {
    "parser-full-120b": (761, 14, 3171, 2),
    "parser-full-120b-typed": (769, 15, 2149, 1),
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


if __name__ == "__main__":
    unittest.main()
