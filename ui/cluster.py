"""Cluster/namespace/workspace discovery and per-request selection."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any

from fastapi import Request

logger = logging.getLogger(__name__)

_SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_SA_NS_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"

NAMESPACE_COOKIE = "cu_namespace"
WORKSPACE_COOKIE = "cu_workspace"


def sanitize_workspace(value: str | None) -> str:
    """Sanitize a workspace name to URL/path-safe characters, defaulting to 'default'."""
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", (value or "").strip()).strip("-")[:120]
    return cleaned if cleaned else "default"


def default_namespace() -> str:
    """Return the default namespace from env or the in-cluster service account."""
    for env_key in ("KFP_NAMESPACE", "MLFLOW_WORKSPACE", "POD_NAMESPACE"):
        val = os.environ.get(env_key, "").strip()
        if val:
            return val
    if os.path.exists(_SA_NS_PATH):
        try:
            with open(_SA_NS_PATH) as f:
                val = f.read().strip()
                if val:
                    return val
        except Exception:
            pass
    return "demo"


def default_workspace() -> str:
    """Return the default project workspace from env or 'default'."""
    return sanitize_workspace(os.environ.get("PROJECT_WORKSPACE", "default"))


def get_active_namespace(request: Request | None = None) -> str:
    """Return the active namespace for this request (cookie -> default)."""
    if request is not None:
        cookie_val = (request.cookies.get(NAMESPACE_COOKIE) or "").strip()
        if cookie_val:
            return cookie_val
    return default_namespace()


def get_active_workspace(request: Request | None = None) -> str:
    """Return the active project workspace for this request (cookie -> env -> 'default')."""
    if request is not None:
        cookie_val = (request.cookies.get(WORKSPACE_COOKIE) or "").strip()
        if cookie_val:
            return sanitize_workspace(cookie_val)
    return default_workspace()


def list_workspaces(namespace: str | None = None) -> list[str]:
    """Discover existing project workspaces inside *namespace* from MLflow and KFP experiments."""
    ns = (namespace or default_namespace()).strip()
    found: set[str] = {"default"}
    env_ws = default_workspace()
    if env_ws:
        found.add(env_ws)

    # 1. Discover workspaces from MLflow experiment names ({ns}/{workspace}/code-refactoring/...)
    try:
        from mlflow.tracking import MlflowClient

        client = MlflowClient()
        experiments = client.search_experiments()
        prefix = f"{ns}/"
        marker = "/code-refactoring/"
        for exp in experiments or []:
            name = getattr(exp, "name", "") or ""
            if name.startswith(prefix) and marker in name:
                middle = name[len(prefix) :].split(marker, 1)[0].strip("/")
                if middle and "/" not in middle:
                    found.add(sanitize_workspace(middle))
    except Exception:
        logger.debug("Could not search MLflow experiments for workspaces", exc_info=True)

    # 2. Discover workspaces from KFP experiments (workspace-<ws>)
    try:
        from utils.service_utils import create_client

        kfp_client = create_client(namespace=ns)
        resp = kfp_client.list_experiments(page_size=100)
        for exp in getattr(resp, "experiments", None) or []:
            dname = getattr(exp, "display_name", None) or getattr(exp, "name", "") or ""
            if dname.startswith("workspace-"):
                ws_name = sanitize_workspace(dname[len("workspace-") :])
                if ws_name:
                    found.add(ws_name)
    except Exception:
        logger.debug("Could not list KFP experiments for workspaces", exc_info=True)

    others = sorted(w for w in found if w != "default")
    return ["default", *others]


def _k8s_client():
    """Return a configured ``kubernetes.client`` module, or None if unavailable."""
    try:
        from kubernetes import client as k8s_client
        from kubernetes import config as k8s_config

        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()
        return k8s_client
    except Exception:
        logger.debug("Kubernetes python client unavailable", exc_info=True)
        return None


def get_cluster_info() -> dict[str, Any]:
    """Return basic cluster metadata for the UI header."""
    server = ""
    user = ""
    console_url = os.environ.get("OPENSHIFT_CONSOLE_URL", "").strip()

    try:
        from kubernetes.client import Configuration

        if _k8s_client() is not None:
            cfg = Configuration.get_default_copy()
            server = cfg.host or ""
            user = "in-cluster-sa" if os.path.exists(_SA_TOKEN_PATH) else ""
    except Exception:
        pass

    if not os.path.exists(_SA_TOKEN_PATH):
        try:
            if not server:
                server = subprocess.check_output(
                    ["oc", "whoami", "--show-server"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                ).strip()
            if not user:
                user = subprocess.check_output(
                    ["oc", "whoami"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                ).strip()
            if not console_url:
                console_url = subprocess.check_output(
                    ["oc", "whoami", "--show-console"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                ).strip()
        except Exception:
            pass

    return {
        "server": server,
        "user": user,
        "console_url": console_url,
        "in_cluster": os.path.exists(_SA_TOKEN_PATH),
    }


def list_namespaces() -> list[str]:
    """Return accessible namespaces/projects sorted alphabetically."""
    names: set[str] = set()
    default_ns = default_namespace()
    if default_ns:
        names.add(default_ns)

    env_list = os.environ.get("AVAILABLE_NAMESPACES", "").strip()
    if env_list:
        for item in env_list.split(","):
            if item.strip():
                names.add(item.strip())

    k8s = _k8s_client()
    if k8s is not None:
        try:
            v1 = k8s.CoreV1Api()
            for item in v1.list_namespace(_request_timeout=3).items:
                if item.metadata and item.metadata.name:
                    names.add(item.metadata.name)
        except Exception:
            try:
                co = k8s.CustomObjectsApi()
                res = co.list_cluster_custom_object(
                    group="project.openshift.io",
                    version="v1",
                    plural="projects",
                    _request_timeout=3,
                )
                for item in res.get("items", []):
                    n = item.get("metadata", {}).get("name")
                    if n:
                        names.add(n)
            except Exception:
                logger.debug("Could not list namespaces via k8s API", exc_info=True)

    if len(names) <= 1 and not os.path.exists(_SA_TOKEN_PATH):
        try:
            out = subprocess.check_output(
                ["oc", "get", "projects", "-o", "jsonpath={.items[*].metadata.name}"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=4,
            ).strip()
            for n in out.split():
                if n:
                    names.add(n)
        except Exception:
            pass

    return sorted(names)
