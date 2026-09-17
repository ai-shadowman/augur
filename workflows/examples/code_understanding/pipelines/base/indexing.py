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

    git_slug = generate_git_slug(git_repo, git_branch) if git_repo else None

    status = "fail"

    try:

        logging.info("Starting process...")

        os.makedirs(f"{graphrag_source_path}/input", exist_ok=True)

        os.makedirs(f"{graphrag_source_path}/output", exist_ok=True)

        try:
            from utils.duration_tracker import DurationTracker
            dur_tracker = DurationTracker.get_instance()
            for check_path in [codebase_path, os.path.dirname(codebase_path)]:
                dur_file = os.path.join(check_path, "durations.json")
                if os.path.exists(dur_file):
                    dur_tracker.load_and_merge(dur_file)
                    break
        except Exception:
            dur_tracker = None

        if dur_tracker:
            with dur_tracker.measure(stage="Indexing", step="Prepare Config and Inputs"):
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
            with dur_tracker.measure(stage="Indexing", step="Build GraphRAG Index"):
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

        try:
            from utils.token_tracker import TokenCostTracker
            TokenCostTracker.get_instance().log_to_mlflow()
        except Exception as e:
            logging.debug(f"Failed to log token metrics to MLflow: {e}")

        if dur_tracker:
            for save_dir in [graphrag_source_path, f"{graphrag_source_path}/output"]:
                try:
                    dur_tracker.save_to_file(os.path.join(save_dir, "durations.json"))
                except Exception as e:
                    logging.debug(f"Failed to save durations to {save_dir}: {e}")
            try:
                dur_tracker.log_to_mlflow()
            except Exception as e:
                logging.debug(f"Failed to log duration metrics to MLflow: {e}")

        artifact_path = DefaultAssetLoader.get_log_results_artifact_path(

            DefaultAssetLoader.RESULTS_PATH_PREFIX_REPO_DATASETS,

            git_slug=git_slug,

            multi_repo=multi_repo,

        )

        DefaultAssetLoader().log_results(f"{graphrag_source_path}/output",
                                         artifact_path=artifact_path,
                                         tags={"git_slug": git_slug,
                                               "category": "indexing",
                                               "multi_repo": multi_repo})

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
