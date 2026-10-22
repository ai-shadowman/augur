"""How wrong the line scan is, measured against the AST on the files both answer.

The Phase 4 gate withholds a symbol kind from a line-scanned header when that
kind is imprecise enough to mislead the extraction model. Which kinds those are
is a measurement, not a judgement, and :data:`provenance.symbols.SCAN_PRECISION`
records it. A constant copied from a commit message rots, so this module
recomputes the figures from source and fails when the scanner and the gate
disagree about reality.

The oracle is ``ast``. For Python it is ground truth, so a disagreement on a
file both parsers accept is the line scan's, and no model is involved anywhere
here.

**The population caveat, which the numbers do not carry on their own.** The
files measured are the 187 that ``ast`` accepts. The files the gate governs are
the 50 it rejects, which are Python 2-only. The measurement transfers only if
the two populations nest functions and write multi-line calls at similar rates.
That is an assumption about celery 2.5.5, not a finding about it.
"""

import os
import unittest
import warnings
from pathlib import Path

from provenance import symbols
from provenance.imports import imports_in_lines, imports_in_source

CANDIDATES = Path(
    os.environ.get("AUGUR_CANDIDATES", Path.home() / "Developer" / "augur-test" / "candidates")
)
CHECKOUT = CANDIDATES / "celery"
EXCLUDE_PARTS = {".git", "__pycache__"}

KINDS = ("classes", "functions", "methods", "bindings")

#: The table published with this change, measured on celery 2.5.5. ``emitted``
#: is what the line scan reports, ``truth`` what ``ast`` reports, and the two
#: differ by the false positives in one direction and the misses in the other.
#:
#: Precision is micro-averaged: pooled over names rather than averaged over
#: files, because the question is what fraction of the names reaching the
#: extraction model are real, and a 400-name module should not count the same
#: as a 3-name one. Macro-averaging moves ``bindings`` across the bar and
#: nothing else, and no header section renders ``bindings``.
PUBLISHED = {
    "classes": {"emitted": 445, "truth": 371, "tp": 371, "fp": 74, "fn": 0},
    "functions": {"emitted": 319, "truth": 226, "tp": 218, "fp": 101, "fn": 8},
    "methods": {"emitted": 1730, "truth": 1661, "tp": 1656, "fp": 74, "fn": 5},
    "bindings": {"emitted": 864, "truth": 824, "tp": 799, "fp": 65, "fn": 25},
}

#: Imports come from a different scanner, so they get their own row.
PUBLISHED_IMPORTS = {"emitted": 626, "truth": 621, "tp": 621, "fp": 5, "fn": 0}

EXPECTED_MEASURED = 187
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
    """Pooled TP/FP/FN per kind, plus imports, over every dual-answer file."""
    counts = {kind: dict.fromkeys(("tp", "fp", "fn"), 0) for kind in (*KINDS, "imports")}
    measured = 0

    # Compiling 237 Python 2 files raises a DeprecationWarning per invalid
    # escape sequence in a docstring; unfiltered they bury the suite's own.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        warnings.simplefilter("ignore", SyntaxWarning)
        for path in FILES:
            source = path.read_text(encoding="utf-8", errors="replace")
            try:
                truth = symbols.symbols_in_source(source)
            except (SyntaxError, ValueError, RecursionError):
                continue  # the line scan is the only answer here; nothing to check it against
            measured += 1
            scan = symbols.symbols_in_lines(source)

            pairs = [
                (kind, {n.lower() for n in getattr(scan, kind)}, {n.lower() for n in getattr(truth, kind)})
                for kind in KINDS
            ]
            pairs.append(("imports", imports_in_lines(source), imports_in_source(source)))

            for kind, emitted, actual in pairs:
                counts[kind]["tp"] += len(emitted & actual)
                counts[kind]["fp"] += len(emitted - actual)
                counts[kind]["fn"] += len(actual - emitted)

    return measured, counts


def precision(row):
    emitted = row["tp"] + row["fp"]
    return row["tp"] / emitted if emitted else 1.0


def recall(row):
    actual = row["tp"] + row["fn"]
    return row["tp"] / actual if actual else 1.0


@unittest.skipIf(not FILES, WHY_SKIPPED)
class TestLineScanFidelity(unittest.TestCase):
    """The measurement the gate was set from, recomputed every run."""

    @classmethod
    def setUpClass(cls):
        cls.measured, cls.counts = tally()

    def test_the_measured_population_is_the_one_the_table_describes(self):
        if len(FILES) != EXPECTED_FILES:
            self.skipTest(
                f"{CHECKOUT} has {len(FILES)} files, not the {EXPECTED_FILES} of "
                f"celery 2.5.5 these figures were measured on"
            )

        self.assertEqual(
            EXPECTED_MEASURED,
            self.measured,
            "the dual-answer population changed, so every figure below is about "
            "a different corpus than the one the gate was set on",
        )

    def test_the_published_table_is_what_the_scanners_still_do(self):
        if len(FILES) != EXPECTED_FILES:
            self.skipTest(f"{CHECKOUT} is not the celery 2.5.5 checkout the table was measured on")

        for kind in KINDS:
            with self.subTest(kind=kind):
                row = self.counts[kind]
                self.assertEqual(
                    PUBLISHED[kind],
                    {
                        "emitted": row["tp"] + row["fp"],
                        "truth": row["tp"] + row["fn"],
                        "tp": row["tp"],
                        "fp": row["fp"],
                        "fn": row["fn"],
                    },
                    f"{kind} no longer measures what was published; "
                    f"precision is now {precision(row):.3f}",
                )

    def test_the_gate_constant_matches_the_measurement_it_claims_to_record(self):
        """``SCAN_PRECISION`` is the gate's input, so it must not drift from source."""
        if len(FILES) != EXPECTED_FILES:
            self.skipTest(f"{CHECKOUT} is not the celery 2.5.5 checkout the table was measured on")

        for kind in KINDS:
            with self.subTest(kind=kind):
                self.assertAlmostEqual(
                    symbols.SCAN_PRECISION[kind],
                    precision(self.counts[kind]),
                    places=3,
                    msg=f"symbols.SCAN_PRECISION[{kind!r}] is stale",
                )

    def test_the_kinds_that_clear_the_bar_are_the_ones_the_header_writes(self):
        if len(FILES) != EXPECTED_FILES:
            self.skipTest(f"{CHECKOUT} is not the celery 2.5.5 checkout the table was measured on")

        passing = {kind for kind in KINDS if precision(self.counts[kind]) >= symbols.PRECISION_BAR}

        self.assertEqual({"methods"}, passing)
        self.assertEqual(passing, set(symbols.SCAN_EMITS))

    def test_methods_clears_the_bar_only_just(self):
        """Recorded so the margin is visible rather than implied.

        ``methods`` carries the same 74 false positives as ``classes`` against
        23 times more true names, so it passes on denominator rather than on
        being structurally safer. At 0.96 it would be withheld too.
        """
        if len(FILES) != EXPECTED_FILES:
            self.skipTest(f"{CHECKOUT} is not the celery 2.5.5 checkout the table was measured on")

        measured = precision(self.counts["methods"])

        self.assertGreaterEqual(measured, symbols.PRECISION_BAR)
        self.assertLess(measured, 0.96)
        self.assertEqual(self.counts["methods"]["fp"], self.counts["classes"]["fp"])

    def test_recall_is_high_everywhere_which_is_why_precision_decides(self):
        """The scan misses almost nothing; it over-reports. Hence the gate's shape."""
        if len(FILES) != EXPECTED_FILES:
            self.skipTest(f"{CHECKOUT} is not the celery 2.5.5 checkout the table was measured on")

        for kind in KINDS:
            with self.subTest(kind=kind):
                self.assertGreaterEqual(recall(self.counts[kind]), 0.96)

    def test_imports_measured_clean_so_dependencies_is_not_gated(self):
        """A different scanner and a different verdict, kept explicit.

        ``Dependencies:`` is filled by ``imports_in_lines`` on the same files
        the symbol gate governs. It is not withheld, and this is the evidence
        for that rather than an oversight.
        """
        if len(FILES) != EXPECTED_FILES:
            self.skipTest(f"{CHECKOUT} is not the celery 2.5.5 checkout the table was measured on")

        row = self.counts["imports"]

        self.assertEqual(
            PUBLISHED_IMPORTS,
            {
                "emitted": row["tp"] + row["fp"],
                "truth": row["tp"] + row["fn"],
                "tp": row["tp"],
                "fp": row["fp"],
                "fn": row["fn"],
            },
        )
        self.assertAlmostEqual(symbols.SCAN_IMPORT_PRECISION, precision(row), places=3)
        self.assertGreaterEqual(precision(row), symbols.PRECISION_BAR)


class TestTheSeamTheMeasurementNeeded(unittest.TestCase):
    """``symbols`` exposes both parsers separately, as ``declarations`` does.

    Measuring one scanner against the other needs each callable on the same
    source. Reaching through ``_walk_ast`` and ``_scan_lines`` from a committed
    test would pin private names; mirroring ``declarations``' three-function
    shape is the seam instead.
    """

    PY2 = 'class Worker(object):\n    def start(self):\n        try:\n            pass\n        except E, e:\n            print e\n'

    def test_the_ast_path_raises_where_the_line_scan_answers(self):
        with self.assertRaises(SyntaxError):
            symbols.symbols_in_source(self.PY2)

        recovered = symbols.symbols_in_lines(self.PY2)

        self.assertEqual(("Worker",), recovered.classes)
        self.assertFalse(recovered.used_ast)

    def test_the_combinator_is_the_pair_of_them(self):
        """``symbols_in`` must stay exactly 'parse, else scan'."""
        parseable = "class A(object):\n    def m(self):\n        pass\n"

        self.assertEqual(symbols.symbols_in_source(parseable), symbols.symbols_in(parseable))
        self.assertEqual(symbols.symbols_in_lines(self.PY2), symbols.symbols_in(self.PY2))

    def test_the_two_paths_disagree_about_function_locals_by_design(self):
        """The mechanism behind nearly all of the measured loss, in one case.

        ``_walk_ast`` does not descend into a function body, deliberately, so
        a class defined inside one is invisible to it. The line scan reports
        it qualified, which in a header reads as a member of something that
        does not exist. Real name, wrong scope; that is what the gate stops.
        """
        source = "def make():\n    class Inner(object):\n        def go(self):\n            pass\n"

        parsed = symbols.symbols_in_source(source)
        scanned = symbols.symbols_in_lines(source)

        self.assertEqual((), parsed.classes)
        self.assertEqual(("make",), parsed.functions)
        self.assertEqual(("make.Inner",), scanned.classes)
        self.assertEqual(("make.Inner.go",), scanned.methods)


if __name__ == "__main__":
    unittest.main()
