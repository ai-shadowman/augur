import json
import os
import mlflow
import requests
from mlflow.tracking import MlflowClient
import logging
logging.basicConfig(level=logging.INFO)

from .asset_loader import AssetLoader


class MlFlowAssetLoader(AssetLoader):
    """Loads an asset from the MLflow artifacts registry."""

    _EXPERIMENT_NAME = f"/{os.environ.get('MLFLOW_WORKSPACE', 'demo')}/code-refactoring/assets"
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

            experiment = client.get_experiment_by_name(self._EXPERIMENT_NAME)

            if not experiment:

                experiment = client.get_experiment(client.create_experiment(name=self._EXPERIMENT_NAME))

            self.asset_base_uri = experiment.artifact_location

            logging.info(f"Base absolute storage URI: {self.asset_base_uri}")

        except Exception as e:

            logging.error(f"Error initializing MlFlowAssetLoader: {e}")

            raise e

    def _get_absolute_artifact_uri(self, asset_file_path: str):

        return os.path.join(self.asset_base_uri, asset_file_path)

    def download(self, asset_file_path: str, download_dir: str = None):
        """Downloads and returns the asset from the MLflow artifacts registry.

        Args:
            asset_file_path: Path to the asset file relative to its artifact backend location in MLflow.
            download_dir: Optional directory path to download the artifact to.

        Returns:
            The asset content (parsed dict for .json files, str otherwise), or None if not found.
        """
        try:

            asset_uri = self._get_absolute_artifact_uri(asset_file_path)

            if download_dir is not None:

                os.makedirs(download_dir, exist_ok=True)

            local_path = mlflow.artifacts.download_artifacts(artifact_uri=asset_uri,
                                                             dst_path=download_dir)

            asset_path = os.path.join(local_path, asset_uri)

            if not os.path.exists(asset_path):

                logging.info(f"Asset {asset_uri} not found.")

                return None

            with open(asset_path, "r") as f:

                return json.load(f) if asset_uri.endswith(".json") else f.read()

        except Exception as e:

            logging.error(f"Error downloading asset {asset_file_path}: {e}")

            raise e

    def download_dir(self, asset_dir_path: str, download_dir: str):
        """Downloads a directory from the MLflow artifacts registry to a local directory."""
        try:

            asset_uri = self._get_absolute_artifact_uri(asset_dir_path)

            os.makedirs(download_dir, exist_ok=True)

            mlflow.artifacts.download_artifacts(artifact_uri=asset_uri, dst_path=download_dir)

        except Exception as e:

            logging.error(f"Error downloading asset directory {asset_dir_path}: {e}")

            raise e

    def upload_dir(self, local_dir_path: str, upload_dir: str):
        """Uploads a local directory to the MLflow artifacts registry."""
        try:

            client = MlflowClient()

            experiment = client.get_experiment_by_name(self._EXPERIMENT_NAME)

            if not experiment:

                experiment = client.get_experiment(client.create_experiment(name=self._EXPERIMENT_NAME))

            run = client.create_run(experiment.experiment_id, run_name=self._RUN_NAME)

            client.log_artifacts(run.info.run_id, local_dir_path, artifact_path=upload_dir)

        except Exception as e:

            logging.error(f"Error uploading directory {local_dir_path}: {e}")

            raise e

    def log_results(self, results_path: str, artifact_path: str = None, tags: dict = None):
        """Logs pipeline output artifacts to a new MLflow run."""
        try:

            client = MlflowClient()

            experiment = client.get_experiment_by_name(self._EXPERIMENT_NAME)

            if not experiment:

                experiment = client.get_experiment(client.create_experiment(name=self._EXPERIMENT_NAME))

            with mlflow.start_run(experiment_id=experiment.experiment_id, run_name=self._RUN_NAME) as run:

                if tags:

                    mlflow.set_tags(tags)

                if os.path.isdir(results_path):

                    mlflow.log_artifacts(results_path, artifact_path=artifact_path)

                else:

                    mlflow.log_artifact(results_path, artifact_path=artifact_path)

                logging.info(f"Logged results to run {run.info.run_id}")

        except Exception as e:

            logging.error(f"Error logging results {results_path}: {e}")

            raise e

    def upload(self, asset_file_path: str, upload_dir: str):
        """Uploads a local file to the MLflow artifacts registry.

        Args:
            asset_file_path: Local absolute path to the asset file.
            upload_dir: Artifact path within the MLflow experiment to place the file.
        """
        try:

            client = MlflowClient()

            experiment = client.get_experiment_by_name(self._EXPERIMENT_NAME)

            if not experiment:

                experiment = client.get_experiment(client.create_experiment(name=self._EXPERIMENT_NAME))

            run = client.create_run(experiment.experiment_id, run_name=self._RUN_NAME)

            client.log_artifact(run.info.run_id, asset_file_path, artifact_path=upload_dir)

        except Exception as e:

            logging.error(f"Error uploading asset {asset_file_path}: {e}")

            raise e
