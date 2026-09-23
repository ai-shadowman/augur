import logging
import os
import sys
from contextlib import nullcontext
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())


from utils.otel_utils import enable_telemetry


def clone_from_repo(repo_url, 
                    destination_path, 
                    branch: str = "main",
                    git_username: str = os.environ.get('GIT_USERNAME',""),
                    git_token: str = os.environ.get('GIT_TOKEN',"")):
    """Clones the given git repo to the specified destination."""
    from git import Repo
    from urllib.parse import urlparse, urlunparse

    updated_repo_url = repo_url
    if git_username and git_token:
        parsed = urlparse(repo_url)
        updated_repo_url = urlunparse(parsed._replace(netloc=f"{git_username}:{git_token}@{parsed.netloc}"))

    try:

        Repo.clone_from(updated_repo_url, destination_path, branch=branch)

        all_files = [os.path.join(root, f) for root, _, files in
                     os.walk(destination_path) for f in files]

        logging.debug(f"Files in code_dir: {all_files}")

        logging.info(f"Repository '{repo_url}' cloned successfully to '{destination_path}'.")

    except Exception as e:

        err_msg = str(e).replace(git_token, '***') if git_token else str(e)
        logging.error(f"Error cloning repository: {err_msg}")

        raise e


def reset_environment(source_path: str, target_path: str):
    """Removes the source and target directories."""
    import shutil

    logging.info("Resetting environment...")

    shutil.rmtree(source_path, ignore_errors=True)

    shutil.rmtree(target_path, ignore_errors=True)


def prepare_environment(source_path: str, 
                        target_path: str, 
                        git_repo: str, 
                        git_branch: str,
                        git_username: str = "",
                        git_token: str = ""):
    """Prepares the environment at the start of the pipeline."""
    logging.info("Preparing the environment for pipeline run...")

    try:
        from utils.duration_tracker import DurationTracker
        dur_tracker = DurationTracker.get_instance()
    except Exception:
        dur_tracker = None

    try:
        cm_reset = dur_tracker.measure(stage="Data Generation", step="Reset Environment") if dur_tracker else nullcontext()
        with cm_reset:
            reset_environment(source_path, target_path)

        cm_clone = dur_tracker.measure(stage="Data Generation", step="GitHub Checkout") if dur_tracker else nullcontext()
        with cm_clone:
            clone_from_repo(git_repo, 
                            source_path, 
                            branch=git_branch,
                            git_username=git_username,
                            git_token=git_token)

    except Exception as e:

        logging.error(f"Error preparing environment: {e}")

        raise e


def generate_raw_dataset(source_path: str, target_path: str, git_repo: str, git_branch: str,
                         language: str = "python", split_sections=True, config=False,
                         multi_repo: bool = False):
    """Walks source_path and returns a DataFrame of source files for the given language."""
    from dotenv import load_dotenv
    import os

    load_dotenv()

    import pandas as pd
    from utils import code_utils
    import os

    git_slug = code_utils.generate_slug_from_repo(git_repo, git_branch) if git_repo else None

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
                        records.append({
                            "code": code,
                            "file_path": rel_path,
                            "git_repo": git_repo,
                            "git_slug": git_slug,
                            "language": language,
                            "multi_repo": multi_repo,
                        })
                except (UnicodeDecodeError, PermissionError):
                    continue

        return pd.DataFrame(records) if records else None
    except Exception as e:
        logging.error(f"Error generating dataframe with raw code: {e}")
        raise e


def _ensure_data_generation_tokens(token_tracker, df, language: str, converted_df=None):
    """Ensures token usage is recorded for data generation even if flow parsing threw an error
    or callbacks intercepted 0 tokens."""
    if not token_tracker or df is None or len(df) == 0:
        return
    try:
        records_for_lang = [
            (k, r) for k, r in token_tracker.records.items()
            if f"Data Generation ({language})" in k
        ]
        has_real_tokens = any(
            r.get("prompt_tokens", 0) > 0 or r.get("output_tokens", 0) > 0
            for _, r in records_for_lang
        )
        if not has_real_tokens:
            model_name = f"{os.getenv('GRAPHRAG_LLM_PROVIDER')}/{os.getenv('GRAPHRAG_LLM_ID')}"
            if not os.getenv("GRAPHRAG_LLM_ID"):
                model_name = getattr(token_tracker, "chat_model", "gpt-oss-120b")

            total_p_tokens = 0
            total_o_tokens = 0
            for _, row in df.iterrows():
                code_str = str(row.get("code") or row.get("content") or "")
                total_p_tokens += max(50, len(code_str) // 4 + 200)

            if converted_df is not None and not converted_df.empty:
                for _, row in converted_df.iterrows():
                    out_str = str(row.get("metadata") or row.get("yaml_output") or row.to_dict())
                    total_o_tokens += max(20, len(out_str) // 4)
            else:
                total_o_tokens = len(df) * 150

            token_tracker.track(
                source=f"Data Generation ({language}) ({model_name})",
                calls=len(df),
                prompt_tokens=total_p_tokens,
                output_tokens=total_o_tokens,
                model=model_name,
                stage="Data Generation",
                category="Data Generation",
            )
    except Exception as e:
        logging.debug(f"Failed fallback token estimation in _ensure_data_generation_tokens: {e}")


def get_parsed_code_metadata(df, language, config=False):
    """Runs an SDG Hub flow over df and returns a DataFrame with extracted metadata."""
    from datasets import Dataset
    from sdg_hub.core.flow import Flow
    from flows.flow_extensions import CustomDeleteColumnsBlock
    from datetime import datetime
    from loaders.default_asset_loader import DefaultAssetLoader
    import os

    try:

        # Per-request LLM timeout (seconds), LiteLLM retry count, and concurrent request limit
        llm_timeout = float(os.getenv("SDG_LLM_TIMEOUT", "600"))
        llm_num_retries = int(os.getenv("SDG_LLM_NUM_RETRIES", "10"))
        llm_max_concurrency = int(os.getenv("SDG_LLM_MAX_CONCURRENCY", "10"))

        logging.info(
            f"Parsing code metadata... (timeout={llm_timeout}s, num_retries={llm_num_retries}, "
            f"max_concurrency={llm_max_concurrency})"
        )

        try:
            from utils.token_tracker import TokenCostTracker
            token_tracker = TokenCostTracker.get_instance()
            token_tracker.set_category(f"Data Generation ({language})")
            token_tracker.enable_litellm_callbacks(category=f"Data Generation ({language})")
            token_tracker.enable_openai_tracking(category=f"Data Generation ({language})")
            calls_before = token_tracker.get_totals()["total_calls"]
        except Exception as e:
            logging.debug(f"Failed to enable token tracking in get_parsed_code_metadata: {e}")
            token_tracker = None
            calls_before = 0

        dataset = Dataset.from_pandas(df)

        flow_dir = "config_generation" if config else "code_generation"

        flows_dir = f"flows/{flow_dir}"
        DefaultAssetLoader().download_dir(f"sdghub/{flow_dir}", download_dir=flows_dir)

        flow = Flow.from_yaml(f"{flows_dir}/flow.yaml")

        flow.set_model_config(
            model=f"{os.getenv('GRAPHRAG_LLM_PROVIDER')}/{os.getenv('GRAPHRAG_LLM_ID')}",
            api_base=os.getenv("GRAPHRAG_LLM_API_BASE"),
            api_key=os.getenv("GRAPHRAG_LLM_TOKEN"),
            temperature=0,
            best_of=1,
            n=1,
            max_tokens=32_000,
            response_format={"type": "json_object"},
            top_k=1,
            timeout=llm_timeout,
            num_retries=llm_num_retries,
        )

        converted_df = None
        try:
            converted_dataset = flow.generate(dataset, max_concurrency=llm_max_concurrency)
            converted_df = converted_dataset.to_pandas()
        finally:
            # Fallback token accounting if callbacks did not intercept async Flow executions or captured 0 tokens
            if token_tracker and len(df) > 0:
                _ensure_data_generation_tokens(token_tracker, df, language, converted_df=converted_df)

        if converted_df is not None:
            converted_df.to_csv(
                f"data_{language}_{'config_' if config else '_'}{str(int(datetime.now().timestamp()))}.csv"
            )

        return converted_df

    except Exception as e:

        logging.error(f"Error extracting metadata from code: {e}")

        raise e


def load_external_data(source_path: str) -> dict:
    """Loads and merges all JSON files from source_path/.code_metadata/ into a single dict."""
    import os, json
    from utils import code_utils

    code_metadata_dir = os.path.join(source_path, code_utils.CODE_METADATA_DIR)
    result = {}

    if not os.path.isdir(code_metadata_dir):
        return result

    for root, _, files in os.walk(code_metadata_dir):
        for filename in files:
            try:
                with open(os.path.join(root, filename), "r", encoding="utf-8") as f:
                    data = json.load(f)
                for key, value in data.items():
                    if isinstance(value, list) and isinstance(result.get(key), list):
                        result[key].extend(value)
                    else:
                        result[key] = value
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

    return result


def generate_code_comment(metadata: dict, file_path: str, config=False, external_metadata: dict = None):
    """Builds a structured text comment from a code file's metadata dictionary."""
    import os
    from utils import code_utils

    try:

        external_metadata = external_metadata or {}

        lines = []

        header = (f"This file is located at {metadata.get('file_path')} "
                  f"from repository url {metadata.get('git_repo')}, "
                  f"repository slug {metadata.get('git_slug')}, "
                  f"multi_repo {str(metadata.get('multi_repo', False)).lower()}")

        runtime_stack = external_metadata.get('runtime_stack') or []
        if runtime_stack:
            runtime_parts = " / ".join(
                f"{r.get('name', '')} {r.get('runtime_version', '')}".strip()
                for r in runtime_stack if r.get('name')
            )
            if runtime_parts:
                header += f", runtime {runtime_parts}"

        lines.append(header)

        if metadata.get('package'):
            lines.append(f"\n Package: {metadata['package']}")

        if metadata.get('purpose'):
            lines.append(f"\n Purpose: {metadata['purpose']}")

        imports = metadata.get('imports') or []
        libraries = metadata.get('libraries') or []

        package = metadata.get('package')
        external_libraries = []
        if package:
            for pkg_entry in external_metadata.get('repo_packages', []):
                if pkg_entry.get('package') == package:
                    external_libraries.extend(pkg_entry.get('libraries', []))

        if imports or libraries or external_libraries:
            lines.append(f"\nDependencies:")
            lines.extend(f"- [import] {imp}" for imp in imports)
            lines.extend(
                f"- [library] {lib.get('library_name', '')} {lib.get('library_version', '')}".strip()
                for lib in libraries
            )
            lines.extend(
                f"- [library] {lib.get('library_name', '')} {lib.get('library_version', '')}".strip()
                for lib in external_libraries
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


def save_metadata_file(metadata: dict, target_path: str, relative_file_path: str,
                       git_repo: str, git_slug: str, language: str, schema: dict = None):
    """Writes a flattened metadata YAML file for a single source file to target_path."""
    import os
    from pathlib import Path
    from utils import json_utils
    from loaders.default_asset_loader import DefaultAssetLoader

    if schema is None:
        schema = DefaultAssetLoader().download("schemas/code_metadata_schema.json")

    metadata_file_path = os.path.join(
        target_path, str(Path(relative_file_path).with_suffix("")) + "_metadata.txt"
    )

    os.makedirs(os.path.dirname(metadata_file_path), exist_ok=True)

    with open(metadata_file_path, "w", encoding="utf-8") as f:
        f.write(json_utils.flatten_code_metadata(metadata, schema))


def save_code_and_metadata_files(df, target_path, git_repo: str, git_slug: str, language: str, config=False,
                                 external_metadata: dict = None):
    """Writes annotated code and flattened metadata files to target_path."""
    import os
    from pathlib import Path
    from utils import json_utils
    from loaders.default_asset_loader import DefaultAssetLoader

    try:

        logging.info("Saving code and metadata files...")

        schema = DefaultAssetLoader().download("schemas/code_metadata_schema.json")

        for _, row in df.iterrows():

            rel_file_path = row.get("file_path", "")
            if not rel_file_path:
                continue

            logging.debug(f"**Processing file {rel_file_path}...**")

            code = row.get("code", "")

            extracted = row.get("extracted_data") if "extracted_data" in row else None
            metadata = json_utils.extract_json_from_string(extracted) if extracted else None

            if not metadata:
                logging.info(f"No LLM metadata found for file {rel_file_path}. Using fallback metadata.")
                metadata = {
                    "file_path": rel_file_path,
                    "language": language,
                    "git_repo": git_repo,
                    "git_slug": git_slug,
                    "multi_repo": str(row.get("multi_repo", False)).lower(),
                }

            if not metadata.get('language'):
                metadata['language'] = language
            if not metadata.get('file_path'):
                metadata['file_path'] = rel_file_path
            if not metadata.get('git_repo'):
                metadata['git_repo'] = git_repo
            if not metadata.get('git_slug'):
                metadata['git_slug'] = git_slug

            target_file_path = os.path.join(target_path, Path(rel_file_path).with_suffix(".txt"))

            code_header_comment = generate_code_comment(
                metadata=metadata, file_path=rel_file_path, config=config,
                external_metadata=external_metadata,
            ) or ""

            os.makedirs(os.path.dirname(target_file_path), exist_ok=True)

            with open(target_file_path, "w", encoding="utf-8") as f:
                f.write(f"{code_header_comment}\n{code}")

            save_metadata_file(metadata, target_path, rel_file_path,
                               git_repo=git_repo, git_slug=git_slug, language=language,
                               schema=schema)

    except Exception as e:

        logging.error(f"Error saving code and metadata: {e}")

        raise e


@enable_telemetry
def generate_code_and_meta(git_repo: str, git_branch: str, language: str,
                            source_path: str, target_path: str, config: bool = False,
                            multi_repo: bool = False, external_metadata: dict = None):
    """Generates and saves code metadata for one language/config combination."""
    import json, traceback
    from loaders.default_asset_loader import DefaultAssetLoader
    from utils import code_utils
    import shutil

    git_slug = code_utils.generate_slug_from_repo(git_repo, git_branch) if git_repo else None

    result = {"git_slug": git_slug, "language": language, "config": config,
              "status": "error", "fail_message": ""}

    try:
        cfg_suffix = " config" if config else ""
        step_label = f"{language}{cfg_suffix}"

        try:
            from utils.duration_tracker import DurationTracker
            dur_tracker = DurationTracker.get_instance()
        except Exception:
            dur_tracker = None

        parse_cm = dur_tracker.measure(stage="Data Generation", step=f"Parse Raw Code ({step_label})") if dur_tracker else nullcontext()
        with parse_cm:
            code_df = generate_raw_dataset(source_path, target_path, git_repo, git_branch,
                                           language=language, config=config, multi_repo=multi_repo)

        if code_df is None:
            logging.info(f"No {language} files found (config={config}).")
            result["status"] = "skipped"
            return

        try:
            llm_cm = dur_tracker.measure(stage="Data Generation", step=f"LLM Metadata Extraction ({step_label})") if dur_tracker else nullcontext()
            with llm_cm:
                code_and_metadata_df = get_parsed_code_metadata(code_df, language=language, config=config)
        except Exception as e:
            logging.warning(
                f"LLM Metadata Extraction failed for {language} (config={config}): {e}. "
                f"Falling back to saving raw code files without LLM metadata."
            )
            code_and_metadata_df = code_df

        save_cm = dur_tracker.measure(stage="Data Generation", step=f"Save Metadata Files ({step_label})") if dur_tracker else nullcontext()
        with save_cm:
            save_code_and_metadata_files(code_and_metadata_df, target_path, git_repo=git_repo,
                                         git_slug=git_slug, language=language, config=config,
                                         external_metadata=external_metadata)

        logging.info(f"Successfully generated code metadata for '{git_repo}'.")

        result["status"] = "complete"

        # Immediately persist telemetry to target_path before asset logging
        try:
            if dur_tracker:
                dur_tracker.save_to_file(os.path.join(target_path, "durations.json"))
        except Exception:
            pass
        try:
            from utils.token_tracker import TokenCostTracker, extract_data_generation_tokens
            t_tr = TokenCostTracker.get_instance()
            extract_data_generation_tokens(target_path, t_tr)
            t_tr.save_to_file(os.path.join(target_path, "tokens.json"))
        except Exception:
            pass

        log_meta_cm = dur_tracker.measure(stage="Data Generation", step=f"Log Metadata Results ({step_label})") if dur_tracker else nullcontext()
        with log_meta_cm:
            DefaultAssetLoader().log_results(
                target_path,
                artifact_path=DefaultAssetLoader.get_log_results_artifact_path(
                    DefaultAssetLoader.RESULTS_PATH_PREFIX_METADATA,
                    git_slug=git_slug,
                    multi_repo=multi_repo,
                ),
                tags={"git_slug": str(git_slug or "multi-repo"), "category": "data-generation", "code-metadata": "true", "multi_repo": str(multi_repo)},
            )

    except Exception as e:

        logging.error(f"Error generating code and metadata: {e}")

        result["fail_message"] = traceback.format_exc()

        raise e

    finally:

        suffix = f"_{language}_config" if config else f"_{language}"

        result_file = f"data_generation_result_{git_slug}{suffix}.json"

        log_res_cm = dur_tracker.measure(stage="Data Generation", step=f"Log Pipeline Result ({step_label})") if dur_tracker else nullcontext()
        with log_res_cm:
            DefaultAssetLoader().log_results(
                result_file,
                artifact_path=DefaultAssetLoader.get_log_results_artifact_path(
                    DefaultAssetLoader.RESULTS_PATH_PREFIX_PIPELINES,
                    git_slug=git_slug,
                    multi_repo=multi_repo,
                ),
                content=json.dumps(result),
                tags={"git_slug": git_slug, "category": "data-generation", "multi_repo": multi_repo},
            )


def generate_git_slug(git_repo: str, git_branch: str) -> str:
    """Returns a filesystem-safe slug derived from the repo URL and branch."""
    from utils import code_utils

    return code_utils.generate_slug_from_repo(git_repo, git_branch)


def detect_languages(source_path: str) -> list:
    """Returns the list of programming languages detected in source_path."""
    from utils import code_utils

    languages = code_utils.get_detected_languages_for_repo(source_path)

    if not languages:
        raise Exception(f"No languages detected in source_path='{source_path}'.")

    return languages


##############################################################################
# Pipeline stage
##############################################################################

class DataGenerationPipeline:

    @enable_telemetry
    def run(self, git_repo: str, git_branch: str, source_path: str, target_path: str,
            multi_repo: bool = False):
        """Prepares the environment, generates code metadata for all detected languages, and returns a status dict."""
        import traceback

        git_slug = generate_git_slug(git_repo, git_branch)

        try:
            from utils.token_tracker import TokenCostTracker
            TokenCostTracker.reset_instance()
            token_tracker = TokenCostTracker.get_instance()
            token_tracker.set_category("Data Generation")
            token_tracker.enable_litellm_callbacks(category="Data Generation")
            token_tracker.enable_openai_tracking(category="Data Generation")
        except Exception:
            token_tracker = None

        try:
            from utils.duration_tracker import DurationTracker
            dur_tracker = DurationTracker.get_instance()
        except Exception:
            dur_tracker = None

        try:
            if dur_tracker:
                with dur_tracker.measure(stage="Data Generation", step="Data Generation Total", metadata={"is_aggregate": True}):
                    prepare_environment(source_path=source_path, target_path=target_path,
                                        git_repo=git_repo, git_branch=git_branch)

                    with dur_tracker.measure(stage="Data Generation", step="Detect Languages"):
                        languages = detect_languages(source_path)

                    with dur_tracker.measure(stage="Data Generation", step="Load External Data"):
                        external_metadata = load_external_data(source_path)

                    for language in languages:
                        for config in [False, True]:
                            generate_code_and_meta(
                                git_repo=git_repo, git_branch=git_branch,
                                language=language, source_path=source_path, target_path=target_path,
                                config=config, multi_repo=multi_repo, external_metadata=external_metadata,
                            )
            else:
                prepare_environment(source_path=source_path, target_path=target_path,
                                    git_repo=git_repo, git_branch=git_branch)
                languages = detect_languages(source_path)
                external_metadata = load_external_data(source_path)
                for language in languages:
                    for config in [False, True]:
                        generate_code_and_meta(
                            git_repo=git_repo, git_branch=git_branch,
                            language=language, source_path=source_path, target_path=target_path,
                            config=config, multi_repo=multi_repo, external_metadata=external_metadata,
                        )

            git_slug = generate_git_slug(git_repo, git_branch) if git_repo else None

            logging.info("Data generation pipeline complete.")

            result = {"git_slug": git_slug, "status": "complete", "fail_message": ""}

        except Exception as e:

            logging.error("PIPELINE FAILED!")

            error_message = traceback.format_exc()

            logging.error(error_message)

            reset_environment(source_path, target_path)

            result = {"git_slug": git_slug, "status": "error", "fail_message": error_message}

        finally:
            if dur_tracker:
                if target_path:
                    try:
                        dur_tracker.save_to_file(os.path.join(target_path, "durations.json"))
                        parent_t = os.path.dirname(target_path)
                        if parent_t and parent_t != target_path:
                            dur_tracker.save_to_file(os.path.join(parent_t, "durations.json"))
                        if os.path.isdir("target"):
                            dur_tracker.save_to_file(os.path.join("target", "durations.json"))
                    except Exception as e:
                        logging.debug(f"Failed to save duration metrics to {target_path}: {e}")
                try:
                    dur_tracker.upload_to_mlflow(git_slug=git_slug, stage="Data Generation", multi_repo=multi_repo)
                except Exception as e:
                    logging.debug(f"Failed to upload duration metrics to MLflow: {e}")

            try:
                from utils.token_tracker import TokenCostTracker
                token_tracker = TokenCostTracker.get_instance()
                if target_path:
                    try:
                        token_tracker.save_to_file(os.path.join(target_path, "tokens.json"))
                        parent_t = os.path.dirname(target_path)
                        if parent_t and parent_t != target_path:
                            token_tracker.save_to_file(os.path.join(parent_t, "tokens.json"))
                        if os.path.isdir("target"):
                            token_tracker.save_to_file(os.path.join("target", "tokens.json"))
                    except Exception as e:
                        logging.debug(f"Failed to save token metrics to {target_path}: {e}")
                token_tracker.upload_to_mlflow(git_slug=git_slug, stage="Data Generation", multi_repo=multi_repo)
            except Exception as e:
                logging.debug(f"Failed to upload token metrics to MLflow: {e}")

            if dur_tracker:
                try:
                    summary = dur_tracker.format_summary()
                    logging.info("\n" + summary)
                except Exception as e:
                    logging.debug(f"Failed to print duration summary in data generation pipeline: {e}")

            try:
                from utils.token_tracker import TokenCostTracker
                tok_tr = TokenCostTracker.get_instance()
                summary = tok_tr.format_summary()
                logging.info("\n" + summary)
            except Exception as e:
                logging.debug(f"Failed to print token summary in data generation pipeline: {e}")

        return result

    def run_multi_repo(self, git_repos: list):
        """Runs run for each repository in git_repos and returns a list of status dicts."""
        from utils import code_utils
        import os

        parent_source_path = os.getenv("PARENT_SOURCE_PATH", "source")
        parent_target_path = os.getenv("PARENT_TARGET_PATH", "target")

        pipeline_results = []

        for git_data in git_repos:
            git_repo = git_data["git_repo"]
            git_branch = git_data["git_branch"]
            repo_slug = code_utils.generate_slug_from_repo(git_repo, git_branch)
            source_path = f"{parent_source_path}/{repo_slug}"
            target_path = f"{parent_target_path}/{repo_slug}"

            logging.info(f"Generating data for git repo={git_repo}, branch={git_branch}, slug={repo_slug}...")
            result = self.run(git_repo=git_repo, git_branch=git_branch,
                              source_path=source_path, target_path=target_path,
                              multi_repo=True)
            pipeline_results.append(result)

        try:
            from utils.duration_tracker import DurationTracker
            dur_tr = DurationTracker.get_instance()
            if parent_target_path:
                try:
                    dur_tr.save_to_file(os.path.join(parent_target_path, "durations.json"))
                except Exception:
                    pass
            dur_tr.upload_to_mlflow(git_slug=None, stage="Data Generation", multi_repo=True)
        except Exception as e:
            logging.debug(f"Failed to persist aggregate durations in DataGenerationPipeline.run_multi_repo: {e}")

        try:
            from utils.token_tracker import TokenCostTracker
            tok_tr = TokenCostTracker.get_instance()
            if parent_target_path:
                try:
                    tok_tr.save_to_file(os.path.join(parent_target_path, "tokens.json"))
                except Exception:
                    pass
            tok_tr.upload_to_mlflow(git_slug=None, stage="Data Generation", multi_repo=True)
        except Exception as e:
            logging.debug(f"Failed to persist aggregate tokens in DataGenerationPipeline.run_multi_repo: {e}")

        return pipeline_results


##############################################################################
# Module-level aliases for external callers (notebooks)
##############################################################################

def run_full_pipeline(*args, **kwargs):
    return DataGenerationPipeline().run(*args, **kwargs)

def run_full_pipeline_multi_repo(*args, **kwargs):
    return DataGenerationPipeline().run_multi_repo(*args, **kwargs)
