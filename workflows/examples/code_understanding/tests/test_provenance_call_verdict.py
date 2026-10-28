"""The CALLS verdict and its evidence, on constructed checkouts.

Each case is one rule from :mod:`provenance.call_verdict`, chosen where the
celery reading found the rule necessary: a bare title grounding on an import
binding, a settled caller that still reaches a class by name, a property read,
a module claim that must not count its subpackages. The celery arms are scored
by ``augur-test/harness/call_reach.py`` and must agree with this module row
for row; MANIFEST "Relationship layer Phase 8".
"""

import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from provenance import edge_types
from provenance.call_evidence import CallEvidence
from provenance.call_verdict import verdict
from provenance.edge_evidence import EdgeEvidence

SOURCE, TARGET = edge_types.SOURCE, edge_types.TARGET

CHECKOUT = {
    "pkg/__init__.py": "",
    "pkg/util.py": """\
        import importlib
        def helper(): pass
        def load(name, imp=None):
            imp = imp or importlib.import_module
            return imp(name)
        """,
    "pkg/sub/__init__.py": "",
    "pkg/sub/deep.py": "def deep(): pass\n",
    "pkg/core.py": """\
        from pkg.util import helper, load
        from pkg.sub.deep import deep
        def settled():
            helper()
        def loose(x):
            x.go()
        def by_name():
            load("pkg.other:Thing")
        def chain():
            settled()
        def hands_on(run):
            run(helper)
        def hands_back():
            return (helper, ())
        def deco(f): return f
        @deco
        def wrapped(): pass
        class C(object):
            @property
            def p(self): return 1
            def __getitem__(self, k): pass
            def reads(self):
                return self.p
            def index(self):
                return self[0]
        """,
    "lib/thing.rb": "class Thing\nend\n",
    "pkg/top.py": "from pkg.sub.deep import deep\ndef f():\n    deep()\n",
}

#: ``(case, source title, target title, subject, verdict, reason)``.
CASES = [
    ("exact", "PKG.CORE.SETTLED", "PKG.UTIL.HELPER", SOURCE, "confirmed", "exact"),
    ("a bare title follows the import binding", "PKG.CORE.SETTLED", "HELPER", SOURCE, "confirmed", "exact"),
    ("contained", "PKG.CORE", "PKG.UTIL.HELPER", SOURCE, "confirmed", "contained"),
    ("container: a member of the module", "PKG.CORE.SETTLED", "PKG/UTIL.PY", SOURCE, "confirmed", "container"),
    ("a decorator, filed under what it wraps", "PKG.CORE.WRAPPED", "PKG.CORE.DECO", SOURCE, "confirmed", "binds"),
    ("reversed", "PKG.UTIL.HELPER", "PKG.CORE.SETTLED", SOURCE, "contradicted", "reversed"),
    ("transitive", "PKG.CORE.CHAIN", "PKG.UTIL.HELPER", SOURCE, "contradicted", "transitive"),
    ("no edge from a settled caller", "PKG.CORE.SETTLED", "PKG.SUB.DEEP.DEEP", SOURCE, "contradicted", "no-edge"),
    ("an unsettled site is not evidence of absence", "PKG.CORE.LOOSE", "PKG.UTIL.HELPER", SOURCE,
     "unchecked", "unsettled"),
    ("a settled caller that imports by name", "PKG.CORE.BY_NAME", "PKG.UTIL.HELPER", SOURCE,
     "unchecked", "imports-by-name"),
    ("a property read", "PKG.CORE.C.READS", "PKG.CORE.C.P", SOURCE, "out-of-reach", "property"),
    ("a protocol call", "PKG.CORE.C.INDEX", "PKG.CORE.C.__GETITEM__", SOURCE, "out-of-reach", "protocol"),
    ("a callable passed, not called", "PKG.CORE.HANDS_ON", "PKG.UTIL.HELPER", SOURCE, "out-of-reach", "reference"),
    ("a callable returned by a claimant that makes no call", "PKG.CORE.HANDS_BACK", "PKG.UTIL.HELPER", SOURCE,
     "out-of-reach", "reference"),
    ("another dialect's file", "PKG.CORE.SETTLED", "LIB/THING.RB", SOURCE, "out-of-reach", "dialect"),
    ("ungrounded", "PKG.CORE.SETTLED", "PKG.NOWHERE.THING", SOURCE, "out-of-reach", "ungrounded"),
    # ``pkg.top`` calls ``pkg.sub.deep.deep``; that is not a call to ``pkg``.
    ("a package does not contain its subpackages' calls", "PKG/__INIT__.PY", "PKG.SUB.DEEP.DEEP", SOURCE,
     "contradicted", "no-calls"),
    ("the subject decides the claimant", "PKG.UTIL.HELPER", "PKG.CORE.SETTLED", TARGET, "confirmed", "exact"),
]


class TestCallVerdict(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, text in CHECKOUT.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(textwrap.dedent(text))
            cls.ev = EdgeEvidence.from_checkout(root)
            cls.cev = CallEvidence.from_checkout(root)

    def test_cases(self):
        for case, source, target, subject, expected, reason in CASES:
            with self.subTest(case):
                self.assertEqual(verdict(self.ev, self.cev, source, target, subject), (expected, reason))

    def test_survives_the_trip(self):
        data = self.cev.to_dict()
        again = CallEvidence.from_dict(json.loads(json.dumps(data)))
        self.assertEqual(again.to_dict(), data)
        for case, source, target, subject, expected, reason in CASES:
            with self.subTest(case):
                self.assertEqual(verdict(self.ev, again, source, target, subject), (expected, reason))

    def test_merge_unions_two_repositories(self):
        other = CallEvidence(internal={"x.f": {"x.g"}}, unsettled={"x.f": 2}, properties={"x.c.p"}).to_dict()
        merged = CallEvidence.from_dict(CallEvidence.merge([self.cev.to_dict(), other]))
        self.assertEqual(merged.internal["x.f"], {"x.g"})
        self.assertEqual(merged.unsettled["x.f"], 2)
        self.assertIn("x.c.p", merged.properties)
        self.assertIn("pkg.core.c.p", merged.properties)

    def test_old_facts_read_as_empty(self):
        self.assertFalse(CallEvidence.from_dict(None))
        self.assertFalse(CallEvidence.from_dict({}))


if __name__ == "__main__":
    unittest.main()
