"""List recent KFP runs in the configured experiment."""

from __future__ import annotations

import logging

from services.get_run_status import (
    _ACTIVE_RUN_STATES,
    _normalize_state,
    extract_active_stage,
    extract_run_target,
    matches_run_workspace,
)
from utils.service_utils import create_client, get_kfp_experiment_name

logger = logging.getLogger(__name__)


def list_kfp_runs(
    limit: int = 20,
    namespace: str | None = None,
    workspace: str | None = None,
) -> list[dict]:
    """Return the most recent KFP runs as normalised summary dicts."""
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

    runs_resp = client.list_runs(
        experiment_id=experiment_id,
        page_size=limit,
        sort_by="created_at desc",
    )
    items = getattr(runs_resp, "runs", None) or []

    out: list[dict] = []
    for r in items[:limit]:
        run_id = getattr(r, "run_id", None) or getattr(r, "id", "")
        state_raw = getattr(r, "state", None) or getattr(r, "runtime_state", None)
        state = _normalize_state(state_raw) or "PENDING"
        created_at = getattr(r, "created_at", None)
        finished_at = getattr(r, "finished_at", None)

        target = extract_run_target(r, fallback_id=run_id)
        if not matches_run_workspace(target["project_workspace"], workspace):
            continue
        display = target["display"]
        git_repo = target["git_repo"]
        git_branch = target["git_branch"]
        is_multi = target["multi_repo"]
        project_slug = target["project_slug"]
        project_workspace = target["project_workspace"]
        repos = target["repos"]

        current_stage = ""
        if state in _ACTIVE_RUN_STATES:
            run_for_stage = r
            rd = getattr(r, "run_details", None)
            tds = getattr(rd, "task_details", None) if rd is not None else None
            if not (isinstance(tds, (list, tuple)) and tds) and run_id:
                try:
                    run_for_stage = client.get_run(run_id=run_id)
                except Exception:
                    logger.debug("Could not fetch full run details for active run %s", run_id, exc_info=True)
            current_stage = extract_active_stage(
                run_for_stage,
                run_state=state,
                run_id=run_id,
                namespace=namespace,
            )

        out.append(
            {
                "job_id": run_id,
                "run_id": run_id,
                "run_name": display,
                "name": display,
                "status": state,
                "current_stage": current_stage,
                "git_repo": git_repo,
                "git_branch": git_branch,
                "multi_repo": is_multi,
                "project_slug": project_slug,
                "project_workspace": project_workspace,
                "repos": repos,
                "created_at": created_at.isoformat() if created_at else None,
                "finished_at": finished_at.isoformat() if finished_at else None,
            }
        )
    return out
