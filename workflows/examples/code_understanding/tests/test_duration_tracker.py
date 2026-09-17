import json
import os
import sys
import tempfile
import time

import unittest
from unittest.mock import MagicMock, patch

# Ensure code_understanding package is on sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Provide mock stubs for container dependencies when running in local environments
for pkg_name in [
    "graphrag", "graphrag.api", "graphrag.config", "graphrag.config.load_config",
    "pandas", "yaml", "mlflow", "mlflow.tracking", "requests", "deepeval",
    "pyvis", "pyvis.network", "networkx", "matplotlib", "matplotlib.pyplot", "litellm"
]:
    if pkg_name not in sys.modules:
        m = MagicMock()
        m.__path__ = []
        sys.modules[pkg_name] = m

from utils.duration_tracker import DurationTracker, track_duration


class TestDurationTracker(unittest.TestCase):
    """Unit tests for DurationTracker singleton, context manager, decorator, and formatting."""

    def setUp(self):
        self.tracker = DurationTracker.reset_instance()

    def tearDown(self):
        DurationTracker.reset_instance()

    def test_singleton_get_and_reset(self):
        """Verify get_instance() returns the same singleton and reset_instance() creates a new one."""
        inst1 = DurationTracker.get_instance()
        inst2 = DurationTracker.get_instance()
        self.assertIs(inst1, inst2)

        inst1.record_step("Stage1", "Step1", 1.5)
        self.assertEqual(len(inst2.get_steps()), 1)

        inst3 = DurationTracker.reset_instance()
        self.assertIsNot(inst1, inst3)
        self.assertEqual(len(inst3.get_steps()), 0)

    def test_measure_context_manager_success(self):
        """Verify measure() captures step timing and records success status."""
        with self.tracker.measure(stage="Data Generation", step="Clone Repo"):
            time.sleep(0.01)

        steps = self.tracker.get_steps()
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["stage"], "Data Generation")
        self.assertEqual(steps[0]["step"], "Clone Repo")
        self.assertEqual(steps[0]["status"], "success")
        self.assertGreater(steps[0]["duration"], 0.0)

    def test_measure_context_manager_failure(self):
        """Verify measure() captures status='failed' when an exception occurs and re-raises it."""
        with self.assertRaises(ValueError):
            with self.tracker.measure(stage="Indexing", step="Build GraphRAG"):
                raise ValueError("GraphRAG build failed")

        steps = self.tracker.get_steps()
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["stage"], "Indexing")
        self.assertEqual(steps[0]["step"], "Build GraphRAG")
        self.assertEqual(steps[0]["status"], "failed")
        self.assertGreater(steps[0]["duration"], 0.0)

    def test_track_duration_decorator(self):
        """Verify @track_duration decorator records function execution timing."""
        @track_duration(stage="Analysis", step="Analyze Dependencies")
        def run_analysis(val):
            time.sleep(0.01)
            return val * 2

        result = run_analysis(5)
        self.assertEqual(result, 10)

        steps = self.tracker.get_steps()
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["stage"], "Analysis")
        self.assertEqual(steps[0]["step"], "Analyze Dependencies")
        self.assertEqual(steps[0]["status"], "success")

    def test_format_duration(self):
        """Verify duration formatting across milliseconds, seconds, minutes, and hours."""
        self.assertEqual(DurationTracker.format_duration(0.05), "50ms")
        self.assertEqual(DurationTracker.format_duration(14.234), "14.23s")
        self.assertEqual(DurationTracker.format_duration(125.0), "2m 5.0s")
        self.assertEqual(DurationTracker.format_duration(3665.0), "1h 01m 5s")
        self.assertEqual(DurationTracker.format_duration(-1.0), "0.00s")

    def test_get_stage_durations(self):
        """Verify get_stage_durations aggregates durations correctly per stage."""
        self.tracker.record_step("Stage A", "Step 1", 10.0)
        self.tracker.record_step("Stage A", "Step 2", 5.0)
        self.tracker.record_step("Stage B", "Step 1", 20.0)

        stage_totals = self.tracker.get_stage_durations()
        self.assertAlmostEqual(stage_totals["Stage A"], 15.0)
        self.assertAlmostEqual(stage_totals["Stage B"], 20.0)
        self.assertAlmostEqual(self.tracker.get_total_duration(), 35.0)

    def test_format_summary_ascii(self):
        """Verify ASCII summary table includes headers, rows, and total runtime."""
        self.tracker.record_step("Data Generation", "Clone Repo", 5.2)
        self.tracker.record_step("Indexing", "Build GraphRAG", 120.5)

        summary = self.tracker.format_summary()
        self.assertIn("Pipeline Execution Duration Summary", summary)
        self.assertIn("Data Generation", summary)
        self.assertIn("Clone Repo", summary)
        self.assertIn("Build GraphRAG", summary)
        self.assertIn("Total Runtime", summary)

    def test_format_markdown_section(self):
        """Verify Markdown section contains correct headers and total row."""
        self.tracker.record_step("Data Generation", "Clone Repo", 14.2)
        self.tracker.record_step("Indexing", "Build GraphRAG", 185.0)

        md = self.tracker.format_markdown_section()
        self.assertIn("### Pipeline Execution Duration Summary", md)
        self.assertIn("Pipeline Stage", md)
        self.assertIn("Data Generation", md)
        self.assertIn("Clone Repo", md)
        self.assertIn("Total Runtime", md)
        self.assertTrue(md.rstrip().endswith("```"))

    def test_serialization_and_merge(self):
        """Verify serialization to dict/file and merging with another tracker."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            filepath = os.path.join(tmp_dir, "durations.json")

            self.tracker.record_step("Stage 1", "Step 1", 10.0)
            self.tracker.save_to_file(filepath)

            other = DurationTracker()
            other.load_from_file(filepath)
            self.assertEqual(len(other.get_steps()), 1)
            self.assertEqual(other.get_steps()[0]["step"], "Step 1")

            # Merge
            third = DurationTracker()
            third.record_step("Stage 2", "Step 2", 15.0)
            other.merge(third)
            self.assertEqual(len(other.get_steps()), 2)
            self.assertAlmostEqual(other.get_total_duration(), 25.0)

    def test_log_to_mlflow(self):
        """Verify log_to_mlflow logs step duration metrics and ends active run."""
        import sys
        mlflow_mock = sys.modules["mlflow"]
        active_run_mock = MagicMock()
        mlflow_mock.active_run.return_value = active_run_mock

        self.tracker.record_step("Data Generation", "Clone Repo", 12.5)
        self.tracker.record_step("Indexing", "Build GraphRAG", 45.0)
        self.tracker.log_to_mlflow()

        mlflow_mock.log_metrics.assert_called()
        logged_metrics = mlflow_mock.log_metrics.call_args[0][0]
        self.assertIn("duration_data_generation_clone_repo_sec", logged_metrics)
        self.assertIn("duration_indexing_build_graphrag_sec", logged_metrics)
        self.assertIn("pipeline_total_duration_sec", logged_metrics)
        self.assertEqual(logged_metrics["pipeline_total_duration_sec"], 57.5)
        mlflow_mock.end_run.assert_called()

    def test_migration_report_contains_duration_table_next_to_token_table(self):
        """Verify that DependencyAnalyzer embeds the duration table right next to the token table."""
        import asyncio
        from utils.graphrag_utils import DependencyAnalyzer
        from utils.token_tracker import TokenCostTracker

        custom_tokens = TokenCostTracker.reset_instance()
        custom_tokens.track_chat(prompt_tokens=200, output_tokens=100)

        self.tracker.record_step("Data Generation", "Clone and Parse", 12.3)
        self.tracker.record_step("Indexing", "Build GraphRAG", 145.0)

        with patch.object(DependencyAnalyzer, '_setup_configuration'), \
             patch.object(DependencyAnalyzer, '_setup_search'), \
             patch.object(DependencyAnalyzer, '_setup_prompts'), \
             patch.object(DependencyAnalyzer, '_extract_indexed_git_urls', return_value={"https://github.com/org/repo"}), \
             patch('utils.visualization_utils.log_interactive_dependency_graph'):

            analyzer = DependencyAnalyzer(token_tracker=custom_tokens)

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
                self.assertIn("### Pipeline Execution Duration Summary", report)
                self.assertIn("### Code Migration Plan (JSON)", report)

                token_pos = report.index("### LLM Token Usage & Cost Summary")
                duration_pos = report.index("### Pipeline Execution Duration Summary")
                plan_pos = report.index("### Code Migration Plan (JSON)")

                # Both should be above the JSON plan, and duration table immediately follows token table
                self.assertLess(token_pos, duration_pos)
                self.assertLess(duration_pos, plan_pos)

    def test_multi_repo_migration_report_contains_duration_table(self):
        """Verify that multi-repo report places token and duration tables at the end."""
        import asyncio
        from utils.graphrag_utils import DependencyAnalyzer
        from utils.token_tracker import TokenCostTracker

        custom_tokens = TokenCostTracker.reset_instance()
        custom_tokens.track_chat(prompt_tokens=200, output_tokens=100)

        self.tracker.record_step("Analysis", "Dependency Graph", 34.0)

        with patch.object(DependencyAnalyzer, '_setup_configuration'), \
             patch.object(DependencyAnalyzer, '_setup_search'), \
             patch.object(DependencyAnalyzer, '_setup_prompts'), \
             patch.object(DependencyAnalyzer, '_extract_indexed_git_urls', return_value={"https://github.com/org/repo"}), \
             patch('utils.visualization_utils.log_interactive_dependency_graph'):

            analyzer = DependencyAnalyzer(token_tracker=custom_tokens, multi_repo=True)

            mock_loader = MagicMock()
            mock_loader.num_prompts.return_value = 1
            mock_loader.download_prompt.return_value = ("prompt 1", {"title": "### Overview", "skip_prompt": None})

            with patch('loaders.default_asset_loader.DefaultAssetLoader', return_value=mock_loader), \
                 patch.object(analyzer, 'query_with_llm', return_value="Multi repo overview"):

                report = asyncio.run(analyzer.generate_migration_report())

    def test_load_and_merge_from_file(self):
        """Verify load_and_merge reads from file and avoids duplicating steps."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            file1 = os.path.join(tmp_dir, "durations1.json")
            t1 = DurationTracker()
            t1.record_step("Data Generation", "Prepare Environment", 5.0)
            t1.save_to_file(file1)

            self.tracker.record_step("Data Generation", "Prepare Environment", 5.0)
            self.tracker.record_step("Indexing", "Build GraphRAG", 20.0)
            self.tracker.load_and_merge(file1)

            # "Prepare Environment" should not be duplicated
            self.assertEqual(len(self.tracker.get_steps()), 2)
            self.assertAlmostEqual(self.tracker.get_total_duration(), 25.0)

    def test_active_measurements_included_in_summary(self):
        """Verify that an in-progress measure block is included in summary and markdown output."""
        with self.tracker.measure("Analysis", "Active Long Job"):
            summary = self.tracker.format_summary(include_active=True)
            self.assertIn("Active Long Job", summary)
            self.assertIn("Running", summary)

            md = self.tracker.format_markdown_section(include_active=True)
            self.assertIn("### Pipeline Execution Duration Summary", md)
            self.assertIn("Active Long Job", md)

    def test_generate_migration_report_auto_populates_duration_table_even_if_no_prior_steps(self):
        """Verify that generate_migration_report populates duration summary even with 0 prior steps."""
        import asyncio
        from utils.graphrag_utils import DependencyAnalyzer
        from utils.token_tracker import TokenCostTracker

        # Clear all records to simulate isolated pod start in Kubeflow
        self.tracker.reset()
        self.assertEqual(len(self.tracker.get_steps()), 0)

        custom_tokens = TokenCostTracker.reset_instance()
        custom_tokens.track_chat(prompt_tokens=100, output_tokens=50)

        with patch.object(DependencyAnalyzer, '_setup_configuration'), \
             patch.object(DependencyAnalyzer, '_setup_search'), \
             patch.object(DependencyAnalyzer, '_setup_prompts'), \
             patch.object(DependencyAnalyzer, '_extract_indexed_git_urls', return_value={"https://github.com/org/repo"}), \
             patch('utils.visualization_utils.log_interactive_dependency_graph'):

            analyzer = DependencyAnalyzer(token_tracker=custom_tokens)

            mock_loader = MagicMock()
            mock_loader.num_prompts.side_effect = lambda path: 1 if "enhanced" in path else 1
            mock_loader.download_prompt.side_effect = [
                ("prompt 1", {"title": "### Overview", "skip_prompt": None}),
                ("prompt 2", {"title": "### Code Migration Plan (JSON)", "skip_prompt": None}),
            ]

            with patch('loaders.default_asset_loader.DefaultAssetLoader', return_value=mock_loader), \
                 patch.object(analyzer, 'query_with_llm', side_effect=["Overview content", '{"plan": []}']):

                report = asyncio.run(analyzer.generate_migration_report())

                # Duration table MUST be present even though tracker had 0 prior records
                self.assertIn("### Pipeline Execution Duration Summary", report)
                self.assertIn("Prompt 1: Overview", report)
                self.assertIn("Dependency Graph Visualization", report)
                self.assertIn("Migration Report Total", report)

    def test_stage_breakdown_in_summary(self):
        """Verify format_summary includes Stage Breakdown section with percentages when multiple stages exist."""
        self.tracker.record_step("Data Generation", "Clone Repository", 30.0)
        self.tracker.record_step("Indexing", "Build GraphRAG Index", 60.0)
        self.tracker.record_step("Analysis", "Prompt 1: Overview", 10.0)

        summary = self.tracker.format_summary()
        self.assertIn("Stage Breakdown:", summary)
        self.assertIn("Data Generation: 30.00s (30.0%)", summary)
        self.assertIn("Indexing: 1m 0.0s (60.0%)", summary)
        self.assertIn("Analysis: 10.00s (10.0%)", summary)
        self.assertIn("Total Runtime", summary)

    def test_granular_sub_steps_logging(self):
        """Verify granular sub-steps across all three pipeline stages are tracked and MLflow compatible."""
        # Data Generation
        self.tracker.record_step("Data Generation", "Reset Environment", 0.5)
        self.tracker.record_step("Data Generation", "Clone Repository", 4.2)
        self.tracker.record_step("Data Generation", "Detect Languages", 0.2)
        self.tracker.record_step("Data Generation", "Parse Raw Code (python)", 0.8)
        self.tracker.record_step("Data Generation", "LLM Metadata Extraction (python)", 35.0)
        self.tracker.record_step("Data Generation", "Save Metadata Files (python)", 0.3)

        # Indexing
        self.tracker.record_step("Indexing", "Prepare Settings & Config", 1.1)
        self.tracker.record_step("Indexing", "Copy Codebase Inputs", 0.4)
        self.tracker.record_step("Indexing", "Initialize GraphRAG Project", 1.5)
        self.tracker.record_step("Indexing", "Build GraphRAG Index (Entities & Graph)", 120.0)

        # Analysis
        self.tracker.record_step("Analysis", "Prompt 1: High-Level Overview", 15.0)
        self.tracker.record_step("Analysis", "Prompt 2: Dependency Mapping", 14.5)
        self.tracker.record_step("Analysis", "Dependency Graph Visualization", 3.2)
        self.tracker.record_step("Analysis", "Migration Report Total", 33.0)

        summary = self.tracker.format_summary()
        self.assertIn("Clone Repository", summary)
        self.assertIn("LLM Metadata Extraction (python)", summary)
        self.assertIn("Build GraphRAG Index (Entities & Graph)", summary)
        self.assertIn("Prompt 1: High-Level Overview", summary)
        self.assertIn("Stage Breakdown:", summary)

        # Verify MLflow metric logging handles these step names cleanly
        with patch('mlflow.active_run', return_value=None), \
             patch('mlflow.start_run') as mock_start_run:
            mock_run_ctx = MagicMock()
            mock_start_run.return_value.__enter__.return_value = mock_run_ctx
            with patch('mlflow.log_metrics') as mock_log_metrics:
                self.tracker.log_to_mlflow()
                self.assertTrue(mock_log_metrics.called)
                logged_metrics = mock_log_metrics.call_args[0][0]
                self.assertIn("duration_data_generation_clone_repository_sec", logged_metrics)
                self.assertIn("duration_indexing_initialize_graphrag_project_sec", logged_metrics)
                self.assertIn("pipeline_total_duration_sec", logged_metrics)


    def test_duration_tracker_upload_to_mlflow(self):
        """Verify upload_to_mlflow logs metrics and uploads durations.json via AssetLoader and mlflow."""
        import sys
        mlflow_mock = sys.modules["mlflow"]
        active_mock = MagicMock()
        active_mock.info.run_id = "test-run-123"
        mlflow_mock.active_run.return_value = active_mock

        self.tracker.record_step("Data Generation", "Clone Repository", 5.0)

        mock_loader = MagicMock()
        with patch('loaders.default_asset_loader.DefaultAssetLoader', return_value=mock_loader):
            self.tracker.upload_to_mlflow(git_slug="org-repo-main", stage="Data Generation")

            # Check that log_artifact was called with telemetry path
            mlflow_mock.log_artifact.assert_called()
            call_args = mlflow_mock.log_artifact.call_args
            self.assertEqual(call_args[1]["artifact_path"], "telemetry")

            # Check that DefaultAssetLoader().log_results was called
            mock_loader.log_results.assert_called_once()
            _, kwargs = mock_loader.log_results.call_args
            self.assertEqual(kwargs["tags"]["git_slug"], "org-repo-main")
            self.assertEqual(kwargs["tags"]["category"], "telemetry")
            self.assertEqual(kwargs["tags"]["type"], "durations")

    def test_duration_tracker_download_from_mlflow_by_run_id(self):
        """Verify download_from_mlflow downloads telemetry/durations.json via mlflow artifacts."""
        import sys
        import tempfile
        mlflow_mock = sys.modules["mlflow"]

        with tempfile.TemporaryDirectory() as tmp_dir:
            dur_file = os.path.join(tmp_dir, "durations.json")
            with open(dur_file, "w") as f:
                json.dump({"records": [{"stage": "Data Generation", "step": "Clone", "duration": 4.5, "status": "success"}]}, f)

            mlflow_mock.artifacts.download_artifacts.return_value = dur_file

            tracker = DurationTracker()
            success = tracker.download_from_mlflow(run_id="run-xyz")
            self.assertTrue(success)
            self.assertEqual(len(tracker.records), 1)
            self.assertEqual(tracker.records[0]["step"], "Clone")

    def test_duration_tracker_download_from_mlflow_by_git_slug(self):
        """Verify download_from_mlflow downloads via DefaultAssetLoader when git_slug is provided."""
        mock_loader = MagicMock()
        mock_loader.download.return_value = {
            "records": [{"stage": "Indexing", "step": "GraphRAG", "duration": 15.0, "status": "success"}]
        }

        tracker = DurationTracker()
        with patch('loaders.default_asset_loader.DefaultAssetLoader', return_value=mock_loader):
            success = tracker.download_from_mlflow(git_slug="my-org-repo-main")
            self.assertTrue(success)
            self.assertEqual(len(tracker.records), 1)
            self.assertEqual(tracker.records[0]["step"], "GraphRAG")

    def test_log_to_mlflow_nested_and_matching_run(self):
        """Verify log_to_mlflow behavior with explicit run_id matching or nesting."""
        import sys
        mlflow_mock = sys.modules["mlflow"]

        # Case 1: active run matches run_id -> no start_run call
        active_mock = MagicMock()
        active_mock.info.run_id = "run-same"
        mlflow_mock.active_run.return_value = active_mock
        mlflow_mock.start_run.reset_mock()

        self.tracker.record_step("Analysis", "Report", 2.0)
        self.tracker.log_to_mlflow(run_id="run-same")
        mlflow_mock.log_metrics.assert_called()
        mlflow_mock.start_run.assert_not_called()

        # Case 2: active run differs from run_id -> nested start_run
        active_mock.info.run_id = "run-other"
        mlflow_mock.active_run.return_value = active_mock
        mlflow_mock.start_run.reset_mock()

        self.tracker.log_to_mlflow(run_id="run-child")
        mlflow_mock.start_run.assert_called_with(run_id="run-child", nested=True)


if __name__ == "__main__":
    unittest.main()



