"""Opt-in, content-free timing events; independent of aggregate duration/cost totals.

SDK elapsed time includes transport and opaque provider retries, not GPU time.
Nested/overlapping spans and different instrumentation layers must not be summed.
"""
import functools
import inspect
import json
import logging
import os
import re
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

_current = ContextVar("augur_timing_span", default=None)
_logger = logging.getLogger("augur.timing")
_clock_domain = uuid.uuid4().hex
_numeric_fields = {"prompt_tokens", "output_tokens", "attempts_remaining", "bytes",
                   "payload_regular_files", "payload_logical_bytes", "payload_scan_entries",
                   "payload_scan_errors", "payload_symlinks", "upload_ordinal"}


def _enabled():
    return os.getenv("AUGUR_TIMING_ENABLED", "false").lower() in ("true", "1", "yes")


def timing_enabled():
    """Gate optional measurement work, including filesystem scans."""
    return _enabled()


def _identifier(value):
    # Opaque run/clock/request IDs only, not filesystem paths or URI syntax.
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        return value
    return None


def _model_identifier(value):
    # Namespaces may contain slashes; reject absolute/drive/traversal paths.
    if (isinstance(value, str) and len(value) <= 160
            and re.fullmatch(r"[A-Za-z0-9_.:-]+(?:/[A-Za-z0-9_.:-]+)*", value)
            and not re.match(r"[A-Za-z]:", value)
            and all(part not in (".", "..") for part in value.split("/"))):
        return value
    return None


def response_usage(response):
    """Read actual usage and optional SDK request ID, never response content."""
    try:
        usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
        fields = {}
        for key in ("prompt_tokens", "completion_tokens"):
            value = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
            if isinstance(value, int) and not isinstance(value, bool):
                fields["output_tokens" if key == "completion_tokens" else key] = value
        request_id = getattr(response, "_request_id", None)
        if _identifier(request_id):
            fields["provider_request_id"] = request_id
        return fields
    except Exception:
        return {}


def _emit(event, fields):
    try:
        for key, value in fields.items():
            if key in _numeric_fields and isinstance(value, (int, float)) and not isinstance(value, bool):
                event[key] = value
            elif key in ("stream_requested", "payload_scan_complete", "payload_scan_truncated") and isinstance(value, bool):
                event[key] = value
            elif key == "model" and _model_identifier(value):
                event[key] = value
            elif key == "provider_request_id" and _identifier(value):
                event[key] = value
            elif key in ("payload_key", "mlflow_run_id", "prior_mlflow_run_id", "experiment_id") and isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
                event[key] = value
        _logger.info("AUGUR_TIMING %s", json.dumps(event, sort_keys=True))
    except Exception:
        # Observability must not replace an SDK response or exception.
        pass


def _event(operation, layer):
    parent = _current.get()
    event = {"event": "span_end", "operation": operation, "layer": layer,
             "span_id": uuid.uuid4().hex, "parent_span_id": parent["span_id"] if parent else None,
             "trace_id": parent["trace_id"] if parent else uuid.uuid4().hex,
             "additive": False}
    # Default isolates each process. Override only with an approved shared,
    # synchronized clock domain; cross-process timestamps are otherwise unsafe.
    event["clock_domain"] = _identifier(os.getenv("AUGUR_CLOCK_DOMAIN")) or _clock_domain
    run_id = _identifier(os.getenv("AUGUR_RUN_ID") or os.getenv("PIPELINE_RUN_ID") or os.getenv("MLFLOW_RUN_ID"))
    if run_id:
        event["run_id"] = run_id
    return event


@contextmanager
def timing_span(operation, layer="application", **fields):
    """Emit unique wall timestamps + monotonic elapsed, including failed/cancelled calls."""
    if not _enabled():
        yield {}
        return
    event = _event(operation, layer)
    token = _current.set(event)
    event["start_time"] = time.time()
    start = time.perf_counter()
    event["status"] = "success"
    try:
        yield fields
    except BaseException as exc:
        event["status"] = "failed"
        event["error_type"] = type(exc).__name__
        raise
    finally:
        event["elapsed_seconds"] = time.perf_counter() - start
        event["end_time"] = time.time()
        _current.reset(token)
        _emit(event, fields)


def timed(operation, layer="application"):
    """Preserve sync/async behavior while adding an invocation span."""
    def decorate(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                with timing_span(operation, layer):
                    return await fn(*args, **kwargs)
            return async_wrapper
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with timing_span(operation, layer):
                return fn(*args, **kwargs)
        return wrapper
    return decorate


def record_callback_timing(start_time, end_time, *, model=None, error=None):
    """LiteLLM envelope; may overlap SDK events and is not additive."""
    if not _enabled():
        return
    try:
        start = start_time.timestamp() if hasattr(start_time, "timestamp") else float(start_time)
        end = end_time.timestamp() if hasattr(end_time, "timestamp") else float(end_time)
        event = _event("litellm.request", "litellm")
        event.update(start_time=start, end_time=end, elapsed_seconds=max(0.0, end-start),
                     status="failed" if error is not None else "success")
        if error is not None:
            event["error_type"] = type(error).__name__
        _emit(event, {"model": model})
    except Exception:
        pass
