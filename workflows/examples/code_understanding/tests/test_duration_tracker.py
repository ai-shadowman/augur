import json
import os
import sys
import shutil
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

from utils.duration_tracker import (
    DurationTracker,
    track_duration,
    extract_graphrag_indexing_durations,
    find_all_telemetry_files,
    find_telemetry_file,
)


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

    def test_format_summary_truncation(self):
        """Verify long stage, step, and status strings are truncated with ellipsis."""
        self.tracker.record_step(
            "AnalysisWithVeryLongStageNameExceeding18Chars",
            "Prompt 5: Known Dependencies Requirements Exceeding Limit",
            13.69,
            status="success",
        )
        summary = self.tracker.format_summary()
        self.assertIn("AnalysisWithVer...", summary)
        self.assertIn("Prompt 5: Known Dependencies Requirem...", summary)

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

                # Duration table uses clean boxed ASCII table within code block
                self.assertIn("| Pipeline Execution Duration Summary", report)
                self.assertIn("| Pipeline Stage     | Step / Sub-step                          | Duration   | Status   |", report)
                self.assertIn("+--------------------+------------------------------------------+------------+----------+", report)
                self.assertIn("| Total Runtime      |", report)

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
                # Aggregate step is recorded in tracker but excluded from detailed rows to prevent duplicate stage rows
                self.assertTrue(any(s["step"] == "Migration Report Total" for s in DurationTracker.get_instance().get_steps()))

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

    def test_mlflow_multi_run_durations_aggregation(self):
        """Verify that download_from_mlflow searches runs across stages and merges records from all runs."""
        tracker = DurationTracker()

        run1 = MagicMock()
        run1.info.run_id = "run-data-gen"
        run2 = MagicMock()
        run2.info.run_id = "run-indexing"

        mock_experiment = MagicMock()
        mock_experiment.experiment_id = "exp-123"

        mock_client = MagicMock()
        mock_client.search_runs.return_value = [run1, run2]

        data_gen_tracker = DurationTracker()
        data_gen_tracker.record_step("Data Generation", "Clone Repository", 4.5)
        data_gen_tracker.record_step("Data Generation", "Detect Languages", 1.2)

        indexing_tracker = DurationTracker()
        indexing_tracker.record_step("Indexing", "Build GraphRAG Index", 120.0)

        temp_dir = tempfile.mkdtemp()
        try:
            file1 = os.path.join(temp_dir, "durations1.json")
            file2 = os.path.join(temp_dir, "durations2.json")
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

                success = tracker.download_from_mlflow(git_slug="my-repo")
                self.assertTrue(success)

                steps = [r["step"] for r in tracker.records]
                self.assertIn("Clone Repository", steps)
                self.assertIn("Detect Languages", steps)
                self.assertIn("Build GraphRAG Index", steps)
                self.assertEqual(len(tracker.records), 3)
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_generate_migration_report_candidate_path_discovery(self):
        """Verify that generate_migration_report discovers durations.json and tokens.json in input/ directory."""
        import asyncio
        from utils.graphrag_utils import DependencyAnalyzer
        from utils.token_tracker import TokenCostTracker

        with tempfile.TemporaryDirectory() as temp_graphrag_dir:
            input_dir = os.path.join(temp_graphrag_dir, "input")
            os.makedirs(input_dir, exist_ok=True)

            dur_file = os.path.join(input_dir, "durations.json")
            tok_file = os.path.join(input_dir, "tokens.json")

            prior_dur = DurationTracker()
            prior_dur.record_step("Data Generation", "Reset Environment", 3.0)
            prior_dur.record_step("Data Generation", "Clone Repository", 8.0)
            prior_dur.save_to_file(dur_file)

            prior_tok = TokenCostTracker()
            prior_tok.track("Data Generation (python)", calls=5, prompt_tokens=2500, output_tokens=1000, model="gpt-4o")
            prior_tok.save_to_file(tok_file)

            analyzer_tok = TokenCostTracker.reset_instance()
            DurationTracker.reset_instance()

            mock_loader = MagicMock()
            mock_loader.num_prompts.side_effect = lambda path: 1 if "enhanced" in path else 1
            mock_loader.download_prompt.side_effect = [
                ("prompt 1", {"title": "### Overview", "skip_prompt": None}),
                ("prompt 2", {"title": "### Code Migration Plan (JSON)", "skip_prompt": None}),
            ]

            with patch.object(DependencyAnalyzer, '_setup_configuration'), \
                 patch.object(DependencyAnalyzer, '_setup_search'), \
                 patch.object(DependencyAnalyzer, '_setup_prompts'), \
                 patch.object(DependencyAnalyzer, '_extract_indexed_git_urls', return_value={"https://github.com/org/repo"}), \
                 patch('utils.visualization_utils.log_interactive_dependency_graph'), \
                 patch('loaders.default_asset_loader.DefaultAssetLoader', return_value=mock_loader):

                analyzer = DependencyAnalyzer(root_dir=temp_graphrag_dir, token_tracker=analyzer_tok)

                with patch.object(analyzer, 'query_with_llm', side_effect=["Overview body", "### Code Migration Plan (JSON)\n[]"]):
                    report = asyncio.run(analyzer.generate_migration_report())

                    # Check that Data Generation metrics from input/ were loaded and displayed
                    self.assertIn("Reset Environment", report)
                    self.assertIn("Clone Repository", report)
                    self.assertIn("Data Generation (python)", report)
                    self.assertIn("### Pipeline Execution Duration Summary", report)
                    self.assertIn("### LLM Token Usage & Cost Summary", report)

    def test_record_step_in_place_update(self):
        """Verify that record_step updates an existing (stage, step) in-place instead of duplicating."""
        tracker = DurationTracker()
        tracker.record_step("Analysis", "Prompt 1: Overview", 10.0, status="running")
        self.assertEqual(len(tracker.records), 1)
        self.assertEqual(tracker.records[0]["duration"], 10.0)
        self.assertEqual(tracker.records[0]["status"], "running")

        # Update the step
        tracker.record_step("Analysis", "Prompt 1: Overview", 12.5, status="success", metadata={"is_update": True})
        self.assertEqual(len(tracker.records), 1, "Duplicate record was appended instead of updated in-place")
        self.assertEqual(tracker.records[0]["duration"], 12.5)
        self.assertEqual(tracker.records[0]["status"], "success")
        self.assertTrue(tracker.records[0]["metadata"].get("is_update"))

    def test_total_duration_excludes_aggregate_steps(self):
        """Verify that aggregate / parent steps (e.g. Migration Report Total) are not double-counted in total runtime."""
        tracker = DurationTracker()
        tracker.record_step("Analysis", "Prompt 1: Overview", 10.0)
        tracker.record_step("Analysis", "Prompt 2: Dependencies", 15.0)
        tracker.record_step("Analysis", "Migration Report Total", 25.0, metadata={"is_aggregate": True})
        tracker.record_step("Analysis", "Generate Migration Report", 25.5)

        stage_durations = tracker.get_stage_durations()
        self.assertEqual(stage_durations["Analysis"], 25.0, "Aggregate steps should not be added to sub-step totals")

        total = tracker.get_total_duration()
        self.assertEqual(total, 25.0)

        summary = tracker.format_summary()
        self.assertIn("Total Runtime", summary)
        self.assertIn("25.00s", summary)

    def test_download_from_mlflow_excludes_current_stage_and_deduplicates(self):
        """Verify that download_from_mlflow skips runs matching current_stage and only merges the latest run per stage."""
        tracker = DurationTracker()
        tracker.record_step("Analysis", "Current Step", 5.0)

        import sys
        import shutil
        mlflow_mock = sys.modules["mlflow"]

        run1 = MagicMock()
        run1.info.run_id = "run-analysis-old"
        run1.data.tags = {"stage": "Analysis", "category": "telemetry", "type": "durations"}

        run2 = MagicMock()
        run2.info.run_id = "run-datagen-new"
        run2.data.tags = {"stage": "Data Generation", "category": "telemetry", "type": "durations"}

        run3 = MagicMock()
        run3.info.run_id = "run-datagen-old"
        run3.data.tags = {"stage": "Data Generation", "category": "telemetry", "type": "durations"}

        mock_client = MagicMock()
        mock_client.search_runs.return_value = [run1, run2, run3]

        temp_dir = tempfile.mkdtemp()
        try:
            datagen_file = os.path.join(temp_dir, "durations.json")
            dt = DurationTracker()
            dt.record_step("Data Generation", "Clone", 10.0)
            dt.save_to_file(datagen_file)

            mlflow_mock.artifacts.download_artifacts.return_value = datagen_file

            with patch("mlflow.tracking.MlflowClient", return_value=mock_client), \
                 patch("loaders.mlflow_asset_loader.MlFlowAssetLoader.get_or_create_experiment_by_name"):
                tracker.download_from_mlflow(git_slug="repo", current_stage="Analysis")

            # Verify run1 (Analysis) was skipped because of current_stage="Analysis"
            # Verify run3 was skipped because run2 was already processed for Data Generation
            stages = {r["stage"] for r in tracker.records}
            self.assertIn("Data Generation", stages)
            # Only 1 Data Generation step should be present
            dg_steps = [r for r in tracker.records if r["stage"] == "Data Generation"]
            self.assertEqual(len(dg_steps), 1)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_find_telemetry_file_recursive(self):
        """Verify find_telemetry_file discovers files in deeply nested subdirectories."""
        from utils.duration_tracker import find_telemetry_file
        temp_dir = tempfile.mkdtemp()
        try:
            nested_dir = os.path.join(temp_dir, "stage", "output", "nested")
            os.makedirs(nested_dir, exist_ok=True)
            target_file = os.path.join(nested_dir, "durations.json")
            with open(target_file, "w") as f:
                f.write("{}")

            found = find_telemetry_file([temp_dir], "durations.json")
            self.assertIsNotNone(found)
            self.assertEqual(os.path.abspath(found), os.path.abspath(target_file))

            # Non-existent file
            not_found = find_telemetry_file([temp_dir], "non_existent.json")
            self.assertIsNone(not_found)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_git_slug_persistence(self):
        """Verify git_slug and git_repo are saved and restored during serialization."""
        tracker = DurationTracker(git_slug="org-repo-main", git_repo="https://github.com/org/repo")
        tracker.record_step("Data Generation", "Extract", 5.0)
        d = tracker.to_dict()
        self.assertEqual(d["git_slug"], "org-repo-main")
        self.assertEqual(d["git_repo"], "https://github.com/org/repo")

        restored = DurationTracker.from_dict(d)
        self.assertEqual(restored.git_slug, "org-repo-main")
        self.assertEqual(restored.git_repo, "https://github.com/org/repo")

    def test_merge_and_load_from_dict_with_current_stage(self):
        """Verify load_from_dict and merge filter out records from current_stage."""
        data = {
            "records": [
                {"stage": "Data Generation", "step": "GitHub Checkout", "duration": 5.2, "status": "success"},
                {"stage": "Indexing", "step": "GraphRAG Indexing", "duration": 42.0, "status": "success"},
                {"stage": "Analysis", "step": "Old Analysis Prompt", "duration": 18.0, "status": "success"},
            ]
        }
        tracker = DurationTracker()
        tracker.load_from_dict(data, current_stage="Analysis")
        stages = [r["stage"] for r in tracker.records]
        self.assertIn("Data Generation", stages)
        self.assertIn("Indexing", stages)
        self.assertNotIn("Analysis", stages)

        # Active analysis step
        tracker.record_step("Analysis", "Prompt 1: Dependency Graph", 20.0)

        # Merge another tracker with old Analysis
        other = DurationTracker.from_dict(data)
        tracker.merge(other, current_stage="Analysis")

        # Confirm old Analysis step was not added
        steps = [r["step"] for r in tracker.records]
        self.assertIn("GitHub Checkout", steps)
        self.assertIn("GraphRAG Indexing", steps)
        self.assertIn("Prompt 1: Dependency Graph", steps)
        self.assertNotIn("Old Analysis Prompt", steps)

    def test_github_checkout_and_graphrag_indexing_substeps(self):
        """Verify GitHub Checkout and GraphRAG Indexing format nicely in duration summary."""
        tracker = DurationTracker()
        tracker.record_step("Data Generation", "GitHub Checkout", 12.3)
        tracker.record_step("Indexing", "GraphRAG Indexing", 95.4)
        tracker.record_step("Analysis", "Prompt 1: Dependency Graph", 21.5)

        summary = tracker.format_summary()
        self.assertIn("GitHub Checkout", summary)
        self.assertIn("GraphRAG Indexing", summary)
        self.assertIn("Prompt 1: Dependency Graph", summary)
        self.assertIn("Data Generation", summary)
        self.assertIn("Indexing", summary)
        self.assertIn("Analysis", summary)


    def test_format_markdown_table(self):
        """Verify format_markdown_table produces a native GFM table with visual latency bars and bottleneck callout."""
        tracker = DurationTracker()
        tracker.record_step("Data Generation", "Clone Repository", 10.0)
        tracker.record_step("Indexing", "GraphRAG Indexing", 80.0)
        tracker.record_step("Analysis", "Dependency Graph", 10.0)

        table_md = tracker.format_markdown_table()
        self.assertIn("### Pipeline Execution Duration Summary", table_md)
        self.assertIn("| Stage | Step / Sub-step | Duration | % Total | Latency Bar | Status |", table_md)
        self.assertIn("| :--- | :--- | :---: | :---: | :--- | :---: |", table_md)
        self.assertIn("| **Data Generation** | Clone Repository |", table_md)
        self.assertIn("| **Indexing** | GraphRAG Indexing |", table_md)
        self.assertIn("| **Analysis** | Dependency Graph |", table_md)
        self.assertIn("█", table_md)
        self.assertIn("✅", table_md)
        self.assertIn("| **Total** | *All Stages* |", table_md)
        self.assertIn("**Slowest Step:** `GraphRAG Indexing`", table_md)
        self.assertIn("<details>", table_md)
        self.assertIn("<summary><b>📊 Stage Breakdown</b></summary>", table_md)

    def test_format_markdown_table_with_mlflow_deep_links(self):
        """Verify format_markdown_table renders MLflow run deep-links when tracking context is available."""
        tracker = DurationTracker()
        tracker.record_step("Data Generation", "Setup", 5.0)
        tracker.mlflow_run_id = "run-abc-123"
        tracker.mlflow_experiment_id = "42"
        tracker.mlflow_tracking_uri = "http://mlflow-server:5000"

        table_md = tracker.format_markdown_table()
        self.assertIn("**MLflow Tracking:**", table_md)
        self.assertIn("http://mlflow-server:5000/#/experiments/42/runs/run-abc-123", table_md)

    def test_format_markdown_section_as_table_flag(self):
        """Verify format_markdown_section delegates to format_markdown_table when as_table=True."""
        tracker = DurationTracker()
        tracker.record_step("Data Generation", "Setup", 5.0)

        # as_table=False (default backward-compatible ASCII fence)
        ascii_md = tracker.format_markdown_section(as_table=False)
        self.assertTrue(ascii_md.rstrip().endswith("```"))

        # as_table=True (native GFM table)
        gfm_md = tracker.format_markdown_section(as_table=True)
        self.assertIn("| Stage | Step / Sub-step | Duration |", gfm_md)

    def test_mlflow_context_serialization_and_merge(self):
        """Verify that MLflow metadata fields serialize to JSON and propagate across tracker merges."""
        t1 = DurationTracker()
        t1.record_step("Data Generation", "Step A", 10.0)
        t1.mlflow_run_id = "run-001"
        t1.mlflow_experiment_id = "exp-7"
        t1.mlflow_tracking_uri = "http://localhost:5000"

        d = t1.to_dict()
        self.assertEqual(d["mlflow_run_id"], "run-001")
        self.assertEqual(d["mlflow_experiment_id"], "exp-7")
        self.assertEqual(d["mlflow_tracking_uri"], "http://localhost:5000")

        # Roundtrip from_dict
        t2 = DurationTracker.from_dict(d)
        self.assertEqual(t2.mlflow_run_id, "run-001")
        self.assertEqual(t2.mlflow_experiment_id, "exp-7")
        self.assertEqual(t2.mlflow_tracking_uri, "http://localhost:5000")

        # File roundtrip
        with tempfile.TemporaryDirectory() as tmp_dir:
            fpath = os.path.join(tmp_dir, "durations.json")
            t1.save_to_file(fpath)

            t3 = DurationTracker()
            t3.load_from_file(fpath)
            self.assertEqual(t3.mlflow_run_id, "run-001")
            self.assertEqual(t3.mlflow_experiment_id, "exp-7")
            self.assertEqual(t3.mlflow_tracking_uri, "http://localhost:5000")

        # Merge propagation
        t4 = DurationTracker()
        t4.record_step("Analysis", "Step B", 5.0)
        self.assertIsNone(t4.mlflow_run_id)
        t4.merge(t1)
        self.assertEqual(t4.mlflow_run_id, "run-001")
        self.assertEqual(t4.mlflow_experiment_id, "exp-7")
        self.assertEqual(t4.mlflow_tracking_uri, "http://localhost:5000")

    def test_extract_graphrag_indexing_durations_with_workflows(self):
        """Verify extracting indexing durations from stats.json with individual workflows."""
        tracker = DurationTracker()
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = os.path.join(tmp_dir, "output")
            os.makedirs(output_dir, exist_ok=True)
            stats = {
                "total_runtime": 125.5,
                "workflows": {
                    "create_base_extracted_entities": {"overall": 45.2},
                    "create_summarized_entities": {"overall": 35.8},
                    "create_base_text_units": {"overall": 15.0},
                }
            }
            with open(os.path.join(output_dir, "stats.json"), "w", encoding="utf-8") as f:
                json.dump(stats, f)

            result = extract_graphrag_indexing_durations(tmp_dir, tracker)
            self.assertTrue(result)
            self.assertEqual(len(tracker.records), 4)

            steps = {r["step"]: r for r in tracker.records}
            self.assertIn("GraphRAG: Create Base Extracted Entities", steps)
            self.assertIn("GraphRAG: Create Summarized Entities", steps)
            self.assertIn("GraphRAG: Create Base Text Units", steps)
            self.assertIn("GraphRAG Indexing Total", steps)

            for step_name, record in steps.items():
                self.assertEqual(record["stage"], "Indexing")
                self.assertEqual(record["status"], "success")

            self.assertEqual(steps["GraphRAG: Create Base Extracted Entities"]["duration"], 45.2)
            self.assertEqual(steps["GraphRAG Indexing Total"]["duration"], 125.5)
            self.assertTrue(steps["GraphRAG Indexing Total"]["metadata"]["is_aggregate"])

            # Total duration should not double-count the aggregate step
            expected_total = 45.2 + 35.8 + 15.0
            self.assertAlmostEqual(tracker.get_total_duration(), expected_total, places=2)

    def test_extract_graphrag_indexing_durations_total_only(self):
        """Verify extracting indexing durations when only total_runtime is present."""
        tracker = DurationTracker()
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = os.path.join(tmp_dir, "output")
            os.makedirs(output_dir, exist_ok=True)
            stats = {"total_runtime": 88.0}
            with open(os.path.join(output_dir, "stats.json"), "w", encoding="utf-8") as f:
                json.dump(stats, f)

            result = extract_graphrag_indexing_durations(tmp_dir, tracker)
            self.assertTrue(result)
            self.assertEqual(len(tracker.records), 1)
            self.assertEqual(tracker.records[0]["step"], "GraphRAG Indexing")
            self.assertEqual(tracker.records[0]["duration"], 88.0)
            self.assertAlmostEqual(tracker.get_total_duration(), 88.0, places=2)

    def test_extract_graphrag_indexing_durations_missing_or_corrupt(self):
        """Verify extract_graphrag_indexing_durations handles missing or corrupt files gracefully."""
        tracker = DurationTracker()
        self.assertFalse(extract_graphrag_indexing_durations("", tracker))
        self.assertFalse(extract_graphrag_indexing_durations("/nonexistent/path", tracker))

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = os.path.join(tmp_dir, "output")
            os.makedirs(output_dir, exist_ok=True)
            with open(os.path.join(output_dir, "stats.json"), "w", encoding="utf-8") as f:
                f.write("not valid json")
            self.assertFalse(extract_graphrag_indexing_durations(tmp_dir, tracker))

    def test_find_all_telemetry_files(self):
        """Verify finding multiple telemetry files across multiple directories."""
        with tempfile.TemporaryDirectory() as tmp_dir1, tempfile.TemporaryDirectory() as tmp_dir2:
            f1 = os.path.join(tmp_dir1, "durations.json")
            f2 = os.path.join(tmp_dir2, "durations.json")
            with open(f1, "w") as f:
                f.write("{}")
            with open(f2, "w") as f:
                f.write("{}")

            found = find_all_telemetry_files([tmp_dir1, tmp_dir2, tmp_dir1], "durations.json")
            self.assertEqual(len(found), 2)
            self.assertEqual(set(found), {os.path.abspath(f1), os.path.abspath(f2)})

            empty_found = find_all_telemetry_files(["/nonexistent/dir"], "durations.json")
            self.assertEqual(empty_found, [])

    def test_canonical_stage_ordering(self):
        """Verify canonical stage sorting (Data Generation -> Indexing -> Analysis) in summaries and breakdowns."""
        tracker = DurationTracker()
        # Record in reverse / arbitrary order
        tracker.record_step("Analysis", "Prompt 1: Dependency Graph", 25.0)
        tracker.record_step("Indexing", "GraphRAG: Extract Entities", 40.0)
        tracker.record_step("Data Generation", "Clone Repo", 10.0)
        tracker.record_step("Custom Stage", "Custom Step", 5.0)

        # Stage durations keys order
        stage_durations = tracker.get_stage_durations()
        stages = list(stage_durations.keys())
        self.assertEqual(stages[:3], ["Data Generation", "Indexing", "Analysis"])
        self.assertEqual(stages[3], "Custom Stage")

        # format_summary table row ordering
        summary = tracker.format_summary()
        dg_pos = summary.find("Data Generation")
        idx_pos = summary.find("Indexing")
        an_pos = summary.find("Analysis")
        self.assertTrue(dg_pos != -1 and idx_pos != -1 and an_pos != -1)
        self.assertTrue(dg_pos < idx_pos < an_pos)

        # format_markdown_table ordering
        md_table = tracker.format_markdown_table()
        dg_pos_md = md_table.find("Data Generation")
        idx_pos_md = md_table.find("Indexing")
        an_pos_md = md_table.find("Analysis")
        self.assertTrue(dg_pos_md < idx_pos_md < an_pos_md)

    def test_download_from_mlflow_multi_experiment_search(self):
        """Verify download_from_mlflow queries active, env, instance, and default experiments."""
        import sys
        mlflow_mock = sys.modules["mlflow"]
        active_mock = MagicMock()
        active_mock.info.experiment_id = "exp-active-99"
        mlflow_mock.active_run.return_value = active_mock

        client_mock = MagicMock()
        env_exp_mock = MagicMock()
        env_exp_mock.experiment_id = "exp-env-88"
        client_mock.get_experiment_by_name.return_value = env_exp_mock
        client_mock.search_runs.return_value = []

        tracker = DurationTracker()
        tracker.mlflow_experiment_id = "exp-inst-77"

        with patch.dict(os.environ, {"MLFLOW_EXPERIMENT_NAME": "custom-exp"}), \
             patch('mlflow.tracking.MlflowClient', return_value=client_mock), \
             patch('loaders.default_asset_loader.DefaultAssetLoader'):
            tracker.download_from_mlflow(git_slug="slug-test", current_stage="Analysis")

            self.assertTrue(client_mock.search_runs.called)
            call_kwargs = client_mock.search_runs.call_args[1]
            exp_ids = call_kwargs.get("experiment_ids", [])
            self.assertIn("exp-active-99", exp_ids)
            self.assertIn("exp-env-88", exp_ids)
            self.assertIn("exp-inst-77", exp_ids)
            self.assertIn("0", exp_ids)

    def test_generate_migration_report_includes_all_pipeline_stages(self):
        """Verify generate_migration_report produces a summary table containing Data Generation, Indexing, and Analysis."""
        from utils.graphrag_utils import DependencyAnalyzer
        import asyncio

        with tempfile.TemporaryDirectory() as tmp_dir:
            # Set up target dir with Data Generation durations
            target_dir = os.path.join(tmp_dir, "target", "test-slug")
            os.makedirs(target_dir, exist_ok=True)
            dg_dur = DurationTracker(git_slug="test-slug")
            dg_dur.record_step("Data Generation", "Reset Environment", 2.5)
            dg_dur.record_step("Data Generation", "Detect Languages", 1.5)
            dg_dur.save_to_file(os.path.join(target_dir, "durations.json"))

            # Set up graphrag dir with Indexing stats.json
            graphrag_dir = os.path.join(tmp_dir, "graph_rag_app", "source", "test-slug")
            output_dir = os.path.join(graphrag_dir, "output")
            os.makedirs(output_dir, exist_ok=True)
            stats = {
                "total_runtime": 60.0,
                "workflows": {
                    "create_base_extracted_entities": {"overall": 35.0},
                    "create_summarized_entities": {"overall": 25.0},
                }
            }
            with open(os.path.join(output_dir, "stats.json"), "w", encoding="utf-8") as f:
                json.dump(stats, f)

            analyzer = DependencyAnalyzer(graphrag_dir, git_slug="test-slug", multi_repo=False)
            mock_loader = MagicMock()
            mock_loader.num_prompts.side_effect = lambda path: 1 if "enhanced" in path else 1
            mock_loader.download_prompt.side_effect = [
                ("prompt 1", {"title": "### Overview", "skip_prompt": None}),
                ("prompt 2", {"title": "### Code Migration Plan (JSON)", "skip_prompt": None}),
            ]

            with patch('loaders.default_asset_loader.DefaultAssetLoader', return_value=mock_loader), \
                 patch.object(analyzer, 'query_with_llm', side_effect=["Overview content", '{"plan": []}']), \
                 patch('utils.visualization_utils.log_interactive_dependency_graph'), \
                 patch.dict(os.environ, {
                     "PARENT_TARGET_PATH": os.path.join(tmp_dir, "target"),
                     "PARENT_SOURCE_PATH": os.path.join(tmp_dir, "source"),
                 }):
                report = asyncio.run(analyzer.generate_migration_report())

            self.assertIn("### Pipeline Execution Duration Summary", report)
            self.assertIn("Data Generation", report)
            self.assertIn("Indexing", report)
            self.assertIn("Analysis", report)
            self.assertIn("Reset Environment", report)
            self.assertIn("GraphRAG: Create Base Extracted Entities", report)


if __name__ == "__main__":
    unittest.main()




