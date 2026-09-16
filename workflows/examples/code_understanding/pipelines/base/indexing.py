import os
import json
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))


def generate_graphrag_index(codebase_path: str, graphrag_source_path: str,
                            git_repo: str = "", git_branch: str = "", multi_repo: bool = False):
    """Generates a GraphRAG index from the provided codebase."""
    import json, os, lancedb, shutil, traceback, subprocess, tracemalloc, nest_asyncio, logging
    from loaders.default_asset_loader import DefaultAssetLoader
    from pipelines.base.data_generation import generate_git_slug
    from utils.graphrag_utils import DependencyAnalyzer

    tracemalloc.start()

    nest_asyncio.apply()

    logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

    git_slug = generate_git_slug(git_repo, git_branch) if git_repo else None

    status = "fail"

    try:

        logging.info("Starting process...")

        graph_rag_config_path = f"{graphrag_source_path}/settings.yaml"

        os.makedirs(f"{graphrag_source_path}/input", exist_ok=True)

        os.makedirs(f"{graphrag_source_path}/output", exist_ok=True)

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

        graphrag_sh = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "graphrag.sh")

        proc = subprocess.run(
            ["bash", graphrag_sh, graphrag_source_path, graph_rag_config_path],
            check=False, capture_output=True, text=True
        )

        if proc.returncode != 0:
            raise Exception(
                f"GraphRAG indexing failed (exit {proc.returncode}):\n"
                f"STDOUT: {proc.stdout}\n"
                f"STDERR: {proc.stderr}"
            )

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


def evaluate_graphrag_index(graphrag_source_path: str, git_repo: str, git_branch: str,
                            multi_repo: bool = False):
    """Evaluates a GraphRAG index using DefaultCustomEvaluator.evaluate_with_dataset."""
    import logging
    import os

    logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

    from eval.default_custom_evaluator import DefaultCustomEvaluator

    logging.info("Starting GraphRAG index evaluation...")

    try:

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

    def run(self, codebase_path: str, graphrag_source_path: str, git_repo: str = "", git_branch: str = "",
            multi_repo: bool = False, metrics_tracker=None):
        """Generates a GraphRAG index and returns a status dict."""
        import traceback, logging
        import os
        from utils.metrics_tracker import PipelineMetricsTracker

        logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

        from pipelines.base.data_generation import generate_git_slug
        git_slug = generate_git_slug(git_repo, git_branch) if git_repo else None

        candidate_search_paths = [codebase_path, graphrag_source_path]
        if git_slug:
            candidate_search_paths.extend([
                os.path.join(codebase_path, git_slug),
                os.path.join(graphrag_source_path, git_slug),
            ])
        candidate_search_paths.extend(["target", "source", "graph_rag_app/source", "."])
        if git_slug:
            candidate_search_paths.extend([f"target/{git_slug}", f"source/{git_slug}", f"graph_rag_app/source/{git_slug}"])

        tracker = metrics_tracker
        own_tracker = False
        if tracker is None:
            default_name = "single-repo-pipeline" if not multi_repo else "multi-repo-pipeline"
            tracker = PipelineMetricsTracker.load_or_create(
                search_paths=candidate_search_paths,
                pipeline_name=default_name,
                multi_repo=multi_repo,
                git_repo=git_repo,
                git_branch=git_branch,
            )
            own_tracker = True
            tracker.start_stage("Indexing")
        else:
            prior_tracker = PipelineMetricsTracker.load_or_create(
                search_paths=candidate_search_paths,
                pipeline_name=tracker.pipeline_name,
                multi_repo=multi_repo,
                git_repo=git_repo,
                git_branch=git_branch,
            )
            if prior_tracker:
                tracker.merge(prior_tracker)
            tracker.start_stage("Indexing")

        try:

            with tracker.track_step("generate_graphrag_index", stage="Indexing"):
                generate_graphrag_index(codebase_path=codebase_path,
                                        graphrag_source_path=graphrag_source_path,
                                        git_repo=git_repo, git_branch=git_branch,
                                        multi_repo=multi_repo)

            logging.info("GraphRAG index generation complete.")

        except Exception as e:

            logging.error(f"Error processing Sample Codebase Index: {e}")

            error_message = traceback.format_exc()

            logging.error(error_message)

            if own_tracker:
                tracker.stop_stage("Indexing", status="FAILED", error_message=str(e))
                tracker.stop_pipeline(status="FAILED", error_message=str(e))
                tracker.log_summary()

            return {"codebase_path": codebase_path, "graphrag_source_path": graphrag_source_path,
                    "status": "fail", "fail_message": error_message,
                    "metrics": tracker.to_dict() if tracker else {}}

        if multi_repo:

            logging.info("*** No-op: skipping evaluation for multi-repo index. ***")

        else:

            try:

                with tracker.track_step("evaluate_graphrag_index", stage="Indexing"):
                    evaluate_graphrag_index(graphrag_source_path=graphrag_source_path,
                                            git_repo=git_repo, git_branch=git_branch,
                                            multi_repo=False)

            except Exception as e:

                logging.warning(f"GraphRAG index evaluation failed: {e}")

        if own_tracker:
            tracker.stop_stage("Indexing", status="COMPLETED")
            tracker.stop_pipeline(status="COMPLETED")
            tracker.log_summary()

        if tracker:
            if not own_tracker and tracker.stages.get("Indexing") and tracker.stages["Indexing"].status == "running":
                tracker.stop_stage("Indexing", status="COMPLETED")
            tracker.save_and_log(
                target_dir=graphrag_source_path,
                git_repo=git_repo,
                git_branch=git_branch,
                multi_repo=multi_repo,
            )

        return {"codebase_path": codebase_path, "graphrag_source_path": graphrag_source_path,
                "status": "success", "fail_message": "",
                "metrics": tracker.to_dict() if tracker else {}}

    def run_multi_repo(self, parent_target_path: str, graphrag_source_path: str = None, metrics_tracker=None):
        """Runs GraphRAG indexing and evaluation across the combined multi-repo codebase."""
        import os, logging
        from loaders.default_asset_loader import DefaultAssetLoader
        from utils.loader_utils import download_code_metadata_directories
        from utils.metrics_tracker import PipelineMetricsTracker

        logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

        multi_search_paths = [
            parent_target_path,
            graphrag_source_path or "graph_rag_app/source",
            os.getenv("PARENT_TARGET_PATH", "target"),
            "target",
            "source",
            ".",
        ]
        tracker = metrics_tracker
        own_tracker = False
        if tracker is None:
            tracker = PipelineMetricsTracker.load_or_create(
                search_paths=multi_search_paths,
                pipeline_name="multi-repo-pipeline",
                multi_repo=True,
            )
            own_tracker = True
            tracker.start_stage("Indexing")
        else:
            prior_tracker = PipelineMetricsTracker.load_or_create(
                search_paths=multi_search_paths,
                pipeline_name=tracker.pipeline_name,
                multi_repo=True,
            )
            if prior_tracker:
                tracker.merge(prior_tracker)
            tracker.start_stage("Indexing")

        if graphrag_source_path is None:
            graphrag_source_path = os.getenv("KFP_DATA_INDEXING_OUTPUT_PATH", "graph_rag_app/source")

        #git_repos = DefaultAssetLoader().download("repos/repo_list.json") or []
        git_repos = json.loads(os.getenv("GIT_REPO_LIST_CONTENTS")) or []

        with tracker.track_step("download_code_metadata", stage="Indexing"):
            download_code_metadata_directories(git_repos, parent_target_path)

        result = self.run(
            codebase_path=parent_target_path,
            graphrag_source_path=graphrag_source_path,
            git_repo="",
            git_branch="",
            multi_repo=True,
            metrics_tracker=tracker,
        )

        if own_tracker:
            tracker.stop_stage("Indexing", status="COMPLETED" if result.get("status") == "success" else "FAILED")
            tracker.stop_pipeline(status="COMPLETED" if result.get("status") == "success" else "FAILED")
            tracker.log_summary()
            tracker.save_and_log(
                target_dir=graphrag_source_path,
                multi_repo=True,
            )

        if result.get("status") != "success":
            raise Exception(f"GraphRAG indexing failed: {result.get('fail_message', '')}")

        return result


##############################################################################
# Module-level aliases for external callers (notebooks)
##############################################################################

def run_full_pipeline(*args, **kwargs):
    return IndexingPipeline().run(*args, **kwargs)

def run_full_pipeline_multi_repo(*args, **kwargs):
    return IndexingPipeline().run_multi_repo(*args, **kwargs)
