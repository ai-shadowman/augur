"""tree-sitter must say what ``ast`` says, on every file ``ast`` will read.

``ast`` is the primary reader and stays that way: it is the stdlib, it needs no
dependency, and for Python 3 it is the language's own definition of the syntax.
It has one gap, and on celery 2.5.5 the gap is 50 of 237 files, which are
Python 2. tree-sitter reads those. The arrangement is only sound if the two
readers answer the same question, so this module recomputes the agreement from
source on every run rather than quoting a number from a commit message.

On the 187 files both accept they agree exactly: 371 classes, 226 functions,
1,661 methods, 824 bindings and 621 imports, with no false positive, no miss
and no disagreeing file. That is what licenses treating the 50 as data rather
than as a degraded tier, and it is why ``used_ast`` records which parser ran
without implying how much to trust it.

**What this replaced.** Until Phase 6 the fallback was a regex line scan, and
the file in this slot measured how wrong it was so a precision gate could
withhold the kinds it got wrong too often. That module carried a caveat it
could not discharge: the figures came from the 187 files the scan did not
govern, and transferring them to the 50 it did was an assumption about celery
rather than a finding. Measuring the scan against tree-sitter on the 50
themselves, once, before deleting it, closed that out. Per kind, scan against
parser: classes 0.989 precision and 0.994 recall, functions 0.987 and 0.981,
methods 0.997 and 0.990, bindings 0.978 and 0.974, imports 0.987 and 1.000.
Concretely the scan was putting 17 invented names into headers and missing 25
real ones. Those figures are not recomputable here, because recomputing them
would mean keeping the scan; the checkable residue is the net +8 in
``test_provenance_symbols.EXPECTED``.

Worth recording that ``bindings`` measured 0.978 against the population it
actually governed, well above the 0.95 bar the gate withheld it on from the
other population's 0.925. The gate's decision was right for the wrong corpus.
"""

import os
import unittest
import warnings
from pathlib import Path

from provenance import symbols, treesitter
from provenance.imports import imports_in_source, imports_in_tree

CANDIDATES = Path(
    os.environ.get("AUGUR_CANDIDATES", Path.home() / "Developer" / "augur-test" / "candidates")
)
CHECKOUT = CANDIDATES / "celery"
EXCLUDE_PARTS = {".git", "__pycache__"}

KINDS = ("classes", "functions", "methods", "bindings")

#: Every name on the 187 files both readers accept, counted once per reader.
#: Pinned as totals rather than as a precision figure because the only
#: interesting value is exact agreement, and a ratio would round 0.9994 to
#: 0.999 and pass.
AGREEMENT = {
    "classes": 371,
    "functions": 226,
    "methods": 1661,
    "bindings": 824,
    "imports": 621,
}

#: What tree-sitter reads out of the 50 files ``ast`` refuses: the population
#: nothing could previously check, since the oracle by definition has no
#: opinion about it. Pinned so a grammar upgrade that quietly stops reading
#: Python 2 fails here rather than silently emptying a third of the corpus.
RECOVERED = {
    "classes": 177,
    "functions": 158,
    "methods": 908,
    "bindings": 464,
    "imports": 314,
}

EXPECTED_MEASURED = 187
EXPECTED_RECOVERED = 50
EXPECTED_FILES = 237


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


def tally():
    """Per-kind TP/FP/FN against the oracle, plus totals on the files it refuses."""
    counts = {kind: dict.fromkeys(("tp", "fp", "fn"), 0) for kind in (*KINDS, "imports")}
    recovered = dict.fromkeys((*KINDS, "imports"), 0)
    disagreeing: list[str] = []
    measured = 0
    recovered_files = 0

    # Compiling 237 Python 2 files raises a DeprecationWarning per invalid
    # escape sequence in a docstring; unfiltered they bury the suite's own.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        warnings.simplefilter("ignore", SyntaxWarning)
        for path in FILES:
            source = path.read_text(encoding="utf-8", errors="replace")
            found = symbols.symbols_in_tree(source)
            imported = imports_in_tree(source, treesitter.PYTHON)

            try:
                truth = symbols.symbols_in_source(source)
                truth_imports = imports_in_source(source)
            except (SyntaxError, ValueError, RecursionError):
                # No oracle here, which is the entire reason tree-sitter is in
                # the tree. Counted rather than compared.
                recovered_files += 1
                for kind in KINDS:
                    recovered[kind] += len(getattr(found, kind))
                recovered["imports"] += len(imported)
                continue

            measured += 1
            pairs = [
                (
                    kind,
                    {n.lower() for n in getattr(found, kind)},
                    {n.lower() for n in getattr(truth, kind)},
                )
                for kind in KINDS
            ]
            pairs.append(("imports", imported, truth_imports))

            for kind, emitted, actual in pairs:
                counts[kind]["tp"] += len(emitted & actual)
                counts[kind]["fp"] += len(emitted - actual)
                counts[kind]["fn"] += len(actual - emitted)
                if emitted != actual:
                    disagreeing.append(f"{path.name}:{kind}")

    return measured, recovered_files, counts, recovered, disagreeing


@unittest.skipIf(not FILES, WHY_SKIPPED)
class TestTheTwoReadersAgree(unittest.TestCase):
    """237 files, recomputed every run. The claim is equality, not closeness."""

    @classmethod
    def setUpClass(cls):
        (cls.measured, cls.recovered_files, cls.counts,
         cls.recovered, cls.disagreeing) = tally()

    def skip_unless_celery(self):
        if len(FILES) != EXPECTED_FILES:
            self.skipTest(
                f"{CHECKOUT} has {len(FILES)} files, not the {EXPECTED_FILES} of "
                f"celery 2.5.5 these figures were measured on"
            )

    def test_the_measured_population_is_the_one_the_table_describes(self):
        self.skip_unless_celery()

        self.assertEqual(
            (EXPECTED_MEASURED, EXPECTED_RECOVERED),
            (self.measured, self.recovered_files),
            "the dual-answer population changed, so every figure below is "
            "about a different corpus",
        )

    def test_not_one_name_differs_on_a_file_both_readers_accept(self):
        """The whole licence for the arrangement, as an equality per kind."""
        self.skip_unless_celery()

        for kind in (*KINDS, "imports"):
            with self.subTest(kind=kind):
                row = self.counts[kind]
                self.assertEqual(
                    (0, 0),
                    (row["fp"], row["fn"]),
                    f"{kind}: {row['fp']} name(s) tree-sitter invents, "
                    f"{row['fn']} it misses, out of {row['tp'] + row['fn']} real",
                )

    def test_no_file_disagrees_anywhere(self):
        """Per file rather than pooled: offsetting errors would net to zero."""
        self.skip_unless_celery()

        self.assertEqual([], self.disagreeing[:10])

    def test_the_agreement_is_over_the_names_that_were_published(self):
        """Anti-vacuity. Two readers that both return nothing agree perfectly.

        Without this the suite would pass if ``symbols_in_tree`` were replaced
        by a stub, since every count above is a difference and differences of
        empty sets are empty. The totals are what make the agreement mean
        something was read.
        """
        self.skip_unless_celery()

        self.assertEqual(
            AGREEMENT,
            {kind: self.counts[kind]["tp"] for kind in (*KINDS, "imports")},
        )

    def test_the_files_the_oracle_refuses_are_read_rather_than_skipped(self):
        """The population that had no check at all until the scan was retired.

        Nothing can verify these against a Python oracle, so what is pinned is
        that they are non-empty and stable. A grammar upgrade that dropped
        Python 2 support would empty a third of the corpus and otherwise pass
        every test in this file.
        """
        self.skip_unless_celery()

        self.assertEqual(RECOVERED, self.recovered)

    def test_python_2_is_grammar_rather_than_recovery(self):
        """None of the 50 parse as damaged, which is a stronger claim.

        An error-tolerant parser returns what it salvaged from a file it did
        not understand. This grammar has the Python 2 productions, so it
        understands them, and ``has_error`` stays a signal about genuinely
        malformed source rather than a proxy for 'old Python'.
        """
        self.skip_unless_celery()

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            warnings.simplefilter("ignore", SyntaxWarning)
            recovered_with_errors = [
                path.name
                for path in FILES
                if not _parses_as_python_3(source := _read(path))
                and treesitter.has_error(source, treesitter.PYTHON)
            ]

        self.assertEqual([], recovered_with_errors)


def _read(path):
    return path.read_text(encoding="utf-8", errors="replace")


def _parses_as_python_3(source: str) -> bool:
    try:
        symbols.symbols_in_source(source)
    except (SyntaxError, ValueError, RecursionError):
        return False
    return True


class TestTheSeamTheMeasurementNeeds(unittest.TestCase):
    """``symbols`` exposes both readers separately, as ``declarations`` does.

    Comparing one against the other needs each callable on the same source.
    Reaching through ``_walk_ast`` and ``treesitter._walk`` from a committed
    test would pin private names; mirroring ``declarations``' three-function
    shape is the seam instead.
    """

    PY2 = (
        "class Worker(object):\n"
        "    def start(self):\n"
        "        try:\n"
        "            pass\n"
        "        except E, e:\n"
        "            print e\n"
    )

    def test_the_ast_path_raises_where_tree_sitter_answers(self):
        with self.assertRaises(SyntaxError):
            symbols.symbols_in_source(self.PY2)

        recovered = symbols.symbols_in_tree(self.PY2)

        self.assertEqual(("Worker",), recovered.classes)
        self.assertEqual(("Worker.start",), recovered.methods)
        self.assertFalse(recovered.used_ast)

    def test_the_combinator_is_the_pair_of_them(self):
        """``symbols_in`` must stay exactly 'parse, else tree-sitter'."""
        parseable = "class A(object):\n    def m(self):\n        pass\n"

        self.assertEqual(symbols.symbols_in_source(parseable), symbols.symbols_in(parseable))
        self.assertEqual(symbols.symbols_in_tree(self.PY2), symbols.symbols_in(self.PY2))

    def test_the_two_paths_agree_about_function_locals(self):
        """The rule the traversals share, and the reason one mirrors the other.

        ``_walk_ast`` does not descend into a function body, deliberately, so
        a class defined inside one is invisible to it. A reader that did
        descend would report it qualified as ``make.Inner``, which in a header
        reads as a member of something that does not exist: a real name in a
        scope nobody can import it from.
        """
        source = "def make():\n    class Inner(object):\n        def go(self):\n            pass\n"

        parsed = symbols.symbols_in_source(source)
        recovered = symbols.symbols_in_tree(source)

        self.assertEqual((), parsed.classes)
        self.assertEqual(("make",), parsed.functions)
        self.assertEqual(parsed.names, recovered.names)

    def test_a_class_behind_a_statement_is_still_reached(self):
        """The other half of the rule: a non-namespace block is descended into.

        A compatibility branch declares real names, and a Python 2 codebase is
        full of them. ``_blocks`` crosses the ``if`` and the ``except_clause``
        to find them while refusing to cross a ``def``.
        """
        source = (
            "import sys\n"
            "if sys.version_info[0] == 2:\n"
            "    class Old(object):\n"
            "        def go(self):\n"
            "            pass\n"
            "else:\n"
            "    class New(object):\n"
            "        pass\n"
        )

        recovered = symbols.symbols_in_tree(source)

        self.assertEqual(("Old", "New"), recovered.classes)
        self.assertEqual(("Old.go",), recovered.methods)
        self.assertEqual(symbols.symbols_in_source(source).names, recovered.names)

    def test_a_decorated_definition_is_unwrapped(self):
        """``@task`` wraps the def in the grammar where it does not in ``ast``.

        The one structural difference between the two trees that the traversal
        has to know about, which is what :attr:`Dialect.unwrap` is for.
        """
        source = "@task(ignore_result=True)\ndef add(x, y):\n    return x + y\n"

        self.assertEqual(("add",), symbols.symbols_in_tree(source).functions)

    def test_a_malformed_file_yields_what_the_grammar_understood(self):
        """Error recovery is reported, not acted on, and it still returns names."""
        source = "class Good(object):\n    pass\n\nclass ((( :\n"

        self.assertTrue(treesitter.has_error(source, treesitter.PYTHON))
        self.assertEqual(("Good",), symbols.symbols_in_tree(source).classes)


if __name__ == "__main__":
    unittest.main()
