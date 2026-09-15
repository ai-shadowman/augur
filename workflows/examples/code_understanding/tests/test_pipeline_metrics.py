"""Unit tests for PipelineMetricsTracker."""

import os
import sys
import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Ensure test import path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# Provide lightweight mock stubs for external packages if not installed in current environment
mock_kfp = MagicMock()
mock_dsl = MagicMock()
mock_dsl.pipeline = lambda *args, **kwargs: (lambda fn: fn)
mock_kfp.dsl = mock_dsl

mock_module_names = [
    "jsonpath_ng",
    "github",
    "pygments",
    "pygments.lexers",
    "pygments.util",
    "mlflow",
    "mlflow.tracking",
    "graphrag",
    "graphrag.api",
    "graphrag.config",
    "graphrag.config.load_config",
    "loaders",
    "loaders.default_asset_loader",
    "loaders.mlflow_asset_loader",
    "pandas",
    "networkx",
    "matplotlib",
    "matplotlib.pyplot",
    "pyvis",
    "pyvis.network",
]
for mod_name in mock_module_names:
    if mod_name not in sys.modules:
        m = MagicMock()
        m.__path__ = []
        sys.modules[mod_name] = m

for mod_name in mock_module_names:
    if "." in mod_name:
        parts = mod_name.split(".")
        parent = sys.modules.get(parts[0])
        if parent and len(parts) == 2:
            setattr(parent, parts[1], sys.modules[mod_name])

sys.modules["kfp"] = mock_kfp
sys.modules["kfp.dsl"] = mock_dsl

import utils.pipeline_utils
utils.pipeline_utils.uses_kfp = lambda: False

# Configure DefaultAssetLoader mock
dummy_loader_instance = MagicMock()
dummy_loader_instance.download.return_value = {
    "file_extensions": {"python": [".py"]},
    "config_file_extensions": {"python": [".yaml"]},
    "exclude_dirs": {"python": []},
    "comment_delimiters_by_extension": {".py": ["#"]},
    "comment_delimiters_by_language": {"python": ["#"]},
    "pygments_mappings": {"python": "python"},
}
dummy_loader_instance.download_prompt.return_value = ("", {})
dummy_loader_instance.num_prompts.return_value = 0
dummy_loader_instance.get_log_results_artifact_path.return_value = "results/path"
dummy_loader_instance.RESULTS_PATH_PREFIX_PIPELINES = "pipelines"
dummy_loader_instance.RESULTS_PATH_PREFIX_REPO_DATASETS = "datasets"
DefaultAssetLoaderMock = MagicMock(return_value=dummy_loader_instance)
DefaultAssetLoaderMock.RESULTS_PATH_PREFIX_PIPELINES = "pipelines"
DefaultAssetLoaderMock.RESULTS_PATH_PREFIX_REPO_DATASETS = "datasets"
DefaultAssetLoaderMock.get_log_results_artifact_path = MagicMock(return_value="results/path")
sys.modules["loaders.default_asset_loader"].DefaultAssetLoader = DefaultAssetLoaderMock

from utils.metrics_tracker import (
    PipelineMetricsTracker,
    StepMetric,
    AppMetric,
    StageMetric,
    format_duration,
)


class TestFormatDuration(unittest.TestCase):
    """Test duration formatting helper."""

    def test_seconds(self):
        self.assertEqual(format_duration(0), "0.00s")
        self.assertEqual(format_duration(0.456), "0.46s")
        self.assertEqual(format_duration(14.2), "14.20s")
        self.assertEqual(format_duration(59.99), "59.99s")

    def test_minutes(self):
        self.assertEqual(format_duration(60), "1m 0.0s")
        self.assertEqual(format_duration(95.5), "1m 35.5s")
        self.assertEqual(format_duration(3599), "59m 59.0s")

    def test_hours(self):
        self.assertEqual(format_duration(3600), "1h 00m 00s")
        self.assertEqual(format_duration(3665), "1h 01m 05s")
        self.assertEqual(format_duration(7325), "2h 02m 05s")

    def test_edge_cases(self):
        self.assertEqual(format_duration(None), "0.00s")
        self.assertEqual(format_duration(-10), "0.00s")


class TestPipelineMetricsTracker(unittest.TestCase):
    """Test PipelineMetricsTracker tracking and reporting."""

    def test_basic_pipeline_lifecycle(self):
        tracker = PipelineMetricsTracker("test-pipeline")
        self.assertEqual(tracker.pipeline_name, "test-pipeline")
        self.assertEqual(tracker.status, "running")
        self.assertIsNotNone(tracker.started_at)
        self.assertIsNone(tracker.stopped_at)

        dur = tracker.stop_pipeline(status="COMPLETED")
        self.assertGreaterEqual(dur, 0.0)
        self.assertEqual(tracker.status, "COMPLETED")
        self.assertIsNotNone(tracker.stopped_at)

    def test_pipeline_context_manager(self):
        tracker = PipelineMetricsTracker()
        with tracker.track_pipeline("cm-pipeline"):
            tracker.start_stage("Stage 1")
            tracker.stop_stage("Stage 1")
        self.assertEqual(tracker.status, "COMPLETED")
        self.assertEqual(tracker.pipeline_name, "cm-pipeline")

    def test_pipeline_context_manager_failure(self):
        tracker = PipelineMetricsTracker()
        with self.assertRaises(ValueError):
            with tracker.track_pipeline("failing-pipeline"):
                raise ValueError("Stage exploded")
        self.assertEqual(tracker.status, "FAILED")
        self.assertIn("Stage exploded", tracker.error_message)
        self.assertIsNotNone(tracker.stopped_at)

    def test_stage_tracking(self):
        tracker = PipelineMetricsTracker()
        with tracker.track_stage("Data Generation"):
            pass

        self.assertIn("Data Generation", tracker.stages)
        stage = tracker.stages["Data Generation"]
        self.assertEqual(stage.status, "COMPLETED")
        self.assertGreaterEqual(stage.duration, 0.0)
        self.assertIsNotNone(stage.stopped_at)

    def test_app_tracking_multi_repo(self):
        tracker = PipelineMetricsTracker(multi_repo=True)
        with tracker.track_stage("Data Generation"):
            with tracker.track_app("repo-alpha", git_repo="https://github.com/org/alpha", git_branch="main"):
                with tracker.track_step("prepare_environment"):
                    pass
                with tracker.track_step("detect_languages"):
                    pass

            with tracker.track_app("repo-beta", git_repo="https://github.com/org/beta", git_branch="develop"):
                with tracker.track_step("prepare_environment"):
                    pass

        self.assertIn("repo-alpha", tracker.apps)
        self.assertIn("repo-beta", tracker.apps)

        alpha = tracker.apps["repo-alpha"]
        self.assertEqual(alpha.git_branch, "main")
        self.assertEqual(len(alpha.steps), 2)
        self.assertEqual(alpha.steps[0].name, "prepare_environment")
        self.assertEqual(alpha.steps[1].name, "detect_languages")

        beta = tracker.apps["repo-beta"]
        self.assertEqual(beta.git_branch, "develop")
        self.assertEqual(len(beta.steps), 1)

    def test_granular_step_tracking(self):
        tracker = PipelineMetricsTracker()
        with tracker.track_stage("Indexing"):
            with tracker.track_step("generate_graphrag_index", details={"model": "gpt-4o"}):
                pass
            with tracker.track_step("evaluate_graphrag_index"):
                pass

        stage = tracker.stages["Indexing"]
        self.assertEqual(len(stage.steps), 2)
        self.assertEqual(stage.steps[0].name, "generate_graphrag_index")
        self.assertEqual(stage.steps[0].details.get("model"), "gpt-4o")

    def test_step_failure_records_error(self):
        tracker = PipelineMetricsTracker()
        try:
            with tracker.track_step("failing_step"):
                raise RuntimeError("Disk full")
        except RuntimeError:
            pass

        self.assertEqual(len(tracker.steps), 1)
        self.assertEqual(tracker.steps[0].status, "FAILED")
        self.assertIn("Disk full", tracker.steps[0].error_message)

    def test_format_summary_table_single_repo(self):
        tracker = PipelineMetricsTracker("single-repo-pipeline", multi_repo=False)
        with tracker.track_pipeline():
            with tracker.track_stage("Data Generation"):
                with tracker.track_step("prepare_environment"):
                    pass
                with tracker.track_step("generate_code_and_meta"):
                    pass
            with tracker.track_stage("Indexing"):
                with tracker.track_step("generate_graphrag_index"):
                    pass

        table = tracker.format_summary_table()
        self.assertIn("PIPELINE EXECUTION METRICS", table)
        self.assertIn("Pipeline:       single-repo-pipeline", table)
        self.assertIn("[STAGE] Data Generation", table)
        self.assertIn("- prepare_environment", table)
        self.assertIn("- generate_code_and_meta", table)
        self.assertIn("[STAGE] Indexing", table)
        self.assertIn("- generate_graphrag_index", table)
        self.assertIn("TOTAL DURATION", table)

    def test_format_summary_table_multi_repo(self):
        tracker = PipelineMetricsTracker("multi-repo-pipeline", multi_repo=True)
        with tracker.track_pipeline():
            with tracker.track_stage("Data Generation"):
                with tracker.track_app("app-a", git_repo="https://git/app-a", git_branch="main"):
                    with tracker.track_step("clone"):
                        pass
                with tracker.track_app("app-b", git_repo="https://git/app-b", git_branch="v1"):
                    with tracker.track_step("clone"):
                        pass
            with tracker.track_stage("Indexing"):
                with tracker.track_step("indexing"):
                    pass

        table = tracker.format_summary_table()
        self.assertIn("PIPELINE EXECUTION METRICS", table)
        self.assertIn("[STAGE] Data Generation", table)
        self.assertIn("[APP] app-a", table)
        self.assertIn("[APP] app-b", table)
        self.assertIn("MULTI-REPO APP DURATION BREAKDOWN", table)
        self.assertIn("app-a", table)
        self.assertIn("app-b", table)
        self.assertIn("Total Apps (2)", table)

    def test_format_markdown_table(self):
        tracker = PipelineMetricsTracker("test-markdown", multi_repo=True)
        with tracker.track_pipeline():
            with tracker.track_stage("Data Generation"):
                with tracker.track_app("app-one", git_branch="main"):
                    with tracker.track_step("step-1"):
                        pass

        md = tracker.format_markdown_table()
        self.assertIn("### Pipeline Execution Metrics: `test-markdown`", md)
        self.assertIn("| Stage / App / Step | Started At | Stopped At | Duration | Status |", md)
        self.assertIn("**Stage: Data Generation**", md)
        self.assertIn("**App: app-one**", md)
        self.assertIn("↳ step-1", md)
        self.assertIn("Multi-Repo Application Breakdown", md)

    def test_to_dict_and_save_to_file(self):
        tracker = PipelineMetricsTracker("serialize-test", multi_repo=True)
        with tracker.track_pipeline():
            with tracker.track_stage("Stage A"):
                with tracker.track_app("app-x"):
                    with tracker.track_step("step-x"):
                        pass

        d = tracker.to_dict()
        self.assertEqual(d["pipeline_name"], "serialize-test")
        self.assertTrue(d["multi_repo"])
        self.assertEqual(d["status"], "COMPLETED")
        self.assertIn("Stage A", d["stages"])
        self.assertIn("app-x", d["apps"])

        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "metrics.json")
            tracker.save_to_file(file_path)
            self.assertTrue(os.path.exists(file_path))

            with open(file_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            self.assertEqual(loaded["pipeline_name"], "serialize-test")

    def test_log_to_mlflow(self):
        tracker = PipelineMetricsTracker("mlflow-test")
        with tracker.track_pipeline():
            with tracker.track_stage("Data Generation"):
                with tracker.track_app("test-app"):
                    pass

        mock_mlflow = MagicMock()
        with patch.dict("sys.modules", {"mlflow": mock_mlflow}):
            tracker.log_to_mlflow()
            mock_mlflow.log_metrics.assert_called_once()
            metrics_arg = mock_mlflow.log_metrics.call_args[0][0]
            self.assertIn("pipeline_total_duration_seconds", metrics_arg)
            self.assertIn("stage_data_generation_duration_seconds", metrics_arg)
            self.assertIn("app_test_app_duration_seconds", metrics_arg)


class TestPipelineStagesIntegration(unittest.TestCase):
    """Test metrics tracking integration across pipeline stages."""

    @patch("pipelines.base.data_generation.generate_code_and_meta")
    @patch("pipelines.base.data_generation.load_external_data", return_value={})
    @patch("pipelines.base.data_generation.detect_languages", return_value=["python"])
    @patch("pipelines.base.data_generation.prepare_environment")
    def test_data_generation_pipeline_run_with_metrics(
        self, mock_prep, mock_detect, mock_load, mock_gen
    ):
        from pipelines.base.data_generation import DataGenerationPipeline

        tracker = PipelineMetricsTracker("dg-test")
        with tracker.track_pipeline():
            with tracker.track_stage("Data Generation"):
                res = DataGenerationPipeline().run(
                    git_repo="https://github.com/example/test-repo",
                    git_branch="main",
                    source_path="source/test",
                    target_path="target/test",
                    metrics_tracker=tracker,
                )

        self.assertEqual(res["status"], "complete")
        self.assertIn("metrics", res)
        stage = tracker.stages["Data Generation"]
        step_names = [s.name for s in stage.steps]
        self.assertIn("prepare_environment", step_names)
        self.assertIn("detect_languages", step_names)
        self.assertIn("load_external_data", step_names)
        self.assertTrue(any("generate_code_and_meta" in s for s in step_names))

    @patch("pipelines.base.data_generation.generate_code_and_meta")
    @patch("pipelines.base.data_generation.load_external_data", return_value={})
    @patch("pipelines.base.data_generation.detect_languages", return_value=["python"])
    @patch("pipelines.base.data_generation.prepare_environment")
    def test_data_generation_pipeline_run_multi_repo(
        self, mock_prep, mock_detect, mock_load, mock_gen
    ):
        from pipelines.base.data_generation import DataGenerationPipeline

        repos = [
            {"git_repo": "https://github.com/org/repo-one", "git_branch": "main"},
            {"git_repo": "https://github.com/org/repo-two", "git_branch": "develop"},
        ]

        tracker = PipelineMetricsTracker("multi-repo-test", multi_repo=True)
        with tracker.track_pipeline():
            with tracker.track_stage("Data Generation"):
                results = DataGenerationPipeline().run_multi_repo(repos, metrics_tracker=tracker)

        self.assertEqual(len(results), 2)
        self.assertIn("org-repo-one-main", tracker.apps)
        self.assertIn("org-repo-two-develop", tracker.apps)

        # Check that table contains both apps
        table = tracker.format_summary_table()
        self.assertIn("org-repo-one-main", table)
        self.assertIn("org-repo-two-develop", table)
        self.assertIn("MULTI-REPO APP DURATION BREAKDOWN", table)

    @patch("pipelines.base.indexing.evaluate_graphrag_index")
    @patch("pipelines.base.indexing.generate_graphrag_index")
    def test_indexing_pipeline_run_with_metrics(self, mock_gen, mock_eval):
        from pipelines.base.indexing import IndexingPipeline

        with tempfile.TemporaryDirectory() as tmp_codebase, \
             tempfile.TemporaryDirectory() as tmp_graphrag:
            tracker = PipelineMetricsTracker("idx-test")
            with tracker.track_pipeline():
                with tracker.track_stage("Indexing"):
                    res = IndexingPipeline().run(
                        codebase_path=tmp_codebase,
                        graphrag_source_path=tmp_graphrag,
                        git_repo="https://github.com/example/repo",
                        git_branch="main",
                        metrics_tracker=tracker,
                    )

            self.assertEqual(res["status"], "success")
            stage = tracker.stages["Indexing"]
            step_names = [s.name for s in stage.steps]
            self.assertIn("generate_graphrag_index", step_names)
            self.assertIn("evaluate_graphrag_index", step_names)

    @patch("utils.graphrag_utils.DependencyAnalyzer")
    def test_analysis_pipeline_run_with_metrics(self, mock_analyzer_cls):
        from pipelines.base.analysis import AnalysisPipeline
        import asyncio

        mock_analyzer = MagicMock()
        mock_analyzer.generate_migration_report.return_value = asyncio.sleep(0, result="# Migration Report")
        mock_analyzer_cls.return_value = mock_analyzer

        with tempfile.TemporaryDirectory() as tmp_graphrag:
            tracker = PipelineMetricsTracker("analysis-test")
            with tracker.track_pipeline():
                with tracker.track_stage("Analysis"):
                    report = AnalysisPipeline().run(
                        graphrag_source_path=tmp_graphrag,
                        git_repo="https://github.com/example/repo",
                        git_branch="main",
                        metrics_tracker=tracker,
                    )

            self.assertEqual(report, "# Migration Report")
            stage = tracker.stages["Analysis"]
            step_names = [s.name for s in stage.steps]
            self.assertIn("generate_migration_report", step_names)
            self.assertIn("log_results", step_names)


class TestOrchestratorIntegration(unittest.TestCase):
    """Test orchestrator execution and metrics reporting."""

    def test_single_repo_orchestrator(self):
        from pipelines.orchestrator import single_repo_pipeline

        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                with patch("pipelines.orchestrator.uses_kfp", return_value=False), \
                     patch("pipelines.orchestrator.DataGenerationPipeline.run") as mock_dg, \
                     patch("pipelines.orchestrator.IndexingPipeline.run") as mock_idx, \
                     patch("pipelines.orchestrator.AnalysisPipeline.run", return_value="# Report") as mock_an:
                    single_repo_pipeline(
                        git_repo="https://github.com/org/test-repo",
                        git_branch="main",
                        parent_source_path="source",
                        parent_target_path="target",
                    )
                    self.assertTrue(mock_dg.called)
                    self.assertTrue(mock_idx.called)
                    self.assertTrue(mock_an.called)

                # Verify pipeline metrics json was saved
                metrics_files = [f for f in os.listdir(tmpdir) if f.startswith("pipeline_metrics_")]
                self.assertTrue(len(metrics_files) >= 1)
            finally:
                os.chdir(old_cwd)

    def test_multi_repo_orchestrator(self):
        from pipelines.orchestrator import multi_repo_pipeline

        repos = [
            {"git_repo": "https://github.com/org/alpha", "git_branch": "main"},
            {"git_repo": "https://github.com/org/beta", "git_branch": "main"},
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            with patch.dict(os.environ, {"GIT_REPO_LIST_CONTENTS": json.dumps(repos)}):
                try:
                    os.chdir(tmpdir)
                    with patch("pipelines.orchestrator.uses_kfp", return_value=False), \
                         patch("pipelines.orchestrator.DataGenerationPipeline.run_multi_repo") as mock_dg, \
                         patch("pipelines.orchestrator.IndexingPipeline.run_multi_repo") as mock_idx, \
                         patch("pipelines.orchestrator.AnalysisPipeline.run_multi_repo", return_value="# Multi Report") as mock_an:
                        multi_repo_pipeline(
                            parent_source_path="source",
                            parent_target_path="target",
                        )
                        self.assertTrue(mock_dg.called)
                        self.assertTrue(mock_idx.called)
                        self.assertTrue(mock_an.called)

                    self.assertTrue(os.path.exists(os.path.join(tmpdir, "pipeline_metrics_multi_repo.json")))
                finally:
                    os.chdir(old_cwd)


class TestReportMetricsTablePlacement(unittest.TestCase):
    """Test placement of the Pipeline Execution Metrics table after the LLM Token Usage Table in reports."""

    def test_analyzer_metrics_tracker_initialization(self):
        """Verify DependencyAnalyzer receives or initializes a PipelineMetricsTracker."""
        from utils.graphrag_utils import DependencyAnalyzer
        custom_tracker = PipelineMetricsTracker("custom-pipeline", multi_repo=True)

        with patch.object(DependencyAnalyzer, '_setup_configuration'), \
             patch.object(DependencyAnalyzer, '_setup_search'), \
             patch.object(DependencyAnalyzer, '_setup_prompts'):
            analyzer = DependencyAnalyzer(metrics_tracker=custom_tracker)
            self.assertEqual(analyzer.metrics_tracker, custom_tracker)
            summary = analyzer.get_pipeline_metrics_summary()
            self.assertIn("PIPELINE EXECUTION METRICS", summary)

    def test_single_repo_report_table_placement(self):
        """Verify that in single-repo mode, the metrics table is placed immediately after the LLM token usage table, above JSON plan."""
        import asyncio
        from utils.graphrag_utils import DependencyAnalyzer

        custom_tracker = PipelineMetricsTracker("single-repo-pipeline", multi_repo=False)
        custom_tracker.start_pipeline()
        custom_tracker.start_stage("Data Generation")
        custom_tracker.stop_stage("Data Generation")
        custom_tracker.start_stage("Analysis")

        with patch.object(DependencyAnalyzer, '_setup_configuration'), \
             patch.object(DependencyAnalyzer, '_setup_search'), \
             patch.object(DependencyAnalyzer, '_setup_prompts'), \
             patch.object(DependencyAnalyzer, '_extract_indexed_git_urls', return_value={"https://github.com/org/repo"}), \
             patch('utils.visualization_utils.log_interactive_dependency_graph'):

            analyzer = DependencyAnalyzer(metrics_tracker=custom_tracker)

            mock_loader = MagicMock()
            mock_loader.num_prompts.side_effect = lambda path: 1 if "enhanced" in path else 1
            mock_loader.download_prompt.side_effect = [
                ("prompt 1", {"title": "### Overview", "skip_prompt": None}),
                ("prompt 2", {"title": "### Code Migration Plan (JSON)", "skip_prompt": None}),
            ]

            orig_loader = DefaultAssetLoaderMock.return_value
            DefaultAssetLoaderMock.return_value = mock_loader
            try:
                with patch.object(analyzer, 'query_with_llm', side_effect=["Overview content", '{"plan": []}']):
                    report = asyncio.run(analyzer.generate_migration_report())
            finally:
                DefaultAssetLoaderMock.return_value = orig_loader

            self.assertIn("### LLM Token Usage & Cost Summary", report)
            self.assertIn("### Pipeline Execution Metrics Summary", report)
            self.assertIn("### Code Migration Plan (JSON)", report)

            token_pos = report.index("### LLM Token Usage & Cost Summary")
            metrics_pos = report.index("### Pipeline Execution Metrics Summary")
            plan_pos = report.index("### Code Migration Plan (JSON)")

            # Table MUST be after LLM Token Usage Table
            self.assertGreater(metrics_pos, token_pos)
            # Both MUST be above Code Migration Plan (JSON)
            self.assertLess(metrics_pos, plan_pos)

            # Content checks
            self.assertIn("PIPELINE EXECUTION METRICS", report)
            self.assertIn("Data Generation", report)

    def test_multi_repo_report_table_placement(self):
        """Verify that in multi-repo mode, the metrics table is placed after the LLM token table at the very end of the report."""
        import asyncio
        from utils.graphrag_utils import DependencyAnalyzer

        custom_tracker = PipelineMetricsTracker("multi-repo-pipeline", multi_repo=True)
        custom_tracker.start_pipeline()
        with custom_tracker.track_app("service-alpha", git_branch="main"):
            pass
        with custom_tracker.track_app("service-beta", git_branch="feature"):
            pass

        with patch.object(DependencyAnalyzer, '_setup_configuration'), \
             patch.object(DependencyAnalyzer, '_setup_search'), \
             patch.object(DependencyAnalyzer, '_setup_prompts'), \
             patch.object(DependencyAnalyzer, '_extract_indexed_git_urls', return_value={"https://github.com/org/repo1", "https://github.com/org/repo2"}), \
             patch.object(DependencyAnalyzer, '_community_level_for_multi_repo', return_value=0), \
             patch('utils.visualization_utils.log_interactive_dependency_graph'):

            analyzer = DependencyAnalyzer(multi_repo=True, metrics_tracker=custom_tracker)

            mock_loader = MagicMock()
            mock_loader.num_prompts.side_effect = lambda path: 1 if "enhanced" in path else 1
            mock_loader.download_prompt.side_effect = [
                ("prompt 1", {"title": "### System Architecture Summary", "skip_prompt": None}),
                ("prompt 2", {"title": "### Recommended Migration Order", "skip_prompt": None}),
            ]

            orig_loader = DefaultAssetLoaderMock.return_value
            DefaultAssetLoaderMock.return_value = mock_loader
            try:
                with patch.object(analyzer, 'query_with_llm', side_effect=["Multi-repo architecture.", "Recommended order."]):
                    report = asyncio.run(analyzer.generate_migration_report())
            finally:
                DefaultAssetLoaderMock.return_value = orig_loader

            self.assertIn("### LLM Token Usage & Cost Summary", report)
            self.assertIn("### Pipeline Execution Metrics Summary", report)
            self.assertIn("### Recommended Migration Order", report)

            order_pos = report.index("### Recommended Migration Order")
            token_pos = report.index("### LLM Token Usage & Cost Summary")
            metrics_pos = report.index("### Pipeline Execution Metrics Summary")

            # In multi-repo, token table is placed after report content
            self.assertGreater(token_pos, order_pos)
            # Metrics table is placed after token table
            self.assertGreater(metrics_pos, token_pos)

            # In multi-repo, contains app duration breakdown
            self.assertIn("MULTI-REPO APP DURATION BREAKDOWN", report)
            self.assertIn("service-alpha", report)
            self.assertIn("service-beta", report)

            # Ends with code block
            self.assertTrue(report.rstrip().endswith("```"))


class TestCrossStagePipelineMetrics(unittest.TestCase):
    """Test cross-stage persistence, deserialization, merging, and in-flight duration."""

    def test_in_flight_duration_calculation(self):
        """Verify that an in-flight step or stage calculates active elapsed duration."""
        import time
        tracker = PipelineMetricsTracker("active-test")
        tracker.start_pipeline()
        tracker.start_stage("Analysis")
        step = tracker.start_step("generate_migration_report", stage="Analysis")

        time.sleep(0.05)

        # In-flight duration without finalize_running
        self.assertEqual(step.duration, 0.0)
        self.assertGreater(step.get_duration(finalize_running=True), 0.04)

        # Stage and tracker total duration
        stage = tracker.stages["Analysis"]
        self.assertGreater(stage.get_duration(finalize_running=True), 0.04)
        self.assertGreater(tracker.get_total_duration(finalize_running=True), 0.04)

        # Formatted markdown table should NOT show 0.00s for active stage/step
        md_table = tracker.format_markdown_table(finalize_running=True)
        self.assertIn("Stage: Analysis", md_table)
        self.assertIn("generate_migration_report", md_table)
        self.assertNotIn("0.00s", md_table)

        # ASCII summary table
        summary_table = tracker.format_summary_table(finalize_running=True)
        self.assertIn("[STAGE] Analysis", summary_table)
        self.assertIn("generate_migration_report", summary_table)
        self.assertNotIn("0.00s", summary_table)

    def test_serialization_and_file_round_trip(self):
        """Test to_dict, from_dict, save_to_file, load_from_file."""
        tracker = PipelineMetricsTracker("roundtrip-test", multi_repo=True)
        tracker.start_pipeline()
        tracker.start_stage("Data Generation")
        with tracker.track_app("service-one", git_branch="main"):
            with tracker.track_step("prepare_environment", stage="Data Generation", app="service-one"):
                pass
        tracker.stop_stage("Data Generation", status="COMPLETED")

        tracker.start_stage("Indexing")
        with tracker.track_step("generate_graphrag_index", stage="Indexing"):
            pass
        tracker.stop_stage("Indexing", status="COMPLETED")
        tracker.stop_pipeline(status="COMPLETED")

        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = os.path.join(tmpdir, "pipeline_metrics.json")
            tracker.save_to_file(filepath)
            self.assertTrue(os.path.exists(filepath))

            loaded = PipelineMetricsTracker.load_from_file(filepath)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.pipeline_name, "roundtrip-test")
            self.assertTrue(loaded.multi_repo)
            self.assertIn("Data Generation", loaded.stages)
            self.assertIn("Indexing", loaded.stages)
            self.assertIn("service-one", loaded.apps)
            self.assertEqual(len(loaded.steps), 2)

    def test_cross_stage_simulated_workflow(self):
        """Simulate single-repo 3-stage Kubeflow flow across distinct processes/directories."""
        import asyncio
        from utils.graphrag_utils import DependencyAnalyzer

        with tempfile.TemporaryDirectory() as stage1_dir, \
             tempfile.TemporaryDirectory() as stage2_dir, \
             tempfile.TemporaryDirectory() as stage3_dir:

            # Stage 1: Data Generation container
            t1 = PipelineMetricsTracker.load_or_create([stage1_dir], pipeline_name="single-repo-pipeline")
            t1.start_stage("Data Generation")
            with t1.track_step("prepare_environment", stage="Data Generation"):
                pass
            with t1.track_step("generate_code_and_meta", stage="Data Generation"):
                pass
            t1.stop_stage("Data Generation", status="COMPLETED")
            t1.save_to_file(os.path.join(stage1_dir, "pipeline_metrics.json"))

            # Stage 2: Indexing container (reads stage1_dir, outputs to stage2_dir)
            t2 = PipelineMetricsTracker.load_or_create([stage1_dir], pipeline_name="single-repo-pipeline")
            self.assertIn("Data Generation", t2.stages)
            t2.start_stage("Indexing")
            with t2.track_step("generate_graphrag_index", stage="Indexing"):
                pass
            with t2.track_step("evaluate_graphrag_index", stage="Indexing"):
                pass
            t2.stop_stage("Indexing", status="COMPLETED")
            t2.save_to_file(os.path.join(stage2_dir, "pipeline_metrics.json"))

            # Stage 3: Analysis container (reads stage2_dir)
            t3 = PipelineMetricsTracker.load_or_create([stage2_dir], pipeline_name="single-repo-pipeline")
            self.assertIn("Data Generation", t3.stages)
            self.assertIn("Indexing", t3.stages)
            t3.start_stage("Analysis")

            # Run DependencyAnalyzer inside Analysis stage
            with t3.track_step("generate_migration_report", stage="Analysis"):
                with patch.object(DependencyAnalyzer, '_setup_configuration'), \
                     patch.object(DependencyAnalyzer, '_setup_search'), \
                     patch.object(DependencyAnalyzer, '_setup_prompts'), \
                     patch.object(DependencyAnalyzer, '_extract_indexed_git_urls', return_value={"https://github.com/org/repo1"}), \
                     patch('utils.visualization_utils.log_interactive_dependency_graph'):

                    analyzer = DependencyAnalyzer(stage2_dir, metrics_tracker=t3)

                    mock_loader = MagicMock()
                    mock_loader.num_prompts.side_effect = lambda path: 1 if "enhanced" in path else 1
                    mock_loader.download_prompt.side_effect = [
                        ("prompt 1", {"title": "### System Architecture Summary", "skip_prompt": None}),
                        ("prompt 2", {"title": "### Recommended Migration Order", "skip_prompt": None}),
                    ]

                    orig_loader = DefaultAssetLoaderMock.return_value
                    DefaultAssetLoaderMock.return_value = mock_loader
                    try:
                        with patch.object(analyzer, 'query_with_llm', side_effect=["Arch summary.", "Migration order."]):
                            report = asyncio.run(analyzer.generate_migration_report())
                    finally:
                        DefaultAssetLoaderMock.return_value = orig_loader

            # Final check on report content
            self.assertIn("### Pipeline Execution Metrics Summary", report)
            self.assertIn("[STAGE] Data Generation", report)
            self.assertIn("prepare_environment", report)
            self.assertIn("generate_code_and_meta", report)
            self.assertIn("[STAGE] Indexing", report)
            self.assertIn("generate_graphrag_index", report)
            self.assertIn("evaluate_graphrag_index", report)
            self.assertIn("[STAGE] Analysis", report)
            self.assertIn("generate_migration_report", report)

            # Check that generate_migration_report step is present and status is COMPLETED
            lines = report.splitlines()
            analysis_step_line = [l for l in lines if "generate_migration_report" in l]
            self.assertTrue(len(analysis_step_line) > 0)
            self.assertIn("COMPLETED", analysis_step_line[0])


if __name__ == "__main__":
    unittest.main()


