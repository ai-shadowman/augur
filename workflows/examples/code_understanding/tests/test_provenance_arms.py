"""The registered experiments stay checkable, and each one can lose.

``test_provenance_compare.py`` pins the first arm's bounds in detail. What is
pinned here is what every arm owes: a bound on every metric, a reason that is
its own, and a baseline quoted in the reason that is the baseline actually
measured. Arm A adds one more obligation, since every one of its predictions
says the treatment will not win: a run in which it does win has to break
something, or the arm could not have reported the outcome it exists to detect.
"""

import re
import unittest

from provenance import arms, compare
from provenance.models import VerificationReport

#: ``verify`` on ``augur-test/runs/parser-full-120b`` and its ``-dedup`` join,
#: read 2026-10-23 with the code that registered Arm A. Each prediction's
#: reason quotes its baseline, and the pin below holds the two together.
MEASURED = {
    "purpose": {
        "symbol_layer": {"declared": 1431, "misattributed": 773, "leaked": 6,
                         "invented": 262, "bare": 141, "precision": 0.548},
        "module_layer": {"indexed": 269, "package": 4, "package_on_disk": 0,
                         "leaked": 0, "invented": 86, "precision": 0.76},
        "package_layer": {"invented": 10, "undeclared": 16, "granularity": 2,
                          "internal": 1, "grounded": 7},
    },
    "purpose-joined": {
        "symbol_layer": {"declared": 1671, "misattributed": 53, "leaked": 5,
                         "invented": 259, "bare": 141, "precision": 0.785},
        "module_layer": {"indexed": 116, "package": 4, "package_on_disk": 0,
                         "leaked": 0, "invented": 78, "precision": 0.606},
        "package_layer": {"invented": 10, "undeclared": 16, "granularity": 2,
                          "internal": 1, "grounded": 10},
    },
}

METRICS = {metric.name: metric for metric in compare.METRICS}


def measured(arm_name, **overrides):
    """The recorded baseline, with named fields replaced, as a bare report."""
    stats = {section: dict(counts) for section, counts in MEASURED[arm_name].items()}
    for key, value in overrides.items():
        section, field = key.split("__")
        stats[section][field] = value
    return VerificationReport(stats=stats)


class TestEveryArmIsComplete(unittest.TestCase):
    def test_the_registry_is_keyed_by_each_arms_own_name(self):
        self.assertEqual({arm.name for arm in arms.ARMS.values()}, set(arms.ARMS))

    def test_the_first_arm_is_the_one_compare_defaults_to(self):
        """``compare`` with no arm named must grade as it did before arms existed."""
        self.assertIs(compare.PREDICTIONS, arms.HEADER.predictions)
        self.assertEqual(compare.EXPERIMENT, arms.HEADER.experiment)

    def test_every_arm_predicts_every_metric_once(self):
        for arm in arms.ARMS.values():
            with self.subTest(arm=arm.name):
                predicted = [p.metric for p in arm.predictions]
                self.assertCountEqual(METRICS, predicted)

    def test_every_prediction_makes_a_claim_with_a_known_operator(self):
        for arm in arms.ARMS.values():
            for prediction in arm.predictions:
                with self.subTest(arm=arm.name, metric=prediction.metric):
                    self.assertTrue(prediction.bounds())
                    for _name, bound in prediction.bounds():
                        self.assertIn(bound.op, compare._OPS)

    def test_no_reason_is_reused_across_arms(self):
        reasons = [p.reason.strip() for arm in arms.ARMS.values() for p in arm.predictions]

        self.assertTrue(all(reasons))
        self.assertEqual(len(reasons), len(set(reasons)), "a reused reason is filler")

    def test_each_arm_names_its_population_model_and_sides(self):
        for arm in arms.ARMS.values():
            with self.subTest(arm=arm.name):
                self.assertIn("celery 2.5.5", arm.experiment)
                self.assertIn("gpt-oss", arm.experiment)
                self.assertNotEqual(*arm.labels)


class TestArmAQuotesTheBaselineItWasWrittenAgainst(unittest.TestCase):
    """A reason citing a stale baseline argues from a number nobody measured."""

    def test_each_reason_quotes_the_measured_baseline(self):
        for arm_name in MEASURED:
            for prediction in arms.ARMS[arm_name].predictions:
                with self.subTest(arm=arm_name, metric=prediction.metric):
                    quoted = re.match(r"Baseline ([0-9]+(?:\.[0-9]+)?)", prediction.reason)
                    self.assertIsNotNone(quoted, prediction.reason[:60])
                    reading = compare.read(measured(arm_name), METRICS[prediction.metric])
                    self.assertEqual(float(quoted.group(1)), reading.value)


class TestArmACanLose(unittest.TestCase):
    """Every Arm A bound predicts no win, so a win has to grade as broken."""

    def grade(self, arm_name, **treatment):
        return compare.compare(
            measured(arm_name),
            measured(arm_name, **treatment),
            labels=arms.ARMS[arm_name].labels,
            predictions=arms.ARMS[arm_name].predictions,
        )

    def broken(self, result):
        return {(m.metric.name, name) for m, name, _bound, _measured in result.broken_predictions}

    def test_a_run_identical_to_its_baseline_holds_everywhere(self):
        for arm_name in MEASURED:
            with self.subTest(arm=arm_name):
                self.assertEqual(set(), self.broken(self.grade(arm_name)))

    def test_a_real_gain_on_each_metric_breaks_its_bound(self):
        wins = {
            "purpose": [
                ("symbol_precision", {"symbol_layer__precision": 0.62}),
                ("misattributed_symbols", {"symbol_layer__misattributed": 400}),
                ("invented_package_entities", {"package_layer__invented": 4}),
                ("module_precision", {"module_layer__precision": 0.82}),
            ],
            "purpose-joined": [
                ("symbol_precision", {"symbol_layer__precision": 0.86}),
                ("misattributed_symbols", {"symbol_layer__misattributed": 20}),
                ("invented_package_entities", {"package_layer__invented": 4}),
                ("module_precision", {"module_layer__precision": 0.72}),
            ],
        }
        for arm_name, cases in wins.items():
            for metric, treatment in cases:
                with self.subTest(arm=arm_name, metric=metric):
                    broken = {name for m, name in self.broken(self.grade(arm_name, **treatment))
                              if m == metric}
                    self.assertTrue(broken, f"a win on {metric} graded as held")

    def test_the_volume_effect_on_joined_modules_is_predicted_not_credited(self):
        """78 invented modules falling to 64 with volume gives 120/184, about 0.652."""
        result = self.grade("purpose-joined", module_layer__invented=64,
                            module_layer__precision=0.652)

        self.assertEqual(set(), self.broken(result))


if __name__ == "__main__":
    unittest.main()
