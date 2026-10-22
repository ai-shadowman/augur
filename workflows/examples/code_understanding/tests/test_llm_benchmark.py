import ast
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

if "pygments" not in sys.modules:
    pygments = types.ModuleType("pygments")
    lexers = types.ModuleType("pygments.lexers")
    lexers.guess_lexer_for_filename = lambda *_: None
    util = types.ModuleType("pygments.util")
    util.ClassNotFound = type("ClassNotFound", (Exception,), {})
    sys.modules.update({"pygments": pygments, "pygments.lexers": lexers, "pygments.util": util})

if "pandas" not in sys.modules:
    pandas = MagicMock()
    pandas.__path__ = []
    sys.modules["pandas"] = pandas

if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None
    sys.modules["dotenv"] = dotenv

if "graphrag" not in sys.modules:
    graphrag_mod = types.ModuleType("graphrag")
    graphrag_api = types.ModuleType("graphrag.api")
    graphrag_config = types.ModuleType("graphrag.config")
    graphrag_load_config = types.ModuleType("graphrag.config.load_config")
    graphrag_load_config.load_config = MagicMock()
    graphrag_mod.api = graphrag_api
    sys.modules.update({
        "graphrag": graphrag_mod,
        "graphrag.api": graphrag_api,
        "graphrag.config": graphrag_config,
        "graphrag.config.load_config": graphrag_load_config,
    })

if "loaders.default_asset_loader" not in sys.modules:
    loader_module = types.ModuleType("loaders.default_asset_loader")

    class _DefaultAssetLoader:
        RESULTS_PATH_PREFIX_PIPELINES = "results/pipelines"
        RESULTS_PATH_PREFIX_VISUALIZATIONS = "results/visualizations"

        def download(self, *args, **kwargs):
            return {}

        def download_dir(self, *args, **kwargs):
            return None

        def log_results(self, *args, **kwargs):
            return None

        def num_prompts(self, *args, **kwargs):
            return 0

        def download_prompt(self, *args, **kwargs):
            return "", {}

        @staticmethod
        def get_log_results_artifact_path(*args, **kwargs):
            return "test"

    loader_module.DefaultAssetLoader = _DefaultAssetLoader
    sys.modules["loaders.default_asset_loader"] = loader_module
    import loaders
    loaders.default_asset_loader = loader_module

from utils.llm_benchmark import (
    BENCHMARK_RESULT_FILENAME,
    apply_best_concurrency,
    format_benchmark_report,
    is_benchmark_enabled,
    is_benchmark_skipped,
    load_benchmark_result,
    main as benchmark_cli_main,
    parse_concurrency_levels,
    parse_guidellm_json,
    resolve_llm_endpoint,
    run_guidellm_benchmark,
    save_benchmark_result,
    select_best_concurrency,
)


class TestGuideLLMBenchmarkUtils(unittest.TestCase):
    def test_parse_concurrency_levels(self):
        self.assertEqual(parse_concurrency_levels("1, 2, 4, 8, 16"), [1, 2, 4, 8, 16])
        self.assertEqual(parse_concurrency_levels("16;4;8;4"), [4, 8, 16])
        self.assertEqual(parse_concurrency_levels("invalid"), [1, 2, 4, 8, 16, 32])

    def test_resolve_llm_endpoint_from_env_and_overrides(self):
        with patch.dict(
            os.environ,
            {
                "GRAPHRAG_LLM_API_BASE": "https://vllm.apps.cluster/v1/",
                "GRAPHRAG_LLM_ID": "meta-llama/Llama-3.1-70B",
                "GRAPHRAG_LLM_TOKEN": "secret-token",
            },
            clear=False,
        ):
            ep = resolve_llm_endpoint()
            self.assertEqual(ep["api_base"], "https://vllm.apps.cluster/v1")
            self.assertEqual(ep["model"], "meta-llama/Llama-3.1-70B")
            self.assertEqual(ep["api_key"], "secret-token")

            override_ep = resolve_llm_endpoint(
                api_base="http://localhost:8000/v1/",
                model="custom-model",
                api_key="override-key",
            )
            self.assertEqual(override_ep["api_base"], "http://localhost:8000/v1")
            self.assertEqual(override_ep["model"], "custom-model")
            self.assertEqual(override_ep["api_key"], "override-key")

    def test_parse_guidellm_json_modern_and_legacy_schemas(self):
        modern_payload = {
            "benchmarks": [
                {
                    "args": {"profile": {"streams": [4]}},
                    "metrics": {
                        "output_tokens_per_second": {"total": {"mean": 145.5}},
                        "requests_per_second": {"total": {"mean": 3.2}},
                        "request_latency": {"total": {"mean": 1.25}},
                    },
                    "request_totals": {"successful": 16, "errored": 0, "total": 16},
                },
                {
                    "args": {"profile": {"streams": [8]}},
                    "metrics": {
                        "output_tokens_per_second": {"total": {"mean": 280.0}},
                        "requests_per_second": {"total": {"mean": 6.1}},
                        "request_latency": {"total": {"mean": 1.31}},
                    },
                    "request_totals": {"successful": 16, "errored": 0, "total": 16},
                },
            ]
        }
        parsed = parse_guidellm_json(modern_payload)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["concurrency"], 4)
        self.assertAlmostEqual(parsed[0]["output_tokens_per_second"], 145.5)
        self.assertEqual(parsed[1]["concurrency"], 8)
        self.assertAlmostEqual(parsed[1]["output_tokens_per_second"], 280.0)

        legacy_payload = [
            {
                "rate": 16,
                "output_token_throughput": 310.0,
                "request_throughput": 7.0,
                "mean_latency": 2.1,
                "completed_requests": 15,
                "failed_requests": 1,
            }
        ]
        parsed_legacy = parse_guidellm_json(legacy_payload)
        self.assertEqual(len(parsed_legacy), 1)
        self.assertEqual(parsed_legacy[0]["concurrency"], 16)
        self.assertAlmostEqual(parsed_legacy[0]["output_tokens_per_second"], 310.0)
        self.assertEqual(parsed_legacy[0]["successful_requests"], 15)
        self.assertEqual(parsed_legacy[0]["failed_requests"], 1)

    def test_select_best_concurrency_prefers_lowest_concurrency_near_peak(self):
        levels = [
            {
                "concurrency": 1,
                "output_tokens_per_second": 50.0,
                "requests_per_second": 1.0,
                "latency_mean_s": 1.0,
                "successful_requests": 16,
                "failed_requests": 0,
                "error_rate": 0.0,
            },
            {
                "concurrency": 4,
                "output_tokens_per_second": 180.0,
                "requests_per_second": 3.8,
                "latency_mean_s": 1.1,
                "successful_requests": 16,
                "failed_requests": 0,
                "error_rate": 0.0,
            },
            {
                "concurrency": 8,
                "output_tokens_per_second": 300.0,
                "requests_per_second": 6.2,
                "latency_mean_s": 1.3,
                "successful_requests": 16,
                "failed_requests": 0,
                "error_rate": 0.0,
            },
            {
                # Within 5% of 305.0 peak, so concurrency=8 should be chosen over 16 to avoid saturating queue
                "concurrency": 16,
                "output_tokens_per_second": 305.0,
                "requests_per_second": 6.3,
                "latency_mean_s": 2.5,
                "successful_requests": 16,
                "failed_requests": 0,
                "error_rate": 0.0,
            },
            {
                # High error rate (>10%), must be filtered out even if throughput is higher
                "concurrency": 32,
                "output_tokens_per_second": 400.0,
                "requests_per_second": 8.0,
                "latency_mean_s": 4.0,
                "successful_requests": 10,
                "failed_requests": 6,
                "error_rate": 6 / 16,
            },
        ]
        self.assertEqual(select_best_concurrency(levels), 8)

    def test_apply_and_persist_benchmark_result(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {}, clear=False):
            applied = apply_best_concurrency(12)
            self.assertEqual(applied, 12)
            self.assertEqual(os.environ["SDG_LLM_MAX_CONCURRENCY"], "12")
            self.assertEqual(os.environ["GRAPHRAG_CONCURRENT_REQUESTS"], "12")

            payload = {
                "best_concurrency": 12,
                "api_base": "https://vllm.example.com/v1",
                "model": "qwen-coder",
                "runner": "guidellm_cli",
                "skipped": False,
                "levels": [
                    {
                        "concurrency": 12,
                        "output_tokens_per_second": 220.0,
                        "requests_per_second": 4.5,
                        "latency_mean_s": 1.2,
                        "successful_requests": 16,
                        "failed_requests": 0,
                        "error_rate": 0.0,
                    }
                ],
                "duration_seconds": 14.2,
            }
            saved_path = save_benchmark_result(payload, tmpdir)
            self.assertTrue(os.path.isfile(saved_path))
            loaded = load_benchmark_result(tmpdir)
            self.assertEqual(loaded["best_concurrency"], 12)

            report = format_benchmark_report(payload)
            self.assertIn("Recommended Concurrency : 12", report)
            self.assertIn("SDG_LLM_MAX_CONCURRENCY=12", report)
            self.assertIn("GRAPHRAG_CONCURRENT_REQUESTS=12", report)

    def test_run_guidellm_benchmark_respects_skip_env(self):
        with patch.dict(
            os.environ,
            {
                "RUN_LLM_BENCHMARK": "false",
                "SDG_LLM_MAX_CONCURRENCY": "14",
                "GRAPHRAG_LLM_API_BASE": "https://vllm.example.com/v1",
                "GRAPHRAG_LLM_ID": "test-model",
            },
            clear=False,
        ):
            self.assertFalse(is_benchmark_enabled())
            self.assertTrue(is_benchmark_skipped())
            res = run_guidellm_benchmark(apply_env=True)
            self.assertTrue(res["skipped"])
            self.assertEqual(res["best_concurrency"], 14)
            self.assertEqual(os.environ["GRAPHRAG_CONCURRENT_REQUESTS"], "14")

        with patch.dict(os.environ, {"RUN_LLM_BENCHMARK": "true"}, clear=False):
            self.assertTrue(is_benchmark_enabled())
            self.assertFalse(is_benchmark_skipped())

    def test_cli_main_prints_recommended_concurrency_without_mutating_env_files(self):
        mock_result = {
            "best_concurrency": 16,
            "api_base": "https://vllm.example.com/v1",
            "model": "test-model",
            "runner": "guidellm_cli",
            "skipped": False,
            "levels": [
                {
                    "concurrency": 16,
                    "output_tokens_per_second": 250.0,
                    "requests_per_second": 5.0,
                    "latency_mean_s": 1.1,
                    "successful_requests": 16,
                    "failed_requests": 0,
                    "error_rate": 0.0,
                }
            ],
            "duration_seconds": 5.0,
        }
        with patch("utils.llm_benchmark.run_guidellm_benchmark", return_value=mock_result) as mock_run:
            rc = benchmark_cli_main(["--concurrency-levels", "8,16", "--max-seconds", "10"])
            self.assertEqual(rc, 0)
            mock_run.assert_called_once()
            # CLI should pass apply_env=False and respect_skip_env=False
            self.assertFalse(mock_run.call_args.kwargs["apply_env"])
            self.assertFalse(mock_run.call_args.kwargs["respect_skip_env"])


class TestPipelineIntegrationWithGuideLLM(unittest.TestCase):
    def test_base_data_generation_pipeline_runs_benchmark_prestep(self):
        from pipelines.base import data_generation

        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = os.path.join(tmpdir, "src")
            target_dir = os.path.join(tmpdir, "target")
            os.makedirs(source_dir, exist_ok=True)
            os.makedirs(target_dir, exist_ok=True)

            mock_bench_result = {
                "best_concurrency": 16,
                "api_base": "https://vllm.example.com/v1",
                "model": "test-model",
                "runner": "guidellm_cli",
                "skipped": False,
                "levels": [],
                "duration_seconds": 3.5,
            }

            def fake_run_benchmark(apply_env=False, **kwargs):
                if apply_env:
                    apply_best_concurrency(16)
                return mock_bench_result

            with patch("utils.llm_benchmark.run_guidellm_benchmark", side_effect=fake_run_benchmark) as mock_bench, \
                 patch("pipelines.base.data_generation.prepare_environment"), \
                 patch("pipelines.base.data_generation.detect_languages", return_value=["python"]), \
                 patch("pipelines.base.data_generation.load_external_data", return_value={}), \
                 patch("pipelines.base.data_generation.generate_code_and_meta"), \
                 patch.dict(os.environ, {}, clear=False):
                res = data_generation.DataGenerationPipeline().run(
                    git_repo="https://github.com/example/repo",
                    git_branch="main",
                    source_path=source_dir,
                    target_path=target_dir,
                )
                self.assertEqual(res["status"], "complete")
                mock_bench.assert_called_once()
                self.assertEqual(os.environ["SDG_LLM_MAX_CONCURRENCY"], "16")
                self.assertEqual(os.environ["GRAPHRAG_CONCURRENT_REQUESTS"], "16")
                self.assertTrue(os.path.isfile(os.path.join(target_dir, BENCHMARK_RESULT_FILENAME)))

                from utils.duration_tracker import DurationTracker
                dur_tracker = DurationTracker.get_instance()
                step_names = [r["step"] for r in dur_tracker.records]
                self.assertIn("GuideLLM Concurrency Benchmark", step_names)
                self.assertEqual(step_names[0], "GuideLLM Concurrency Benchmark")

                md_section = dur_tracker.format_markdown_section()
                self.assertIn("GuideLLM Concurrency Benchmark", md_section)
                self.assertIn("## Pipeline Execution Duration Summary", md_section)

                md_table = dur_tracker.format_markdown_table()
                self.assertIn("GuideLLM Concurrency Benchmark", md_table)

    def test_duration_tracker_reconstructs_benchmark_step_and_sorts_first_in_report(self):
        from utils.duration_tracker import DurationTracker, extract_benchmark_duration

        with tempfile.TemporaryDirectory() as tmpdir:
            payload = {
                "best_concurrency": 16,
                "api_base": "https://vllm.example.com/v1",
                "model": "test-model",
                "runner": "guidellm_cli",
                "skipped": False,
                "levels": [],
                "duration_seconds": 18.4,
            }
            save_benchmark_result(payload, tmpdir)

            tracker = DurationTracker()
            tracker.record_step(stage="Data Generation", step="Prepare Environment", duration=4.2)
            tracker.record_step(stage="Indexing", step="GraphRAG Indexing", duration=120.0)
            tracker.record_step(stage="Analysis", step="Migration Report Generation", duration=15.0)

            extracted = extract_benchmark_duration([tmpdir], tracker)
            self.assertTrue(extracted)

            # Verify it is in tracker records with accurate duration
            bench_rec = next((r for r in tracker.records if r["step"] == "GuideLLM Concurrency Benchmark"), None)
            self.assertIsNotNone(bench_rec)
            self.assertAlmostEqual(bench_rec["duration"], 18.4, places=2)

            # Verify it appears as the very first row in both format_markdown_section() and format_markdown_table()
            md_section = tracker.format_markdown_section()
            self.assertIn("GuideLLM Concurrency Benchmark", md_section)
            self.assertIn("18.40s", md_section)
            bench_idx = md_section.index("GuideLLM Concurrency Benchmark")
            prep_idx = md_section.index("Prepare Environment")
            self.assertLess(bench_idx, prep_idx)

            md_table = tracker.format_markdown_table()
            self.assertIn("| **Data Generation** | GuideLLM Concurrency Benchmark | 18.40s |", md_table)
            self.assertLess(md_table.index("GuideLLM Concurrency Benchmark"), md_table.index("Prepare Environment"))

            ascii_summary = tracker.format_summary()
            self.assertIn("GuideLLM Concurrency Benchmark", ascii_summary)
            self.assertLess(ascii_summary.index("GuideLLM Concurrency Benchmark"), ascii_summary.index("Prepare Environment"))

    def test_graphrag_prepare_settings_uses_benchmarked_concurrency(self):
        from utils.graphrag_utils import DependencyAnalyzer

        with tempfile.TemporaryDirectory() as tmpdir:
            # Copy actual settings.yaml.in into tmpdir
            src_template = Path(BASE_DIR) / "assets" / "graphrag" / "settings.yaml.in"
            dst_template = Path(tmpdir) / "settings.yaml.in"
            dst_template.write_text(src_template.read_text(encoding="utf-8"), encoding="utf-8")

            mock_loader = MagicMock()
            with patch("loaders.default_asset_loader.DefaultAssetLoader", return_value=mock_loader), \
                 patch.dict(
                     os.environ,
                     {
                         "GRAPHRAG_LLM_API_BASE": "https://vllm.example.com/v1",
                         "GRAPHRAG_LLM_TOKEN": "tok",
                         "GRAPHRAG_LLM_ID": "chat-model",
                         "GRAPHRAG_LLM_PROVIDER_SETTINGS_XML": "openai",
                         "EMBED_LLM_API_BASE": "https://embed.example.com/v1",
                         "EMBED_LLM_TOKEN": "tok",
                         "EMBED_LLM_ID": "embed-model",
                         "EMBED_LLM_PROVIDER_SETTINGS_XML": "openai",
                         "SDG_LLM_MAX_CONCURRENCY": "16",
                     },
                     clear=False,
                 ):
                os.environ.pop("GRAPHRAG_CONCURRENT_REQUESTS", None)
                DependencyAnalyzer.prepare_settings(template_dir=tmpdir, output_dir=tmpdir)
                rendered = (Path(tmpdir) / "settings.yaml").read_text(encoding="utf-8")
                self.assertIn("concurrent_requests: 16", rendered)
                self.assertIn("max_tokens: 8192", rendered)

                # Also verify stale MLflow template with hardcoded concurrent_requests: 25 is overridden
                stale_template = src_template.read_text(encoding="utf-8").replace(
                    "concurrent_requests: ${GRAPHRAG_CONCURRENT_REQUESTS}",
                    "concurrent_requests: 25",
                ).replace("    max_tokens: 8192\n", "")
                dst_template.write_text(stale_template, encoding="utf-8")
                os.environ["GRAPHRAG_CONCURRENT_REQUESTS"] = "20"
                DependencyAnalyzer.prepare_settings(template_dir=tmpdir, output_dir=tmpdir)
                rendered_stale = (Path(tmpdir) / "settings.yaml").read_text(encoding="utf-8")
                self.assertIn("concurrent_requests: 20", rendered_stale)
                self.assertNotIn("concurrent_requests: 25", rendered_stale)
                self.assertIn("max_tokens: 8192", rendered_stale)

    def test_kubeflow_data_generation_pipeline_wires_benchmark_llm_op(self):
        kfp_dg_path = Path(BASE_DIR) / "pipelines" / "kubeflow" / "data_generation.py"
        source = kfp_dg_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        func_names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        self.assertIn("benchmark_llm_op", func_names)
        self.assertIn("_run_pipeline", func_names)
        self.assertIn("_run_pipeline_multi_repo", func_names)
        self.assertIn("prep.after(bench)", source)
        self.assertIn("max_concurrency=bench.output", source)

    def test_generate_raw_dataset_excludes_telemetry_json_files_in_config_mode(self):
        from pipelines.base import data_generation

        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "source"
            tgt = Path(tmpdir) / "target"
            src.mkdir()
            tgt.mkdir()

            for telemetry_name in ("durations.json", "tokens.json", "project_metrics.json", "benchmark_results.json"):
                (src / telemetry_name).write_text('{"test": 1}', encoding="utf-8")
            (src / "app_config.json").write_text('{"port": 8080}', encoding="utf-8")

            with patch("utils.code_utils.get_exclude_dirs_for_language", return_value=set()), \
                 patch("utils.code_utils.get_config_file_extensions_for_language", return_value=[".json"]), \
                 patch("utils.code_utils.is_large_code_file", return_value=False), \
                 patch("utils.code_utils.generate_slug_from_repo", return_value="org-repo-main"), \
                 patch("pandas.DataFrame", side_effect=lambda records: records):
                records = data_generation.generate_raw_dataset(
                    str(src),
                    str(tgt),
                    "https://github.com/org/repo.git",
                    "main",
                    language="java",
                    config=True,
                )

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["file_path"], "app_config.json")


if __name__ == "__main__":
    unittest.main()

