import logging
import os

logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())


def download_result_directory(git_slug: str, download_dir: str, results_prefix: str,
                              multi_repo: bool, asset_tags: dict):
    """Downloads a single repo's assets from the backing store to download_dir."""
    from loaders.default_asset_loader import DefaultAssetLoader
    from loaders.mlflow_asset_loader import MlFlowAssetLoader

    DefaultAssetLoader().download_dir(
        asset_dir_path=DefaultAssetLoader.get_log_results_artifact_path(
            results_prefix,
            git_slug=git_slug,
            multi_repo=multi_repo,
        ),
        download_dir=download_dir,
        experiment_name=MlFlowAssetLoader.RESULT_DIRECTORY_ASSET_EXPERIMENT,
        asset_tags=asset_tags,
    )


def download_code_metadata_directories(git_repos: list, parent_target_path: str):
    """Downloads every requested repo's metadata, failing if any download fails."""
    if not git_repos:
        raise ValueError("No repositories configured for multi-repo indexing.")

    from loaders.default_asset_loader import DefaultAssetLoader
    from utils.code_utils import generate_slug_from_repo

    for git_data in git_repos:
        git_repo = git_data["git_repo"]
        git_branch = git_data["git_branch"]
        repo_slug = generate_slug_from_repo(git_repo, git_branch)
        repo_target_path = os.path.join(parent_target_path, repo_slug)

        try:
            logging.info(f"Downloading generated data for {git_repo} ({repo_slug})...")
            download_result_directory(
                git_slug=repo_slug,
                download_dir=repo_target_path,
                results_prefix=DefaultAssetLoader.RESULTS_PATH_PREFIX_METADATA,
                multi_repo=True,
                asset_tags={"git_slug": repo_slug, "category": "data-generation",
                            "code-metadata": "true", "multi_repo": "True"},
            )
        except Exception as e:
            raise RuntimeError(
                f"Could not download generated data for {git_repo} ({repo_slug}). "
                "Multi-repo indexing requires generated data for every configured repository."
            ) from e
