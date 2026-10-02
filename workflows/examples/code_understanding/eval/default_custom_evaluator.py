import os

from .custom_evaluator import CustomEvaluator, _DEFAULT_EVAL_DATASET
from .basic_custom_evaluator import BasicCustomEvaluator
from .mlflow_custom_evaluator import MlFlowCustomEvaluator


class DefaultCustomEvaluator(CustomEvaluator):
    """Delegates to BasicCustomEvaluator or MlFlowCustomEvaluator based on the CUSTOM_EVALUATOR env var."""

    def __init__(self):
        if os.getenv("CUSTOM_EVALUATOR") == "mlflow":
            self._evaluator = MlFlowCustomEvaluator()
        else:
            self._evaluator = BasicCustomEvaluator()

    def evaluate(self, *args, **kwargs):
        return self._evaluator.evaluate(*args, **kwargs)

    def evaluate_with_dataset(self, *args, **kwargs):
        return self._evaluator.evaluate_with_dataset(*args, **kwargs)
