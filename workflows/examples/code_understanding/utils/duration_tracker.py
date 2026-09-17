import functools
import json
import logging
import os
import re
import tempfile
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional



def _is_mock(obj: Any) -> bool:
    """Helper to detect mock objects in unit tests to prevent serialization issues."""
    return obj is not None and (hasattr(obj, "_mock_name") or hasattr(obj, "_mock_return_value"))


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
        self.mlflow_run_id: Optional[str] = None
        self.mlflow_experiment_id: Optional[str] = None
        self.mlflow_tracking_uri: Optional[str] = None

    def reset(self):
        """Clears all recorded timing records."""
        self.records.clear()
        self._active_measurements.clear()
        self.mlflow_run_id = None
        self.mlflow_experiment_id = None
        self.mlflow_tracking_uri = None

    def capture_mlflow_context(self):
        """Captures MLflow tracking URI, run ID, and experiment ID from active run or environment."""
        try:
            import mlflow
            if not self.mlflow_tracking_uri:
                uri = mlflow.get_tracking_uri()
                if uri and not _is_mock(uri):
                    self.mlflow_tracking_uri = str(uri)
            active_run = mlflow.active_run()
            if active_run and hasattr(active_run, "info"):
                if not self.mlflow_run_id:
                    rid = getattr(active_run.info, "run_id", None)
                    if rid and not _is_mock(rid):
                        self.mlflow_run_id = str(rid)
                if not self.mlflow_experiment_id:
                    eid = getattr(active_run.info, "experiment_id", None)
                    if eid and not _is_mock(eid):
                        self.mlflow_experiment_id = str(eid)
        except Exception:
            pass
        if not self.mlflow_run_id:
            env_rid = os.environ.get("MLFLOW_RUN_ID")
            if env_rid:
                self.mlflow_run_id = env_rid
        if not self.mlflow_tracking_uri:
            env_uri = os.environ.get("MLFLOW_TRACKING_URI")
            if env_uri:
                self.mlflow_tracking_uri = env_uri

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

    def format_markdown_table(self, include_active: bool = False) -> str:
        """Renders a native GFM Markdown table with visual latency bars, bottleneck analysis, and MLflow deep links."""
        records = self.get_all_records(include_active=include_active)
        if not records:
            return ""

        self.capture_mlflow_context()

        total_duration = self.get_total_duration()
        stages = self.get_stage_durations(include_active=include_active)

        # Identify slowest non-aggregate step as bottleneck
        non_agg = [r for r in records if not self._is_aggregate_step(r)]
        bottleneck = max(non_agg, key=lambda r: r.get("duration", 0.0)) if non_agg else None

        lines = [
            "\n\n### Pipeline Execution Duration Summary\n",
        ]

        # Callout block with KPIs and MLflow links
        mlflow_links = []
        if self.mlflow_tracking_uri and self.mlflow_run_id:
            base_url = self.mlflow_tracking_uri.rstrip("/")
            exp_id = self.mlflow_experiment_id or "0"
            run_url = f"{base_url}/#/experiments/{exp_id}/runs/{self.mlflow_run_id}"
            mlflow_links.append(f"[MLflow Run `{self.mlflow_run_id[:8]}`]({run_url})")
            mlflow_links.append(f"[Artifacts]({run_url}/artifacts)")

        kpi_parts = [f"**Total Pipeline Runtime:** `{self.format_duration(total_duration)}`"]
        if bottleneck and total_duration > 0:
            b_pct = (bottleneck.get("duration", 0.0) / total_duration) * 100.0
            kpi_parts.append(
                f"**Slowest Step:** `{bottleneck.get('step')}` ({self.format_duration(bottleneck.get('duration', 0.0))} — {b_pct:.1f}%)"
            )
        if mlflow_links:
            kpi_parts.append(f"**MLflow Tracking:** {' • '.join(mlflow_links)}")

        lines.append("> " + " | ".join(kpi_parts) + "\n")

        # Table header
        lines.append("| Stage | Step / Sub-step | Duration | % Total | Latency Bar | Status |")
        lines.append("| :--- | :--- | :---: | :---: | :--- | :---: |")

        max_bar_width = 15
        stages_with_substeps = set()
        for rec in records:
            if not self._is_aggregate_step(rec):
                stages_with_substeps.add(rec["stage"])

        for rec in records:
            if rec["stage"] in stages_with_substeps and self._is_aggregate_step(rec):
                continue
            dur = rec.get("duration", 0.0)
            pct = (dur / total_duration * 100.0) if total_duration > 0 else 0.0
            bar_len = max(1, int(pct / 100.0 * max_bar_width)) if pct > 0 else 1
            bar = "█" * bar_len
            raw_status = rec.get("status", "success").lower()
            if raw_status == "success":
                status_icon = "✅"
            elif raw_status == "running":
                status_icon = "⏳"
            else:
                status_icon = "❌"

            lines.append(
                f"| **{rec['stage']}** | {rec['step']} | {self.format_duration(dur)} | {pct:.1f}% | `{bar}` | {status_icon} |"
            )

        total_str = self.format_duration(total_duration)
        lines.append(f"| **Total** | *All Stages* | **{total_str}** | **100%** | | |")

        # Collapsible stage breakdown
        if len(stages) > 1:
            lines.append("\n<details>")
            lines.append("<summary><b>📊 Stage Breakdown</b></summary>\n")
            for stage, s_dur in stages.items():
                pct = (s_dur / total_duration * 100.0) if total_duration > 0 else 0.0
                lines.append(f"- **{stage}:** `{self.format_duration(s_dur)}` ({pct:.1f}%)")
            lines.append("\n</details>\n")

        return "\n".join(lines)

    def format_markdown_section(self, include_active: bool = True, as_table: bool = False) -> str:
        """Returns a Markdown-formatted section ready to append to migration_report.md."""
        if as_table:
            return self.format_markdown_table(include_active=include_active)
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

            try:
                uri = mlflow.get_tracking_uri()
                if uri and not _is_mock(uri):
                    self.mlflow_tracking_uri = str(uri)
            except Exception:
                self.mlflow_tracking_uri = os.environ.get("MLFLOW_TRACKING_URI")

            active_run = mlflow.active_run()
            if run_id:
                if not _is_mock(run_id):
                    self.mlflow_run_id = str(run_id)
                if active_run and hasattr(active_run, "info") and active_run.info.run_id == run_id:
                    eid = getattr(active_run.info, "experiment_id", None)
                    if eid and not _is_mock(eid):
                        self.mlflow_experiment_id = str(eid)
                    mlflow.log_metrics(metrics)
                else:
                    with mlflow.start_run(run_id=run_id, nested=bool(active_run)) as r:
                        if hasattr(r, "info"):
                            eid = getattr(r.info, "experiment_id", None)
                            if eid and not _is_mock(eid):
                                self.mlflow_experiment_id = str(eid)
                        mlflow.log_metrics(metrics)
            else:
                if active_run:
                    if hasattr(active_run, "info"):
                        rid = getattr(active_run.info, "run_id", None)
                        if rid and not _is_mock(rid):
                            self.mlflow_run_id = str(rid)
                        eid = getattr(active_run.info, "experiment_id", None)
                        if eid and not _is_mock(eid):
                            self.mlflow_experiment_id = str(eid)
                    mlflow.log_metrics(metrics)
                    mlflow.end_run()
                else:
                    with mlflow.start_run() as r:
                        if hasattr(r, "info"):
                            rid = getattr(r.info, "run_id", None)
                            if rid and not _is_mock(rid):
                                self.mlflow_run_id = str(rid)
                            eid = getattr(r.info, "experiment_id", None)
                            if eid and not _is_mock(eid):
                                self.mlflow_experiment_id = str(eid)
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
                target_run = run_id or (active_run.info.run_id if active_run and hasattr(active_run, "info") else None) or os.environ.get("MLFLOW_RUN_ID")
                if target_run and not _is_mock(target_run):
                    self.mlflow_run_id = str(target_run)
                    if active_run and hasattr(active_run, "info"):
                        eid = getattr(active_run.info, "experiment_id", None)
                        if eid and not _is_mock(eid):
                            self.mlflow_experiment_id = self.mlflow_experiment_id or str(eid)
                    run_tags = {
                        "category": "telemetry",
                        "type": "durations",
                        "git_slug": str(git_slug or "multi-repo"),
                    }
                    if stage:
                        run_tags["stage"] = str(stage)
                    if active_run and active_run.info.run_id == target_run:
                        mlflow.set_tags(run_tags)
                        mlflow.log_artifact(temp_file, artifact_path="telemetry")
                    else:
                        with mlflow.start_run(run_id=target_run, nested=bool(active_run)):
                            mlflow.set_tags(run_tags)
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
        If current_stage is specified, runs and records belonging to current_stage are skipped,
        and only upstream stages (e.g. Data Generation, Indexing for Analysis) are accepted.
        Returns True if records were retrieved and merged, False otherwise."""
        merged_any = False
        if not hasattr(self, "_merged_runs"):
            self._merged_runs = set()

        # 1. Try downloading via run_id or MLFLOW_RUN_ID
        target_run = run_id or os.environ.get("MLFLOW_RUN_ID")
        if target_run and target_run not in self._merged_runs:
            if not self.mlflow_run_id:
                self.mlflow_run_id = target_run
            try:
                import mlflow
                local_path = mlflow.artifacts.download_artifacts(
                    run_id=target_run, artifact_path="telemetry/durations.json"
                )
                if local_path:
                    target_file = None
                    if os.path.isfile(local_path):
                        target_file = local_path
                    elif os.path.isdir(local_path):
                        cand = os.path.join(local_path, "durations.json")
                        if os.path.isfile(cand):
                            target_file = cand
                    if target_file and os.path.exists(target_file):
                        self.load_and_merge(target_file, current_stage=current_stage)
                        self._merged_runs.add(target_run)
                        merged_any = True
            except Exception as e:
                logging.debug(f"Failed to download durations.json for run {target_run}: {e}")

        # 2. Try searching and aggregating across ALL telemetry runs in MLflow
        seen_stages = set()
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

                try:
                    runs = client.search_runs(
                        experiment_ids=[experiment.experiment_id],
                        filter_string=filter_string,
                        order_by=["attributes.start_time DESC"],
                    )
                except Exception:
                    # Fallback to quoted tag keys in filter
                    quoted_parts = [
                        f'tags."{p.split(" = ")[0].split(".", 1)[1]}" = {p.split(" = ")[1]}'
                        for p in filter_parts
                    ]
                    runs = client.search_runs(
                        experiment_ids=[experiment.experiment_id],
                        filter_string=" AND ".join(quoted_parts),
                        order_by=["attributes.start_time DESC"],
                    )

                allowed_stages = None
                if current_stage and current_stage.lower() == "analysis":
                    allowed_stages = {"data generation", "indexing"}
                elif current_stage and current_stage.lower() == "indexing":
                    allowed_stages = {"data generation"}

                for run in runs:
                    if run.info.run_id in self._merged_runs:
                        continue
                    stage = run.data.tags.get("stage")
                    if current_stage and stage and stage.lower() == current_stage.lower():
                        continue
                    if allowed_stages is not None:
                        if not stage or stage.lower() not in allowed_stages:
                            continue
                    elif current_stage and not stage:
                        continue

                    if stage:
                        if stage.lower() in seen_stages:
                            continue
                    else:
                        if "untagged" in seen_stages:
                            continue

                    candidate_subpaths = []
                    if git_slug:
                        candidate_subpaths.append(f"results/telemetry/{git_slug}/durations.json")
                        candidate_subpaths.append(f"results/telemetry/{git_slug}")
                    if multi_repo:
                        candidate_subpaths.append(f"results/telemetry/multi-repo/{git_slug or ''}/durations.json".replace("//", "/"))
                        candidate_subpaths.append("results/telemetry/multi-repo/durations.json")
                    candidate_subpaths.extend([
                        "results/telemetry/durations.json",
                        "telemetry/durations.json",
                        "durations.json",
                    ])

                    run_merged = False
                    for subpath in candidate_subpaths:
                        try:
                            downloaded_path = mlflow.artifacts.download_artifacts(
                                run_id=run.info.run_id,
                                artifact_path=subpath,
                            )
                            if downloaded_path:
                                target_file = None
                                if os.path.isfile(downloaded_path):
                                    target_file = downloaded_path
                                elif os.path.isdir(downloaded_path):
                                    cand = os.path.join(downloaded_path, "durations.json")
                                    if os.path.isfile(cand):
                                        target_file = cand
                                if target_file and os.path.exists(target_file):
                                    self.load_and_merge(target_file, current_stage=current_stage)
                                    self._merged_runs.add(run.info.run_id)
                                    if not self.mlflow_run_id:
                                        self.mlflow_run_id = run.info.run_id
                                        self.mlflow_experiment_id = run.info.experiment_id
                                    merged_any = True
                                    run_merged = True
                                    if stage:
                                        seen_stages.add(stage.lower())
                                    else:
                                        seen_stages.add("untagged")
                                    break
                        except Exception:
                            continue
                    if run_merged and allowed_stages and seen_stages.issuperset(allowed_stages):
                        break

            except Exception as e:
                logging.debug(f"MLflow client multi-run search for durations.json failed: {e}")

            # Fallback to DefaultAssetLoader for missing upstream stages
            upstream_targets = []
            if current_stage and current_stage.lower() == "analysis":
                upstream_targets = ["Data Generation", "Indexing"]
            elif current_stage and current_stage.lower() == "indexing":
                upstream_targets = ["Data Generation"]
            else:
                upstream_targets = [None]

            for target_stage in upstream_targets:
                if target_stage and target_stage.lower() in seen_stages:
                    continue
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
                        asset_tags = {"git_slug": str(git_slug or "multi-repo"), "type": "durations"}
                        if target_stage:
                            asset_tags["stage"] = target_stage
                        content = DefaultAssetLoader().download(
                            asset_file,
                            download_dir=temp_dir,
                            experiment_name=MlFlowAssetLoader.RESULT_ASSET_EXPERIMENT,
                            asset_tags=asset_tags,
                        )
                        loaded_from_content = False
                        if isinstance(content, dict) and "records" in content:
                            other = DurationTracker.from_dict(content)
                            self.merge(other, current_stage=current_stage)
                            merged_any = True
                            loaded_from_content = True
                        elif isinstance(content, str):
                            data = json.loads(content)
                            if isinstance(data, dict) and "records" in data:
                                other = DurationTracker.from_dict(data)
                                self.merge(other, current_stage=current_stage)
                                merged_any = True
                                loaded_from_content = True

                        if not loaded_from_content:
                            downloaded_file = os.path.join(temp_dir, "durations.json")
                            if os.path.exists(downloaded_file):
                                self.load_and_merge(downloaded_file, current_stage=current_stage)
                                merged_any = True
                        if target_stage:
                            seen_stages.add(target_stage.lower())
                    finally:
                        import shutil
                        shutil.rmtree(temp_dir, ignore_errors=True)
                except Exception as e:
                    logging.debug(f"Failed to download durations.json for {target_stage} via DefaultAssetLoader: {e}")

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
        if self.mlflow_run_id and not _is_mock(self.mlflow_run_id):
            d["mlflow_run_id"] = str(self.mlflow_run_id)
        if self.mlflow_experiment_id and not _is_mock(self.mlflow_experiment_id):
            d["mlflow_experiment_id"] = str(self.mlflow_experiment_id)
        if self.mlflow_tracking_uri and not _is_mock(self.mlflow_tracking_uri):
            d["mlflow_tracking_uri"] = str(self.mlflow_tracking_uri)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DurationTracker":
        """Reconstructs a DurationTracker from a dictionary."""
        tracker = cls(git_slug=data.get("git_slug"), git_repo=data.get("git_repo"))
        tracker.records = list(data.get("records", []))
        if data.get("mlflow_run_id") and not _is_mock(data["mlflow_run_id"]):
            tracker.mlflow_run_id = str(data["mlflow_run_id"])
        if data.get("mlflow_experiment_id") and not _is_mock(data["mlflow_experiment_id"]):
            tracker.mlflow_experiment_id = str(data["mlflow_experiment_id"])
        if data.get("mlflow_tracking_uri") and not _is_mock(data["mlflow_tracking_uri"]):
            tracker.mlflow_tracking_uri = str(data["mlflow_tracking_uri"])
        return tracker

    def load_from_dict(self, data: Dict[str, Any], current_stage: Optional[str] = None):
        """Populates records from dictionary into this instance.
        If current_stage is specified, records belonging to that stage are excluded."""
        recs = data.get("records", [])
        if current_stage:
            stage_low = current_stage.lower()
            self.records = [r for r in recs if r.get("stage", "").lower() != stage_low]
        else:
            self.records = list(recs)
        if "git_slug" in data and not self.git_slug:
            self.git_slug = data["git_slug"]
        if "git_repo" in data and not self.git_repo:
            self.git_repo = data["git_repo"]
        if "mlflow_run_id" in data and not self.mlflow_run_id and not _is_mock(data["mlflow_run_id"]):
            self.mlflow_run_id = str(data["mlflow_run_id"])
        if "mlflow_experiment_id" in data and not self.mlflow_experiment_id and not _is_mock(data["mlflow_experiment_id"]):
            self.mlflow_experiment_id = str(data["mlflow_experiment_id"])
        if "mlflow_tracking_uri" in data and not self.mlflow_tracking_uri and not _is_mock(data["mlflow_tracking_uri"]):
            self.mlflow_tracking_uri = str(data["mlflow_tracking_uri"])

    def save_to_file(self, filepath: str):
        """Saves duration records to a JSON file."""
        if dirname := os.path.dirname(filepath):
            os.makedirs(dirname, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

    def load_from_file(self, filepath: str, current_stage: Optional[str] = None):
        """Loads duration records from a JSON file, replacing current state."""
        if not os.path.exists(filepath):
            return
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.load_from_dict(data, current_stage=current_stage)

    def load_and_merge(self, filepath: str, current_stage: Optional[str] = None):
        """Loads duration records from a JSON file and merges them into this instance."""
        if not os.path.exists(filepath):
            return
        if not hasattr(self, "_merged_files"):
            self._merged_files = set()
        abs_p = os.path.abspath(filepath)
        if abs_p in self._merged_files:
            return
        try:
            other = DurationTracker()
            other.load_from_file(filepath, current_stage=current_stage)
            self.merge(other, current_stage=current_stage)
            self._merged_files.add(abs_p)
        except Exception as e:
            logging.debug(f"Failed to load and merge durations from {filepath}: {e}")

    def merge(self, other: "DurationTracker", current_stage: Optional[str] = None):
        """Merges records from another DurationTracker instance into this one, deduplicating identical records.
        If current_stage is provided, records belonging to that stage are ignored so current measurements are not overwritten."""
        if not other:
            return
        if not self.mlflow_run_id and other.mlflow_run_id and not _is_mock(other.mlflow_run_id):
            self.mlflow_run_id = str(other.mlflow_run_id)
        if not self.mlflow_experiment_id and other.mlflow_experiment_id and not _is_mock(other.mlflow_experiment_id):
            self.mlflow_experiment_id = str(other.mlflow_experiment_id)
        if not self.mlflow_tracking_uri and other.mlflow_tracking_uri and not _is_mock(other.mlflow_tracking_uri):
            self.mlflow_tracking_uri = str(other.mlflow_tracking_uri)
        existing_indices = {(r.get("stage"), r.get("step")): i for i, r in enumerate(self.records)}
        stage_low = current_stage.lower() if current_stage else None
        for rec in other.records:
            if stage_low and rec.get("stage", "").lower() == stage_low:
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
