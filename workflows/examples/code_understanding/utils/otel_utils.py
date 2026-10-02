import functools


def enable_telemetry(fn):
    """Decorator that initializes telemetry tracking before the wrapped function runs."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            from telemetry.default_custom_telemetry import DefaultCustomTelemetry
            DefaultCustomTelemetry().track()
        except Exception as e:
            import logging
            logging.warning(f"enable_telemetry: telemetry setup failed, continuing without telemetry: {e}")
        return fn(*args, **kwargs)
    return wrapper
