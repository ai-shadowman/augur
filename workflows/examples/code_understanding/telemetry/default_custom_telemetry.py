import os

from .basic_custom_telemetry import BasicCustomTelemetry
from .custom_telemetry import CustomTelemetry
from .mlflow_custom_telemetry import MlFlowCustomTelemetry


class DefaultCustomTelemetry(CustomTelemetry):
    """Delegates to MlFlowCustomTelemetry or BasicCustomTelemetry based on the CUSTOM_EVALUATOR env var."""

    def __init__(self):

        if os.getenv("CUSTOM_EVALUATOR") == "mlflow":

            self._telemetry = MlFlowCustomTelemetry()

        else:

            self._telemetry = BasicCustomTelemetry()

    def track(self):

        self._telemetry.track()
