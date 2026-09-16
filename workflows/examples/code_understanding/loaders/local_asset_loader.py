import json
import os
import logging
logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

from .asset_loader import AssetLoader


class LocalAssetLoader(AssetLoader):
    """Loads an asset from the local assets directory."""

    def __init__(self):

        self.asset_base_uri = self._ASSETS_DIR

    def download(self, asset_file_path: str, download_dir: str = None, **kwargs):
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
                if os.path.exists(asset_file_path):
                    asset_uri = asset_file_path
                else:
                    alt_uri = os.path.join(self.asset_base_uri, os.path.basename(asset_file_path))
                    if os.path.exists(alt_uri):
                        asset_uri = alt_uri
                    else:
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

    def download_dir(self, asset_dir_path: str, download_dir: str, **kwargs):
        """Downloads a directory from the local assets directory to a local directory."""
        import shutil

        try:

            source_dir = os.path.join(self.asset_base_uri, asset_dir_path)

            if not os.path.exists(source_dir):

                raise FileNotFoundError(f"Asset directory {source_dir} not found.")

            os.makedirs(download_dir, exist_ok=True)

            shutil.copytree(source_dir, download_dir, dirs_exist_ok=True)

        except Exception as e:

            logging.error(f"Error downloading asset directory {asset_dir_path}: {e}")

            raise e

    def log_results(self, results_path: str, artifact_path: str = None, tags: dict = None,
                    content: str = None):
        """Writes content to results_path if provided. No remote logging step."""
        if content is not None and not os.path.isdir(results_path):
            dir_name = os.path.dirname(results_path)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            with open(results_path, "w") as f:
                f.write(content)

            try:
                if artifact_path:
                    target_asset = os.path.join(self.asset_base_uri, artifact_path, os.path.basename(results_path))
                    os.makedirs(os.path.dirname(target_asset), exist_ok=True)
                    with open(target_asset, "w") as f:
                        f.write(content)
                else:
                    target_asset = os.path.join(self.asset_base_uri, os.path.basename(results_path))
                    os.makedirs(os.path.dirname(target_asset), exist_ok=True)
                    with open(target_asset, "w") as f:
                        f.write(content)
            except Exception as e:
                logging.debug(f"Failed to mirror result to asset_base_uri: {e}")

    def download_matching_artifacts(self, artifact_filename: str = None, tags: dict = None, **kwargs):
        """Finds and returns matching metrics artifacts from local asset base directory."""
        results = []
        try:
            if not os.path.isdir(self.asset_base_uri):
                return results
            for root, _, files in os.walk(self.asset_base_uri):
                for fn in files:
                    if artifact_filename and fn == artifact_filename:
                        fp = os.path.join(root, fn)
                        with open(fp, "r", encoding="utf-8") as f:
                            results.append(json.load(f) if fn.endswith(".json") else f.read())
                    elif not artifact_filename and "pipeline_metrics" in fn and fn.endswith(".json"):
                        fp = os.path.join(root, fn)
                        with open(fp, "r", encoding="utf-8") as f:
                            results.append(json.load(f))
        except Exception as e:
            logging.debug(f"Error scanning local assets: {e}")
        return results

    def upload_all_assets(self, assets_dir: str):
        """No-op. Local assets are already on disk and require no upload step."""
        pass

    def upload_prompt(self, prompt_path: str):
        """No-op. Local prompts are read directly from disk."""
        pass

    def download_prompt(self, prompt_path: str, **kwargs) -> tuple[str, dict]:
        """Reads a prompt template from the local assets directory and renders it with Jinja2."""
        from jinja2 import Template

        asset_uri = os.path.join(self._PROMPTS_DIR, prompt_path + ".txt")

        with open(asset_uri, "r") as f:
            raw = f.read()

        body, meta = self._get_prompt_body_and_metadata(raw)
        return Template(body).render(**kwargs), meta

    def num_prompts(self, prompt_prefix: str) -> int:
        """Returns the number of .txt files directly under the prefixed prompts directory."""
        prompt_dir = os.path.join(self._PROMPTS_DIR, prompt_prefix)

        return sum(1 for f in os.listdir(prompt_dir) if f.endswith(".txt"))
