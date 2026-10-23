import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from provenance import arms, cli

TREE = {
    "app/__init__.py": "",
    "requirements.txt": "kombu>=2.1.8,<2.2.0\nanyjson==0.3.1\n",
    "Containerfile": "FROM registry.access.redhat.com/ubi9/python-311:latest\n",
}


class CliTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name).resolve()
        self.repo = self.work / "repo"
        for relative, contents in TREE.items():
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")
        self.facts_path = self.work / "facts.json"

    def run_cli(self, *argv):
        """Invoke main, capturing stdout and the exit status."""
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            status = cli.main(list(argv))
        return status, buffer.getvalue()

    def extract(self, *extra):
        return self.run_cli(
            "extract", str(self.repo), "--no-syft", "-o", str(self.facts_path), *extra
        )

    def write_index(self, rows, name="output"):
        # Imported here, not at module scope: see tests/conftest.py.
        import pandas as pd

        output = self.work / name
        output.mkdir(exist_ok=True)
        pd.DataFrame(rows, columns=["title", "type", "description"]).to_parquet(
            output / "entities.parquet"
        )
        return output


class TestExtractCommand(CliTestCase):
    def test_human_summary_reports_what_was_found(self):
        status, output = self.extract()
        self.assertEqual(status, cli.EXIT_OK)
        self.assertIn("packages:    2 declarations, 2 distinct", output)
        self.assertIn("own modules: app", output)
        self.assertIn("base images: 1", output)

    def test_facts_file_is_written_and_reloadable(self):
        self.extract()
        data = json.loads(self.facts_path.read_text(encoding="utf-8"))
        self.assertEqual(
            sorted(p["name"] for p in data["packages"]), ["anyjson", "kombu"]
        )
        self.assertEqual(data["own_modules"], ["app"])

    def test_json_flag_emits_the_full_record_on_stdout(self):
        status, output = self.extract("--json")
        self.assertEqual(status, cli.EXIT_OK)
        self.assertEqual(json.loads(output)["base_images"][0]["line_number"], 1)

    def test_a_missing_checkout_is_an_error_not_a_traceback(self):
        status = cli.main(["extract", str(self.work / "nope"), "--no-syft"])
        self.assertEqual(status, cli.EXIT_ERROR)


class TestVerifyCommand(CliTestCase):
    def setUp(self):
        super().setUp()
        self.extract()

    def test_a_clean_index_exits_zero(self):
        output = self.write_index([
            ("kombu", "library", "Messaging. Version: >=2.1.8,<2.2.0"),
            ("anyjson", "library", "JSON wrapper. Version: ==0.3.1"),
        ])
        status, text = self.run_cli("verify", str(self.facts_path), str(output))
        self.assertEqual(status, cli.EXIT_OK, msg=text)
        self.assertIn("verdict: clean", text)

    def test_findings_exit_nonzero_so_a_pipeline_runner_notices(self):
        output = self.write_index([
            ("kombu", "library", "A messaging library"),
            ("click", "library", "A command line toolkit"),
        ])
        status, text = self.run_cli("verify", str(self.facts_path), str(output))
        self.assertEqual(status, cli.EXIT_FINDINGS)
        self.assertIn("no manifest declares it and no source file imports it", text)

    def test_json_output_carries_the_counts(self):
        output = self.write_index([("kombu", "library", "A messaging library")])
        status, text = self.run_cli("verify", str(self.facts_path), str(output), "--json")
        self.assertEqual(status, cli.EXIT_FINDINGS)
        report = json.loads(text)
        self.assertFalse(report["ok"])
        self.assertEqual(report["stats"]["declared_constraints"], 2)

    def test_an_unbuilt_index_is_an_error_not_a_traceback(self):
        status = cli.main(["verify", str(self.facts_path), str(self.work)])
        self.assertEqual(status, cli.EXIT_ERROR)


class TestStitchCommand(CliTestCase):
    """stitch then verify, which is the order the pipeline runs them in."""

    MODEL_OUTPUT = [
        ("kombu", "library", "A messaging library, required at 2.1.8 or above."),
        ("anyjson", "library", "A JSON wrapper."),
    ]

    def setUp(self):
        super().setUp()
        self.extract()

    def test_stitching_turns_a_failing_verify_into_a_passing_one(self):
        output = self.write_index(self.MODEL_OUTPUT)
        status, _ = self.run_cli("verify", str(self.facts_path), str(output))
        self.assertEqual(status, cli.EXIT_FINDINGS)

        status, text = self.run_cli("stitch", str(self.facts_path), str(output))
        self.assertEqual(status, cli.EXIT_OK)
        self.assertIn("stitched:    2", text)

        status, text = self.run_cli("verify", str(self.facts_path), str(output))
        self.assertEqual(status, cli.EXIT_OK, msg=text)
        self.assertIn("declared constraints surviving into the graph: 2/2", text)

    def test_a_dry_run_leaves_the_index_failing(self):
        output = self.write_index(self.MODEL_OUTPUT)
        self.run_cli("stitch", str(self.facts_path), str(output), "--dry-run")
        status, _ = self.run_cli("verify", str(self.facts_path), str(output))
        self.assertEqual(status, cli.EXIT_FINDINGS)
        self.assertFalse((output / "entities.parquet.orig").exists())

    def test_json_output_lists_each_change(self):
        output = self.write_index(self.MODEL_OUTPUT)
        _, text = self.run_cli("stitch", str(self.facts_path), str(output), "--json")
        report = json.loads(text)
        self.assertEqual(report["changed"], 2)
        self.assertEqual(
            sorted(c["after"] for c in report["changes"]),
            ["Version: ==0.3.1", "Version: >=2.1.8,<2.2.0"],
        )

    def test_backup_can_be_declined(self):
        output = self.write_index(self.MODEL_OUTPUT)
        self.run_cli("stitch", str(self.facts_path), str(output), "--no-backup")
        self.assertFalse((output / "entities.parquet.orig").exists())

    def test_an_unbuilt_index_is_an_error_not_a_traceback(self):
        status = cli.main(["stitch", str(self.facts_path), str(self.work)])
        self.assertEqual(status, cli.EXIT_ERROR)


@unittest.skipUnless(shutil.which("veripak"), "veripak is not installed")
class TestHealthCommand(CliTestCase):
    """Driven entirely from a seeded cache, so the test makes no network call."""

    PAYLOAD = {
        "package": "kombu",
        "ecosystem": "python",
        "versions_in_use": [],
        "version": {"version": "5.6.2", "method": "registry_api"},
        "download": {"confirmed": True},
        "eol": {"project_status": "active", "eol": False, "confidence": "medium"},
        "cves": {"total_count": 0, "high_critical_count": 0},
        "hitl_flags": [],
    }

    def setUp(self):
        super().setUp()
        self.extract()
        from provenance import health

        self.cache = self.work / "cache"
        for name, versions in (("kombu", ()), ("anyjson", ("0.3.1",))):
            path = health._cache_path(self.cache, "python", name, versions)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = dict(self.PAYLOAD, package=name)
            path.write_text(json.dumps(payload), encoding="utf-8")

    def test_cached_results_are_summarized_for_every_declaration(self):
        status, text = self.run_cli(
            "health", str(self.facts_path), "--cache", str(self.cache)
        )
        self.assertEqual(status, cli.EXIT_OK)
        results = json.loads(text)
        self.assertEqual(sorted(results), ["anyjson", "kombu"])
        self.assertEqual(results["kombu"]["latest_upstream"], "5.6.2")

    def test_limit_bounds_how_many_distributions_are_audited(self):
        status, text = self.run_cli(
            "health", str(self.facts_path), "--cache", str(self.cache), "--limit", "1"
        )
        self.assertEqual(status, cli.EXIT_OK)
        self.assertEqual(list(json.loads(text)), ["anyjson"])

    def test_output_file_is_written_when_requested(self):
        destination = self.work / "health.json"
        self.run_cli(
            "health", str(self.facts_path), "--cache", str(self.cache),
            "-o", str(destination),
        )
        self.assertIn("kombu", json.loads(destination.read_text(encoding="utf-8")))


class TestCompareCommand(CliTestCase):
    """Two indexes of one checkout, which is the shape of an ingest experiment.

    The arithmetic is covered in ``test_provenance_compare.py`` against
    synthetic reports. What is tested here is that the subcommand verifies
    both directories against the same facts and that the exit status carries
    the prediction verdict, since a runner reads that and not the prose.
    """

    DECLARED = [
        ("kombu", "library", "Messaging. Version: >=2.1.8,<2.2.0"),
        ("anyjson", "library", "JSON wrapper. Version: ==0.3.1"),
    ]

    def setUp(self):
        super().setUp()
        self.extract()

    def hallucinated(self, count):
        """Package entities no manifest declares and no source file imports."""
        return [(f"ghost-{n}", "library", "A library this repo never names") for n in range(count)]

    def compare(self, baseline_rows, treatment_rows, *extra):
        baseline = self.write_index(baseline_rows, name="positional")
        treatment = self.write_index(treatment_rows, name="parser")
        return self.run_cli(
            "compare", str(self.facts_path), str(baseline), str(treatment), *extra
        )

    def test_a_run_matching_every_prediction_exits_zero(self):
        status, text = self.compare(
            self.DECLARED + self.hallucinated(40), self.DECLARED + self.hallucinated(2)
        )

        self.assertEqual(cli.EXIT_OK, status, msg=text)
        self.assertIn("invented_package_entities", text)
        self.assertIn("-> held", text)

    def test_a_broken_prediction_exits_nonzero_and_prints_its_reason(self):
        rows = self.DECLARED + self.hallucinated(5)

        status, text = self.compare(rows, rows)

        self.assertEqual(cli.EXIT_FINDINGS, status)
        self.assertIn("predictions that did not hold", text)
        self.assertIn("why it was predicted", text)

    def test_both_indexes_are_read_rather_than_one_twice(self):
        _status, text = self.compare(
            self.DECLARED + self.hallucinated(40), self.DECLARED + self.hallucinated(2)
        )

        self.assertIn("40", text)
        self.assertIn("delta        -38", text)

    def test_the_labels_name_the_two_sides(self):
        _status, text = self.compare(
            self.DECLARED, self.DECLARED, "--labels", "before", "after"
        )

        self.assertIn("before -> after", text)

    def test_the_experiment_line_is_overridable_so_a_run_is_attributable(self):
        _status, text = self.compare(self.DECLARED, self.DECLARED, "--experiment", "a dry run")

        self.assertIn("experiment: a dry run", text)

    def test_an_arm_brings_its_own_predictions_labels_and_experiment(self):
        """The flat run the header arm calls broken is the one Arm A predicts."""
        status, text = self.compare(self.DECLARED, self.DECLARED, "--arm", "purpose")

        self.assertEqual(cli.EXIT_OK, status, msg=text)
        self.assertIn("parser -> purpose", text)
        self.assertIn("experiment: " + arms.PURPOSE.experiment, text)

    def test_json_output_carries_the_verdicts_alongside_the_numbers(self):
        _status, text = self.compare(
            self.DECLARED + self.hallucinated(40),
            self.DECLARED + self.hallucinated(2),
            "--json",
        )

        payload = json.loads(text)
        invented = next(
            m for m in payload["metrics"] if m["metric"] == "invented_package_entities"
        )
        self.assertEqual(40, invented["positional"])
        self.assertEqual(2, invented["parser"])
        self.assertTrue(invented["improved"])
        self.assertTrue(all(p["held"] for p in invented["predictions"]))

    def test_an_unbuilt_index_is_an_error_not_a_traceback(self):
        built = self.write_index(self.DECLARED, name="positional")

        status = cli.main(["compare", str(self.facts_path), str(built), str(self.work)])

        self.assertEqual(cli.EXIT_ERROR, status)


class TestParser(unittest.TestCase):
    def test_a_subcommand_is_required(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args([])

    def test_syft_runs_by_default_and_is_opt_out(self):
        parser = cli.build_parser()
        self.assertTrue(parser.parse_args(["extract", "."]).syft)
        self.assertFalse(parser.parse_args(["extract", ".", "--no-syft"]).syft)


if __name__ == "__main__":
    unittest.main()
