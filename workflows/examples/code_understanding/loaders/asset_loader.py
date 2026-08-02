import os
from abc import ABC, abstractmethod


class AssetLoader(ABC):

    _ASSETS_DIR = os.path.join(os.path.dirname(__file__), "..", "assets")
    _PROMPTS_DIR = os.path.join(_ASSETS_DIR, "prompts")


    @abstractmethod
    def download(self, asset_file_path: str, download_dir: str = None):
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
    def download_dir(self, asset_dir_path: str, download_dir: str):
        """Downloads a directory from the backing store to a local directory.

        Args:
            asset_dir_path: Path to the asset directory.
            download_dir: Local directory path to download into. Created if it does not exist.
        """

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
    def download_prompt(self, prompt_path: str, **kwargs) -> str:
        """Downloads and renders a prompt template from the backing store.

        Args:
            prompt_path: Path to the prompt file relative to _ASSETS_DIR, without extension.
            **kwargs: Variables to render into the prompt template.

        Returns:
            The rendered prompt string.
        """
