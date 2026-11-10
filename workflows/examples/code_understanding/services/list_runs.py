"""List recent KFP runs in the configured experiment."""

from __future__ import annotations

import json
import logging
import os

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
        display = getattr(r, "display_name", None) or getattr(r, "name", "") or run_id
        state_raw = getattr(r, "state", None) or getattr(r, "runtime_state", None)
        state = str(getattr(state_raw, "name", state_raw) or "PENDING").upper()
        if not state or state in ("NONE", "RUNTIME_STATE_UNSPECIFIED"):
            state = "PENDING"
        created_at = getattr(r, "created_at", None)
        finished_at = getattr(r, "finished_at", None)

        rc = getattr(r, "runtime_config", None)
        params = (getattr(rc, "parameters", None) if rc else None) or {}
        git_repo = str(params.get("git_repo") or "")
        git_branch = str(params.get("git_branch") or "main") if git_repo else ""
        project_slug = str(params.get("project_slug") or "")
        project_workspace = str(params.get("project_workspace") or "")
        if workspace and workspace.strip().lower() != "default":
            if project_workspace and project_workspace.lower() != workspace.strip().lower():
                continue
        elif project_workspace and project_workspace.lower() != "default":
            if not workspace or workspace.strip().lower() == "default":
                continue
        display_lower = display.lower()
        is_single = display_lower.startswith("single_repo") or display_lower.startswith("single-repo")
        is_multi = (
            display_lower.startswith("multi_repo")
            or display_lower.startswith("multi-repo")
            or (not git_repo and not is_single)
        )
        if not is_multi and not git_repo:
            git_repo = os.getenv("GIT_REPO", "")
            git_branch = str(params.get("git_branch") or os.getenv("GIT_BRANCH", "main") or "main")

        repos: list[dict] = []
        if is_multi:
            raw_repos = params.get("runnable_repos", "")
            if raw_repos:
                try:
                    parsed = json.loads(raw_repos) if isinstance(raw_repos, str) else raw_repos
                    if isinstance(parsed, list):
                        repos = [
                            {
                                "git_repo": entry.get("git_repo", ""),
                                "git_branch": entry.get("git_branch", "main"),
                            }
                            for entry in parsed
                            if isinstance(entry, dict) and entry.get("git_repo")
                        ]
                except Exception:
                    logger.debug("Could not parse runnable_repos for run %s", run_id, exc_info=True)
            if not project_slug and repos:
                try:
                    from utils.pipeline_utils import generate_multi_repo_slug
                    project_slug = generate_multi_repo_slug(repos)
                except Exception:
                    pass
        elif git_repo:
            repos = [{"git_repo": git_repo, "git_branch": git_branch}]

        out.append(
            {
                "job_id": run_id,
                "run_id": run_id,
                "run_name": display,
                "name": display,
                "status": state,
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
