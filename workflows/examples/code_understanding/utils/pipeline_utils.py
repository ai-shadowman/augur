import os
import re


def uses_jupyter_runtime():
    """Returns True if the current process is running inside a Jupyter notebook."""
    try:
        from IPython import get_ipython
        return get_ipython() is not None
    except ImportError:
        return False


def _running_in_kubernetes_pod() -> bool:
    """Returns True if the process is running inside a Kubernetes pod."""
    return os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token")


def uses_kfp():
    """Returns True if KFP is installed and the process is not running in a Jupyter notebook."""
    try:
        if uses_jupyter_runtime():
            return False
        import kfp  # noqa: F401
        return True
    except ImportError:
        return False


def get_pipeline_run_id(allow_generate: bool = False) -> str | None:
    """Returns a stable identifier for the current pipeline run.

    - Checks ``PIPELINE_RUN_ID`` or ``_AGENTMESH_PIPELINE_RUN_ID`` if already set.
    - KFP (RHOAI): checks ``KFP_RUN_ID`` or reads the ``pipeline/runid`` pod label via
      ``get_kfp_run_id()``.
    - Local/non-KFP: when ``allow_generate=True``, a UUID is generated once per process
      and cached in ``_AGENTMESH_PIPELINE_RUN_ID``.
    """
    if explicit_id := os.environ.get("PIPELINE_RUN_ID", "").strip():
        return explicit_id

    if "_AGENTMESH_PIPELINE_RUN_ID" in os.environ:
        return os.environ["_AGENTMESH_PIPELINE_RUN_ID"]

    try:
        from utils.kubeflow_utils import get_kfp_run_id

        pipeline_run_id = get_kfp_run_id()
        if pipeline_run_id:
            os.environ["_AGENTMESH_PIPELINE_RUN_ID"] = pipeline_run_id
            return pipeline_run_id
    except Exception:
        pass

    if os.getenv("KFP_POD_NAME") and uses_kfp() and _running_in_kubernetes_pod():
        raise RuntimeError(
            "Running in KFP but could not read the pipeline/runid pod label. "
            "Ensure KFP_RUN_ID or KFP_POD_NAME and KFP_NAMESPACE are set."
        )

    if allow_generate:
        import uuid

        os.environ["_AGENTMESH_PIPELINE_RUN_ID"] = str(uuid.uuid4())
        return os.environ["_AGENTMESH_PIPELINE_RUN_ID"]

    return None


def sanitize_slug(value: str | None) -> str:
    """Sanitizes a project slug or workspace identifier to URL/path-safe characters."""
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", (value or "").strip().lower()).strip("-")
    return cleaned[:120]


def resolve_mlflow_prefix(namespace: str | None = None, workspace: str | None = None) -> str:
    """Resolves the MLflow experiment prefix for a given Kubernetes namespace and Project Workspace.

    When ``workspace`` is empty or ``"default"``, returns ``{namespace}`` for backward compatibility.
    When a custom ``workspace`` is active, returns ``{namespace}/{workspace}`` so multiple project
    workspaces inside the same Kubernetes namespace have isolated MLflow experiments.
    """
    ns = (
        namespace
        or os.environ.get("MLFLOW_NAMESPACE")
        or os.environ.get("MLFLOW_WORKSPACE")
        or os.environ.get("KFP_NAMESPACE")
        or "demo"
    ).strip()
    ws_raw = workspace if workspace is not None else os.environ.get("PROJECT_WORKSPACE", "")
    ws = sanitize_slug(ws_raw)
    if ws and ws.lower() != "default":
        return f"{ns}/{ws}"
    return ns


def generate_multi_repo_slug(repo_list: list[dict] | None = None, project_slug: str = "") -> str:
    """Returns an explicit sanitized project_slug or derives a deterministic slug from repo_list."""
    explicit = sanitize_slug(project_slug)
    if explicit:
        return explicit

    if not repo_list:
        return "multi-repo"

    parts = []
    for item in repo_list:
        if isinstance(item, dict) and item.get("git_repo"):
            repo_url = item["git_repo"].rstrip("/").removesuffix(".git")
            short_name = repo_url.split("/")[-1]
            if not short_name:
                from utils.code_utils import generate_slug_from_repo

                short_name = generate_slug_from_repo(
                    item["git_repo"], item.get("git_branch", "main")
                )
            branch = item.get("git_branch", "main")
            parts.append(sanitize_slug(f"{short_name}-{branch}"))
    parts = sorted(filter(None, parts))
    if not parts:
        return "multi-repo"
    combined = "multi-repo-" + "--".join(parts)
    if len(combined) > 120:
        import hashlib

        digest = hashlib.sha1(combined.encode("utf-8")).hexdigest()[:8]
        combined = f"{combined[:110].rstrip('-')}-{digest}"
    return combined
