"""Fetch and normalise the state and parameters of a KFP run."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from utils.service_utils import create_client

logger = logging.getLogger(__name__)

_ACTIVE_RUN_STATES = frozenset({"RUNNING", "IN_PROGRESS", "PENDING", "QUEUED"})

_LEAF_STAGE_MAP: dict[str, tuple[int, str]] = {
    "benchmark-llm-op": (10, "Data Generation (LLM Benchmark)"),
    "get-repo-list-op": (15, "Data Generation (Loading Repos)"),
    "prepare-environment-op": (20, "Data Generation (Cloning Repo)"),
    "generate-code-and-meta-op": (30, "Data Generation"),
    "graphrag-indexing-op": (40, "GraphRAG Indexing"),
    "run-indexing-multi-repo-op": (40, "GraphRAG Indexing"),
    "graphrag-evaluation-op": (50, "Evaluation"),
    "generate-migration-report-op": (60, "Analysis"),
    "run-analysis-multi-repo-op": (60, "Analysis"),
}

_DAG_STAGE_MAP: dict[str, tuple[int, str]] = {
    "data-generation-pipeline": (25, "Data Generation"),
    "data-generation-multi-repo-pipeline": (25, "Data Generation"),
    "graphrag-indexing-pipeline": (40, "GraphRAG Indexing"),
    "graphrag-analysis-pipeline": (60, "Analysis"),
}

_WRAPPER_NAMES = frozenset(
    {
        "",
        "root",
        "single-repo-pipeline",
        "multi-repo-pipeline",
        "single-repo",
        "multi-repo",
        "run-pipeline",
        "run-pipeline-multi-repo",
        "driver",
        "system-dag-driver",
        "system-container-driver",
        "exit-handler",
    }
)
_WRAPPER_PREFIXES = ("for-loop", "condition", "parallelfor", "exit-handler")


def _safe_str(obj: Any, attr: str) -> str:
    """Safely read a string attribute or dict key without coercing MagicMocks."""
    val = obj.get(attr) if isinstance(obj, dict) else getattr(obj, attr, None)
    return val.strip() if isinstance(val, str) else ""


def _normalize_state(raw_state: Any) -> str:
    """Normalise KFP enum or string runtime state to uppercase token."""
    if raw_state is None:
        return ""
    name = getattr(raw_state, "name", None)
    text = name if isinstance(name, str) else (raw_state if isinstance(raw_state, str) else str(raw_state))
    token = text.strip().upper().split(".")[-1]
    if token in ("NONE", "RUNTIME_STATE_UNSPECIFIED"):
        return ""
    return token


def _normalize_task_name(raw_name: str) -> str:
    """Normalise a KFP task/component name to canonical hyphenated form."""
    cleaned = (raw_name or "").strip().lower().replace("_", "-")
    cleaned = re.sub(r"-\d+$", "", cleaned)
    return cleaned


def _is_structural_wrapper(norm_name: str) -> bool:
    if norm_name in _WRAPPER_NAMES:
        return True
    return any(norm_name.startswith(prefix) for prefix in _WRAPPER_PREFIXES)


def _humanize_task_name(norm_name: str) -> str:
    base = re.sub(r"-op$", "", norm_name).replace("-", " ").strip()
    return base.title() if base else ""


def _dedupe_labels(entries: list[tuple[int, str]], lowest_order_only: bool = False) -> str:
    if not entries:
        return ""
    sorted_entries = sorted(entries, key=lambda item: item[0])
    if lowest_order_only:
        min_order = sorted_entries[0][0]
        sorted_entries = [item for item in sorted_entries if item[0] == min_order]
    seen: list[str] = []
    for _, label in sorted_entries:
        if label and label not in seen:
            seen.append(label)
    return ", ".join(seen)


def _extract_pod_stage_fallback(run_id: str, namespace: str | None = None) -> str:
    """Inspect Kubernetes pods labeled ``pipeline/runid=<run_id>`` as a fallback."""
    ns = (namespace or os.environ.get("KFP_NAMESPACE", "")).strip()
    if not run_id or not ns:
        return ""
    try:
        from kubernetes import client as k8s_client
        from kubernetes import config as k8s_config

        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()

        pod_list = k8s_client.CoreV1Api().list_namespaced_pod(
            namespace=ns,
            label_selector=f"pipeline/runid={run_id}",
            _request_timeout=2,
        )
        pods = getattr(pod_list, "items", None) or []
        running_entries: list[tuple[int, str]] = []
        pending_entries: list[tuple[int, str]] = []
        for pod in pods:
            phase = (getattr(getattr(pod, "status", None), "phase", None) or "").strip().upper()
            if phase not in ("RUNNING", "PENDING"):
                continue
            spec = getattr(pod, "spec", None)
            containers = getattr(spec, "containers", None) or []
            candidates: list[str] = []
            for container in containers:
                for env_var in getattr(container, "env", None) or []:
                    if getattr(env_var, "name", None) == "KFP_TASK_NAME":
                        val = getattr(env_var, "value", None)
                        if isinstance(val, str) and val and not val.startswith("{{"):
                            candidates.append(val)
                tokens = list(getattr(container, "command", None) or []) + list(
                    getattr(container, "args", None) or []
                )
                for idx, tok in enumerate(tokens):
                    if not isinstance(tok, str):
                        continue
                    if tok == "--function_to_execute" and idx + 1 < len(tokens):
                        nxt = tokens[idx + 1]
                        if isinstance(nxt, str) and nxt:
                            candidates.append(nxt)
                    elif tok.startswith("--function_to_execute="):
                        candidates.append(tok.split("=", 1)[1])
            for raw in candidates:
                norm = _normalize_task_name(raw)
                mapped = _LEAF_STAGE_MAP.get(norm)
                if not mapped and not _is_structural_wrapper(norm) and norm not in _DAG_STAGE_MAP:
                    human = _humanize_task_name(norm)
                    if human:
                        mapped = (90, human)
                if mapped:
                    if phase == "RUNNING":
                        running_entries.append(mapped)
                    else:
                        pending_entries.append(mapped)
        if running_entries:
            return _dedupe_labels(running_entries)
        if pending_entries:
            return _dedupe_labels(pending_entries, lowest_order_only=True)
    except Exception:
        logger.debug("Kubernetes pod stage fallback unavailable for run %s", run_id, exc_info=True)
    return ""


def _unwrap_run(run_detail: Any) -> Any:
    """Return inner ``.run`` when wrapped, handling dicts and direct MagicMocks safely."""
    if isinstance(run_detail, dict):
        return run_detail.get("run") or run_detail
    inner = getattr(run_detail, "run", None)
    if inner is None:
        return run_detail
    if type(inner).__name__ == "MagicMock" and isinstance(getattr(run_detail, "state", None), str):
        return run_detail
    return inner


def extract_active_stage(
    run_obj: Any,
    run_state: str = "",
    run_id: str = "",
    namespace: str | None = None,
) -> str:
    """Determine the human-readable active pipeline stage for an in-progress KFP run.

    Returns ``""`` when the run is not in an active/pending state.
    """
    actual_run = _unwrap_run(run_obj)

    state = (run_state or "").strip().upper()
    if not state:
        raw_st = (
            actual_run.get("state") or actual_run.get("runtime_state")
            if isinstance(actual_run, dict)
            else getattr(actual_run, "state", None) or getattr(actual_run, "runtime_state", None)
        )
        state = _normalize_state(raw_st) or "PENDING"

    if state not in _ACTIVE_RUN_STATES:
        return ""

    resolved_run_id = run_id or _safe_str(actual_run, "run_id") or _safe_str(actual_run, "id")

    task_details: list[Any] = []
    for candidate in (actual_run, run_obj):
        if candidate is None:
            continue
        rd = candidate.get("run_details") if isinstance(candidate, dict) else getattr(candidate, "run_details", None)
        if rd is None:
            continue
        tds = rd.get("task_details") if isinstance(rd, dict) else getattr(rd, "task_details", None)
        if isinstance(tds, (list, tuple)) and tds:
            task_details = list(tds)
            break

    if task_details:
        parent_ids = {
            pid
            for td in task_details
            if (pid := _safe_str(td, "parent_task_id"))
        }

        running_leaf: list[tuple[int, str]] = []
        pending_leaf: list[tuple[int, str]] = []
        active_dag: list[tuple[int, str]] = []
        completed_leaf_orders: list[int] = []

        for td in task_details:
            raw_name = _safe_str(td, "display_name") or _safe_str(td, "name")
            norm = _normalize_task_name(raw_name)
            if not norm:
                continue

            td_state_raw = (
                td.get("state") or td.get("runtime_state")
                if isinstance(td, dict)
                else getattr(td, "state", None) or getattr(td, "runtime_state", None)
            )
            t_state = _normalize_state(td_state_raw)
            end_time = td.get("end_time") if isinstance(td, dict) else getattr(td, "end_time", None)

            task_id = _safe_str(td, "task_id")
            child_tasks = (
                td.get("child_tasks") if isinstance(td, dict) else getattr(td, "child_tasks", None)
            )
            has_dag_children = bool(
                (task_id and task_id in parent_ids)
                or (
                    isinstance(child_tasks, (list, tuple))
                    and any(bool(_safe_str(ct, "task_id")) for ct in child_tasks)
                )
            )

            if norm in _LEAF_STAGE_MAP and not has_dag_children:
                entry = _LEAF_STAGE_MAP[norm]
                if t_state in ("RUNNING", "IN_PROGRESS"):
                    running_leaf.append(entry)
                elif t_state in ("SUCCEEDED", "CACHED", "SKIPPED"):
                    completed_leaf_orders.append(entry[0])
                elif t_state in ("PENDING", "QUEUED", "") and not end_time:
                    pending_leaf.append(entry)
            elif norm in _DAG_STAGE_MAP:
                dag_entry = _DAG_STAGE_MAP[norm]
                if t_state in ("RUNNING", "IN_PROGRESS", "PENDING", "QUEUED"):
                    active_dag.append(dag_entry)
            elif not _is_structural_wrapper(norm) and not has_dag_children:
                human = _humanize_task_name(norm)
                if human:
                    custom_entry = (90, human)
                    if t_state in ("RUNNING", "IN_PROGRESS"):
                        running_leaf.append(custom_entry)
                    elif t_state in ("PENDING", "QUEUED", "") and not end_time:
                        pending_leaf.append(custom_entry)

        if running_leaf:
            return _dedupe_labels(running_leaf)
        if pending_leaf:
            return _dedupe_labels(pending_leaf, lowest_order_only=True)
        if active_dag:
            return _dedupe_labels(active_dag, lowest_order_only=True)
        if completed_leaf_orders and state in ("RUNNING", "IN_PROGRESS"):
            max_done = max(completed_leaf_orders)
            if max_done >= 60:
                return "Finalizing…"
            if max_done >= 40:
                return "Analysis"
            if max_done >= 30:
                return "GraphRAG Indexing"
            if max_done >= 20:
                return "Data Generation"
            if max_done >= 10:
                return "Data Generation (Cloning Repo)"

    pod_stage = _extract_pod_stage_fallback(resolved_run_id, namespace=namespace)
    if pod_stage:
        return pod_stage

    return "Queued" if state == "PENDING" else "Starting…"


def get_kfp_run_info(job_id: str, namespace: str | None = None) -> dict[str, Any]:
    """Return normalised state and runtime parameters for a KFP run."""
    client = create_client(namespace=namespace)
    try:
        run_detail = client.get_run(run_id=job_id)
    except Exception as exc:
        raise ValueError(f"KFP run '{job_id}' not found: {exc}") from exc

    run_obj = _unwrap_run(run_detail)
    state_raw = getattr(run_obj, "state", None) or getattr(run_obj, "runtime_state", None)
    state = _normalize_state(state_raw) or "PENDING"

    display = _safe_str(run_obj, "display_name") or _safe_str(run_obj, "name") or job_id
    display_lower = display.lower()
    rc = getattr(run_obj, "runtime_config", None)
    params = (getattr(rc, "parameters", None) if rc else None) or {}
    git_repo = str(params.get("git_repo") or "")
    git_branch = str(params.get("git_branch") or "main") if git_repo else ""
    project_slug = str(params.get("project_slug") or "")
    project_workspace = str(params.get("project_workspace") or "")
    is_single = display_lower.startswith("single_repo") or display_lower.startswith("single-repo")
    is_multi = (
        display_lower.startswith("multi_repo")
        or display_lower.startswith("multi-repo")
        or (not git_repo and not is_single)
    )
    if not is_multi and not git_repo:
        git_repo = os.getenv("GIT_REPO", "")
        git_branch = str(params.get("git_branch") or os.getenv("GIT_BRANCH", "main") or "main")

    if is_multi and not project_slug:
        raw_repos = params.get("runnable_repos", "")
        if raw_repos:
            try:
                import json
                from utils.pipeline_utils import generate_multi_repo_slug

                parsed = json.loads(raw_repos) if isinstance(raw_repos, str) else raw_repos
                if isinstance(parsed, list) and parsed:
                    project_slug = generate_multi_repo_slug(parsed)
            except Exception:
                pass

    current_stage = extract_active_stage(
        run_detail,
        run_state=state,
        run_id=job_id,
        namespace=namespace,
    )

    return {
        "job_id": job_id,
        "run_name": display,
        "state": state,
        "current_stage": current_stage,
        "git_repo": git_repo,
        "git_branch": git_branch or "main",
        "multi_repo": is_multi,
        "project_slug": project_slug,
        "project_workspace": project_workspace,
    }


def get_kfp_run_state(job_id: str, namespace: str | None = None) -> str:
    """Return the normalised uppercase state string for a KFP run."""
    return get_kfp_run_info(job_id, namespace=namespace)["state"]
