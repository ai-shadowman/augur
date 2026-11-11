"""Standalone FastAPI app serving the Code Understanding UI + v2 API."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from pathlib import Path

os.environ.setdefault("AUGUR_SERVER_MODE", "1")
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("LITELLM_TELEMETRY", "False")
os.environ.setdefault("DISABLE_LITELLM_VERSION_CHECK", "True")

_UI_DIR = str(Path(__file__).resolve().parent)
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
_CODE_UNDERSTANDING_DIR = str(
    Path(__file__).resolve().parent.parent / "workflows" / "examples" / "code_understanding"
)
for _p in (_CODE_UNDERSTANDING_DIR, _PROJECT_ROOT, _UI_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Pre-import mlflow on the main thread before FastAPI worker threads start.
# mlflow/__init__.py has an internal circular import chain (mlflow -> mlflow.genai ->
# mlflow.tracking -> mlflow.models -> mlflow.version) that can deadlock or leave a
# partially-initialized module in sys.modules if first imported concurrently by
# multiple request threads.
try:
    import mlflow  # noqa: E402,F401
    import mlflow.tracking  # noqa: E402,F401
    import mlflow.version  # noqa: E402,F401
except Exception:
    pass

from fastapi import FastAPI, HTTPException, Request, Response  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402

from api.pipelines import router as pipelines_router  # noqa: E402
from api.queries import router as queries_router  # noqa: E402
from api.repositories import router as repositories_router  # noqa: E402

try:
    from ui.cluster import (
        NAMESPACE_COOKIE,
        get_active_namespace,
        get_active_workspace,
        list_namespaces,
    )
    from ui.indexes import list_indexes_and_repos, queryable_options
except ImportError:
    from cluster import (  # type: ignore[no-redef]
        NAMESPACE_COOKIE,
        get_active_namespace,
        get_active_workspace,
        list_namespaces,
    )
    from indexes import list_indexes_and_repos, queryable_options  # type: ignore[no-redef]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
_SA_CA_CERT = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

# shortcut: in-memory 60s token cache (128 entries), move to shared cache only if k8s API rate-limits
_TOKEN_CACHE_TTL_SECONDS = 60.0
_TOKEN_CACHE_MAX = 128
_TOKEN_CACHE: OrderedDict[str, float] = OrderedDict()
_TOKEN_CACHE_LOCK = threading.Lock()


def _console_origin() -> str:
    return os.environ.get("OPENSHIFT_CONSOLE_URL", "").strip().rstrip("/")


def _allowed_origins() -> list[str]:
    origin = _console_origin()
    return [origin] if origin else ["http://localhost:8080", "http://127.0.0.1:8080"]


def _frame_ancestors_csp() -> str:
    origin = _console_origin()
    return f"frame-ancestors 'self' {origin}" if origin else "frame-ancestors 'self'"


def _require_openshift_auth() -> bool:
    return os.environ.get("REQUIRE_OPENSHIFT_AUTH", "").strip().lower() in ("1", "true", "yes")


def _extract_bearer_token(request: Request) -> str:
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("x-forwarded-access-token") or "").strip()


def _verify_openshift_token(token: str) -> bool:
    if not token:
        return False
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.monotonic()
    with _TOKEN_CACHE_LOCK:
        exp = _TOKEN_CACHE.get(digest)
        if exp is not None and now < exp:
            _TOKEN_CACHE.move_to_end(digest)
            return True

    api_base = os.environ.get("KUBERNETES_API_URL", "https://kubernetes.default.svc").rstrip("/")
    ssl_ctx = (
        ssl.create_default_context(cafile=_SA_CA_CERT)
        if os.path.exists(_SA_CA_CERT)
        else ssl.create_default_context()
    )
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    verified = False
    try:
        req = urllib.request.Request(f"{api_base}/apis/user.openshift.io/v1/users/~", headers=headers)
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            verified = resp.status == 200 and bool((data.get("metadata") or {}).get("name"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            try:
                ssr_body = b'{"apiVersion":"authentication.k8s.io/v1","kind":"SelfSubjectReview"}'
                ssr_req = urllib.request.Request(
                    f"{api_base}/apis/authentication.k8s.io/v1/selfsubjectreviews",
                    data=ssr_body,
                    headers={**headers, "Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(ssr_req, context=ssl_ctx, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    verified = resp.status in (200, 201) and bool(
                        ((data.get("status") or {}).get("userInfo") or {}).get("username")
                    )
            except Exception:
                verified = False
    except Exception:
        logger.debug("OpenShift token verification failed", exc_info=True)
        verified = False

    if verified:
        with _TOKEN_CACHE_LOCK:
            _TOKEN_CACHE[digest] = time.monotonic() + _TOKEN_CACHE_TTL_SECONDS
            _TOKEN_CACHE.move_to_end(digest)
            while len(_TOKEN_CACHE) > _TOKEN_CACHE_MAX:
                _TOKEN_CACHE.popitem(last=False)
    return verified


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (
            _require_openshift_auth()
            and path.startswith("/api/")
            and path != "/api/health"
            and request.method != "OPTIONS"
        ):
            token = _extract_bearer_token(request)
            if not _verify_openshift_token(token):
                resp = JSONResponse({"detail": "Unauthorized"}, status_code=401)
                resp.headers["Content-Security-Policy"] = _frame_ancestors_csp()
                return resp

        response = await call_next(request)
        response.headers["Content-Security-Policy"] = _frame_ancestors_csp()
        return response


app = FastAPI(title="Code Understanding Console", version="0.1.0")
app.add_middleware(SecurityMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRFToken"],
)
app.include_router(pipelines_router)
app.include_router(queries_router)
app.include_router(repositories_router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class SelectNamespaceRequest(BaseModel):
    namespace: str = Field(..., min_length=1)


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
    return {
        "ok": True,
        "namespace": get_active_namespace(request),
        "workspace": get_active_workspace(request),
    }


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


@app.get("/api/indexes", tags=["indexes"])
def api_indexes(request: Request) -> dict:
    ns = get_active_namespace(request)
    ws = get_active_workspace(request)
    indexes, repos = list_indexes_and_repos(namespace=ns, workspace=ws)
    default_key = f"{repos[0]['git_repo']}|{repos[0]['git_branch']}" if repos else None
    return {
        "namespace": ns,
        "workspace": ws,
        "indexes": indexes,
        "queryable": queryable_options(indexes),
        "repos": repos,
        "default_key": default_key,
    }
