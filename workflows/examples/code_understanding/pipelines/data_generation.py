import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def clone_from_repo(repo_url, destination_path, branch="master"):
    """Clones the given git repo to the specified destination."""
    from git import Repo
    import logging

    logging.basicConfig(level=logging.INFO)

    try:

        Repo.clone_from(repo_url, destination_path, branch=branch)

        logging.info(f"Repository '{repo_url}' cloned successfully to '{destination_path}'.")

    except Exception as e:

        logging.error(f"Error cloning repository: {e}")

        raise e


def prepare_environment(source_path: str, target_path: str, git_repo: str, git_branch: str):
    """Prepares the environment at the start of the pipeline."""
    import shutil
    import logging

    logging.basicConfig(level=logging.INFO)

    logging.info("Preparing the environment for pipeline run...")

    try:

        shutil.rmtree(source_path, ignore_errors=True)

        shutil.rmtree(target_path, ignore_errors=True)

        clone_from_repo(git_repo, source_path, branch=git_branch)

    except Exception as e:

        logging.error(f"Error preparing environment: {e}")

        raise e


def generate_raw_dataset(source_path: str, target_path: str, git_repo: str, git_slug: str,
                         language: str = "python", split_sections=True, config=False,
                         multi_repo: bool = False):
    """Walks source_path and returns a DataFrame of source files for the given language."""
    from dotenv import load_dotenv
    import os

    load_dotenv()

    import pandas as pd
    from utils import code_utils
    import logging

    logging.basicConfig(level=logging.INFO)

    try:

        logging.info(f"Generating raw dataset for git repo={git_repo}, language={language}...")

        records = []

        excluded_dirs = code_utils.get_exclude_dirs_for_language(language)

        for root, dirs, files in os.walk(source_path):

            dirs[:] = [d for d in dirs if d not in excluded_dirs]

            include_extensions = (
                code_utils.get_config_file_extensions_for_language(language) if config
                else code_utils.get_file_extensions_for_language(language)
            )

            for filename in files:

                if os.path.splitext(filename)[1] not in include_extensions:
                    continue

                logging.debug(f"Processing {filename}...")

                abs_path = os.path.join(root, filename)

                if code_utils.is_large_code_file(abs_path, max_size=200_000):
                    code_utils.process_large_code_file(abs_path, source_path)
                    continue

                rel_path = os.path.relpath(abs_path, source_path)

                try:

                    with open(abs_path, "r", encoding="utf-8") as f:
                        code = f.read()
                        records.append({"code": code,
                                        "file_path": rel_path,
                                        "git_repo": git_repo,
                                        "git_slug": git_slug,
                                        "language": language,
                                        "multi_repo": multi_repo,})

                except (UnicodeDecodeError, PermissionError):
                    continue

        return pd.DataFrame(records) if records else None

    except Exception as e:

        logging.error(f"Error generating dataframe with raw code: {e}")

        raise e


def get_parsed_code_metadata(df, language, config=False):
    """Runs an SDG Hub flow over df and returns a DataFrame with extracted metadata."""
    from datasets import Dataset
    from sdg_hub.core.flow import Flow
    from flows.flow_extensions import CustomDeleteColumnsBlock
    from datetime import datetime
    from loaders.default_asset_loader import DefaultAssetLoader
    import logging, os

    logging.basicConfig(level=logging.INFO)

    try:

        logging.info("Parsing code metadata...")

        dataset = Dataset.from_pandas(df)

        flow_dir = "config_generation" if config else "code_generation"

        DefaultAssetLoader().download_dir(f"sdghub/{flow_dir}", download_dir=f"flows/{flow_dir}")

        flow = Flow.from_yaml(f"flows/{flow_dir}/flow.yaml")

        flow.set_model_config(
            model=f"{os.getenv('GRAPHRAG_LLM_PROVIDER')}/{os.getenv('GRAPHRAG_LLM_ID')}",
            api_base=os.getenv("GRAPHRAG_LLM_API_BASE"),
            api_key=os.getenv("GRAPHRAG_LLM_TOKEN"),
            temperature=0,
            max_tokens=32_000,
            response_format={"type": "json_object"},
            top_k=1,
        )

        converted_dataset = flow.generate(dataset, max_concurrency=10)

        converted_df = converted_dataset.to_pandas()

        converted_df.to_csv(
            f"data_{language}_{'config_' if config else '_'}{str(int(datetime.now().timestamp()))}.csv"
        )

        return converted_df

    except Exception as e:

        logging.error(f"Error extracting metadata from code: {e}")

        raise e


def generate_code_comment(metadata: dict, file_path: str, config=False):
    """Builds a structured text comment from a code file's metadata dictionary."""
    import os
    from utils import code_utils
    import logging

    logging.basicConfig(level=logging.INFO)

    try:

        lines = []

        lines.append(f"This file is located at {metadata.get('file_path')} "
                     f"from repository url {metadata.get('git_repo')}, "
                     f"repository slug {metadata.get('git_slug')}")

        if metadata.get('package'):
            lines.append(f"\n Package: {metadata['package']}")

        if metadata.get('purpose'):
            lines.append(f"\n Purpose: {metadata['purpose']}")

        imports = metadata.get('imports') or []
        libraries = metadata.get('libraries') or []

        if imports or libraries:
            lines.append(f"\nDependencies:")
            lines.extend(f"- [import] {imp}" for imp in imports)
            lines.extend(
                f"- [library] {lib.get('library_name', '')} {lib.get('library_version', '')}".strip()
                for lib in libraries
            )

        if metadata.get('classes'):
            lines.append(f"\n Classes:")
            lines.extend(f"- {cls}" for cls in metadata['classes'])

        if metadata.get('functions'):
            lines.append(f"\n Functions:")
            lines.extend(f"- {func}" for func in metadata['functions'])

        if metadata.get('methods'):
            lines.append(f"\n Methods:")
            lines.extend([
                f"- {method.get('method_name', method) if isinstance(method, dict) else method}"
                for method in metadata['methods']
            ])

        if config:
            extension = os.path.splitext(file_path)[1]
            begin, end = code_utils.get_comment_delimiters_for_file_extension(extension)
        else:
            begin, end = code_utils.get_comment_delimiters_for_language(metadata.get('language'))

        return begin + '\n'.join(lines) + end

    except Exception as e:

        logging.error(f"Error generating comment: {e}")

        raise e


def save_code_and_metadata_files(df, target_path, config=False):
    """Writes annotated code and flattened metadata files to target_path."""
    import os
    from pathlib import Path
    import logging, json
    from utils import json_utils
    from loaders.default_asset_loader import DefaultAssetLoader

    logging.basicConfig(level=logging.INFO)

    try:

        logging.info("Saving code and metadata files...")

        _SCHEMA = DefaultAssetLoader().download("schemas/code_metadata_schema.json")

        for _, row in df.iterrows():

            code = row["code"]

            metadata = json_utils.extract_json_from_string(row["extracted_data"])

            if not metadata:
                logging.info(f"No metadata found for file {row['file_path']}. Skipping...")
                continue

            rel_file_path = row["file_path"]

            target_file_path = os.path.join(target_path, Path(rel_file_path).with_suffix(".txt"))

            metadata_file_path = os.path.join(
                target_path, str(Path(rel_file_path).with_suffix("")) + "_metadata.txt"
            )

            code_header_comment = generate_code_comment(
                metadata=metadata, file_path=rel_file_path, config=config
            ) or ""

            os.makedirs(os.path.dirname(target_file_path), exist_ok=True)

            os.makedirs(os.path.dirname(metadata_file_path), exist_ok=True)

            with open(target_file_path, "w", encoding="utf-8") as f:
                f.write(f"{code_header_comment}\n{code}")

            with open(metadata_file_path, "w", encoding="utf-8") as f:
                f.write(json_utils.flatten_code_metadata(metadata, _SCHEMA))

    except Exception as e:

        logging.error(f"Error saving code and metadata: {e}")

        raise e


def generate_code_and_meta(git_repo: str, git_branch: str, git_slug: str, language: str,
                            source_path: str, target_path: str, config: bool = False,
                            multi_repo: bool = False):
    """Generates and saves code metadata for one language/config combination."""
    import logging

    logging.basicConfig(level=logging.INFO)

    try:

        code_df = generate_raw_dataset(source_path, target_path, git_repo, git_slug,
                                       language=language, config=config, multi_repo=multi_repo)

        if code_df is None:
            logging.info(f"No {language} files found (config={config}).")
            return

        code_and_metadata_df = get_parsed_code_metadata(code_df, language=language, config=config)

        save_code_and_metadata_files(code_and_metadata_df, target_path, config=config)

        logging.info(f"Successfully generated code metadata for '{git_repo}'.")

    except Exception as e:

        logging.error(f"Error generating code and metadata: {e}")

        raise e


def generate_all_code_and_meta(git_repo: str, git_branch: str, source_path: str, target_path: str,
                               git_slug: str = None, multi_repo: bool = False):
    """Detects languages and runs generate_code_and_meta for every language/config combination."""
    import logging
    from utils import code_utils

    logging.basicConfig(level=logging.INFO)

    if git_slug is None:
        git_slug = code_utils.generate_slug_from_repo(git_repo, git_branch)

    languages = code_utils.get_detected_languages_for_repo(source_path)

    if not languages:
        raise Exception(f"No languages detected in repo={git_repo}.")

    for language in languages:

        for config in [False, True]:

            generate_code_and_meta(
                git_repo=git_repo, git_branch=git_branch, git_slug=git_slug,
                language=language, source_path=source_path, target_path=target_path, config=config,
                multi_repo=multi_repo,
            )


def run_full_pipeline(git_repo: str, git_branch: str, source_path: str, target_path: str,
                      git_slug: str = None, multi_repo: bool = False):
    """Prepares the environment, generates code metadata for all detected languages, and returns a status dict."""
    import json, traceback, logging
    from pathlib import Path
    from utils import code_utils
    from loaders.default_asset_loader import DefaultAssetLoader

    logging.basicConfig(level=logging.INFO)

    if git_slug is None:
        git_slug = code_utils.generate_slug_from_repo(git_repo, git_branch)

    try:

        prepare_environment(source_path=source_path, target_path=target_path,
                            git_repo=git_repo, git_branch=git_branch)

        generate_all_code_and_meta(git_repo=git_repo, git_branch=git_branch,
                                   source_path=source_path, target_path=target_path,
                                   git_slug=git_slug, multi_repo=multi_repo)

        logging.info("Data generation pipeline complete.")

        result = {"git_slug": git_slug, "status": "complete", "fail_message": ""}

    except Exception as e:

        logging.error("PIPELINE FAILED!")

        error_message = traceback.format_exc()

        logging.error(error_message)

        result = {"git_slug": git_slug, "status": "error", "fail_message": error_message}

    result_file = f"data_generation_result_{git_slug}.json"

    Path(result_file).write_text(json.dumps(result))

    DefaultAssetLoader().log_results(result_file, artifact_path="results/pipelines")

    return result


def run_full_pipeline_aggregated(git_repos: list):
    """Runs run_full_pipeline for each repository in git_repos and returns a list of status dicts."""
    import logging
    from utils import code_utils

    logging.basicConfig(level=logging.INFO)

    pipeline_results = []

    for git_data in git_repos:

        git_repo = git_data["git_repo"]

        git_branch = git_data["git_branch"]

        repo_slug = code_utils.generate_slug_from_repo(git_repo, git_branch)

        source_path = f"{git_data['parent_source_path']}/{repo_slug}"

        target_path = f"{git_data['parent_target_path']}/{repo_slug}"

        logging.info(f"Generating data for git repo={git_repo}, branch={git_branch}, slug={repo_slug}...")

        result = run_full_pipeline(git_repo=git_repo, git_branch=git_branch,
                                   source_path=source_path, target_path=target_path,
                                   git_slug=repo_slug, multi_repo=True)

        pipeline_results.append(result)

    return pipeline_results
