import json
import logging
import os
import types
from typing import Any, Dict, Optional

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("LITELLM_TELEMETRY", "False")
os.environ.setdefault("DISABLE_LITELLM_VERSION_CHECK", "True")

import mlflow
import litellm
from .custom_telemetry import CustomTelemetry
from utils.pipeline_utils import patch_mlflow_session_forwarded_token as _patch_session_forwarded_token

logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

_OTEL_SPAN_PATCHED = False
_CURRENT_KFP_ATTRS: Dict[str, str] = {}


def _tag_mlflow_trace_info(
    trace_info: Any,
    kfp_attrs: Optional[Dict[str, str]],
    span_name: Optional[str] = None,
) -> None:
    """Populates tags and metadata on an MLflow TraceInfo/TraceInfoV3 object."""
    if trace_info is None or not kfp_attrs:
        return
    try:
        mlflow_tags: Dict[str, str] = {}
        for attr_k, attr_v in kfp_attrs.items():
            if attr_v:
                mlflow_tags[attr_k] = str(attr_v)
        if span_name:
            mlflow_tags["mlflow.traceName"] = span_name

        tags_dict = getattr(trace_info, "tags", None)
        if isinstance(tags_dict, dict):
            tags_dict.update(mlflow_tags)

        for meta_field in ("trace_metadata", "request_metadata"):
            meta_dict = getattr(trace_info, meta_field, None)
            if isinstance(meta_dict, dict):
                for attr_k, attr_v in kfp_attrs.items():
                    if attr_v:
                        meta_dict[attr_k] = str(attr_v)
    except Exception:
        pass


def _enrich_otel_span(span: Any, kfp_attrs: Optional[Dict[str, str]] = None) -> None:
    """Enriches an OpenTelemetry Span or ReadableSpan with KFP metadata and parsed token usage."""
    try:
        attrs_to_apply = kfp_attrs if kfp_attrs is not None else _CURRENT_KFP_ATTRS
        raw_attrs = getattr(span, "attributes", None) or getattr(span, "_attributes", None)
        current_attrs: Dict[str, Any] = dict(raw_attrs) if raw_attrs else {}
        changed = False

        if attrs_to_apply:
            for attr_k, attr_v in attrs_to_apply.items():
                if current_attrs.get(attr_k) != attr_v:
                    current_attrs[attr_k] = attr_v
                    changed = True

            # Tag the active MLflow Trace if context is active
            if getattr(span, "_end_time", True) is None and hasattr(mlflow, "update_current_trace"):
                try:
                    mlflow_tags = {attr_k: attr_v for attr_k, attr_v in attrs_to_apply.items() if attr_v}
                    mlflow.update_current_trace(tags=mlflow_tags, metadata=mlflow_tags)
                except Exception:
                    pass

        raw_usage = current_attrs.get("mlflow.chat.tokenUsage")
        if raw_usage:
            usage = json.loads(raw_usage) if isinstance(raw_usage, str) else raw_usage
            if isinstance(usage, dict):
                in_t = usage.get("input_tokens", usage.get("prompt_tokens"))
                out_t = usage.get("output_tokens", usage.get("completion_tokens"))
                tot_t = usage.get("total_tokens")
                if in_t is not None:
                    current_attrs["gen_ai.usage.input_tokens"] = int(in_t)
                    changed = True
                if out_t is not None:
                    current_attrs["gen_ai.usage.output_tokens"] = int(out_t)
                    changed = True
                if tot_t is not None:
                    current_attrs["gen_ai.usage.total_tokens"] = int(tot_t)
                    changed = True
                if tot_t is not None and hasattr(span, "_name") and isinstance(span._name, str) and span._name:
                    if "tokens:" not in span._name:
                        in_s = int(in_t) if in_t is not None else 0
                        out_s = int(out_t) if out_t is not None else 0
                        tot_s = int(tot_t)
                        span._name = f"{span._name} (tokens: {tot_s} | in: {in_s}, out: {out_s})"

        # Also update MLflow's InMemoryTraceManager directly by OTel trace_id (works for async no-context spans)
        if attrs_to_apply:
            try:
                from mlflow.tracing.trace_manager import InMemoryTraceManager

                tm = InMemoryTraceManager.get_instance()
                ctx = getattr(span, "context", None) or getattr(span, "_context", None)
                otel_tid = getattr(ctx, "trace_id", None) if ctx else None
                if otel_tid is not None and hasattr(tm, "get_trace_id_from_otel_id"):
                    mlflow_tid = tm.get_trace_id_from_otel_id(otel_tid)
                    traces_dict = getattr(tm, "_traces", None)
                    if mlflow_tid and isinstance(traces_dict, dict) and mlflow_tid in traces_dict:
                        tr_info = getattr(traces_dict[mlflow_tid], "info", None)
                        _tag_mlflow_trace_info(tr_info, attrs_to_apply, getattr(span, "_name", None))
            except Exception:
                pass

        if changed:
            if getattr(span, "_end_time", True) is None and hasattr(span, "set_attribute"):
                for k, v in current_attrs.items():
                    try:
                        span.set_attribute(k, v)
                    except Exception:
                        pass
            target_attrs = getattr(span, "_attributes", None)
            if target_attrs is not None and hasattr(target_attrs, "_dict"):
                try:
                    target_attrs._dict.update(current_attrs)
                except Exception:
                    pass
            try:
                span._attributes = types.MappingProxyType(current_attrs)
            except Exception:
                pass
    except Exception:
        pass


def _install_otel_span_hooks() -> None:
    """Patches OpenTelemetry SDK Span.end, SpanProcessor.on_end, and MLflow InMemoryTraceManager once."""
    global _OTEL_SPAN_PATCHED
    try:
        from mlflow.tracing.trace_manager import InMemoryTraceManager

        if not getattr(InMemoryTraceManager, "_kfp_register_patched", False):
            _orig_register_trace = InMemoryTraceManager.register_trace

            def _patched_register_trace(self, otel_trace_id, trace_info):
                _tag_mlflow_trace_info(trace_info, _CURRENT_KFP_ATTRS)
                return _orig_register_trace(self, otel_trace_id, trace_info)

            InMemoryTraceManager.register_trace = _patched_register_trace

            if hasattr(InMemoryTraceManager, "pop_trace"):
                _orig_pop_trace = InMemoryTraceManager.pop_trace

                def _patched_pop_trace(self, otel_trace_id):
                    trace = _orig_pop_trace(self, otel_trace_id)
                    if trace is not None:
                        tr_info = getattr(trace, "info", None)
                        span_name = None
                        try:
                            span_dict = getattr(trace, "span_dict", None) or {}
                            for live_span in span_dict.values():
                                if getattr(live_span, "parent_id", None) is None:
                                    inner = getattr(live_span, "_span", None) or live_span
                                    _enrich_otel_span(inner, _CURRENT_KFP_ATTRS)
                                    span_name = getattr(inner, "_name", None) or getattr(live_span, "name", None)
                                    break
                        except Exception:
                            pass
                        _tag_mlflow_trace_info(tr_info, _CURRENT_KFP_ATTRS, span_name)
                    return trace

                InMemoryTraceManager.pop_trace = _patched_pop_trace

            setattr(InMemoryTraceManager, "_kfp_register_patched", True)
    except Exception as e:
        logging.debug(f"MlFlowCustomTelemetry: failed to patch InMemoryTraceManager: {e}")

    if _OTEL_SPAN_PATCHED:
        return
    try:
        from opentelemetry.sdk.trace import Span as SdkSpan
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

        _orig_span_end = SdkSpan.end

        def _patched_span_end(self, end_time=None):
            _enrich_otel_span(self)
            return _orig_span_end(self, end_time=end_time)

        SdkSpan.end = _patched_span_end

        for proc_cls in (BatchSpanProcessor, SimpleSpanProcessor):
            _orig_on_end = proc_cls.on_end

            def _make_patched_on_end(orig_fn):
                def _patched_on_end(self, span):
                    _enrich_otel_span(span)
                    return orig_fn(self, span)

                return _patched_on_end

            proc_cls.on_end = _make_patched_on_end(_orig_on_end)

        _OTEL_SPAN_PATCHED = True
    except Exception as e:
        logging.debug(f"MlFlowCustomTelemetry: failed to install OTel span hooks: {e}")


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
        _CURRENT_KFP_ATTRS.clear()
        _CURRENT_KFP_ATTRS.update(attrs)
        _install_otel_span_hooks()

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
                    _enrich_otel_span(span, self._attributes)

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
        if otlp_endpoint and os.environ.get("MLFLOW_TRACKING_URI"):
            os.environ.setdefault("MLFLOW_TRACE_ENABLE_OTLP_DUAL_EXPORT", "true")

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

        # Re-apply OTel resource/span hooks after MLflow tracking URI, experiment, and autolog setup
        self._configure_otel_resource_attributes()

        MlFlowCustomTelemetry._initialized = True
