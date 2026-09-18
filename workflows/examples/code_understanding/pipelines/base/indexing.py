import os
import json
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))


from utils.otel_utils import enable_telemetry


@enable_telemetry
def generate_graphrag_index(codebase_path: str, graphrag_source_path: str,
                            git_repo: str = "", git_branch: str = "", multi_repo: bool = False):
    """Generates a GraphRAG index from the provided codebase."""
    import lancedb, shutil, traceback, tracemalloc, nest_asyncio, logging
    from loaders.default_asset_loader import DefaultAssetLoader
    from pipelines.base.data_generation import generate_git_slug
    from utils.graphrag_utils import DependencyAnalyzer
    from pipelines.graphrag import run_graphrag

    tracemalloc.start()

    nest_asyncio.apply()

    logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

    git_repo = git_repo or os.getenv("GIT_REPO", "")
    git_branch = git_branch or os.getenv("GIT_BRANCH", "main")
    git_slug = generate_git_slug(git_repo, git_branch) if git_repo else None

    status = "fail"

    try:

        logging.info("Starting process...")

        os.makedirs(f"{graphrag_source_path}/input", exist_ok=True)

        os.makedirs(f"{graphrag_source_path}/output", exist_ok=True)

        from utils.duration_tracker import find_all_telemetry_files

        candidate_dirs = [
            codebase_path,
            os.path.join(codebase_path, "input"),
            os.path.join(codebase_path, "output"),
            os.path.dirname(codebase_path),
            graphrag_source_path,
            os.path.join(graphrag_source_path, "input"),
            os.path.join(graphrag_source_path, "output"),
            os.path.dirname(graphrag_source_path),
            os.getenv("PARENT_TARGET_PATH", "target"),
            os.getenv("PARENT_SOURCE_PATH", "source"),
        ]
        if git_slug:
            candidate_dirs.extend([
                os.path.join(os.getenv("PARENT_TARGET_PATH", "target"), git_slug),
                os.path.join(os.getenv("PARENT_SOURCE_PATH", "source"), git_slug),
                os.path.join(os.path.dirname(codebase_path), git_slug),
            ])

        dur_tracker = None
        try:
            from utils.duration_tracker import DurationTracker
            dur_tracker = DurationTracker.get_instance()
            for dur_file in find_all_telemetry_files(candidate_dirs, "durations.json"):
                dur_tracker.load_and_merge(dur_file, current_stage="Indexing")
                if not git_slug and dur_tracker.git_slug:
                    git_slug = dur_tracker.git_slug
                if not git_repo and dur_tracker.git_repo:
                    git_repo = dur_tracker.git_repo
            try:
                dur_tracker.download_from_mlflow(git_slug=git_slug, multi_repo=multi_repo, current_stage="Indexing")
            except Exception as e:
                logging.debug(f"Failed to download durations from MLflow in indexing: {e}")
        except Exception as e:
            logging.debug(f"DurationTracker handling in indexing: {e}")

        token_tracker = None
        try:
            from utils.token_tracker import TokenCostTracker
            token_tracker = TokenCostTracker.get_instance()
            token_tracker.enable_litellm_callbacks(category="GraphRAG Indexing")
            token_tracker.enable_openai_tracking(category="GraphRAG Indexing")
            for tokens_file in find_all_telemetry_files(candidate_dirs, "tokens.json"):
                token_tracker.load_and_merge(tokens_file, current_stage="Indexing")
                if not git_slug and token_tracker.git_slug:
                    git_slug = token_tracker.git_slug
                if not git_repo and token_tracker.git_repo:
                    git_repo = token_tracker.git_repo
            try:
                token_tracker.download_from_mlflow(git_slug=git_slug, multi_repo=multi_repo, current_stage="Indexing", only_current_run=False)
            except Exception as e:
                logging.debug(f"Failed to download tokens from MLflow in indexing: {e}")
        except Exception as e:
            logging.debug(f"TokenCostTracker handling in indexing: {e}")

        # Immediately preserve prior durations.json and tokens.json in graphrag_source_path
        if dur_tracker:
            for d in [graphrag_source_path, f"{graphrag_source_path}/output", f"{graphrag_source_path}/input"]:
                try:
                    dur_tracker.save_to_file(os.path.join(d, "durations.json"))
                except Exception:
                    pass
        if token_tracker:
            for d in [graphrag_source_path, f"{graphrag_source_path}/output", f"{graphrag_source_path}/input"]:
                try:
                    token_tracker.save_to_file(os.path.join(d, "tokens.json"))
                except Exception:
                    pass

        try:
            if dur_tracker:
                with dur_tracker.measure(stage="Indexing", step="Prepare Settings & Config"):
                    DependencyAnalyzer.prepare_settings(template_dir="templates", output_dir="templates")
                    from utils.prompt_utils import prepare_indexing_config
                    logging.info("Preparing GraphRAG config files...")
                    prepare_indexing_config(graphrag_source_path,
                                            git_slug=git_slug or "",
                                            git_repo=git_repo or "",
                                            multi_repo=multi_repo)
                with dur_tracker.measure(stage="Indexing", step="Copy Source to Input"):
                    logging.info("Copying source code to GraphRAG directory...")
                    shutil.copytree(codebase_path, f"{graphrag_source_path}/input", dirs_exist_ok=True)
                with dur_tracker.measure(stage="Indexing", step="GraphRAG Indexing Execution"):
                    logging.info(f"Running index for git_slug={git_slug}, multi_repo={multi_repo}...")
                    run_graphrag(graphrag_source_path)
            else:
                DependencyAnalyzer.prepare_settings(template_dir="templates", output_dir="templates")
                from utils.prompt_utils import prepare_indexing_config
                logging.info("Preparing GraphRAG config files...")
                prepare_indexing_config(graphrag_source_path,
                                        git_slug=git_slug or "",
                                        git_repo=git_repo or "",
                                        multi_repo=multi_repo)
                logging.info("Copying source code to GraphRAG directory...")
                shutil.copytree(codebase_path, f"{graphrag_source_path}/input", dirs_exist_ok=True)
                logging.info(f"Running index for git_slug={git_slug}, multi_repo={multi_repo}...")
                run_graphrag(graphrag_source_path)
        finally:
            # Extract indexing tokens from GraphRAG output files (stats.json, text_units.parquet)
            if token_tracker:
                try:
                    from utils.token_tracker import extract_graphrag_indexing_tokens
                    extract_graphrag_indexing_tokens(graphrag_source_path, token_tracker)
                except Exception as e:
                    logging.debug(f"Indexing token extraction: {e}")

            if token_tracker:
                for save_dir in [graphrag_source_path, f"{graphrag_source_path}/output", f"{graphrag_source_path}/input"]:
                    try:
                        token_tracker.save_to_file(os.path.join(save_dir, "tokens.json"))
                    except Exception:
                        pass
                try:
                    token_tracker.upload_to_mlflow(git_slug=git_slug, stage="Indexing", multi_repo=multi_repo)
                except Exception as e:
                    logging.debug(f"Failed to upload token metrics to MLflow: {e}")

            if dur_tracker:
                try:
                    from utils.duration_tracker import extract_graphrag_indexing_durations
                    extract_graphrag_indexing_durations(graphrag_source_path, dur_tracker)
                except Exception as e:
                    logging.debug(f"Failed to extract indexing durations: {e}")
                for save_dir in [graphrag_source_path, f"{graphrag_source_path}/output", f"{graphrag_source_path}/input"]:
                    try:
                        dur_tracker.save_to_file(os.path.join(save_dir, "durations.json"))
                    except Exception as e:
                        logging.debug(f"Failed to save durations to {save_dir}: {e}")
                try:
                    dur_tracker.upload_to_mlflow(git_slug=git_slug, stage="Indexing", multi_repo=multi_repo)
                except Exception as e:
                    logging.debug(f"Failed to upload duration metrics to MLflow: {e}")

            if dur_tracker:
                try:
                    summary = dur_tracker.format_summary()
                    logging.info("\n" + summary)
                    print("\n" + summary, flush=True)
                except Exception as e:
                    logging.debug(f"Failed to log duration summary: {e}")

            if token_tracker:
                try:
                    summary = token_tracker.format_summary()
                    logging.info("\n" + summary)
                    print("\n" + summary, flush=True)
                except Exception as e:
                    logging.debug(f"Failed to log token summary: {e}")

        artifact_path = DefaultAssetLoader.get_log_results_artifact_path(
            DefaultAssetLoader.RESULTS_PATH_PREFIX_REPO_DATASETS,
            git_slug=git_slug,
            multi_repo=multi_repo,
        )

        DefaultAssetLoader().log_results(f"{graphrag_source_path}/output",
                                         artifact_path=artifact_path,
                                         tags={"git_slug": str(git_slug or "multi-repo"),
                                               "category": "indexing",
                                               "multi_repo": str(multi_repo)})

        status = "success"

    except Exception as e:

        logging.error(f"Error processing GraphRAG DB: {e}")

        logging.error(traceback.format_exc())

        raise e

    finally:

        result = {"codebase_path": codebase_path, "graphrag_source_path": graphrag_source_path,
                  "status": status, "fail_message": "" if status == "success" else traceback.format_exc()}

        result_file = "indexing_result_multi_repo.json" if multi_repo else f"indexing_result_{git_slug}.json"

        DefaultAssetLoader().log_results(

            result_file,

            artifact_path=DefaultAssetLoader.get_log_results_artifact_path(

                DefaultAssetLoader.RESULTS_PATH_PREFIX_PIPELINES,

                git_slug=git_slug,

                multi_repo=multi_repo,

            ),

            content=json.dumps(result),

            tags={"git_slug": git_slug, "category": "indexing", "multi_repo": multi_repo},

        )


@enable_telemetry
def evaluate_graphrag_index(graphrag_source_path: str, git_repo: str, git_branch: str,
                            multi_repo: bool = False):
    """Evaluates a GraphRAG index using DefaultCustomEvaluator.evaluate_with_dataset."""
    import logging
    import os

    logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

    from eval.default_custom_evaluator import DefaultCustomEvaluator

    logging.info("Starting GraphRAG index evaluation...")

    try:
        try:
            from utils.duration_tracker import DurationTracker
            dur_tracker = DurationTracker.get_instance()
        except Exception:
            dur_tracker = None

        if dur_tracker:
            with dur_tracker.measure(stage="Indexing", step="Evaluate Index"):
                results = DefaultCustomEvaluator().evaluate_with_dataset(graphrag_source_path,
                                                                         git_repo, git_branch,
                                                                         multi_repo=multi_repo)
                try:
                    dur_tracker.log_to_mlflow()
                except Exception as e:
                    logging.debug(f"Failed to log duration metrics to MLflow: {e}")
        else:
            results = DefaultCustomEvaluator().evaluate_with_dataset(graphrag_source_path,
                                                                     git_repo, git_branch,
                                                                     multi_repo=multi_repo)

        logging.info("GraphRAG index evaluation complete.")

        return results

    except Exception as e:

        logging.warning(f"GraphRAG index evaluation failed: {e}")


##############################################################################
# Pipeline stage
##############################################################################

class IndexingPipeline:

    @enable_telemetry
    def run(self, codebase_path: str, graphrag_source_path: str, git_repo: str, git_branch: str,
            multi_repo: bool = False):
        """Generates a GraphRAG index and returns a status dict."""
        import traceback, logging
        import os

        logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

        try:

            generate_graphrag_index(codebase_path=codebase_path,
                                    graphrag_source_path=graphrag_source_path,
                                    git_repo=git_repo, git_branch=git_branch,
                                    multi_repo=multi_repo)

            logging.info("GraphRAG index generation complete.")

        except Exception as e:

            logging.error(f"Error processing Sample Codebase Index: {e}")

            error_message = traceback.format_exc()

            logging.error(error_message)

            return {"codebase_path": codebase_path, "graphrag_source_path": graphrag_source_path,
                    "status": "fail", "fail_message": error_message}

        if multi_repo:

            logging.info("*** No-op: skipping evaluation for multi-repo index. ***")

        else:

            try:

                evaluate_graphrag_index(graphrag_source_path=graphrag_source_path,
                                        git_repo=git_repo, git_branch=git_branch,
                                        multi_repo=False)

            except Exception as e:

                logging.warning(f"GraphRAG index evaluation failed: {e}")

        return {"codebase_path": codebase_path, "graphrag_source_path": graphrag_source_path,
                "status": "success", "fail_message": ""}

    def run_multi_repo(self, parent_target_path: str, graphrag_source_path: str = None):
        """Runs GraphRAG indexing and evaluation across the combined multi-repo codebase."""
        import os, logging
        from loaders.default_asset_loader import DefaultAssetLoader
        from utils.loader_utils import download_code_metadata_directories

        logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

        if graphrag_source_path is None:
            graphrag_source_path = os.getenv("KFP_DATA_INDEXING_OUTPUT_PATH", "graph_rag_app/source")

        #git_repos = DefaultAssetLoader().download("repos/repo_list.json") or []
        git_repos = json.loads(os.getenv("GIT_REPO_LIST_CONTENTS")) or []

        download_code_metadata_directories(git_repos, parent_target_path)

        result = self.run(
            codebase_path=parent_target_path,
            graphrag_source_path=graphrag_source_path,
            git_repo="",
            git_branch="",
            multi_repo=True,
        )

        if result.get("status") != "success":
            raise Exception(f"GraphRAG indexing failed: {result.get('fail_message', '')}")


##############################################################################
# Module-level aliases for external callers (notebooks)
##############################################################################

def run_full_pipeline(*args, **kwargs):
    return IndexingPipeline().run(*args, **kwargs)

def run_full_pipeline_multi_repo(*args, **kwargs):
    return IndexingPipeline().run_multi_repo(*args, **kwargs)
