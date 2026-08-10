import os

from .asset_loader import AssetLoader
from .local_asset_loader import LocalAssetLoader
from .mlflow_asset_loader import MlFlowAssetLoader


class DefaultAssetLoader(AssetLoader):
    """Delegates to LocalAssetLoader or MlFlowAssetLoader based on the ASSET_LOADER env var."""

    def __init__(self):

        if os.getenv("ASSET_LOADER") == "mlflow":

            self._loader = MlFlowAssetLoader()

        else:

            self._loader = LocalAssetLoader()

    def download(self, asset_file_path: str, download_dir: str = None):

        return self._loader.download(asset_file_path, download_dir)

    def download_dir(self, asset_dir_path: str, download_dir: str):

        return self._loader.download_dir(asset_dir_path, download_dir)

    def log_results(self, results_path: str, artifact_path: str = None, tags: dict = None,
                    content: str = None):

        return self._loader.log_results(results_path, artifact_path, tags, content)

    def upload_all_assets(self, assets_dir: str):

        return self._loader.upload_all_assets(assets_dir)

    def upload_prompt(self, prompt_path: str):

        return self._loader.upload_prompt(prompt_path)

    def download_prompt(self, prompt_path: str, **kwargs) -> tuple[str, dict]:

        return self._loader.download_prompt(prompt_path, **kwargs)

    def num_prompts(self, prompt_prefix: str) -> int:

        return self._loader.num_prompts(prompt_prefix)
