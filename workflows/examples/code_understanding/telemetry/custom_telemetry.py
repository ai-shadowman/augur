from abc import ABC, abstractmethod


class CustomTelemetry(ABC):
    """Abstract base class for telemetry providers."""

    @abstractmethod
    def track(self):
        """Enable telemetry instrumentation for LLM calls."""
