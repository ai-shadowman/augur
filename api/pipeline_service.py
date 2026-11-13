"""Thin bridge between the FastAPI layer and the KFP / MLflow services."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_CODE_UNDERSTANDING_DIR = str(
    Path(__file__).resolve().parent.parent / "workflows" / "examples" / "code_understanding"
)
if _CODE_UNDERSTANDING_DIR not in sys.path:
    sys.path.insert(0, _CODE_UNDERSTANDING_DIR)

logger = logging.getLogger(__name__)


def submit_pipeline_run(
    repos: list[dict[str, str]],
    git_branch: str = "main",
    namespace: str | None = None,
    workspace: str | None = None,
    pipeline_run_id: str = "",
    project_slug: str = "",
) -> dict[str, Any]:
    """Submit a KFP pipeline run via ``services.trigger_run``."""
    from services.trigger_run import trigger_run
    from services.update_repos import log_repos
    from utils.pipeline_utils import generate_multi_repo_slug, sanitize_slug

    source_path = os.environ.get("PARENT_SOURCE_PATH", "source")
    target_path = os.environ.get("PARENT_TARGET_PATH", "target")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    clean_ws = sanitize_slug(workspace or "")
    ws_param = clean_ws if (clean_ws and clean_ws.lower() != "default") else ""

    if len(repos) == 1:
        entry = repos[0]
        repo_url = entry["git_repo"]
        branch = entry.get("git_branch") or git_branch
        pipeline_name = "single_repo"
        run_name = f"single_repo_{timestamp}"
        resolved_slug = ""
        params: dict[str, Any] = {
            "git_repo": repo_url,
            "git_branch": branch,
            "parent_source_path": source_path,
            "parent_target_path": target_path,
            "pipeline_run_id": pipeline_run_id,
            "project_workspace": ws_param,
        }
        try:
            log_repos(
                [{"git_repo": repo_url, "git_branch": branch}],
                namespace=namespace,
                workspace=workspace,
            )
        except Exception:
            logger.warning(
                "log_repos failed for single-repo; continuing with pipeline submission.",
                exc_info=True,
            )
    else:
        pipeline_name = "multi_repo"
        resolved_slug = generate_multi_repo_slug(repos, project_slug=project_slug)
        run_name = (
            f"multi_repo_{resolved_slug}_{timestamp}"
            if resolved_slug and resolved_slug != "multi-repo"
            else f"multi_repo_{timestamp}"
        )
        if repos:
            try:
                log_repos(repos, namespace=namespace, workspace=workspace)
            except Exception:
                logger.warning(
                    "log_repos failed; continuing with pipeline submission.",
                    exc_info=True,
                )
        params = {
            "parent_source_path": source_path,
            "parent_target_path": target_path,
            "pipeline_run_id": pipeline_run_id,
            "runnable_repos": json.dumps(repos),
            "project_slug": resolved_slug,
            "project_workspace": ws_param,
        }

    enable_caching = bool(pipeline_run_id)
    run = trigger_run(
        pipeline_name,
        run_name,
        params,
        enable_caching=enable_caching,
        namespace=namespace,
        workspace=workspace,
    )
    return {
        "job_id": run.run_id,
        "run_name": run_name,
        "pipeline_name": pipeline_name,
        "project_slug": resolved_slug,
        "project_workspace": ws_param or "default",
    }


def list_runs(
    limit: int = 20,
    namespace: str | None = None,
    workspace: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent KFP runs via ``services.list_runs``."""
    from services.list_runs import list_kfp_runs

    return list_kfp_runs(limit=limit, namespace=namespace, workspace=workspace)


def get_run_status(job_id: str, namespace: str | None = None) -> str:
    """Return the normalised KFP state via ``services.get_run_status``."""
    from services.get_run_status import get_kfp_run_state

    return get_kfp_run_state(job_id, namespace=namespace)


def get_run_info(job_id: str, namespace: str | None = None) -> dict[str, Any]:
    """Return the normalised KFP state and runtime parameters via ``services.get_run_status``."""
    from services.get_run_status import get_kfp_run_info

    return get_kfp_run_info(job_id, namespace=namespace)


def fetch_reports_from_mlflow(
    git_repo: str = "",
    git_branch: str = "main",
    multi_repo: bool = False,
    namespace: str | None = None,
    workspace: str | None = None,
    project_slug: str = "",
    pipeline_run_id: str = "",
) -> dict[str, Any]:
    """Fetch migration + evaluation reports via ``services.fetch_reports``."""
    from services.fetch_reports import fetch_reports
    from utils.eval_utils import aggregate_evaluation_scores

    migration_report, evaluation_report, git_slug = fetch_reports(
        git_repo=git_repo,
        git_branch=git_branch,
        multi_repo=multi_repo,
        namespace=namespace,
        workspace=workspace,
        project_slug=project_slug,
        pipeline_run_id=pipeline_run_id,
    )
    evaluation_scores = aggregate_evaluation_scores(evaluation_report)
    return {
        "migration_report": migration_report,
        "analysis_report": migration_report,
        "evaluation_report": evaluation_report,
        "evaluation_scores": evaluation_scores,
        "git_slug": git_slug,
    }
