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

    def download(self, *args, **kwargs):
        return self._loader.download(*args, **kwargs)

    def download_dir(self, *args, **kwargs):
        return self._loader.download_dir(*args, **kwargs)

    def log_results(self, *args, **kwargs):
        return self._loader.log_results(*args, **kwargs)

    def upload_all_assets(self, *args, **kwargs):
        return self._loader.upload_all_assets(*args, **kwargs)

    def upload_prompt(self, *args, **kwargs):
        return self._loader.upload_prompt(*args, **kwargs)

    def download_prompt(self, *args, **kwargs):
        return self._loader.download_prompt(*args, **kwargs)

    def num_prompts(self, *args, **kwargs):
        return self._loader.num_prompts(*args, **kwargs)
