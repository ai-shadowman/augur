import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))


def generate_graphrag_index(codebase_path: str, graphrag_source_path: str,
                            git_repo: str = "", git_branch: str = "", multi_repo: bool = False):
    """Generates a GraphRAG index from the provided codebase."""
    import json, os, lancedb, shutil, traceback, subprocess, string, tracemalloc, nest_asyncio, logging
    from loaders.default_asset_loader import DefaultAssetLoader
    from pipelines.base.data_generation import generate_git_slug

    tracemalloc.start()

    nest_asyncio.apply()

    logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

    git_slug = generate_git_slug(git_repo, git_branch) if git_repo else None

    def prepare_settings(template_path: str, output_path: str):

        logging.info("Preparing settings...")

        try:

            with open(template_path) as f:
                content = string.Template(f.read())

            with open(output_path, "w") as f:
                f.write(content.substitute(os.environ))

        except KeyError as keyerr:
            raise ValueError(f"Required environment variable {keyerr} is not set")

    status = "fail"

    try:

        logging.info("Starting process...")

        DefaultAssetLoader().download("graphrag/settings.yaml.in", download_dir="templates")

        settings_config_path = "templates/settings.yaml.in"

        settings_config_path_updated = "templates/settings.yaml"

        graph_rag_config_path = f"{graphrag_source_path}/settings.yaml"

        os.makedirs(f"{graphrag_source_path}/input", exist_ok=True)

        os.makedirs(f"{graphrag_source_path}/output", exist_ok=True)

        prepare_settings(settings_config_path, settings_config_path_updated)

        logging.info("Copying source code to GraphRAG directory...")

        DefaultAssetLoader().download("graphrag/extract_graph.txt", download_dir="prompts")

        DefaultAssetLoader().download("graphrag/community_report.txt", download_dir="prompts")

        DefaultAssetLoader().download("graphrag/summarize_descriptions.txt", download_dir="prompts")

        shutil.copytree(codebase_path, f"{graphrag_source_path}/input", dirs_exist_ok=True)

        shutil.copytree("prompts", f"{graphrag_source_path}/prompts", dirs_exist_ok=True)

        logging.info(f"Running index for git_slug={git_slug}, multi_repo={multi_repo}...")

        graphrag_sh = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "graphrag.sh")

        proc = subprocess.run(
            ["bash", graphrag_sh, graphrag_source_path, graph_rag_config_path],
            capture_output=True, text=True, check=False,
        )

        logging.info(f"\nSubprocess output: {proc.stdout}")

        if proc.returncode != 0:
            raise Exception(f"GraphRAG indexing failed (exit {proc.returncode}): {proc.stdout}")

        artifact_path = ("results/datasets/repos/multi-repo" if multi_repo
                         else f"results/datasets/repos/{git_slug}").rstrip("/")

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

        DefaultAssetLoader().log_results(result_file, artifact_path="results/pipelines",
                                         content=json.dumps(result),
                                         tags={"git_slug": git_slug, "category":"indexing",
                                               "multi_repo": multi_repo})


def evaluate_graphrag_index(graphrag_source_path: str, git_repo: str, git_branch: str,
                            multi_repo: bool = False):
    """Evaluates a GraphRAG index using DefaultCustomEvaluator.evaluate_with_dataset."""
    import logging
    import os

    logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

    from eval.default_custom_evaluator import DefaultCustomEvaluator

    logging.info("Starting GraphRAG index evaluation...")

    DefaultCustomEvaluator().evaluate_with_dataset(graphrag_source_path, git_repo, git_branch,
                                                   multi_repo=multi_repo)

    logging.info("GraphRAG index evaluation complete.")


def run_full_pipeline(codebase_path: str, graphrag_source_path: str, git_repo: str, git_branch: str,
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

    try:

        from loaders.default_asset_loader import DefaultAssetLoader

        git_repos = (DefaultAssetLoader().download("repos/repo_list.json") if multi_repo
                     else [{"git_repo": git_repo, "git_branch": git_branch}])

        for repo in git_repos:

            evaluate_graphrag_index(graphrag_source_path=graphrag_source_path,
                                    git_repo=repo["git_repo"], git_branch=repo["git_branch"],
                                    multi_repo=multi_repo)

    except Exception as e:

        logging.warning(f"GraphRAG index evaluation failed: {e}")

    return {"codebase_path": codebase_path, "graphrag_source_path": graphrag_source_path,
            "status": "success", "fail_message": ""}


def run_full_pipeline_multi_repo(parent_target_path: str):
    """Runs GraphRAG indexing and evaluation across the combined multi-repo codebase."""
    import os

    graphrag_source_path = os.getenv("KFP_DATA_INDEXING_OUTPUT_PATH", "graph_rag_app/source")

    run_full_pipeline(
        codebase_path=parent_target_path,
        graphrag_source_path=graphrag_source_path,
        git_repo="",
        git_branch="",
        multi_repo=True,
    )


