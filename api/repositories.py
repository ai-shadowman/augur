"""FastAPI router for reading and updating the repository list."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from api.repositories_service import fetch_catalog_repos
from api.repositories_service import log_repos as _log_repos
from ui.cluster import get_active_namespace, get_active_workspace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/repositories", tags=["repositories"])


class RepoItem(BaseModel):
    git_repo: str = Field(..., min_length=1, description="Git repository URL")
    git_branch: str = Field(default="main", description="Branch to clone")


class UpdateReposRequest(BaseModel):
    repos: list[RepoItem] = Field(..., min_length=1, description="Repositories to log")


@router.get("", response_model=list[RepoItem])
def get_repositories(request: Request) -> list[RepoItem]:
    """Return the current repository list from the asset store."""
    ns = get_active_namespace(request)
    ws = get_active_workspace(request)
    try:
        items = fetch_catalog_repos(namespace=ns, workspace=ws)
    except Exception as exc:
        logger.exception("Failed to fetch repository list")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return [
        RepoItem(
            git_repo=r.get("git_repo", ""),
            git_branch=r.get("git_branch", "main"),
        )
        for r in items
        if isinstance(r, dict) and r.get("git_repo")
    ]


@router.get("/catalog-repos")
def get_catalog_repositories_envelope(request: Request) -> dict[str, Any]:
    """Return the repository catalog wrapped in ``{"repos": [...], "default_key": ...}``."""
    repos = [r.model_dump() for r in get_repositories(request)]
    default_key = f"{repos[0]['git_repo']}|{repos[0]['git_branch']}" if repos else None
    return {"repos": repos, "default_key": default_key}


@router.post("", response_model=list[RepoItem])
def post_repositories(body: UpdateReposRequest, request: Request) -> list[RepoItem]:
    """Log the given repos to the asset store and return the updated catalog."""
    ns = get_active_namespace(request)
    ws = get_active_workspace(request)
    repo_dicts = [{"git_repo": r.git_repo, "git_branch": r.git_branch} for r in body.repos]
    try:
        _log_repos(repo_dicts, namespace=ns, workspace=ws)
    except Exception as exc:
        logger.exception("Failed to log repository list")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return get_repositories(request)
