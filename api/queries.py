"""FastAPI router for ad-hoc GraphRAG queries."""

from __future__ import annotations

import logging
import sys
import threading
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

_CODE_UNDERSTANDING_DIR = str(
    Path(__file__).resolve().parent.parent / "workflows" / "examples" / "code_understanding"
)
if _CODE_UNDERSTANDING_DIR not in sys.path:
    sys.path.insert(0, _CODE_UNDERSTANDING_DIR)

from services.run_adhoc_query import execute_adhoc_query  # noqa: E402
try:
    from ui.cluster import get_active_namespace, get_active_workspace
except ImportError:
    from cluster import get_active_namespace, get_active_workspace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/queries", tags=["queries"])

# shortcut: in-memory 50-entry query cache, move to shared store if multi-replica async polling is added
_QUERY_RESULTS_MAX = 50
_QUERY_RESULTS: OrderedDict[str, dict[str, Any]] = OrderedDict()
_QUERY_RESULTS_LOCK = threading.Lock()


def _store_query_result(query_id: str, payload: dict[str, Any]) -> None:
    with _QUERY_RESULTS_LOCK:
        _QUERY_RESULTS[query_id] = payload
        _QUERY_RESULTS.move_to_end(query_id)
        while len(_QUERY_RESULTS) > _QUERY_RESULTS_MAX:
            _QUERY_RESULTS.popitem(last=False)


class AdhocQueryRequest(BaseModel):
    question: str = Field(..., min_length=1, description="Natural-language question to ask")
    git_repo: str = Field(
        default="",
        description="Git repo URL; leave empty (or set multi_repo=true) to query the multi-repo index",
    )
    git_branch: str = Field(default="main", description="Branch of the indexed repo")
    multi_repo: bool = Field(default=False, description="Query the multi-repo index")
    use_global: bool = Field(default=True, description="Use GraphRAG global search")
    retry_count: int = Field(default=3, ge=1, le=10)
    pipeline_run_id: str = Field(default="", description="Optional KFP run ID to scope the index")
    project_slug: str = Field(default="", description="Optional project slug for multi-repo index")


class AdhocQueryResponse(BaseModel):
    query_id: str = ""
    status: str = "succeeded"
    question: str
    answer: str
    result: str = ""
    git_repo: str = ""
    git_branch: str = "main"
    multi_repo: bool = False
    use_global: bool = True
    project_slug: str = ""


@router.post("", response_model=AdhocQueryResponse)
def post_adhoc_query(body: AdhocQueryRequest, request: Request) -> AdhocQueryResponse:
    """Run an ad-hoc question against the GraphRAG index."""
    ns = get_active_namespace(request)
    ws = get_active_workspace(request)
    query_id = str(uuid.uuid4())
    try:
        answer = execute_adhoc_query(
            question=body.question,
            retry_count=body.retry_count,
            use_global=body.use_global,
            git_repo=body.git_repo,
            git_branch=body.git_branch,
            multi_repo=body.multi_repo,
            namespace=ns,
            workspace=ws,
            pipeline_run_id=body.pipeline_run_id,
            project_slug=body.project_slug,
        )
    except Exception as exc:
        logger.exception("Ad-hoc query failed")
        _store_query_result(
            query_id,
            {"query_id": query_id, "status": "failed", "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    payload = {
        "query_id": query_id,
        "status": "succeeded",
        "question": body.question,
        "answer": answer,
        "result": answer,
        "git_repo": body.git_repo,
        "git_branch": body.git_branch,
        "multi_repo": body.multi_repo or not body.git_repo,
        "use_global": body.use_global,
        "project_slug": body.project_slug,
    }
    _store_query_result(query_id, payload)
    return AdhocQueryResponse(**payload)


@router.get("/{query_id}")
def get_adhoc_query_status(query_id: str) -> dict[str, Any]:
    """Return status/result of a previously submitted ad-hoc query."""
    with _QUERY_RESULTS_LOCK:
        item = _QUERY_RESULTS.get(query_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Query '{query_id}' not found")
    return item
