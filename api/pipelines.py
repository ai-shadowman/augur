"""FastAPI router for triggering KFP pipeline runs and retrieving results."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api.pipeline_service import (
    fetch_reports_from_mlflow,
    get_run_info,
    get_run_status,
    list_runs,
    submit_pipeline_run,
)
try:
    from ui.cluster import get_active_namespace, get_active_workspace
except ImportError:
    from cluster import get_active_namespace, get_active_workspace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/pipelines", tags=["pipelines"])


def _ui_status_label(raw_status: str) -> str:
    """Convert uppercase KFP status into UI badge label ('Succeeded', 'Running', 'Failed', etc.)."""
    s = (raw_status or "PENDING").upper()
    if s in ("SUCCEEDED", "COMPLETE", "COMPLETED"):
        return "Succeeded"
    if s in ("SKIPPED",):
        return "Skipped"
    if s in ("FAILED", "ERROR"):
        return "Failed"
    if s in ("CANCELLED", "CANCELED"):
        return "Cancelled"
    if s in ("RUNNING", "IN_PROGRESS"):
        return "Running"
    return "Pending"


class RepoEntry(BaseModel):
    git_repo: str = Field(..., min_length=1, description="Git repository URL")
    git_branch: str = Field(default="main", description="Branch to clone")


class TriggerPipelineRequest(BaseModel):
    repos: list[RepoEntry] = Field(
        default_factory=list,
        description="1 repo -> single_repo pipeline; 2+ (or empty on report re-run) -> multi_repo pipeline",
    )
    pipeline_run_id: str = Field(
        default="",
        description="KFP run ID of a prior full run; when set, skips data-gen and indexing (analysis-only).",
    )
    project_slug: str = Field(
        default="",
        description="Optional multi-repo project slug; auto-derived from repos if omitted.",
    )


class TriggerPipelineResponse(BaseModel):
    job_id: str
    run_name: str
    pipeline_name: str
    status: str = "SUBMITTED"
    project_slug: str = ""
    project_workspace: str = "default"


class RunSummary(BaseModel):
    job_id: str
    run_id: str = ""
    run_name: str
    name: str = ""
    status: str
    git_repo: str = ""
    git_branch: str = ""
    multi_repo: bool = False
    project_slug: str = ""
    project_workspace: str = ""
    repos: list[RepoEntry] = []
    created_at: str | None = None
    start_time: str | None = None
    finished_at: str | None = None


class EvaluationMetric(BaseModel):
    name: str
    average: float
    pass_rate_3: float | None = None
    pass_rate_4: float | None = None


class EvaluationScores(BaseModel):
    metrics: list[EvaluationMetric]
    average_pass_rate_3: float
    average_pass_rate_4: float
    total_rows: int


class PipelineStatusResponse(BaseModel):
    job_id: str
    run_id: str = ""
    status: str
    git_slug: str = ""
    project_slug: str = ""
    migration_report: str | None = None
    analysis_report: str | None = None
    evaluation_report: str | None = None
    evaluation_scores: EvaluationScores | None = None
    error: str | None = None


def _enrich_run_summary(r: dict[str, Any], ui_status: bool = False) -> dict[str, Any]:
    job_id = r.get("job_id") or r.get("run_id") or ""
    run_name = r.get("run_name") or r.get("name") or job_id
    created = r.get("created_at") or r.get("start_time")
    raw_status = r.get("status", "PENDING")
    return {
        **r,
        "job_id": job_id,
        "run_id": job_id,
        "run_name": run_name,
        "name": run_name,
        "status": _ui_status_label(raw_status) if ui_status else raw_status,
        "created_at": created,
        "start_time": created,
    }


@router.get("", response_model=list[RunSummary])
def get_recent_runs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[RunSummary]:
    """Return the most recent KFP runs in the active namespace and workspace."""
    ns = get_active_namespace(request)
    ws = get_active_workspace(request)
    try:
        items = list_runs(limit=limit, namespace=ns, workspace=ws)
    except Exception as exc:
        logger.exception("Failed to list KFP runs")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return [RunSummary(**_enrich_run_summary(r, ui_status=False)) for r in items]


@router.get("/runs")
def get_recent_runs_envelope(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    """Return recent KFP runs wrapped in ``{"runs": [...]}`` for the Console UI."""
    ns = get_active_namespace(request)
    ws = get_active_workspace(request)
    try:
        items = list_runs(limit=limit, namespace=ns, workspace=ws)
    except Exception as exc:
        logger.exception("Failed to list KFP runs")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"runs": [_enrich_run_summary(r, ui_status=True) for r in items]}


@router.post("", response_model=TriggerPipelineResponse, status_code=202)
def trigger_pipeline(
    body: TriggerPipelineRequest,
    request: Request,
) -> TriggerPipelineResponse:
    """Submit a single-repo or multi-repo KFP pipeline run."""
    if not body.repos and not body.pipeline_run_id:
        raise HTTPException(status_code=422, detail="At least one repository is required.")
    ns = get_active_namespace(request)
    ws = get_active_workspace(request)
    repo_dicts = [{"git_repo": r.git_repo, "git_branch": r.git_branch} for r in body.repos]
    try:
        info = submit_pipeline_run(
            repo_dicts,
            namespace=ns,
            workspace=ws,
            pipeline_run_id=body.pipeline_run_id,
            project_slug=body.project_slug,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to submit pipeline run")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return TriggerPipelineResponse(
        job_id=info["job_id"],
        run_name=info["run_name"],
        pipeline_name=info["pipeline_name"],
        project_slug=info.get("project_slug", ""),
        project_workspace=info.get("project_workspace", ws or "default"),
    )


def _build_status_response(
    job_id: str,
    ns: str,
    ws: str,
    git_repo: str = "",
    git_branch: str = "main",
    multi_repo: bool = False,
    project_slug: str = "",
    lowercase_status: bool = False,
) -> PipelineStatusResponse:
    try:
        if not git_repo and not multi_repo and not project_slug:
            try:
                info = get_run_info(job_id, namespace=ns)
                state = info["state"]
                git_repo = info.get("git_repo", "")
                git_branch = info.get("git_branch", "main")
                multi_repo = bool(info.get("multi_repo", False))
                project_slug = info.get("project_slug", "")
            except ValueError:
                raise
            except Exception:
                state = get_run_status(job_id, namespace=ns)
        else:
            state = get_run_status(job_id, namespace=ns)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to get KFP run status for %s", job_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    norm_status = _ui_status_label(state).lower() if lowercase_status else state
    resp: dict[str, Any] = {
        "job_id": job_id,
        "run_id": job_id,
        "status": norm_status,
        "project_slug": project_slug,
    }

    if state in ("SUCCEEDED", "SKIPPED"):
        try:
            reports = fetch_reports_from_mlflow(
                git_repo=git_repo,
                git_branch=git_branch,
                multi_repo=multi_repo,
                namespace=ns,
                workspace=ws,
                project_slug=project_slug,
                pipeline_run_id=job_id,
            )
            resp.update(reports)
        except Exception as exc:
            logger.exception("Failed to fetch reports from MLflow for run %s", job_id)
            resp["error"] = f"Run succeeded, but fetching reports failed: {exc}"
    elif state in ("FAILED", "ERROR", "CANCELLED", "CANCELED"):
        resp["error"] = f"KFP run ended in state {state}"

    return PipelineStatusResponse(**resp)


@router.get("/runs/{job_id}", response_model=PipelineStatusResponse)
def get_pipeline_run_detail(
    job_id: str,
    request: Request,
    git_repo: str = Query(default=""),
    git_branch: str = Query(default="main"),
    multi_repo: bool = Query(default=False),
    project_slug: str = Query(default=""),
) -> PipelineStatusResponse:
    """Return run status (lowercase for UI polling) and reports from MLflow."""
    ns = get_active_namespace(request)
    ws = get_active_workspace(request)
    return _build_status_response(
        job_id=job_id,
        ns=ns,
        ws=ws,
        git_repo=git_repo,
        git_branch=git_branch,
        multi_repo=multi_repo,
        project_slug=project_slug,
        lowercase_status=True,
    )


@router.get("/{job_id}", response_model=PipelineStatusResponse)
def get_pipeline_status(
    job_id: str,
    request: Request,
    git_repo: str = Query(default="", description="Repo URL (for single-repo report lookup)"),
    git_branch: str = Query(default="main", description="Branch (for single-repo report lookup)"),
    multi_repo: bool = Query(default=False, description="True for multi-repo runs"),
    project_slug: str = Query(default="", description="Optional project slug for multi-repo runs"),
) -> PipelineStatusResponse:
    """Return the KFP run status and, when SUCCEEDED, the reports from MLflow."""
    ns = get_active_namespace(request)
    ws = get_active_workspace(request)
    return _build_status_response(
        job_id=job_id,
        ns=ns,
        ws=ws,
        git_repo=git_repo,
        git_branch=git_branch,
        multi_repo=multi_repo,
        project_slug=project_slug,
        lowercase_status=False,
    )
