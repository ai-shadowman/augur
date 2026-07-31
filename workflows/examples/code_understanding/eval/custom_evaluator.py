import os
from abc import ABC, abstractmethod

_DEFAULT_EVAL_DATASET = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__),
        "..", "assets", "datasets", "eval", "code_understanding.csv",
    )
)


class CustomEvaluator(ABC):

    @abstractmethod
    def evaluate(self, input: str, graphrag_source_dir: str, git_slug: str = None):
        """Evaluates a single input against a GraphRAG index using LLM-as-judge.

        Args:
            input: The question or prompt to evaluate.
            graphrag_source_dir: Root directory of the GraphRAG index.
            git_slug: Optional repository slug used to scope results.

        Returns:
            dict of metric scores and metadata for the evaluated input.
        """

    @abstractmethod
    def evaluate_with_dataset(
        self,
        graphrag_source_dir: str,
        eval_dataset_file: str = _DEFAULT_EVAL_DATASET,
        git_slug: str = None,
    ):
        """Runs evaluate() for every row in a CSV dataset and uploads the results.

        Args:
            graphrag_source_dir: Root directory of the GraphRAG index
                (must contain output/*.parquet files).
            eval_dataset_file: Path to the evaluation CSV. Defaults to
                assets/datasets/eval/code_understanding.csv.
            git_slug: Optional repository slug used to scope results and artifact paths.

        Returns:
            The updated pandas DataFrame with "answer" and metric columns populated.
        """
