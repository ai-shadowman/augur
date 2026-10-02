from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from typing import Any, Dict, List, Optional, Set, Tuple, Union


# Language and file type classification constants
EXT_TO_LANGUAGE: Dict[str, str] = {
    ".py": "python", ".pyi": "python", ".pyw": "python",
    ".java": "java", ".kt": "java", ".kts": "java", ".scala": "java", ".groovy": "java",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "javascript", ".mts": "javascript", ".cts": "javascript", ".tsx": "javascript",
    ".c": "c", ".h": "c",
    ".cpp": "c++", ".cc": "c++", ".cxx": "c++", ".hpp": "c++", ".hh": "c++", ".hxx": "c++",
    ".go": "go",
    ".rs": "rust",
    ".cs": "c#", ".fs": "f#", ".vb": "vb",
    ".rb": "ruby", ".erb": "ruby", ".gemspec": "ruby",
    ".php": "php", ".phtml": "php",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".html": "html", ".htm": "html",
    ".css": "css", ".scss": "css", ".sass": "css", ".less": "css",
    ".yaml": "yaml", ".yml": "yaml",
    ".json": "json",
    ".xml": "xml",
    ".toml": "toml",
    ".ini": "ini", ".cfg": "config", ".conf": "config",
    ".sql": "sql",
    ".md": "markdown",
    ".tf": "terraform", ".tfvars": "terraform",
    ".proto": "protobuf",
    ".graphql": "graphql", ".gql": "graphql",
}

BUILD_FILE_LANGUAGE: Dict[str, str] = {
    "pom.xml": "java",
    "build.gradle": "java",
    "build.gradle.kts": "java",
    "settings.gradle": "java",
    "requirements.txt": "python",
    "setup.py": "python",
    "setup.cfg": "python",
    "pyproject.toml": "python",
    "pipfile": "python",
    "pipfile.lock": "python",
    "package.json": "javascript",
    "package-lock.json": "javascript",
    "tsconfig.json": "javascript",
    "yarn.lock": "javascript",
    "pnpm-lock.yaml": "javascript",
    "cargo.toml": "rust",
    "cargo.lock": "rust",
    "go.mod": "go",
    "go.sum": "go",
    "gemfile": "ruby",
    "gemfile.lock": "ruby",
    "dockerfile": "dockerfile",
    "containerfile": "dockerfile",
    "docker-compose.yml": "yaml",
    "docker-compose.yaml": "yaml",
    "makefile": "other",
    "cmakelists.txt": "other",
}

CONFIG_EXTENSIONS: Set[str] = {
    ".yaml", ".yml", ".json", ".xml", ".toml", ".ini", ".cfg", ".conf", ".tfvars", ".properties"
}

BINARY_EXTENSIONS: Set[str] = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".zip", ".tar", ".gz", ".tgz",
    ".jar", ".war", ".ear", ".class", ".pyc", ".pyo", ".so", ".dll", ".dylib", ".exe",
    ".bin", ".parquet", ".db", ".sqlite", ".pdf", ".woff", ".woff2", ".ttf", ".eot",
    ".lock", ".map", ".min.js", ".min.css"
}

EXCLUDED_DIRS: Set[str] = {
    ".git", "node_modules", "target", "vendor", "__pycache__", ".venv", "venv",
    ".idea", ".vscode", "dist", "build", "bin", "obj", ".pytest_cache", ".ruff_cache"
}

SINGLE_LINE_COMMENTS: Dict[str, List[str]] = {
    "python": ["#"], "ruby": ["#"], "shell": ["#"], "bash": ["#"],
    "yaml": ["#"], "toml": ["#"], "terraform": ["#"],
    "java": ["//"], "javascript": ["//"], "typescript": ["//"],
    "c": ["//"], "c++": ["//"], "go": ["//"], "rust": ["//"],
    "scala": ["//"], "kotlin": ["//"], "c#": ["//"],
    "sql": ["--"],
}

BLOCK_COMMENTS: Dict[str, Tuple[str, str]] = {
    "java": ("/*", "*/"), "javascript": ("/*", "*/"), "typescript": ("/*", "*/"),
    "c": ("/*", "*/"), "c++": ("/*", "*/"), "go": ("/*", "*/"), "rust": ("/*", "*/"),
    "scala": ("/*", "*/"), "kotlin": ("/*", "*/"), "c#": ("/*", "*/"), "sql": ("/*", "*/"),
    "html": ("<!--", "-->"), "xml": ("<!--", "-->"),
}


def _empty_lang_stats() -> Dict[str, int]:
    return {
        "source_files": 0, "config_files": 0, "total_files": 0,
        "code_lines": 0, "comment_lines": 0, "blank_lines": 0, "total_lines": 0,
    }


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
        """Resets all tracked metrics."""
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
        """Counts total lines, blank lines, comment lines, and SLOC in a file."""
        total = blank = comment = 0

        ext = (ext or (os.path.splitext(filepath)[1].lower() if filepath else "")).lower()
        lang_lower = (language or "").lower().strip()

        single_prefixes = list(SINGLE_LINE_COMMENTS.get(lang_lower, []))
        block_delim = BLOCK_COMMENTS.get(lang_lower)

        # Fallback to extension lookup if language did not specify delimiters
        if not single_prefixes:
            if ext in (".py", ".pyi", ".rb", ".sh", ".bash", ".yaml", ".yml", ".tf", ".conf", ".ini", ".cfg", ".toml"):
                single_prefixes = ["#"]
            elif ext in (".java", ".kt", ".js", ".ts", ".c", ".cpp", ".cc", ".h", ".hpp", ".go", ".rs", ".cs"):
                single_prefixes = ["//"]
            elif ext == ".sql":
                single_prefixes = ["--"]

        if not block_delim:
            if ext in (".java", ".kt", ".js", ".ts", ".c", ".cpp", ".cc", ".h", ".hpp", ".cs", ".sql"):
                block_delim = ("/*", "*/")
            elif ext in (".html", ".xml", ".htm", ".xhtml"):
                block_delim = ("<!--", "-->")

        # Consult code_utils mappings if present
        try:
            from utils import code_utils
            delims = code_utils.get_comment_delimiters_for_language(lang_lower) if lang_lower else None
            if not delims and ext:
                delims = code_utils.get_comment_delimiters_for_file_extension(ext)
            if delims and len(delims) >= 2 and delims[0]:
                if delims[1]:
                    block_delim = (delims[0], delims[1])
                elif delims[0] not in single_prefixes:
                    single_prefixes.append(delims[0])
        except Exception:
            pass

        in_block = False
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    total += 1
                    s = line.strip()
                    if not s:
                        blank += 1
                        continue

                    if in_block:
                        comment += 1
                        if block_delim and block_delim[1] in s:
                            in_block = False
                        continue

                    if block_delim and s.startswith(block_delim[0]):
                        comment += 1
                        if block_delim[1] not in s[len(block_delim[0]):]:
                            in_block = True
                        continue

                    if any(s.startswith(p) for p in single_prefixes):
                        comment += 1
                        continue
        except Exception as e:
            logging.debug("Error reading file %s for line count: %s", filepath, e)
            return {"total": 0, "blank": 0, "comment": 0, "code": 0}

        return {
            "total": total,
            "blank": blank,
            "comment": comment,
            "code": max(0, total - blank - comment),
        }

    def _resolve_language(
        self,
        base_fname: str,
        ext: str,
        rel_path: str,
        filter_langs: Optional[Set[str]],
        code_utils_mod: Any,
    ) -> Optional[Tuple[str, bool]]:
        """Determines the language and whether the file is a configuration/build file."""
        fname_lower = base_fname.lower()

        # Build / manifest files have explicit language mappings
        if fname_lower in BUILD_FILE_LANGUAGE:
            b_lang = BUILD_FILE_LANGUAGE[fname_lower]
            if not filter_langs or b_lang in filter_langs:
                return b_lang, True

        # When filtering by specific languages, match against their code and config extensions
        if filter_langs:
            for fl in filter_langs:
                try:
                    if ext in set(code_utils_mod.get_file_extensions_for_language(fl)):
                        return fl, False
                except Exception:
                    pass

            rel_p_lower = rel_path.lower()
            for fl in filter_langs:
                try:
                    if fl in rel_p_lower and (ext in set(code_utils_mod.get_config_file_extensions_for_language(fl)) or fname_lower in BUILD_FILE_LANGUAGE):
                        return fl, True
                except Exception:
                    pass

            for fl in filter_langs:
                try:
                    if ext in set(code_utils_mod.get_config_file_extensions_for_language(fl)):
                        return fl, True
                except Exception:
                    pass

            if "other" in filter_langs:
                return "other", (fname_lower in BUILD_FILE_LANGUAGE or ext in CONFIG_EXTENSIONS)

            return None

        # Unfiltered classification
        lang = EXT_TO_LANGUAGE.get(ext)
        if not lang:
            lang = "text" if ext in (".txt", ".log", ".csv", ".tsv") else "other"

        is_config = fname_lower in BUILD_FILE_LANGUAGE or ext in CONFIG_EXTENSIONS
        return lang, is_config

    def _recalculate_totals(self):
        self.total_repo_files = sum(l["total_files"] for l in self.languages.values())
        self.total_repo_lines = sum(l["total_lines"] for l in self.languages.values())

    def measure_repository(
        self,
        source_path: str,
        max_file_size: int = 200_000,
        languages: Optional[List[str]] = None,
    ):
        """Scans the repository directory and aggregates file counts and line metrics."""
        if not source_path or not os.path.exists(source_path):
            logging.warning("CodeMetricsTracker: Source path '%s' does not exist.", source_path)
            return

        from utils import code_utils

        filter_languages = {l.lower() for l in languages} if languages else None
        if filter_languages:
            for fl in filter_languages:
                self.languages.setdefault(fl, _empty_lang_stats())

        for root, dirs, files in os.walk(source_path):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS and not d.startswith(".")]

            for fname in files:
                if fname.startswith(".") and fname not in (".env.example", ".gitignore"):
                    continue
                if fname.endswith("_metadata.txt") or fname in ("metadata.json", "code-metadata.json"):
                    continue

                # Strip .txt wrapper generated for GraphRAG indexing
                base_fname = fname[:-4] if (fname.endswith(".txt") and "." in fname[:-4]) else fname
                ext = os.path.splitext(base_fname)[1].lower()

                if ext in BINARY_EXTENSIONS or base_fname.lower().endswith((".min.js", ".min.css")):
                    continue

                fpath = os.path.join(root, fname)
                rel_path = os.path.relpath(fpath, source_path)

                resolved = self._resolve_language(base_fname, ext, rel_path, filter_languages, code_utils)
                if not resolved:
                    continue

                lang, is_config = resolved
                scan_key = f"{lang}:{rel_path}"
                if scan_key in self._scanned_files:
                    continue
                self._scanned_files.add(scan_key)

                try:
                    if os.path.getsize(fpath) > max_file_size:
                        if rel_path not in self.skipped_large_files:
                            self.skipped_large_files.append(rel_path)
                        continue
                except Exception:
                    continue

                counts = self.count_file_lines(fpath, language=lang, ext=ext)
                rec = self.languages.setdefault(lang, _empty_lang_stats())

                if is_config:
                    rec["config_files"] += 1
                else:
                    rec["source_files"] += 1

                rec["total_files"] += 1
                rec["code_lines"] += counts["code"]
                rec["comment_lines"] += counts["comment"]
                rec["blank_lines"] += counts["blank"]
                rec["total_lines"] += counts["total"]

        self._recalculate_totals()

    def measure_dataframe(self, code_df, language: str, config: bool = False):
        """Aggregates metrics directly from a DataFrame row iterable."""
        if code_df is None or len(code_df) == 0:
            return

        rec = self.languages.setdefault(language, _empty_lang_stats())

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

        self._recalculate_totals()

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
        """Reconstructs an instance from a dictionary."""
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
        """Serializes and writes code metrics to a JSON file."""
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load_from_file(cls, filepath: str) -> Optional["CodeMetricsTracker"]:
        """Loads metrics from a JSON file, returning None if missing or corrupt."""
        if not filepath or not os.path.exists(filepath):
            return None
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return cls.from_dict(json.load(f))
        except Exception as e:
            logging.debug("Failed loading code metrics from %s: %s", filepath, e)
            return None

    def load_and_merge(self, filepath: str, current_stage: Optional[str] = None):
        """Loads external metrics and merges max counts into this tracker."""
        if not filepath or not os.path.exists(filepath):
            return

        norm_path = os.path.normpath(os.path.abspath(filepath))
        if norm_path in self._merged_files:
            return

        other = self.load_from_file(filepath)
        if not other:
            return

        self.git_slug = self.git_slug or other.git_slug
        self.git_repo = self.git_repo or other.git_repo
        self.run_id = self.run_id or other.run_id

        for lang, rec in other.languages.items():
            if lang not in self.languages:
                self.languages[lang] = dict(rec)
            else:
                for k, v in rec.items():
                    self.languages[lang][k] = max(self.languages[lang].get(k, 0), v)

        self._recalculate_totals()

        for sf in other.skipped_large_files:
            if sf not in self.skipped_large_files:
                self.skipped_large_files.append(sf)

        self._merged_files.add(norm_path)

    def log_to_mlflow(self, run_id: Optional[str] = None):
        """Logs file counts and line metrics to MLflow."""
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
                prefix = f"project_{re.sub(r'[^a-zA-Z0-9_]', '_', lang.lower())}"
                metrics[f"{prefix}_total_files"] = float(rec.get("total_files", 0))
                metrics[f"{prefix}_source_files"] = float(rec.get("source_files", 0))
                metrics[f"{prefix}_config_files"] = float(rec.get("config_files", 0))
                metrics[f"{prefix}_code_lines"] = float(rec.get("code_lines", 0))
                metrics[f"{prefix}_total_lines"] = float(rec.get("total_lines", 0))

            if mlflow.active_run():
                mlflow.log_metrics(metrics)
            elif run_id:
                with mlflow.start_run(run_id=run_id):
                    mlflow.log_metrics(metrics)
        except Exception as e:
            logging.debug("Failed logging code metrics to MLflow: %s", e)

    def upload_to_mlflow(
        self,
        git_slug: Optional[str] = None,
        stage: Optional[str] = None,
        multi_repo: bool = False,
        run_id: Optional[str] = None,
    ):
        """Uploads project_metrics.json as an MLflow artifact and via DefaultAssetLoader."""
        slug = git_slug or self.git_slug
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_file = os.path.join(tmp_dir, "project_metrics.json")
            self.save_to_file(tmp_file)

            # Direct MLflow artifact logging
            try:
                import mlflow
                active = mlflow.active_run()
                target_run = (
                    run_id
                    or (active.info.run_id if active and hasattr(active, "info") else None)
                    or os.environ.get("MLFLOW_RUN_ID")
                )
                if target_run:
                    tags = {
                        "category": "telemetry",
                        "type": "code_metrics",
                        "git_slug": str(slug or "multi-repo"),
                    }
                    if stage:
                        tags["stage"] = str(stage)

                    if active and hasattr(active, "info") and active.info.run_id == target_run:
                        mlflow.set_tags(tags)
                        mlflow.log_artifact(tmp_file, artifact_path="telemetry")
                    else:
                        with mlflow.start_run(run_id=target_run, nested=bool(active)):
                            mlflow.set_tags(tags)
                            mlflow.log_artifact(tmp_file, artifact_path="telemetry")
            except Exception as e:
                logging.debug("Direct MLflow log_artifact failed for project_metrics.json: %s", e)

            # Asset loader artifact upload
            try:
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
                logging.debug("Asset loader upload failed for project_metrics.json: %s", e)

    def download_from_mlflow(
        self,
        git_slug: Optional[str] = None,
        multi_repo: bool = False,
        current_stage: Optional[str] = None,
        run_id: Optional[str] = None,
    ):
        """Downloads and merges project_metrics.json from MLflow or DefaultAssetLoader."""
        slug = git_slug or self.git_slug
        target_run = run_id or os.environ.get("MLFLOW_RUN_ID")

        if not target_run:
            try:
                import mlflow
                active = mlflow.active_run()
                if active and hasattr(active, "info"):
                    target_run = getattr(active.info, "run_id", None)
            except Exception:
                pass

        if target_run and target_run not in self._merged_runs:
            try:
                import mlflow
                local_path = mlflow.artifacts.download_artifacts(
                    run_id=target_run, artifact_path="telemetry/project_metrics.json"
                )
                cand = os.path.join(local_path, "project_metrics.json") if os.path.isdir(local_path or "") else local_path
                if cand and os.path.isfile(cand):
                    self.load_and_merge(cand, current_stage=current_stage)
                    self._merged_runs.add(target_run)
                    return
            except Exception as e:
                logging.debug("Failed downloading project_metrics.json from MLflow for run %s: %s", target_run, e)

        if slug or multi_repo:
            try:
                from loaders.default_asset_loader import DefaultAssetLoader
                artifact_path = DefaultAssetLoader.get_log_results_artifact_path(
                    DefaultAssetLoader.RESULTS_PATH_PREFIX_TELEMETRY,
                    git_slug=slug,
                    multi_repo=multi_repo,
                )
                with tempfile.TemporaryDirectory() as tmp_dir:
                    if DefaultAssetLoader().download_dir(artifact_path, tmp_dir):
                        cand = os.path.join(tmp_dir, "project_metrics.json")
                        if os.path.isfile(cand):
                            self.load_and_merge(cand, current_stage=current_stage)
            except Exception as e:
                logging.debug("Asset loader download failed for project_metrics.json: %s", e)

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
            if self.total_repo_files > 0:
                return (
                    "\n### Project Codebase & Scope Summary\n\n"
                    f"> **Repository Scope**: **{self.total_repo_files:,}** file(s) analyzed with "
                    f"**{self.total_repo_lines:,}** total lines.\n\n"
                    "| Metric | Count |\n"
                    "| :--- | :---: |\n"
                    f"| **Total Files** | {self.total_repo_files:,} |\n"
                    f"| **Total Lines** | {self.total_repo_lines:,} |\n"
                )
            return (
                "\n### Project Codebase & Scope Summary\n\n"
                "> **Repository Scope**: **0** file(s) analyzed across **0** detected language(s) with **0** total lines.\n\n"
                "| Language | Source Files | Config Files | Total Files | Code Lines (SLOC) | Comment Lines | Blank Lines |\n"
                "| :--- | :---: | :---: | :---: | :---: | :---: | :---: |\n"
                "| *(None detected)* | 0 | 0 | 0 | 0 | 0 | 0 |\n"
            )

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
    candidate_dirs: Union[str, List[str]],
    tracker: Optional[CodeMetricsTracker] = None,
) -> CodeMetricsTracker:
    """Finds project_metrics.json in candidate directories or reconstructs metrics from sources."""
    if tracker is None:
        tracker = CodeMetricsTracker.get_instance()

    from utils.duration_tracker import find_all_telemetry_files

    dirs = [candidate_dirs] if isinstance(candidate_dirs, str) else list(candidate_dirs)

    for fpath in find_all_telemetry_files(dirs, "project_metrics.json"):
        tracker.load_and_merge(fpath)

    if tracker.languages and tracker.total_repo_files > 0:
        return tracker

    # Fallback: scan candidate input or root directories for files
    for cand in dirs:
        if not cand or not os.path.exists(cand):
            continue
        for check_dir in [os.path.join(cand, "input"), cand]:
            if os.path.isdir(check_dir):
                tracker.measure_repository(check_dir)
                if tracker.total_repo_files > 0:
                    return tracker

    # Fallback: inspect documents.parquet if available
    for cand in dirs:
        if not cand or not os.path.exists(cand):
            continue
        parquet_path = os.path.join(cand, "output", "documents.parquet") if os.path.isdir(cand) else cand
        if not os.path.isfile(parquet_path):
            parquet_path = os.path.join(cand, "documents.parquet")
        if os.path.isfile(parquet_path):
            try:
                import pandas as pd
                df = pd.read_parquet(parquet_path)
                if df is not None and not df.empty:
                    tracker.measure_dataframe(df, language="text")
                    if tracker.total_repo_files > 0:
                        return tracker
            except Exception as e:
                logging.debug("Fallback documents.parquet reading failed: %s", e)

    return tracker
