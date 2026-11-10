"""Fetch and normalise the state and parameters of a KFP run."""

from __future__ import annotations

import os
from typing import Any

from utils.service_utils import create_client


def get_kfp_run_info(job_id: str, namespace: str | None = None) -> dict[str, Any]:
    """Return normalised state and runtime parameters for a KFP run."""
    client = create_client(namespace=namespace)
    try:
        run_detail = client.get_run(run_id=job_id)
    except Exception as exc:
        raise ValueError(f"KFP run '{job_id}' not found: {exc}") from exc

    run_obj = getattr(run_detail, "run", run_detail)
    state_raw = getattr(run_obj, "state", None) or getattr(run_obj, "runtime_state", None)
    state = str(getattr(state_raw, "name", state_raw) or "PENDING").upper()
    if not state or state in ("NONE", "RUNTIME_STATE_UNSPECIFIED"):
        state = "PENDING"

    display = getattr(run_obj, "display_name", None) or getattr(run_obj, "name", "") or job_id
    display_lower = display.lower()
    rc = getattr(run_obj, "runtime_config", None)
    params = (getattr(rc, "parameters", None) if rc else None) or {}
    git_repo = str(params.get("git_repo") or "")
    git_branch = str(params.get("git_branch") or "main") if git_repo else ""
    project_slug = str(params.get("project_slug") or "")
    project_workspace = str(params.get("project_workspace") or "")
    is_single = display_lower.startswith("single_repo") or display_lower.startswith("single-repo")
    is_multi = (
        display_lower.startswith("multi_repo")
        or display_lower.startswith("multi-repo")
        or (not git_repo and not is_single)
    )
    if not is_multi and not git_repo:
        git_repo = os.getenv("GIT_REPO", "")
        git_branch = str(params.get("git_branch") or os.getenv("GIT_BRANCH", "main") or "main")

    if is_multi and not project_slug:
        raw_repos = params.get("runnable_repos", "")
        if raw_repos:
            try:
                import json
                from utils.pipeline_utils import generate_multi_repo_slug

                parsed = json.loads(raw_repos) if isinstance(raw_repos, str) else raw_repos
                if isinstance(parsed, list) and parsed:
                    project_slug = generate_multi_repo_slug(parsed)
            except Exception:
                pass

    return {
        "job_id": job_id,
        "run_name": display,
        "state": state,
        "git_repo": git_repo,
        "git_branch": git_branch or "main",
        "multi_repo": is_multi,
        "project_slug": project_slug,
        "project_workspace": project_workspace,
    }


def get_kfp_run_state(job_id: str, namespace: str | None = None) -> str:
    """Return the normalised uppercase state string for a KFP run."""
    return get_kfp_run_info(job_id, namespace=namespace)["state"]
