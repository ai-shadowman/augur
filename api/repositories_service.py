"""Thin bridge between the FastAPI layer and the repository services."""

from __future__ import annotations

import sys
from pathlib import Path

_CODE_UNDERSTANDING_DIR = str(
    Path(__file__).resolve().parent.parent / "workflows" / "examples" / "code_understanding"
)
if _CODE_UNDERSTANDING_DIR not in sys.path:
    sys.path.insert(0, _CODE_UNDERSTANDING_DIR)


def fetch_catalog_repos(
    namespace: str | None = None,
    workspace: str | None = None,
) -> list[dict[str, str]]:
    """Return the full repo catalog via ``services.get_catalog_repos``."""
    from services.get_catalog_repos import get_catalog_repos

    return get_catalog_repos(namespace=namespace, workspace=workspace)


def log_repos(
    repos: list[dict[str, str]],
    namespace: str | None = None,
    workspace: str | None = None,
) -> None:
    """Log repos to the asset store via ``services.update_repos``."""
    from services.update_repos import log_repos as _log

    _log(repos, namespace=namespace, workspace=workspace)
