import asyncio
import logging
import os

import mlflow
import pandas as pd
import requests
from mlflow.metrics.genai import faithfulness, answer_relevance, answer_similarity

from .custom_evaluator import CustomEvaluator
from ..utils.graphrag_utils import DependencyAnalyzer

logging.basicConfig(level=logging.INFO)


class MlFlowCustomEvaluator(CustomEvaluator):
    """LLM-as-judge evaluator backed by MLflow's genai evaluation API.

    Uses GROUND_TRUTH_LLM to generate a reference answer and JUDGE_LLM as
    the evaluator model for MLflow's built-in faithfulness, answer_relevance,
    and answer_similarity metrics.

    Reads model configuration from environment variables:
      GROUND_TRUTH_LLM_PROVIDER, GROUND_TRUTH_LLM_ID, GROUND_TRUTH_LLM_API_BASE, GROUND_TRUTH_LLM_TOKEN
      JUDGE_LLM_PROVIDER, JUDGE_LLM_ID, JUDGE_LLM_API_BASE, JUDGE_LLM_TOKEN

    Authentication against MLflow tracking server follows the same pattern as
    MlFlowAssetLoader: reads MLFLOW_TRACKING_TOKEN or falls back to the
    Kubernetes service account token.
    """

    _EXPERIMENT_NAME = f"/{os.environ.get('MLFLOW_WORKSPACE', 'demo')}/code-refactoring/evaluations"
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

        # MLflow genai metrics use the OpenAI client internally; point it at the
        # configured judge endpoint so any OpenAI-compatible provider works.
        if judge_api_base:
            os.environ.setdefault("OPENAI_API_BASE", judge_api_base)

        if judge_token:
            os.environ.setdefault("OPENAI_API_KEY", judge_token)

        return f"openai:/{judge_id}"

    def _ground_truth_answer(self, input: str) -> str:
        """Generates a reference answer using GROUND_TRUTH_LLM via LiteLLM."""
        from litellm import completion

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
                {"role": "user", "content": input},
            ],
        )

        return response.choices[0].message.content

    def evaluate(self, input: str, graphrag_source_dir: str):
        """Evaluates a GraphRAG response using MLflow's genai evaluation API.

        Queries GraphRAG with the input, generates a reference answer via GROUND_TRUTH_LLM,
        then runs MLflow faithfulness, answer_relevance, and answer_similarity metrics
        using JUDGE_LLM and logs the results to the MLflow tracking server.

        Args:
            input: The question or prompt to evaluate.
            graphrag_source_dir: Root directory of the GraphRAG index (must contain output/*.parquet).

        Returns:
            dict of MLflow evaluation metric scores for the single sample.
        """
        try:

            analyzer = DependencyAnalyzer(root_dir=graphrag_source_dir)

            actual_answer = asyncio.run(analyzer.query_with_llm(input))

            reference_answer = self._ground_truth_answer(input)

            judge_model = self._judge_model_uri()

            eval_data = pd.DataFrame({
                "inputs": [input],
                "targets": [reference_answer],
                "predictions": [actual_answer],
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
                    model_type="question-answering",
                    extra_metrics=[
                        faithfulness(model=judge_model),
                        answer_relevance(model=judge_model),
                        answer_similarity(model=judge_model),
                    ],
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
