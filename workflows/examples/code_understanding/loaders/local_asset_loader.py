import json
import os
import logging
logging.basicConfig(level=logging.INFO)

from .asset_loader import AssetLoader


class LocalAssetLoader(AssetLoader):
    """Loads an asset from the local assets directory."""

    _ASSETS_DIR = os.path.join(os.path.dirname(__file__), "..", "assets")

    def __init__(self):

        self.asset_base_uri = self._ASSETS_DIR

    def download(self, asset_file_path: str, download_dir: str = None):
        """Downloads and returns the asset from the local assets directory.

        Args:
            asset_file_path: Absolute path to the asset file.
            download_dir: Optional directory path to save the asset.

        Returns:
            The asset content (parsed dict for .json files, str otherwise), or None if not found.
        """
        try:

            asset_uri = os.path.join(self.asset_base_uri, asset_file_path)

            if not os.path.exists(asset_uri):

                logging.info(f"Asset {asset_uri} not found.")

                return None

            with open(asset_uri, "r") as f:

                content = json.load(f) if asset_uri.endswith(".json") else f.read()

            if download_dir is not None:

                os.makedirs(download_dir, exist_ok=True)

                dest_file = os.path.join(download_dir, os.path.basename(asset_uri))

                with open(dest_file, "w") as f:

                    json.dump(content, f) if asset_uri.endswith(".json") else f.write(content)

            return content

        except Exception as e:

            logging.error(f"Error downloading asset {asset_file_path}: {e}")

            raise e

    def download_dir(self, asset_dir_path: str, download_dir: str):
        """Downloads a directory from the local assets directory to a local directory."""
        import shutil

        try:

            source_dir = os.path.join(self.asset_base_uri, asset_dir_path)

            if not os.path.exists(source_dir):

                logging.info(f"Asset directory {source_dir} not found.")

                return

            os.makedirs(download_dir, exist_ok=True)

            shutil.copytree(source_dir, download_dir, dirs_exist_ok=True)

        except Exception as e:

            logging.error(f"Error downloading asset directory {asset_dir_path}: {e}")

            raise e

    def upload_dir(self, local_dir_path: str, upload_dir: str):
        """No-op. Local directories are already on disk and require no upload step."""
        pass

    def log_results(self, results_path: str, artifact_path: str = None, tags: dict = None):
        """No-op. Results are already on local disk and require no logging step."""
        pass

    def upload(self, asset_file_path: str, upload_dir: str):
        """No-op. Local assets are already on disk and require no upload step."""
        pass
