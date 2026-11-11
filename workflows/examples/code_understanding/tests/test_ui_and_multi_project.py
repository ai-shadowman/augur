import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_TESTS_DIR = Path(__file__).resolve().parent
_CODE_UNDERSTANDING_DIR = _TESTS_DIR.parent
_REPO_ROOT = _CODE_UNDERSTANDING_DIR.parents[2]

for _p in (str(_CODE_UNDERSTANDING_DIR), str(_REPO_ROOT), str(_REPO_ROOT / "ui")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Provide lightweight module stubs when running in a minimal Python environment without mlflow/requests
if "requests" not in sys.modules:
    sys.modules["requests"] = mock.MagicMock()

if "mlflow" not in sys.modules:
    _mlflow_mod = mock.MagicMock()
    sys.modules["mlflow"] = _mlflow_mod
    sys.modules["mlflow.tracking"] = _mlflow_mod.tracking

from utils.pipeline_utils import (
    generate_multi_repo_slug,
    get_pipeline_run_id,
    resolve_mlflow_prefix,
    sanitize_slug,
)


class TestMultiProjectAndWorkspaceUtils(unittest.TestCase):
    def test_sanitize_slug(self):
        self.assertEqual(sanitize_slug("Billing Modernization!"), "billing-modernization")
        self.assertEqual(sanitize_slug("team_a/sub-proj"), "team_a-sub-proj")
        self.assertEqual(sanitize_slug(""), "")
        self.assertEqual(sanitize_slug(None), "")

    def test_resolve_mlflow_prefix_default_workspace(self):
        with mock.patch.dict(
            os.environ,
            {"MLFLOW_NAMESPACE": "ns-demo", "PROJECT_WORKSPACE": ""},
            clear=False,
        ):
            self.assertEqual(resolve_mlflow_prefix(), "ns-demo")
            self.assertEqual(resolve_mlflow_prefix(workspace="default"), "ns-demo")

    def test_resolve_mlflow_prefix_named_workspace(self):
        with mock.patch.dict(
            os.environ,
            {"MLFLOW_NAMESPACE": "ns-demo", "PROJECT_WORKSPACE": "payments"},
            clear=False,
        ):
            self.assertEqual(resolve_mlflow_prefix(), "ns-demo/payments")
            self.assertEqual(
                resolve_mlflow_prefix(namespace="ns-override", workspace="Core Banking"),
                "ns-override/core-banking",
            )

    def test_generate_multi_repo_slug_is_deterministic_and_distinct(self):
        repos_a = [
            {"git_repo": "https://github.com/org/service-b.git", "git_branch": "main"},
            {"git_repo": "https://github.com/org/service-a.git", "git_branch": "main"},
        ]
        repos_a_reordered = list(reversed(repos_a))
        repos_b = [
            {"git_repo": "https://github.com/org/service-c.git", "git_branch": "main"},
            {"git_repo": "https://github.com/org/service-d.git", "git_branch": "main"},
        ]
        slug_a1 = generate_multi_repo_slug(repos_a)
        slug_a2 = generate_multi_repo_slug(repos_a_reordered)
        slug_b = generate_multi_repo_slug(repos_b)

        self.assertEqual(slug_a1, slug_a2)
        self.assertNotEqual(slug_a1, slug_b)
        self.assertIn("service-a", slug_a1)
        self.assertIn("service-b", slug_a1)

    def test_get_pipeline_run_id_respects_allow_generate(self):
        env_clean = {
            k: v
            for k, v in os.environ.items()
            if k not in ("PIPELINE_RUN_ID", "_AGENTMESH_PIPELINE_RUN_ID")
        }
        with mock.patch.dict(os.environ, env_clean, clear=True):
            with mock.patch("utils.kubeflow_utils.get_kfp_run_id", return_value=None):
                self.assertIsNone(get_pipeline_run_id(allow_generate=False))
                generated = get_pipeline_run_id(allow_generate=True)
                self.assertIsNotNone(generated)
                # Subsequent calls without allow_generate still see the cached env var
                self.assertEqual(get_pipeline_run_id(allow_generate=False), generated)


class TestConcurrentAdhocQueryIsolation(unittest.TestCase):
    def test_concurrent_adhoc_queries_use_isolated_temp_dirs(self):
        import pipelines.base.analysis as base_analysis

        observed_dirs = []

        class FakeAnalyzer:
            def __init__(self, graphrag_source_path, git_slug="", multi_repo=False):
                self.graphrag_source_path = graphrag_source_path
                self.git_slug = git_slug
                self.multi_repo = multi_repo
                self.token_tracker = mock.MagicMock()

            @staticmethod
            def download_graphrag_directory(download_dir, git_slug=None, multi_repo=False, **kwargs):
                observed_dirs.append((download_dir, git_slug, multi_repo))

            async def query_with_llm(self, prompt, retry_count=3, use_global=True, response_type=""):
                self.assertTrue_dir_exists = os.path.isdir(self.graphrag_source_path)
                return f"answer for {self.git_slug}"

        fake_graphrag_utils = types.ModuleType("utils.graphrag_utils")
        fake_graphrag_utils.DependencyAnalyzer = FakeAnalyzer
        with mock.patch.dict(sys.modules, {"utils.graphrag_utils": fake_graphrag_utils}):
            with mock.patch("loaders.default_asset_loader.DefaultAssetLoader"):
                res1 = base_analysis.run_adhoc_query_pipeline(
                    question="Q1",
                    multi_repo=True,
                    project_slug="proj-alpha",
                )
                res2 = base_analysis.run_adhoc_query_pipeline(
                    question="Q2",
                    multi_repo=True,
                    project_slug="proj-beta",
                )

        self.assertIn("answer for proj-alpha", res1)
        self.assertIn("answer for proj-beta", res2)
        self.assertEqual(len(observed_dirs), 2)
        self.assertNotEqual(observed_dirs[0][0], observed_dirs[1][0])
        self.assertEqual(observed_dirs[0][1], "proj-alpha")
        self.assertEqual(observed_dirs[1][1], "proj-beta")
        # Verify cleanup after completion
        self.assertFalse(os.path.exists(observed_dirs[0][0]))
        self.assertFalse(os.path.exists(observed_dirs[1][0]))


class TestServicesMultiProjectIsolation(unittest.TestCase):
    def test_submit_pipeline_run_generates_deterministic_project_slug_and_workspace_experiment(self):
        import services.trigger_run as trigger_run_mod
        import services.update_repos as update_repos_mod
        from api.pipeline_service import submit_pipeline_run

        fake_run = mock.MagicMock()
        fake_run.run_id = "kfp-run-999"
        fake_client = mock.MagicMock()
        fake_pipeline = mock.MagicMock(pipeline_id="pipe-multi-id")
        fake_client.list_pipelines.return_value = mock.MagicMock(pipelines=[fake_pipeline])
        fake_version = mock.MagicMock(pipeline_version_id="ver-1", created_at="2026-01-01")
        fake_client.list_pipeline_versions.return_value = mock.MagicMock(
            total_size=1, pipeline_versions=[fake_version]
        )
        fake_client.create_experiment.return_value = mock.MagicMock(experiment_id="exp-ws-id")
        fake_client.run_pipeline.return_value = fake_run

        with mock.patch.object(trigger_run_mod, "create_client", return_value=fake_client):
            with mock.patch.object(update_repos_mod, "log_repos", return_value="logged-run-123") as mock_log:
                res = submit_pipeline_run(
                    repos=[
                        {"git_repo": "https://github.com/org/repo-two", "git_branch": "main"},
                        {"git_repo": "https://github.com/org/repo-one", "git_branch": "main"},
                    ],
                    namespace="test-ns",
                    workspace="Team Alpha",
                )

        self.assertEqual(res["job_id"], "kfp-run-999")
        self.assertEqual(res["project_workspace"], "team-alpha")
        self.assertIn("repo-one-main", res["project_slug"])
        self.assertIn("repo-two-main", res["project_slug"])

        fake_client.create_experiment.assert_called_once_with(name="workspace-team-alpha")
        call_kwargs = fake_client.run_pipeline.call_args.kwargs
        self.assertEqual(
            call_kwargs["params"]["project_workspace"],
            "team-alpha",
        )
        self.assertEqual(
            call_kwargs["params"]["project_slug"],
            res["project_slug"],
        )
        mock_log.assert_called_once()

    def test_list_kfp_runs_filters_by_workspace_and_extracts_project_slug(self):
        import services.list_runs as list_runs_mod

        param_a1 = mock.MagicMock()
        param_a1.name = "project_workspace"
        param_a1.value = "team-alpha"
        param_a2 = mock.MagicMock()
        param_a2.name = "project_slug"
        param_a2.value = "billing-modernization"

        run_a = mock.MagicMock()
        run_a.run_id = "run-a"
        run_a.display_name = "multi_repo_billing-modernization_20260101"
        run_a.state = "SUCCEEDED"
        run_a.created_at = None
        run_a.finished_at = None
        run_a.runtime_config = mock.MagicMock(
            parameters={
                "project_workspace": "team-alpha",
                "project_slug": "billing-modernization",
            }
        )

        run_b = mock.MagicMock()
        run_b.run_id = "run-b"
        run_b.display_name = "multi_repo_other_20260101"
        run_b.state = "SUCCEEDED"
        run_b.created_at = None
        run_b.finished_at = None
        run_b.runtime_config = mock.MagicMock(
            parameters={
                "project_workspace": "team-beta",
                "project_slug": "other-proj",
            }
        )

        fake_client = mock.MagicMock()
        fake_client.get_experiment.return_value = mock.MagicMock(experiment_id="exp-123")
        fake_client.list_runs.return_value = mock.MagicMock(runs=[run_a, run_b])

        with mock.patch.object(list_runs_mod, "create_client", return_value=fake_client):
            runs_alpha = list_runs_mod.list_kfp_runs(
                limit=10, namespace="test-ns", workspace="team-alpha"
            )
            runs_beta = list_runs_mod.list_kfp_runs(
                limit=10, namespace="test-ns", workspace="team-beta"
            )

        self.assertEqual(len(runs_alpha), 1)
        self.assertEqual(runs_alpha[0]["run_id"], "run-a")
        self.assertEqual(runs_alpha[0]["project_slug"], "billing-modernization")
        self.assertEqual(runs_alpha[0]["project_workspace"], "team-alpha")

        self.assertEqual(len(runs_beta), 1)
        self.assertEqual(runs_beta[0]["run_id"], "run-b")
        self.assertEqual(runs_beta[0]["project_slug"], "other-proj")
        self.assertEqual(runs_beta[0]["project_workspace"], "team-beta")

    def test_fetch_reports_passes_project_slug_and_workspace(self):
        import services.fetch_reports as fetch_reports_mod

        fake_loader = mock.MagicMock()
        fake_loader.download_dir.return_value = None

        with mock.patch.object(fetch_reports_mod, "DefaultAssetLoader", return_value=fake_loader) as mock_cls:
            mig, ev, slug = fetch_reports_mod.fetch_reports(
                multi_repo=True,
                namespace="ns-1",
                workspace="ws-1",
                project_slug="my-multi-slug",
                pipeline_run_id="run-777",
            )

        self.assertEqual(slug, "my-multi-slug")
        mock_cls.assert_called_once_with(namespace="ns-1", workspace="ws-1")

    def test_mlflow_asset_loader_download_candidates_and_catalog_fallback(self):
        from loaders.mlflow_asset_loader import MlFlowAssetLoader

        loader_ws = MlFlowAssetLoader(namespace="ns-1", workspace="ws-1")
        self.assertEqual(loader_ws.namespace, "ns-1")
        self.assertEqual(loader_ws._namespace, "ns-1")
        self.assertEqual(loader_ws.workspace, "ws-1")
        self.assertEqual(loader_ws._workspace, "ws-1")
        self.assertEqual(
            loader_ws.STATIC_ASSET_EXPERIMENT,
            "ns-1/ws-1/code-refactoring/assets/static",
        )
        self.assertEqual(
            loader_ws.DEFAULT_STATIC_ASSET_EXPERIMENT,
            "ns-1/code-refactoring/assets/static",
        )
        candidates = loader_ws._get_download_candidate_metadata(
            "repos/repo_list.json", None, None, None
        )
        self.assertEqual(len(candidates), 3)
        self.assertEqual(candidates[1][1], "ns-1/ws-1/code-refactoring/assets/static")
        self.assertEqual(candidates[2][1], "ns-1/code-refactoring/assets/static")

        with mock.patch.object(loader_ws, "download", return_value=None):
            repos = loader_ws.load_catalog_repos()
            self.assertIsInstance(repos, list)

    def test_list_indexed_repos_separates_multi_repo_projects_by_slug(self):
        from ui.indexes import list_indexed_repos, queryable_options

        run1 = mock.MagicMock()
        run1.info.run_id = "r1"
        run1.info.start_time = 2000
        run1.info.artifact_uri = "s3://bucket/r1"
        run1.data.tags = {
            "category": "indexing",
            "multi_repo": "True",
            "git_slug": "project-one",
            "project_slug": "project-one",
        }

        run2 = mock.MagicMock()
        run2.info.run_id = "r2"
        run2.info.start_time = 1000
        run2.info.artifact_uri = "s3://bucket/r2"
        run2.data.tags = {
            "category": "indexing",
            "multi_repo": "True",
            "git_slug": "project-two",
            "project_slug": "project-two",
        }

        fake_exp = mock.MagicMock(experiment_id="exp-1", lifecycle_stage="active")
        fake_mlflow_client = mock.MagicMock()
        fake_mlflow_client.get_experiment_by_name.return_value = fake_exp
        fake_mlflow_client.search_runs.return_value = [run1, run2]

        fake_tracking_mod = sys.modules.get("mlflow.tracking") or mock.MagicMock()
        sys.modules["mlflow.tracking"] = fake_tracking_mod
        with mock.patch.object(fake_tracking_mod, "MlflowClient", return_value=fake_mlflow_client):
            with mock.patch("ui.indexes._load_configured_repos", return_value=[]):
                with mock.patch.dict(os.environ, {"ASSET_LOADER": "mlflow"}, clear=False):
                    indexes = list_indexed_repos(
                        namespace="demo-ns",
                        workspace="team-x",
                    )
                    queryable = queryable_options(indexes)

        fake_mlflow_client.get_experiment_by_name.assert_called_once_with(
            "demo-ns/team-x/code-refactoring/assets/result-directories"
        )
        self.assertEqual(len(indexes), 2)
        slugs = [idx["project_slug"] for idx in indexes]
        self.assertEqual(slugs, ["project-one", "project-two"])
        queryable_keys = [q["key"] for q in queryable]
        self.assertIn("__multi__:project-one", queryable_keys)
        self.assertIn("__multi__:project-two", queryable_keys)


class TestUIStaticAndTemplates(unittest.TestCase):
    def test_ui_index_html_has_workspace_selector_and_project_slug_input(self):
        html_path = _REPO_ROOT / "ui" / "static" / "index.html"
        content = html_path.read_text(encoding="utf-8")
        self.assertIn('id="ws-select"', content)
        self.assertIn('id="ws-list"', content)
        self.assertIn('id="project-slug-group"', content)
        self.assertIn('id="project-slug-input"', content)
        self.assertIn("/api/workspaces", content)
        self.assertIn("/api/workspace", content)
        self.assertIn("project_slug", content)
        self.assertIn("!isTableStart(i)", content)
        self.assertIn("renderMermaidError", content)
        self.assertIn("mermaid.render(", content)

    def test_ensure_mlflow_imported_binds_version(self):
        from loaders.mlflow_asset_loader import _ensure_mlflow_imported

        mlflow_mod, client_cls = _ensure_mlflow_imported()
        self.assertIsNotNone(mlflow_mod)
        self.assertTrue(hasattr(mlflow_mod, "version"))
        self.assertIsNotNone(client_cls)

    def test_console_plugin_and_helm_templates_exist(self):
        self.assertTrue((_REPO_ROOT / "console-plugin" / "Dockerfile").is_file())
        self.assertTrue((_REPO_ROOT / "console-plugin" / "package.json").is_file())
        self.assertTrue((_REPO_ROOT / "resources" / "helm" / "templates" / "console-app.yaml").is_file())
        self.assertTrue((_REPO_ROOT / "resources" / "helm" / "templates" / "console-plugin.yaml").is_file())
        self.assertTrue((_REPO_ROOT / "resources" / "helm" / "templates" / "_console-plugin.tpl").is_file())


class TestEvaluatorImprovements(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import test_token_tracker  # noqa: F401 - ensures stubs for mlflow/pandas/litellm
    def test_extract_json_from_string_strips_think_blocks(self):
        from utils.json_utils import extract_json_from_string

        raw = (
            "<think>Let me check {foo: bar} first...</think>\n"
            '{"faithfulness": 4, "answer_relevance": 5, "answer_similarity": 4, '
            '"answer_correctness": 4, "reasoning": "Accurate and complete"}'
        )
        parsed = extract_json_from_string(raw)
        self.assertIsInstance(parsed, dict)
        self.assertEqual(parsed["faithfulness"], 4)
        self.assertEqual(parsed["answer_relevance"], 5)

    def test_is_mlflow_tracking_request_scopes_to_tracking_uri(self):
        from eval.mlflow_custom_evaluator import _is_mlflow_tracking_request

        with mock.patch.dict(
            os.environ,
            {"MLFLOW_TRACKING_URI": "https://mlflow.redhat-ods-applications.svc.cluster.local:8443"},
            clear=False,
        ):
            self.assertTrue(
                _is_mlflow_tracking_request(
                    "https://mlflow.redhat-ods-applications.svc.cluster.local:8443/api/2.0/mlflow/runs/create"
                )
            )
            self.assertFalse(
                _is_mlflow_tracking_request("https://vllm-judge.apps.cluster.example.com/v1/chat/completions")
            )

    def test_judge_model_uri_sets_openai_api_base_and_base_url_and_tls(self):
        from tests import test_token_tracker  # noqa: F401 - ensures stubs for mlflow/pandas/litellm
        from eval.mlflow_custom_evaluator import MlFlowCustomEvaluator

        with mock.patch.dict(
            os.environ,
            {
                "JUDGE_LLM_ID": "gpt-oss-120b",
                "JUDGE_LLM_API_BASE": "https://judge.internal.svc:8443/v1",
                "JUDGE_LLM_TOKEN": "",
                "OPENAI_API_BASE": "https://stale-host/v1",
                "OPENAI_API_KEY": "",
                "ENABLE_TLS_VERIFICATION": "false",
            },
            clear=False,
        ):
            evaluator = MlFlowCustomEvaluator()
            uri = evaluator._judge_model_uri()
            self.assertEqual(uri, "openai:/gpt-oss-120b")
            self.assertEqual(os.environ.get("OPENAI_API_BASE"), "https://judge.internal.svc:8443/v1")
            self.assertEqual(os.environ.get("OPENAI_BASE_URL"), "https://judge.internal.svc:8443/v1")
            self.assertEqual(os.environ.get("OPENAI_API_KEY"), "EMPTY")
            self.assertEqual(os.environ.get("MLFLOW_TRACKING_INSECURE_TLS"), "true")

    def test_fallback_judge_populates_v1_scores_when_all_nan(self):
        from eval.basic_custom_evaluator import BasicCustomEvaluator
        from eval.mlflow_custom_evaluator import MlFlowCustomEvaluator
        from utils.eval_utils import aggregate_evaluation_scores

        evaluator = MlFlowCustomEvaluator()

        fake_score = {
            "faithfulness": 4.0,
            "answer_relevance": 5.0,
            "answer_similarity": 4.0,
            "answer_correctness": 4.0,
            "reasoning": "Accurate and well structured",
        }

        class _FakeDF:
            def __init__(self, rows):
                self._rows = [dict(r) for r in rows]
                self.index = list(range(len(self._rows)))
                self.empty = len(self._rows) == 0

            @property
            def columns(self):
                return list(self._rows[0].keys()) if self._rows else []

            def iterrows(self):
                return enumerate(self._rows)

            def __getitem__(self, col):
                return [r.get(col) for r in self._rows]

            def __setitem__(self, col, vals):
                for r, v in zip(self._rows, vals):
                    r[col] = v

        with mock.patch.object(BasicCustomEvaluator, "score_answer", return_value=fake_score) as mock_score:
            with mock.patch("eval.mlflow_custom_evaluator.pd.DataFrame", side_effect=lambda rows, index=None: _FakeDF(rows)):
                df = _FakeDF(
                    [
                        {
                            "inputs": "Q1",
                            "targets": "Ref1",
                            "predictions": "Ans1",
                            "faithfulness/v1/score": None,
                        }
                    ]
                )
                df = evaluator._apply_basic_judge_fallback_df(df)
                mock_score.assert_called_once_with(question="Q1", reference="Ref1", actual="Ans1")
                self.assertEqual(df["faithfulness/v1/score"], [4.0])
                self.assertEqual(df["answer_relevance/v1/score"], [5.0])
                self.assertEqual(df["answer_similarity/v1/score"], [4.0])
                self.assertEqual(df["answer_correctness/v1/score"], [4.0])
                self.assertEqual(df["faithfulness/v1/justification"], ["Accurate and well structured"])

        csv_with_fallback_scores = (
            "question,faithfulness/v1/score,faithfulness/v1/justification,"
            "answer_relevance/v1/score,answer_similarity/v1/score,answer_correctness/v1/score\n"
            "Q1,4.0,Good,5.0,4.0,4.0\n"
            "Q2,3.0,Fair,4.0,3.0,3.0\n"
        )
        agg = aggregate_evaluation_scores(csv_with_fallback_scores)
        self.assertIsNotNone(agg)
        by_name = {m["name"]: m for m in agg["metrics"]}
        self.assertEqual(by_name["faithfulness"]["average"], 3.5)
        self.assertEqual(by_name["faithfulness"]["pass_rate_3"], 100.0)
        self.assertEqual(by_name["faithfulness"]["pass_rate_4"], 50.0)
        self.assertEqual(by_name["answer relevance"]["average"], 4.5)


if __name__ == "__main__":
    unittest.main()
