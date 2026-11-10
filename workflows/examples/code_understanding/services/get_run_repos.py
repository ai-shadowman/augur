"""Resolve the repo list for a multi-repo pipeline run."""

from __future__ import annotations

import logging

logging.basicConfig(level=logging.INFO)


def get_run_repos(repos: list = None) -> list:
    """Return the repo list for a multi-repo pipeline run."""
    import json
    import os

    logging.info(f"get_run_repos: starting...repos={repos}")

    if repos:
        return repos

    env_contents = os.environ.get("GIT_REPO_LIST_CONTENTS", "")
    if env_contents:
        return json.loads(env_contents)

    logging.info(
        "get_run_repos: no repo list found in repos or GIT_REPO_LIST_CONTENTS;"
        " returning empty list."
    )
    return []
