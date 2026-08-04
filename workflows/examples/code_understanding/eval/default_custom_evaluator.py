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

    def evaluate(self, input: str, graphrag_source_dir: str, git_repo: str, git_branch: str,
                 git_slug: str = None, multi_repo: bool = False):

        return self._evaluator.evaluate(input, graphrag_source_dir, git_repo, git_branch,
                                        git_slug=git_slug, multi_repo=multi_repo)

    def evaluate_with_dataset(
        self,
        graphrag_source_dir: str,
        git_repo: str,
        git_branch: str,
        eval_dataset_file: str = _DEFAULT_EVAL_DATASET,
        git_slug: str = None,
        multi_repo: bool = False,
    ):

        return self._evaluator.evaluate_with_dataset(graphrag_source_dir, git_repo, git_branch,
                                                     eval_dataset_file, git_slug=git_slug,
                                                     multi_repo=multi_repo)
