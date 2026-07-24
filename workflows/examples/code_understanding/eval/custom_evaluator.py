import logging
import os
import tempfile
from abc import ABC, abstractmethod

import pandas as pd

_DEFAULT_EVAL_DATASET = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__),
        "..", "assets", "datasets", "eval", "code_understanding.csv",
    )
)


class CustomEvaluator(ABC):

    @abstractmethod
    def evaluate(self, input: str, graphrag_source_dir: str):
        """
        Provides an abstract method for providing LLM-as-judge
        reference-guided evaluations
        for input data, using a graphrag index file directory as its data
        source.

        For the evaluator models, it will pull from the environment
        configuration. It will use the configured GROUND_TRUTH_LLM for
        reference-based metrics and the configured JUDGE_LLM for judgement-based metrics.

        Args:
            input: The input string to be used for evaluation.
            graphrag_source_dir: The directory path containing graph resources
            required for the evaluation process.

        Returns:
            None

        Raises:
            NotImplementedError: This method must be implemented by subclasses.
        """

    def evaluate_with_dataset(
        self,
        graphrag_source_dir: str,
        eval_dataset_file: str = _DEFAULT_EVAL_DATASET,
    ):
        """Runs evaluate() for every row in a CSV dataset and uploads the results.

        For each row:
        - The input is constructed by concatenating the "Question"
        and "One-shot example" columns.
        - The actual answer is stored in the "Answer" column.
        - All metric results (scores and reasoning) returned by evaluate()
        are expanded into additional columns.

        The updated dataset is uploaded once after the full loop completes.

        Args:
            graphrag_source_dir: Root directory of the GraphRAG index
                (must contain output/*.parquet files).
            eval_dataset_file: Path to the evaluation CSV. Defaults to
                assets/datasets/eval/code_understanding.csv.

        Returns:
            The updated pandas DataFrame with "Answer" and metric columns populated.
        """
        from ..loaders.default_asset_loader import DefaultAssetLoader

        df = pd.read_csv(eval_dataset_file)

        for idx, row in df.iterrows():

            question = str(row["question"])

            one_shot = row.get("one_shot_example", "")

            if pd.notna(one_shot) and str(one_shot).strip():

                input_text = f"{question}\n{one_shot}"

            else:

                input_text = question

            try:

                result = self.evaluate(input_text, graphrag_source_dir)

                df.at[idx, "answer"] = result.get("actual_answer", "")

                for key, value in result.items():

                    if key not in ("question", "actual_answer"):

                        df.at[idx, key] = value

            except Exception as e:

                logging.error(f"Error evaluating row {idx}: {e}")

                df.at[idx, "answer"] = f"ERROR: {e}"

        result_file = os.path.join(
            tempfile.gettempdir(), "code_understanding_results.csv"
        )

        df.to_csv(result_file, index=False)
        
        DefaultAssetLoader().log_results(
            result_file, artifact_path="results/evaluations"
        )

        return df