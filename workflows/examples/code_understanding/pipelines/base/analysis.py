import logging
import os
import re
import shutil
import sys
import tempfile
import threading
from collections import OrderedDict
from contextlib import contextmanager, nullcontext
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

from utils.kubeflow_utils import setup_logging
setup_logging()

from utils.otel_utils import enable_telemetry
from utils.request_timing import timed, timing_span

# shortcut: caches 2 index dirs on local pod disk, move to shared PVC if scaling >1 replica
_INDEX_CACHE_MAX = 2
_INDEX_CACHE: OrderedDict[tuple, dict] = OrderedDict()
_INDEX_CACHE_LOCK = threading.Lock()


def _resolve_mlflow_index_run_id(
    *,
    git_slug: str,
    use_multi_repo: bool,
    clean_project_slug: str,
    pipeline_run_id: str,
    namespace: str | None,
    workspace: str | None,
) -> str | None:
    """Return the active MLflow indexing run_id when ASSET_LOADER=mlflow, else None."""
    if os.environ.get("ASSET_LOADER", "local") != "mlflow":
        return None
    try:
        from loaders.default_asset_loader import DefaultAssetLoader
        from loaders.mlflow_asset_loader import MlFlowAssetLoader, _ensure_mlflow_imported
        from utils.pipeline_utils import escape_mlflow_filter_value

        loader = DefaultAssetLoader(namespace=namespace, workspace=workspace)
        if not isinstance(loader._loader, MlFlowAssetLoader):
            return None
        _ensure_mlflow_imported()
        from mlflow.tracking import MlflowClient

        client = MlflowClient()
        exp = client.get_experiment_by_name(loader._loader.RESULT_DIRECTORY_ASSET_EXPERIMENT)
        if not exp or exp.lifecycle_stage != "active":
            return None

        effective_slug = (clean_project_slug or git_slug or None) if use_multi_repo else git_slug
        base_tags: dict[str, object] = {"multi_repo": use_multi_repo, "category": "indexing"}
        candidate_tags: list[dict[str, object]] = []
        if effective_slug:
            slug_tags = {**base_tags, "git_slug": effective_slug}
            if pipeline_run_id:
                candidate_tags.append({**slug_tags, "pipeline_run_id": pipeline_run_id})
            candidate_tags.append(slug_tags)
        if use_multi_repo:
            if pipeline_run_id:
                candidate_tags.append({**base_tags, "pipeline_run_id": pipeline_run_id})
            candidate_tags.append(base_tags)

        for cand in candidate_tags:
            flt = " AND ".join(
                f"tags.\"{k}\" = '{escape_mlflow_filter_value(v)}'"
                for k, v in cand.items()
                if v is not None and v != ""
            )
            runs = client.search_runs(
                experiment_ids=[exp.experiment_id],
                filter_string=flt,
                order_by=["start_time DESC"],
                max_results=1,
            )
            if runs:
                rid = getattr(getattr(runs[0], "info", None), "run_id", None)
                if isinstance(rid, str) and rid:
                    return rid
    except Exception:
        logging.debug("Could not resolve MLflow index run_id for cache", exc_info=True)
    return None


def _evict_idle_index_dirs() -> None:
    for k in list(_INDEX_CACHE.keys()):
        if len(_INDEX_CACHE) <= _INDEX_CACHE_MAX:
            break
        if _INDEX_CACHE[k]["refs"] == 0:
            old = _INDEX_CACHE.pop(k)
            shutil.rmtree(old["path"], ignore_errors=True)


@contextmanager
def _acquire_query_index_dir(
    *,
    index_run_id: str | None,
    download_fn,
    cache_scope: tuple,
):
    if not index_run_id:
        with tempfile.TemporaryDirectory(prefix="augur_query_", ignore_cleanup_errors=True) as tmp_dir:
            download_fn(tmp_dir)
            yield tmp_dir
        return

    cache_key = (*cache_scope, index_run_id)
    with _INDEX_CACHE_LOCK:
        entry = _INDEX_CACHE.get(cache_key)
        if entry is None:
            entry = {
                "path": tempfile.mkdtemp(prefix="augur_idx_"),
                "lock": threading.Lock(),
                "refs": 0,
                "ready": False,
            }
            _INDEX_CACHE[cache_key] = entry
        _INDEX_CACHE.move_to_end(cache_key)
        entry["refs"] += 1
        _evict_idle_index_dirs()

    try:
        if not entry["ready"]:
            with entry["lock"]:
                if not entry["ready"]:
                    download_fn(entry["path"])
                    entry["ready"] = True
        yield entry["path"]
    finally:
        with _INDEX_CACHE_LOCK:
            entry["refs"] -= 1
            if not entry["ready"] and entry["refs"] == 0:
                _INDEX_CACHE.pop(cache_key, None)
                shutil.rmtree(entry["path"], ignore_errors=True)
            _evict_idle_index_dirs()


def _inject_section(report: str, section_md: str) -> str:
    """Injects a markdown section before the Code Migration Plan or at the end."""
    if not section_md or not section_md.strip():
        return report
    match = re.search(r'(#+\s*Code\s+Migration\s+Plan\s*\(?JSON\)?)', report, re.IGNORECASE)
    if match:
        idx = match.start()
        return report[:idx] + section_md.strip() + "\n\n" + report[idx:]
    return f"{report.rstrip()}\n\n{section_md.strip()}\n"


##############################################################################
# Pipeline stage
##############################################################################

class AnalysisPipeline:

    @enable_telemetry
    def run(self, graphrag_source_path: str, git_repo: str = "", git_branch: str = "",
            multi_repo: bool = False, pipeline_run_id: str = "", project_slug: str = ""):
        """Generates a migration report from the GraphRAG index and returns the result."""
        import asyncio
        from loaders.default_asset_loader import DefaultAssetLoader
        from utils.graphrag_utils import DependencyAnalyzer
        from pipelines.base.data_generation import generate_git_slug
        from utils.loader_utils import download_graphrag_output_directory
        from utils.pipeline_utils import generate_multi_repo_slug, sanitize_slug

        git_repo = git_repo or os.getenv("GIT_REPO", "")
        git_branch = git_branch or os.getenv("GIT_BRANCH", "main")
        clean_project_slug = sanitize_slug(project_slug)
        if multi_repo and not clean_project_slug:
            try:
                repos = DefaultAssetLoader().load_run_repos(run_id=pipeline_run_id or None)
                if repos:
                    clean_project_slug = generate_multi_repo_slug(repos)
            except Exception:
                pass
        git_slug = (
            clean_project_slug
            if (multi_repo and clean_project_slug)
            else (generate_git_slug(git_repo, git_branch) if git_repo else None)
        )

        if pipeline_run_id:
            download_graphrag_output_directory(
                graphrag_source_path,
                pipeline_run_id=pipeline_run_id,
                git_repo=git_repo,
                git_branch=git_branch,
                multi_repo=multi_repo,
                project_slug=git_slug or "",
            )

        from utils.duration_tracker import find_all_telemetry_files

        candidate_dirs = [
            graphrag_source_path,
            os.path.join(graphrag_source_path, "output"),
            os.path.join(graphrag_source_path, "input"),
            os.path.dirname(graphrag_source_path),
            os.getenv("PARENT_TARGET_PATH", "target"),
            os.getenv("PARENT_SOURCE_PATH", "source"),
        ]
        if git_slug:
            candidate_dirs.extend([
                os.path.join(os.getenv("PARENT_TARGET_PATH", "target"), git_slug),
                os.path.join(os.getenv("PARENT_SOURCE_PATH", "source"), git_slug),
                os.path.join(os.path.dirname(graphrag_source_path), git_slug),
            ])

        dur_tracker = None
        try:
            from utils.duration_tracker import DurationTracker, extract_benchmark_duration
            dur_tracker = DurationTracker.get_instance()
            for dur_file in find_all_telemetry_files(candidate_dirs, "durations.json"):
                dur_tracker.load_and_merge(dur_file, current_stage="Analysis")
                if not git_slug and dur_tracker.git_slug:
                    git_slug = dur_tracker.git_slug
                if not git_repo and dur_tracker.git_repo:
                    git_repo = dur_tracker.git_repo
            try:
                dur_tracker.download_from_mlflow(git_slug=git_slug, multi_repo=multi_repo, current_stage="Analysis")
            except Exception as e:
                logging.debug(f"Failed to download durations from MLflow in analysis: {e}")
            extract_benchmark_duration(candidate_dirs, dur_tracker)
            try:
                dur_tracker.record_pod_creation_overhead(stage="Analysis")
            except Exception as e:
                logging.debug(f"Failed to record pod creation overhead in analysis: {e}")
        except Exception as e:
            logging.debug(f"DurationTracker handling in analysis: {e}")

        analyzer = DependencyAnalyzer(graphrag_source_path, git_slug=git_slug or "", multi_repo=multi_repo)

        try:
            if hasattr(analyzer, "token_tracker") and analyzer.token_tracker:
                for tokens_file in find_all_telemetry_files(candidate_dirs, "tokens.json"):
                    analyzer.token_tracker.load_and_merge(tokens_file, current_stage="Analysis")
                    if not git_slug and analyzer.token_tracker.git_slug:
                        git_slug = analyzer.token_tracker.git_slug
                        analyzer.git_slug = git_slug
                    if not git_repo and analyzer.token_tracker.git_repo:
                        git_repo = analyzer.token_tracker.git_repo
                try:
                    analyzer.token_tracker.download_from_mlflow(
                        git_slug=git_slug,
                        multi_repo=multi_repo,
                        current_stage="Analysis",
                        only_current_run=False,
                    )
                except Exception as e:
                    logging.debug(f"Failed to download tokens from MLflow in analysis: {e}")
        except Exception as e:
            logging.debug(f"TokenCostTracker handling in analysis: {e}")

        code_tracker = None
        try:
            from utils.code_metrics_tracker import CodeMetricsTracker, extract_data_generation_code_metrics
            code_tracker = CodeMetricsTracker.get_instance(git_slug=git_slug, git_repo=git_repo)
            extract_data_generation_code_metrics(candidate_dirs, code_tracker)
            git_slug = git_slug or code_tracker.git_slug
            git_repo = git_repo or code_tracker.git_repo
            code_tracker.download_from_mlflow(git_slug=git_slug, multi_repo=multi_repo, current_stage="Analysis")
        except Exception as e:
            logging.debug(f"CodeMetricsTracker handling in analysis: {e}")

        report = asyncio.run(analyzer.generate_migration_report())

        # Safeguard: ensure telemetry summaries are present in the final report
        if code_tracker and "### Project Codebase & Scope Summary" not in report:
            report = _inject_section(report, code_tracker.format_markdown_section())

        if analyzer.token_tracker and "### LLM Token Usage & Cost Summary" not in report:
            report = _inject_section(report, analyzer.token_tracker.format_markdown_section())

        if dur_tracker and "### Pipeline Execution Duration Summary" not in report:
            report = _inject_section(report, dur_tracker.format_markdown_section())
        elif dur_tracker and "### GuideLLM Concurrency Benchmark Summary" not in report:
            bench_md = dur_tracker.format_benchmark_markdown_section()
            if bench_md:
                report = _inject_section(report, bench_md)

        try:
            analyzer.token_tracker.upload_to_mlflow(git_slug=git_slug, stage="Analysis", multi_repo=multi_repo)
        except Exception as e:
            logging.debug(f"Failed to upload token metrics to MLflow: {e}")

        if dur_tracker:
            try:
                dur_tracker.upload_to_mlflow(git_slug=git_slug, stage="Analysis", multi_repo=multi_repo)
            except Exception as e:
                logging.debug(f"Failed to upload duration metrics to MLflow: {e}")

        if code_tracker:
            try:
                code_tracker.log_to_mlflow()
                code_tracker.upload_to_mlflow(git_slug=git_slug, stage="Analysis", multi_repo=multi_repo)
                summary = code_tracker.format_summary()
                logging.info("\n" + summary)
            except Exception as e:
                logging.debug(f"Failed to upload/log code metrics in analysis: {e}")

        result_file = f"migration_report_{git_slug}.md" if git_slug else "migration_report.md"

        DefaultAssetLoader().log_results(
            result_file,
            artifact_path=DefaultAssetLoader.get_log_results_artifact_path(
                DefaultAssetLoader.RESULTS_PATH_PREFIX_PIPELINES,
                git_slug=git_slug,
                multi_repo=multi_repo,
            ),
            content=report,
            tags={"git_slug": str(git_slug or "multi-repo"), "multi_repo": str(multi_repo), "category": "analysis"},
        )

        return report

    def run_multi_repo(self, pipeline_run_id: str = "", project_slug: str = ""):
        """Runs migration report generation across the combined multi-repo GraphRAG index."""
        import os
        from loaders.default_asset_loader import DefaultAssetLoader
        from utils.loader_utils import download_result_directory
        from utils.pipeline_utils import generate_multi_repo_slug

        git_repos = DefaultAssetLoader().load_run_repos(run_id=pipeline_run_id or None)
        resolved_slug = (
            generate_multi_repo_slug(git_repos, project_slug=project_slug)
            if (git_repos or project_slug)
            else ""
        )

        base_source = os.getenv("KFP_DATA_INDEXING_OUTPUT_PATH", "graph_rag_app/source")
        graphrag_source_path = (
            os.path.join(base_source, resolved_slug)
            if resolved_slug and resolved_slug != "multi-repo"
            else base_source
        )

        if not pipeline_run_id:
            logging.info("Downloading multi-repo GraphRAG index...")
            output_dir = os.path.join(graphrag_source_path, "output")
            os.makedirs(output_dir, exist_ok=True)
            download_result_directory(
                git_slug=resolved_slug or None,
                download_dir=output_dir,
                results_prefix=DefaultAssetLoader.RESULTS_PATH_PREFIX_REPO_DATASETS,
                multi_repo=True,
                asset_tags={"multi_repo": True, "category": "indexing"},
            )

        return self.run(
            graphrag_source_path=graphrag_source_path,
            multi_repo=True,
            pipeline_run_id=pipeline_run_id,
            project_slug=resolved_slug,
        )

    @timed("analysis.adhoc_invocation")
    @enable_telemetry
    def run_adhoc_query(
        self,
        question: str,
        retry_count: int = 3,
        use_global: bool = True,
        git_repo: str = "",
        git_branch: str = "main",
        multi_repo: bool = False,
        namespace: str | None = None,
        workspace: str | None = None,
        pipeline_run_id: str = "",
        project_slug: str = "",
    ):
        """Queries the GraphRAG index with an LLM and returns the result."""
        import asyncio
        import tempfile
        from datetime import datetime
        from loaders.default_asset_loader import DefaultAssetLoader
        from utils.graphrag_utils import DependencyAnalyzer
        from pipelines.base.data_generation import generate_git_slug
        from utils.pipeline_utils import sanitize_slug

        use_multi_repo = multi_repo or not git_repo
        clean_project_slug = sanitize_slug(project_slug)
        git_slug = (
            clean_project_slug
            if (use_multi_repo and clean_project_slug)
            else (generate_git_slug(git_repo, git_branch) if git_repo else clean_project_slug)
        )

        adhoc_results_header = "\n---\n\n# ##################ADHOC RESULTS##################\n"

        index_run_id = _resolve_mlflow_index_run_id(
            git_slug=git_slug,
            use_multi_repo=use_multi_repo,
            clean_project_slug=clean_project_slug,
            pipeline_run_id=pipeline_run_id,
            namespace=namespace,
            workspace=workspace,
        )

        def _download_index(target_dir: str) -> None:
            with timing_span("analysis.index_download", "io"):
                DependencyAnalyzer.download_graphrag_directory(
                    download_dir=target_dir,
                    git_slug=git_slug,
                    multi_repo=use_multi_repo,
                    git_repo=git_repo,
                    namespace=namespace,
                    workspace=workspace,
                    pipeline_run_id=pipeline_run_id,
                    project_slug=clean_project_slug,
                )

        try:
            index_ctx = _acquire_query_index_dir(
                index_run_id=index_run_id,
                download_fn=_download_index,
                cache_scope=(namespace or "", workspace or "", git_slug or "", bool(use_multi_repo)),
            )
            graphrag_source_path = index_ctx.__enter__()
        except Exception:
            msg = (
                "Could not perform query: "
                + ("no multi-repository index was found" if use_multi_repo else
                   f"no index was found (git_repo='{git_repo}')")
                + ". Maybe you need to generate it first?"
            )
            logging.error(msg)
            print(adhoc_results_header, flush=True)
            return msg

        try:
            try:
                from utils.token_tracker import TokenCostTracker
                req_token_tracker = TokenCostTracker(git_slug=git_slug, git_repo=git_repo)
            except Exception:
                req_token_tracker = None

            with timing_span("analysis.analyzer_setup", "io"):
                try:
                    analyzer = DependencyAnalyzer(
                        graphrag_source_path,
                        git_slug=git_slug,
                        multi_repo=use_multi_repo,
                        token_tracker=req_token_tracker,
                    )
                except TypeError:
                    analyzer = DependencyAnalyzer(
                        graphrag_source_path,
                        git_slug=git_slug,
                        multi_repo=use_multi_repo,
                    )

            postamble = "Provide as much detail as possible. Include the git repo url(s) in the report."

            if use_multi_repo:
                postamble += (" Include ALL the git repo urls that you can find."
                              " Group git repositories by their git repo url.")

            try:
                from utils.duration_tracker import DurationTracker
                dur_tracker = DurationTracker(git_slug=git_slug, git_repo=git_repo)
            except Exception:
                dur_tracker = None

            cm = dur_tracker.measure(stage="Analysis", step="Adhoc Query") if dur_tracker else nullcontext()
            with cm:
                result = asyncio.run(analyzer.query_with_llm(
                    question + postamble,
                    retry_count=retry_count,
                    use_global=use_global,
                    response_type="Multiple Paragraphs, plain text, no markdown formatting",
                ))

            try:
                analyzer.token_tracker.log_to_mlflow()
            except Exception as e:
                logging.debug(f"Failed to log token metrics to MLflow: {e}")

            if dur_tracker:
                try:
                    dur_tracker.log_to_mlflow()
                except Exception as e:
                    logging.debug(f"Failed to log duration metrics to MLflow: {e}")

            timestamp = datetime.now().strftime("%Y%m%d%H%M%S_%f")
            with tempfile.TemporaryDirectory(prefix="augur_qres_", ignore_cleanup_errors=True) as res_dir:
                result_file = os.path.join(res_dir, f"adhoc_query_{timestamp}.txt")
                DefaultAssetLoader(namespace=namespace, workspace=workspace).log_results(
                    result_file,
                    artifact_path=DefaultAssetLoader.get_log_results_artifact_path(
                        DefaultAssetLoader.RESULTS_PATH_PREFIX_ADHOC_QUERIES,
                        git_slug=git_slug,
                        multi_repo=use_multi_repo,
                    ),
                    content=f"Question: {question}\n\nAnswer:\n{result}",
                    tags={"category": "analysis",
                          "adhoc_query": "true",
                          "git_slug": git_slug or "multi-repo",
                          "multi_repo": use_multi_repo},
                )

            print(adhoc_results_header, flush=True)
            return f"{result}\n\n"
        finally:
            index_ctx.__exit__(*sys.exc_info())


##############################################################################
# Helpers
##############################################################################

def write_migration_report(graphrag_source_path: str, report_path: str,
                           git_repo: str = "", git_branch: str = "",
                           multi_repo: bool = False,
                           pipeline_run_id: str = "",
                           project_slug: str = ""):
    """Run the migration report and write the result to report_path."""
    import os
    migration_report = AnalysisPipeline().run(
        graphrag_source_path,
        git_repo=git_repo,
        git_branch=git_branch,
        multi_repo=multi_repo,
        pipeline_run_id=pipeline_run_id,
        project_slug=project_slug,
    )
    if dirname := os.path.dirname(report_path):
        os.makedirs(dirname, exist_ok=True)
    with open(report_path, "w") as f:
        f.write(migration_report)


##############################################################################
# Module-level aliases for external callers (notebooks, scripts)
##############################################################################

def run_full_pipeline(*args, **kwargs):
    return AnalysisPipeline().run(*args, **kwargs)

def run_full_pipeline_multi_repo(*args, **kwargs):
    return AnalysisPipeline().run_multi_repo(*args, **kwargs)

def run_adhoc_query_pipeline(*args, **kwargs):
    return AnalysisPipeline().run_adhoc_query(*args, **kwargs)
