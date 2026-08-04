import logging
import os
import pathlib
from abc import ABC, abstractmethod

_DEFAULT_EVAL_DATASET = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__),
        "..", "assets", "datasets", "eval", "code_understanding.csv",
    )
)


def read_codebase_context(graphrag_source_dir: str) -> str:

    """Return the codebase source files as an LLM-ready string via gitingest."""

    codebase_dir = pathlib.Path(graphrag_source_dir) / "input"

    if not codebase_dir.is_dir():

        logging.warning(
            "Codebase directory %s not found; ground truth LLM will answer without source context.",
            codebase_dir,
        )

        return ""

    try:

        from gitingest import ingest

        _, _, content = ingest(str(codebase_dir))

        return f"Source code of the codebase being analyzed:\n\n{content}"

    except ImportError:

        logging.warning("gitingest is not installed; ground truth LLM will answer without source context.")

        return ""

    except Exception as e:

        logging.warning("Failed to ingest codebase with gitingest: %s", e)

        return ""


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
