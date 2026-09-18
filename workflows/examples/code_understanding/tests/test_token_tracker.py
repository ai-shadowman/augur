import unittest
from unittest.mock import MagicMock, patch, AsyncMock
from contextlib import redirect_stdout
import io
import json
import logging
import os
import sys
import tempfile

# Ensure code_understanding package is on sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Provide mock stubs for container dependencies when running in local environments
for pkg_name in [
    "graphrag", "graphrag.api", "graphrag.config", "graphrag.config.load_config",
    "pandas", "yaml", "mlflow", "mlflow.tracking", "mlflow.metrics", "mlflow.metrics.genai",
    "requests", "deepeval",
    "pyvis", "pyvis.network", "networkx", "matplotlib", "matplotlib.pyplot", "litellm"
]:
    if pkg_name not in sys.modules:
        m = MagicMock()
        m.__path__ = []
        sys.modules[pkg_name] = m

import loaders.default_asset_loader
import utils.visualization_utils
import telemetry.default_custom_telemetry
from utils.token_tracker import TokenCostTracker


class TestTokenCostTracker(unittest.TestCase):
    """Unit tests for TokenCostTracker with LiteLLM integration and ASCII table formatting."""

    def setUp(self):
        self.tracker = TokenCostTracker(
            chat_model="openai/gpt-oss-120b",
            embed_model="e5-mistral-7b-instruct",
            chat_prompt_price=0.000002,   # $2.00 / 1M
            chat_output_price=0.000008,   # $8.00 / 1M
            embed_prompt_price=0.0000002, # $0.20 / 1M
        )

    def test_token_counting(self):
        """Test token counting functionality and empty text handling."""
        self.assertEqual(self.tracker.count_tokens(""), 0)
        tokens = self.tracker.count_tokens("Hello world, this is a test prompt.")
        self.assertGreater(tokens, 0)

    def test_cost_calculation(self):
        """Test pricing math for chat and embedding models."""
        # Chat model: 10,000 prompt @ $0.000002 = $0.02, 5,000 output @ $0.000008 = $0.04 -> $0.06
        chat_cost = self.tracker.calculate_cost(
            prompt_tokens=10000,
            output_tokens=5000,
            model="openai/gpt-oss-120b"
        )
        self.assertAlmostEqual(chat_cost, 0.06, places=5)

        # Embedding model: 10,000 prompt @ $0.0000002 = $0.002, 0 output -> $0.002
        embed_cost = self.tracker.calculate_cost(
            prompt_tokens=10000,
            output_tokens=0,
            model="e5-mistral-7b-instruct"
        )
        self.assertAlmostEqual(embed_cost, 0.002, places=6)

    def test_exact_sample_metrics_and_total_cost(self):
        """Verify the exact sample table metrics from the prompt:
        - 15 invocations
        - 20,724 prompt tokens
        - 14,141 output tokens
        - 34,865 total tokens
        - $0.1536 total cost
        """
        # Row 1: e5-mistral-7b-instruct (3 calls, 561 prompt, 0 output)
        self.tracker.track_embedding(prompt_tokens=561, calls=3)

        # Row 2: Local search (3 calls, 10,881 prompt, 2,093 output)
        self.tracker.track_local_search(prompt_tokens=10881, output_tokens=2093, calls=3)

        # Row 3: Global search (5 calls, 461 prompt, 4,170 output)
        self.tracker.track_global_search(prompt_tokens=461, output_tokens=4170, calls=5)

        # Row 4: Chat (4 calls, 8,821 prompt, 7,878 output)
        self.tracker.track_chat(prompt_tokens=8821, output_tokens=7878, calls=4)

        totals = self.tracker.get_totals()

        self.assertEqual(totals["total_calls"], 15)
        self.assertEqual(totals["total_prompt_tokens"], 20724)
        self.assertEqual(totals["total_output_tokens"], 14141)
        self.assertEqual(totals["total_tokens"], 34865)

        # Cost breakdown check:
        # e5-mistral: 561 * 0.0000002 = $0.0001122 -> $0.0001
        # local search: 10881 * 0.000002 + 2093 * 0.000008 = 0.021762 + 0.016744 = $0.038506 -> $0.0385
        # global search: 461 * 0.000002 + 4170 * 0.000008 = 0.000922 + 0.033360 = $0.034282 -> $0.0343
        # chat: 8821 * 0.000002 + 7878 * 0.000008 = 0.017642 + 0.063024 = $0.080666 -> $0.0807
        # Total cost = 0.0001122 + 0.038506 + 0.034282 + 0.080666 = 0.1535662 -> $0.1536
        self.assertAlmostEqual(totals["total_cost"], 0.1535662, places=6)
        self.assertEqual(f"${totals['total_cost']:.4f}", "$0.1536")

    def test_format_summary_structure(self):
        """Verify the generated ASCII summary matches the required layout, dividers, and columns."""
        self.tracker.track_embedding(prompt_tokens=561, calls=3)
        self.tracker.track_local_search(prompt_tokens=10881, output_tokens=2093, calls=3)
        self.tracker.track_global_search(prompt_tokens=461, output_tokens=4170, calls=5)
        self.tracker.track_chat(prompt_tokens=8821, output_tokens=7878, calls=4)

        summary = self.tracker.format_summary()
        lines = summary.split("\n")

        # Title check
        self.assertIn("LLM TOKEN USAGE & COST SUMMARY", lines[0])

        # Border widths check (78 characters)
        self.assertEqual(lines[1], "=" * 78)
        self.assertEqual(lines[7], "-" * 78)
        self.assertEqual(lines[9], "-" * 78)

        # Totals section check
        self.assertIn("Total LLM Invocations : 15", lines[2])
        self.assertIn("Total Prompt Tokens   : 20,724", lines[3])
        self.assertIn("Total Output Tokens   : 14,141", lines[4])
        self.assertIn("Total Tokens Used     : 34,865", lines[5])
        self.assertIn("Estimated Total Cost  : $0.1536", lines[6])

        # Column header check
        self.assertEqual(
            lines[8],
            " Source / Model                   Calls   Prompt     Output     Total      Est. Cost "
        )

        # Row count check (4 rows after header divider)
        self.assertEqual(len(lines), 14)

        # Check name truncation for source names exceeding 32 characters
        long_tracker = TokenCostTracker()
        long_tracker.track("A" * 40, calls=1, prompt_tokens=100, output_tokens=50)
        long_summary = long_tracker.format_summary()
        self.assertIn("A" * 29 + "...", long_summary)

    def test_format_markdown_section(self):
        """Verify markdown section wrapping."""
        self.tracker.track_chat(prompt_tokens=100, output_tokens=50, calls=1)
        md_section = self.tracker.format_markdown_section()

        self.assertTrue(md_section.startswith("\n\n### LLM Token Usage & Cost Summary\n\n```\n"))
        self.assertTrue(md_section.endswith("\n```\n"))
        self.assertIn("Total LLM Invocations : 1", md_section)

    def test_reset(self):
        """Verify tracker reset clears all metrics."""
        self.tracker.track_chat(prompt_tokens=500, output_tokens=200, calls=2)
        self.assertGreater(self.tracker.get_totals()["total_calls"], 0)

        self.tracker.reset()
        totals = self.tracker.get_totals()
        self.assertEqual(totals["total_calls"], 0)
        self.assertEqual(totals["total_prompt_tokens"], 0)
        self.assertEqual(totals["total_output_tokens"], 0)
        self.assertEqual(totals["total_cost"], 0.0)


class TestDependencyAnalyzerIntegration(unittest.TestCase):
    """Test DependencyAnalyzer integration with TokenCostTracker."""

    def test_analyzer_token_tracker_initialization(self):
        """Verify DependencyAnalyzer correctly initializes or receives a token tracker."""
        from utils.graphrag_utils import DependencyAnalyzer
        custom_tracker = TokenCostTracker()
        custom_tracker.track_chat(prompt_tokens=123, output_tokens=456)

        # Mock out setup methods to avoid requiring disk parquet files in unit test
        with patch.object(DependencyAnalyzer, '_setup_configuration'), \
             patch.object(DependencyAnalyzer, '_setup_search'), \
             patch.object(DependencyAnalyzer, '_setup_prompts'):
            analyzer = DependencyAnalyzer(token_tracker=custom_tracker)
            self.assertEqual(analyzer.token_tracker, custom_tracker)
            summary = analyzer.get_token_usage_summary()
            self.assertIn("Total LLM Invocations : 1", summary)
            self.assertIn("123", summary)

    def test_report_placement_above_code_migration_plan_json(self):
        """Verify that the token usage table is placed immediately above the Code Migration Plan (JSON) section."""
        import asyncio
        from utils.graphrag_utils import DependencyAnalyzer

        custom_tracker = TokenCostTracker()
        custom_tracker.track_chat(prompt_tokens=500, output_tokens=250)

        with patch.object(DependencyAnalyzer, '_setup_configuration'), \
             patch.object(DependencyAnalyzer, '_setup_search'), \
             patch.object(DependencyAnalyzer, '_setup_prompts'), \
             patch.object(DependencyAnalyzer, '_extract_indexed_git_urls', return_value={"https://github.com/org/repo"}), \
             patch('utils.visualization_utils.log_interactive_dependency_graph'):

            analyzer = DependencyAnalyzer(token_tracker=custom_tracker)

            mock_loader = MagicMock()
            mock_loader.num_prompts.side_effect = lambda path: 1 if "enhanced" in path else 1
            mock_loader.download_prompt.side_effect = [
                ("prompt 1", {"title": "### Overview", "skip_prompt": None}),
                ("prompt 2", {"title": "### Code Migration Plan (JSON)", "skip_prompt": None}),
            ]

            with patch('loaders.default_asset_loader.DefaultAssetLoader', return_value=mock_loader), \
                 patch.object(analyzer, 'query_with_llm', side_effect=["Overview content", '{"plan": []}']):

                report = asyncio.run(analyzer.generate_migration_report())

                self.assertIn("### LLM Token Usage & Cost Summary", report)
                self.assertIn("### Code Migration Plan (JSON)", report)

                summary_pos = report.index("### LLM Token Usage & Cost Summary")
                plan_pos = report.index("### Code Migration Plan (JSON)")
                self.assertLess(summary_pos, plan_pos)

    def test_prepare_settings_empty_tokens_fallback(self):
        """Verify that prepare_settings replaces empty or missing tokens with 'EMPTY' to prevent Pydantic validation failures."""
        import tempfile
        from utils.graphrag_utils import DependencyAnalyzer

        with tempfile.TemporaryDirectory() as tmp_dir:
            template_path = os.path.join(tmp_dir, "settings.yaml.in")
            with open(template_path, "w") as f:
                f.write("chat_key: ${GRAPHRAG_LLM_TOKEN}\nembed_key: ${EMBED_LLM_TOKEN}\n")

            mock_loader = MagicMock()
            with patch('loaders.default_asset_loader.DefaultAssetLoader', return_value=mock_loader), \
                 patch.dict(os.environ, {"GRAPHRAG_LLM_TOKEN": "", "EMBED_LLM_TOKEN": ""}):

                DependencyAnalyzer.prepare_settings(template_dir=tmp_dir, output_dir=tmp_dir)

                output_path = os.path.join(tmp_dir, "settings.yaml")
                with open(output_path) as f:
                    content = f.read()

                self.assertIn("chat_key: EMPTY", content)
                self.assertIn("embed_key: EMPTY", content)


    def test_token_tracker_serialization_and_merge(self):
        """Verify saving to file, loading from file, and merging tracker states."""
        import tempfile
        tracker1 = TokenCostTracker()
        tracker1.track_chat(prompt_tokens=100, output_tokens=50, calls=1)

        tracker2 = TokenCostTracker()
        tracker2.track_global_search(prompt_tokens=200, output_tokens=80, calls=2)

        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = os.path.join(tmp_dir, "token_tracker.json")
            tracker1.save_to_file(file_path)

            loaded = TokenCostTracker.load_from_file(file_path)
            self.assertEqual(loaded.get_totals()["total_calls"], 1)
            self.assertEqual(loaded.get_totals()["total_prompt_tokens"], 100)

            loaded.merge(tracker2)
            totals = loaded.get_totals()
            self.assertEqual(totals["total_calls"], 3)
            self.assertEqual(totals["total_prompt_tokens"], 300)
            self.assertEqual(totals["total_output_tokens"], 130)

    def test_litellm_callbacks(self):
        """Verify that LiteLLM callback records calls properly."""
        from utils.token_tracker import HAS_LITELLM, litellm
        tracker = TokenCostTracker()
        tracker.enable_litellm_callbacks(category="Data Generation (sdg_hub)")

        if HAS_LITELLM and litellm is not None:
            self.assertIn(tracker._litellm_callback, litellm.success_callback)

            # Simulate a LiteLLM callback invocation
            mock_response = MagicMock()
            mock_response.model = "test-model"
            mock_response.usage.prompt_tokens = 40
            mock_response.usage.completion_tokens = 20
            mock_response._response_cost = 0.001

            tracker._litellm_callback(
                kwargs={"model": "test-model", "response_cost": 0.001},
                completion_response=mock_response,
                start_time=0,
                end_time=1,
            )

            self.assertIn("Data Generation (sdg_hub) (test-model)", tracker.records)
            self.assertEqual(tracker.records["Data Generation (sdg_hub) (test-model)"]["calls"], 1)
            self.assertEqual(tracker.records["Data Generation (sdg_hub) (test-model)"]["prompt_tokens"], 40)

            tracker.disable_litellm_callbacks()
            self.assertNotIn(tracker._litellm_callback, litellm.success_callback)

    def test_multi_repo_search_mode_resolution(self):
        """Verify that multi_repo respects explicit search_mode metadata."""
        import asyncio
        from utils.graphrag_utils import DependencyAnalyzer

        with patch.object(DependencyAnalyzer, '_setup_configuration'), \
             patch.object(DependencyAnalyzer, '_setup_search'), \
             patch.object(DependencyAnalyzer, '_setup_prompts'), \
             patch.object(DependencyAnalyzer, '_extract_indexed_git_urls', return_value=set()), \
             patch('utils.visualization_utils.log_interactive_dependency_graph'):

            analyzer = DependencyAnalyzer(multi_repo=True)

            mock_loader = MagicMock()
            mock_loader.num_prompts.side_effect = lambda path: 0 if "enhanced" in path else 2
            mock_loader.download_prompt.side_effect = [
                ("prompt 0", {"title": "### Dependency Graph", "search_mode": "local", "skip_prompt": None}),
                ("prompt 1", {"title": "### High-Level Summary", "skip_prompt": None}),
            ]

            calls_recorded = []

            async def fake_query(prompt, bypass_index=False, use_global=True):
                calls_recorded.append({"prompt": prompt, "use_global": use_global})
                return "Query Result"

            analyzer.query_with_llm = fake_query

            with patch('loaders.default_asset_loader.DefaultAssetLoader', return_value=mock_loader):
                asyncio.run(analyzer.generate_migration_report())

            # Prompt 0 declared search_mode: local -> use_global must be False even with multi_repo=True
            self.assertFalse(calls_recorded[0]["use_global"])
            # Prompt 1 had no search_mode -> use_global must be True for multi_repo
            self.assertTrue(calls_recorded[1]["use_global"])

    def test_analysis_pipeline_run_multi_repo_returns_report(self):
        """Verify that AnalysisPipeline.run_multi_repo returns the report."""
        from pipelines.base.analysis import AnalysisPipeline
        pipeline = AnalysisPipeline()

        with patch('loaders.default_asset_loader.DefaultAssetLoader'), \
             patch('utils.loader_utils.download_result_directory'), \
             patch.object(pipeline, 'run', return_value="# Migration Report Content") as mock_run:
            result = pipeline.run_multi_repo()
            self.assertEqual(result, "# Migration Report Content")
            mock_run.assert_called_once()

    def test_multi_repo_token_table_placed_at_end(self):
        """Verify that in multi-repo mode, the LLM token summary table is placed at the very end of the report."""
        import asyncio
        from utils.graphrag_utils import DependencyAnalyzer

        with patch.object(DependencyAnalyzer, '_setup_configuration'), \
             patch.object(DependencyAnalyzer, '_setup_search'), \
             patch.object(DependencyAnalyzer, '_setup_prompts'), \
             patch.object(DependencyAnalyzer, '_extract_indexed_git_urls', return_value={"https://github.com/org/repo1"}), \
             patch('utils.visualization_utils.log_interactive_dependency_graph'):

            analyzer = DependencyAnalyzer(multi_repo=True)

            mock_loader = MagicMock()
            mock_loader.num_prompts.side_effect = lambda path: 0 if "enhanced" in path else 3
            mock_loader.download_prompt.side_effect = [
                ("prompt 0", {"title": "### Dependency Graph", "skip_prompt": None}),
                ("prompt 1", {"title": "### High-Level Summary", "skip_prompt": None}),
                ("prompt 2", {"title": "### Recommended Migration Order", "skip_prompt": None}),
            ]

            async def fake_query(prompt, bypass_index=False, use_global=True):
                return "Section body content"

            analyzer.query_with_llm = fake_query

            with patch('loaders.default_asset_loader.DefaultAssetLoader', return_value=mock_loader):
                report = asyncio.run(analyzer.generate_migration_report())

            self.assertIn("### LLM Token Usage & Cost Summary", report)
            rec_pos = report.index("### Recommended Migration Order")
            summary_pos = report.index("### LLM Token Usage & Cost Summary")
            self.assertTrue(
                report.rstrip().endswith("</details>")
                or report.rstrip().endswith("```")
                or report.rstrip().endswith("|")
            )

    def test_singleton_get_instance_and_reset(self):
        """Verify TokenCostTracker.get_instance() and reset_instance() behavior."""
        TokenCostTracker.reset_instance()
        inst1 = TokenCostTracker.get_instance()
        inst2 = TokenCostTracker.get_instance()
        self.assertIs(inst1, inst2)

        inst1.track_chat(prompt_tokens=10, output_tokens=5)
        self.assertEqual(inst2.get_totals()["total_calls"], 1)

        inst3 = TokenCostTracker.reset_instance()
        self.assertIsNot(inst1, inst3)
        self.assertEqual(inst3.get_totals()["total_calls"], 0)

    def test_enable_telemetry_decorator(self):
        """Verify @enable_telemetry decorator invokes DefaultCustomTelemetry.track()."""
        from utils.otel_utils import enable_telemetry

        called = []

        @enable_telemetry
        def sample_func(x):
            called.append(x)
            return x * 2

        with patch('telemetry.default_custom_telemetry.DefaultCustomTelemetry.track') as mock_track:
            result = sample_func(5)
            self.assertEqual(result, 10)
            self.assertEqual(called, [5])
            mock_track.assert_called_once()

    def test_mlflow_custom_telemetry_idempotency(self):
        """Verify MlFlowCustomTelemetry.track() is idempotent and only configures setup once."""
        from telemetry.mlflow_custom_telemetry import MlFlowCustomTelemetry
        import mlflow

        MlFlowCustomTelemetry.reset()
        mlflow.set_experiment.reset_mock()
        mlflow.openai.autolog.reset_mock()
        try:
            with patch.dict(os.environ, {"MLFLOW_TRACKING_URI": "http://mock-mlflow:5000", "MLFLOW_EXPERIMENT_NAME": "test-exp"}):
                telem = MlFlowCustomTelemetry()
                telem.track()
                telem.track()

                self.assertEqual(mlflow.set_experiment.call_count, 1)
                self.assertEqual(mlflow.openai.autolog.call_count, 1)
        finally:
            MlFlowCustomTelemetry.reset()

    def test_indexing_pipeline_run_has_enable_telemetry(self):
        """Verify IndexingPipeline.run invokes DefaultCustomTelemetry.track via @enable_telemetry."""
        from pipelines.base.indexing import IndexingPipeline

        with patch('telemetry.default_custom_telemetry.DefaultCustomTelemetry.track') as mock_track, \
             patch('pipelines.base.indexing.generate_graphrag_index'), \
             patch('pipelines.base.indexing.evaluate_graphrag_index'):
            pipeline = IndexingPipeline()
            pipeline.run(codebase_path="/tmp/code", graphrag_source_path="/tmp/gr", git_repo="repo", git_branch="main")
            mock_track.assert_called()

    def test_dependency_analyzer_uses_singleton_by_default(self):
        """Verify DependencyAnalyzer defaults to TokenCostTracker.get_instance()."""
        from utils.graphrag_utils import DependencyAnalyzer

        singleton = TokenCostTracker.reset_instance()

        with patch.object(DependencyAnalyzer, '_setup_configuration'), \
             patch.object(DependencyAnalyzer, '_setup_search'), \
             patch.object(DependencyAnalyzer, '_setup_prompts'):
            analyzer = DependencyAnalyzer()
            self.assertIs(analyzer.token_tracker, singleton)

    def test_log_to_mlflow_ends_active_run(self):
        """Verify log_to_mlflow logs metrics and ends active run to prevent conflict with asset loader."""
        import sys
        mlflow_mock = sys.modules["mlflow"]
        active_run_mock = MagicMock()
        mlflow_mock.active_run.return_value = active_run_mock

        tracker = TokenCostTracker()
        tracker.track_chat(prompt_tokens=100, output_tokens=50)
        tracker.log_to_mlflow()

        mlflow_mock.log_metrics.assert_called()
        mlflow_mock.end_run.assert_called()


    def test_token_tracker_upload_to_mlflow(self):
        """Verify upload_to_mlflow logs metrics and uploads tokens.json via AssetLoader and mlflow."""
        import sys
        mlflow_mock = sys.modules["mlflow"]
        active_mock = MagicMock()
        active_mock.info.run_id = "test-run-456"
        mlflow_mock.active_run.return_value = active_mock

        tracker = TokenCostTracker()
        tracker.track_chat(prompt_tokens=200, output_tokens=100)

        mock_loader = MagicMock()
        with patch('loaders.default_asset_loader.DefaultAssetLoader', return_value=mock_loader):
            tracker.upload_to_mlflow(git_slug="org-repo-main", stage="Data Generation")

            mlflow_mock.log_artifact.assert_called()
            call_args = mlflow_mock.log_artifact.call_args
            self.assertEqual(call_args[1]["artifact_path"], "telemetry")

            mock_loader.log_results.assert_called_once()
            _, kwargs = mock_loader.log_results.call_args
            self.assertEqual(kwargs["tags"]["git_slug"], "org-repo-main")
            self.assertEqual(kwargs["tags"]["category"], "telemetry")
            self.assertEqual(kwargs["tags"]["type"], "tokens")

    def test_token_tracker_download_from_mlflow_by_run_id(self):
        """Verify download_from_mlflow downloads telemetry/tokens.json via mlflow artifacts."""
        import sys
        import tempfile
        mlflow_mock = sys.modules["mlflow"]

        with tempfile.TemporaryDirectory() as tmp_dir:
            tokens_file = os.path.join(tmp_dir, "tokens.json")
            sample_tracker = TokenCostTracker()
            sample_tracker.track_chat(prompt_tokens=500, output_tokens=200)
            sample_tracker.save_to_file(tokens_file)

            mlflow_mock.artifacts.download_artifacts.return_value = tokens_file

            tracker = TokenCostTracker()
            success = tracker.download_from_mlflow(run_id="run-token-123")
            self.assertTrue(success)
            self.assertEqual(tracker.get_totals()["total_calls"], 1)
            self.assertEqual(tracker.get_totals()["total_tokens"], 700)

    def test_token_tracker_download_from_mlflow_by_git_slug(self):
        """Verify download_from_mlflow downloads via DefaultAssetLoader when git_slug is provided."""
        sample_tracker = TokenCostTracker()
        sample_tracker.track_embedding(prompt_tokens=1000)
        sample_dict = sample_tracker.to_dict()

        mock_loader = MagicMock()
        mock_loader.download.return_value = sample_dict

        tracker = TokenCostTracker()
        with patch('loaders.default_asset_loader.DefaultAssetLoader', return_value=mock_loader):
            success = tracker.download_from_mlflow(git_slug="org-repo-main", only_current_run=False)
            self.assertTrue(success)
            self.assertEqual(tracker.get_totals()["total_calls"], 1)
            self.assertEqual(tracker.get_totals()["total_prompt_tokens"], 1000)

    def test_token_tracker_log_to_mlflow_nested_and_matching_run(self):
        """Verify log_to_mlflow with matching run_id or nested run_id."""
        import sys
        mlflow_mock = sys.modules["mlflow"]

        # Case 1: active run matches run_id
        active_mock = MagicMock()
        active_mock.info.run_id = "run-same"
        mlflow_mock.active_run.return_value = active_mock
        mlflow_mock.start_run.reset_mock()

        tracker = TokenCostTracker()
        tracker.track_chat(prompt_tokens=10, output_tokens=10)
        tracker.log_to_mlflow(run_id="run-same")
        mlflow_mock.log_metrics.assert_called()
        mlflow_mock.start_run.assert_not_called()

        # Case 2: active run differs from run_id -> nested run
        active_mock.info.run_id = "run-parent"
        mlflow_mock.active_run.return_value = active_mock
        mlflow_mock.start_run.reset_mock()

        tracker.log_to_mlflow(run_id="run-child")
        mlflow_mock.start_run.assert_called_with(run_id="run-child", nested=True)

    def test_generate_migration_report_aggregates_downloaded_telemetry(self):
        """Verify generate_migration_report downloads prior telemetry from MLflow and renders both tables."""
        from utils.graphrag_utils import DependencyAnalyzer
        from utils.duration_tracker import DurationTracker
        import asyncio

        dur_singleton = DurationTracker.reset_instance()
        token_singleton = TokenCostTracker.reset_instance()

        mock_loader = MagicMock()
        mock_loader.num_prompts.side_effect = lambda prefix: 1 if "enhanced" in prefix else 1
        mock_loader.download_prompt.side_effect = [
            ("Prompt overview", {"search_mode": "global"}),
            ("Prompt plan", {"search_mode": "global"}),
        ]

        # Simulate downloading prior Data Generation step from MLflow
        def fake_dur_download(*args, **kwargs):
            dur_singleton.record_step("Data Generation", "Clone Repository", 8.5)
            return True

        def fake_token_download(*args, **kwargs):
            token_singleton.track_chat(prompt_tokens=400, output_tokens=100)
            return True

        with patch.object(DependencyAnalyzer, '_setup_configuration'), \
             patch.object(DependencyAnalyzer, '_setup_search'), \
             patch.object(DependencyAnalyzer, '_setup_prompts'), \
             patch.object(DependencyAnalyzer, '_extract_indexed_git_urls', return_value={"https://github.com/org/repo"}), \
             patch('utils.visualization_utils.log_interactive_dependency_graph'), \
             patch('loaders.default_asset_loader.DefaultAssetLoader', return_value=mock_loader), \
             patch.object(DurationTracker, 'download_from_mlflow', side_effect=fake_dur_download), \
             patch.object(TokenCostTracker, 'download_from_mlflow', side_effect=fake_token_download), \
             patch('sys.modules'):

            analyzer = DependencyAnalyzer(root_dir="/dummy/dir", git_slug="test-slug")

            with patch.object(analyzer, 'query_with_llm', side_effect=["Overview text", "### Code Migration Plan (JSON)\n[]"]):
                report = asyncio.run(analyzer.generate_migration_report())

                self.assertIn("### LLM Token Usage & Cost Summary", report)
                self.assertIn("### Pipeline Execution Duration Summary", report)
                self.assertIn("Clone Repository", report)
                self.assertIn("Data Generation", report)

    def test_openai_tracking_interception(self):
        """Verify that direct OpenAI completions and embeddings calls are intercepted and recorded."""
        import types
        import asyncio
        tracker = TokenCostTracker()

        openai_mod = types.ModuleType("openai")
        chat_pkg = types.ModuleType("openai.resources.chat")
        chat_mod = types.ModuleType("openai.resources.chat.completions")
        embed_pkg = types.ModuleType("openai.resources")
        embed_mod = types.ModuleType("openai.resources.embeddings")

        class MockUsage:
            def __init__(self, prompt_tokens, completion_tokens):
                self.prompt_tokens = prompt_tokens
                self.completion_tokens = completion_tokens

        class MockResponse:
            def __init__(self, model, prompt_tokens, completion_tokens):
                self.model = model
                self.usage = MockUsage(prompt_tokens, completion_tokens)

        orig_async_create = AsyncMock(return_value=MockResponse("gpt-4o", 50, 25))
        orig_sync_create = MagicMock(return_value=MockResponse("gpt-4o", 30, 15))
        orig_async_embed = AsyncMock(return_value=MockResponse("text-embedding-3-small", 40, 0))
        orig_sync_embed = MagicMock(return_value=MockResponse("text-embedding-3-small", 20, 0))

        class MockAsyncCompletions:
            create = orig_async_create

        class MockCompletions:
            create = orig_sync_create

        class MockAsyncEmbeddings:
            create = orig_async_embed

        class MockEmbeddings:
            create = orig_sync_embed

        chat_mod.AsyncCompletions = MockAsyncCompletions
        chat_mod.Completions = MockCompletions
        embed_mod.AsyncEmbeddings = MockAsyncEmbeddings
        embed_mod.Embeddings = MockEmbeddings

        modules_patch = {
            "openai": openai_mod,
            "openai.resources": embed_pkg,
            "openai.resources.chat": chat_pkg,
            "openai.resources.chat.completions": chat_mod,
            "openai.resources.embeddings": embed_mod,
        }

        with patch.dict(sys.modules, modules_patch):
            tracker.enable_openai_tracking(category="GraphRAG Indexing")

            # 1. Test async chat completion
            resp1 = asyncio.run(chat_mod.AsyncCompletions.create(model="gpt-4o"))
            self.assertEqual(resp1.model, "gpt-4o")

            # 2. Test sync chat completion
            resp2 = chat_mod.Completions.create(model="gpt-4o")
            self.assertEqual(resp2.model, "gpt-4o")

            # 3. Test async embedding
            resp3 = asyncio.run(embed_mod.AsyncEmbeddings.create(model="text-embedding-3-small"))
            self.assertEqual(resp3.model, "text-embedding-3-small")

            # 4. Test sync embedding
            resp4 = embed_mod.Embeddings.create(model="text-embedding-3-small")
            self.assertEqual(resp4.model, "text-embedding-3-small")

            # Verify recorded metrics
            chat_key = "GraphRAG Indexing (gpt-4o)"
            embed_key = "GraphRAG Indexing Embeddings (text-embedding-3-small)"
            self.assertIn(chat_key, tracker.records)
            self.assertEqual(tracker.records[chat_key]["calls"], 2)
            self.assertEqual(tracker.records[chat_key]["prompt_tokens"], 80)
            self.assertEqual(tracker.records[chat_key]["output_tokens"], 40)

            self.assertIn(embed_key, tracker.records)
            self.assertEqual(tracker.records[embed_key]["calls"], 2)
            self.assertEqual(tracker.records[embed_key]["prompt_tokens"], 60)
            self.assertEqual(tracker.records[embed_key]["output_tokens"], 0)

            # Test disable restores originals
            tracker.disable_openai_tracking()
            self.assertEqual(chat_mod.AsyncCompletions.create, orig_async_create)
            self.assertEqual(chat_mod.Completions.create, orig_sync_create)
            self.assertEqual(embed_mod.AsyncEmbeddings.create, orig_async_embed)
            self.assertEqual(embed_mod.Embeddings.create, orig_sync_embed)

    def test_mlflow_multi_run_tokens_aggregation(self):
        """Verify that download_from_mlflow searches runs across stages and merges records from all runs."""
        tracker = TokenCostTracker()

        # Create two fake runs
        run1 = MagicMock()
        run1.info.run_id = "run-data-gen"
        run2 = MagicMock()
        run2.info.run_id = "run-indexing"

        mock_experiment = MagicMock()
        mock_experiment.experiment_id = "exp-123"

        mock_client = MagicMock()
        mock_client.search_runs.return_value = [run1, run2]

        # Prepare dummy json files for the runs
        data_gen_tracker = TokenCostTracker()
        data_gen_tracker.track("Data Generation (python)", calls=10, prompt_tokens=5000, output_tokens=2000, model="gpt-4o")

        indexing_tracker = TokenCostTracker()
        indexing_tracker.track("GraphRAG Indexing (gpt-4o)", calls=50, prompt_tokens=30000, output_tokens=8000, model="gpt-4o")

        temp_dir = tempfile.mkdtemp()
        try:
            file1 = os.path.join(temp_dir, "tokens1.json")
            file2 = os.path.join(temp_dir, "tokens2.json")
            data_gen_tracker.save_to_file(file1)
            indexing_tracker.save_to_file(file2)

            def mock_download(run_id, artifact_path):
                if run_id == "run-data-gen":
                    return file1
                elif run_id == "run-indexing":
                    return file2
                return None

            mlflow_mock = sys.modules["mlflow"]
            with patch('mlflow.tracking.MlflowClient', return_value=mock_client), \
                 patch('loaders.mlflow_asset_loader.MlFlowAssetLoader.get_or_create_experiment_by_name', return_value=mock_experiment), \
                 patch.object(mlflow_mock.artifacts, 'download_artifacts', side_effect=mock_download):

                success = tracker.download_from_mlflow(git_slug="my-repo", only_current_run=False)
                self.assertTrue(success)

                # Both stages should be present in records
                self.assertIn("Data Generation (python)", tracker.records)
                self.assertIn("GraphRAG Indexing (gpt-4o)", tracker.records)
                self.assertEqual(tracker.records["Data Generation (python)"]["prompt_tokens"], 5000)
                self.assertEqual(tracker.records["GraphRAG Indexing (gpt-4o)"]["prompt_tokens"], 30000)
                self.assertEqual(tracker.get_totals()["total_calls"], 60)
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_track_outputs_to_console(self):
        """Verify that track() outputs live token usage and cost metrics to console (stdout)."""
        import io
        from contextlib import redirect_stdout

        tracker = TokenCostTracker()
        f = io.StringIO()
        with redirect_stdout(f):
            tracker.track(
                source="GraphRAG Chat (gpt-4o)",
                calls=1,
                prompt_tokens=1500,
                output_tokens=500,
                cost=0.0125,
                model="gpt-4o",
            )

        output = f.getvalue()
        self.assertIn("[LLM Call]", output)
        self.assertIn("Source: GraphRAG Chat (gpt-4o)", output)
        self.assertIn("Model: gpt-4o", output)
        self.assertIn("Calls: 1", output)
        self.assertIn("Prompt Tokens: 1,500", output)
        self.assertIn("Output Tokens: 500", output)
        self.assertIn("Total Tokens: 2,000", output)
        self.assertIn("Est. Cost: $0.0125", output)

    def test_track_embedding_source_naming(self):
        """Verify track_embedding standardizes source naming to GraphRAG Embeddings ({model})."""
        tracker = TokenCostTracker(embed_model="e5-mistral-7b-instruct")
        tracker.track_embedding(prompt_tokens=300, calls=1)
        self.assertIn("GraphRAG Embeddings (e5-mistral-7b-instruct)", tracker.records)

    def test_download_from_mlflow_skips_current_stage_and_deduplicates(self):
        """Verify download_from_mlflow skips runs matching current_stage and only merges the latest run per stage."""
        tracker = TokenCostTracker()
        tracker.track("Analysis Chat", calls=1, prompt_tokens=100, output_tokens=50)

        import sys
        mlflow_mock = sys.modules["mlflow"]

        run1 = MagicMock()
        run1.info.run_id = "run-analysis-old"
        run1.data.tags = {"stage": "Analysis", "category": "telemetry", "type": "tokens"}

        run2 = MagicMock()
        run2.info.run_id = "run-datagen-new"
        run2.data.tags = {"stage": "Data Generation", "category": "telemetry", "type": "tokens"}

        run3 = MagicMock()
        run3.info.run_id = "run-datagen-old"
        run3.data.tags = {"stage": "Data Generation", "category": "telemetry", "type": "tokens"}

        mock_client = MagicMock()
        mock_client.search_runs.return_value = [run1, run2, run3]

        temp_dir = tempfile.mkdtemp()
        try:
            datagen_file = os.path.join(temp_dir, "tokens.json")
            dt = TokenCostTracker()
            dt.track("Data Generation Code", calls=2, prompt_tokens=2000, output_tokens=0)
            dt.save_to_file(datagen_file)

            mlflow_mock.artifacts.download_artifacts.return_value = datagen_file

            with patch("mlflow.tracking.MlflowClient", return_value=mock_client), \
                 patch("loaders.mlflow_asset_loader.MlFlowAssetLoader.get_or_create_experiment_by_name"):
                tracker.download_from_mlflow(git_slug="repo", current_stage="Analysis", only_current_run=False)

            # Verify Data Generation was merged once, and run1 (Analysis) was skipped
            self.assertIn("Data Generation Code", tracker.records)
            self.assertEqual(tracker.records["Data Generation Code"]["calls"], 2)

            # Test that calling download_from_mlflow a second time uses _merged_runs cache and does not double-count
            tracker.download_from_mlflow(git_slug="repo", current_stage="Analysis", only_current_run=False)
            self.assertEqual(tracker.records["Data Generation Code"]["calls"], 2)
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_git_slug_persistence(self):
        """Verify git_slug and git_repo are preserved in to_dict() and from_dict()."""
        tracker = TokenCostTracker(git_slug="org-repo-main", git_repo="https://github.com/org/repo")
        tracker.track("Embedding", calls=1, prompt_tokens=100, output_tokens=0)
        d = tracker.to_dict()
        self.assertEqual(d["git_slug"], "org-repo-main")
        self.assertEqual(d["git_repo"], "https://github.com/org/repo")

        restored = TokenCostTracker.from_dict(d)
        self.assertEqual(restored.git_slug, "org-repo-main")
        self.assertEqual(restored.git_repo, "https://github.com/org/repo")

    def test_merge_filters_analysis_records_when_current_stage_is_analysis(self):
        """Verify that merge() omits GraphRAG Local Search and GraphRAG Chat when current_stage='Analysis'."""
        current_tracker = TokenCostTracker()
        current_tracker.track_chat(prompt_tokens=500, output_tokens=200)  # GraphRAG Chat in active analysis

        # Past run artifact containing both upstream and old analysis calls
        past_run_tracker = TokenCostTracker()
        past_run_tracker.track("GraphRAG Indexing Embeddings (e5-mistral)", calls=10, prompt_tokens=1000, output_tokens=0)
        past_run_tracker.track_chat(prompt_tokens=5000, output_tokens=2000)  # Old analysis chat
        past_run_tracker.track_local_search(prompt_tokens=8000, output_tokens=3000)  # Old analysis local search

        current_tracker.merge(past_run_tracker, current_stage="Analysis")

        # Current tracker should contain its OWN chat (1 call, 500 prompt tokens), NOT the past run's chat or local search
        self.assertEqual(current_tracker.records["GraphRAG Chat (openai/gpt-oss-120b)"]["calls"], 1)
        self.assertEqual(current_tracker.records["GraphRAG Chat (openai/gpt-oss-120b)"]["prompt_tokens"], 500)
        self.assertNotIn("GraphRAG Local Search (openai/gpt-oss-120b)", current_tracker.records)

        # But it SHOULD contain the upstream indexing tokens
        self.assertIn("GraphRAG Indexing Embeddings (e5-mistral)", current_tracker.records)
        self.assertEqual(current_tracker.records["GraphRAG Indexing Embeddings (e5-mistral)"]["calls"], 10)

    def test_merge_deduplicates_upstream_sources(self):
        """Verify that merging upstream sources multiple times does not compound token counts."""
        current_tracker = TokenCostTracker()
        current_tracker.track_chat(prompt_tokens=100, output_tokens=50)

        upstream_tracker = TokenCostTracker()
        upstream_tracker.track("Code Understanding (e5-mistral-7b-instruct)", calls=5, prompt_tokens=2500, output_tokens=0)

        # Merge first time
        current_tracker.merge(upstream_tracker, current_stage="Analysis")
        self.assertEqual(current_tracker.records["Code Understanding (e5-mistral-7b-instruct)"]["calls"], 5)

        # Merge second time (e.g. from local file and then from MLflow)
        current_tracker.merge(upstream_tracker, current_stage="Analysis")
        self.assertEqual(current_tracker.records["Code Understanding (e5-mistral-7b-instruct)"]["calls"], 5)
        self.assertEqual(current_tracker.records["Code Understanding (e5-mistral-7b-instruct)"]["prompt_tokens"], 2500)

    def test_load_and_merge_file_deduplication(self):
        """Verify load_and_merge does not load the same file multiple times."""
        temp_dir = tempfile.mkdtemp()
        try:
            tokens_file = os.path.join(temp_dir, "tokens.json")
            src_tracker = TokenCostTracker()
            src_tracker.track("Embedding", calls=2, prompt_tokens=200, output_tokens=0)
            src_tracker.save_to_file(tokens_file)

            tracker = TokenCostTracker()
            tracker.load_and_merge(tokens_file, current_stage="Analysis")
            self.assertEqual(tracker.records["Embedding"]["calls"], 2)

            tracker.load_and_merge(tokens_file, current_stage="Analysis")
            self.assertEqual(tracker.records["Embedding"]["calls"], 2)
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)


    def test_download_from_mlflow_only_current_run_blocks_cross_run_search(self):
        """Verify download_from_mlflow with only_current_run=True (default) and no run_id blocks searching other runs."""
        tracker = TokenCostTracker()
        with patch('mlflow.tracking.MlflowClient') as mock_client, \
             patch('loaders.default_asset_loader.DefaultAssetLoader') as mock_loader:
            success = tracker.download_from_mlflow(git_slug="some-repo")
            self.assertFalse(success)
            mock_client.assert_not_called()
            mock_loader.assert_not_called()

    def test_merge_rejects_different_run_id(self):
        """Verify merge rejects records from an upstream tracker with a different run_id."""
        tracker1 = TokenCostTracker(run_id="run-current")
        tracker1.track("Analysis LLM", calls=1, prompt_tokens=100, output_tokens=50)

        tracker2 = TokenCostTracker(run_id="run-old")
        tracker2.track("Data Generation Code", calls=2, prompt_tokens=200, output_tokens=0)

        tracker1.merge(tracker2)
        self.assertNotIn("Data Generation Code", tracker1.records)
        self.assertEqual(tracker1.get_totals()["total_calls"], 1)

    def test_merge_accepts_same_run_id(self):
        """Verify merge accepts records from an upstream tracker with matching run_id."""
        tracker1 = TokenCostTracker(run_id="run-same")
        tracker2 = TokenCostTracker(run_id="run-same")
        tracker2.track("Upstream Datagen", calls=2, prompt_tokens=200, output_tokens=0)

        tracker1.merge(tracker2)
        self.assertIn("Upstream Datagen", tracker1.records)
        self.assertEqual(tracker1.get_totals()["total_calls"], 2)

    def test_reset_instance_clears_records_and_sources(self):
        """Verify reset_instance completely clears in-memory records and merge caches."""
        TokenCostTracker.reset_instance()
        tracker = TokenCostTracker.get_instance()
        tracker.track("Test Source", calls=5, prompt_tokens=500, output_tokens=500)
        self.assertEqual(tracker.get_totals()["total_calls"], 5)

        new_tracker = TokenCostTracker.reset_instance()
        self.assertEqual(len(new_tracker.records), 0)
        self.assertEqual(new_tracker.get_totals()["total_calls"], 0)
        self.assertEqual(len(new_tracker._merged_runs), 0)
        self.assertEqual(len(new_tracker._merged_upstream_sources), 0)
        self.assertEqual(len(new_tracker._merged_files), 0)

    def test_console_output_no_double_logging(self):
        """Verify that real-time [LLM Call] uses logging.debug (not logging.info), preventing double-printing."""
        tracker = TokenCostTracker(print_to_console=True)
        stdout_buf = io.StringIO()

        with self.assertLogs(level="DEBUG") as log_cm:
            with redirect_stdout(stdout_buf):
                tracker.track(
                    source="Test Logging",
                    calls=1,
                    prompt_tokens=100,
                    output_tokens=50,
                    model="test-model",
                )

        stdout_text = stdout_buf.getvalue()
        self.assertIn("[LLM Call]", stdout_text)

        # Confirm the log record is emitted at DEBUG level, NOT INFO
        debug_logs = [record for record in log_cm.records if record.levelno == logging.DEBUG and "[LLM Call]" in record.getMessage()]
        info_logs = [record for record in log_cm.records if record.levelno >= logging.INFO and "[LLM Call]" in record.getMessage()]
        self.assertEqual(len(debug_logs), 1)
        self.assertEqual(len(info_logs), 0)

    def test_print_to_console_suppression_and_env(self):
        """Verify print_to_console parameter, env var suppression, and to_dict/from_dict serialization."""
        # 1. Test parameter suppression
        tracker_quiet = TokenCostTracker(print_to_console=False)
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            tracker_quiet.track(source="Quiet", calls=1, prompt_tokens=10, output_tokens=10)
        self.assertEqual(stdout_buf.getvalue(), "")

        # 2. Test to_dict / from_dict persistence
        d = tracker_quiet.to_dict()
        self.assertIn("print_to_console", d)
        self.assertFalse(d["print_to_console"])
        reconstructed = TokenCostTracker.from_dict(d)
        self.assertFalse(reconstructed.print_to_console)

        # 3. Test env var suppression
        with patch.dict(os.environ, {"TOKEN_TRACKER_PRINT_CONSOLE": "false"}):
            tracker_env = TokenCostTracker()
            self.assertFalse(tracker_env.print_to_console)
            stdout_env = io.StringIO()
            with redirect_stdout(stdout_env):
                tracker_env.track(source="EnvQuiet", calls=1, prompt_tokens=10, output_tokens=10)
            self.assertEqual(stdout_env.getvalue(), "")

    def test_evaluator_initializes_token_tracking(self):
        """Verify MlFlowCustomEvaluator initializes token callbacks and openai tracking."""
        from eval.mlflow_custom_evaluator import MlFlowCustomEvaluator
        with patch.object(TokenCostTracker, "enable_litellm_callbacks") as mock_litellm, \
             patch.object(TokenCostTracker, "enable_openai_tracking") as mock_openai:
            evaluator = MlFlowCustomEvaluator()
            self.assertIsNotNone(evaluator)
            mock_litellm.assert_called_once_with(category="Evaluation (Ground Truth)")
            mock_openai.assert_called_once_with(category="Evaluation (Judge)")

    def test_extract_graphrag_indexing_tokens_from_stats_workflows(self):
        """Verify extract_graphrag_indexing_tokens extracts tokens from stats.json workflows dict."""
        from utils.token_tracker import extract_graphrag_indexing_tokens
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = os.path.join(tmp_dir, "output")
            os.makedirs(output_dir, exist_ok=True)
            stats_path = os.path.join(output_dir, "stats.json")
            stats_content = {
                "workflows": {
                    "create_base_extracted_entities": {
                        "llm_calls": 25,
                        "prompt_tokens": 12500,
                        "completion_tokens": 3000,
                    },
                    "create_final_community_reports": {
                        "llm_calls": 10,
                        "prompt_tokens": 8000,
                        "completion_tokens": 2000,
                    },
                    "create_final_text_units_embeddings": {
                        "llm_calls": 5,
                        "prompt_tokens": 4000,
                        "completion_tokens": 0,
                    },
                }
            }
            with open(stats_path, "w", encoding="utf-8") as f:
                json.dump(stats_content, f)

            tracker = TokenCostTracker(chat_model="gpt-4o", embed_model="text-embedding-3-small")
            result = extract_graphrag_indexing_tokens(tmp_dir, tracker)
            self.assertTrue(result)
            self.assertIn("GraphRAG Indexing (gpt-4o)", tracker.records)
            self.assertIn("GraphRAG Indexing Embeddings (text-embedding-3-small)", tracker.records)

            indexing_rec = tracker.records["GraphRAG Indexing (gpt-4o)"]
            self.assertEqual(indexing_rec["calls"], 35)
            self.assertEqual(indexing_rec["prompt_tokens"], 20500)
            self.assertEqual(indexing_rec["output_tokens"], 5000)

            embed_rec = tracker.records["GraphRAG Indexing Embeddings (text-embedding-3-small)"]
            self.assertEqual(embed_rec["calls"], 5)
            self.assertEqual(embed_rec["prompt_tokens"], 4000)

    def test_extract_graphrag_indexing_tokens_from_stats_flat(self):
        """Verify extract_graphrag_indexing_tokens extracts tokens from flat stats.json."""
        from utils.token_tracker import extract_graphrag_indexing_tokens
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = os.path.join(tmp_dir, "output")
            os.makedirs(output_dir, exist_ok=True)
            stats_path = os.path.join(output_dir, "stats.json")
            stats_content = {
                "llm_calls": 42,
                "prompt_tokens": 15000,
                "completion_tokens": 4200,
            }
            with open(stats_path, "w", encoding="utf-8") as f:
                json.dump(stats_content, f)

            tracker = TokenCostTracker(chat_model="mistral-7b-chat")
            result = extract_graphrag_indexing_tokens(tmp_dir, tracker)
            self.assertTrue(result)
            self.assertIn("GraphRAG Indexing (mistral-7b-chat)", tracker.records)
            self.assertEqual(tracker.records["GraphRAG Indexing (mistral-7b-chat)"]["calls"], 42)
            self.assertEqual(tracker.records["GraphRAG Indexing (mistral-7b-chat)"]["prompt_tokens"], 15000)
            self.assertEqual(tracker.records["GraphRAG Indexing (mistral-7b-chat)"]["output_tokens"], 4200)

    def test_extract_graphrag_indexing_tokens_from_text_units_parquet(self):
        """Verify extract_graphrag_indexing_tokens sums n_tokens from text_units.parquet."""
        from utils.token_tracker import extract_graphrag_indexing_tokens
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = os.path.join(tmp_dir, "output")
            os.makedirs(output_dir, exist_ok=True)
            tu_path = os.path.join(output_dir, "text_units.parquet")
            with open(tu_path, "wb") as f:
                f.write(b"PARQUET_STUB")

            mock_df = MagicMock()
            mock_df.__len__.return_value = 3
            mock_df.columns = ["id", "text", "n_tokens"]
            mock_df.__getitem__.side_effect = lambda col: MagicMock(sum=lambda: 750) if col == "n_tokens" else MagicMock()

            tracker = TokenCostTracker(embed_model="e5-mistral-7b-instruct")
            with patch("pandas.read_parquet", return_value=mock_df):
                result = extract_graphrag_indexing_tokens(tmp_dir, tracker)
                self.assertTrue(result)
                self.assertIn("GraphRAG Indexing Embeddings (e5-mistral-7b-instruct)", tracker.records)
                self.assertEqual(tracker.records["GraphRAG Indexing Embeddings (e5-mistral-7b-instruct)"]["calls"], 3)
                self.assertEqual(tracker.records["GraphRAG Indexing Embeddings (e5-mistral-7b-instruct)"]["prompt_tokens"], 750)

    def test_extract_graphrag_indexing_tokens_artifacts_subfolder(self):
        """Verify extract_graphrag_indexing_tokens finds files in output/artifacts/ subfolder."""
        from utils.token_tracker import extract_graphrag_indexing_tokens
        with tempfile.TemporaryDirectory() as tmp_dir:
            artifacts_dir = os.path.join(tmp_dir, "output", "artifacts")
            os.makedirs(artifacts_dir, exist_ok=True)
            stats_path = os.path.join(artifacts_dir, "stats.json")
            with open(stats_path, "w", encoding="utf-8") as f:
                json.dump({"llm_calls": 12, "prompt_tokens": 3000, "output_tokens": 600}, f)

            tracker = TokenCostTracker()
            result = extract_graphrag_indexing_tokens(tmp_dir, tracker)
            self.assertTrue(result)
            self.assertIn(f"GraphRAG Indexing ({tracker.chat_model})", tracker.records)
            self.assertEqual(tracker.records[f"GraphRAG Indexing ({tracker.chat_model})"]["calls"], 12)

    def test_merge_accepts_different_run_id_when_current_stage_provided(self):
        """Verify merge accepts upstream records across different run_ids when current_stage is set."""
        analysis_tracker = TokenCostTracker(run_id="run-analysis-123")
        analysis_tracker.track("GraphRAG Local Search (gpt-4o)", calls=1, prompt_tokens=500, output_tokens=100)

        indexing_tracker = TokenCostTracker(run_id="run-indexing-456")
        indexing_tracker.track("GraphRAG Indexing (mistral-7b-chat)", calls=20, prompt_tokens=10000, output_tokens=2500)
        indexing_tracker.track("GraphRAG Indexing Embeddings (text-embedding-3-small)", calls=15, prompt_tokens=4500, output_tokens=0)

        # Merge with current_stage="Analysis" should accept indexing records despite run_id difference
        # and should NOT filter out "mistral-7b-chat"
        analysis_tracker.merge(indexing_tracker, current_stage="Analysis")
        self.assertIn("GraphRAG Indexing (mistral-7b-chat)", analysis_tracker.records)
        self.assertIn("GraphRAG Indexing Embeddings (text-embedding-3-small)", analysis_tracker.records)
        self.assertIn("GraphRAG Local Search (gpt-4o)", analysis_tracker.records)
        totals = analysis_tracker.get_totals()
        self.assertEqual(totals["total_calls"], 36)
        self.assertEqual(totals["total_prompt_tokens"], 15000)

    def test_generate_migration_report_includes_indexing_tokens(self):
        """Verify generate_migration_report includes GraphRAG Indexing tokens in report."""
        from utils.graphrag_utils import DependencyAnalyzer
        from utils.duration_tracker import DurationTracker
        import asyncio

        DurationTracker.reset_instance()
        token_singleton = TokenCostTracker.reset_instance()

        mock_loader = MagicMock()
        mock_loader.num_prompts.side_effect = lambda prefix: 1 if "enhanced" in prefix else 1
        mock_loader.download_prompt.side_effect = [
            ("Prompt overview", {"search_mode": "global"}),
            ("Prompt plan", {"search_mode": "global"}),
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            # Create indexing stats in tmp_dir/output/stats.json
            output_dir = os.path.join(tmp_dir, "output")
            os.makedirs(output_dir, exist_ok=True)
            with open(os.path.join(output_dir, "stats.json"), "w", encoding="utf-8") as f:
                json.dump({"llm_calls": 30, "prompt_tokens": 12000, "completion_tokens": 3000}, f)

            with patch.object(DependencyAnalyzer, '_setup_configuration'), \
                 patch.object(DependencyAnalyzer, '_setup_search'), \
                 patch.object(DependencyAnalyzer, '_setup_prompts'), \
                 patch.object(DependencyAnalyzer, '_extract_indexed_git_urls', return_value={"https://github.com/org/repo"}), \
                 patch('utils.visualization_utils.log_interactive_dependency_graph'), \
                 patch('loaders.default_asset_loader.DefaultAssetLoader', return_value=mock_loader), \
                 patch('sys.modules'):

                analyzer = DependencyAnalyzer(root_dir=tmp_dir, git_slug="test-slug")

                with patch.object(analyzer, 'query_with_llm', side_effect=["Overview text", "### Code Migration Plan (JSON)\n[]"]):
                    report = asyncio.run(analyzer.generate_migration_report())

                    self.assertIn("### LLM Token Usage & Cost Summary", report)
                    self.assertIn("GraphRAG Indexing", report)
                    self.assertIn("12,000", report)


if __name__ == "__main__":
    unittest.main()




