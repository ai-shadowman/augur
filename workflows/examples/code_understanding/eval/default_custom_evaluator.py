import os

from .custom_evaluator import CustomEvaluator
from .basic_custom_evaluator import BasicCustomEvaluator
from .mlflow_custom_evaluator import MlFlowCustomEvaluator


class DefaultCustomEvaluator(CustomEvaluator):
    """Delegates to LocalCustomEvaluator or MlFlowCustomEvaluator based on the EVALUATOR env var."""

    def __init__(self):

        if os.getenv("CUSTOM_EVALUATOR") == "mlflow":

            self._evaluator = MlFlowCustomEvaluator()

        else:

            self._evaluator = BasicCustomEvaluator()

    def evaluate(self, input: str, graphrag_source_dir: str):

        return self._evaluator.evaluate(input, graphrag_source_dir)
