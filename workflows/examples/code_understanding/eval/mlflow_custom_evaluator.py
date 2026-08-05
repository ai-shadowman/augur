import asyncio
import logging
import os
import tempfile

import mlflow
import pandas as pd
import requests
from mlflow.metrics.genai import faithfulness, answer_relevance, answer_similarity, answer_correctness

from .custom_evaluator import CustomEvaluator, _DEFAULT_EVAL_DATASET, build_repo_context
from utils.graphrag_utils import DependencyAnalyzer

logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())


class MlFlowCustomEvaluator(CustomEvaluator):
    """LLM-as-judge evaluator backed by MLflow's genai evaluation API.

    Uses GROUND_TRUTH_LLM to generate a reference answer and JUDGE_LLM as
    the evaluator model for MLflow's built-in faithfulness, answer_relevance,
    and answer_similarity metrics.
    """

    _EXPERIMENT_NAME = f"{os.environ.get('MLFLOW_WORKSPACE', 'demo')}/code-refactoring/evaluations"
    _RUN_NAME = "code-understanding-eval"
    _SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"

    def __init__(self):

        if not os.environ.get("MLFLOW_TRACKING_TOKEN") and os.path.exists(self._SA_TOKEN_PATH):

            with open(self._SA_TOKEN_PATH) as f:

                logging.info("Setting MLFLOW_TRACKING_TOKEN from Kubernetes service account token...")

                os.environ["MLFLOW_TRACKING_TOKEN"] = f.read().strip()

        _token = os.environ.get("MLFLOW_TRACKING_TOKEN")

        if _token:

            _orig_send = requests.Session.send

            def _send_with_forwarded_token(self, request, **kwargs):
                request.headers["X-Forwarded-Access-Token"] = _token
                return _orig_send(self, request, **kwargs)

            requests.Session.send = _send_with_forwarded_token

    def _judge_model_uri(self) -> str:
        """Returns the MLflow judge model URI, using an OpenAI-compatible endpoint."""
        judge_id = os.getenv("JUDGE_LLM_ID")
        judge_api_base = os.getenv("JUDGE_LLM_API_BASE")
        judge_token = os.getenv("JUDGE_LLM_TOKEN")

        if judge_api_base:
            os.environ.setdefault("OPENAI_API_BASE", judge_api_base)

        if judge_token:
            os.environ.setdefault("OPENAI_API_KEY", judge_token)

        return f"openai:/{judge_id}"

    def _ground_truth_answer(self, input: str, graphrag_source_dir: str = "") -> str:
        """Generates a reference answer using GROUND_TRUTH_LLM via LiteLLM."""
        from litellm import completion

        repo_context = build_repo_context(graphrag_source_dir)

        response = completion(
            model=f"{os.getenv('GROUND_TRUTH_LLM_PROVIDER')}/{os.getenv('GROUND_TRUTH_LLM_ID')}",
            api_base=os.getenv("GROUND_TRUTH_LLM_API_BASE"),
            api_key=os.getenv("GROUND_TRUTH_LLM_TOKEN"),
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

        return response.choices[0].message.content

    def evaluate(self, input: str, graphrag_source_dir: str, git_repo: str, git_branch: str,
                 git_slug: str = None, multi_repo: bool = False):
        """Evaluates a GraphRAG response using MLflow's genai evaluation API.

        Args:
            input: The question or prompt to evaluate.
            graphrag_source_dir: Root directory of the GraphRAG index (must contain output/*.parquet).
            git_repo: Repository URL used as context for the ground truth LLM.
            git_branch: Branch name used as context for the ground truth LLM.
            git_slug: Optional repository slug used to scope results.

        Returns:
            dict of MLflow evaluation metric scores for the single sample.
        """
        try:

            analyzer = DependencyAnalyzer(root_dir=graphrag_source_dir)

            actual_answer, context_data = asyncio.run(analyzer.query_with_llm(input, include_context=True))

            context_str = str(context_data) if context_data is not None else ""

            reference_answer = self._ground_truth_answer(input, graphrag_source_dir)

            judge_model = self._judge_model_uri()

            eval_data = pd.DataFrame({
                "inputs": [input],
                "targets": [reference_answer],
                "predictions": [actual_answer],
                "context": [context_str],
            })

            from mlflow.tracking import MlflowClient

            client = MlflowClient()

            experiment = client.get_experiment_by_name(self._EXPERIMENT_NAME)

            if not experiment:

                experiment = client.get_experiment(
                    client.create_experiment(name=self._EXPERIMENT_NAME)
                )

            with mlflow.start_run(
                experiment_id=experiment.experiment_id, run_name=self._RUN_NAME
            ):

                results = mlflow.evaluate(
                    data=eval_data,
                    predictions="predictions",
                    targets="targets",
                    extra_metrics=[
                        faithfulness(model=judge_model),
                        answer_relevance(model=judge_model),
                        answer_similarity(model=judge_model),
                    ],
                    evaluators=[],
                )

            metrics = results.metrics

            metrics["question"] = input
            metrics["actual_answer"] = actual_answer
            metrics["reference_answer"] = reference_answer

            logging.info(f"MLflow evaluation complete: {results.metrics}")

            return metrics

        except Exception as e:

            logging.error(f"Error during MLflow evaluation: {e}")

            raise e

    def evaluate_with_dataset(
        self,
        graphrag_source_dir: str,
        git_repo: str,
        git_branch: str,
        eval_dataset_file: str = _DEFAULT_EVAL_DATASET,
        git_slug: str = None,
        multi_repo: bool = False,
    ):
        """Evaluates all rows in a CSV dataset in a single MLflow run.

        Builds inputs from the question and one_shot_example columns, generates
        reference answers via the ground truth LLM, then calls mlflow.evaluate()
        once using a GraphRAG model callable. Per-row metric scores are mapped
        back from results.tables["eval_results_table"].

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
            The updated pandas DataFrame with "answer" and metric columns populated.
        """
        from loaders.default_asset_loader import DefaultAssetLoader

        from mlflow.tracking import MlflowClient

        from utils import code_utils

        df = pd.read_csv(eval_dataset_file)

        analyzer = DependencyAnalyzer(root_dir=graphrag_source_dir)

        df["inputs"] = df["question"].astype(str) + "\n" + df["one_shot_example"].astype(str)

        df["targets"] = df["inputs"].apply(
            lambda t: self._ground_truth_answer(t, graphrag_source_dir)
        )

        def _query(input_text):

            try:

                answer, _ = asyncio.run(analyzer.query_with_llm(input_text, include_context=False))

            except Exception as e:

                logging.error(f"GraphRAG query failed: {e}")

                answer = f"ERROR: {e}"

            return answer

        df["predictions"] = df["inputs"].apply(_query)

        eval_data = df[["inputs", "targets", "predictions"]]

        judge_model = self._judge_model_uri()

        client = MlflowClient()

        experiment = client.get_experiment_by_name(self._EXPERIMENT_NAME)

        if not experiment:

            experiment = client.get_experiment(
                client.create_experiment(name=self._EXPERIMENT_NAME)
            )

        with mlflow.start_run(experiment_id=experiment.experiment_id, run_name=self._RUN_NAME):

            results = mlflow.evaluate(
                data=eval_data,
                predictions="predictions",
                targets="targets",
                extra_metrics=[
                    faithfulness(model=judge_model),
                    answer_relevance(model=judge_model),
                    answer_similarity(model=judge_model),
                    answer_correctness(model=judge_model),
                ],
                evaluators=[],
            )

        logging.info(f"MLflow batch evaluation complete: {results.metrics}")

        per_row = results.tables.get("eval_results_table")

        if per_row is not None:

            for col in per_row.columns:

                if col not in ("inputs", "targets", "predictions"):

                    df[col] = per_row[col].values

        df["answer"] = df["predictions"]

        df["reference_answer"] = df["targets"]

        slug = git_slug or code_utils.generate_slug_from_repo(git_repo, git_branch)

        artifact_path = (
            f"results/evaluations/multi_repo/{slug}" if multi_repo
            else f"results/evaluations/{slug}"
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
