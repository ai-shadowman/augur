import asyncio
import logging
import os
import tempfile

import pandas as pd
from litellm import completion

from .custom_evaluator import CustomEvaluator, _DEFAULT_EVAL_DATASET, build_repo_context
from utils.graphrag_utils import DependencyAnalyzer
from utils.json_utils import extract_json_from_string

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

    def evaluate(self, input: str, graphrag_source_dir: str, git_repo: str, git_branch: str,
                 git_slug: str = None, multi_repo: bool = False):
        """Evaluates a GraphRAG response using LLM-as-judge with a generated reference answer.

        Queries GraphRAG with the input, generates a reference answer via GROUND_TRUTH_LLM,
        then scores the response using JUDGE_LLM.

        Args:
            input: The question or prompt to evaluate.
            graphrag_source_dir: Root directory of the GraphRAG index (must contain output/*.parquet).
            git_repo: Repository URL used as context for the ground truth LLM.
            git_branch: Branch name used as context for the ground truth LLM.
            git_slug: Optional repository slug used to scope results.

        Returns:
            dict with keys: faithfulness, relevancy, completeness, reasoning,
            question, actual_answer, reference_answer.
        """
        try:

            analyzer = DependencyAnalyzer(root_dir=graphrag_source_dir, git_slug=git_slug or "", multi_repo=multi_repo)

            actual_answer = asyncio.run(analyzer.query_with_llm(input))

            repo_context = build_repo_context(graphrag_source_dir)

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
                    {
                        "role": "user",
                        "content": f"{repo_context}\n\n{input}" if repo_context else input,
                    },
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

            metrics = extract_json_from_string(judge_response.choices[0].message.content)
            if not isinstance(metrics, dict):
                logging.error("Judge LLM returned no valid JSON object")
                metrics = {}

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

            return {}

    def evaluate_with_dataset(
        self,
        graphrag_source_dir: str,
        git_repo: str,
        git_branch: str,
        eval_dataset_file: str = _DEFAULT_EVAL_DATASET,
        git_slug: str = None,
        multi_repo: bool = False,
    ):
        """Evaluates all rows in a CSV dataset using a DataFrame-based pipeline and uploads the results.

        Builds inputs from question and one_shot_example columns, generates reference answers
        via GROUND_TRUTH_LLM, queries GraphRAG for actual answers, then scores each row
        using JUDGE_LLM.

        Args:
            graphrag_source_dir: Root directory of the GraphRAG index
                (must contain output/*.parquet files).
            git_repo: Repository URL used as context for the ground truth LLM.
            git_branch: Branch name used as context for the ground truth LLM.
            eval_dataset_file: Path to the evaluation CSV. Defaults to
                assets/datasets/eval/code_understanding.csv.
            git_slug: Optional repository slug used to scope results and artifact paths.
            multi_repo: Whether the index spans multiple repositories.

        Returns:
            The updated pandas DataFrame with "answer", "reference_answer", and metric columns populated.
        """
        from loaders.default_asset_loader import DefaultAssetLoader

        df = pd.read_csv(eval_dataset_file)

        analyzer = DependencyAnalyzer(root_dir=graphrag_source_dir, git_slug=git_slug or "", multi_repo=multi_repo)

        df["inputs"] = df.apply(
            lambda row: (
                f"{row['question']}\n\n**Example format:**\n{row['one_shot_example']}"
                if pd.notna(row.get("one_shot_example")) and str(row.get("one_shot_example", "")).strip()
                else str(row["question"])
            ),
            axis=1,
        )

        repo_context = build_repo_context(graphrag_source_dir)

        def _ground_truth(input_text):
            try:
                return completion(
                    **_llm_kwargs("GROUND_TRUTH"),
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are a senior software architect providing authoritative "
                                "reference answers about code structure and dependencies."
                            ),
                        },
                        {
                            "role": "user",
                            "content": f"{repo_context}\n\n{input_text}" if repo_context else input_text,
                        },
                    ],
                ).choices[0].message.content
            except Exception as e:
                logging.error(f"Ground truth LLM failed: {e}")
                return "Could not process query"

        df["targets"] = df["inputs"].apply(_ground_truth)

        def _query(row):
            global_val = row.get("global_query", False)
            use_global = bool(global_val) if pd.notna(global_val) else False
            try:
                return asyncio.run(analyzer.query_with_llm(row["inputs"], use_global=use_global))
            except Exception as e:
                logging.error(f"GraphRAG query failed: {e}")
                return f"ERROR: {e}"

        df["predictions"] = df.apply(_query, axis=1)

        _METRIC_KEYS = ["faithfulness", "relevancy", "completeness", "reasoning"]

        def _judge(row):
            try:
                response = completion(
                    **_llm_kwargs("JUDGE"),
                    messages=[
                        {
                            "role": "user",
                            "content": self._JUDGE_PROMPT.format(
                                question=row["inputs"],
                                reference=row["targets"],
                                actual=row["predictions"],
                            ),
                        }
                    ],
                )
                result = extract_json_from_string(response.choices[0].message.content)
                if not isinstance(result, dict):
                    logging.error("Judge LLM returned no valid JSON object")
                return result if isinstance(result, dict) else {k: None for k in _METRIC_KEYS}
            except Exception as e:
                logging.error(f"Judge LLM failed: {e}")
                return {k: None for k in _METRIC_KEYS}

        metrics_df = pd.DataFrame(df.apply(_judge, axis=1).tolist(), index=df.index)
        for col in metrics_df.columns:
            df[col] = metrics_df[col]

        df["answer"] = df["predictions"]
        df["reference_answer"] = df["targets"]

        from utils import code_utils
        slug = git_slug or code_utils.generate_slug_from_repo(git_repo, git_branch)

        artifact_path = DefaultAssetLoader.get_log_results_artifact_path(

            DefaultAssetLoader.RESULTS_PATH_PREFIX_EVAL,

            git_slug=slug,

            multi_repo=multi_repo,

        )

        df["git_slug"] = slug

        result_file = os.path.join(tempfile.gettempdir(), f"code_understanding_results_{slug}.csv")

        df.to_csv(result_file, index=False)

        DefaultAssetLoader().log_results(
            result_file,
            artifact_path=artifact_path,
            tags={"category": "evaluation", "git_slug": slug},
        )

        return df
