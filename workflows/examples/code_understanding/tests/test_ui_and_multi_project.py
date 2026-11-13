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

# Provide lightweight module stubs when running in a minimal Python environment without mlflow/requests/fastapi
import importlib.util

if "requests" not in sys.modules:
    _req_mod = types.ModuleType("requests")

    class _StubSession:
        def send(self, request, **kwargs):
            return types.SimpleNamespace(status_code=200)

    _req_mod.Session = _StubSession
    sys.modules["requests"] = _req_mod

if "mlflow" not in sys.modules:
    _mlflow_mod = mock.MagicMock()
    sys.modules["mlflow"] = _mlflow_mod
    sys.modules["mlflow.tracking"] = _mlflow_mod.tracking

if importlib.util.find_spec("fastapi") is None and "fastapi" not in sys.modules:
    _fastapi = types.ModuleType("fastapi")
    _fastapi_cors = types.ModuleType("fastapi.middleware.cors")
    _fastapi_resp = types.ModuleType("fastapi.responses")
    _fastapi_static = types.ModuleType("fastapi.staticfiles")
    _pydantic = types.ModuleType("pydantic")
    _starlette = types.ModuleType("starlette")
    _starlette_mw = types.ModuleType("starlette.middleware")
    _starlette_base = types.ModuleType("starlette.middleware.base")

    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str = ""):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class Request:
        def __init__(self, cookies=None, headers=None, path="/", method="GET"):
            self.cookies = cookies or {}
            self.headers = {k.lower(): v for k, v in (headers or {}).items()}
            self.url = types.SimpleNamespace(path=path)
            self.method = method

    class Response:
        def __init__(self):
            self.headers = {}
            self.cookies = {}

        def set_cookie(self, key: str, value: str, **kwargs):
            self.cookies[key] = {"value": value, **kwargs}

    def Query(default=None, **kwargs):
        return default

    class APIRouter:
        def __init__(self, prefix: str = "", tags=None):
            self.prefix = prefix
            self.routes = []

        def get(self, path: str, **kwargs):
            def _dec(fn):
                self.routes.append(("GET", self.prefix + path, fn))
                return fn
            return _dec

        def post(self, path: str, **kwargs):
            def _dec(fn):
                self.routes.append(("POST", self.prefix + path, fn))
                return fn
            return _dec

    class FastAPI(APIRouter):
        def __init__(self, **kwargs):
            super().__init__()
            self.middlewares = []

        def add_middleware(self, cls, **kwargs):
            self.middlewares.append((cls, kwargs))

        def include_router(self, router):
            self.routes.extend(router.routes)

        def mount(self, *args, **kwargs):
            pass

    class FileResponse:
        def __init__(self, path, headers=None, **kwargs):
            self.path = path
            self.headers = dict(headers or {})

    class JSONResponse:
        def __init__(self, content, status_code: int = 200):
            self.content = content
            self.status_code = status_code
            self.headers = {}

    class _FieldSpec:
        def __init__(self, default=None, **kwargs):
            self.default = None if default is Ellipsis else default
            self.default_factory = kwargs.get("default_factory")

    def Field(default=None, **kwargs):
        return _FieldSpec(default=default, **kwargs)

    class BaseModel:
        def __init__(self, **kwargs):
            for k, v in type(self).__dict__.items():
                if k.startswith("_") or callable(v):
                    continue
                if isinstance(v, _FieldSpec):
                    setattr(self, k, v.default_factory() if v.default_factory else v.default)
                else:
                    setattr(self, k, v)
            for k, v in kwargs.items():
                setattr(self, k, v)

        def model_dump(self):
            out = {}
            for k, v in self.__dict__.items():
                if isinstance(v, BaseModel):
                    out[k] = v.model_dump()
                elif isinstance(v, list):
                    out[k] = [x.model_dump() if isinstance(x, BaseModel) else x for x in v]
                else:
                    out[k] = v
            return out

    class BaseHTTPMiddleware:
        def __init__(self, app=None):
            self.app = app

    _fastapi.HTTPException = HTTPException
    _fastapi.Request = Request
    _fastapi.Response = Response
    _fastapi.Query = Query
    _fastapi.APIRouter = APIRouter
    _fastapi.FastAPI = FastAPI
    _fastapi_cors.CORSMiddleware = object
    _fastapi_resp.FileResponse = FileResponse
    _fastapi_resp.JSONResponse = JSONResponse
    _fastapi_static.StaticFiles = lambda **kw: kw
    _pydantic.BaseModel = BaseModel
    _pydantic.Field = Field
    _starlette_base.BaseHTTPMiddleware = BaseHTTPMiddleware

    sys.modules.update(
        {
            "fastapi": _fastapi,
            "fastapi.middleware.cors": _fastapi_cors,
            "fastapi.responses": _fastapi_resp,
            "fastapi.staticfiles": _fastapi_static,
            "pydantic": _pydantic,
            "starlette": _starlette,
            "starlette.middleware": _starlette_mw,
            "starlette.middleware.base": _starlette_base,
        }
    )

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
            if k not in ("PIPELINE_RUN_ID", "_AGENTMESH_PIPELINE_RUN_ID", "AUGUR_SERVER_MODE")
        }
        with mock.patch.dict(os.environ, env_clean, clear=True):
            with mock.patch("utils.kubeflow_utils.get_kfp_run_id", return_value=None):
                self.assertIsNone(get_pipeline_run_id(allow_generate=False))
                generated = get_pipeline_run_id(allow_generate=True)
                self.assertIsNotNone(generated)
                # Subsequent calls without allow_generate still see the cached env var in CLI mode
                self.assertEqual(get_pipeline_run_id(allow_generate=False), generated)

    def test_get_pipeline_run_id_server_mode_does_not_mutate_environ(self):
        env_clean = {
            k: v
            for k, v in os.environ.items()
            if k not in ("PIPELINE_RUN_ID", "_AGENTMESH_PIPELINE_RUN_ID")
        }
        env_clean["AUGUR_SERVER_MODE"] = "1"
        with mock.patch.dict(os.environ, env_clean, clear=True):
            with mock.patch("utils.kubeflow_utils.get_kfp_run_id", return_value=None):
                r1 = get_pipeline_run_id(allow_generate=True)
                r2 = get_pipeline_run_id(allow_generate=True)
                self.assertNotEqual(r1, r2)
                self.assertNotIn("_AGENTMESH_PIPELINE_RUN_ID", os.environ)
                self.assertIsNone(get_pipeline_run_id(allow_generate=False))


class TestConcurrentAdhocQueryIsolation(unittest.TestCase):
    def test_concurrent_adhoc_queries_use_isolated_temp_dirs(self):
        import pipelines.base.analysis as base_analysis

        observed_dirs = []

        class FakeAnalyzer:
            def __init__(self, graphrag_source_path, git_slug="", multi_repo=False, token_tracker=None):
                self.graphrag_source_path = graphrag_source_path
                self.git_slug = git_slug
                self.multi_repo = multi_repo
                self.token_tracker = token_tracker or mock.MagicMock()

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
    def test_ui_index_html_has_project_slug_input_and_no_default_workspace_dropdown(self):
        html_path = _REPO_ROOT / "ui" / "static" / "index.html"
        content = html_path.read_text(encoding="utf-8")
        self.assertIn('id="ns-select"', content)
        self.assertNotIn('id="ws-row"', content)
        self.assertNotIn('id="ws-select"', content)
        self.assertIn('id="project-slug-group"', content)
        self.assertIn('id="project-slug-input"', content)
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

    def test_dockerfile_layer_caching_order(self):
        ui_df = (_REPO_ROOT / "ui" / "Dockerfile").read_text(encoding="utf-8")
        deps_idx = ui_df.index("COPY --chown=1001:0 ui/pyproject.toml ui/uv.lock ./")
        sync_idx = ui_df.index("uv sync --frozen --no-install-project --no-dev --no-cache")
        code_idx = ui_df.index("COPY --chown=1001:0 ui/ /opt/app-root/src/")
        self.assertLess(deps_idx, sync_idx)
        self.assertLess(sync_idx, code_idx)
        self.assertIn("! -name .venv", ui_df[code_idx:])

        plugin_df = (_REPO_ROOT / "console-plugin" / "Dockerfile").read_text(encoding="utf-8")
        pkg_idx = plugin_df.index("COPY package.json package-lock.json* ./")
        src_idx = plugin_df.index("COPY src ./src")
        nginx_idx = plugin_df.index("COPY nginx.conf /etc/nginx/nginx.conf")
        dist_idx = plugin_df.index("COPY --from=build")
        self.assertLess(pkg_idx, src_idx)
        self.assertLess(nginx_idx, dist_idx)


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


class TestRunStageTracking(unittest.TestCase):
    @staticmethod
    def _make_task(display_name: str, state: str, task_id: str = "", parent_task_id: str = ""):
        return types.SimpleNamespace(
            display_name=display_name,
            state=state,
            task_id=task_id,
            parent_task_id=parent_task_id,
            child_tasks=[],
            end_time=None,
        )

    def test_extract_active_stage_across_pipeline_lifecycle(self):
        from services.get_run_status import extract_active_stage

        # 1. Completed / Failed runs return empty stage
        done_run = types.SimpleNamespace(
            state="SUCCEEDED",
            run_details=types.SimpleNamespace(
                task_details=[self._make_task("generate-migration-report-op", "SUCCEEDED")]
            ),
        )
        self.assertEqual(extract_active_stage(done_run, run_state="SUCCEEDED"), "")

        # 2. Newly queued / starting runs with no task details yet
        empty_run = types.SimpleNamespace(state="PENDING", run_details=None)
        self.assertEqual(extract_active_stage(empty_run, run_state="PENDING"), "Queued")
        self.assertEqual(extract_active_stage(empty_run, run_state="RUNNING"), "Starting…")

        # 3. Active GraphRAG Indexing leaf task (ignoring parent DAG tasks)
        indexing_run = types.SimpleNamespace(
            state="RUNNING",
            run_details=types.SimpleNamespace(
                task_details=[
                    self._make_task("root", "RUNNING", task_id="t-root"),
                    self._make_task("data-generation-pipeline", "SUCCEEDED", task_id="t-dg", parent_task_id="t-root"),
                    self._make_task("benchmark-llm-op", "SUCCEEDED", task_id="t-b", parent_task_id="t-dg"),
                    self._make_task("prepare-environment-op", "SUCCEEDED", task_id="t-p", parent_task_id="t-dg"),
                    self._make_task("generate-code-and-meta-op", "SUCCEEDED", task_id="t-g", parent_task_id="t-dg"),
                    self._make_task("graphrag-indexing-pipeline", "RUNNING", task_id="t-idx", parent_task_id="t-root"),
                    self._make_task("graphrag-indexing-op", "RUNNING", task_id="t-idx-op", parent_task_id="t-idx"),
                ]
            ),
        )
        self.assertEqual(extract_active_stage(indexing_run, run_state="RUNNING"), "GraphRAG Indexing")

        # 4. Concurrent Evaluation + Analysis leaf tasks
        parallel_run = types.SimpleNamespace(
            state="RUNNING",
            run_details=types.SimpleNamespace(
                task_details=[
                    self._make_task("graphrag-indexing-op", "SUCCEEDED"),
                    self._make_task("generate-migration-report-op", "RUNNING"),
                    self._make_task("graphrag-evaluation-op", "RUNNING"),
                ]
            ),
        )
        self.assertEqual(extract_active_stage(parallel_run, run_state="RUNNING"), "Evaluation, Analysis")

        # 5. Pod scheduling transition gap after Data Generation finishes
        gap_run = types.SimpleNamespace(
            state="RUNNING",
            run_details=types.SimpleNamespace(
                task_details=[
                    self._make_task("benchmark-llm-op", "SUCCEEDED"),
                    self._make_task("prepare-environment-op", "SUCCEEDED"),
                    self._make_task("generate-code-and-meta-op", "SUCCEEDED"),
                ]
            ),
        )
        self.assertEqual(extract_active_stage(gap_run, run_state="RUNNING"), "GraphRAG Indexing")

    def test_list_kfp_runs_populates_current_stage_for_running_only(self):
        import services.list_runs as list_runs_mod

        run_running = types.SimpleNamespace(
            run_id="run-live",
            display_name="single_repo_20261111_152357",
            state="RUNNING",
            created_at=None,
            finished_at=None,
            runtime_config=types.SimpleNamespace(
                parameters={
                    "git_repo": "https://github.com/kfrankli/northpolesouthern-train.git",
                    "git_branch": "main",
                }
            ),
            run_details=None,
        )
        full_running_detail = types.SimpleNamespace(
            run_id="run-live",
            state="RUNNING",
            run_details=types.SimpleNamespace(
                task_details=[self._make_task("benchmark-llm-op", "RUNNING")]
            ),
        )
        run_done = types.SimpleNamespace(
            run_id="run-done",
            display_name="single_repo_20261110_120000",
            state="SUCCEEDED",
            created_at=None,
            finished_at=None,
            runtime_config=types.SimpleNamespace(
                parameters={
                    "git_repo": "https://github.com/kfrankli/northpolesouthern-train.git",
                    "git_branch": "main",
                }
            ),
            run_details=None,
        )

        fake_client = mock.MagicMock()
        fake_client.get_experiment.return_value = types.SimpleNamespace(experiment_id="exp-1")
        fake_client.list_runs.return_value = types.SimpleNamespace(runs=[run_running, run_done])
        fake_client.get_run.return_value = full_running_detail

        with mock.patch.object(list_runs_mod, "create_client", return_value=fake_client):
            items = list_runs_mod.list_kfp_runs(limit=10, namespace="demo-ns")

        fake_client.get_run.assert_called_once_with(run_id="run-live")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["current_stage"], "Data Generation (LLM Benchmark)")
        self.assertEqual(items[1]["current_stage"], "")

    def test_ui_index_html_renders_stage_under_repo_url(self):
        html_path = _REPO_ROOT / "ui" / "static" / "index.html"
        content = html_path.read_text(encoding="utf-8")
        self.assertIn("cu-run-stage", content)
        self.assertIn("run.current_stage", content)
        self.assertIn("refreshActiveRuns", content)
        self.assertIn("syncRunsAutoRefresh", content)


class TestReviewFixesAndFastAPIRoutes(unittest.TestCase):
    def test_escape_mlflow_filter_value_and_scoped_session_token_patch(self):
        import requests
        from utils.pipeline_utils import (
            escape_mlflow_filter_value,
            patch_mlflow_session_forwarded_token,
        )

        self.assertEqual(escape_mlflow_filter_value(r"a'b\c"), r"a\'b\\c")

        patch_mlflow_session_forwarded_token()
        sess = requests.Session()
        req_mlflow = types.SimpleNamespace(
            url="https://mlflow.demo.svc:8443/api/2.0/mlflow/runs/search",
            headers={},
        )
        req_external = types.SimpleNamespace(
            url="https://api.github.com/repos/org/repo",
            headers={},
        )
        with mock.patch.dict(
            os.environ,
            {
                "MLFLOW_TRACKING_URI": "https://mlflow.demo.svc:8443",
                "MLFLOW_TRACKING_TOKEN": "secret-ocp-token",
            },
            clear=False,
        ):
            sess.send(req_mlflow)
            sess.send(req_external)

        self.assertEqual(req_mlflow.headers.get("X-Forwarded-Access-Token"), "secret-ocp-token")
        self.assertNotIn("X-Forwarded-Access-Token", req_external.headers)

    def test_get_kfp_host_preserves_env_host_for_default_namespace_only(self):
        from utils.service_utils import get_kfp_host

        with mock.patch.dict(
            os.environ,
            {
                "KFP_NAMESPACE": "team-a",
                "KFP_HOST": "https://ds-pipeline-dspa-team-a.apps.cluster.example.com",
            },
            clear=False,
        ):
            self.assertEqual(
                get_kfp_host(),
                ("https://ds-pipeline-dspa-team-a.apps.cluster.example.com", "team-a"),
            )
            self.assertEqual(
                get_kfp_host("team-a"),
                ("https://ds-pipeline-dspa-team-a.apps.cluster.example.com", "team-a"),
            )
            self.assertEqual(
                get_kfp_host("team-b"),
                ("https://ds-pipeline-dspa.team-b.svc.cluster.local:8443", "team-b"),
            )

    def test_get_active_workspace_ignores_stale_cookie(self):
        from fastapi import Request
        from ui.cluster import get_active_workspace

        req = Request(cookies={"cu_workspace": "stale-team"})
        with mock.patch.dict(os.environ, {"PROJECT_WORKSPACE": ""}, clear=False):
            self.assertEqual(get_active_workspace(req), "default")
        with mock.patch.dict(os.environ, {"PROJECT_WORKSPACE": "configured-ws"}, clear=False):
            self.assertEqual(get_active_workspace(req), "configured-ws")

    def test_adhoc_query_caches_index_by_mlflow_run_id_and_isolates_trackers(self):
        import shutil
        import pipelines.base.analysis as base_analysis

        downloads = []
        seen_trackers = []

        class CachingFakeAnalyzer:
            def __init__(self, graphrag_source_path, git_slug="", multi_repo=False, token_tracker=None):
                self.graphrag_source_path = graphrag_source_path
                self.git_slug = git_slug
                self.token_tracker = token_tracker or mock.MagicMock()
                seen_trackers.append(self.token_tracker)

            @staticmethod
            def download_graphrag_directory(download_dir, git_slug=None, multi_repo=False, **kwargs):
                downloads.append(download_dir)

            async def query_with_llm(self, prompt, **kwargs):
                return "cached answer"

        fake_graphrag_utils = types.ModuleType("utils.graphrag_utils")
        fake_graphrag_utils.DependencyAnalyzer = CachingFakeAnalyzer

        with base_analysis._INDEX_CACHE_LOCK:
            for entry in base_analysis._INDEX_CACHE.values():
                shutil.rmtree(entry["path"], ignore_errors=True)
            base_analysis._INDEX_CACHE.clear()

        try:
            with mock.patch.dict(sys.modules, {"utils.graphrag_utils": fake_graphrag_utils}):
                with mock.patch("loaders.default_asset_loader.DefaultAssetLoader"):
                    with mock.patch.object(base_analysis, "_resolve_mlflow_index_run_id", return_value="idx-run-1"):
                        base_analysis.run_adhoc_query_pipeline(question="Q1", git_repo="https://github.com/o/r.git")
                        base_analysis.run_adhoc_query_pipeline(question="Q2", git_repo="https://github.com/o/r.git")
                    with mock.patch.object(base_analysis, "_resolve_mlflow_index_run_id", return_value="idx-run-2"):
                        base_analysis.run_adhoc_query_pipeline(question="Q3", git_repo="https://github.com/o/r.git")

            # Same run_id downloaded once; new run_id triggered fresh download
            self.assertEqual(len(downloads), 2)
            self.assertNotEqual(downloads[0], downloads[1])
            self.assertIsNot(seen_trackers[0], seen_trackers[1])
        finally:
            with base_analysis._INDEX_CACHE_LOCK:
                for entry in base_analysis._INDEX_CACHE.values():
                    shutil.rmtree(entry["path"], ignore_errors=True)
                base_analysis._INDEX_CACHE.clear()

    def test_security_middleware_and_openshift_token_verification(self):
        import asyncio
        import json
        from fastapi import Request, Response
        import ui.main as ui_main

        with ui_main._TOKEN_CACHE_LOCK:
            ui_main._TOKEN_CACHE.clear()

        mw = ui_main.SecurityMiddleware()

        async def _ok_next(_req):
            r = Response()
            r.status_code = 200
            return r

        with mock.patch.dict(
            os.environ,
            {
                "REQUIRE_OPENSHIFT_AUTH": "true",
                "OPENSHIFT_CONSOLE_URL": "https://console-openshift-console.apps.example.com/",
            },
            clear=False,
        ):
            # Missing token on /api/status -> 401
            unauth_resp = asyncio.run(mw.dispatch(Request(path="/api/status"), _ok_next))
            self.assertEqual(unauth_resp.status_code, 401)
            self.assertEqual(
                unauth_resp.headers["Content-Security-Policy"],
                "frame-ancestors 'self' https://console-openshift-console.apps.example.com",
            )
            self.assertNotIn("X-Frame-Options", unauth_resp.headers)

            # /api/health remains unauthenticated for kubelet probes
            health_resp = asyncio.run(mw.dispatch(Request(path="/api/health"), _ok_next))
            self.assertEqual(health_resp.status_code, 200)

            # Valid bearer token verified once against k8s API and cached on repeat call
            fake_http_resp = mock.MagicMock()
            fake_http_resp.__enter__.return_value = fake_http_resp
            fake_http_resp.status = 200
            fake_http_resp.read.return_value = json.dumps({"metadata": {"name": "alice"}}).encode("utf-8")

            with mock.patch("urllib.request.urlopen", return_value=fake_http_resp) as mock_urlopen:
                req_authed = Request(
                    path="/api/status",
                    headers={"Authorization": "Bearer valid-ocp-token"},
                )
                r1 = asyncio.run(mw.dispatch(req_authed, _ok_next))
                r2 = asyncio.run(mw.dispatch(req_authed, _ok_next))
                self.assertEqual(r1.status_code, 200)
                self.assertEqual(r2.status_code, 200)
                self.assertEqual(mock_urlopen.call_count, 1)

    def test_fastapi_routes_namespace_indexes_queries_repositories_and_pipelines(self):
        from fastapi import HTTPException, Request, Response
        import api.pipelines as api_pipelines
        import api.queries as api_queries
        import api.repositories as api_repositories
        import ui.main as ui_main

        # 1. Index no-cache header + Namespace cookie + validation
        idx_file_resp = ui_main.index()
        self.assertEqual(
            idx_file_resp.headers.get("Cache-Control"),
            "no-cache, no-store, must-revalidate",
        )
        resp = Response()
        out = ui_main.api_set_namespace(ui_main.SelectNamespaceRequest(namespace=" team-ns "), resp)
        self.assertEqual(out, {"active": "team-ns"})
        with self.assertRaises(HTTPException) as ctx:
            ui_main.api_set_namespace(ui_main.SelectNamespaceRequest(namespace="   "), Response())
        self.assertEqual(ctx.exception.status_code, 400)

        # 2. /api/indexes returns repos + default_key in a single response
        req = Request(cookies={"cu_namespace": "team-ns"})
        with mock.patch.object(
            ui_main,
            "list_indexes_and_repos",
            return_value=(
                [{"git_repo": "https://github.com/o/r.git", "git_branch": "main", "git_slug": "o-r-main", "indexed": True, "multi_repo": False}],
                [{"git_repo": "https://github.com/o/r.git", "git_branch": "main"}],
            ),
        ):
            idx_payload = ui_main.api_indexes(req)
        self.assertEqual(idx_payload["namespace"], "team-ns")
        self.assertEqual(idx_payload["default_key"], "https://github.com/o/r.git|main")
        self.assertEqual(len(idx_payload["queryable"]), 1)

        # 3. /api/v2/queries executes synchronously and caps _QUERY_RESULTS at 50
        with api_queries._QUERY_RESULTS_LOCK:
            api_queries._QUERY_RESULTS.clear()
        with mock.patch.object(api_queries, "execute_adhoc_query", return_value="GraphRAG answer") as mock_q:
            q_resp = api_queries.post_adhoc_query(
                api_queries.AdhocQueryRequest(question="What services exist?", git_repo="https://github.com/o/r.git"),
                req,
            )
            self.assertEqual(q_resp.answer, "GraphRAG answer")
            mock_q.assert_called_once()
            self.assertEqual(mock_q.call_args.kwargs["namespace"], "team-ns")
            for i in range(60):
                api_queries._store_query_result(f"qid-{i}", {"query_id": f"qid-{i}", "status": "succeeded"})
        self.assertEqual(len(api_queries._QUERY_RESULTS), api_queries._QUERY_RESULTS_MAX)

        # 4. /api/v2/repositories scopes reads/writes to active namespace
        with mock.patch.object(
            api_repositories,
            "fetch_catalog_repos",
            return_value=[{"git_repo": "https://github.com/o/r.git", "git_branch": "dev"}],
        ) as mock_fetch, mock.patch.object(api_repositories, "_log_repos") as mock_log:
            updated = api_repositories.post_repositories(
                api_repositories.UpdateReposRequest(
                    repos=[api_repositories.RepoItem(git_repo="https://github.com/o/r.git", git_branch="dev")]
                ),
                req,
            )
            mock_log.assert_called_once_with(
                [{"git_repo": "https://github.com/o/r.git", "git_branch": "dev"}],
                namespace="team-ns",
                workspace="default",
            )
            mock_fetch.assert_called_once_with(namespace="team-ns", workspace="default")
            self.assertEqual(updated[0].git_branch, "dev")

        # 5. /api/v2/pipelines validates empty payload and maps UI status labels
        with self.assertRaises(HTTPException) as p_ctx:
            api_pipelines.trigger_pipeline(api_pipelines.TriggerPipelineRequest(), req)
        self.assertEqual(p_ctx.exception.status_code, 422)

        with mock.patch.object(
            api_pipelines,
            "list_runs",
            return_value=[{"run_id": "r-1", "run_name": "run-1", "status": "IN_PROGRESS", "current_stage": "Analysis"}],
        ):
            runs_env = api_pipelines.get_recent_runs_envelope(req, limit=5)
        self.assertEqual(runs_env["runs"][0]["status"], "Running")
        self.assertEqual(runs_env["runs"][0]["current_stage"], "Analysis")


if __name__ == "__main__":
    unittest.main()
