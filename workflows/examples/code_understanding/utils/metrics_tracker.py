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
    """Tracks metrics for pipeline execution including timestamps, step durations,
    per-app breakdown for multi-repo runs, and summary reporting.
    """

    def __init__(self, pipeline_name: str = "pipeline", multi_repo: bool = False):
        self.pipeline_name = pipeline_name
        self.multi_repo = multi_repo
        self.started_at: str = _current_timestamp()
        self.stopped_at: Optional[str] = None
        self._start_perf: float = time.perf_counter()
        self.total_duration: float = 0.0
        self.status: str = "running"
        self.error_message: str = ""

        # Hierarchy collections
        self.stages: Dict[str, StageMetric] = {}
        self.apps: Dict[str, AppMetric] = {}
        self.steps: List[StepMetric] = []

        # Currently active contexts
        self._active_stage: Optional[str] = None
        self._active_app: Optional[str] = None
        self._active_steps: Dict[str, StepMetric] = {}

    # ------------------------------------------------------------------------
    # Pipeline lifecycle
    # ------------------------------------------------------------------------

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
        self.total_duration = max(0.0, time.perf_counter() - self._start_perf)
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
        duration = stage.complete(status=status, error_message=error_message)
        if self._active_stage == stage_name:
            self._active_stage = None
        logging.info(f"-- [STAGE END] {stage_name} [{status}] - Duration: {format_duration(duration)} --")
        return duration

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
        duration = app.complete(status=status, error_message=error_message)
        if self._active_app == app_name:
            self._active_app = None
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

    def format_summary_table(self, border_width: int = 104, finalize_running: bool = False) -> str:
        """Formats the entire pipeline execution metrics into an ASCII summary table."""
        lines = []

        status_str = "COMPLETED" if (self.status == "running" and finalize_running) else self.status
        stop_str = (self.stopped_at or _current_timestamp()) if finalize_running else (self.stopped_at or "In Progress")

        lines.append("=" * border_width)
        lines.append(f"{'PIPELINE EXECUTION METRICS':^{border_width}}")
        lines.append("=" * border_width)
        lines.append(f"Pipeline:       {self.pipeline_name}")
        lines.append(f"Status:         {status_str}")
        lines.append(f"Started At:     {self.started_at}")
        lines.append(f"Stopped At:     {stop_str}")
        lines.append(f"Total Duration: {format_duration(self.total_duration)} ({self.total_duration:.2f}s)")
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
                stage_row = (
                    f"{f'[STAGE] {stage.name}':<{col_w['name']}}"
                    f"{s_start:<{col_w['start']}}"
                    f"{s_stop:<{col_w['stop']}}"
                    f"{format_duration(stage.duration):<{col_w['dur']}}"
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
                        app_row = (
                            f"{app_label:<{col_w['name']}}"
                            f"{a_start:<{col_w['start']}}"
                            f"{a_stop:<{col_w['stop']}}"
                            f"{format_duration(app.duration):<{col_w['dur']}}"
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
                            step_row = (
                                f"{step_label:<{col_w['name']}}"
                                f"{step_start:<{col_w['start']}}"
                                f"{step_stop:<{col_w['stop']}}"
                                f"{format_duration(step.duration):<{col_w['dur']}}"
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
                    step_row = (
                        f"{step_label:<{col_w['name']}}"
                        f"{step_start:<{col_w['start']}}"
                        f"{step_stop:<{col_w['stop']}}"
                        f"{format_duration(step.duration):<{col_w['dur']}}"
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
                step_row = (
                    f"{step_label:<{col_w['name']}}"
                    f"{s_start:<{col_w['start']}}"
                    f"{s_stop:<{col_w['stop']}}"
                    f"{format_duration(step.duration):<{col_w['dur']}}"
                    f"{step_status:<{col_w['status']}}"
                )
                lines.append(step_row)

        lines.append("-" * border_width)
        total_label = "TOTAL DURATION"
        total_status = "COMPLETED" if (self.status == "running" and finalize_running) else self.status
        lines.append(
            f"{total_label:<{col_w['name'] + col_w['start'] + col_w['stop']}}"
            f"{format_duration(self.total_duration):<{col_w['dur']}}"
            f"{total_status:<{col_w['status']}}"
        )
        lines.append("=" * border_width)

        # If multi-repo or multiple apps present, append the App Summary Table
        if self.apps or self.multi_repo:
            lines.append("")
            lines.append(self.format_app_summary_table(border_width=border_width, finalize_running=finalize_running))

        return "\n".join(lines)

    def format_app_summary_table(self, border_width: int = 104, finalize_running: bool = False) -> str:
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

        total_app_time = sum(a.duration for a in self.apps.values())

        for app_name, app in self.apps.items():
            pct = (app.duration / total_app_time * 100.0) if total_app_time > 0 else 0.0
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
                f"{format_duration(app.duration):<{col_w['dur']}}"
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

    def format_markdown_table(self) -> str:
        """Formats the execution metrics as a GitHub-flavored Markdown document."""
        md = []
        md.append(f"### Pipeline Execution Metrics: `{self.pipeline_name}`\n")
        md.append(f"- **Status**: `{self.status}`")
        md.append(f"- **Started At**: `{self.started_at}`")
        md.append(f"- **Stopped At**: `{self.stopped_at or 'In Progress'}`")
        md.append(f"- **Total Duration**: `{format_duration(self.total_duration)}` ({self.total_duration:.2f}s)\n")

        md.append("| Stage / App / Step | Started At | Stopped At | Duration | Status |")
        md.append("| :--- | :--- | :--- | :--- | :--- |")

        if self.stages:
            for stage_name, stage in self.stages.items():
                md.append(
                    f"| **Stage: {stage.name}** | {stage.started_at} | {stage.stopped_at or '-'} | "
                    f"**{format_duration(stage.duration)}** | `{stage.status}` |"
                )
                if stage.apps:
                    for app_name, app in stage.apps.items():
                        md.append(
                            f"| &nbsp;&nbsp;&nbsp;&nbsp;**App: {app.app_name}** | {app.started_at} | "
                            f"{app.stopped_at or '-'} | **{format_duration(app.duration)}** | `{app.status}` |"
                        )
                        for step in app.steps:
                            md.append(
                                f"| &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;↳ {step.name} | "
                                f"{step.started_at} | {step.stopped_at or '-'} | "
                                f"{format_duration(step.duration)} | `{step.status}` |"
                            )
                for step in stage.steps:
                    if step.app:
                        continue
                    md.append(
                        f"| &nbsp;&nbsp;&nbsp;&nbsp;↳ {step.name} | {step.started_at} | "
                        f"{step.stopped_at or '-'} | {format_duration(step.duration)} | `{step.status}` |"
                    )
        else:
            for step in self.steps:
                md.append(
                    f"| {step.name} | {step.started_at} | {step.stopped_at or '-'} | "
                    f"{format_duration(step.duration)} | `{step.status}` |"
                )

        md.append(
            f"| **TOTAL DURATION** | **{self.started_at}** | **{self.stopped_at or '-'}** | "
            f"**{format_duration(self.total_duration)}** | **`{self.status}`** |"
        )

        if self.apps or self.multi_repo:
            md.append("\n#### Multi-Repo Application Breakdown\n")
            md.append("| Application / Repository | Branch | Started At | Stopped At | Duration | % Time | Status |")
            md.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
            total_app_time = sum(a.duration for a in self.apps.values())
            for app_name, app in self.apps.items():
                pct = (app.duration / total_app_time * 100.0) if total_app_time > 0 else 0.0
                md.append(
                    f"| `{app.app_name}` | `{app.git_branch}` | {app.started_at} | "
                    f"{app.stopped_at or '-'} | {format_duration(app.duration)} | {pct:.1f}% | `{app.status}` |"
                )
            md.append(
                f"| **Total Apps ({len(self.apps)})** | - | - | - | "
                f"**{format_duration(total_app_time)}** | **100.0%** | **`{self.status}`** |"
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
        return {
            "pipeline_name": self.pipeline_name,
            "multi_repo": self.multi_repo,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "total_duration": round(self.total_duration, 4),
            "total_duration_formatted": format_duration(self.total_duration),
            "status": self.status,
            "error_message": self.error_message,
            "stages": {k: s.to_dict() for k, s in self.stages.items()},
            "apps": {k: a.to_dict() for k, a in self.apps.items()},
            "steps": [s.to_dict() for s in self.steps],
        }

    def save_to_file(self, filepath: str):
        """Saves metrics state to a JSON file."""
        dirname = os.path.dirname(filepath)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)
        logging.info(f"Saved pipeline metrics to '{filepath}'.")

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
