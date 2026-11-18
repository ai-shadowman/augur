"""FastAPI router for reading and updating the repository list."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

_CODE_UNDERSTANDING_DIR = str(
    Path(__file__).resolve().parent.parent / "workflows" / "examples" / "code_understanding"
)
if _CODE_UNDERSTANDING_DIR not in sys.path:
    sys.path.insert(0, _CODE_UNDERSTANDING_DIR)

from services.get_catalog_repos import get_catalog_repos as fetch_catalog_repos  # noqa: E402
from services.update_repos import log_repos as _log_repos  # noqa: E402
try:
    from ui.cluster import get_active_namespace, get_active_workspace
except ImportError:
    from cluster import get_active_namespace, get_active_workspace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/repositories", tags=["repositories"])


class RepoItem(BaseModel):
    git_repo: str = Field(..., min_length=1, description="Git repository URL")
    git_branch: str = Field(default="main", description="Branch to clone")
    git_username: str | None = Field(default=None, description="Username for private repository")
    git_token: str | None = Field(default=None, description="Git token for private repository")


class UpdateReposRequest(BaseModel):
    repos: list[RepoItem] = Field(..., min_length=1, description="Repositories to log")


def _serialize_repo_item(r: RepoItem) -> dict[str, str]:
    username = (r.git_username or "").strip()
    token = (r.git_token or "").strip()
    if (r.git_username is not None or r.git_token is not None) and not token:
        raise HTTPException(
            status_code=422,
            detail="git_token is required for private repositories.",
        )
    item: dict[str, str] = {"git_repo": r.git_repo, "git_branch": r.git_branch}
    if token:
        item["git_username"] = username
        item["git_token"] = token
    return item


@router.get("", response_model=list[RepoItem], response_model_exclude_none=True)
def get_repositories(request: Request) -> list[RepoItem]:
    """Return the current repository list from the asset store (omitting secret tokens)."""
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
            git_username=(
                str(r.get("git_username") or "")
                if (r.get("git_token") or "git_username" in r)
                else None
            ),
        )
        for r in items
        if isinstance(r, dict) and r.get("git_repo")
    ]


@router.get("/catalog-repos")
def get_catalog_repositories_envelope(request: Request) -> dict[str, Any]:
    """Return the repository catalog wrapped in ``{"repos": [...], "default_key": ...}``."""
    repos = [
        {k: v for k, v in r.model_dump().items() if v is not None}
        for r in get_repositories(request)
    ]
    default_key = f"{repos[0]['git_repo']}|{repos[0]['git_branch']}" if repos else None
    return {"repos": repos, "default_key": default_key}


@router.post("", response_model=list[RepoItem], response_model_exclude_none=True)
def post_repositories(body: UpdateReposRequest, request: Request) -> list[RepoItem]:
    """Log the given repos to the asset store and return the updated catalog."""
    ns = get_active_namespace(request)
    ws = get_active_workspace(request)
    repo_dicts = [_serialize_repo_item(r) for r in body.repos]
    try:
        _log_repos(repo_dicts, namespace=ns, workspace=ws)
    except Exception as exc:
        logger.exception("Failed to log repository list")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return get_repositories(request)
