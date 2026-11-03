import logging
import os
import mlflow
import litellm
import requests
from .custom_telemetry import CustomTelemetry

logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

_SESSION_PATCHED = False


def _patch_session_forwarded_token():
    """Patches requests.Session.send once to attach X-Forwarded-Access-Token."""
    global _SESSION_PATCHED
    if _SESSION_PATCHED:
        return
    _orig_send = requests.Session.send

    def _send_with_forwarded_token(self, request, **kwargs):
        token = os.environ.get("MLFLOW_TRACKING_TOKEN")
        if token:
            request.headers["X-Forwarded-Access-Token"] = token
        return _orig_send(self, request, **kwargs)

    requests.Session.send = _send_with_forwarded_token
    _SESSION_PATCHED = True


class MlFlowCustomTelemetry(CustomTelemetry):
    """MLflow telemetry provider. Enables OpenAI and LiteLLM autologging for traces, token counts, and latency."""

    _DEFAULT_EXPERIMENT_NAME = None
    _initialized = False
    _SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"

    def _get_default_experiment_name(self) -> str:
        """Returns the experiment name from MLFLOW_EXPERIMENT_NAME or workspace-scoped default."""
        exp_name = os.environ.get("MLFLOW_EXPERIMENT_NAME")
        if exp_name:
            return exp_name
        workspace = os.environ.get("MLFLOW_WORKSPACE")
        if workspace:
            return f"{workspace}/AIP-default"
        return "AIP-default"

    def __init__(self):
        if not os.environ.get("MLFLOW_TRACKING_TOKEN") and os.path.exists(self._SA_TOKEN_PATH):
            try:
                with open(self._SA_TOKEN_PATH) as f:
                    logging.info("Setting MLFLOW_TRACKING_TOKEN from Kubernetes service account token...")
                    os.environ["MLFLOW_TRACKING_TOKEN"] = f.read().strip()
            except Exception as e:
                logging.debug(f"Failed to read SA token: {e}")

        if os.environ.get("MLFLOW_TRACKING_TOKEN"):
            _patch_session_forwarded_token()

        if not MlFlowCustomTelemetry._DEFAULT_EXPERIMENT_NAME:
            MlFlowCustomTelemetry._DEFAULT_EXPERIMENT_NAME = self._get_default_experiment_name()
            logging.info(
                f"MlFlowCustomTelemetry: default experiment resolved to '{self._DEFAULT_EXPERIMENT_NAME}'")

    @classmethod
    def reset(cls):
        """Resets the initialization state (useful for testing)."""
        cls._initialized = False
        cls._DEFAULT_EXPERIMENT_NAME = None

    def track(self):
        if MlFlowCustomTelemetry._initialized:
            logging.debug("MlFlowCustomTelemetry: already initialized, skipping duplicate track() setup.")
            return

        # Ensure OTLP trace-specific env vars are populated from generic OTLP vars if omitted
        otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        otlp_protocol = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
        if otlp_endpoint and not os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
            if "http" in otlp_protocol and not otlp_endpoint.rstrip("/").endswith("/v1/traces"):
                os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = f"{otlp_endpoint.rstrip('/')}/v1/traces"
            else:
                os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = otlp_endpoint
        if otlp_endpoint and not os.environ.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL"):
            os.environ["OTEL_EXPORTER_OTLP_TRACES_PROTOCOL"] = otlp_protocol

        tracking_uri = os.environ.get("MLFLOW_TRACKING_URI")

        logging.debug(f"MlFlowCustomTelemetry.track() called. MLFLOW_TRACKING_URI={tracking_uri}")

        if tracking_uri:
            mlflow.set_tracking_uri(tracking_uri)

        workspace = os.environ.get("MLFLOW_WORKSPACE")
        if workspace and hasattr(mlflow, "set_workspace"):
            try:
                mlflow.set_workspace(workspace)
            except Exception as e:
                logging.debug(f"mlflow.set_workspace('{workspace}') failed: {e}")

        try:
            mlflow.set_experiment(self._DEFAULT_EXPERIMENT_NAME)
        except Exception as e:
            logging.warning(f"mlflow.set_experiment('{self._DEFAULT_EXPERIMENT_NAME}') failed: {e}")

        try:
            mlflow.openai.autolog()
            logging.info("Mlflow OpenAI autologging registered successfully")
        except Exception as e:
            logging.error(f"mlflow.openai.autolog() failed: {e}")

        try:
            mlflow.litellm.autolog()
            logging.info("Mlflow LiteLLM autologging registered successfully")
        except Exception as e:
            logging.error(f"mlflow.litellm.autolog() failed: {e}")

        if not hasattr(litellm, "callbacks") or not isinstance(litellm.callbacks, list):
            litellm.callbacks = ["mlflow"]
        elif "mlflow" not in litellm.callbacks:
            litellm.callbacks.append("mlflow")

        MlFlowCustomTelemetry._initialized = True
