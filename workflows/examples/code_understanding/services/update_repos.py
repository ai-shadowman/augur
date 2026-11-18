"""Update the repository list in the asset store."""

from __future__ import annotations


def log_repos(
    repos: list[dict[str, str]],
    namespace: str | None = None,
    workspace: str | None = None,
) -> None:
    """Logs repos to the asset store via the configured loader."""
    from loaders.default_asset_loader import DefaultAssetLoader

    DefaultAssetLoader(namespace=namespace, workspace=workspace).log_repos(repos)


def delete_repo(
    git_repo: str,
    git_branch: str = "main",
    namespace: str | None = None,
    workspace: str | None = None,
) -> list[dict]:
    """Removes a repository from the catalog in the asset store."""
    from loaders.default_asset_loader import DefaultAssetLoader

    return DefaultAssetLoader(namespace=namespace, workspace=workspace).delete_repo(
        git_repo, git_branch
    )
