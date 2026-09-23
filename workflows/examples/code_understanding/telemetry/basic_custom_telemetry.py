from .custom_telemetry import CustomTelemetry


class BasicCustomTelemetry(CustomTelemetry):
    """No-op telemetry provider."""

    def track(self):
        pass
