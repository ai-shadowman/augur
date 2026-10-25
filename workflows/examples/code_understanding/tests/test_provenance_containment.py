"""Writing where each symbol is defined, rather than asking the model.

Constructed files per registered dialect, because each composes a qualified
name differently and the edge has to land on the title grounding writes:
Python and TypeScript by path, Java by declared package, Ruby by the
``module`` chain the walker already returned. The celery pin at the end is
the truth set on a real checkout; the corpus effect of the pass is in
``augur-test/corpus/MANIFEST.md``, Relationship layer Phase 2b.
"""

import os
import unittest
from pathlib import Path

from provenance import containment, graph
from provenance.corpus import from_documents
from provenance.grounding_report import GroundingReport, render

CANDIDATES = Path(
    os.environ.get("AUGUR_CANDIDATES", Path.home() / "Developer" / "augur-test" / "candidates")
)
CELERY = CANDIDATES / "celery"

_LINE = (
    "This file is located at {path} from repository url https://example.com/r.git, "
    "repository slug r, multi_repo false"
)
#: What ``generate_code_comment`` wraps a header in, per language.
HEADERS = {
    ".py": '"""' + _LINE + '"""\n',
    ".java": "/*\n" + _LINE + "\n*/\n",
    ".ts": "/*\n" + _LINE + "\n*/\n",
    ".rb": "=begin\n" + _LINE + "\n=end\n",
}

PYTHON = """\
class Outer(object):
    def method(self):
        pass

    class Inner(object):
        def deep(self):
            pass


def top():
    pass


CONSTANT = 1
"""
PYTHON_TITLES = [
    "PKG/MOD.PY", "PKG.MOD.OUTER", "PKG.MOD.OUTER.METHOD", "PKG.MOD.OUTER.INNER",
    "PKG.MOD.OUTER.INNER.DEEP", "PKG.MOD.TOP", "PKG.MOD.CONSTANT",
]
PYTHON_PAIRS = {
    ("PKG.MOD.OUTER.METHOD", "PKG.MOD.OUTER"),
    ("PKG.MOD.OUTER.INNER", "PKG.MOD.OUTER"),
    ("PKG.MOD.OUTER.INNER.DEEP", "PKG.MOD.OUTER.INNER"),
    ("PKG.MOD.OUTER", "PKG/MOD.PY"),
    ("PKG.MOD.TOP", "PKG/MOD.PY"),
}


def documents(rows):
    """``(path, body)`` pairs, each under the header its language gets."""
    import pandas as pd

    return pd.DataFrame([
        {"title": path, "text": HEADERS[Path(path).suffix].format(path=path) + body}
        for path, body in rows
    ])


def entities(titles):
    import pandas as pd

    return pd.DataFrame({"title": titles, "type": "", "description": ""})


def edges(rows=()):
    import pandas as pd

    return pd.DataFrame(list(rows), columns=["source", "target", "description", "weight"])


def written(titles, rows, existing=()):
    report = GroundingReport()
    out = containment.synthesize(entities(titles), edges(existing), documents(rows), report)
    added = out.iloc[len(existing):]
    pairs = set(zip(added["source"], added["target"], strict=True))
    assert report.defined_in_added == len(pairs) == len(added), (report.defined_in_added, list(added))
    return pairs, added


class TestWhatGetsWritten(unittest.TestCase):
    def test_python_writes_direct_pairs_only(self):
        """No ``DEEP -> OUTER`` and no ``METHOD -> file``: those are true and
        reachable through the direct pairs. The binding gets nothing."""
        pairs, _ = written(PYTHON_TITLES, [("pkg/mod.py", PYTHON)])
        self.assertEqual(pairs, PYTHON_PAIRS)

    def test_the_row_is_typed_and_weighted_like_the_models(self):
        _, added = written(PYTHON_TITLES, [("pkg/mod.py", PYTHON)])
        row = added[added["source"] == "PKG.MOD.TOP"].iloc[0]
        self.assertEqual(row["description"], "PKG.MOD.TOP DEFINED_IN PKG/MOD.PY")
        self.assertEqual(row["weight"], containment.DEFINED_IN_WEIGHT)
        self.assertEqual(list(row["text_unit_ids"]), [])

    def test_dialects_compose_onto_the_titles_grounding_writes(self):
        cases = [
            ("java", "src/main/java/org/x/Pair.java",
             "package org.x;\npublic class Pair { void accept() {} class In { void f() {} } }\n",
             ["SRC/MAIN/JAVA/ORG/X/PAIR.JAVA", "ORG.X.PAIR", "ORG.X.PAIR.ACCEPT",
              "ORG.X.PAIR.IN", "ORG.X.PAIR.IN.F"],
             {("ORG.X.PAIR", "SRC/MAIN/JAVA/ORG/X/PAIR.JAVA"), ("ORG.X.PAIR.ACCEPT", "ORG.X.PAIR"),
              ("ORG.X.PAIR.IN", "ORG.X.PAIR"), ("ORG.X.PAIR.IN.F", "ORG.X.PAIR.IN")}),
            ("typescript", "src/foo.ts",
             "export class Foo { bar(): void {} }\nexport function baz() {}\n",
             ["SRC/FOO.TS", "SRC.FOO.FOO", "SRC.FOO.FOO.BAR", "SRC.FOO.BAZ"],
             {("SRC.FOO.FOO", "SRC/FOO.TS"), ("SRC.FOO.FOO.BAR", "SRC.FOO.FOO"),
              ("SRC.FOO.BAZ", "SRC/FOO.TS")}),
            # The file declares the whole module chain, so the outermost
            # module is what the file contains.
            ("ruby nested", "lib/jekyll/tags/link.rb",
             "module Jekyll\n  module Tags\n    class Link\n      def render; end\n    end\n  end\nend\n",
             ["LIB/JEKYLL/TAGS/LINK.RB", "JEKYLL", "JEKYLL.TAGS", "JEKYLL.TAGS.LINK",
              "JEKYLL.TAGS.LINK.RENDER"],
             {("JEKYLL", "LIB/JEKYLL/TAGS/LINK.RB"), ("JEKYLL.TAGS", "JEKYLL"),
              ("JEKYLL.TAGS.LINK", "JEKYLL.TAGS"), ("JEKYLL.TAGS.LINK.RENDER", "JEKYLL.TAGS.LINK")}),
            # The compact form opens ``Jekyll::Tags`` without declaring it here,
            # so the class belongs to the file, not to a namespace opened elsewhere.
            ("ruby compact", "lib/jekyll/tags/link.rb",
             "class Jekyll::Tags::Link\n  def render; end\nend\n",
             ["LIB/JEKYLL/TAGS/LINK.RB", "JEKYLL.TAGS", "JEKYLL.TAGS.LINK", "JEKYLL.TAGS.LINK.RENDER"],
             {("JEKYLL.TAGS.LINK", "LIB/JEKYLL/TAGS/LINK.RB"),
              ("JEKYLL.TAGS.LINK.RENDER", "JEKYLL.TAGS.LINK")}),
            # Compact inside a declared module: the walker writes
            # ``Jekyll.Tags::Link``, and ``Jekyll.Tags`` is not declared here,
            # so the file owns it rather than ``Jekyll``.
            ("ruby compact in module", "lib/link.rb",
             "module Jekyll\n  class Tags::Link\n    def render; end\n  end\nend\n",
             ["LIB/LINK.RB", "JEKYLL", "JEKYLL.TAGS", "JEKYLL.TAGS.LINK", "JEKYLL.TAGS.LINK.RENDER"],
             {("JEKYLL", "LIB/LINK.RB"), ("JEKYLL.TAGS.LINK", "LIB/LINK.RB"),
              ("JEKYLL.TAGS.LINK.RENDER", "JEKYLL.TAGS.LINK")}),
        ]
        for name, path, body, titles, expected in cases:
            with self.subTest(name):
                pairs, _ = written(titles, [(path, body)])
                self.assertEqual(pairs, expected)

    def test_an_owner_hidden_by_the_kind_fold_still_owns(self):
        """``symbols`` folds across kinds first-wins, so ``def command`` hides
        ``class Command`` from ``classes`` (celery/bin/celeryctl.py:47 and 52).
        The method still belongs to the class, not to the file."""
        body = "def command(fun):\n    return fun\n\n\nclass Command(object):\n    def run(self):\n        pass\n"
        pairs, _ = written(["PKG/MOD.PY", "PKG.MOD.COMMAND", "PKG.MOD.COMMAND.RUN"], [("pkg/mod.py", body)])
        self.assertIn(("PKG.MOD.COMMAND.RUN", "PKG.MOD.COMMAND"), pairs)
        self.assertNotIn(("PKG.MOD.COMMAND.RUN", "PKG/MOD.PY"), pairs)

    def test_every_spelling_of_a_name_is_linked(self):
        """The entity layer leaves ``::``/``#`` and ``.`` spellings apart (37
        names on jekyll). Both are the declaration; linking one would leave the
        other isolated, chosen by iteration order."""
        body = "module Jekyll\n  class Link\n    def render; end\n  end\nend\n"
        titles = ["LIB/LINK.RB", "JEKYLL", "JEKYLL.LINK", "JEKYLL::LINK", "JEKYLL::LINK#RENDER"]
        pairs, _ = written(titles, [("lib/link.rb", body)])
        self.assertEqual(pairs, {
            ("JEKYLL", "LIB/LINK.RB"), ("JEKYLL.LINK", "JEKYLL"), ("JEKYLL::LINK", "JEKYLL"),
            ("JEKYLL::LINK#RENDER", "JEKYLL.LINK"), ("JEKYLL::LINK#RENDER", "JEKYLL::LINK"),
        })


class TestWhatDoesNot(unittest.TestCase):
    def test_a_pair_already_linked_either_way_round_is_not_repeated(self):
        """The columns carry no direction, so the model's row from class to
        method already states the pair."""
        existing = [("PKG.MOD.OUTER", "PKG.MOD.OUTER.METHOD", "METHOD is a method of OUTER", 10.0)]
        pairs, _ = written(PYTHON_TITLES, [("pkg/mod.py", PYTHON)], existing)
        self.assertEqual(pairs, PYTHON_PAIRS - {("PKG.MOD.OUTER.METHOD", "PKG.MOD.OUTER")})

    def test_no_entity_is_created_and_no_owner_is_skipped_over(self):
        """With ``INNER`` unextracted, ``DEEP`` gets no edge at all, rather than
        one to ``OUTER``, which would be true but not the direct pair."""
        titles = [t for t in PYTHON_TITLES if t != "PKG.MOD.OUTER.INNER"]
        pairs, added = written(titles, [("pkg/mod.py", PYTHON)])
        self.assertEqual(pairs, PYTHON_PAIRS - {
            ("PKG.MOD.OUTER.INNER", "PKG.MOD.OUTER"),
            ("PKG.MOD.OUTER.INNER.DEEP", "PKG.MOD.OUTER.INNER"),
        })
        self.assertTrue(set(added["source"]) | set(added["target"]) <= set(titles))

    def test_a_file_is_not_mistaken_for_a_class_of_the_same_folded_name(self):
        """``CELERY/SCHEDULES/CRONTAB.PY`` (a file the model invented) and the
        class ``celery.schedules.crontab`` fold to one ``canonical_key``. The
        symbol index uses ``dotted_key`` so the class keeps its edge."""
        body = "class crontab(object):\n    def now(self):\n        pass\n"
        titles = ["CELERY/SCHEDULES.PY", "CELERY.SCHEDULES.CRONTAB", "CELERY/SCHEDULES/CRONTAB.PY",
                  "CELERY.SCHEDULES.CRONTAB.NOW"]
        pairs, _ = written(titles, [("celery/schedules.py", body)])
        self.assertEqual(pairs, {
            ("CELERY.SCHEDULES.CRONTAB", "CELERY/SCHEDULES.PY"),
            ("CELERY.SCHEDULES.CRONTAB.NOW", "CELERY.SCHEDULES.CRONTAB"),
        })

    def test_a_repository_typed_row_gets_no_containment_edge(self):
        import pandas as pd

        body = "module Jekyll\n  class A; end\nend\n"
        frame = pd.DataFrame({
            "title": ["LIB/A.RB", "JEKYLL", "JEKYLL.A"],
            "type": ["", "REPOSITORY", "CLASS"],
            "description": "",
        })
        report = GroundingReport()
        out = containment.synthesize(frame, edges(), documents([("lib/a.rb", body)]), report)
        self.assertEqual(report.defined_in_added, 0, list(zip(out["source"], out["target"], strict=True)))

    def test_no_documents_or_no_entities_is_a_no_op(self):
        frame = edges()
        self.assertIs(containment.synthesize(entities(PYTHON_TITLES), frame, None), frame)
        self.assertIs(containment.synthesize(entities([]), frame, documents([("pkg/mod.py", PYTHON)])), frame)

    def test_an_unregistered_language_writes_nothing(self):
        import pandas as pd

        docs = pd.DataFrame([{"title": "a.go", "text": "// " + _LINE.format(path="a.go") + "\nfunc F() {}\n"}])
        report = GroundingReport()
        containment.synthesize(entities(["A.GO", "A.F"]), edges(), docs, report)
        self.assertEqual(report.defined_in_added, 0)


class TestGround(unittest.TestCase):
    def test_ground_writes_the_edges_and_reports_them(self):
        docs = documents([("pkg/mod.py", PYTHON)])
        _, relationships, report = graph.ground(
            from_documents(docs), entities(PYTHON_TITLES), edges(), documents=docs,
        )
        written_rows = relationships[relationships["description"].str.contains(" DEFINED_IN ")]
        self.assertEqual(set(zip(written_rows["source"], written_rows["target"], strict=True)), PYTHON_PAIRS)
        self.assertEqual(report.defined_in_added, len(PYTHON_PAIRS))
        self.assertIn(f"DEFINED_IN added: {len(PYTHON_PAIRS)}", render(report))


#: celery 2.5.5: what the truth set holds over all 237 files. 917 top-level
#: classes and functions belong to a file; 2,584 methods and nested classes to
#: an enclosing owner.
EXPECTED_CELERY = {"files": 237, "pairs": 3501, "to_file": 917}


@unittest.skipIf(not CELERY.is_dir(), f"no candidate checkout at {CELERY}")
class TestCeleryPin(unittest.TestCase):
    def test_direct_pairs_over_the_checkout(self):
        files = [
            p for p in sorted(CELERY.rglob("*.py"))
            if not ({".git", "__pycache__"} & set(p.relative_to(CELERY).parts))
        ]
        if len(files) != EXPECTED_CELERY["files"]:
            self.skipTest(f"{CELERY} has {len(files)} files, not the {EXPECTED_CELERY['files']} of 2.5.5")
        pairs = [
            pair for p in files
            for pair in containment._pairs(str(p.relative_to(CELERY)), p.read_text(encoding="utf-8", errors="replace"))
        ]
        self.assertEqual(len(pairs), EXPECTED_CELERY["pairs"])
        self.assertEqual(sum(parent is None for _, parent in pairs), EXPECTED_CELERY["to_file"])


if __name__ == "__main__":
    unittest.main()
