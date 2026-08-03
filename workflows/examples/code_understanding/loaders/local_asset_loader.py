import json
import os
import logging
logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

from .asset_loader import AssetLoader


class LocalAssetLoader(AssetLoader):
    """Loads an asset from the local assets directory."""

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

    def log_results(self, results_path: str, artifact_path: str = None, tags: dict = None,
                    content: str = None):
        """Writes content to results_path if provided. No remote logging step."""
        if content is not None and not os.path.isdir(results_path):
            with open(results_path, "w") as f:
                f.write(content)

    def upload_all_assets(self, assets_dir: str):
        """No-op. Local assets are already on disk and require no upload step."""
        pass

    def upload_prompt(self, prompt_path: str):
        """No-op. Local prompts are read directly from disk."""
        pass

    def download_prompt(self, prompt_path: str, **kwargs) -> str:
        """Reads a prompt template from the local assets directory and renders it with Jinja2."""
        from jinja2 import Template

        asset_uri = os.path.join(self._PROMPTS_DIR, prompt_path + ".txt")

        with open(asset_uri, "r") as f:
            content = f.read()

        return Template(content).render(**kwargs)

    def num_prompts(self, prompt_prefix: str) -> int:
        """Returns the number of .txt files directly under the prefixed prompts directory."""
        prompt_dir = os.path.join(self._PROMPTS_DIR, prompt_prefix)

        return sum(1 for f in os.listdir(prompt_dir) if f.endswith(".txt"))
