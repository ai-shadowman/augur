"""No network/model calls: exercise attribution, privacy, failures and concurrency."""
import asyncio
import io
import json
import logging
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# Reuse the existing suite's dependency stubs, not installed provider packages.
from tests.test_token_tracker import TokenCostTracker
from utils import request_timing as timing
from utils.duration_tracker import DurationTracker
from utils.kubeflow_utils import read_from_input_artifact, write_to_output_artifact


class TestRequestTiming(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"AUGUR_TIMING_ENABLED": "true", "AUGUR_RUN_ID": "test-run"})
        self.env.start()
        self.output = io.StringIO()
        self.handler = logging.StreamHandler(self.output)
        self.level = timing._logger.level
        timing._logger.setLevel(logging.INFO)
        timing._logger.addHandler(self.handler)
        DurationTracker.reset_instance()

    def tearDown(self):
        timing._logger.removeHandler(self.handler)
        timing._logger.setLevel(self.level)
        self.env.stop()

    def events(self):
        return [json.loads(line.split("AUGUR_TIMING ", 1)[1])
                for line in self.output.getvalue().splitlines() if "AUGUR_TIMING " in line]

    def test_opt_in_and_failure_preserve_exception_and_hide_content(self):
        error = ValueError("secret-token and customer source text")
        with self.assertRaises(ValueError) as caught:
            with timing.timing_span("sdk.test", "sdk", prompt="secret-prompt", api_key="secret-key"):
                raise error
        self.assertIs(caught.exception, error)
        event = self.events()[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_type"], "ValueError")
        self.assertFalse(event["additive"])
        self.assertNotIn("secret", self.output.getvalue())
        self.assertGreaterEqual(event["elapsed_seconds"], 0)
        self.output.truncate(0); self.output.seek(0)
        with patch.dict(os.environ, {"AUGUR_TIMING_ENABLED": "false"}):
            with timing.timing_span("disabled"):
                pass
        self.assertEqual(self.events(), [])

    def test_repeated_spans_are_unique_without_touching_duration_totals(self):
        for _ in range(2):
            with timing.timing_span("same.operation"):
                pass
        self.assertNotEqual(self.events()[0]["span_id"], self.events()[1]["span_id"])
        self.assertEqual(DurationTracker.get_instance().records, [])

    def test_concurrent_contexts_keep_their_parents(self):
        @timing.timed("query")
        async def query():
            await asyncio.sleep(0)
            with timing.timing_span("sdk", "sdk"):
                await asyncio.sleep(0)
        async def run():
            await asyncio.gather(query(), query())
        asyncio.run(run())
        events = self.events()
        parents = {e["span_id"]: e for e in events if e["operation"] == "query"}
        children = [e for e in events if e["operation"] == "sdk"]
        self.assertEqual(len(parents), 2)
        self.assertEqual(len({e["parent_span_id"] for e in children}), 2)
        for child in children:
            self.assertEqual(child["trace_id"], parents[child["parent_span_id"]]["trace_id"])

    def test_logger_failure_does_not_change_return_value(self):
        @timing.timed("test")
        def call():
            return "original"
        with patch.object(timing._logger, "info", side_effect=RuntimeError("logger failed")):
            self.assertEqual(call(), "original")

    def fake_sdk(self, failure=None):
        response = types.SimpleNamespace(model="test-model", _request_id="req-backend-123",
                                         usage=types.SimpleNamespace(prompt_tokens=40, completion_tokens=12))
        async def async_create(*args, **kwargs):
            await asyncio.sleep(0)
            if failure is not None:
                raise failure
            return response
        def sync_create(*args, **kwargs):
            if failure is not None:
                raise failure
            return response
        names = ("openai", "openai.resources", "openai.resources.chat",
                 "openai.resources.chat.completions", "openai.resources.embeddings")
        modules = {name: types.ModuleType(name) for name in names}
        chat = modules[names[3]]; embed = modules[names[4]]
        chat.AsyncCompletions = type("AsyncCompletions", (), {"create": staticmethod(async_create)})
        chat.Completions = type("Completions", (), {"create": staticmethod(sync_create)})
        embed.AsyncEmbeddings = type("AsyncEmbeddings", (), {"create": staticmethod(async_create)})
        embed.Embeddings = type("Embeddings", (), {"create": staticmethod(sync_create)})
        return modules, chat, embed, response

    def test_all_sdk_paths_preserve_returns_metrics_and_sanitized_events(self):
        modules, chat, embed, response = self.fake_sdk()
        tracker = TokenCostTracker(print_to_console=False)
        with patch.dict(sys.modules, modules):
            tracker.enable_openai_tracking()
            try:
                self.assertIs(asyncio.run(chat.AsyncCompletions.create(model="test-model", messages=["secret-source"])), response)
                self.assertIs(chat.Completions.create(model="test-model", api_key="secret-key"), response)
                self.assertIs(asyncio.run(embed.AsyncEmbeddings.create(model="test-model", input="secret-input")), response)
                self.assertIs(embed.Embeddings.create(model="test-model"), response)
            finally:
                tracker.disable_openai_tracking()
        self.assertEqual(tracker.get_totals()["total_calls"], 4)
        self.assertEqual(tracker.get_totals()["total_prompt_tokens"], 160)
        self.assertEqual(len(self.events()), 4)
        self.assertEqual(len({e["span_id"] for e in self.events()}), 4)
        for event in self.events():
            self.assertEqual(event["prompt_tokens"], 40)
            self.assertEqual(event["provider_request_id"], "req-backend-123")
            self.assertEqual(event["layer"], "sdk")
        self.assertNotIn("secret", self.output.getvalue())

    def test_all_sdk_failure_paths_and_cancellation_preserve_exception(self):
        for error in (RuntimeError("secret-failure"), asyncio.CancelledError("secret-cancel")):
            modules, chat, embed, _ = self.fake_sdk(error)
            tracker = TokenCostTracker(print_to_console=False)
            with patch.dict(sys.modules, modules):
                tracker.enable_openai_tracking()
                try:
                    calls = (lambda: asyncio.run(chat.AsyncCompletions.create()),
                             lambda: chat.Completions.create(),
                             lambda: asyncio.run(embed.AsyncEmbeddings.create()),
                             lambda: embed.Embeddings.create())
                    for call in calls:
                        with self.assertRaises(type(error)) as caught:
                            call()
                        self.assertIs(caught.exception, error)
                finally:
                    tracker.disable_openai_tracking()
            self.assertEqual(tracker.get_totals()["total_calls"], 0)
        self.assertEqual(len(self.events()), 8)
        self.assertTrue(all(e["status"] == "failed" for e in self.events()))
        self.assertNotIn("secret", self.output.getvalue())

    def test_litellm_callbacks_keep_autolog_and_record_failures(self):
        fake = types.SimpleNamespace(success_callback=["mlflow"], _async_success_callback=[],
                                     callbacks=["mlflow"], failure_callback=["mlflow"], _async_failure_callback=[])
        with patch("utils.token_tracker.litellm", fake), patch("utils.token_tracker.HAS_LITELLM", True):
            tracker = TokenCostTracker(print_to_console=False)
            tracker.enable_litellm_callbacks()
            response = types.SimpleNamespace(model="test-model", usage=types.SimpleNamespace(prompt_tokens=5, completion_tokens=2))
            start = datetime(2026, 10, 2, tzinfo=timezone.utc)
            end = datetime(2026, 10, 2, 0, 0, 2, tzinfo=timezone.utc)
            tracker._litellm_callback({"model": "test-model"}, response, start, end)
            tracker._litellm_failure_callback({"model": "test-model", "exception": TimeoutError("secret")}, None, start, end)
            tracker.disable_litellm_callbacks()
        self.assertEqual(fake.success_callback, ["mlflow"])
        self.assertEqual(fake.failure_callback, ["mlflow"])
        self.assertEqual(tracker.get_totals()["total_calls"], 1)
        self.assertEqual([e["status"] for e in self.events()], ["success", "failed"])
        self.assertEqual(self.events()[0]["elapsed_seconds"], 2)
        self.assertNotIn("secret", self.output.getvalue())

    def test_repeated_callback_registration_does_not_duplicate_or_leak(self):
        names = ("success_callback", "_async_success_callback", "callbacks",
                 "failure_callback", "_async_failure_callback")
        fake = types.SimpleNamespace(**{name: ["mlflow"] for name in names})
        with patch("utils.token_tracker.litellm", fake), patch("utils.token_tracker.HAS_LITELLM", True):
            tracker = TokenCostTracker(print_to_console=False)
            tracker.enable_litellm_callbacks("first")
            tracker.enable_litellm_callbacks("second")
            self.assertEqual(tracker._active_category, "second")
            self.assertTrue(all(len(getattr(fake, name)) == 2 for name in names))
            response = types.SimpleNamespace(model="test", usage=types.SimpleNamespace(prompt_tokens=5, completion_tokens=2))
            for callback in fake.success_callback:
                if callable(callback):
                    callback({"model": "test"}, response, 0, 1)
            self.assertEqual(tracker.get_totals()["total_calls"], 1)
            self.assertEqual(len(self.events()), 1)
            tracker.disable_litellm_callbacks()
            self.assertTrue(all(getattr(fake, name) == ["mlflow"] for name in names))

    def test_unavailable_usage_metadata_does_not_replace_sdk_result(self):
        class Response:
            @property
            def usage(self):
                raise ValueError("secret response metadata")
        self.assertEqual(timing.response_usage(Response()), {})

    def test_identity_fields_reject_paths_and_keep_safe_model_namespace(self):
        with patch.dict(os.environ, {"AUGUR_RUN_ID": "/private/example", "AUGUR_CLOCK_DOMAIN": "../example"}):
            with timing.timing_span("openai.chat", "sdk", model="/private/example", provider_request_id="../example"):
                pass
        event=self.events()[-1]
        self.assertNotIn("run_id",event)
        self.assertNotIn("model",event)
        self.assertNotIn("provider_request_id",event)
        self.assertNotEqual(event["clock_domain"],"../example")
        with timing.timing_span("openai.chat", "sdk", model="openai/fixture-model", provider_request_id="req_mock"):
            pass
        self.assertEqual(self.events()[-1]["model"],"openai/fixture-model")
        response=types.SimpleNamespace(usage=None,_request_id="/private/example")
        self.assertEqual(timing.response_usage(response),{})
        self.assertNotIn("/private/example",self.output.getvalue())
        for value in ("C:/example/model", "C:example/model", "namespace/../model"):
            with timing.timing_span("openai.chat", "sdk", model=value):
                pass
            self.assertNotIn("model", self.events()[-1])
            self.assertNotIn(value, self.output.getvalue())

    def test_artifact_spans_have_bytes_and_keep_archive_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = types.SimpleNamespace(path=os.path.join(tmp, "artifact.tar.gz"))
            with write_to_output_artifact(artifact) as directory:
                with open(os.path.join(directory, "data.txt"), "w") as f:
                    f.write("secret-source-content")
            size = os.path.getsize(artifact.path)
            with read_from_input_artifact(artifact) as directory:
                with open(os.path.join(directory, "data.txt")) as f:
                    self.assertEqual(f.read(), "secret-source-content")
        self.assertEqual([e["operation"] for e in self.events()], ["artifact.archive", "artifact.extract"])
        self.assertTrue(all(e["bytes"] == size for e in self.events()))
        self.assertNotIn("secret", self.output.getvalue())

    def test_query_retry_keeps_policy_and_records_failed_attempt_and_backoff(self):
        from utils.graphrag_utils import DependencyAnalyzer
        with patch.object(DependencyAnalyzer, "_setup_search"), patch.object(DependencyAnalyzer, "_setup_prompts"):
            analyzer = DependencyAnalyzer(token_tracker=MagicMock())
        analyzer.entity_df = MagicMock(); analyzer.communities_df = MagicMock()
        analyzer.community_reports_df = MagicMock(); analyzer.community_level = 0
        search = AsyncMock(side_effect=[TimeoutError("test timeout"), ("original-result", None)])
        sleep = AsyncMock()
        with patch("utils.graphrag_utils.api.global_search", search), patch("asyncio.sleep", sleep):
            result = asyncio.run(analyzer.query_with_llm("secret-question", retry_count=2, use_global=True))
        self.assertEqual(result, "original-result")
        self.assertEqual(search.await_count, 2)
        sleep.assert_awaited_once_with(5)
        searches = [e for e in self.events() if e["operation"] == "graphrag.global_search"]
        self.assertEqual([e["status"] for e in searches], ["failed", "success"])
        backoff = next(e for e in self.events() if e["operation"] == "graphrag.retry_backoff")
        self.assertEqual(backoff["attempts_remaining"], 1)
        self.assertNotIn("secret-question", self.output.getvalue())

    def test_adhoc_download_setup_and_query_share_invocation_parent(self):
        from pipelines.base.analysis import AnalysisPipeline
        fake_analyzer = MagicMock()
        fake_analyzer.query_with_llm = AsyncMock(return_value="original-answer")
        with patch("utils.graphrag_utils.DependencyAnalyzer", return_value=fake_analyzer), \
             patch("loaders.default_asset_loader.DefaultAssetLoader", return_value=MagicMock()), \
             patch("telemetry.default_custom_telemetry.DefaultCustomTelemetry", return_value=MagicMock()), \
             patch("utils.duration_tracker.DurationTracker", return_value=MagicMock()):
            result = AnalysisPipeline().run_adhoc_query("secret-question")
        self.assertIn("original-answer", result)
        events = self.events()
        parent = next(e for e in events if e["operation"] == "analysis.adhoc_invocation")
        children = [e for e in events if e["operation"] in ("analysis.index_download", "analysis.analyzer_setup")]
        self.assertEqual(len(children), 2)
        self.assertTrue(all(e["parent_span_id"] == parent["span_id"] for e in children))
        self.assertNotIn("secret-question", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
