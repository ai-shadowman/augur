import functools
import json
import logging
import os
import re
import tempfile
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

    def __init__(self, git_slug: Optional[str] = None, git_repo: Optional[str] = None):
        self.records: List[Dict[str, Any]] = []
        self._active_measurements: List[Dict[str, Any]] = []
        self.git_slug: Optional[str] = git_slug
        self.git_repo: Optional[str] = git_repo

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
        """Records a single step timing record. If (stage, step) already exists, updates it in-place."""
        dur = max(0.0, float(duration))
        s_time = start_time if start_time is not None else time.time() - dur
        e_time = end_time if end_time is not None else time.time()

        for rec in self.records:
            if rec.get("stage") == stage and rec.get("step") == step:
                rec["duration"] = dur
                rec["start_time"] = s_time
                rec["end_time"] = e_time
                rec["status"] = status
                if metadata:
                    rec.setdefault("metadata", {}).update(metadata)
                return

        self.records.append({
            "stage": stage,
            "step": step,
            "duration": dur,
            "start_time": s_time,
            "end_time": e_time,
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

    @staticmethod
    def _is_aggregate_step(rec: Dict[str, Any]) -> bool:
        """Determines if a step is a parent or summary aggregate to avoid double-counting in totals."""
        meta = rec.get("metadata") or {}
        if meta.get("is_aggregate") or meta.get("is_parent"):
            return True
        step_name = rec.get("step", "").strip().lower()
        if step_name in ["migration report total", "generate migration report"]:
            return True
        return False

    def get_stage_durations(self, include_active: bool = False) -> Dict[str, float]:
        """Returns a mapping of stage names to total elapsed seconds,
        avoiding double-counting parent/aggregate steps when sub-steps exist."""
        all_recs = self.get_all_records(include_active=include_active)
        stages: Dict[str, List[Dict[str, Any]]] = {}
        for rec in all_recs:
            stages.setdefault(rec["stage"], []).append(rec)

        stage_totals: Dict[str, float] = {}
        for stage, recs in stages.items():
            non_agg = [r for r in recs if not self._is_aggregate_step(r)]
            if non_agg:
                stage_totals[stage] = sum(r["duration"] for r in non_agg)
            else:
                stage_totals[stage] = sum(r["duration"] for r in recs)
        return stage_totals

    def get_total_duration(self) -> float:
        """Returns the total elapsed duration across all recorded stages in seconds,
        avoiding double-counting parent/aggregate steps."""
        return sum(self.get_stage_durations(include_active=False).values())

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
        """Renders an ASCII summary table of all recorded step durations with stage breakdown."""
        all_records = self.get_all_records(include_active=include_active)
        if not all_records:
            return "No pipeline duration records captured."

        stage_w = 18
        step_w = 40
        dur_w = 10
        status_w = 8

        col_sep = f"+{'-' * (stage_w + 2)}+{'-' * (step_w + 2)}+{'-' * (dur_w + 2)}+{'-' * (status_w + 2)}+"
        total_w = len(col_sep)
        border = f"+{'-' * (total_w - 2)}+"

        lines = [
            border,
            f"| {'Pipeline Execution Duration Summary':<{total_w - 4}} |",
            col_sep,
            f"| {'Pipeline Stage':<{stage_w}} | {'Step / Sub-step':<{step_w}} | {'Duration':<{dur_w}} | {'Status':<{status_w}} |",
            col_sep,
        ]

        stages_with_substeps = set()
        for rec in all_records:
            if not self._is_aggregate_step(rec):
                stages_with_substeps.add(rec["stage"])

        for rec in all_records:
            if rec["stage"] in stages_with_substeps and self._is_aggregate_step(rec):
                continue
            dur_str = self.format_duration(rec["duration"])
            status_str = rec.get("status", "success").capitalize()
            stage_str = rec["stage"][:stage_w]
            step_str = rec["step"][:step_w]
            lines.append(
                f"| {stage_str:<{stage_w}} | {step_str:<{step_w}} | {dur_str:>{dur_w}} | {status_str:<{status_w}} |"
            )

        stages = self.get_stage_durations(include_active=include_active)
        total_duration = sum(stages.values())
        total_str = self.format_duration(total_duration)

        # Stage breakdown if more than one stage exists
        if len(stages) > 1:
            lines.append(col_sep)
            lines.append(f"| {'Stage Breakdown:':<{total_w - 4}} |")
            for stage_name, stage_dur in stages.items():
                pct = (stage_dur / total_duration * 100.0) if total_duration > 0 else 0.0
                stage_line = f"  - {stage_name}: {self.format_duration(stage_dur)} ({pct:.1f}%)"
                lines.append(f"| {stage_line:<{total_w - 4}} |")

        lines.append(col_sep)
        lines.append(f"| {'Total Runtime':<{stage_w}} | {'':<{step_w}} | {total_str:>{dur_w}} | {'':<{status_w}} |")
        lines.append(border)

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
                metric_name = f"duration_{stage_clean}_{step_clean}_sec"[:250]
                metrics[metric_name] = rec["duration"]

            for stage, stage_dur in self.get_stage_durations().items():
                stage_clean = re.sub(r"[^a-zA-Z0-9_]", "_", stage.lower()).strip("_")
                metrics[f"duration_{stage_clean}_total_sec"[:250]] = stage_dur

            metrics["pipeline_total_duration_sec"] = self.get_total_duration()

            active_run = mlflow.active_run()
            if run_id:
                if active_run and active_run.info.run_id == run_id:
                    mlflow.log_metrics(metrics)
                else:
                    with mlflow.start_run(run_id=run_id, nested=bool(active_run)):
                        mlflow.log_metrics(metrics)
            else:
                if active_run:
                    mlflow.log_metrics(metrics)
                    mlflow.end_run()
                else:
                    with mlflow.start_run():
                        mlflow.log_metrics(metrics)
        except Exception as e:
            logging.debug(f"MLflow duration metric logging skipped or failed: {e}")

    def upload_to_mlflow(
        self,
        git_slug: Optional[str] = None,
        stage: Optional[str] = None,
        run_id: Optional[str] = None,
        multi_repo: bool = False,
    ):
        """Uploads duration metrics and durations.json artifact to MLflow."""
        if not self.records:
            return

        # 1. Log numerical metrics
        try:
            self.log_to_mlflow(run_id=run_id)
        except Exception as e:
            logging.debug(f"Failed to log duration metrics to MLflow: {e}")

        # 2. Upload durations.json artifact
        temp_dir = tempfile.mkdtemp()
        temp_file = os.path.join(temp_dir, "durations.json")
        try:
            self.save_to_file(temp_file)

            # Direct MLflow run upload if active run or run_id available
            try:
                import mlflow
                active_run = mlflow.active_run()
                target_run = run_id or (active_run.info.run_id if active_run else None) or os.environ.get("MLFLOW_RUN_ID")
                if target_run:
                    if active_run and active_run.info.run_id == target_run:
                        mlflow.log_artifact(temp_file, artifact_path="telemetry")
                    else:
                        with mlflow.start_run(run_id=target_run, nested=bool(active_run)):
                            mlflow.log_artifact(temp_file, artifact_path="telemetry")
            except Exception as e:
                logging.debug(f"Failed to log durations.json directly to MLflow run: {e}")

            # Catalog upload via DefaultAssetLoader for git_slug / tag search
            if git_slug or multi_repo:
                try:
                    from loaders.default_asset_loader import DefaultAssetLoader
                    artifact_path = DefaultAssetLoader.get_log_results_artifact_path(
                        DefaultAssetLoader.RESULTS_PATH_PREFIX_TELEMETRY,
                        git_slug=git_slug,
                        multi_repo=multi_repo,
                    )
                    tags = {
                        "git_slug": str(git_slug or "multi-repo"),
                        "category": "telemetry",
                        "type": "durations",
                        "multi_repo": str(multi_repo),
                    }
                    if stage:
                        tags["stage"] = str(stage)

                    content_str = None
                    try:
                        with open(temp_file, "r", encoding="utf-8") as f:
                            content_str = f.read()
                    except Exception:
                        pass

                    DefaultAssetLoader().log_results(
                        temp_file,
                        artifact_path=artifact_path,
                        tags=tags,
                        content=content_str,
                    )
                except Exception as e:
                    logging.debug(f"Failed to upload durations.json via DefaultAssetLoader: {e}")
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def download_from_mlflow(
        self,
        git_slug: Optional[str] = None,
        run_id: Optional[str] = None,
        multi_repo: bool = False,
        current_stage: Optional[str] = None,
    ) -> bool:
        """Downloads and merges duration records from MLflow.
        If current_stage is specified, runs and records belonging to current_stage are skipped.
        Returns True if records were retrieved and merged, False otherwise."""
        merged_any = False
        if not hasattr(self, "_merged_runs"):
            self._merged_runs = set()

        # 1. Try downloading via run_id or MLFLOW_RUN_ID
        target_run = run_id or os.environ.get("MLFLOW_RUN_ID")
        if target_run and target_run not in self._merged_runs:
            try:
                import mlflow
                local_path = mlflow.artifacts.download_artifacts(
                    run_id=target_run, artifact_path="telemetry/durations.json"
                )
                if local_path and os.path.exists(local_path):
                    self.load_and_merge(local_path, current_stage=current_stage)
                    self._merged_runs.add(target_run)
                    merged_any = True
            except Exception as e:
                logging.debug(f"Failed to download durations.json for run {target_run}: {e}")

        # 2. Try searching and aggregating across ALL telemetry runs in MLflow
        if git_slug or multi_repo:
            try:
                import mlflow
                from mlflow.tracking import MlflowClient
                from loaders.mlflow_asset_loader import MlFlowAssetLoader

                client = MlflowClient()
                experiment = MlFlowAssetLoader().get_or_create_experiment_by_name(
                    client, MlFlowAssetLoader.RESULT_ASSET_EXPERIMENT
                )

                filter_parts = ["tags.category = 'telemetry'", "tags.type = 'durations'"]
                if git_slug:
                    filter_parts.append(f"tags.git_slug = '{git_slug}'")
                elif multi_repo:
                    filter_parts.append("tags.git_slug = 'multi-repo'")
                filter_string = " AND ".join(filter_parts)

                runs = client.search_runs(
                    experiment_ids=[experiment.experiment_id],
                    filter_string=filter_string,
                    order_by=["attributes.start_time DESC"],
                )

                seen_stages = set()
                for run in runs:
                    if run.info.run_id in self._merged_runs:
                        continue
                    stage = run.data.tags.get("stage")
                    if current_stage and stage and stage.lower() == current_stage.lower():
                        continue
                    if current_stage and not stage:
                        continue
                    if stage:
                        if stage.lower() in seen_stages:
                            continue
                        seen_stages.add(stage.lower())
                    else:
                        if "untagged" in seen_stages:
                            continue
                        seen_stages.add("untagged")

                    candidate_subpaths = []
                    if git_slug:
                        candidate_subpaths.append(f"results/telemetry/{git_slug}/durations.json")
                    if multi_repo:
                        candidate_subpaths.append(f"results/telemetry/multi-repo/{git_slug or ''}/durations.json".replace("//", "/"))
                        candidate_subpaths.append("results/telemetry/multi-repo/durations.json")
                    candidate_subpaths.extend([
                        "results/telemetry/durations.json",
                        "telemetry/durations.json",
                        "durations.json",
                    ])

                    for subpath in candidate_subpaths:
                        try:
                            downloaded_path = mlflow.artifacts.download_artifacts(
                                run_id=run.info.run_id,
                                artifact_path=subpath,
                            )
                            if downloaded_path and os.path.exists(downloaded_path):
                                self.load_and_merge(downloaded_path, current_stage=current_stage)
                                self._merged_runs.add(run.info.run_id)
                                merged_any = True
                                break
                        except Exception:
                            continue

            except Exception as e:
                logging.debug(f"MLflow client multi-run search for durations.json failed: {e}")

            # Fallback to DefaultAssetLoader if not merged yet
            if not merged_any:
                try:
                    from loaders.default_asset_loader import DefaultAssetLoader
                    from loaders.mlflow_asset_loader import MlFlowAssetLoader

                    artifact_path = DefaultAssetLoader.get_log_results_artifact_path(
                        DefaultAssetLoader.RESULTS_PATH_PREFIX_TELEMETRY,
                        git_slug=git_slug,
                        multi_repo=multi_repo,
                    )
                    asset_file = f"{artifact_path}/durations.json"
                    temp_dir = tempfile.mkdtemp()
                    try:
                        content = DefaultAssetLoader().download(
                            asset_file,
                            download_dir=temp_dir,
                            experiment_name=MlFlowAssetLoader.RESULT_ASSET_EXPERIMENT,
                            asset_tags={"git_slug": str(git_slug or "multi-repo"), "type": "durations"},
                        )
                        if isinstance(content, dict) and "records" in content:
                            other = DurationTracker.from_dict(content)
                            self.merge(other, current_stage=current_stage)
                            merged_any = True
                        elif isinstance(content, str):
                            data = json.loads(content)
                            if isinstance(data, dict) and "records" in data:
                                other = DurationTracker.from_dict(data)
                                self.merge(other, current_stage=current_stage)
                                merged_any = True
                        downloaded_file = os.path.join(temp_dir, "durations.json")
                        if os.path.exists(downloaded_file):
                            self.load_and_merge(downloaded_file, current_stage=current_stage)
                            merged_any = True
                    finally:
                        import shutil
                        shutil.rmtree(temp_dir, ignore_errors=True)
                except Exception as e:
                    logging.debug(f"Failed to download durations.json via DefaultAssetLoader: {e}")

        return merged_any

    def to_dict(self) -> Dict[str, Any]:
        """Serializes records to dictionary."""
        d = {
            "records": self.records,
            "total_duration": self.get_total_duration(),
        }
        if self.git_slug:
            d["git_slug"] = self.git_slug
        if self.git_repo:
            d["git_repo"] = self.git_repo
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DurationTracker":
        """Reconstructs a DurationTracker from a dictionary."""
        tracker = cls(git_slug=data.get("git_slug"), git_repo=data.get("git_repo"))
        tracker.records = list(data.get("records", []))
        return tracker

    def load_from_dict(self, data: Dict[str, Any]):
        """Populates records from dictionary into this instance."""
        self.records = list(data.get("records", []))
        if "git_slug" in data and not self.git_slug:
            self.git_slug = data["git_slug"]
        if "git_repo" in data and not self.git_repo:
            self.git_repo = data["git_repo"]

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
        self.load_from_dict(data)

    def load_and_merge(self, filepath: str, current_stage: Optional[str] = None):
        """Loads duration records from a JSON file and merges them into this instance."""
        if not os.path.exists(filepath):
            return
        try:
            other = DurationTracker()
            other.load_from_file(filepath)
            self.merge(other, current_stage=current_stage)
        except Exception as e:
            logging.debug(f"Failed to load and merge durations from {filepath}: {e}")

    def merge(self, other: "DurationTracker", current_stage: Optional[str] = None):
        """Merges records from another DurationTracker instance into this one, deduplicating identical records.
        If current_stage is provided, records belonging to that stage are ignored so current measurements are not overwritten."""
        if not other:
            return
        existing_indices = {(r.get("stage"), r.get("step")): i for i, r in enumerate(self.records)}
        for rec in other.records:
            if current_stage and rec.get("stage") == current_stage:
                continue
            key = (rec.get("stage"), rec.get("step"))
            if key not in existing_indices:
                self.records.append(rec)
                existing_indices[key] = len(self.records) - 1
            else:
                idx = existing_indices[key]
                existing_rec = self.records[idx]
                if existing_rec.get("duration", 0.0) <= 0.0 or existing_rec.get("status") == "running":
                    self.records[idx] = rec


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


def find_telemetry_file(base_paths: List[str], filename: str) -> Optional[str]:
    """Searches given base paths and their subdirectories recursively for filename."""
    for base in base_paths:
        if not base or not os.path.exists(base):
            continue
        direct = os.path.join(base, filename)
        if os.path.isfile(direct):
            return direct
        try:
            for root, _dirs, files in os.walk(base):
                if filename in files:
                    found = os.path.join(root, filename)
                    if os.path.isfile(found):
                        return found
        except Exception:
            pass
    return None
