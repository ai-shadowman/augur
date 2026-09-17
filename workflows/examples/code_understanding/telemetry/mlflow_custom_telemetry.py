import logging
import os
import mlflow
import litellm
from .custom_telemetry import CustomTelemetry

logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())


class MlFlowCustomTelemetry(CustomTelemetry):
    """MLflow telemetry provider. Enables LiteLLM autologging for token counts and latency."""

    _DEFAULT_EXPERIMENT_NAME = None

    def _get_default_experiment_name(self) -> str:
        """Returns the experiment name from the MLFLOW_EXPERIMENT_NAME env var."""
        return os.environ.get("MLFLOW_EXPERIMENT_NAME", "AIP-default")

    def __init__(self):
        if not MlFlowCustomTelemetry._DEFAULT_EXPERIMENT_NAME:
            MlFlowCustomTelemetry._DEFAULT_EXPERIMENT_NAME = self._get_default_experiment_name()

        logging.info(
            f"MlFlowCustomTelemetry: default experiment resolved to '{self._DEFAULT_EXPERIMENT_NAME}'")

    def track(self):
        tracking_uri = os.environ.get("MLFLOW_TRACKING_URI")

        logging.debug(f"MlFlowCustomTelemetry.track() called. MLFLOW_TRACKING_URI={tracking_uri}")

        if tracking_uri:
            mlflow.set_tracking_uri(tracking_uri)

        mlflow.set_experiment(self._DEFAULT_EXPERIMENT_NAME)

        try:
            mlflow.openai.autolog()
            logging.info("Mlflow tracking registered successfully")
        except Exception as e:
            logging.error(f"mlflow.openai.autolog() failed: {e}")

        litellm.callbacks = ["mlflow"]

