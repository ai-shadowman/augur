"""Execute an ad-hoc GraphRAG query against a pre-built index."""

from __future__ import annotations

from pipelines.base.analysis import AnalysisPipeline


def execute_adhoc_query(
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
    """Run an ad-hoc question against the GraphRAG index via ``AnalysisPipeline``."""
    answer = AnalysisPipeline().run_adhoc_query(
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
    return (answer or "").strip()
