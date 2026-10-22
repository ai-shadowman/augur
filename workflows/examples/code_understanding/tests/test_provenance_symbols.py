"""``symbols`` must agree with ``declarations`` on every file it reads.

The header writer needs kinds, which the oracle does not carry, so this module
is what keeps the extra structure honest: same parser choice, same set of
names, only the kind and the casing added. The agreement is checked over the
whole candidate checkout rather than a sample, because the interesting files
are the Python 2 ones and a sample would miss them.

The checkout is the sandbox at ``~/Developer/augur-test/candidates/celery``,
overridable with ``AUGUR_CANDIDATES``. It is not part of this repository, so
the corpus-wide cases skip when it is absent and the classification rules
below still run.
"""

import os
import unittest
import warnings
from pathlib import Path

from provenance import symbols
from provenance.declarations import declarations_in
from provenance.dialects import PYTHON
from provenance.imports import imports_in, imports_in_source, imports_in_tree

CANDIDATES = Path(
    os.environ.get("AUGUR_CANDIDATES", Path.home() / "Developer" / "augur-test" / "candidates")
)
CHECKOUT = CANDIDATES / "celery"
EXCLUDE_PARTS = {".git", "__pycache__"}

#: What celery 2.5.5 measures. The counts are only asserted when the checkout
#: has exactly this many files, so a different candidate still gets the
#: equality checks without failing on a figure that was never about it.
EXPECTED_FILES = 237
#: Only the 50 recovered files can move the declaration count, and the history
#: of that number is the history of what reads them. 4,885 under the line scan,
#: then 4,781 once Phase 5 taught it to skip function bodies, shedding 104
#: closure locals the AST never counted either. 4,789 now that tree-sitter
#: reads them: the scan was missing 25 real names and inventing 17, and +8 is
#: the net. The 187 parsed files have never moved it.
EXPECTED = {"ast": 187, "tree_sitter": 50, "zero_declarations": 14, "declarations": 4789}


def candidate_files():
    if not CHECKOUT.is_dir():
        return []
    return [
        path
        for path in sorted(CHECKOUT.rglob("*.py"))
        if not (EXCLUDE_PARTS & set(path.relative_to(CHECKOUT).parts))
    ]


FILES = candidate_files()
WHY_SKIPPED = f"no candidate checkout at {CHECKOUT}"


def read(path):
    return path.read_text(encoding="utf-8", errors="replace")


class TestAstClassification(unittest.TestCase):
    """The rules, on sources small enough to read."""

    CASES = {
        "module function": (
            "def run():\n    pass\n",
            {"functions": ("run",)},
        ),
        "class and its method": (
            "class LocalCache(object):\n    def get(self):\n        pass\n",
            {"classes": ("LocalCache",), "methods": ("LocalCache.get",)},
        ),
        "casing is the source's, not the oracle's": (
            "class LocalCache(object):\n    def GetItem(self):\n        pass\n",
            {"classes": ("LocalCache",), "methods": ("LocalCache.GetItem",)},
        ),
        "nested class is a class, its def a method": (
            "class Outer(object):\n"
            "    class Meta(object):\n"
            "        def opt(self):\n"
            "            pass\n",
            {"classes": ("Outer", "Outer.Meta"), "methods": ("Outer.Meta.opt",)},
        ),
        "a closure is not addressable and is dropped": (
            "def outer():\n    def inner():\n        pass\n",
            {"functions": ("outer",)},
        ),
        "module binding is neither class nor function": (
            "logger = get_logger()\n",
            {"bindings": ("logger",)},
        ),
        "a class behind a compatibility branch still counts": (
            "import sys\nif sys.version_info[0] == 2:\n    class Old(object):\n        pass\n",
            {"classes": ("Old",)},
        ),
        "a class inside try/except still counts": (
            "try:\n"
            "    class A(object):\n"
            "        pass\n"
            "except ImportError:\n"
            "    class A(object):\n"
            "        pass\n",
            {"classes": ("A",)},
        ),
        "attribute assignment binds no module name": (
            "class A(object):\n    def __init__(self):\n        self.conf = 1\n",
            {"classes": ("A",), "methods": ("A.__init__",)},
        ),
        "an async def is a function like any other": (
            "async def fetch():\n    pass\n",
            {"functions": ("fetch",)},
        ),
    }

    def test_each_rule_classifies_as_described(self):
        for label, (source, expected) in self.CASES.items():
            with self.subTest(case=label):
                found = symbols.symbols_in(source)
                self.assertTrue(found.used_ast, "sample should parse as Python 3")
                for kind in ("classes", "functions", "methods", "bindings"):
                    self.assertEqual(
                        expected.get(kind, ()),
                        getattr(found, kind),
                        f"{kind} mismatch for {label!r}",
                    )


#: ``except X, e`` is a syntax error under Python 3, which is what forces the
#: fallback parser on roughly a third of the indexed corpus.
PY2 = (
    "class Worker(object):\n"
    "    def start(self):\n"
    "        try:\n"
    "            pass\n"
    "        except Exception, exc:\n"
    "            print exc\n"
    "\n"
    "def bootstrap():\n"
    "    pass\n"
    "\n"
    "DEFAULTS = {}\n"
)


class TestTreeSitterFallback(unittest.TestCase):
    """What the header gets when ``ast`` refuses the file."""

    def test_tree_sitter_classifies_when_ast_refuses(self):
        found = symbols.symbols_in(PY2)

        self.assertFalse(found.used_ast, "Python 2 source should force the fallback")
        self.assertEqual(("Worker",), found.classes)
        self.assertEqual(("Worker.start",), found.methods)
        self.assertEqual(("bootstrap",), found.functions)
        self.assertEqual(("DEFAULTS",), found.bindings)

    def test_a_nested_def_is_declined_as_the_ast_path_declines_it(self):
        """A closure's locals are not addressable, so neither reader emits them.

        ``inner`` is a real name and nothing is inventing it, but no import can
        reach it from outside ``outer``, so emitting it as ``outer.inner``
        would put a symbol in front of the extraction model that does not
        resolve. ``treesitter._walk`` declines it for the same reason
        ``_walk_ast`` does, which is the point of mirroring the traversal.
        """
        found = symbols.symbols_in("def outer():\n    def inner():\n        pass\n    raise X, y\n")

        self.assertFalse(found.used_ast)
        self.assertEqual((), found.methods)
        self.assertEqual(("outer",), found.functions)

    def test_a_duplicate_folded_name_keeps_the_first_kind(self):
        """``def f`` then ``f = wrap(f)`` is one name to the oracle."""
        found = symbols.symbols_in("def f():\n    pass\nf = wrap(f)\n")

        self.assertEqual(("f",), found.functions)
        self.assertEqual((), found.bindings)
        self.assertEqual({"f"}, found.names)

    def test_an_empty_file_declares_nothing_and_is_falsy(self):
        found = symbols.symbols_in("")

        self.assertTrue(found.used_ast, "an empty file parses; it just says nothing")
        self.assertFalse(found)
        self.assertEqual(0, found.declared)


class TestImportsCombinator(unittest.TestCase):
    """The pair ``declarations_in`` always had and ``imports`` did not."""

    def test_ast_answers_and_says_so(self):
        found, used_ast = imports_in("import os\nfrom kombu.pools import get\n")

        self.assertTrue(used_ast)
        self.assertEqual({"os", "kombu"}, found)

    def test_python_2_falls_back_instead_of_raising(self):
        """The whole reason for the combinator: a third of the corpus is this."""
        with self.assertRaises(SyntaxError):
            imports_in_source(PY2)

        found, used_ast = imports_in(PY2)

        self.assertFalse(used_ast)
        self.assertEqual(imports_in_tree(PY2, PYTHON), found)

    def test_future_is_still_reported_as_an_import(self):
        """A language directive, not a dependency. Deliberately not filtered.

        ``harness/build_corpus.py`` does not filter it either, and the corpus
        rebuild is checked byte for byte against this path. Filtering in one
        tree and not the other would break that check for a cosmetic gain.
        Phase 5 owns it.
        """
        found, _ = imports_in("from __future__ import absolute_import\n")

        self.assertEqual({"__future__"}, found)


@unittest.skipIf(not FILES, WHY_SKIPPED)
class TestKindsAgreeWithTheOracleOnEveryFile(unittest.TestCase):
    """237 files, not a sample. The interesting ones are the Python 2 files."""

    @classmethod
    def setUpClass(cls):
        # Both parsers run here rather than in the test bodies: compiling 237
        # Python 2 files raises a DeprecationWarning per invalid escape
        # sequence in a docstring, and left unfiltered they bury the suite's
        # real warnings.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            warnings.simplefilter("ignore", SyntaxWarning)
            cls.cases = {
                str(path.relative_to(CHECKOUT)): (
                    declarations_in(source := read(path)),
                    symbols.symbols_in(source),
                )
                for path in FILES
            }

    def test_the_four_kinds_folded_are_exactly_what_declarations_reports(self):
        for relative, ((oracle, oracle_used_ast), found) in self.cases.items():
            with self.subTest(file=relative):
                self.assertEqual(
                    oracle_used_ast,
                    found.used_ast,
                    f"parser disagreement: symbols used "
                    f"{'ast' if found.used_ast else 'tree-sitter'}, declarations used "
                    f"{'ast' if oracle_used_ast else 'tree-sitter'}",
                )
                self.assertEqual(
                    oracle,
                    found.names,
                    f"{len(found.names - oracle)} name(s) only in symbols "
                    f"({sorted(found.names - oracle)[:5]}), "
                    f"{len(oracle - found.names)} only in declarations "
                    f"({sorted(oracle - found.names)[:5]})",
                )

    def test_no_name_is_filed_under_two_kinds(self):
        for relative, (_oracle, found) in self.cases.items():
            with self.subTest(file=relative):
                folded = [
                    name.lower()
                    for bucket in (found.classes, found.functions, found.methods, found.bindings)
                    for name in bucket
                ]
                duplicated = sorted({name for name in folded if folded.count(name) > 1})
                self.assertEqual([], duplicated[:5], "a name was filed under two kinds")

    def test_the_parser_split_is_what_the_commit_message_published(self):
        """The fallback and zero-declaration counts, asserted rather than implied."""
        if len(FILES) != EXPECTED_FILES:
            self.skipTest(
                f"{CHECKOUT} has {len(FILES)} files, not the {EXPECTED_FILES} of "
                f"celery 2.5.5 these counts were measured on"
            )

        found = [classified for _oracle, classified in self.cases.values()]
        measured = {
            "ast": sum(1 for s in found if s.used_ast),
            "tree_sitter": sum(1 for s in found if not s.used_ast),
            "zero_declarations": sum(1 for s in found if not s),
            "declarations": sum(s.declared for s in found),
        }

        self.assertEqual(EXPECTED, measured)


if __name__ == "__main__":
    unittest.main()
