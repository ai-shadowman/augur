import functools
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional


class DurationTracker:
    """Tracks step and stage durations across pipeline executions using a singleton pattern."""

    _global_instance: Optional["DurationTracker"] = None

    @classmethod
    def get_instance(cls) -> "DurationTracker":
        """Returns the shared global DurationTracker instance."""
        if cls._global_instance is None:
            cls._global_instance = cls()
        return cls._global_instance

    @classmethod
    def reset_instance(cls) -> "DurationTracker":
        """Resets and returns the global singleton instance."""
        cls._global_instance = cls()
        return cls._global_instance

    def __init__(self):
        self.records: List[Dict[str, Any]] = []
        self._active_measurements: List[Dict[str, Any]] = []

    def reset(self):
        """Clears all recorded timing records."""
        self.records.clear()
        self._active_measurements.clear()

    def record_step(
        self,
        stage: str,
        step: str,
        duration: float,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        status: str = "success",
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """Records a single step timing record."""
        self.records.append({
            "stage": stage,
            "step": step,
            "duration": max(0.0, float(duration)),
            "start_time": start_time if start_time is not None else time.time() - duration,
            "end_time": end_time if end_time is not None else time.time(),
            "status": status,
            "metadata": metadata or {},
        })

    @contextmanager
    def measure(self, stage: str, step: str, metadata: Optional[Dict[str, Any]] = None):
        """Context manager measuring execution duration of a block with time.perf_counter()."""
        start_perf = time.perf_counter()
        start_wall = time.time()
        active_rec = {
            "stage": stage,
            "step": step,
            "start_perf": start_perf,
            "start_time": start_wall,
            "metadata": metadata or {},
        }
        self._active_measurements.append(active_rec)
        status = "success"
        try:
            yield
        except Exception:
            status = "failed"
            raise
        finally:
            if active_rec in self._active_measurements:
                self._active_measurements.remove(active_rec)
            duration = time.perf_counter() - start_perf
            end_wall = time.time()
            self.record_step(
                stage=stage,
                step=step,
                duration=duration,
                start_time=start_wall,
                end_time=end_wall,
                status=status,
                metadata=metadata,
            )

    def get_all_records(self, include_active: bool = True) -> List[Dict[str, Any]]:
        """Returns all completed records, plus currently active measurements if requested."""
        records = list(self.records)
        if include_active and hasattr(self, "_active_measurements"):
            now_perf = time.perf_counter()
            for active in self._active_measurements:
                dur = now_perf - active["start_perf"]
                records.append({
                    "stage": active["stage"],
                    "step": active["step"],
                    "duration": dur,
                    "start_time": active["start_time"],
                    "end_time": time.time(),
                    "status": "running",
                    "metadata": active.get("metadata", {}),
                })
        return records

    def get_steps(self) -> List[Dict[str, Any]]:
        """Returns a copy of all recorded steps."""
        return list(self.records)

    def get_total_duration(self) -> float:
        """Returns the total elapsed duration across all recorded steps in seconds."""
        return sum(rec["duration"] for rec in self.records)

    def get_stage_durations(self) -> Dict[str, float]:
        """Returns a mapping of stage names to total elapsed seconds."""
        stage_totals: Dict[str, float] = {}
        for rec in self.records:
            stage = rec["stage"]
            stage_totals[stage] = stage_totals.get(stage, 0.0) + rec["duration"]
        return stage_totals

    @staticmethod
    def format_duration(seconds: float) -> str:
        """Formats seconds into human-readable duration string."""
        if seconds < 0:
            return "0.00s"
        if seconds < 1.0:
            return f"{seconds * 1000:.0f}ms"
        if seconds < 60.0:
            return f"{seconds:.2f}s"
        minutes = int(seconds // 60)
        rem_seconds = seconds % 60
        if minutes < 60:
            return f"{minutes}m {rem_seconds:.1f}s"
        hours = int(minutes // 60)
        rem_minutes = minutes % 60
        return f"{hours}h {rem_minutes:02d}m {rem_seconds:.0f}s"

    def format_summary(self, include_active: bool = True) -> str:
        """Renders an ASCII summary table of all recorded step durations."""
        all_records = self.get_all_records(include_active=include_active)
        if not all_records:
            return "No pipeline duration records captured."

        lines = [
            "+" + "-" * 78 + "+",
            f"| {'Pipeline Execution Duration Summary':<76} |",
            "+" + "-" * 20 + "+" + "-" * 34 + "+" + "-" * 11 + "+" + "-" * 9 + "+",
            f"| {'Pipeline Stage':<18} | {'Step / Sub-step':<32} | {'Duration':<9} | {'Status':<7} |",
            "+" + "-" * 20 + "+" + "-" * 34 + "+" + "-" * 11 + "+" + "-" * 9 + "+",
        ]

        for rec in all_records:
            dur_str = self.format_duration(rec["duration"])
            status_str = rec.get("status", "success").capitalize()
            stage_str = rec["stage"][:18]
            step_str = rec["step"][:32]
            lines.append(
                f"| {stage_str:<18} | {step_str:<32} | {dur_str:>9} | {status_str:<7} |"
            )

        total_str = self.format_duration(sum(rec["duration"] for rec in all_records))
        lines.append("+" + "-" * 20 + "+" + "-" * 34 + "+" + "-" * 11 + "+" + "-" * 9 + "+")
        lines.append(f"| {'Total Runtime':<18} | {'':<32} | {total_str:>9} | {'':<7} |")
        lines.append("+" + "-" * 78 + "+")

        return "\n".join(lines)

    def format_markdown_section(self, include_active: bool = True) -> str:
        """Returns a Markdown-formatted section ready to append to migration_report.md."""
        all_records = self.get_all_records(include_active=include_active)
        if not all_records:
            return ""

        return f"\n\n### Pipeline Execution Duration Summary\n\n```\n{self.format_summary(include_active=include_active)}\n```\n"

    def log_to_mlflow(self, run_id: Optional[str] = None):
        """Logs recorded step durations as metrics to active MLflow run."""
        if not self.records:
            return
        try:
            import mlflow

            metrics = {}
            for rec in self.records:
                stage_clean = re.sub(r"[^a-zA-Z0-9_]", "_", rec["stage"].lower()).strip("_")
                step_clean = re.sub(r"[^a-zA-Z0-9_]", "_", rec["step"].lower()).strip("_")
                metric_name = f"duration_{stage_clean}_{step_clean}_sec"
                metrics[metric_name] = rec["duration"]

            metrics["pipeline_total_duration_sec"] = self.get_total_duration()

            if run_id:
                with mlflow.start_run(run_id=run_id):
                    mlflow.log_metrics(metrics)
            else:
                active_run = mlflow.active_run()
                if active_run:
                    mlflow.log_metrics(metrics)
                    mlflow.end_run()
                else:
                    with mlflow.start_run():
                        mlflow.log_metrics(metrics)
        except Exception as e:
            logging.debug(f"MLflow duration metric logging skipped or failed: {e}")

    def to_dict(self) -> Dict[str, Any]:
        """Serializes records to dictionary."""
        return {
            "records": self.records,
            "total_duration": self.get_total_duration(),
        }

    def from_dict(self, data: Dict[str, Any]):
        """Populates records from dictionary."""
        self.records = list(data.get("records", []))

    def save_to_file(self, filepath: str):
        """Saves duration records to a JSON file."""
        if dirname := os.path.dirname(filepath):
            os.makedirs(dirname, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

    def load_from_file(self, filepath: str):
        """Loads duration records from a JSON file, replacing current state."""
        if not os.path.exists(filepath):
            return
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.from_dict(data)

    def load_and_merge(self, filepath: str):
        """Loads duration records from a JSON file and merges them into this instance."""
        if not os.path.exists(filepath):
            return
        try:
            other = DurationTracker()
            other.load_from_file(filepath)
            self.merge(other)
        except Exception as e:
            logging.debug(f"Failed to load and merge durations from {filepath}: {e}")

    def merge(self, other: "DurationTracker"):
        """Merges records from another DurationTracker instance into this one, deduplicating identical records."""
        existing_keys = {(r.get("stage"), r.get("step")) for r in self.records}
        for rec in other.records:
            key = (rec.get("stage"), rec.get("step"))
            if key not in existing_keys:
                self.records.append(rec)
                existing_keys.add(key)


def track_duration(stage: str, step: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None):
    """Decorator that wraps a function call in DurationTracker.get_instance().measure()."""
    def decorator(fn):
        step_name = step or fn.__name__

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with DurationTracker.get_instance().measure(stage=stage, step=step_name, metadata=metadata):
                return fn(*args, **kwargs)

        return wrapper

    return decorator
