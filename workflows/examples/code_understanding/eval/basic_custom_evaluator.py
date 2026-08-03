import asyncio
import json
import logging
import os
import tempfile

import pandas as pd
from litellm import completion

from .custom_evaluator import CustomEvaluator, _DEFAULT_EVAL_DATASET
from utils.graphrag_utils import DependencyAnalyzer

logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())


def _llm_kwargs(prefix: str) -> dict:
    """Builds litellm completion kwargs from {prefix}_LLM_* env vars."""
    return {
        "model": f"{os.getenv(f'{prefix}_LLM_PROVIDER')}/{os.getenv(f'{prefix}_LLM_ID')}",
        "api_base": os.getenv(f"{prefix}_LLM_API_BASE"),
        "api_key": os.getenv(f"{prefix}_LLM_TOKEN"),
    }


class BasicCustomEvaluator(CustomEvaluator):
    """LLM-as-judge evaluator using direct LiteLLM calls without an external evaluation framework.
    """

    _JUDGE_PROMPT = """Evaluate the following answer against the reference answer.

Question: {question}

Reference Answer:
{reference}

Actual Answer:
{actual}

Rate the actual answer on each dimension from 0.0 to 1.0:
- faithfulness: Does the actual answer stay true to the reference without hallucinations?
- relevancy: Does the actual answer address the question?
- completeness: Does the actual answer cover the key points from the reference?

Respond ONLY with valid JSON in this exact format:
{{"faithfulness": <score>, "relevancy": <score>, "completeness": <score>, "reasoning": "<brief explanation>"}}"""

    def evaluate(self, input: str, graphrag_source_dir: str, git_slug: str = None):
        """Evaluates a GraphRAG response using LLM-as-judge with a generated reference answer.

        Queries GraphRAG with the input, generates a reference answer via GROUND_TRUTH_LLM,
        then scores the response using JUDGE_LLM.

        Args:
            input: The question or prompt to evaluate.
            graphrag_source_dir: Root directory of the GraphRAG index (must contain output/*.parquet).
            git_slug: Optional repository slug used to scope results.

        Returns:
            dict with keys: faithfulness, relevancy, completeness, reasoning,
            question, actual_answer, reference_answer.
        """
        try:

            analyzer = DependencyAnalyzer(root_dir=graphrag_source_dir)

            actual_answer = asyncio.run(analyzer.query_with_llm(input))

            reference_response = completion(
                **_llm_kwargs("GROUND_TRUTH"),
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a senior software architect providing authoritative "
                            "reference answers about code structure and dependencies."
                        ),
                    },
                    {"role": "user", "content": input},
                ],
            )

            reference_answer = reference_response.choices[0].message.content

            judge_response = completion(
                **_llm_kwargs("JUDGE"),
                messages=[
                    {
                        "role": "user",
                        "content": self._JUDGE_PROMPT.format(
                            question=input,
                            reference=reference_answer,
                            actual=actual_answer,
                        ),
                    }
                ],
            )

            metrics = json.loads(judge_response.choices[0].message.content.strip())

            metrics["question"] = input
            metrics["actual_answer"] = actual_answer
            metrics["reference_answer"] = reference_answer

            logging.info(
                f"Evaluation complete: faithfulness={metrics.get('faithfulness')}, "
                f"relevancy={metrics.get('relevancy')}, "
                f"completeness={metrics.get('completeness')}"
            )

            return metrics

        except Exception as e:

            logging.error(f"Error during basic evaluation: {e}")

            raise e

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
        from loaders.default_asset_loader import DefaultAssetLoader

        df = pd.read_csv(eval_dataset_file)

        for idx, row in df.iterrows():

            question = str(row["question"])

            one_shot = row.get("one_shot_example", "")

            if pd.notna(one_shot) and str(one_shot).strip():

                input_text = f"{question}\n{one_shot}"

            else:

                input_text = question

            try:

                result = self.evaluate(input_text, graphrag_source_dir, git_slug=git_slug)

                df.at[idx, "answer"] = result.get("actual_answer", "")

                for key, value in result.items():

                    if key not in ("question", "actual_answer"):

                        df.at[idx, key] = value

            except Exception as e:

                logging.error(f"Error evaluating row {idx}: {e}")

                df.at[idx, "answer"] = f"ERROR: {e}"

        slug = git_slug or "multi_repo"

        result_file = os.path.join(tempfile.gettempdir(), f"code_understanding_results_{slug}.csv")

        df.to_csv(result_file, index=False)

        DefaultAssetLoader().log_results(
            result_file,
            artifact_path=f"results/evaluations/{slug}",
            tags={"category": "evaluation", **({"git_slug": git_slug} if git_slug else {})},
        )

        return df
