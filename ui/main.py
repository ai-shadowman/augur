"""Standalone FastAPI app serving the Code Understanding UI + v2 API."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_UI_DIR = str(Path(__file__).resolve().parent)
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
for _p in (_PROJECT_ROOT, _UI_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi import FastAPI, HTTPException, Request, Response  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402

from api.pipelines import router as pipelines_router  # noqa: E402
from api.queries import router as queries_router  # noqa: E402
from api.repositories import router as repositories_router  # noqa: E402

try:
    from ui.cluster import (
        NAMESPACE_COOKIE,
        WORKSPACE_COOKIE,
        default_namespace,
        default_workspace,
        get_active_namespace,
        get_active_workspace,
        get_cluster_info,
        list_namespaces,
        list_workspaces,
        sanitize_workspace,
    )
    from ui.indexes import list_indexed_repos, queryable_options
except ImportError:
    from cluster import (  # type: ignore[no-redef]
        NAMESPACE_COOKIE,
        WORKSPACE_COOKIE,
        default_namespace,
        default_workspace,
        get_active_namespace,
        get_active_workspace,
        get_cluster_info,
        list_namespaces,
        list_workspaces,
        sanitize_workspace,
    )
    from indexes import list_indexed_repos, queryable_options  # type: ignore[no-redef]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class FrameAncestorsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = "frame-ancestors *"
        response.headers["X-Frame-Options"] = "ALLOWALL"
        return response


app = FastAPI(title="Code Understanding Console", version="0.1.0")
app.add_middleware(FrameAncestorsMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(pipelines_router)
app.include_router(queries_router)
app.include_router(repositories_router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class SelectNamespaceRequest(BaseModel):
    namespace: str = Field(..., min_length=1)


class SelectWorkspaceRequest(BaseModel):
    workspace: str = Field(default="default")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz", tags=["meta"])
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/health", tags=["meta"])
def api_health(request: Request) -> dict[str, str]:
    return {
        "status": "ok",
        "namespace": get_active_namespace(request),
        "workspace": get_active_workspace(request),
    }


@app.get("/api/status", tags=["cluster"])
def api_status(request: Request) -> dict:
    active_ns = get_active_namespace(request)
    active_ws = get_active_workspace(request)
    return {
        "ok": True,
        "namespace": active_ns,
        "workspace": active_ws,
        "cluster": get_cluster_info(),
    }


@app.get("/api/cluster", tags=["cluster"])
def api_cluster(request: Request) -> dict:
    info = get_cluster_info()
    info["active_namespace"] = get_active_namespace(request)
    info["default_namespace"] = default_namespace()
    info["active_workspace"] = get_active_workspace(request)
    info["default_workspace"] = default_workspace()
    return info


@app.get("/api/namespaces", tags=["cluster"])
def api_namespaces(request: Request) -> dict:
    active = get_active_namespace(request)
    items = list_namespaces()
    if active and active not in items:
        items = sorted({*items, active})
    return {"active": active, "current": active, "namespaces": items}


@app.post("/api/namespace", tags=["cluster"])
def api_set_namespace(body: SelectNamespaceRequest, response: Response) -> dict:
    ns = body.namespace.strip()
    if not ns:
        raise HTTPException(status_code=400, detail="namespace must be non-empty")
    response.set_cookie(
        key=NAMESPACE_COOKIE,
        value=ns,
        httponly=False,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return {"active": ns}


@app.get("/api/workspaces", tags=["cluster"])
def api_workspaces(request: Request) -> dict:
    ns = get_active_namespace(request)
    active = get_active_workspace(request)
    items = list_workspaces(namespace=ns)
    if active and active not in items:
        items = ["default", *sorted({*(w for w in items if w != "default"), active})]
    return {"active": active, "current": active, "workspaces": items}


@app.post("/api/workspace", tags=["cluster"])
def api_set_workspace(body: SelectWorkspaceRequest, response: Response) -> dict:
    ws = sanitize_workspace(body.workspace)
    response.set_cookie(
        key=WORKSPACE_COOKIE,
        value=ws,
        httponly=False,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return {"active": ws}


@app.get("/api/indexes", tags=["indexes"])
def api_indexes(request: Request) -> dict:
    ns = get_active_namespace(request)
    ws = get_active_workspace(request)
    indexes = list_indexed_repos(namespace=ns, workspace=ws)
    return {
        "namespace": ns,
        "workspace": ws,
        "indexes": indexes,
        "queryable": queryable_options(indexes),
    }
