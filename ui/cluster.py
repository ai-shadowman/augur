"""Cluster/namespace/workspace discovery and per-request selection."""

from __future__ import annotations

import logging
import os
import re
import subprocess

from fastapi import Request

logger = logging.getLogger(__name__)

_SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_SA_NS_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"

NAMESPACE_COOKIE = "cu_namespace"


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
    """Return the active project workspace (env -> 'default', ignoring browser cookies)."""
    return default_workspace()


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
