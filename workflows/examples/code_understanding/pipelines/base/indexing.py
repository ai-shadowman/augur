import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))


def generate_graphrag_index(codebase_path: str, graphrag_source_path: str,
                            git_slug: str = None, multi_repo: bool = False):
    """Generates a GraphRAG index from the provided codebase."""
    import json, os, lancedb, shutil, traceback, subprocess, string, tracemalloc, nest_asyncio, logging
    from loaders.default_asset_loader import DefaultAssetLoader

    tracemalloc.start()

    nest_asyncio.apply()

    logging.basicConfig(level=logging.INFO)

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

        DefaultAssetLoader().download_dir("graphrag", download_dir="prompts")

        shutil.copytree("prompts", f"{graphrag_source_path}/prompts", dirs_exist_ok=True)

        logging.info("Running index...")

        graphrag_sh = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "graphrag.sh")

        proc = subprocess.run(
            ["bash", graphrag_sh, graphrag_source_path, graph_rag_config_path],
            capture_output=True, text=True, check=False,
        )

        logging.info(f"\nSubprocess output: {proc.stdout}")

        if proc.returncode != 0:
            raise Exception(f"GraphRAG indexing failed (exit {proc.returncode}): {proc.stdout}")

        artifact_path = "results/datasets/repos/multi-repo" if multi_repo else f"results/datasets/repos/{git_slug}"

        DefaultAssetLoader().log_results(f"{graphrag_source_path}/output",
                                         artifact_path=artifact_path,
                                         tags={"git_slug": git_slug,
                                               "category": "indexing"})

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
                                         tags={"git_slug": git_slug, "category":"indexing"})


def run_full_pipeline(codebase_path: str, graphrag_source_path: str,
                      git_slug: str = None, multi_repo: bool = False):
    """Generates a GraphRAG index and returns a status dict."""
    import traceback, logging

    logging.basicConfig(level=logging.INFO)

    try:

        generate_graphrag_index(codebase_path=codebase_path,
                                graphrag_source_path=graphrag_source_path,
                                git_slug=git_slug, multi_repo=multi_repo)

        logging.info("GraphRAG index generation complete.")

        return {"codebase_path": codebase_path, "graphrag_source_path": graphrag_source_path,
                "status": "success", "fail_message": ""}

    except Exception as e:

        logging.error(f"Error processing Sample Codebase Index: {e}")

        error_message = traceback.format_exc()

        logging.error(error_message)

        return {"codebase_path": codebase_path, "graphrag_source_path": graphrag_source_path,
                "status": "fail", "fail_message": error_message}


