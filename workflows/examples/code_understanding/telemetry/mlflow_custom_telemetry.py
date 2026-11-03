import logging
import os
from typing import Dict, Optional
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
    _SA_NAMESPACE_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
    _PROC_CMDLINE_PATH = "/proc/1/cmdline"

    def _get_default_experiment_name(self) -> str:
        """Returns the experiment name from MLFLOW_EXPERIMENT_NAME or workspace-scoped default."""
        exp_name = os.environ.get("MLFLOW_EXPERIMENT_NAME")
        if exp_name:
            return exp_name
        workspace = os.environ.get("MLFLOW_WORKSPACE")
        if workspace:
            return f"{workspace}/AIP-default"
        return "AIP-default"

    @staticmethod
    def _clean_placeholder(val: Optional[str]) -> Optional[str]:
        """Returns stripped value if non-empty and not an unexpanded KFP/Argo placeholder."""
        if not val:
            return None
        val = val.strip()
        if not val or val.startswith("{{"):
            return None
        return val

    @classmethod
    def _resolve_kfp_metadata(cls) -> Dict[str, str]:
        """Resolves Kubeflow Pipeline run/task metadata and pod name from env, /proc/1/cmdline, or K8s pod labels."""
        meta: Dict[str, str] = {}

        run_id = (
            cls._clean_placeholder(os.environ.get("KFP_RUN_ID"))
            or cls._clean_placeholder(os.environ.get("PIPELINE_RUN_ID"))
            or cls._clean_placeholder(os.environ.get("AUGUR_RUN_ID"))
        )
        run_name = cls._clean_placeholder(os.environ.get("KFP_RUN_NAME"))
        task_name = cls._clean_placeholder(os.environ.get("KFP_TASK_NAME"))
        pod_name = cls._clean_placeholder(os.environ.get("HOSTNAME"))

        # Fallback 1: inspect /proc/1/cmdline for KFP v2 launcher_v2 --run_id flag
        if not run_id and os.path.exists(cls._PROC_CMDLINE_PATH):
            try:
                with open(cls._PROC_CMDLINE_PATH, "rb") as f:
                    args = [a.decode("utf-8", errors="ignore") for a in f.read().split(b"\x00") if a]
                for idx, arg in enumerate(args):
                    if arg == "--run_id" and idx + 1 < len(args):
                        run_id = cls._clean_placeholder(args[idx + 1])
                        break
                    if arg.startswith("--run_id="):
                        run_id = cls._clean_placeholder(arg.split("=", 1)[1])
                        break
            except Exception as e:
                logging.debug(f"MlFlowCustomTelemetry: failed to read {cls._PROC_CMDLINE_PATH}: {e}")

        # Fallback 2: query in-cluster Kubernetes Pod labels (pipeline/runid, workflows.argoproj.io/workflow)
        if not run_id and pod_name and os.path.exists(cls._SA_TOKEN_PATH):
            try:
                from kubernetes import client, config

                config.load_incluster_config()
                namespace = os.environ.get("KFP_NAMESPACE", "default")
                if os.path.exists(cls._SA_NAMESPACE_PATH):
                    with open(cls._SA_NAMESPACE_PATH, "r", encoding="utf-8") as f:
                        namespace = f.read().strip() or namespace
                pod = client.CoreV1Api().read_namespaced_pod(name=pod_name, namespace=namespace)
                labels = (pod.metadata and pod.metadata.labels) or {}
                run_id = (
                    cls._clean_placeholder(labels.get("pipeline/runid"))
                    or cls._clean_placeholder(labels.get("workflows.argoproj.io/workflow"))
                )
                if not run_name:
                    run_name = cls._clean_placeholder(labels.get("workflows.argoproj.io/workflow"))
            except Exception as e:
                logging.debug(f"MlFlowCustomTelemetry: failed to read K8s pod labels for run_id: {e}")

        if run_id:
            meta["kfp.run_id"] = run_id
        if run_name:
            meta["kfp.run_name"] = run_name
        if task_name:
            meta["kfp.task_name"] = task_name
        if pod_name:
            meta["k8s.pod.name"] = pod_name

        return meta

    @classmethod
    def _configure_otel_resource_attributes(cls) -> Dict[str, str]:
        """Merges resolved KFP/pod metadata into OTEL_RESOURCE_ATTRIBUTES and active OTel TracerProviders."""
        meta = cls._resolve_kfp_metadata()
        if not meta:
            return {}

        existing_raw = os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "").strip()
        attrs: Dict[str, str] = {}
        if existing_raw:
            for item in existing_raw.split(","):
                item = item.strip()
                if "=" in item:
                    k, v = item.split("=", 1)
                    cleaned_v = cls._clean_placeholder(v)
                    if cleaned_v:
                        attrs[k.strip()] = cleaned_v

        for k, v in meta.items():
            safe_val = v.replace(",", "_").replace("=", "_").strip()
            if safe_val:
                attrs[k] = safe_val

        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = ",".join(f"{k}={v}" for k, v in attrs.items())

        # Also patch any already-initialized OpenTelemetry TracerProvider and attach a SpanProcessor
        # so KFP metadata is present on both Resource (resource.kfp.run_id) and Span (.kfp.run_id)
        try:
            from opentelemetry import trace as otel_trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import SpanProcessor

            class _KfpMetadataSpanProcessor(SpanProcessor):
                def __init__(self, attributes: Dict[str, str]):
                    self._attributes = dict(attributes)

                def on_start(self, span, parent_context=None):
                    for attr_k, attr_v in self._attributes.items():
                        try:
                            span.set_attribute(attr_k, attr_v)
                        except Exception:
                            pass

                def on_end(self, span):
                    pass

                def shutdown(self):
                    pass

                def force_flush(self, timeout_millis: int = 30000) -> bool:
                    return True

            providers = [otel_trace.get_tracer_provider()]
            try:
                from mlflow.tracing.provider import _get_tracer
                mlflow_tracer = _get_tracer(__name__)
                for candidate in (
                    getattr(mlflow_tracer, "tracer_provider", None),
                    getattr(getattr(mlflow_tracer, "_tracer", None), "tracer_provider", None),
                ):
                    if candidate is not None and candidate not in providers:
                        providers.append(candidate)
            except Exception:
                pass

            extra_resource = Resource.create(attrs)
            for provider in providers:
                if provider is None:
                    continue
                if hasattr(provider, "_resource") and provider._resource is not None:
                    try:
                        provider._resource = provider._resource.merge(extra_resource)
                    except Exception:
                        pass
                if hasattr(provider, "add_span_processor") and not getattr(provider, "_kfp_metadata_attached", False):
                    try:
                        provider.add_span_processor(_KfpMetadataSpanProcessor(attrs))
                        setattr(provider, "_kfp_metadata_attached", True)
                    except Exception:
                        pass
        except Exception as e:
            logging.debug(f"MlFlowCustomTelemetry: optional OTel provider resource patch skipped: {e}")

        logging.info(
            f"MlFlowCustomTelemetry: tagging traces via OTEL_RESOURCE_ATTRIBUTES with {meta}"
        )
        return meta

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

        # Tag traces with Kubeflow Pipeline run/task metadata and pod name
        self._configure_otel_resource_attributes()

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
