"""Metadata-only local fixtures and mocked MLflow calls; no external services."""
import asyncio
import io
import json
import logging
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests import test_token_tracker  # Existing optional-dependency stubs.
from loaders import mlflow_asset_loader as loader_module
from utils import artifact_measurement as measurement
from utils import request_timing as timing


class TestMlflowTiming(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"AUGUR_TIMING_ENABLED": "true", "AUGUR_RUN_ID": "mock-pipeline"})
        self.env.start()
        self.output = io.StringIO()
        self.handler = logging.StreamHandler(self.output)
        self.level = timing._logger.level
        timing._logger.setLevel(logging.INFO)
        timing._logger.addHandler(self.handler)
        self.mlflow = MagicMock()
        self.mlflow.active_run.return_value = None
        self.context = self.make_context("a" * 32)
        self.mlflow.start_run.return_value = self.context
        self.api_patch = patch.object(loader_module, "mlflow", self.mlflow)
        self.api_patch.start()
        self.client = MagicMock()
        self.client_patch = patch.object(loader_module, "MlflowClient", return_value=self.client)
        self.client_patch.start()
        self.loader = object.__new__(loader_module.MlFlowAssetLoader)
        self.loader.get_or_create_experiment_by_name = MagicMock(return_value=types.SimpleNamespace(
            experiment_id="123", artifact_location="s3://example-bucket/example-prefix"))
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "private-source"
        self.path.mkdir()

    def tearDown(self):
        self.tmp.cleanup()
        self.api_patch.stop()
        self.client_patch.stop()
        timing._logger.removeHandler(self.handler)
        timing._logger.setLevel(self.level)
        self.env.stop()

    def make_context(self, run_id):
        context = MagicMock()
        context.info = types.SimpleNamespace(run_id=run_id)
        context.__enter__.return_value = context
        context.__exit__.return_value = False
        return context

    def events(self):
        return [json.loads(line.split("AUGUR_TIMING ", 1)[1])
                for line in self.output.getvalue().splitlines() if "AUGUR_TIMING " in line]

    def test_disabled_does_no_extra_measurement_or_run_metadata_work(self):
        (self.path / "data").write_bytes(b"abc")
        with patch.dict(os.environ, {"AUGUR_TIMING_ENABLED": "false"}), \
             patch.object(loader_module, "measure_payload", side_effect=AssertionError("scan")), \
             patch.object(loader_module, "upload_identity", side_effect=AssertionError("identity")):
            self.loader.log_results(str(self.path), artifact_path="original", tags={"phase": "original"})
        self.mlflow.log_artifacts.assert_called_once_with(str(self.path), artifact_path="original")
        self.mlflow.set_tags.assert_called_once_with({"phase": "original"})
        self.context.__exit__.assert_called_once_with(None, None, None)
        self.assertEqual(self.events(), [])

    def test_multipass_growth_correlates_attempts_and_new_runs_without_paths(self):
        (self.path / "first").write_bytes(b"abc")
        second = self.make_context("b" * 32)
        self.mlflow.start_run.side_effect = [self.context, second]
        self.loader.log_results(str(self.path), artifact_path="private-target", tags={"secret": "secret-tag"})
        (self.path / "second").write_bytes(b"defgh")
        self.loader.log_results(str(self.path), artifact_path="private-target")
        uploads = [e for e in self.events() if e["operation"] == "mlflow.artifact_upload"]
        self.assertEqual([e["payload_regular_files"] for e in uploads], [1, 2])
        self.assertEqual([e["payload_logical_bytes"] for e in uploads], [3, 8])
        self.assertEqual([e["upload_ordinal"] for e in uploads], [1, 2])
        self.assertEqual(uploads[0]["payload_key"], uploads[1]["payload_key"])
        self.assertEqual([e["mlflow_run_id"] for e in uploads], ["a" * 32, "b" * 32])
        for upload in uploads:
            parent = next(e for e in self.events() if e["span_id"] == upload["parent_span_id"])
            self.assertEqual(parent["operation"], "mlflow.run_lifecycle")
            self.assertEqual(parent["trace_id"], upload["trace_id"])
            self.assertTrue(upload["payload_scan_complete"])
            self.assertNotIn("network_bytes", upload)
        creations = [e for e in self.events() if e["operation"] == "mlflow.run_create"]
        self.assertEqual([e["mlflow_run_id"] for e in creations], ["a" * 32, "b" * 32])
        for secret in (str(self.path), "private-source", "private-target", "secret-tag", "s3://"):
            self.assertNotIn(secret, self.output.getvalue())

    def test_single_file_content_is_measured_after_write_and_upload_unchanged(self):
        path = self.path / "result.json"
        self.loader.log_results(str(path), content="abc", artifact_path="original")
        self.mlflow.log_artifact.assert_called_once_with(str(path), artifact_path="original")
        upload = next(e for e in self.events() if e["operation"] == "mlflow.artifact_upload")
        self.assertEqual(upload["payload_regular_files"], 1)
        self.assertEqual(upload["payload_logical_bytes"], 3)

    def test_upload_failure_and_cancellation_keep_context_and_exception(self):
        for error in (RuntimeError("private exception content"), asyncio.CancelledError("private cancel")):
            self.output.truncate(0); self.output.seek(0)
            self.context.reset_mock()
            self.mlflow.log_artifacts.side_effect = error
            with self.assertRaises(type(error)) as caught:
                self.loader.log_results(str(self.path))
            self.assertIs(caught.exception, error)
            self.assertIs(self.context.__exit__.call_args.args[1], error)
            for op in ("mlflow.artifact_upload", "mlflow.run_lifecycle", "mlflow.log_results"):
                event = next(e for e in self.events() if e["operation"] == op)
                self.assertEqual(event["status"], "failed")
                self.assertEqual(event["error_type"], type(error).__name__)
            self.assertNotIn("private", self.output.getvalue())

    def test_run_creation_failure_does_not_upload(self):
        error = ValueError("private failure")
        self.mlflow.start_run.side_effect = error
        with self.assertRaises(ValueError) as caught:
            self.loader.log_results(str(self.path))
        self.assertIs(caught.exception, error)
        self.mlflow.log_artifacts.assert_not_called()
        creation = next(e for e in self.events() if e["operation"] == "mlflow.run_create")
        self.assertEqual(creation["status"], "failed")

    def test_prior_run_end_and_lifecycle_close_are_preserved(self):
        self.mlflow.active_run.return_value = types.SimpleNamespace(info=types.SimpleNamespace(run_id="c" * 32))
        self.loader.log_results(str(self.path))
        self.mlflow.active_run.assert_called_once_with()
        self.mlflow.end_run.assert_called_once_with()
        prior = next(e for e in self.events() if e["operation"] == "mlflow.prior_run_end")
        self.assertEqual(prior["prior_mlflow_run_id"], "c" * 32)
        self.context.__exit__.assert_called_once_with(None, None, None)

    def test_artifact_lookup_is_separate_parented_and_preserves_uri_and_failure(self):
        self.client.search_runs.return_value = []
        with timing.timing_span("caller"):
            uri = self.loader._get_absolute_artifact_uri("private-file", "private-experiment", {"secret": "private-tag"})
        self.assertEqual(uri, "s3://example-bucket/example-prefix/private-file")
        lookup, parent = self.events()
        self.assertEqual(lookup["operation"], "mlflow.artifact_uri_lookup")
        self.assertEqual(lookup["parent_span_id"], parent["span_id"])
        self.assertNotIn("private", self.output.getvalue())
        error = TimeoutError("private URI")
        self.client.search_runs.side_effect = error
        with self.assertRaises(TimeoutError) as caught:
            self.loader._get_absolute_artifact_uri("file", "experiment")
        self.assertIs(caught.exception, error)
        self.assertEqual(self.events()[-1]["status"], "failed")

    def test_symlinks_and_missing_files_are_partial_without_following_targets(self):
        (self.path / "data").write_bytes(b"abc")
        outside = Path(self.tmp.name) / "outside"
        outside.write_bytes(b"abcdefghij")
        (self.path / "link").symlink_to(outside)
        counts = measurement.measure_payload(str(self.path))
        self.assertEqual(counts["payload_logical_bytes"], 3)
        self.assertEqual(counts["payload_symlinks"], 1)
        self.assertFalse(counts["payload_scan_complete"])
        missing = measurement.measure_payload(str(self.path / "missing"))
        self.assertEqual(missing["payload_scan_errors"], 1)
        self.assertFalse(missing["payload_scan_complete"])

    def test_scan_budgets_are_partial_and_do_not_change_upload(self):
        for i in range(4):
            (self.path / str(i)).write_bytes(b"abc")
        with patch.object(measurement, "_MAX_ENTRIES", 2):
            self.loader.log_results(str(self.path))
        upload = next(e for e in self.events() if e["operation"] == "mlflow.artifact_upload")
        self.assertEqual(upload["payload_scan_entries"], 2)
        self.assertTrue(upload["payload_scan_truncated"])
        self.assertFalse(upload["payload_scan_complete"])
        self.mlflow.log_artifacts.assert_called_once_with(str(self.path), artifact_path=None)
        with patch.object(measurement, "_MAX_SECONDS", 0):
            self.assertTrue(measurement.measure_payload(str(self.path))["payload_scan_truncated"])
        with patch.object(measurement, "_MAX_DEPTH", 0):
            self.assertTrue(measurement.measure_payload(str(self.path))["payload_scan_truncated"])

    def test_unsafe_backend_metadata_and_tags_are_not_emitted(self):
        self.context.info.run_id = "https://example.invalid/token"
        self.loader.get_or_create_experiment_by_name.return_value.experiment_id = "private/experiment"
        self.loader.log_results(str(self.path), tags={"token": "private-tag"})
        for event in self.events():
            self.assertNotIn("mlflow_run_id", event)
            self.assertNotIn("experiment_id", event)
        self.assertNotIn("private", self.output.getvalue())

    def test_scan_io_failure_is_partial_and_does_not_prevent_upload(self):
        with patch.object(measurement.os, "scandir", side_effect=PermissionError("private path")):
            self.loader.log_results(str(self.path))
        self.mlflow.log_artifacts.assert_called_once_with(str(self.path), artifact_path=None)
        upload=next(e for e in self.events() if e["operation"] == "mlflow.artifact_upload")
        self.assertFalse(upload["payload_scan_complete"])
        self.assertEqual(upload["payload_scan_errors"],1)
        self.assertNotIn("private",self.output.getvalue())

    def test_run_close_failure_propagates_and_lifecycle_records_failure(self):
        error=RuntimeError("private close failure")
        self.context.__exit__.side_effect=error
        with self.assertRaises(RuntimeError) as caught:
            self.loader.log_results(str(self.path))
        self.assertIs(caught.exception,error)
        upload=next(e for e in self.events() if e["operation"] == "mlflow.artifact_upload")
        lifecycle=next(e for e in self.events() if e["operation"] == "mlflow.run_lifecycle")
        self.assertEqual(upload["status"],"success")
        self.assertEqual(lifecycle["status"],"failed")


if __name__ == "__main__":
    unittest.main()
