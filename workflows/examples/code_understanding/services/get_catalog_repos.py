"""Fetch the repository catalog from the asset store."""

from __future__ import annotations


def get_catalog_repos(
    namespace: str | None = None,
    workspace: str | None = None,
) -> list:
    """Returns the full repo catalog via ``load_catalog_repos`` on the configured loader."""
    from loaders.default_asset_loader import DefaultAssetLoader

    return DefaultAssetLoader(namespace=namespace, workspace=workspace).load_catalog_repos()
