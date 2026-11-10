import os
import yaml
from abc import ABC, abstractmethod
from utils.pipeline_utils import resolve_mlflow_prefix


class AssetLoader(ABC):

    _ASSETS_DIR = os.path.join(os.path.dirname(__file__), "..", "assets")
    _PROMPTS_DIR = os.path.join(_ASSETS_DIR, "prompts")

    ENRICHMENTS_DIR = ".enrichments"

    RESULTS_PATH_PREFIX_EVAL = "results/evaluations"
    RESULTS_PATH_PREFIX_PIPELINES = "results/pipelines"
    RESULTS_PATH_PREFIX_ADHOC_QUERIES = "results/adhoc_queries"
    RESULTS_PATH_PREFIX_METADATA = "results/metadata"
    RESULTS_PATH_PREFIX_ENRICHMENTS = f"results/{ENRICHMENTS_DIR}"
    RESULTS_PATH_PREFIX_VISUALIZATIONS = "results/visualizations"
    RESULTS_PATH_PREFIX_REPO_DATASETS = "results/datasets/repos"
    RESULTS_PATH_PREFIX_TELEMETRY = "results/telemetry"

    _DEFAULT_PREFIX = resolve_mlflow_prefix()
    STATIC_ASSET_EXPERIMENT = f"{_DEFAULT_PREFIX}/code-refactoring/assets/static"
    RESULT_DIRECTORY_ASSET_EXPERIMENT = f"{_DEFAULT_PREFIX}/code-refactoring/assets/result-directories"
    RESULT_ASSET_EXPERIMENT = f"{_DEFAULT_PREFIX}/code-refactoring/assets/results"

    def __init__(self, namespace: str | None = None, workspace: str | None = None):
        self.namespace = namespace
        self.workspace = workspace
        prefix = resolve_mlflow_prefix(namespace=namespace, workspace=workspace)
        self.STATIC_ASSET_EXPERIMENT = f"{prefix}/code-refactoring/assets/static"
        self.RESULT_DIRECTORY_ASSET_EXPERIMENT = f"{prefix}/code-refactoring/assets/result-directories"
        self.RESULT_ASSET_EXPERIMENT = f"{prefix}/code-refactoring/assets/results"

    @staticmethod
    def _get_prompt_body_and_metadata(raw: str) -> tuple[str, dict]:
        """Parses YAML frontmatter from a prompt string, returning (body, metadata)."""
        parts = raw.split('---', 2)
        if not raw.startswith('---') or len(parts) < 3:
            return raw, {}
        return parts[2].lstrip('\n'), yaml.safe_load(parts[1]) or {}

    @staticmethod
    def get_log_results_artifact_path(results_path: str,
                                      git_slug: str = None,
                                      multi_repo: bool = False) -> str:
        """Returns the path to a log results artifact within the run's artifact store."""
        if not multi_repo and not git_slug:
            raise ValueError("git_slug is required when multi_repo=False")

        sub_slug = git_slug if (git_slug and git_slug != "multi-repo") else ""
        artifact_path = (
            f"{results_path}/multi-repo/{sub_slug}" if multi_repo
            else f"{results_path}/{git_slug}"
        ).strip("/")

        return artifact_path

    @abstractmethod
    def load_skill(self, name: str) -> tuple[str, dict]:
        """Load a skill file by name.

        Args:
            name: Skill name, matching the file stem and the SKILL.md ``name`` field.

        Returns:
            Tuple of (body string, frontmatter metadata dict).
        """

    @abstractmethod
    def download(self, asset_file_path: str, download_dir: str = None, **kwargs):
        """Downloads and returns the asset, optionally saving it to a directory.

        Args:
            asset_file_path: Path to the asset file.
            download_dir: Optional directory path to save the asset. The asset is saved
                         using its original filename within that directory. The directory
                         is created if it does not exist. If None, the asset is not saved.

        Returns:
            The asset content (parsed dict for .json files, str otherwise), or None if not found.
        """

    @abstractmethod
    def download_dir(self, asset_dir_path: str, download_dir: str, **kwargs):
        """Downloads a directory from the backing store to a local directory.

        Args:
            asset_dir_path: Path to the asset directory.
            download_dir: Local directory path to download into. Created if it does not exist.
            **kwargs: Additional keyword arguments passed to the backing store implementation
                      (e.g. experiment_name, asset_tags for MlFlowAssetLoader).
        """

    @abstractmethod
    def load_results(
        self,
        download_dir: str,
        artifact_path: str = None,
        tags: dict = None,
        git_slug: str | None = None,
    ):
        """Downloads previously logged results to download_dir."""

    @abstractmethod
    def log_results(self, results_path: str, artifact_path: str = None, tags: dict = None,
                    content: str = None):
        """Logs pipeline output artifacts for the current run.

        Args:
            results_path: Local path to the file or directory to log.
            artifact_path: Optional subdirectory within the run's artifact store to organize results under.
            tags: Optional key-value tags to attach to the run.
            content: Optional string content to write to results_path before logging.
        """

    @abstractmethod
    def register_static_asset(
        self,
        results_path: str,
        artifact_path: str = None,
        tags: dict = None,
        content: str = None,
    ):
        """Logs an artifact to the static asset store (same experiment as ``download``)."""

    @abstractmethod
    def upload_all_assets(self, assets_dir: str):
        """Uploads all assets from a directory to the loader's backing store in a single operation.

        Args:
            assets_dir: Local path to the directory containing assets to upload.
        """

    @abstractmethod
    def upload_prompt(self, prompt_path: str):
        """Uploads or registers a prompt template from the assets directory.

        Args:
            prompt_path: Path to the prompt file relative to _ASSETS_DIR, without extension.
        """

    @abstractmethod
    def download_prompt(self, prompt_path: str, **kwargs) -> tuple[str, dict]:
        """Downloads and renders a prompt template from the backing store.

        Args:
            prompt_path: Path to the prompt file relative to _ASSETS_DIR, without extension.
            **kwargs: Variables to render into the prompt template.

        Returns:
            Tuple of (rendered prompt string, frontmatter metadata dict).
        """

    @abstractmethod
    def register_enrichments(
        self,
        repo_dir: str | None = None,
        git_slug: str | None = None,
        enrichment_files: dict | None = None,
    ) -> str:
        """Persists the enrichments directory to the pipeline asset store."""

    @abstractmethod
    def log_repos(self, run_repos: list) -> None:
        """Persists the repository list to the asset store."""

    @abstractmethod
    def load_run_repos(self, run_id: str | None = None) -> list:
        """Loads the repository list from the asset store, falling back to GIT_REPO_LIST_CONTENTS."""

    @abstractmethod
    def load_catalog_repos(self) -> list:
        """Downloads the full repo catalog, trying enrichments first and falling back to static assets."""

    @abstractmethod
    def num_prompts(self, prompt_prefix: str) -> int:
        """Returns the number of prompts with the given prefix.

        Args:
            prompt_prefix: Path prefix relative to _PROMPTS_DIR to filter prompts by.

        Returns:
            The count of matching prompts.
        """
