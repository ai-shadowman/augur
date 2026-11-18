"""Update the repository list in the asset store."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def log_repos(
    repos: list[dict[str, str]],
    namespace: str | None = None,
    workspace: str | None = None,
) -> None:
    """Logs repos to the asset store via the configured loader."""
    from loaders.default_asset_loader import DefaultAssetLoader

    DefaultAssetLoader(namespace=namespace, workspace=workspace).log_repos(repos)


def purge_kfp_runs_for_repo(
    git_repo: str,
    git_branch: str = "main",
    namespace: str | None = None,
    workspace: str | None = None,
) -> list[str]:
    """Terminate (if active) and delete single-repo KFP runs matching ``(git_repo, git_branch)``."""
    from loaders.mlflow_asset_loader import _repo_identity
    from services.get_run_status import _ACTIVE_RUN_STATES, _normalize_state
    from utils.service_utils import create_client, get_kfp_experiment_name

    target = _repo_identity({"git_repo": git_repo, "git_branch": git_branch})
    if not target[0]:
        return []

    client = create_client(namespace=namespace)
    exp_name = get_kfp_experiment_name(workspace=workspace)
    experiment_id: str | None = None
    try:
        exp = client.get_experiment(experiment_name=exp_name)
        experiment_id = getattr(exp, "experiment_id", None)
    except Exception:
        if exp_name != "Default":
            return []
        logger.debug("Could not look up Default experiment; listing across all", exc_info=True)

    deleted_ids: list[str] = []
    seen_tokens: set[str] = set()
    page_token: str | None = None
    while True:
        list_kwargs: dict[str, object] = {
            "experiment_id": experiment_id,
            "page_size": 100,
            "sort_by": "created_at desc",
        }
        if page_token:
            list_kwargs["page_token"] = page_token
        runs_resp = client.list_runs(**list_kwargs)
        items = getattr(runs_resp, "runs", None) or []
        for r in items:
            run_id = getattr(r, "run_id", None) or getattr(r, "id", "")
            if not isinstance(run_id, str) or not run_id:
                continue
            display = getattr(r, "display_name", None) or getattr(r, "name", "") or run_id
            rc = getattr(r, "runtime_config", None)
            params = (getattr(rc, "parameters", None) if rc else None) or {}
            run_repo = str(params.get("git_repo") or "")
            run_branch = str(params.get("git_branch") or "main") if run_repo else ""
            project_workspace = str(params.get("project_workspace") or "")
            if workspace and workspace.strip().lower() != "default":
                if project_workspace and project_workspace.lower() != workspace.strip().lower():
                    continue
            elif project_workspace and project_workspace.lower() != "default":
                if not workspace or workspace.strip().lower() == "default":
                    continue

            display_lower = str(display).lower()
            is_single = display_lower.startswith("single_repo") or display_lower.startswith("single-repo")
            is_multi = (
                display_lower.startswith("multi_repo")
                or display_lower.startswith("multi-repo")
                or (not run_repo and not is_single)
            )
            if is_multi or not run_repo:
                continue
            if _repo_identity({"git_repo": run_repo, "git_branch": run_branch}) != target:
                continue

            state_raw = getattr(r, "state", None) or getattr(r, "runtime_state", None)
            if _normalize_state(state_raw) in _ACTIVE_RUN_STATES:
                try:
                    client.terminate_run(run_id)
                except Exception:
                    logger.debug("Could not terminate active KFP run %s before delete", run_id, exc_info=True)
            if hasattr(client, "delete_run"):
                client.delete_run(run_id)
            else:
                client._run_api.delete_run(run_id)
            deleted_ids.append(run_id)

        next_token = getattr(runs_resp, "next_page_token", None)
        if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
            break
        seen_tokens.add(next_token)
        page_token = next_token

    return deleted_ids


def _is_kfp_configured() -> bool:
    from utils.service_utils import _SA_TOKEN_PATH

    return bool(
        os.environ.get("KFP_HOST", "").strip()
        or os.environ.get("KFP_NAMESPACE", "").strip()
        or os.path.exists(_SA_TOKEN_PATH)
    )


def delete_repo(
    git_repo: str,
    git_branch: str = "main",
    namespace: str | None = None,
    workspace: str | None = None,
) -> list[dict]:
    """Purges single-repo KFP and MLflow records, then removes the repository from the catalog."""
    from loaders.default_asset_loader import DefaultAssetLoader

    loader = DefaultAssetLoader(namespace=namespace, workspace=workspace)
    kfp_run_ids = (
        purge_kfp_runs_for_repo(git_repo, git_branch, namespace=namespace, workspace=workspace)
        if _is_kfp_configured()
        else []
    )
    loader.purge_repo_records(git_repo, git_branch, kfp_run_ids=kfp_run_ids)
    return loader.delete_repo(git_repo, git_branch)
