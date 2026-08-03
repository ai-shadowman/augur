import json
import os
import mlflow
import requests
from mlflow.tracking import MlflowClient
import logging
logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

from .asset_loader import AssetLoader


class MlFlowAssetLoader(AssetLoader):
    """Loads an asset from the MLflow artifacts registry."""

    _STATIC_ASSET_EXPERIMENT = f"/{os.environ.get('MLFLOW_WORKSPACE', 'demo')}/code-refactoring/assets/static"
    _RESULT_DIRECTORY_ASSET_EXPERIMENT = f"/{os.environ.get('MLFLOW_WORKSPACE', 'demo')}/code-refactoring/assets/result-directories"
    _RESULT_ASSET_EXPERIMENT = f"/{os.environ.get('MLFLOW_WORKSPACE', 'demo')}/code-refactoring/assets/results"
    _RUN_NAME = "code-understanding"

    _SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"

    def __init__(self):

        if not os.environ.get("MLFLOW_TRACKING_TOKEN") and os.path.exists(self._SA_TOKEN_PATH):

            with open(self._SA_TOKEN_PATH) as f:

                logging.info("Setting MLFLOW_TRACKING_TOKEN from Kubernetes service account token...")

                os.environ["MLFLOW_TRACKING_TOKEN"] = f.read().strip()

        _token = os.environ.get("MLFLOW_TRACKING_TOKEN")

        if _token:

            _orig_send = requests.Session.send

            def _send_with_forwarded_token(self, request, **kwargs):
                request.headers["X-Forwarded-Access-Token"] = _token
                return _orig_send(self, request, **kwargs)

            requests.Session.send = _send_with_forwarded_token

        try:

            client = MlflowClient()

            experiment = self.get_or_create_experiment_by_name(client, self._STATIC_ASSET_EXPERIMENT)

            latest_runs = client.search_runs(
                experiment_ids=[experiment.experiment_id],
                filter_string="tags.latest = 'true'",
                max_results=1,
            )

            if latest_runs:

                self.asset_base_uri = latest_runs[0].info.artifact_uri

            else:

                self.asset_base_uri = experiment.artifact_location

            logging.info(f"Base absolute storage URI: {self.asset_base_uri}")

        except Exception as e:

            logging.error(f"Error initializing MlFlowAssetLoader: {e}")

            raise e

    def _get_absolute_artifact_uri(self, asset_file_path: str):

        return os.path.join(self.asset_base_uri, asset_file_path)

    def _mark_as_latest(self, client, experiment_id, new_run_id):

        existing = client.search_runs(
            experiment_ids=[experiment_id],
            filter_string="tags.latest = 'true'",
        )

        for run in existing:

            client.set_tag(run.info.run_id, "latest", "false")

        client.set_tag(new_run_id, "latest", "true")

    def get_or_create_experiment_by_name(self, client, experiment_name):
        """Gets or creates an MLflow experiment by name, handling deleted and non-active states."""

        experiment = client.get_experiment_by_name(experiment_name)

        if experiment:

            if experiment.lifecycle_stage == "deleted":

                logging.info(f"Experiment '{experiment_name}' is deleted. Restoring.")

                client.restore_experiment(experiment.experiment_id)

                experiment = client.get_experiment(experiment.experiment_id)

            elif experiment.lifecycle_stage != "active":

                logging.info(f"Experiment '{experiment_name}' is not active. Deleting and recreating.")

                client.delete_experiment(experiment.experiment_id)

                experiment = client.get_experiment(client.create_experiment(name=experiment_name))

            return experiment

        return client.get_experiment(client.create_experiment(name=experiment_name))

    def download(self, asset_file_path: str, download_dir: str = None):
        """Downloads and returns the asset from the MLflow artifacts registry.

        Args:
            asset_file_path: Path to the asset file relative to its artifact backend location in MLflow.
            download_dir: Optional directory path to download the artifact to.

        Returns:
            The asset content (parsed dict for .json files, str otherwise), or None if not found.
        """
        try:

            import shutil

            asset_uri = self._get_absolute_artifact_uri(asset_file_path)

            local_path = mlflow.artifacts.download_artifacts(artifact_uri=asset_uri)

            if not os.path.exists(local_path):

                logging.info(f"Asset {asset_uri} not found.")

                return None

            if download_dir is not None:

                os.makedirs(download_dir, exist_ok=True)

                shutil.copy2(local_path, os.path.join(download_dir, os.path.basename(local_path)))

            with open(local_path, "r") as f:

                return json.load(f) if asset_uri.endswith(".json") else f.read()

        except Exception as e:

            logging.error(f"Error downloading asset {asset_file_path}: {e}")

            raise e

    def download_dir(self, asset_dir_path: str, download_dir: str):
        """Downloads a directory from the MLflow artifacts registry to a local directory."""
        try:

            import shutil

            asset_uri = self._get_absolute_artifact_uri(asset_dir_path)

            local_path = mlflow.artifacts.download_artifacts(artifact_uri=asset_uri)

            os.makedirs(download_dir, exist_ok=True)

            shutil.copytree(local_path, download_dir, dirs_exist_ok=True)

        except Exception as e:

            logging.error(f"Error downloading asset directory {asset_dir_path}: {e}")

            raise e

    def log_results(self, results_path: str, artifact_path: str = None, tags: dict = None,
                    content: str = None):
        """Logs pipeline output artifacts to a new MLflow run."""
        try:
            is_dir = os.path.isdir(results_path)

            if os.path.dirname(results_path):

                os.makedirs(os.path.dirname(results_path), exist_ok=True)

            if content is not None and not is_dir:

                with open(results_path, "w") as f:

                    f.write(content)

            client = MlflowClient()

            experiment_name = self._RESULT_DIRECTORY_ASSET_EXPERIMENT if is_dir else self._RESULT_ASSET_EXPERIMENT

            experiment = self.get_or_create_experiment_by_name(client, experiment_name)

            with mlflow.start_run(experiment_id=experiment.experiment_id) as run:

                if tags:

                    mlflow.set_tags(tags)

                if is_dir:

                    mlflow.log_artifacts(results_path, artifact_path=artifact_path)

                else:

                    mlflow.log_artifact(results_path, artifact_path=artifact_path)

                logging.info(f"Logged results to run {run.info.run_id}")

        except Exception as e:

            logging.error(f"Error logging results {results_path}: {e}")

            raise e

    def upload_prompt(self, prompt_path: str):
        """Registers a prompt template from the local assets directory to the MLflow prompt registry."""
        try:

            asset_uri = os.path.join(self._PROMPTS_DIR, prompt_path + ".txt")

            with open(asset_uri, "r") as f:
                content = f.read()

            name = prompt_path.replace("/", "-")

            mlflow.register_prompt(name=name, template=content)

            logging.info(f"Registered prompt '{name}'")

        except Exception as e:

            logging.error(f"Error registering prompt {prompt_path}: {e}")

            raise e

    def download_prompt(self, prompt_path: str, **kwargs) -> str:
        """Loads a prompt from the MLflow prompt registry and renders it with the provided variables."""
        try:

            name = prompt_path.replace("/", "-")

            prompt = mlflow.load_prompt(name)

            return prompt.format(**kwargs)

        except Exception as e:

            logging.error(f"Error loading prompt {prompt_path}: {e}")

            raise e

    def num_prompts(self, prompt_prefix: str) -> int:
        """Returns the number of prompts in the MLflow registry matching the given prefix."""
        try:

            prefix = prompt_prefix.replace("/", "-")

            prompts = mlflow.search_prompts(filter_string=f"name LIKE '{prefix}-%'")

            return len(prompts)

        except Exception as e:

            logging.error(f"Error counting prompts with prefix {prompt_prefix}: {e}")

            return 0

    def upload_all_assets(self, assets_dir: str):
        """Uploads all assets from a directory to the static MLflow experiment in a single run.

        Args:
            assets_dir: Local path to the directory containing assets to upload.
        """
        try:

            client = MlflowClient()

            experiment = self.get_or_create_experiment_by_name(client, self._STATIC_ASSET_EXPERIMENT)

            run = client.create_run(experiment.experiment_id, run_name=self._RUN_NAME)

            run_id = run.info.run_id

            logging.info(f"Created run: {run_id}")

            try:

                for entry in sorted(os.scandir(assets_dir), key=lambda e: e.name):

                    if entry.is_dir():

                        logging.info(f"Uploading {entry.name}/")

                        client.log_artifacts(run_id, entry.path, artifact_path=entry.name)

                    elif entry.is_file():

                        logging.info(f"Uploading {entry.name}")

                        client.log_artifact(run_id, entry.path, artifact_path="")

                for root, _, files in os.walk(self._PROMPTS_DIR):

                    for file in sorted(files):

                        abs_path = os.path.join(root, file)
                        rel_path = os.path.relpath(abs_path, self._PROMPTS_DIR)
                        prompt_path = os.path.splitext(rel_path)[0]

                        logging.info(f"Registering prompt '{prompt_path}'")

                        self.upload_prompt(prompt_path)

                client.set_terminated(run_id, status="FINISHED")

                logging.info(f"Upload complete. Run id: {run_id}")

                self._mark_as_latest(client, experiment.experiment_id, run_id)

            except Exception:

                client.set_terminated(run_id, status="FAILED")

                raise

        except Exception as e:

            logging.error(f"Error uploading assets from {assets_dir}: {e}")

            raise e
