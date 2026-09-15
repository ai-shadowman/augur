"""Pipeline Execution Metrics and Duration Tracker.

Provides tracking for pipeline start/stop times, stage durations, granular
step durations, per-app breakdown for multi-repo runs, and formatted summary
tables in ASCII and Markdown.
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timezone
from contextlib import contextmanager
from typing import Optional, Dict, Any, List, Union


def format_duration(seconds: float) -> str:
    """Formats a duration in seconds into a human-readable string."""
    if seconds is None or seconds < 0:
        return "0.00s"
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes = int(seconds // 60)
    remaining_sec = seconds % 60
    if minutes < 60:
        return f"{minutes}m {remaining_sec:.1f}s"
    hours = int(minutes // 60)
    remaining_min = minutes % 60
    return f"{hours}h {remaining_min:02d}m {int(remaining_sec):02d}s"


def _current_timestamp() -> str:
    """Returns the current local ISO timestamp with timezone offset."""
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _current_time_short() -> str:
    """Returns short time string for table displays (HH:MM:SS)."""
    return datetime.now().strftime("%H:%M:%S")


def _duration_between_timestamps(start_ts: Optional[str], stop_ts: Optional[str]) -> float:
    """Calculates duration in seconds between two ISO/formatted timestamps."""
    if not start_ts or not stop_ts:
        return 0.0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            t1 = datetime.strptime(start_ts[:19], fmt[:17] if len(fmt) < 19 else "%Y-%m-%d %H:%M:%S" if " " in start_ts else "%Y-%m-%dT%H:%M:%S")
            t2 = datetime.strptime(stop_ts[:19], fmt[:17] if len(fmt) < 19 else "%Y-%m-%d %H:%M:%S" if " " in stop_ts else "%Y-%m-%dT%H:%M:%S")
            diff = (t2 - t1).total_seconds()
            return max(0.0, diff)
        except Exception:
            continue
    return 0.0


class StepMetric:
    """Represents metrics for a single step execution within a stage or app."""

    def __init__(
        self,
        name: str,
        stage: Optional[str] = None,
        app: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.name = name
        self.stage = stage
        self.app = app
        self.started_at: str = _current_timestamp()
        self.stopped_at: Optional[str] = None
        self._start_perf: float = time.perf_counter()
        self.duration: float = 0.0
        self.status: str = "running"
        self.details: Dict[str, Any] = details or {}
        self.error_message: str = ""

    def complete(self, status: str = "COMPLETED", error_message: str = "") -> float:
        """Stops the timer and marks step status."""
        self.stopped_at = _current_timestamp()
        self.duration = max(0.0, time.perf_counter() - self._start_perf)
        self.status = status
        if error_message:
            self.error_message = error_message
        return self.duration

    def get_duration(self, finalize_running: bool = False) -> float:
        """Returns step duration, computing elapsed time if currently running."""
        if self.status != "running":
            if self.duration > 0.0:
                return self.duration
            if self.started_at and self.stopped_at:
                return _duration_between_timestamps(self.started_at, self.stopped_at)
            return self.duration

        # Step is running:
        if finalize_running:
            if hasattr(self, "_start_perf") and self._start_perf:
                return max(0.0, time.perf_counter() - self._start_perf)
            return _duration_between_timestamps(self.started_at, self.stopped_at or _current_timestamp())

        return self.duration

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "stage": self.stage,
            "app": self.app,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "duration": round(self.duration, 4),
            "duration_formatted": format_duration(self.duration),
            "status": self.status,
            "error_message": self.error_message,
            "details": self.details,
        }


class AppMetric:
    """Represents metrics for an individual repository/app in multi-repo workflows."""

    def __init__(
        self,
        app_name: str,
        git_repo: str = "",
        git_branch: str = "main",
    ):
        self.app_name = app_name
        self.git_repo = git_repo
        self.git_branch = git_branch
        self.started_at: str = _current_timestamp()
        self.stopped_at: Optional[str] = None
        self._start_perf: float = time.perf_counter()
        self.duration: float = 0.0
        self.status: str = "running"
        self.steps: List[StepMetric] = []
        self.error_message: str = ""

    def add_step(self, step: StepMetric):
        self.steps.append(step)

    def complete(self, status: str = "COMPLETED", error_message: str = "") -> float:
        self.stopped_at = _current_timestamp()
        self.duration = max(0.0, time.perf_counter() - self._start_perf)
        self.status = status
        if error_message:
            self.error_message = error_message
        return self.duration

    def get_duration(self, finalize_running: bool = False) -> float:
        """Returns app duration, computing elapsed time or step sum if active."""
        step_sum = sum(s.get_duration(finalize_running) for s in self.steps)
        if self.status != "running":
            if self.duration > 0.0:
                return max(self.duration, step_sum)
            if step_sum > 0.0:
                return step_sum
            if self.started_at and self.stopped_at:
                return _duration_between_timestamps(self.started_at, self.stopped_at)
            return self.duration

        # App is running:
        if finalize_running:
            if hasattr(self, "_start_perf") and self._start_perf:
                elapsed = max(0.0, time.perf_counter() - self._start_perf)
                return max(elapsed, step_sum)
            return max(step_sum, _duration_between_timestamps(self.started_at, self.stopped_at or _current_timestamp()))

        return max(self.duration, step_sum)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "app_name": self.app_name,
            "git_repo": self.git_repo,
            "git_branch": self.git_branch,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "duration": round(self.duration, 4),
            "duration_formatted": format_duration(self.duration),
            "status": self.status,
            "error_message": self.error_message,
            "steps": [s.to_dict() for s in self.steps],
        }


class StageMetric:
    """Represents metrics for a pipeline stage (e.g. Data Generation, Indexing, Analysis)."""

    def __init__(self, name: str):
        self.name = name
        self.started_at: str = _current_timestamp()
        self.stopped_at: Optional[str] = None
        self._start_perf: float = time.perf_counter()
        self.duration: float = 0.0
        self.status: str = "running"
        self.steps: List[StepMetric] = []
        self.apps: Dict[str, AppMetric] = {}
        self.error_message: str = ""

    def complete(self, status: str = "COMPLETED", error_message: str = "") -> float:
        self.stopped_at = _current_timestamp()
        self.duration = max(0.0, time.perf_counter() - self._start_perf)
        self.status = status
        if error_message:
            self.error_message = error_message
        return self.duration

    def get_duration(self, finalize_running: bool = False) -> float:
        """Returns stage duration, computing elapsed time or children sum if active."""
        sub_sum = sum(s.get_duration(finalize_running) for s in self.steps)
        if self.apps:
            sub_sum = max(sub_sum, sum(a.get_duration(finalize_running) for a in self.apps.values()))

        if self.status != "running":
            if self.duration > 0.0:
                return max(self.duration, sub_sum)
            if sub_sum > 0.0:
                return sub_sum
            if self.started_at and self.stopped_at:
                return _duration_between_timestamps(self.started_at, self.stopped_at)
            return self.duration

        # Stage is running:
        if finalize_running:
            if hasattr(self, "_start_perf") and self._start_perf:
                elapsed = max(0.0, time.perf_counter() - self._start_perf)
                return max(elapsed, sub_sum)
            return max(sub_sum, _duration_between_timestamps(self.started_at, self.stopped_at or _current_timestamp()))

        return max(self.duration, sub_sum)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "duration": round(self.duration, 4),
            "duration_formatted": format_duration(self.duration),
            "status": self.status,
            "error_message": self.error_message,
            "steps": [s.to_dict() for s in self.steps],
            "apps": {k: a.to_dict() for k, a in self.apps.items()},
        }


class PipelineMetricsTracker:
    """Central metrics collector and reporting engine for code understanding pipelines."""

    def __init__(self, pipeline_name: str = "pipeline", multi_repo: bool = False):
        self.pipeline_name = pipeline_name
        self.multi_repo = multi_repo
        self.started_at: str = _current_timestamp()
        self.stopped_at: Optional[str] = None
        self._start_perf: float = time.perf_counter()
        self.total_duration: float = 0.0
        self.status: str = "running"
        self.error_message: str = ""

        # Hierarchical tracking
        self.stages: Dict[str, StageMetric] = {}
        self.apps: Dict[str, AppMetric] = {}
        self.steps: List[StepMetric] = []

        # Internal active context
        self._active_stage: Optional[str] = None
        self._active_app: Optional[str] = None
        self._active_steps: Dict[str, StepMetric] = {}

    # ------------------------------------------------------------------------
    # Pipeline lifecycle
    # ------------------------------------------------------------------------

    def get_total_duration(self, finalize_running: bool = False) -> float:
        """Returns the total pipeline duration, summing stages or computing elapsed time."""
        stage_sum = sum(s.get_duration(finalize_running) for s in self.stages.values())
        step_sum = sum(s.get_duration(finalize_running) for s in self.steps)
        sub_sum = max(stage_sum, step_sum)

        if self.status != "running":
            if self.total_duration > 0.0:
                return max(self.total_duration, sub_sum)
            if sub_sum > 0.0:
                return sub_sum
            if self.started_at and self.stopped_at:
                return _duration_between_timestamps(self.started_at, self.stopped_at)
            return self.total_duration

        if finalize_running:
            perf_elapsed = 0.0
            if hasattr(self, "_start_perf") and self._start_perf:
                perf_elapsed = max(0.0, time.perf_counter() - self._start_perf)
            ts_elapsed = _duration_between_timestamps(self.started_at, self.stopped_at or _current_timestamp())
            return max(perf_elapsed, sub_sum, ts_elapsed)

        return max(self.total_duration, sub_sum)

    def start_pipeline(self, pipeline_name: Optional[str] = None, multi_repo: Optional[bool] = None):
        """Starts or resets pipeline timing."""
        if pipeline_name:
            self.pipeline_name = pipeline_name
        if multi_repo is not None:
            self.multi_repo = multi_repo
        self.started_at = _current_timestamp()
        self._start_perf = time.perf_counter()
        self.stopped_at = None
        self.total_duration = 0.0
        self.status = "running"
        self.error_message = ""
        logging.info(f"=== Started pipeline '{self.pipeline_name}' at {self.started_at} (multi_repo={self.multi_repo}) ===")

    def stop_pipeline(self, status: str = "COMPLETED", error_message: str = "") -> float:
        """Stops the pipeline timer and records total duration."""
        self.stopped_at = _current_timestamp()
        perf_elapsed = max(0.0, time.perf_counter() - self._start_perf)
        stage_sum = sum(s.get_duration() for s in self.stages.values())
        step_sum = sum(s.get_duration() for s in self.steps)
        ts_diff = _duration_between_timestamps(self.started_at, self.stopped_at)
        self.total_duration = max(perf_elapsed, stage_sum, step_sum, ts_diff)
        self.status = status
        if error_message:
            self.error_message = error_message
        logging.info(
            f"=== Finished pipeline '{self.pipeline_name}' at {self.stopped_at} "
            f"[{status}] - Total Duration: {format_duration(self.total_duration)} ==="
        )
        return self.total_duration

    @contextmanager
    def track_pipeline(self, pipeline_name: Optional[str] = None, multi_repo: Optional[bool] = None):
        """Context manager to track an entire pipeline execution."""
        self.start_pipeline(pipeline_name, multi_repo)
        try:
            yield self
            if self.status == "running":
                self.stop_pipeline(status="COMPLETED")
        except Exception as e:
            self.stop_pipeline(status="FAILED", error_message=str(e))
            raise

    # ------------------------------------------------------------------------
    # Stage lifecycle
    # ------------------------------------------------------------------------

    def start_stage(self, stage_name: str) -> StageMetric:
        """Starts tracking a pipeline stage."""
        if stage_name in self.stages:
            stage = self.stages[stage_name]
            stage.status = "running"
            stage._start_perf = time.perf_counter()
        else:
            stage = StageMetric(stage_name)
            self.stages[stage_name] = stage
        self._active_stage = stage_name
        logging.info(f"-- [STAGE START] {stage_name} at {stage.started_at} --")
        return stage

    def stop_stage(self, stage_name: str, status: str = "COMPLETED", error_message: str = "") -> float:
        """Stops tracking a pipeline stage."""
        stage = self.stages.get(stage_name)
        if not stage:
            stage = StageMetric(stage_name)
            self.stages[stage_name] = stage
        stage.stopped_at = _current_timestamp()
        perf_elapsed = max(0.0, time.perf_counter() - getattr(stage, "_start_perf", time.perf_counter()))
        sub_sum = sum(s.get_duration() for s in stage.steps)
        if stage.apps:
            sub_sum = max(sub_sum, sum(a.get_duration() for a in stage.apps.values()))
        ts_diff = _duration_between_timestamps(stage.started_at, stage.stopped_at)
        stage.duration = max(stage.duration, perf_elapsed, sub_sum, ts_diff)
        stage.status = status
        if error_message:
            stage.error_message = error_message
        if self._active_stage == stage_name:
            self._active_stage = None
        logging.info(f"-- [STAGE END] {stage_name} [{status}] - Duration: {format_duration(stage.duration)} --")
        return stage.duration

    @contextmanager
    def track_stage(self, stage_name: str):
        """Context manager to track a pipeline stage."""
        self.start_stage(stage_name)
        try:
            yield self.stages[stage_name]
            stage = self.stages[stage_name]
            if stage.status == "running":
                self.stop_stage(stage_name, status="COMPLETED")
        except Exception as e:
            self.stop_stage(stage_name, status="FAILED", error_message=str(e))
            raise

    # ------------------------------------------------------------------------
    # App lifecycle (Multi-Repo)
    # ------------------------------------------------------------------------

    def start_app(self, app_name: str, git_repo: str = "", git_branch: str = "main") -> AppMetric:
        """Starts tracking an individual repository/app in multi-repo workflows."""
        if app_name in self.apps:
            app = self.apps[app_name]
            app.status = "running"
            app._start_perf = time.perf_counter()
        else:
            app = AppMetric(app_name=app_name, git_repo=git_repo, git_branch=git_branch)
            self.apps[app_name] = app
        if self._active_stage and self._active_stage in self.stages:
            self.stages[self._active_stage].apps[app_name] = app
        self._active_app = app_name
        logging.info(f"  -> [APP START] {app_name} (branch={git_branch}) at {app.started_at}")
        return app

    def stop_app(self, app_name: str, status: str = "COMPLETED", error_message: str = "") -> float:
        """Stops tracking an individual repository/app."""
        app = self.apps.get(app_name)
        if not app:
            app = AppMetric(app_name=app_name)
            self.apps[app_name] = app
        app.stopped_at = _current_timestamp()
        perf_elapsed = max(0.0, time.perf_counter() - getattr(app, "_start_perf", time.perf_counter()))
        step_sum = sum(s.get_duration() for s in app.steps)
        ts_diff = _duration_between_timestamps(app.started_at, app.stopped_at)
        app.duration = max(app.duration, perf_elapsed, step_sum, ts_diff)
        app.status = status
        if error_message:
            app.error_message = error_message
        if self._active_app == app_name:
            self._active_app = None
        logging.info(f"  -> [APP END] {app_name} [{status}] - Duration: {format_duration(app.duration)}")
        return app.duration
        logging.info(f"  -> [APP END] {app_name} [{status}] - Duration: {format_duration(duration)}")
        return duration

    @contextmanager
    def track_app(self, app_name: str, git_repo: str = "", git_branch: str = "main"):
        """Context manager to track an individual app execution."""
        self.start_app(app_name=app_name, git_repo=git_repo, git_branch=git_branch)
        try:
            yield self.apps[app_name]
            app = self.apps[app_name]
            if app.status == "running":
                self.stop_app(app_name, status="COMPLETED")
        except Exception as e:
            self.stop_app(app_name, status="FAILED", error_message=str(e))
            raise

    # ------------------------------------------------------------------------
    # Step lifecycle
    # ------------------------------------------------------------------------

    def start_step(
        self,
        step_name: str,
        stage: Optional[str] = None,
        app: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> StepMetric:
        """Starts tracking an individual step."""
        stage_name = stage or self._active_stage
        app_name = app or self._active_app
        step = StepMetric(name=step_name, stage=stage_name, app=app_name, details=details)
        self.steps.append(step)

        if stage_name and stage_name in self.stages:
            self.stages[stage_name].steps.append(step)
        if app_name and app_name in self.apps:
            self.apps[app_name].add_step(step)

        key = f"{stage_name}::{app_name}::{step_name}"
        self._active_steps[key] = step
        logging.debug(f"    * [STEP START] {step_name} at {step.started_at}")
        return step

    def stop_step(
        self,
        step_name: str,
        stage: Optional[str] = None,
        app: Optional[str] = None,
        status: str = "COMPLETED",
        error_message: str = "",
    ) -> float:
        """Stops tracking an individual step."""
        stage_name = stage or self._active_stage
        app_name = app or self._active_app
        key = f"{stage_name}::{app_name}::{step_name}"
        step = self._active_steps.pop(key, None)
        if not step:
            # Match by name in recent steps
            for s in reversed(self.steps):
                if s.name == step_name and s.status == "running":
                    step = s
                    break
        if not step:
            step = StepMetric(name=step_name, stage=stage_name, app=app_name)
            self.steps.append(step)

        duration = step.complete(status=status, error_message=error_message)
        logging.debug(f"    * [STEP END] {step_name} [{status}] - Duration: {format_duration(duration)}")
        return duration

    @contextmanager
    def track_step(
        self,
        step_name: str,
        stage: Optional[str] = None,
        app: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        """Context manager to track a granular step execution."""
        step = self.start_step(step_name=step_name, stage=stage, app=app, details=details)
        try:
            yield step
            if step.status == "running":
                self.stop_step(step_name=step_name, stage=stage, app=app, status="COMPLETED")
        except Exception as e:
            self.stop_step(step_name=step_name, stage=stage, app=app, status="FAILED", error_message=str(e))
            raise

    # ------------------------------------------------------------------------
    # Table Formatting (ASCII)
    # ------------------------------------------------------------------------

    def format_summary_table(self, border_width: int = 104, finalize_running: bool = True) -> str:
        """Formats the entire pipeline execution metrics into an ASCII summary table."""
        lines = []

        status_str = "COMPLETED" if (self.status == "running" and finalize_running) else self.status
        stop_str = (self.stopped_at or _current_timestamp()) if finalize_running else (self.stopped_at or "In Progress")
        total_dur = self.get_total_duration(finalize_running=finalize_running)

        lines.append("=" * border_width)
        lines.append(f"{'PIPELINE EXECUTION METRICS':^{border_width}}")
        lines.append("=" * border_width)
        lines.append(f"Pipeline:       {self.pipeline_name}")
        lines.append(f"Status:         {status_str}")
        lines.append(f"Started At:     {self.started_at}")
        lines.append(f"Stopped At:     {stop_str}")
        lines.append(f"Total Duration: {format_duration(total_dur)} ({total_dur:.2f}s)")
        lines.append("-" * border_width)

        col_w = {"name": 44, "start": 20, "stop": 20, "dur": 12, "status": 8}
        header_row = (
            f"{'Stage / App / Step':<{col_w['name']}}"
            f"{'Started At':<{col_w['start']}}"
            f"{'Stopped At':<{col_w['stop']}}"
            f"{'Duration':<{col_w['dur']}}"
            f"{'Status':<{col_w['status']}}"
        )
        lines.append(header_row)
        lines.append("-" * border_width)

        # Print hierarchical breakdown
        if self.stages:
            for stage_name, stage in self.stages.items():
                s_start = stage.started_at[11:19] if len(stage.started_at) >= 19 else stage.started_at
                s_stop = stage.stopped_at[11:19] if stage.stopped_at and len(stage.stopped_at) >= 19 else (
                    _current_timestamp()[11:19] if finalize_running else "-"
                )
                s_status = "COMPLETED" if (stage.status == "running" and finalize_running) else stage.status
                stage_dur = stage.get_duration(finalize_running=finalize_running)
                stage_row = (
                    f"{f'[STAGE] {stage.name}':<{col_w['name']}}"
                    f"{s_start:<{col_w['start']}}"
                    f"{s_stop:<{col_w['stop']}}"
                    f"{format_duration(stage_dur):<{col_w['dur']}}"
                    f"{s_status:<{col_w['status']}}"
                )
                lines.append(stage_row)

                # Apps within stage (e.g. Data Generation)
                if stage.apps:
                    for app_name, app in stage.apps.items():
                        a_start = app.started_at[11:19] if len(app.started_at) >= 19 else app.started_at
                        a_stop = app.stopped_at[11:19] if app.stopped_at and len(app.stopped_at) >= 19 else (
                            _current_timestamp()[11:19] if finalize_running else "-"
                        )
                        a_status = "COMPLETED" if (app.status == "running" and finalize_running) else app.status
                        app_label = f"  [APP] {app.app_name}"
                        if len(app_label) > col_w["name"] - 2:
                            app_label = app_label[: col_w["name"] - 5] + "..."
                        app_dur = app.get_duration(finalize_running=finalize_running)
                        app_row = (
                            f"{app_label:<{col_w['name']}}"
                            f"{a_start:<{col_w['start']}}"
                            f"{a_stop:<{col_w['stop']}}"
                            f"{format_duration(app_dur):<{col_w['dur']}}"
                            f"{a_status:<{col_w['status']}}"
                        )
                        lines.append(app_row)

                        for step in app.steps:
                            step_start = step.started_at[11:19] if len(step.started_at) >= 19 else step.started_at
                            step_stop = step.stopped_at[11:19] if step.stopped_at and len(step.stopped_at) >= 19 else (
                                _current_timestamp()[11:19] if finalize_running else "-"
                            )
                            step_status = "COMPLETED" if (step.status == "running" and finalize_running) else step.status
                            step_label = f"    - {step.name}"
                            if len(step_label) > col_w["name"] - 2:
                                step_label = step_label[: col_w["name"] - 5] + "..."
                            step_dur = step.get_duration(finalize_running=finalize_running)
                            step_row = (
                                f"{step_label:<{col_w['name']}}"
                                f"{step_start:<{col_w['start']}}"
                                f"{step_stop:<{col_w['stop']}}"
                                f"{format_duration(step_dur):<{col_w['dur']}}"
                                f"{step_status:<{col_w['status']}}"
                            )
                            lines.append(step_row)

                # Steps directly attached to stage (without app)
                for step in stage.steps:
                    if step.app:
                        continue  # Already displayed under app
                    step_start = step.started_at[11:19] if len(step.started_at) >= 19 else step.started_at
                    step_stop = step.stopped_at[11:19] if step.stopped_at and len(step.stopped_at) >= 19 else (
                        _current_timestamp()[11:19] if finalize_running else "-"
                    )
                    step_status = "COMPLETED" if (step.status == "running" and finalize_running) else step.status
                    step_label = f"  - {step.name}"
                    if len(step_label) > col_w["name"] - 2:
                        step_label = step_label[: col_w["name"] - 5] + "..."
                    step_dur = step.get_duration(finalize_running=finalize_running)
                    step_row = (
                        f"{step_label:<{col_w['name']}}"
                        f"{step_start:<{col_w['start']}}"
                        f"{step_stop:<{col_w['stop']}}"
                        f"{format_duration(step_dur):<{col_w['dur']}}"
                        f"{step_status:<{col_w['status']}}"
                    )
                    lines.append(step_row)
        else:
            # Flat steps list if stages weren't explicitly used
            for step in self.steps:
                s_start = step.started_at[11:19] if len(step.started_at) >= 19 else step.started_at
                s_stop = step.stopped_at[11:19] if step.stopped_at and len(step.stopped_at) >= 19 else (
                    _current_timestamp()[11:19] if finalize_running else "-"
                )
                step_status = "COMPLETED" if (step.status == "running" and finalize_running) else step.status
                step_label = f"- {step.name}"
                step_dur = step.get_duration(finalize_running=finalize_running)
                step_row = (
                    f"{step_label:<{col_w['name']}}"
                    f"{s_start:<{col_w['start']}}"
                    f"{s_stop:<{col_w['stop']}}"
                    f"{format_duration(step_dur):<{col_w['dur']}}"
                    f"{step_status:<{col_w['status']}}"
                )
                lines.append(step_row)

        lines.append("-" * border_width)
        total_label = "TOTAL DURATION"
        total_status = "COMPLETED" if (self.status == "running" and finalize_running) else self.status
        lines.append(
            f"{total_label:<{col_w['name'] + col_w['start'] + col_w['stop']}}"
            f"{format_duration(total_dur):<{col_w['dur']}}"
            f"{total_status:<{col_w['status']}}"
        )
        lines.append("=" * border_width)

        # If multi-repo or multiple apps present, append the App Summary Table
        if self.apps or self.multi_repo:
            lines.append("")
            lines.append(self.format_app_summary_table(border_width=border_width, finalize_running=finalize_running))

        return "\n".join(lines)

    def format_app_summary_table(self, border_width: int = 104, finalize_running: bool = True) -> str:
        """Formats a dedicated summary table comparing all applications in multi-repo."""
        lines = []
        lines.append("=" * border_width)
        lines.append(f"{'MULTI-REPO APP DURATION BREAKDOWN':^{border_width}}")
        lines.append("=" * border_width)

        col_w = {"app": 38, "branch": 14, "start": 16, "stop": 16, "dur": 12, "pct": 8}
        header = (
            f"{'Application / Repository':<{col_w['app']}}"
            f"{'Branch':<{col_w['branch']}}"
            f"{'Started At':<{col_w['start']}}"
            f"{'Stopped At':<{col_w['stop']}}"
            f"{'Duration':<{col_w['dur']}}"
            f"{'% Time':<{col_w['pct']}}"
        )
        lines.append(header)
        lines.append("-" * border_width)

        total_app_time = sum(a.get_duration(finalize_running=finalize_running) for a in self.apps.values())

        for app_name, app in self.apps.items():
            app_dur = app.get_duration(finalize_running=finalize_running)
            pct = (app_dur / total_app_time * 100.0) if total_app_time > 0 else 0.0
            app_label = app.app_name
            if len(app_label) > col_w["app"] - 2:
                app_label = app_label[: col_w["app"] - 5] + "..."
            branch_label = app.git_branch[: col_w["branch"] - 2] if app.git_branch else "main"
            a_start = app.started_at[11:19] if len(app.started_at) >= 19 else app.started_at
            a_stop = app.stopped_at[11:19] if app.stopped_at and len(app.stopped_at) >= 19 else (
                _current_timestamp()[11:19] if finalize_running else (app.stopped_at or "-")
            )

            row = (
                f"{app_label:<{col_w['app']}}"
                f"{branch_label:<{col_w['branch']}}"
                f"{a_start:<{col_w['start']}}"
                f"{a_stop:<{col_w['stop']}}"
                f"{format_duration(app_dur):<{col_w['dur']}}"
                f"{pct:>5.1f}%  "
            )
            lines.append(row)

        lines.append("-" * border_width)
        summary_row = (
            f"{f'Total Apps ({len(self.apps)})':<{col_w['app'] + col_w['branch'] + col_w['start'] + col_w['stop']}}"
            f"{format_duration(total_app_time):<{col_w['dur']}}"
            f"{'100.0%':<{col_w['pct']}}"
        )
        lines.append(summary_row)
        lines.append("=" * border_width)
        return "\n".join(lines)

    # ------------------------------------------------------------------------
    # Table Formatting (Markdown)
    # ------------------------------------------------------------------------

    def format_markdown_table(self, finalize_running: bool = False) -> str:
        """Formats the execution metrics as a GitHub-flavored Markdown document."""
        md = []
        status_str = "COMPLETED" if (self.status == "running" and finalize_running) else self.status
        stop_str = (self.stopped_at or _current_timestamp()) if finalize_running else (self.stopped_at or "In Progress")
        total_dur = self.get_total_duration(finalize_running=finalize_running)

        md.append(f"### Pipeline Execution Metrics: `{self.pipeline_name}`\n")
        md.append(f"- **Status**: `{status_str}`")
        md.append(f"- **Started At**: `{self.started_at}`")
        md.append(f"- **Stopped At**: `{stop_str}`")
        md.append(f"- **Total Duration**: `{format_duration(total_dur)}` ({total_dur:.2f}s)\n")

        md.append("| Stage / App / Step | Started At | Stopped At | Duration | Status |")
        md.append("| :--- | :--- | :--- | :--- | :--- |")

        if self.stages:
            for stage_name, stage in self.stages.items():
                s_stop = stage.stopped_at or (_current_timestamp() if finalize_running else "-")
                s_status = "COMPLETED" if (stage.status == "running" and finalize_running) else stage.status
                stage_dur = stage.get_duration(finalize_running=finalize_running)
                md.append(
                    f"| **Stage: {stage.name}** | {stage.started_at} | {s_stop} | "
                    f"**{format_duration(stage_dur)}** | `{s_status}` |"
                )
                if stage.apps:
                    for app_name, app in stage.apps.items():
                        a_stop = app.stopped_at or (_current_timestamp() if finalize_running else "-")
                        a_status = "COMPLETED" if (app.status == "running" and finalize_running) else app.status
                        app_dur = app.get_duration(finalize_running=finalize_running)
                        md.append(
                            f"| &nbsp;&nbsp;&nbsp;&nbsp;**App: {app.app_name}** | {app.started_at} | "
                            f"{a_stop} | **{format_duration(app_dur)}** | `{a_status}` |"
                        )
                        for step in app.steps:
                            step_stop = step.stopped_at or (_current_timestamp() if finalize_running else "-")
                            step_status = "COMPLETED" if (step.status == "running" and finalize_running) else step.status
                            step_dur = step.get_duration(finalize_running=finalize_running)
                            md.append(
                                f"| &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;↳ {step.name} | "
                                f"{step.started_at} | {step_stop} | "
                                f"{format_duration(step_dur)} | `{step_status}` |"
                            )
                for step in stage.steps:
                    if step.app:
                        continue
                    step_stop = step.stopped_at or (_current_timestamp() if finalize_running else "-")
                    step_status = "COMPLETED" if (step.status == "running" and finalize_running) else step.status
                    step_dur = step.get_duration(finalize_running=finalize_running)
                    md.append(
                        f"| &nbsp;&nbsp;&nbsp;&nbsp;↳ {step.name} | {step.started_at} | "
                        f"{step_stop} | {format_duration(step_dur)} | `{step_status}` |"
                    )
        else:
            for step in self.steps:
                step_stop = step.stopped_at or (_current_timestamp() if finalize_running else "-")
                step_status = "COMPLETED" if (step.status == "running" and finalize_running) else step.status
                step_dur = step.get_duration(finalize_running=finalize_running)
                md.append(
                    f"| {step.name} | {step.started_at} | {step_stop} | "
                    f"{format_duration(step_dur)} | `{step_status}` |"
                )

        md.append(
            f"| **TOTAL DURATION** | **{self.started_at}** | **{stop_str}** | "
            f"**{format_duration(total_dur)}** | **`{status_str}`** |"
        )

        if self.apps or self.multi_repo:
            md.append("\n#### Multi-Repo Application Breakdown\n")
            md.append("| Application / Repository | Branch | Started At | Stopped At | Duration | % Time | Status |")
            md.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
            total_app_time = sum(a.get_duration(finalize_running=finalize_running) for a in self.apps.values())
            for app_name, app in self.apps.items():
                app_dur = app.get_duration(finalize_running=finalize_running)
                pct = (app_dur / total_app_time * 100.0) if total_app_time > 0 else 0.0
                a_stop = app.stopped_at or (_current_timestamp() if finalize_running else "-")
                a_status = "COMPLETED" if (app.status == "running" and finalize_running) else app.status
                md.append(
                    f"| `{app.app_name}` | `{app.git_branch}` | {app.started_at} | "
                    f"{a_stop} | {format_duration(app_dur)} | {pct:.1f}% | `{a_status}` |"
                )
            md.append(
                f"| **Total Apps ({len(self.apps)})** | - | - | - | "
                f"**{format_duration(total_app_time)}** | **100.0%** | **`{status_str}`** |"
            )

        return "\n".join(md)

    def format_markdown_section(self, finalize_running: bool = True) -> str:
        """Wraps the formatted ASCII table in a markdown code fence block."""
        return f"\n\n### Pipeline Execution Metrics Summary\n\n```\n{self.format_summary_table(finalize_running=finalize_running)}\n```\n"

    # ------------------------------------------------------------------------
    # Serialization & Logging
    # ------------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serializes tracker records to a structured dictionary."""
        total_dur = self.get_total_duration()
        return {
            "pipeline_name": self.pipeline_name,
            "multi_repo": self.multi_repo,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "total_duration": round(total_dur, 4),
            "total_duration_formatted": format_duration(total_dur),
            "status": self.status,
            "error_message": self.error_message,
            "stages": {k: s.to_dict() for k, s in self.stages.items()},
            "apps": {k: a.to_dict() for k, a in self.apps.items()},
            "steps": [s.to_dict() for s in self.steps],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PipelineMetricsTracker":
        """Deserializes tracker records from a structured dictionary."""
        tracker = cls(
            pipeline_name=data.get("pipeline_name", "pipeline"),
            multi_repo=data.get("multi_repo", False),
        )
        tracker._start_perf = 0.0
        tracker.started_at = data.get("started_at", tracker.started_at)
        tracker.stopped_at = data.get("stopped_at")
        tracker.total_duration = float(data.get("total_duration", 0.0))
        tracker.status = data.get("status", "COMPLETED")
        tracker.error_message = data.get("error_message", "")

        # Reconstruct stages
        for stage_name, s_data in data.get("stages", {}).items():
            stage = StageMetric(name=s_data.get("name", stage_name))
            stage._start_perf = 0.0
            stage.started_at = s_data.get("started_at", stage.started_at)
            stage.stopped_at = s_data.get("stopped_at")
            stage.duration = float(s_data.get("duration", 0.0))
            stage.status = s_data.get("status", "COMPLETED")
            stage.error_message = s_data.get("error_message", "")

            # Apps under stage
            for app_name, a_data in s_data.get("apps", {}).items():
                app = AppMetric(
                    app_name=a_data.get("app_name", app_name),
                    git_repo=a_data.get("git_repo", ""),
                    git_branch=a_data.get("git_branch", "main"),
                )
                app._start_perf = 0.0
                app.started_at = a_data.get("started_at", app.started_at)
                app.stopped_at = a_data.get("stopped_at")
                app.duration = float(a_data.get("duration", 0.0))
                app.status = a_data.get("status", "COMPLETED")
                app.error_message = a_data.get("error_message", "")
                for step_data in a_data.get("steps", []):
                    step = StepMetric(
                        name=step_data.get("name", ""),
                        stage=step_data.get("stage", stage.name),
                        app=step_data.get("app", app_name),
                        details=step_data.get("details", {}),
                    )
                    step._start_perf = 0.0
                    step.started_at = step_data.get("started_at", step.started_at)
                    step.stopped_at = step_data.get("stopped_at")
                    step.duration = float(step_data.get("duration", 0.0))
                    step.status = step_data.get("status", "COMPLETED")
                    step.error_message = step_data.get("error_message", "")
                    app.steps.append(step)
                stage.apps[app_name] = app
                tracker.apps[app_name] = app

            # Steps under stage
            for step_data in s_data.get("steps", []):
                step = StepMetric(
                    name=step_data.get("name", ""),
                    stage=step_data.get("stage", stage.name),
                    app=step_data.get("app"),
                    details=step_data.get("details", {}),
                )
                step._start_perf = 0.0
                step.started_at = step_data.get("started_at", step.started_at)
                step.stopped_at = step_data.get("stopped_at")
                step.duration = float(step_data.get("duration", 0.0))
                step.status = step_data.get("status", "COMPLETED")
                step.error_message = step_data.get("error_message", "")
                stage.steps.append(step)
                tracker.steps.append(step)

            if stage.status == "running" and (tracker.status != "running" or (stage.steps and all(s.status != "running" for s in stage.steps))):
                stage.status = "COMPLETED"
                if not stage.stopped_at and stage.steps:
                    stage.stopped_at = stage.steps[-1].stopped_at

            tracker.stages[stage_name] = stage

        # Top-level apps (if not already reconstructed)
        for app_name, a_data in data.get("apps", {}).items():
            if app_name not in tracker.apps:
                app = AppMetric(
                    app_name=a_data.get("app_name", app_name),
                    git_repo=a_data.get("git_repo", ""),
                    git_branch=a_data.get("git_branch", "main"),
                )
                app._start_perf = 0.0
                app.started_at = a_data.get("started_at", app.started_at)
                app.stopped_at = a_data.get("stopped_at")
                app.duration = float(a_data.get("duration", 0.0))
                app.status = a_data.get("status", "COMPLETED")
                app.error_message = a_data.get("error_message", "")
                for step_data in a_data.get("steps", []):
                    step = StepMetric(
                        name=step_data.get("name", ""),
                        stage=step_data.get("stage"),
                        app=step_data.get("app", app_name),
                        details=step_data.get("details", {}),
                    )
                    step._start_perf = 0.0
                    step.started_at = step_data.get("started_at", step.started_at)
                    step.stopped_at = step_data.get("stopped_at")
                    step.duration = float(step_data.get("duration", 0.0))
                    step.status = step_data.get("status", "COMPLETED")
                    step.error_message = step_data.get("error_message", "")
                    app.steps.append(step)
                tracker.apps[app_name] = app

        # Top-level steps (if not already reconstructed)
        existing_step_keys = {(s.stage, s.app, s.name) for s in tracker.steps}
        for step_data in data.get("steps", []):
            key = (step_data.get("stage"), step_data.get("app"), step_data.get("name"))
            if key not in existing_step_keys:
                step = StepMetric(
                    name=step_data.get("name", ""),
                    stage=step_data.get("stage"),
                    app=step_data.get("app"),
                    details=step_data.get("details", {}),
                )
                step._start_perf = 0.0
                step.started_at = step_data.get("started_at", step.started_at)
                step.stopped_at = step_data.get("stopped_at")
                step.duration = float(step_data.get("duration", 0.0))
                step.status = step_data.get("status", "COMPLETED")
                step.error_message = step_data.get("error_message", "")
                tracker.steps.append(step)

        return tracker

    @classmethod
    def load_from_file(cls, filepath: str) -> Optional["PipelineMetricsTracker"]:
        """Loads and returns a tracker instance from a JSON file, or None if not found/invalid."""
        if not filepath or not os.path.exists(filepath):
            return None
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            logging.info(f"Loaded pipeline metrics from '{filepath}'.")
            return cls.from_dict(data)
        except Exception as e:
            logging.warning(f"Could not load pipeline metrics from '{filepath}': {e}")
            return None

    def merge(self, other: "PipelineMetricsTracker"):
        """Merges another tracker's stages, apps, and steps into this tracker."""
        if not other:
            return
        if other.started_at and (not self.started_at or other.started_at < self.started_at):
            self.started_at = other.started_at

        for stage_name, other_stage in other.stages.items():
            if stage_name not in self.stages:
                self.stages[stage_name] = other_stage
            else:
                cur_stage = self.stages[stage_name]
                cur_step_names = {s.name for s in cur_stage.steps}
                for s in other_stage.steps:
                    if s.name not in cur_step_names:
                        cur_stage.steps.insert(0, s)
                        self.steps.insert(0, s)
                for a_name, a_val in other_stage.apps.items():
                    if a_name not in cur_stage.apps:
                        cur_stage.apps[a_name] = a_val
                cur_stage.duration = max(cur_stage.duration, other_stage.duration)
                if other_stage.started_at and (not cur_stage.started_at or other_stage.started_at < cur_stage.started_at):
                    cur_stage.started_at = other_stage.started_at

        for app_name, other_app in other.apps.items():
            if app_name not in self.apps:
                self.apps[app_name] = other_app
            else:
                cur_app = self.apps[app_name]
                cur_step_names = {s.name for s in cur_app.steps}
                for s in other_app.steps:
                    if s.name not in cur_step_names:
                        cur_app.steps.insert(0, s)
                cur_app.duration = max(cur_app.duration, other_app.duration)

        cur_step_keys = {(s.stage, s.app, s.name) for s in self.steps}
        for s in other.steps:
            if (s.stage, s.app, s.name) not in cur_step_keys:
                self.steps.insert(0, s)

        self.total_duration = self.get_total_duration()

    @classmethod
    def load_or_create(
        cls,
        search_paths: Optional[List[str]] = None,
        pipeline_name: str = "pipeline",
        multi_repo: bool = False,
        git_repo: Optional[str] = None,
        git_branch: Optional[str] = None,
    ) -> "PipelineMetricsTracker":
        """Finds and loads prior metrics from paths/DefaultAssetLoader or creates a fresh tracker."""
        tracker = None
        paths = search_paths or []
        for p in paths:
            if not p:
                continue
            fpath = os.path.join(p, "pipeline_metrics.json") if os.path.isdir(p) else p
            if os.path.isfile(fpath):
                tracker = cls.load_from_file(fpath)
                if tracker:
                    break

        asset_tracker = None
        if git_repo:
            try:
                from pipelines.base.data_generation import generate_git_slug
                from loaders.default_asset_loader import DefaultAssetLoader
                git_slug = generate_git_slug(git_repo, git_branch or "")
                loader = DefaultAssetLoader()
                res_path = f"pipeline_metrics_{git_slug}.json" if git_slug else "pipeline_metrics.json"
                import tempfile
                with tempfile.TemporaryDirectory() as tmp_dir:
                    res = loader.download(res_path, download_dir=tmp_dir)
                    if isinstance(res, dict):
                        asset_tracker = cls.from_dict(res)
                    elif res and os.path.isfile(str(res)):
                        asset_tracker = cls.load_from_file(str(res))
            except Exception as e:
                logging.debug(f"Could not load prior metrics via DefaultAssetLoader: {e}")

        if tracker and asset_tracker:
            tracker.merge(asset_tracker)
        elif not tracker and asset_tracker:
            tracker = asset_tracker

        if tracker is None:
            tracker = cls(pipeline_name=pipeline_name, multi_repo=multi_repo)
        else:
            if pipeline_name and pipeline_name != "pipeline":
                tracker.pipeline_name = pipeline_name
            if multi_repo:
                tracker.multi_repo = True
        return tracker

    def save_to_file(self, filepath: str):
        """Saves metrics state to a JSON file."""
        dirname = os.path.dirname(filepath)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)
        logging.info(f"Saved pipeline metrics to '{filepath}'.")

    def save_and_log(
        self,
        target_dir: Optional[str] = None,
        git_repo: Optional[str] = None,
        git_branch: Optional[str] = None,
        multi_repo: bool = False,
    ):
        """Saves metrics to target_dir/pipeline_metrics.json and logs via DefaultAssetLoader."""
        if target_dir:
            try:
                metrics_file = os.path.join(target_dir, "pipeline_metrics.json")
                self.save_to_file(metrics_file)
            except Exception as e:
                logging.warning(f"Failed to save metrics to {target_dir}: {e}")

        try:
            from loaders.default_asset_loader import DefaultAssetLoader
            from pipelines.base.data_generation import generate_git_slug
            git_slug = generate_git_slug(git_repo, git_branch or "") if git_repo else None
            res_filename = f"pipeline_metrics_{git_slug}.json" if git_slug else ("pipeline_metrics_multi_repo.json" if multi_repo else "pipeline_metrics.json")
            loader = DefaultAssetLoader()
            artifact_path = loader.get_log_results_artifact_path(
                loader.RESULTS_PATH_PREFIX_PIPELINES,
                git_slug=git_slug,
                multi_repo=multi_repo,
            ) if hasattr(loader, "get_log_results_artifact_path") else None
            loader.log_results(
                res_filename,
                artifact_path=artifact_path,
                content=json.dumps(self.to_dict(), indent=2),
                tags={"category": "metrics", "pipeline": self.pipeline_name, "git_slug": git_slug, "multi_repo": multi_repo},
            )
        except Exception as e:
            logging.debug(f"Failed to log metrics to DefaultAssetLoader: {e}")

    def log_summary(self):
        """Outputs the formatted ASCII summary table to the logger."""
        print(self.format_summary_table(), flush=True)
        logging.info("\n" + self.format_summary_table())

    def log_to_mlflow(self, run_id: Optional[str] = None):
        """Logs metrics and artifact to an active MLflow run if available."""
        try:
            import mlflow
            metrics = {
                "pipeline_total_duration_seconds": self.total_duration,
            }
            for stage_name, stage in self.stages.items():
                safe_stage = stage_name.lower().replace(" ", "_").replace("-", "_")
                metrics[f"stage_{safe_stage}_duration_seconds"] = stage.duration

            for app_name, app in self.apps.items():
                safe_app = app_name.lower().replace(" ", "_").replace("-", "_").replace("/", "_")
                metrics[f"app_{safe_app}_duration_seconds"] = app.duration

            if run_id:
                with mlflow.start_run(run_id=run_id):
                    mlflow.log_metrics(metrics)
                    mlflow.log_text(self.format_summary_table(), "pipeline_metrics_summary.txt")
                    mlflow.log_text(self.format_markdown_table(), "pipeline_metrics_summary.md")
            else:
                mlflow.log_metrics(metrics)
                mlflow.log_text(self.format_summary_table(), "pipeline_metrics_summary.txt")
                mlflow.log_text(self.format_markdown_table(), "pipeline_metrics_summary.md")
        except Exception as e:
            logging.debug(f"MLflow metric logging skipped or failed: {e}")
