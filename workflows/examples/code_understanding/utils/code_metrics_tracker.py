from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from typing import Any, Dict, List, Optional, Set, Tuple


class CodeMetricsTracker:
    """Tracks project size metrics (file counts and lines of code) across pipeline stages."""

    _global_instance: Optional["CodeMetricsTracker"] = None

    @classmethod
    def get_instance(cls, git_slug: Optional[str] = None, git_repo: Optional[str] = None) -> "CodeMetricsTracker":
        if cls._global_instance is None:
            cls._global_instance = cls(git_slug=git_slug, git_repo=git_repo)
        else:
            if git_slug and not cls._global_instance.git_slug:
                cls._global_instance.git_slug = git_slug
            if git_repo and not cls._global_instance.git_repo:
                cls._global_instance.git_repo = git_repo
        return cls._global_instance

    @classmethod
    def reset_instance(cls, git_slug: Optional[str] = None, git_repo: Optional[str] = None) -> "CodeMetricsTracker":
        cls._global_instance = cls(git_slug=git_slug, git_repo=git_repo)
        return cls._global_instance

    def __init__(self, git_slug: Optional[str] = None, git_repo: Optional[str] = None, run_id: Optional[str] = None):
        self.git_slug: Optional[str] = git_slug
        self.git_repo: Optional[str] = git_repo
        self.run_id: Optional[str] = run_id
        self.total_repo_files: int = 0
        self.total_repo_lines: int = 0
        self.languages: Dict[str, Dict[str, int]] = {}
        self.skipped_large_files: List[str] = []
        self._scanned_files: Set[str] = set()
        self._merged_files: Set[str] = set()
        self._merged_runs: Set[str] = set()

    def reset(self):
        """Resets all metrics state."""
        self.total_repo_files = 0
        self.total_repo_lines = 0
        self.languages.clear()
        self.skipped_large_files.clear()
        self._scanned_files.clear()
        self._merged_files.clear()
        self._merged_runs.clear()

    @staticmethod
    def count_file_lines(
        filepath: str,
        language: Optional[str] = None,
        ext: Optional[str] = None,
    ) -> Dict[str, int]:
        """Counts total lines, blank lines, comment lines, and pure code lines (SLOC) in a file.

        Handles common single-line and block comment delimiters for various languages.
        """
        total = 0
        blank = 0
        comment = 0

        # Determine comment delimiters
        single_line_prefixes: List[str] = []
        block_delim: Optional[Tuple[str, str]] = None

        if not ext and filepath:
            ext = os.path.splitext(filepath)[1].lower()

        # Language-specific single-line comment defaults
        lang_lower = (language or "").lower().strip()
        if lang_lower in ("python", "ruby", "shell", "bash", "ansible", "terraform", "yaml", "yml"):
            single_line_prefixes.extend(["#"])
        elif lang_lower in ("java", "javascript", "typescript", "c", "c++", "go", "rust", "scala", "kotlin"):
            single_line_prefixes.extend(["//"])
        elif lang_lower in ("sql",):
            single_line_prefixes.extend(["--", "#"])
        else:
            # Extension-based fallback
            if ext in (".py", ".pyi", ".rb", ".sh", ".bash", ".yaml", ".yml", ".tf", ".conf", ".ini", ".cfg", ".toml"):
                single_line_prefixes.extend(["#"])
            elif ext in (".java", ".kt", ".js", ".ts", ".c", ".cpp", ".cc", ".h", ".hpp", ".go", ".rs"):
                single_line_prefixes.extend(["//"])
            elif ext in (".sql",):
                single_line_prefixes.extend(["--"])

        # Check language_mappings for delimiters
        try:
            from utils import code_utils
            if ext:
                try:
                    delims = code_utils.get_comment_delimiters_for_file_extension(ext)
                    if delims and len(delims) >= 2:
                        start, end = delims[0], delims[1]
                        if start and not end:
                            if start not in single_line_prefixes:
                                single_line_prefixes.append(start)
                        elif start and end:
                            block_delim = (start, end)
                except Exception:
                    pass
            if not block_delim and language:
                try:
                    delims = code_utils.get_comment_delimiters_for_language(language)
                    if delims and len(delims) >= 2:
                        start, end = delims[0], delims[1]
                        if start and not end:
                            if start not in single_line_prefixes:
                                single_line_prefixes.append(start)
                        elif start and end:
                            block_delim = (start, end)
                except Exception:
                    pass
        except Exception:
            pass

        # Fallback block comment styles if none configured
        if not block_delim:
            if lang_lower in ("java", "javascript", "typescript", "c", "c++", "sql") or ext in (
                ".java", ".kt", ".js", ".ts", ".c", ".cpp", ".cc", ".h", ".sql"
            ):
                block_delim = ("/*", "*/")
            elif lang_lower in ("html", "xml") or ext in (".html", ".xml", ".htm", ".xhtml"):
                block_delim = ("<!--", "-->")

        in_block_comment = False

        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    total += 1
                    stripped = line.strip()

                    if not stripped:
                        blank += 1
                        continue

                    if in_block_comment:
                        comment += 1
                        if block_delim and block_delim[1] in stripped:
                            in_block_comment = False
                        continue

                    # Check for start of block comment
                    if block_delim and stripped.startswith(block_delim[0]):
                        comment += 1
                        # Check if block comment ends on the same line
                        if block_delim[1] not in stripped[len(block_delim[0]):]:
                            in_block_comment = True
                        continue

                    # Check for single-line comments
                    is_comment = False
                    for prefix in single_line_prefixes:
                        if stripped.startswith(prefix):
                            comment += 1
                            is_comment = True
                            break

                    if is_comment:
                        continue

        except Exception as e:
            logging.debug(f"Error reading file {filepath} for line counting: {e}")
            return {"total": 0, "blank": 0, "comment": 0, "code": 0}

        code_lines = max(0, total - blank - comment)
        return {
            "total": total,
            "blank": blank,
            "comment": comment,
            "code": code_lines,
        }

    def measure_repository(
        self,
        source_path: str,
        max_file_size: int = 200_000,
        languages: Optional[List[str]] = None,
    ):
        """Scans the repository directory and aggregates file counts and lines of code.

        Args:
            source_path: Absolute or relative path to the cloned repository.
            max_file_size: Byte threshold above which files are marked as skipped.
            languages: Optional list of languages to restrict analysis to. If None,
                       languages are auto-detected using code_utils.
        """
        if not source_path or not os.path.exists(source_path):
            logging.warning(f"CodeMetricsTracker: Source path '{source_path}' does not exist.")
            return

        from utils import code_utils

        if not languages:
            try:
                languages = code_utils.get_detected_languages_for_repo(source_path)
            except Exception as e:
                logging.debug(f"Failed to auto-detect languages for {source_path}: {e}")
                languages = []

        if not languages:
            languages = ["other"]

        for lang in languages:
            if lang not in self.languages:
                self.languages[lang] = {
                    "source_files": 0,
                    "config_files": 0,
                    "total_files": 0,
                    "code_lines": 0,
                    "comment_lines": 0,
                    "blank_lines": 0,
                    "total_lines": 0,
                }

            try:
                excluded_dirs = code_utils.get_exclude_dirs_for_language(lang)
            except Exception:
                excluded_dirs = {".git", "node_modules", "target", "vendor", "__pycache__", ".venv"}

            try:
                code_exts = set(code_utils.get_file_extensions_for_language(lang))
            except Exception:
                code_exts = set()

            try:
                config_exts = set(code_utils.get_config_file_extensions_for_language(lang))
            except Exception:
                config_exts = set()

            for root, dirs, files in os.walk(source_path):
                # Filter excluded dirs in-place
                dirs[:] = [
                    d for d in dirs
                    if d not in excluded_dirs and not d.startswith(".")
                ]

                for fname in files:
                    ext = os.path.splitext(fname)[1].lower()
                    is_code = ext in code_exts
                    is_config = ext in config_exts

                    # If 'other' or no extensions defined, match non-hidden files
                    if not code_exts and not config_exts:
                        if not fname.startswith("."):
                            is_code = True

                    if not is_code and not is_config:
                        continue

                    fpath = os.path.join(root, fname)
                    rel_path = os.path.relpath(fpath, source_path)

                    # Deduplicate if scanned across multiple passes
                    scan_key = f"{lang}:{rel_path}"
                    if scan_key in self._scanned_files:
                        continue
                    self._scanned_files.add(scan_key)

                    try:
                        fsize = os.path.getsize(fpath)
                    except Exception:
                        continue

                    if fsize > max_file_size:
                        if rel_path not in self.skipped_large_files:
                            self.skipped_large_files.append(rel_path)
                        continue

                    counts = self.count_file_lines(fpath, language=lang, ext=ext)
                    rec = self.languages[lang]

                    if is_code:
                        rec["source_files"] += 1
                    else:
                        rec["config_files"] += 1

                    rec["total_files"] += 1
                    rec["code_lines"] += counts["code"]
                    rec["comment_lines"] += counts["comment"]
                    rec["blank_lines"] += counts["blank"]
                    rec["total_lines"] += counts["total"]

        # Recalculate overall repository totals
        self.total_repo_files = sum(l["total_files"] for l in self.languages.values())
        self.total_repo_lines = sum(l["total_lines"] for l in self.languages.values())

    def measure_dataframe(self, code_df, language: str, config: bool = False):
        """Aggregates metrics directly from a Pandas DataFrame generated during raw dataset parsing."""
        if code_df is None or len(code_df) == 0:
            return

        if language not in self.languages:
            self.languages[language] = {
                "source_files": 0,
                "config_files": 0,
                "total_files": 0,
                "code_lines": 0,
                "comment_lines": 0,
                "blank_lines": 0,
                "total_lines": 0,
            }

        rec = self.languages[language]

        for _, row in code_df.iterrows():
            code_text = row.get("code", "")
            fpath = row.get("file_path", "")

            file_key = f"{language}:{config}:{fpath}"
            if file_key in self._scanned_files:
                continue
            self._scanned_files.add(file_key)

            lines = code_text.splitlines()
            total = len(lines)
            blank = sum(1 for line in lines if not line.strip())
            # Simple comment estimation for DataFrame string
            comment = sum(1 for line in lines if line.strip().startswith(("#", "//", "/*", "*", "<!--")))
            code = max(0, total - blank - comment)

            if config:
                rec["config_files"] += 1
            else:
                rec["source_files"] += 1

            rec["total_files"] += 1
            rec["code_lines"] += code
            rec["comment_lines"] += comment
            rec["blank_lines"] += blank
            rec["total_lines"] += total

        self.total_repo_files = sum(l["total_files"] for l in self.languages.values())
        self.total_repo_lines = sum(l["total_lines"] for l in self.languages.values())

    def to_dict(self) -> Dict[str, Any]:
        """Serializes code metrics to a dictionary."""
        d = {
            "total_repo_files": self.total_repo_files,
            "total_repo_lines": self.total_repo_lines,
            "total_code_lines": sum(l.get("code_lines", 0) for l in self.languages.values()),
            "total_comment_lines": sum(l.get("comment_lines", 0) for l in self.languages.values()),
            "total_blank_lines": sum(l.get("blank_lines", 0) for l in self.languages.values()),
            "languages": self.languages,
            "skipped_large_files": self.skipped_large_files,
        }
        if self.git_slug:
            d["git_slug"] = self.git_slug
        if self.git_repo:
            d["git_repo"] = self.git_repo
        if self.run_id:
            d["run_id"] = self.run_id
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CodeMetricsTracker":
        """Reconstructs a CodeMetricsTracker instance from a dictionary."""
        tracker = cls(
            git_slug=data.get("git_slug"),
            git_repo=data.get("git_repo"),
            run_id=data.get("run_id"),
        )
        tracker.total_repo_files = data.get("total_repo_files", 0)
        tracker.total_repo_lines = data.get("total_repo_lines", 0)
        tracker.languages = data.get("languages", {})
        tracker.skipped_large_files = data.get("skipped_large_files", [])
        return tracker

    def save_to_file(self, filepath: str):
        """Serializes and saves code metrics to a JSON file."""
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load_from_file(cls, filepath: str) -> Optional["CodeMetricsTracker"]:
        """Loads a CodeMetricsTracker from a JSON file, or returns None if missing/invalid."""
        if not filepath or not os.path.exists(filepath):
            return None
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            return cls.from_dict(data)
        except Exception as e:
            logging.debug(f"Failed to load code metrics from {filepath}: {e}")
            return None

    def load_and_merge(self, filepath: str, current_stage: Optional[str] = None):
        """Loads metrics from filepath and merges them into this tracker."""
        if not filepath or not os.path.exists(filepath):
            return

        norm_path = os.path.normpath(os.path.abspath(filepath))
        if norm_path in self._merged_files:
            return

        other = self.load_from_file(filepath)
        if not other:
            return

        if not self.git_slug and other.git_slug:
            self.git_slug = other.git_slug
        if not self.git_repo and other.git_repo:
            self.git_repo = other.git_repo
        if not self.run_id and other.run_id:
            self.run_id = other.run_id

        for lang, rec in other.languages.items():
            if lang not in self.languages:
                self.languages[lang] = dict(rec)
            else:
                for k in rec:
                    self.languages[lang][k] = max(self.languages[lang].get(k, 0), rec[k])

        self.total_repo_files = sum(l["total_files"] for l in self.languages.values())
        self.total_repo_lines = sum(l["total_lines"] for l in self.languages.values())

        for sf in other.skipped_large_files:
            if sf not in self.skipped_large_files:
                self.skipped_large_files.append(sf)

        self._merged_files.add(norm_path)

    def log_to_mlflow(self, run_id: Optional[str] = None):
        """Logs file count and LOC metrics to active MLflow run."""
        try:
            import mlflow
            metrics = {
                "project_total_files": float(self.total_repo_files),
                "project_total_lines": float(self.total_repo_lines),
                "project_total_code_lines": float(sum(l.get("code_lines", 0) for l in self.languages.values())),
                "project_total_comment_lines": float(sum(l.get("comment_lines", 0) for l in self.languages.values())),
                "project_total_blank_lines": float(sum(l.get("blank_lines", 0) for l in self.languages.values())),
            }

            for lang, rec in self.languages.items():
                clean_lang = re.sub(r"[^a-zA-Z0-9_]", "_", lang.lower())
                metrics[f"project_{clean_lang}_total_files"] = float(rec.get("total_files", 0))
                metrics[f"project_{clean_lang}_source_files"] = float(rec.get("source_files", 0))
                metrics[f"project_{clean_lang}_config_files"] = float(rec.get("config_files", 0))
                metrics[f"project_{clean_lang}_code_lines"] = float(rec.get("code_lines", 0))
                metrics[f"project_{clean_lang}_total_lines"] = float(rec.get("total_lines", 0))

            active = mlflow.active_run()
            if active:
                mlflow.log_metrics(metrics)
            elif run_id:
                with mlflow.start_run(run_id=run_id):
                    mlflow.log_metrics(metrics)
        except Exception as e:
            logging.debug(f"Failed to log code metrics to MLflow: {e}")

    def upload_to_mlflow(
        self,
        git_slug: Optional[str] = None,
        stage: Optional[str] = None,
        multi_repo: bool = False,
    ):
        """Persists project_metrics.json as an MLflow artifact."""
        slug = git_slug or self.git_slug
        tmp_dir = tempfile.mkdtemp()
        try:
            tmp_file = os.path.join(tmp_dir, "project_metrics.json")
            self.save_to_file(tmp_file)
            from loaders.default_asset_loader import DefaultAssetLoader
            artifact_path = DefaultAssetLoader.get_log_results_artifact_path(
                DefaultAssetLoader.RESULTS_PATH_PREFIX_TELEMETRY,
                git_slug=slug,
                multi_repo=multi_repo,
            )
            DefaultAssetLoader().log_results(
                tmp_file,
                artifact_path=artifact_path,
                tags={
                    "git_slug": str(slug or "multi-repo"),
                    "category": "telemetry",
                    "type": "code_metrics",
                    "stage": str(stage or ""),
                    "multi_repo": str(multi_repo),
                },
            )
        except Exception as e:
            logging.debug(f"Failed to upload project_metrics.json to MLflow: {e}")
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def download_from_mlflow(
        self,
        git_slug: Optional[str] = None,
        multi_repo: bool = False,
        current_stage: Optional[str] = None,
        run_id: Optional[str] = None,
    ):
        """Downloads and merges project_metrics.json from MLflow run artifacts or asset loader."""
        slug = git_slug or self.git_slug
        target_run = run_id or os.environ.get("MLFLOW_RUN_ID")

        if not target_run:
            try:
                import mlflow
                active = mlflow.active_run()
                if active and hasattr(active, "info"):
                    rid = getattr(active.info, "run_id", None)
                    if isinstance(rid, str) and rid:
                        target_run = rid
            except Exception:
                pass

        merged_any = False
        if target_run and target_run not in self._merged_runs:
            try:
                import mlflow
                local_path = mlflow.artifacts.download_artifacts(
                    run_id=target_run, artifact_path="telemetry/project_metrics.json"
                )
                if local_path and os.path.exists(local_path):
                    self.load_and_merge(local_path, current_stage=current_stage)
                    self._merged_runs.add(target_run)
                    merged_any = True
            except Exception as e:
                logging.debug(f"Failed to download project_metrics.json for run {target_run}: {e}")

        if (slug or multi_repo) and not merged_any:
            try:
                from loaders.default_asset_loader import DefaultAssetLoader
                artifact_path = DefaultAssetLoader.get_log_results_artifact_path(
                    DefaultAssetLoader.RESULTS_PATH_PREFIX_TELEMETRY,
                    git_slug=slug,
                    multi_repo=multi_repo,
                )
                tmp_dir = tempfile.mkdtemp()
                try:
                    downloaded = DefaultAssetLoader().download_dir(artifact_path, tmp_dir)
                    if downloaded:
                        cand = os.path.join(tmp_dir, "project_metrics.json")
                        if os.path.exists(cand):
                            self.load_and_merge(cand, current_stage=current_stage)
                finally:
                    import shutil
                    shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception as e:
                logging.debug(f"Asset loader download for project_metrics.json failed: {e}")

    def format_summary(self) -> str:
        """Returns an ASCII table of code metrics for terminal and pod logging."""
        total_code = sum(l.get("code_lines", 0) for l in self.languages.values())
        total_comments = sum(l.get("comment_lines", 0) for l in self.languages.values())

        lines = [
            "=" * 82,
            f" PROJECT CODEBASE & REPOSITORY METRICS : {self.git_slug or 'unknown'}",
            "=" * 82,
            f" Total Analyzed Files : {self.total_repo_files:,}",
            f" Total Lines of Code  : {self.total_repo_lines:,} (Code: {total_code:,} | Comments: {total_comments:,})",
            "-" * 82,
            f" {'Language':<14} | {'Source':<8} | {'Config':<8} | {'Total Files':<12} | {'Lines (SLOC)':<12} | {'Comments':<8}",
            "-" * 82,
        ]

        for lang, rec in sorted(self.languages.items()):
            lines.append(
                f" {lang:<14} | {rec['source_files']:<8} | {rec['config_files']:<8} | "
                f"{rec['total_files']:<12,d} | {rec['code_lines']:<12,d} | {rec['comment_lines']:<8,d}"
            )

        lines.append("=" * 82)
        return "\n".join(lines)

    def format_markdown_section(self) -> str:
        """Returns a Markdown section with formatted tables for migration_report.md."""
        if not self.languages:
            return ""

        total_code = sum(l.get("code_lines", 0) for l in self.languages.values())
        total_comments = sum(l.get("comment_lines", 0) for l in self.languages.values())
        total_blanks = sum(l.get("blank_lines", 0) for l in self.languages.values())

        lines = [
            "\n### Project Codebase & Scope Summary\n",
            f"> **Repository Scope**: **{self.total_repo_files:,}** file(s) analyzed across "
            f"**{len(self.languages)}** detected language(s) with **{self.total_repo_lines:,}** total lines "
            f"(**{total_code:,}** code, **{total_comments:,}** comments, **{total_blanks:,}** blank).\n",
            "| Language | Source Files | Config Files | Total Files | Code Lines (SLOC) | Comment Lines | Blank Lines |",
            "| :--- | :---: | :---: | :---: | :---: | :---: | :---: |",
        ]

        for lang, rec in sorted(self.languages.items()):
            lines.append(
                f"| **{lang}** | {rec['source_files']:,} | {rec['config_files']:,} | "
                f"{rec['total_files']:,} | {rec['code_lines']:,} | {rec['comment_lines']:,} | {rec['blank_lines']:,} |"
            )

        if self.skipped_large_files:
            lines.append(
                f"\n> [!NOTE]\n> **{len(self.skipped_large_files)}** oversized file(s) (>200 KB) were skipped from deep parsing."
            )

        return "\n".join(lines) + "\n"


def extract_data_generation_code_metrics(
    candidate_dirs: List[str],
    tracker: Optional[CodeMetricsTracker] = None,
) -> Optional[CodeMetricsTracker]:
    """Finds and merges project_metrics.json from candidate directories into tracker."""
    if tracker is None:
        tracker = CodeMetricsTracker.get_instance()

    from utils.duration_tracker import find_all_telemetry_files

    for fpath in find_all_telemetry_files(candidate_dirs, "project_metrics.json"):
        tracker.load_and_merge(fpath)

    return tracker
