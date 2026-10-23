"""The differ is arithmetic, so what needs testing is what it reads and claims.

Three jobs here. Keep the metrics readable from a ``VerificationReport``
alone, which is the acceptance condition Phase 7 prep was given and the thing
that stops the differ growing into a second verifier. Keep the bucket names it
sums in step with the checks that write them, by calling those checks rather
than restating their literals. And keep the registered predictions checkable,
because a prediction nothing grades is a sentence in a file.

No index exists to run this against. Every report here is synthetic, and the
bucket-name tests are the only part anchored to real code.
"""

import unittest

from provenance import compare, declarations, verify
from provenance.corpus import Corpus, CorpusFile
from provenance.models import (
    Finding,
    FindingKind,
    Layer,
    Package,
    RepoFacts,
    VerificationReport,
)

FACTS = RepoFacts(
    root="/tmp/celery-255",
    own_modules=["celery"],
    source_files=["celery/worker/state.py"],
)


def entity_frame(rows):
    """The minimal entity table the checks read.

    pandas is imported inside the function, not at module scope: see
    tests/conftest.py for why a DataFrame must not be built during import.
    """
    import pandas as pd

    return pd.DataFrame(rows, columns=["title", "type", "description"])


#: An AUGUR header plus a body declaring one name. ``check_symbol_layer``
#: returns an empty dict when the corpus declares nothing at all, so a
#: document that parses to zero symbols would make the drift pin vacuous.
DOCUMENT = (
    '"""This file is located at celery/worker/state.py from repository url '
    "https://github.com/celery/celery, repository slug celery-v2-5-5, "
    'multi_repo false, runtime Python 2.7"""\n'
    "def task_accepted():\n    return None\n"
)


def document_frame():
    import pandas as pd

    return pd.DataFrame([{"title": "state.py", "text": DOCUMENT}])


def corpus_file(path):
    """One indexed document, as ``parse_header`` would have produced it."""
    return CorpusFile(
        path=path,
        git_repo="https://github.com/celery/celery.git",
        git_slug="celery-v2-5-5",
        multi_repo=False,
    )


def report(**stats):
    """A report carrying stats and nothing else.

    Deliberately bare. If a metric ever needs ``findings`` or ``checked``, the
    tests below start failing, which is the acceptance condition doing its job.
    """
    return VerificationReport(stats=dict(stats))


def symbol_stats(precision=0.5, declared=50, misattributed=10, leaked=5, invented=5, bare=30):
    return {
        "symbol_layer": {
            "declared": declared,
            "misattributed": misattributed,
            "leaked": leaked,
            "invented": invented,
            "bare": bare,
            "precision": precision,
            # Derived entries verify_index appends to the same dict. No entity
            # belongs to these, so the population must not count them.
            "indexed_modules": 22,
            "checkout_inventory": 237,
        }
    }


class TestTheMetricsComeOutOfOneReport(unittest.TestCase):
    """The acceptance condition: a metric the differ cannot read is not a metric."""

    def test_every_registered_metric_reads_from_stats_alone(self):
        full = report(
            **symbol_stats(),
            package_layer={"invented": 7, "undeclared": 1, "granularity": 2,
                           "internal": 3, "grounded": 40},
            module_layer={"indexed": 30, "package": 5, "package_on_disk": 1,
                          "leaked": 2, "invented": 4, "precision": 0.833,
                          "relative_resolved": 0, "relative_unresolved": 0,
                          "corpus_files": 22},
        )

        for metric in compare.METRICS:
            with self.subTest(metric=metric.name):
                reading = compare.read(full, metric)

                self.assertEqual("", reading.missing)
                self.assertIsNotNone(reading.value)

    def test_the_population_excludes_the_derived_entries(self):
        """``sum(counts.values())`` would be wrong, and wrong by a lot.

        ``indexed_modules`` and ``checkout_inventory`` sit in the same dict as
        the five buckets. Summing the dict would put 259 extra into a
        population of 100 and make every count metric unreadable against it.
        """
        reading = compare.read(report(**symbol_stats()), compare.SYMBOL_PRECISION)

        self.assertEqual(100, reading.population)

    def test_an_absent_section_says_so_rather_than_reading_zero(self):
        reading = compare.read(report(), compare.INVENTED_PACKAGES)

        self.assertIsNone(reading.value)
        self.assertIn("package_layer", reading.missing)

    def test_a_rate_over_an_empty_layer_is_missing_rather_than_zero(self):
        """verify_index writes None, and None is not the same claim as 0.0."""
        empty = report(symbol_layer=dict.fromkeys(compare.SYMBOL_BUCKETS, 0) | {"precision": None})

        reading = compare.read(empty, compare.SYMBOL_PRECISION)

        self.assertIsNone(reading.value)
        self.assertIn("no entities", reading.missing)

    def test_the_reading_prints_its_population_and_not_just_its_value(self):
        """A count without its population is the mistake this epic keeps making."""
        rendered = str(compare.read(report(**symbol_stats()), compare.MISATTRIBUTED_SYMBOLS))

        self.assertIn("10", rendered)
        self.assertIn("n=100", rendered)


class TestTheBucketNamesMatchTheChecksThatWriteThem(unittest.TestCase):
    """Anchored to real code, by calling it.

    The differ sums buckets by name. Restating those names in a test would
    pin the differ to itself; calling the check and reading its keys pins it
    to ``verify_index``, which is the thing that can actually drift.
    """

    def assert_covers(self, stats, section, metric):
        counts = stats[section]

        self.assertIn(metric.field, counts, f"{metric.name} reads a field the check does not write")
        for bucket in metric.population:
            with self.subTest(bucket=bucket):
                self.assertIn(bucket, counts)

    def test_the_package_layer_buckets_are_the_checks_own(self):
        _findings, stats = verify.check_package_layer_grounded(FACTS, entity_frame([]))

        self.assert_covers(stats, "package_layer", compare.INVENTED_PACKAGES)

    def test_the_module_layer_buckets_are_the_checks_own(self):
        corpus = Corpus(files=[corpus_file("celery/worker/state.py")])

        _findings, stats = verify.check_module_layer_grounded(FACTS, entity_frame([]), corpus)

        self.assert_covers(stats, "module_layer", compare.MODULE_PRECISION)

    def test_the_symbol_layer_buckets_are_the_checks_own(self):
        _findings, stats = declarations.check_symbol_layer(
            FACTS, entity_frame([]), document_frame()
        )

        for metric in (compare.SYMBOL_PRECISION, compare.MISATTRIBUTED_SYMBOLS):
            with self.subTest(metric=metric.name):
                self.assert_covers(stats, "symbol_layer", metric)


class TestComparing(unittest.TestCase):
    def movement(self, before, after, metric=compare.MISATTRIBUTED_SYMBOLS):
        result = compare.compare(before, after)
        return next(m for m in result.movements if m.metric is metric)

    def test_a_fall_in_a_lower_is_better_metric_is_an_improvement(self):
        moved = self.movement(report(**symbol_stats(misattributed=60)),
                              report(**symbol_stats(misattributed=4)))

        self.assertEqual(-56, moved.delta)
        self.assertTrue(moved.improved)

    def test_a_fall_in_a_higher_is_better_metric_is_a_regression(self):
        moved = self.movement(report(**symbol_stats(precision=0.61)),
                              report(**symbol_stats(precision=0.39)),
                              metric=compare.SYMBOL_PRECISION)

        self.assertFalse(moved.improved)

    def test_no_movement_is_reported_as_flat_rather_than_improved(self):
        moved = self.movement(report(**symbol_stats()), report(**symbol_stats()))

        self.assertEqual(0, moved.delta)
        self.assertFalse(moved.improved)

    def test_a_metric_neither_report_answers_is_named_rather_than_dropped(self):
        result = compare.compare(report(**symbol_stats()), report(**symbol_stats()))

        self.assertTrue(any("invented_package_entities" in n for n in result.unavailable))
        self.assertNotIn(
            compare.INVENTED_PACKAGES, [m.metric for m in result.movements]
        )

    def test_a_metric_only_one_report_answers_still_compares(self):
        """One index crashing mid-layer must not silently drop the other's number."""
        result = compare.compare(report(), report(**symbol_stats()))
        moved = next(m for m in result.movements if m.metric is compare.SYMBOL_PRECISION)

        self.assertIsNone(moved.delta)
        self.assertIsNone(moved.baseline.value)
        self.assertEqual(0.5, moved.treatment.value)

    def test_the_findings_list_is_never_consulted(self):
        """Reading a finding would make the differ a second verifier."""
        noisy = VerificationReport(
            findings=[Finding(kind=FindingKind.UNGROUNDED, subject="x", detail="y")],
            checked=99,
            stats=symbol_stats(misattributed=4),
        )
        quiet = report(**symbol_stats(misattributed=4))

        self.assertEqual(
            compare.read(noisy, compare.MISATTRIBUTED_SYMBOLS),
            compare.read(quiet, compare.MISATTRIBUTED_SYMBOLS),
        )


class TestThePredictionsAreCheckable(unittest.TestCase):
    """The decidability pattern applied to a measurement: an argument that fails.

    ``PREDICTIONS`` is the only artifact of this session that cannot be
    rebuilt later. Once the numbers arrive, any prediction written after them
    is worth nothing, so the drift pins here matter more than usual.
    """

    def test_every_metric_carries_a_prediction_and_every_prediction_a_metric(self):
        predicted = {p.metric for p in compare.PREDICTIONS}
        measured = {m.name for m in compare.METRICS}

        self.assertEqual(measured, predicted)

    def test_each_prediction_argues_rather_than_asserting(self):
        reasons = [p.reason.strip() for p in compare.PREDICTIONS]

        self.assertTrue(all(reasons), "a prediction with no reason cannot be learned from")
        self.assertEqual(len(reasons), len(set(reasons)), "a reused reason is filler")

    def test_each_prediction_makes_at_least_one_claim(self):
        for prediction in compare.PREDICTIONS:
            with self.subTest(metric=prediction.metric):
                self.assertTrue(prediction.bounds())

    def test_the_delta_bound_points_the_way_the_metric_improves(self):
        """A sign error here would grade a regression as a confirmed prediction."""
        improving = {True: (">", ">="), False: ("<", "<=")}
        for metric in compare.METRICS:
            prediction = compare.prediction_for(metric.name)
            if prediction.delta is None:
                continue
            with self.subTest(metric=metric.name):
                self.assertIn(prediction.delta.op, improving[metric.higher_is_better])

    def test_the_treatment_bound_sits_on_the_improving_side_of_the_baseline(self):
        """Predicting treatment no better than baseline would contradict the delta."""
        for metric in compare.METRICS:
            prediction = compare.prediction_for(metric.name)
            if prediction.baseline is None or prediction.treatment is None:
                continue
            with self.subTest(metric=metric.name):
                if metric.higher_is_better:
                    self.assertGreater(prediction.treatment.value, prediction.baseline.value)
                else:
                    self.assertLess(prediction.treatment.value, prediction.baseline.value)

    def test_a_run_inside_every_bound_grades_as_held(self):
        result = compare.compare(
            report(**symbol_stats(precision=0.39, misattributed=60),
                   package_layer={"invented": 160, "undeclared": 0, "granularity": 0,
                                  "internal": 0, "grounded": 40},
                   module_layer={"indexed": 60, "package": 5, "package_on_disk": 0,
                                 "leaked": 10, "invented": 25, "precision": 0.65}),
            report(**symbol_stats(precision=0.61, misattributed=4),
                   package_layer={"invented": 3, "undeclared": 0, "granularity": 0,
                                  "internal": 0, "grounded": 40},
                   module_layer={"indexed": 85, "package": 5, "package_on_disk": 0,
                                 "leaked": 5, "invented": 5, "precision": 0.90}),
        )

        self.assertEqual([], result.broken_predictions)
        self.assertEqual(len(compare.METRICS), len(result.movements))

    def test_a_run_outside_a_bound_is_reported_as_broken_with_its_reason(self):
        flat = symbol_stats(precision=0.39, misattributed=60)
        result = compare.compare(report(**flat), report(**flat))

        broken = {name for movement, name, _bound, _measured in result.broken_predictions
                  if movement.metric is compare.SYMBOL_PRECISION}

        self.assertEqual({"treatment", "delta"}, broken)
        self.assertIn("did not hold", compare.render(result))
        self.assertIn("why it was predicted", compare.render(result))

    def test_an_unmeasured_bound_is_neither_held_nor_broken(self):
        """Absence of evidence must not grade as evidence."""
        result = compare.compare(report(), report())

        self.assertEqual([], result.broken_predictions)
        self.assertEqual([], result.movements)
        self.assertEqual(len(compare.METRICS), len(result.unavailable))


class TestTheRetiredMetricStaysArguedRatherThanAbsent(unittest.TestCase):
    """What is deliberately not compared, and why it is not.

    ``declared_constraints_surviving`` was Phase 1's acceptance line at 7/7.
    Deleting it quietly would leave the next reader a gap to fill on the
    strength of the gap. These tests are the other half of the retirement:
    the verdict has to survive, its evidence has to stay attached, and the
    stats it names have to still exist, so a re-addition argues past a record
    rather than past an absence.
    """

    def test_no_exclusion_is_also_a_registered_metric(self):
        """Quoting and retiring the same number would be the worst of both."""
        registered = {metric.name for metric in compare.METRICS}

        for exclusion in compare.EXCLUDED:
            with self.subTest(exclusion=exclusion.name):
                self.assertNotIn(exclusion.name, registered)

    def test_each_exclusion_carries_a_known_verdict_and_its_own_reason(self):
        known = {compare.UNMEASURABLE, compare.UNINFORMATIVE}
        reasons = [e.reason.strip() for e in compare.EXCLUDED]

        for exclusion in compare.EXCLUDED:
            with self.subTest(exclusion=exclusion.name):
                self.assertIn(exclusion.verdict, known)
        self.assertTrue(all(reasons), "an exclusion with no reason is a deletion")
        self.assertEqual(len(reasons), len(set(reasons)), "a reused reason is filler")

    def test_the_stats_an_exclusion_names_are_still_written(self):
        """Otherwise the record outlives the code and stops meaning anything."""
        _findings, stats = verify.check_declared_constraints(FACTS, entity_frame([]))

        for key in compare.CONSTRAINT_SURVIVAL.stats:
            with self.subTest(stat=key):
                self.assertIn(key, stats)

    def test_the_reason_keeps_all_three_measurements_attached(self):
        """A verdict quoted without its evidence is re-litigated on vibes."""
        evidence = {
            "16 constrained": "the denominator is 16 declarations, not 7",
            "1/7": "direct verification gives 1/7; the 7/7 was the stitch",
            "116 input documents": "no manifest reaches either Phase 7 corpus",
        }
        reason = compare.CONSTRAINT_SURVIVAL.reason

        for token, why in evidence.items():
            with self.subTest(measurement=why):
                self.assertIn(token, reason)

    def test_the_check_really_is_vacuous_on_a_corpus_holding_no_manifest(self):
        """The third measurement, asserted against the code rather than quoted.

        Both Phase 7 corpora hold only files under ``celery/``. Every declared
        constraint is then held out as unindexed, so the ratio reads 0/0
        whichever header variant the run used, and a differ quoting it would
        report a tie it never measured.
        """
        facts = RepoFacts(
            root="/tmp/celery-255",
            own_modules=["celery"],
            packages=[
                Package(
                    name="kombu",
                    ecosystem="python",
                    layer=Layer.PACKAGE,
                    source_file="requirements/default.txt",
                    constraint="<2.2.0,>=2.1.8",
                    declared_line="kombu>=2.1.8,<2.2.0",
                )
            ],
        )
        sources_only = Corpus(files=[corpus_file("celery/worker/state.py")])

        _findings, stats = verify.check_declared_constraints(
            facts, entity_frame([("kombu", "library", "Messaging.")]), sources_only
        )

        self.assertEqual(0, stats["declared_constraints"])
        self.assertEqual(1, stats["constraints_unindexed"])

    def test_an_unretired_name_is_not_silently_treated_as_retired(self):
        self.assertIsNone(compare.excluded_for("symbol_precision"))
        self.assertIs(
            compare.CONSTRAINT_SURVIVAL,
            compare.excluded_for("declared_constraints_surviving"),
        )


class TestBounds(unittest.TestCase):
    CASES = [
        (compare.Bound("<", 0.45), 0.39, True),
        (compare.Bound("<", 0.45), 0.45, False),
        (compare.Bound("<=", -30), -30, True),
        (compare.Bound(">", 0.55), 0.61, True),
        (compare.Bound(">=", 0.15), 0.14, False),
    ]

    def test_each_comparison_grades_as_written(self):
        for bound, measured, expected in self.CASES:
            with self.subTest(bound=str(bound), measured=measured):
                self.assertEqual(expected, bound.holds(measured))

    def test_nothing_measured_grades_as_nothing(self):
        self.assertIsNone(compare.Bound("<", 1).holds(None))


class TestRendering(unittest.TestCase):
    def rendered(self):
        return compare.render(compare.compare(
            report(**symbol_stats(precision=0.39, misattributed=60)),
            report(**symbol_stats(precision=0.61, misattributed=4)),
        ))

    def test_the_experiment_names_the_population_it_was_run_on(self):
        self.assertIn("celery 2.5.5", self.rendered())
        self.assertIn("gpt-oss-20b", compare.EXPERIMENT)

    def test_every_metric_prints_predicted_against_measured(self):
        text = self.rendered()

        self.assertIn("predicted baseline < 0.45, measured 0.39 -> held", text)
        self.assertIn("predicted delta >= 0.15, measured 0.22 -> held", text)

    def test_both_readings_carry_their_own_population(self):
        """Not one shared population: the two indexes hold different entities.

        Here the baseline carries 150 symbol entities and the treatment 94,
        which is the ordinary case rather than a contrived one. A rate printed
        without the count behind it invites comparing two numbers measured
        over populations that differ by half.
        """
        text = self.rendered()

        self.assertIn("n=150", text)
        self.assertIn("n=94", text)

    def test_a_clean_sweep_is_reported_with_suspicion(self):
        """Five prediction sets so far, four with a wrong entry. Zero is a smell."""
        self.assertIn("worth doubting", self.rendered())


if __name__ == "__main__":
    unittest.main()
