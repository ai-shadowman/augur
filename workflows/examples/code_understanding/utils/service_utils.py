"""Shared helpers for the KFP service layer."""

from __future__ import annotations

import os
import subprocess

try:
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    pass
try:
    import kfp_server_api.configuration as _kfp_conf

    _kfp_conf.Configuration.verify_ssl = property(lambda self: False, lambda self, v: None)
except ImportError:
    pass

_SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"


def get_token() -> str:
    """Return a bearer token for KFP API calls.

    Reads the Kubernetes service account token when running in-cluster;
    falls back to ``oc whoami --show-token`` for local development.
    """
    if os.path.exists(_SA_TOKEN_PATH):
        with open(_SA_TOKEN_PATH) as f:
            return f.read().strip()
    return subprocess.check_output(["oc", "whoami", "--show-token"], text=True).strip()


def get_kfp_host(namespace: str | None = None) -> tuple[str, str]:
    """Return ``(kfp_host, kfp_namespace)`` from environment variables.

    Raises:
        RuntimeError: If neither ``namespace`` nor ``KFP_NAMESPACE`` is set.
    """
    default_ns = os.environ.get("KFP_NAMESPACE", "").strip()
    ns = (namespace or default_ns).strip()
    if not ns:
        raise RuntimeError("KFP_NAMESPACE must be set and non-empty.")
    env_host = os.environ.get("KFP_HOST", "").strip()
    if env_host and (not namespace or ns == default_ns):
        return env_host, ns
    return f"https://ds-pipeline-dspa.{ns}.svc.cluster.local:8443", ns


def create_client(namespace: str | None = None):
    """Create and return an authenticated ``kfp.Client``."""
    import kfp

    host, ns = get_kfp_host(namespace=namespace)
    return kfp.Client(host=host, namespace=ns, existing_token=get_token())


def get_kfp_experiment_name(workspace: str | None = None) -> str:
    """Return the KFP experiment name for a given Project Workspace.

    Returns ``"Default"`` when ``workspace`` is empty or ``"default"``,
    or ``"workspace-{workspace}"`` for a custom workspace.
    """
    from utils.pipeline_utils import sanitize_slug

    ws_raw = workspace if workspace is not None else os.environ.get("PROJECT_WORKSPACE", "")
    ws = sanitize_slug(ws_raw)
    if ws and ws.lower() != "default":
        return f"workspace-{ws}"
    return "Default"
