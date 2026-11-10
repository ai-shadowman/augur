"""Thin bridge between the FastAPI layer and the ad-hoc query service."""

from __future__ import annotations

import sys
from pathlib import Path

_CODE_UNDERSTANDING_DIR = str(
    Path(__file__).resolve().parent.parent / "workflows" / "examples" / "code_understanding"
)
if _CODE_UNDERSTANDING_DIR not in sys.path:
    sys.path.insert(0, _CODE_UNDERSTANDING_DIR)


def run_adhoc_query(
    question: str,
    retry_count: int = 3,
    use_global: bool = True,
    git_repo: str = "",
    git_branch: str = "main",
    multi_repo: bool = False,
    namespace: str | None = None,
    workspace: str | None = None,
    pipeline_run_id: str = "",
    project_slug: str = "",
) -> str:
    """Execute an ad-hoc GraphRAG query via ``services.run_adhoc_query``."""
    from services.run_adhoc_query import execute_adhoc_query

    return execute_adhoc_query(
        question=question,
        retry_count=retry_count,
        use_global=use_global,
        git_repo=git_repo,
        git_branch=git_branch,
        multi_repo=multi_repo,
        namespace=namespace,
        workspace=workspace,
        pipeline_run_id=pipeline_run_id,
        project_slug=project_slug,
    )
