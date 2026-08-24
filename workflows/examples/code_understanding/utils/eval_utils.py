import logging
import os

import pandas as pd

logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())


def load_evaluation_results(results, experiment_name: str, eval_tags: dict) -> pd.DataFrame | None:

    """Returns per-row evaluation scores from an MLflow EvaluationResult.

    Args:
        results: The MLflow EvaluationResult returned by mlflow.evaluate().
        experiment_name: The MLflow experiment name used for the evaluation run.
        eval_tags: The tags set on the evaluation run, used to locate the artifact.

    Returns:
        A DataFrame of per-row scores, or None if not available.
    """

    eval_results = results.tables.get("eval_results_table")

    if eval_results is not None:

        return eval_results

    logging.info("eval_results_table not in run memory; attempting download via DefaultAssetLoader...")

    try:

        from loaders.default_asset_loader import DefaultAssetLoader

        table_data = DefaultAssetLoader().download(
            "eval_results_table.json",
            experiment_name=experiment_name,
            asset_tags=eval_tags,
        )

        if isinstance(table_data, dict) and "columns" in table_data and "data" in table_data:

            logging.info("eval_results_table downloaded successfully via DefaultAssetLoader.")

            return pd.DataFrame(table_data["data"], columns=table_data["columns"])

    except Exception as e:

        logging.warning(f"Could not download eval_results_table via DefaultAssetLoader: {e}")

    return None
