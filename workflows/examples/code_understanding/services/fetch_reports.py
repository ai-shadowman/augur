"""Fetch migration and evaluation reports from MLflow."""

from __future__ import annotations

import logging
import os

from loaders.default_asset_loader import DefaultAssetLoader
from pipelines.base.data_generation import generate_git_slug
from utils.pipeline_utils import resolve_mlflow_prefix, sanitize_slug

logger = logging.getLogger(__name__)


def fetch_reports(
    git_repo: str = "",
    git_branch: str = "main",
    multi_repo: bool = False,
    namespace: str | None = None,
    workspace: str | None = None,
    project_slug: str = "",
    pipeline_run_id: str = "",
) -> tuple[str | None, str | None, str]:
    """Download migration report (markdown) and evaluation report (CSV) from MLflow.

    Returns:
        ``(migration_report_md, evaluation_report_csv, git_slug)``
    """
    use_multi_repo = multi_repo or not git_repo
    clean_project_slug = sanitize_slug(project_slug)
    git_slug = (
        clean_project_slug
        if (use_multi_repo and clean_project_slug)
        else (generate_git_slug(git_repo, git_branch) if git_repo else clean_project_slug)
    )
    loader = DefaultAssetLoader(namespace=namespace, workspace=workspace)

    migration_report: str | None = None
    candidate_slugs = [git_slug] if git_slug else []
    if use_multi_repo and "" not in candidate_slugs:
        candidate_slugs.append("")

    for cand_slug in candidate_slugs:
        try:
            report_filename = (
                f"migration_report_{cand_slug}.md" if cand_slug else "migration_report.md"
            )
            artifact_dir = DefaultAssetLoader.get_log_results_artifact_path(
                DefaultAssetLoader.RESULTS_PATH_PREFIX_PIPELINES,
                git_slug=cand_slug,
                multi_repo=use_multi_repo,
            )
            asset_file_path = f"{artifact_dir}/{report_filename}"
            base_tags = {
                "git_slug": str(cand_slug or "multi-repo"),
                "multi_repo": str(use_multi_repo),
                "category": "analysis",
            }
            tag_sets = []
            if pipeline_run_id:
                tag_sets.append({**base_tags, "pipeline_run_id": pipeline_run_id})
            tag_sets.append(base_tags)

            for tags in tag_sets:
                content = loader.download(
                    asset_file_path,
                    experiment_name=loader._loader.RESULT_ASSET_EXPERIMENT,
                    asset_tags=tags,
                )
                if content:
                    migration_report = str(content)
                    git_slug = cand_slug or git_slug
                    break
            if migration_report:
                break
        except Exception:
            logger.warning(
                "Could not fetch migration report for git_slug=%r multi_repo=%s",
                cand_slug,
                use_multi_repo,
                exc_info=True,
            )

    evaluation_report: str | None = None
    if not use_multi_repo and git_slug:
        try:
            eval_filename = f"code_understanding_results_{git_slug}.csv"
            eval_artifact_dir = DefaultAssetLoader.get_log_results_artifact_path(
                DefaultAssetLoader.RESULTS_PATH_PREFIX_EVAL,
                git_slug=git_slug,
                multi_repo=False,
            )
            eval_asset_path = f"{eval_artifact_dir}/{eval_filename}"
            base_eval_tags = {
                "category": "evaluation",
                "git_slug": git_slug,
            }
            eval_tag_sets = [
                {**base_eval_tags, "multi_repo": str(False)},
                base_eval_tags,
            ]
            for eval_tags in eval_tag_sets:
                content = loader.download(
                    eval_asset_path,
                    experiment_name=loader._loader.RESULT_ASSET_EXPERIMENT,
                    asset_tags=eval_tags,
                )
                if content:
                    evaluation_report = str(content)
                    break
        except Exception:
            logger.warning(
                "Could not fetch evaluation report for git_slug=%r",
                git_slug,
                exc_info=True,
            )

    return migration_report, evaluation_report, git_slug
