from __future__ import annotations

import csv
import io
import logging
import os
from typing import Any

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


def aggregate_evaluation_scores(evaluation_csv: str | None) -> dict[str, Any] | None:
    """Compute aggregate scores from an evaluation report CSV string."""
    if not evaluation_csv:
        return None

    reader = csv.reader(io.StringIO(evaluation_csv))
    rows = list(reader)
    if len(rows) < 2:
        return None

    headers = rows[0]
    score_cols = [(i, h) for i, h in enumerate(headers) if "/v1/score" in h]
    if not score_cols:
        _basic_cols = {
            "faithfulness",
            "answer_relevance",
            "answer_similarity",
            "answer_correctness",
        }
        score_cols = [(i, h) for i, h in enumerate(headers) if h.strip() in _basic_cols]
    if not score_cols:
        return None

    data_rows = rows[1:]
    total = len(data_rows)

    def _cell(row: list[str], idx: int) -> str:
        return row[idx] if idx < len(row) else ""

    def _parse(val: str) -> float | None:
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    PASS_RATE_EXCLUDES = {"answer similarity"}

    metrics: list[dict[str, Any]] = []
    for col_idx, col_name in score_cols:
        vals = [v for r in data_rows if (v := _parse(_cell(r, col_idx))) is not None]
        count = len(vals)
        avg = sum(vals) / count if count else 0.0
        pass3 = sum(1 for v in vals if v >= 3)
        pass4 = sum(1 for v in vals if v >= 4)
        name = col_name.replace("/v1/score", "").replace("_", " ").strip()
        excluded = name in PASS_RATE_EXCLUDES
        metric: dict[str, Any] = {"name": name, "average": round(avg, 2)}
        if not excluded:
            metric["pass_rate_3"] = round(pass3 / count * 100, 1) if count else 0.0
            metric["pass_rate_4"] = round(pass4 / count * 100, 1) if count else 0.0
        metrics.append(metric)

    pr_metrics = [m for m in metrics if "pass_rate_3" in m]
    avg_pass3 = (
        round(sum(m["pass_rate_3"] for m in pr_metrics) / len(pr_metrics), 1)
        if pr_metrics
        else 0.0
    )
    avg_pass4 = (
        round(sum(m["pass_rate_4"] for m in pr_metrics) / len(pr_metrics), 1)
        if pr_metrics
        else 0.0
    )

    return {
        "metrics": metrics,
        "average_pass_rate_3": avg_pass3,
        "average_pass_rate_4": avg_pass4,
        "total_rows": total,
    }
