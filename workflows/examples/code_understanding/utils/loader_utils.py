import json
import logging
import os

logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())


def merge_json_enrichment(base, override, treat_lists_as_singletons: bool = False):
    """Recursively merges override into base. Delegates to json_utils.merge_json."""
    from utils.json_utils import merge_json

    return merge_json(base, override, treat_lists_as_singletons)


def is_global_scoped_enrichment(path: str) -> bool:
    """Returns True if the path is global-scoped — i.e., at or under .enrichments/.assets."""
    parts = os.path.normpath(path).replace("\\", "/").split("/")
    return any(
        parts[i] == ".enrichments" and parts[i + 1] == ".assets" for i in range(len(parts) - 1)
    )


def get_global_scoped_enrichment_dir(base_dir: str | None) -> str | None:
    """Returns the global-scoped enrichments assets directory for base_dir."""
    if not base_dir:
        return None
    if is_global_scoped_enrichment(base_dir):
        return base_dir
    return os.path.join(base_dir, ".enrichments", ".assets")


def is_global_scoped_enrichment_tagset(tags: dict) -> bool:
    """Returns True if tags represent a global-scoped enrichment run
    (category=enrichments with git_slug explicitly None)."""
    return (
        bool(tags)
        and tags.get("category") == "enrichments"
        and "git_slug" in tags
        and tags["git_slug"] is None
    )


def normalize_asset_path(asset_path: str) -> str:
    """Strip any prefix up to and including assets/ from asset_path if present."""
    _, sep, remainder = asset_path.replace("\\", "/").partition("assets/")
    return remainder if sep else asset_path


def load_local_enrichments(download_dir: str, asset_path: str, *, is_dir: bool = False):
    """Tier 1: check the global-scoped enrichments assets directory on the local filesystem."""
    if not download_dir:
        return None

    local_path = os.path.join(get_global_scoped_enrichment_dir(download_dir), asset_path)

    if is_dir and os.path.isdir(local_path):
        logging.debug("Resolved asset dir from local enrichments: %s", local_path)
        return True

    if not is_dir and os.path.isfile(local_path):
        logging.debug("Resolved asset from local enrichments: %s", local_path)
        with open(local_path, "r") as f:
            content = json.load(f) if local_path.endswith(".json") else f.read()
        return content

    return None


def sync_enrichments(loader, assets_dir: str) -> None:
    """Downloads global enrichment assets from the store and merges them into assets_dir."""
    import shutil
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as seed_dir:
        seed_assets = get_global_scoped_enrichment_dir(seed_dir)
        loader.load_results(
            download_dir=seed_assets,
            artifact_path=f"{loader.RESULTS_PATH_PREFIX_ENRICHMENTS}/.assets",
        )
        if not os.path.exists(seed_assets):
            return

        os.makedirs(assets_dir, exist_ok=True)

        for seed_path in Path(seed_assets).rglob("*"):
            if not seed_path.is_file():
                continue
            rel = seed_path.relative_to(seed_assets)
            dst = Path(assets_dir) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)

            if not dst.exists():
                shutil.copy2(seed_path, dst)
            elif seed_path.suffix == ".json":
                try:
                    remote = json.loads(seed_path.read_text())
                    local = json.loads(dst.read_text())
                    dst.write_text(json.dumps(merge_json_enrichment(remote, local), indent=2))
                except (json.JSONDecodeError, ValueError):
                    pass


def download_result_directory(
    git_slug: str,
    download_dir: str,
    results_prefix: str,
    multi_repo: bool,
    asset_tags: dict,
    namespace: str | None = None,
    workspace: str | None = None,
):
    """Downloads a single repo's assets from the backing store to download_dir."""
    from loaders.default_asset_loader import DefaultAssetLoader

    loader = DefaultAssetLoader(namespace=namespace, workspace=workspace)
    loader.download_dir(
        asset_dir_path=DefaultAssetLoader.get_log_results_artifact_path(
            results_prefix,
            git_slug=git_slug,
            multi_repo=multi_repo,
        ),
        download_dir=download_dir,
        experiment_name=loader.RESULT_DIRECTORY_ASSET_EXPERIMENT,
        asset_tags=asset_tags,
    )


def download_graphrag_output_directory(
    graphrag_source_path: str,
    pipeline_run_id: str,
    git_repo: str = "",
    git_branch: str = "",
    multi_repo: bool = False,
    project_slug: str = "",
    namespace: str | None = None,
    workspace: str | None = None,
):
    """Downloads a previous run's GraphRAG index into graphrag_source_path/output/."""
    import shutil
    from loaders.default_asset_loader import DefaultAssetLoader
    from pipelines.base.data_generation import generate_git_slug
    from utils.graphrag_utils import DependencyAnalyzer

    if multi_repo:
        git_slug = project_slug or None
    else:
        git_slug = generate_git_slug(git_repo, git_branch) if git_repo else None

    output_dir = os.path.join(graphrag_source_path, "output")
    base_tags = {"category": "indexing"}
    if git_slug:
        base_tags["git_slug"] = git_slug
    if multi_repo is not None:
        base_tags["multi_repo"] = multi_repo

    try:
        download_result_directory(
            git_slug=git_slug,
            download_dir=output_dir,
            results_prefix=DefaultAssetLoader.RESULTS_PATH_PREFIX_REPO_DATASETS,
            multi_repo=multi_repo,
            asset_tags={**base_tags, "pipeline_run_id": pipeline_run_id},
            namespace=namespace,
            workspace=workspace,
        )
    except Exception:
        logging.debug(
            "Run-scoped GraphRAG output lookup missed (run_id=%s); falling back to slug-scoped lookup",
            pipeline_run_id,
        )
        download_result_directory(
            git_slug=git_slug,
            download_dir=output_dir,
            results_prefix=DefaultAssetLoader.RESULTS_PATH_PREFIX_REPO_DATASETS,
            multi_repo=multi_repo,
            asset_tags={"pipeline_run_id": pipeline_run_id, "category": "indexing"},
            namespace=namespace,
            workspace=workspace,
        )
    DependencyAnalyzer.prepare_settings(template_dir=output_dir, output_dir=graphrag_source_path)


def download_code_metadata_directories(
    git_repos: list,
    parent_target_path: str,
    pipeline_run_id: str | None = None,
    namespace: str | None = None,
    workspace: str | None = None,
):
    """Downloads every requested repo's metadata, failing if any download fails."""
    if not git_repos:
        raise ValueError("No repositories configured for multi-repo indexing.")

    from loaders.default_asset_loader import DefaultAssetLoader
    from utils.code_utils import generate_slug_from_repo
    from utils.pipeline_utils import get_pipeline_run_id

    effective_run_id = pipeline_run_id or os.environ.get("KFP_RUN_ID") or os.environ.get("PIPELINE_RUN_ID")
    if effective_run_id and effective_run_id.startswith("{{"):
        effective_run_id = None
    if not effective_run_id:
        try:
            effective_run_id = get_pipeline_run_id()
        except Exception:
            effective_run_id = None

    for git_data in git_repos:
        git_repo = git_data["git_repo"]
        git_branch = git_data["git_branch"]
        repo_slug = generate_slug_from_repo(git_repo, git_branch)
        repo_target_path = os.path.join(parent_target_path, repo_slug)

        base_tags = {
            "git_slug": repo_slug,
            "category": "data-generation",
            "code-metadata": "true",
            "multi_repo": "True",
        }
        try:
            logging.info(f"Downloading generated data for {git_repo} ({repo_slug})...")
            if effective_run_id:
                try:
                    download_result_directory(
                        git_slug=repo_slug,
                        download_dir=repo_target_path,
                        results_prefix=DefaultAssetLoader.RESULTS_PATH_PREFIX_METADATA,
                        multi_repo=True,
                        asset_tags={**base_tags, "pipeline_run_id": effective_run_id},
                        namespace=namespace,
                        workspace=workspace,
                    )
                    continue
                except Exception:
                    logging.debug(
                        "Run-scoped metadata lookup missed for %s (run_id=%s); falling back to latest",
                        repo_slug,
                        effective_run_id,
                    )
            download_result_directory(
                git_slug=repo_slug,
                download_dir=repo_target_path,
                results_prefix=DefaultAssetLoader.RESULTS_PATH_PREFIX_METADATA,
                multi_repo=True,
                asset_tags=base_tags,
                namespace=namespace,
                workspace=workspace,
            )
        except Exception as e:
            raise RuntimeError(
                f"Could not download generated data for {git_repo} ({repo_slug}). "
                "Multi-repo indexing requires generated data for every configured repository."
            ) from e
