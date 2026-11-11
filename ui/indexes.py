"""Discover which repositories have GraphRAG indexes available in MLflow."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

_CODE_UNDERSTANDING_DIR = str(
    Path(__file__).resolve().parent.parent / "workflows" / "examples" / "code_understanding"
)
if _CODE_UNDERSTANDING_DIR not in sys.path:
    sys.path.insert(0, _CODE_UNDERSTANDING_DIR)

logger = logging.getLogger(__name__)


def _load_configured_repos(
    namespace: str | None = None,
    workspace: str | None = None,
) -> list[dict[str, str]]:
    """Return the configured repo list via ``services.get_catalog_repos``."""
    try:
        from services.get_catalog_repos import get_catalog_repos

        data = get_catalog_repos(namespace=namespace, workspace=workspace)
        if isinstance(data, list):
            return [
                {
                    "git_repo": str(item.get("git_repo", "")),
                    "git_branch": str(item.get("git_branch", "main")),
                }
                for item in data
                if isinstance(item, dict) and item.get("git_repo")
            ]
    except Exception:
        logger.debug("Could not load catalog repos via service", exc_info=True)
    return []


def _slug_for(repo_url: str, branch: str) -> str:
    try:
        from utils.code_utils import generate_slug_from_repo

        return generate_slug_from_repo(repo_url, branch)
    except Exception:
        name = repo_url.rstrip("/").split("/")[-1].removesuffix(".git")
        return f"{name}-{branch}".lower()


def _query_mlflow_indexed_runs(
    namespace: str,
    workspace: str | None = None,
) -> dict[tuple[str, bool], dict[str, Any]]:
    """Return a map of ``(git_slug, multi_repo) -> run_info`` from MLflow."""
    from loaders.default_asset_loader import DefaultAssetLoader
    from loaders.mlflow_asset_loader import MlFlowAssetLoader, _ensure_mlflow_imported

    _ensure_mlflow_imported()
    from mlflow.tracking import MlflowClient

    loader = DefaultAssetLoader(namespace=namespace, workspace=workspace)
    if not isinstance(loader._loader, MlFlowAssetLoader):
        return {}

    exp_name = loader._loader.RESULT_DIRECTORY_ASSET_EXPERIMENT
    client = MlflowClient()
    exp = client.get_experiment_by_name(exp_name)
    if not exp or exp.lifecycle_stage != "active":
        return {}

    runs = client.search_runs(
        experiment_ids=[exp.experiment_id],
        filter_string='tags."category" = \'indexing\'',
        order_by=["start_time DESC"],
        max_results=200,
    )

    out: dict[tuple[str, bool], dict[str, Any]] = {}
    for run in runs:
        tags = run.data.tags or {}
        slug = (tags.get("git_slug") or "").strip()
        multi_raw = (tags.get("multi_repo") or "False").strip().lower()
        is_multi = multi_raw in ("true", "1", "yes")
        norm_slug = ("" if slug == "multi-repo" else slug) if is_multi else slug
        key = (norm_slug, is_multi)
        if key in out:
            continue
        out[key] = {
            "run_id": run.info.run_id,
            "start_time": run.info.start_time,
            "artifact_uri": run.info.artifact_uri,
            "git_slug": norm_slug,
            "multi_repo": is_multi,
            "project_slug": norm_slug if is_multi else "",
            "pipeline_run_id": (tags.get("pipeline_run_id") or "").strip(),
        }
    return out


def list_indexed_repos(
    namespace: str | None = None,
    workspace: str | None = None,
    configured_repos: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Return the combined list of configured + indexed repos for *namespace* and *workspace*."""
    ns = (namespace or os.environ.get("KFP_NAMESPACE") or "demo").strip()
    configured = (
        configured_repos
        if configured_repos is not None
        else _load_configured_repos(namespace=ns, workspace=workspace)
    )

    try:
        indexed_map = _query_mlflow_indexed_runs(ns, workspace=workspace)
    except Exception:
        logger.debug("Could not query MLflow for indexed runs", exc_info=True)
        indexed_map = {}

    slug_to_cfg: dict[str, dict[str, str]] = {}
    for item in configured:
        slug = _slug_for(item["git_repo"], item["git_branch"])
        slug_to_cfg[slug] = item

    rows: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()

    for slug, item in slug_to_cfg.items():
        seen_slugs.add(slug)
        single_info = indexed_map.get((slug, False))
        rows.append(
            {
                "git_repo": item["git_repo"],
                "git_branch": item["git_branch"],
                "git_slug": slug,
                "project_slug": "",
                "multi_repo": False,
                "indexed": single_info is not None,
                "last_indexed_ms": single_info["start_time"] if single_info else None,
                "run_id": single_info["run_id"] if single_info else None,
            }
        )

    for (slug, is_multi), info in indexed_map.items():
        if is_multi or not slug or slug in seen_slugs:
            continue
        rows.append(
            {
                "git_repo": "",
                "git_branch": "",
                "git_slug": slug,
                "project_slug": "",
                "multi_repo": False,
                "indexed": True,
                "last_indexed_ms": info["start_time"],
                "run_id": info["run_id"],
            }
        )

    multi_entries = [
        (slug, info) for (slug, is_multi), info in indexed_map.items() if is_multi
    ]
    if multi_entries:
        for slug, multi_info in multi_entries:
            rows.append(
                {
                    "git_repo": "",
                    "git_branch": "",
                    "git_slug": slug or "multi-repo",
                    "project_slug": slug,
                    "multi_repo": True,
                    "indexed": True,
                    "last_indexed_ms": multi_info["start_time"],
                    "run_id": multi_info["run_id"],
                }
            )
    else:
        rows.append(
            {
                "git_repo": "",
                "git_branch": "",
                "git_slug": "multi-repo",
                "project_slug": "",
                "multi_repo": True,
                "indexed": False,
                "last_indexed_ms": None,
                "run_id": None,
            }
        )

    return rows


def list_indexes_and_repos(
    namespace: str | None = None,
    workspace: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Return ``(indexes, configured_repos)`` in a single catalog read."""
    ns = (namespace or os.environ.get("KFP_NAMESPACE") or "demo").strip()
    configured = _load_configured_repos(namespace=ns, workspace=workspace)
    return list_indexed_repos(namespace=ns, workspace=workspace, configured_repos=configured), configured


def queryable_options(indexes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter *indexes* down to entries that currently have a usable index."""
    opts: list[dict[str, Any]] = []
    for row in indexes:
        if not row.get("indexed"):
            continue
        if row.get("multi_repo"):
            proj_slug = row.get("project_slug") or ""
            label = (
                f"Multi-repository index ({proj_slug})"
                if proj_slug
                else "Multi-repository index (all repos)"
            )
            value = f"__multi__:{proj_slug}" if proj_slug else "__multi__"
            opts.append(
                {
                    "label": label,
                    "key": value,
                    "value": value,
                    "git_repo": "",
                    "git_branch": "main",
                    "git_slug": row.get("git_slug") or "multi-repo",
                    "project_slug": proj_slug,
                    "multi_repo": True,
                    "use_global": True,
                }
            )
        else:
            repo = row.get("git_repo") or ""
            branch = row.get("git_branch") or "main"
            slug = row.get("git_slug") or ""
            label = f"{repo} ({branch})" if repo else slug
            value = f"{repo}|{branch}" if repo else f"slug:{slug}"
            opts.append(
                {
                    "label": label,
                    "key": value,
                    "value": value,
                    "git_repo": repo,
                    "git_branch": branch,
                    "git_slug": slug,
                    "project_slug": "",
                    "multi_repo": False,
                    "use_global": True,
                }
            )
    return opts
