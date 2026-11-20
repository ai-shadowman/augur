"""Delete a single KFP pipeline run and purge its associated MLflow records."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from loaders.default_asset_loader import DefaultAssetLoader
from loaders.mlflow_asset_loader import _repo_identity
from services.get_run_status import (
    _ACTIVE_RUN_STATES,
    _normalize_state,
    _safe_str,
    _unwrap_run,
    extract_run_target,
    matches_run_workspace,
)
from services.update_repos import _is_kfp_configured
from utils.pipeline_utils import sanitize_slug
from utils.service_utils import create_client, get_kfp_experiment_name

logger = logging.getLogger(__name__)


def _to_epoch_ms(ts: Any) -> int | None:
    if isinstance(ts, datetime):
        dt = ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
        ms = int(dt.timestamp() * 1000)
        return ms if ms > 0 else None
    if isinstance(ts, str) and ts.strip():
        try:
            dt = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            ms = int(dt.timestamp() * 1000)
            return ms if ms > 0 else None
        except ValueError:
            return None
    return None


def _has_sibling_target_runs(
    kfp_client: Any,
    exclude_run_id: str,
    git_repo: str,
    git_branch: str,
    multi_repo: bool,
    project_slug: str,
    workspace: str | None,
) -> bool:
    exp_name = get_kfp_experiment_name(workspace=workspace)
    experiment_id: str | None = None
    try:
        exp = kfp_client.get_experiment(experiment_name=exp_name)
        experiment_id = getattr(exp, "experiment_id", None)
    except Exception:  # noqa: BLE001
        if exp_name != "Default":
            return False
        logger.debug("Could not look up Default experiment; listing across all", exc_info=True)

    runs_resp = kfp_client.list_runs(
        experiment_id=experiment_id,
        page_size=100,
        sort_by="created_at desc",
    )
    items = getattr(runs_resp, "runs", None) or []
    target_repo = _repo_identity({"git_repo": git_repo, "git_branch": git_branch}) if git_repo else ("", "")
    norm_proj = sanitize_slug(project_slug)

    for r in items:
        rid = _safe_str(r, "run_id") or _safe_str(r, "id")
        if not rid or rid == exclude_run_id:
            continue
        info = extract_run_target(r, fallback_id=rid)
        if not matches_run_workspace(info["project_workspace"], workspace):
            continue
        if multi_repo:
            if info["multi_repo"] and sanitize_slug(info["project_slug"]) == norm_proj:
                return True
        elif target_repo[0] and not info["multi_repo"]:
            if _repo_identity({"git_repo": info["git_repo"], "git_branch": info["git_branch"] or "main"}) == target_repo:
                return True

    next_token = getattr(runs_resp, "next_page_token", None)
    return bool(isinstance(next_token, str) and next_token)


def delete_kfp_run(
    job_id: str,
    git_repo: str = "",
    git_branch: str = "main",
    multi_repo: bool = False,
    project_slug: str = "",
    namespace: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Terminate (if active) and delete a KFP run along with its MLflow runs and traces."""
    run_id = (job_id or "").strip()
    if not run_id:
        raise ValueError("job_id is required.")

    start_ms: int | None = None
    end_ms: int | None = None
    has_other_target_runs = True
    kfp_client = None

    if _is_kfp_configured():
        kfp_client = create_client(namespace=namespace)
        try:
            run_detail = kfp_client.get_run(run_id=run_id)
        except Exception as exc:
            raise LookupError(f"KFP run '{run_id}' not found: {exc}") from exc

        run_obj = _unwrap_run(run_detail)
        info = extract_run_target(run_obj, fallback_id=run_id)
        if not matches_run_workspace(info["project_workspace"], workspace):
            raise LookupError(f"KFP run '{run_id}' not found in workspace '{workspace or 'default'}'.")

        if info["git_repo"]:
            git_repo = info["git_repo"]
            git_branch = info["git_branch"] or git_branch or "main"
        multi_repo = info["multi_repo"] or (multi_repo and not info["git_repo"])
        if info["project_slug"]:
            project_slug = info["project_slug"]

        start_ms = _to_epoch_ms(getattr(run_obj, "created_at", None))
        end_ms = _to_epoch_ms(getattr(run_obj, "finished_at", None))

        state_raw = getattr(run_obj, "state", None) or getattr(run_obj, "runtime_state", None)
        if _normalize_state(state_raw) in _ACTIVE_RUN_STATES:
            try:
                kfp_client.terminate_run(run_id)
            except Exception:  # noqa: BLE001
                logger.debug("Could not terminate active KFP run %s before delete", run_id, exc_info=True)
            if end_ms is None:
                end_ms = int(time.time() * 1000)

        try:
            has_other_target_runs = _has_sibling_target_runs(
                kfp_client,
                exclude_run_id=run_id,
                git_repo=git_repo,
                git_branch=git_branch or "main",
                multi_repo=multi_repo,
                project_slug=project_slug,
                workspace=workspace,
            )
        except Exception:  # noqa: BLE001
            logger.debug("Could not inspect sibling KFP runs for %s", run_id, exc_info=True)
            has_other_target_runs = True

    loader = DefaultAssetLoader(namespace=namespace, workspace=workspace)
    deleted_mlflow_runs = loader.purge_run_records(
        run_id,
        git_repo=git_repo,
        git_branch=git_branch or "main",
        multi_repo=multi_repo,
        project_slug=project_slug,
        start_time_ms=start_ms,
        end_time_ms=end_ms,
        purge_untagged_for_target=not has_other_target_runs,
    )

    if kfp_client is not None:
        if hasattr(kfp_client, "delete_run"):
            kfp_client.delete_run(run_id)
        else:
            kfp_client._run_api.delete_run(run_id)

    return {
        "deleted": True,
        "job_id": run_id,
        "run_id": run_id,
        "deleted_mlflow_runs": deleted_mlflow_runs,
    }
