"""How wrong the line scan is, measured against the AST on the files both answer.

The gate withholds a symbol kind from a line-scanned header when that kind is
imprecise enough to mislead the extraction model. Which kinds those are is a
measurement, not a judgement, and :data:`provenance.symbols.SCAN_PRECISION`
records it. A constant copied from a commit message rots, so this module
recomputes the figures from source and fails when the scanner and the gate
disagree about reality.

The oracle is ``ast``. For Python it is ground truth, so a disagreement on a
file both parsers accept is the line scan's, and no model is involved anywhere
here.

Phase 4 set the gate and only ``methods`` cleared it. Phase 5 found that the
loss was not the docstrings both of its registered predictions named but
declarations nested inside a function, which ``_walk_ast`` skips by design and
``_scan_lines`` could not, and taught the scan the same rule. 249 false
positives in the three rendered kinds became 4 with no true name lost, so the
gate now withholds nothing a header writes. :data:`PHASE_4` keeps the old
table beside the new one so that is readable here rather than only in a commit
message.

**The population caveat, which the numbers do not carry on their own.** The
files measured are the 187 that ``ast`` accepts. The files the gate governs are
the 50 it rejects, which are Python 2-only. The measurement transfers only if
the two populations nest functions and write multi-line calls at similar rates.
That is an assumption about celery 2.5.5, not a finding about it.
"""

import os
import unittest
import warnings
from dataclasses import replace
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
    "classes": {"emitted": 372, "truth": 371, "tp": 371, "fp": 1, "fn": 0},
    "functions": {"emitted": 219, "truth": 226, "tp": 218, "fp": 1, "fn": 8},
    "methods": {"emitted": 1658, "truth": 1661, "tp": 1656, "fp": 2, "fn": 5},
    "bindings": {"emitted": 864, "truth": 824, "tp": 799, "fp": 65, "fn": 25},
}

#: Phase 4's table, kept so the delta is readable here and not only in a commit
#: message. Phase 5 made ``_scan_lines`` skip function bodies, as ``_walk_ast``
#: already did; the 249 false positives in the three rendered kinds became 4,
#: no true name was lost, and ``bindings`` did not move at all because its
#: errors are multi-line keyword arguments rather than nesting.
PHASE_4 = {
    "classes": {"emitted": 445, "truth": 371, "tp": 371, "fp": 74, "fn": 0},
    "functions": {"emitted": 319, "truth": 226, "tp": 218, "fp": 101, "fn": 8},
    "methods": {"emitted": 1730, "truth": 1661, "tp": 1656, "fp": 74, "fn": 5},
    "bindings": {"emitted": 864, "truth": 824, "tp": 799, "fp": 65, "fn": 25},
}

#: The kinds a header renders. ``bindings`` is measured but never written.
RENDERED = ("classes", "functions", "methods")

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

        self.assertEqual({"classes", "functions", "methods"}, passing)
        self.assertEqual(passing, set(symbols.SCAN_EMITS))

    def test_every_rendered_kind_clears_the_bar_with_room(self):
        """The margin, recorded rather than implied.

        Phase 4 emitted ``methods`` alone and on denominator rather than
        merit: the same 74 false positives as ``classes`` against 23 times
        more true names, failing at 0.96. All three are now above 0.99, so the
        bar could move most of the way to 1.0 without changing what ships.
        """
        if len(FILES) != EXPECTED_FILES:
            self.skipTest(f"{CHECKOUT} is not the celery 2.5.5 checkout the table was measured on")

        for kind in RENDERED:
            with self.subTest(kind=kind):
                self.assertGreaterEqual(precision(self.counts[kind]), 0.99)

    def test_the_gate_still_withholds_something_it_measured(self):
        """The gate is now inert on every kind a header writes. Said, not implied.

        Phase 4's gate existed because the scan was wrong; Phase 5 fixed the
        scan, so nothing the renderer would write is withheld any more. That
        makes the gate a regression detector rather than a filter, and the
        detector is only worth keeping if it can still fire — which is what
        ``bindings`` at 0.925 demonstrates on real source.
        """
        if len(FILES) != EXPECTED_FILES:
            self.skipTest(f"{CHECKOUT} is not the celery 2.5.5 checkout the table was measured on")

        self.assertNotIn("bindings", symbols.SCAN_EMITS)
        self.assertLess(precision(self.counts["bindings"]), symbols.PRECISION_BAR)
        self.assertTrue(set(RENDERED) <= set(symbols.SCAN_EMITS))

    def test_the_fix_removed_false_positives_without_losing_true_names(self):
        """The whole claim of movement one, against Phase 4's table.

        Suppressing function-nested declarations could in principle have
        dropped real module-level names; the recall columns say it did not.
        Every true positive and every miss is unchanged, and only the false
        positives moved.
        """
        if len(FILES) != EXPECTED_FILES:
            self.skipTest(f"{CHECKOUT} is not the celery 2.5.5 checkout the table was measured on")

        for kind in KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(PHASE_4[kind]["tp"], self.counts[kind]["tp"])
                self.assertEqual(PHASE_4[kind]["fn"], self.counts[kind]["fn"])
                self.assertLessEqual(self.counts[kind]["fp"], PHASE_4[kind]["fp"])

        self.assertEqual(
            249, sum(PHASE_4[kind]["fp"] for kind in RENDERED),
            "Phase 4's false positives in the kinds a header renders",
        )
        self.assertEqual(4, sum(self.counts[kind]["fp"] for kind in RENDERED))

    def test_bindings_did_not_move_because_its_errors_are_not_nesting(self):
        """Why one kind stayed below the bar, diagnosed rather than left open.

        Phase 4 never found the mechanism behind these 65. They are keyword
        arguments in multi-line calls — ``Column("id", Integer,`` newline
        ``primary_key=True)`` — which ``_BIND_LINE`` reads as assignments
        because the scan tracks indentation and not bracket depth. Disjoint
        from function nesting, which is why the fix left the row untouched to
        the name.
        """
        if len(FILES) != EXPECTED_FILES:
            self.skipTest(f"{CHECKOUT} is not the celery 2.5.5 checkout the table was measured on")

        self.assertEqual(PHASE_4["bindings"], PUBLISHED["bindings"])

        source = 'Task = Table("task",\n    Column("id", Integer,\n        primary_key=True))\n'

        self.assertEqual(("Task",), symbols.symbols_in_source(source).bindings)
        self.assertEqual(("Task", "primary_key"), symbols.symbols_in_lines(source).bindings)

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

    def test_the_two_paths_agree_about_function_locals(self):
        """The mechanism behind nearly all of Phase 4's loss, now closed.

        ``_walk_ast`` does not descend into a function body, deliberately, so
        a class defined inside one is invisible to it. The line scan used to
        report it qualified as ``make.Inner``, which in a header reads as a
        member of something that does not exist: a real name in a scope
        nobody can import it from. Both paths now decline it, which is the
        whole of movement one in four lines of source.
        """
        source = "def make():\n    class Inner(object):\n        def go(self):\n            pass\n"

        parsed = symbols.symbols_in_source(source)
        scanned = symbols.symbols_in_lines(source)

        self.assertEqual((), parsed.classes)
        self.assertEqual(("make",), parsed.functions)
        self.assertEqual(parsed, replace(scanned, used_ast=True))

    def test_suppression_reaches_past_the_first_nested_level(self):
        """Skipping before the stack push is what makes the deep case work.

        ``_scan_lines`` drops a nested block without pushing a frame for it,
        so the guard has to be the *enclosing* function still on the stack.
        It is: a function frame always has a smaller indent than anything
        inside it, so nothing pops it until the body ends.
        """
        source = (
            "def outer():\n"
            "    class A(object):\n"
            "        class B(object):\n"
            "            def deep(self):\n"
            "                buried = 1\n"
            "TOP = 1\n"
        )

        scanned = symbols.symbols_in_lines(source)

        self.assertEqual(("outer",), scanned.functions)
        self.assertEqual((), scanned.classes)
        self.assertEqual((), scanned.methods)
        self.assertEqual(("TOP",), scanned.bindings)


if __name__ == "__main__":
    unittest.main()
