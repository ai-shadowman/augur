import logging
import os
import sys
from contextlib import nullcontext
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))
from utils.request_timing import timing_span

from utils.kubeflow_utils import setup_logging
setup_logging()

from utils.otel_utils import enable_telemetry
from utils.repository_ignore import (
    AUGURIGNORE_FILENAME,
    RepositoryIgnoreError,
    RepositoryIgnorePolicy,
)


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

        # Declared dependencies have to be read while the checkout exists;
        # indexing runs in another pod, long after reset_environment removes
        # it. Syft is skipped: it reports resolved versions, and the stitch
        # consumes declared constraints, which come from the manifests.
        cm_facts = dur_tracker.measure(stage="Data Generation", step="Extract Provenance Facts") if dur_tracker else nullcontext()
        with cm_facts:
            from provenance.pipeline import capture as capture_provenance
            capture_provenance(source_path, target_path, run_syft=False)

    except Exception as e:

        logging.error(f"Error preparing environment: {e}")

        raise e


#: Stage one makes no model call unless this is set. The one answer left to
#: ask for was ``Purpose:``, and Arm A (2026-10-24, celery full, gpt-oss-120b)
#: measured a header carrying it against the same header without: worse symbol
#: precision on two graders, 121 fewer declared symbols after the join, and no
#: gain outside the band registered in advance as not counting. The prompt is
#: kept rather than deleted because what it might help, the descriptions a
#: report is written from, was not graded. Figures in ``provenance/arms.py``.
PURPOSE_ENV = "AUGUR_STAGE_ONE_PURPOSE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def purpose_enabled() -> bool:
    """Whether stage one asks a model for each file's ``Purpose:`` line."""
    return os.getenv(PURPOSE_ENV, "").strip().lower() in _TRUTHY


def generate_raw_dataset(source_path: str, target_path: str, git_repo: str, git_branch: str,
                         language: str = "python", split_sections=True, config=False,
                         multi_repo: bool = False, ignore_policy=None):
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
        ignore_policy = ignore_policy or RepositoryIgnorePolicy.from_repository(source_path)

        for root, dirs, files in os.walk(source_path):
            dirs[:] = ignore_policy.filter_directories(
                root, dirs, built_in_names=excluded_dirs
            )
            files = ignore_policy.filter_files(
                root, files, built_in_names={AUGURIGNORE_FILENAME}
            )
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

        # max_tokens is deliberately absent. sdg_hub's set_model_config writes
        # straight onto the LLM block, so passing it here silently overrode the
        # max_tokens each flow.yaml declares next to its own prompt and made
        # one number govern two flows that ask for different amounts of text.
        # The output budget belongs with the question; everything below is a
        # property of the endpoint rather than of what is being asked.
        flow.set_model_config(
            model=f"{os.getenv('GRAPHRAG_LLM_PROVIDER')}/{os.getenv('GRAPHRAG_LLM_ID')}",
            api_base=os.getenv("GRAPHRAG_LLM_API_BASE"),
            api_key=os.getenv("GRAPHRAG_LLM_TOKEN"),
            temperature=0,
            n=1,
            response_format={"type": "json_object"},
            top_k=1,
            timeout=llm_timeout,
            num_retries=llm_num_retries,
        )

        converted_df = None
        try:
            with timing_span("sdg.generate_batch", "batch"):
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


def load_external_data(source_path: str, ignore_policy=None) -> dict:
    """Loads and merges all JSON files from source_path/.code_metadata/ into a single dict."""
    import os, json
    from utils import code_utils

    code_metadata_dir = os.path.join(source_path, code_utils.CODE_METADATA_DIR)
    result = {}
    ignore_policy = ignore_policy or RepositoryIgnorePolicy.from_repository(source_path)

    if not os.path.isdir(code_metadata_dir):
        if os.path.islink(code_metadata_dir):
            ignore_policy.filter_files(source_path, [code_utils.CODE_METADATA_DIR])
        return result
    if not ignore_policy.filter_directories(source_path, [code_utils.CODE_METADATA_DIR]):
        return result
    if os.path.islink(code_metadata_dir):
        return result

    for root, dirs, files in os.walk(code_metadata_dir):
        dirs[:] = ignore_policy.filter_directories(root, dirs)
        files = ignore_policy.filter_files(root, files)
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


def generate_code_comment(metadata: dict, file_path: str, config=False, external_metadata: dict = None,
                          *, git_repo: str, git_slug: str, language: str, multi_repo: bool = False):
    """Builds a structured text comment from a code file's metadata dictionary.

    The five identity fields are arguments, not lookups into ``metadata``. The
    caller knows all five before the model runs, so reading them back out of
    the model's answer can only lose: a wrong value is undetectable here, and
    two consumers reject it downstream. ``provenance.corpus.HEADER`` requires
    ``multi_repo`` to be exactly ``true`` or ``false`` or the whole header
    fails to parse, and ``get_comment_delimiters_for_language`` raises on a
    language it has no mapping for.

    Everything still read from ``metadata`` is a genuine model answer.
    """
    import os
    from utils import code_utils

    try:

        external_metadata = external_metadata or {}

        lines = []

        header = (f"This file is located at {file_path} "
                  f"from repository url {git_repo}, "
                  f"repository slug {git_slug}, "
                  f"multi_repo {str(multi_repo).lower()}")

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

        # A method may arrive as a bare name from the parser or as an object
        # from a model. An object carrying no name contributes nothing: the
        # alternative is rendering the whole dict into a header the extraction
        # model then reads as a symbol.
        method_names = [
            name for name in (
                method.get('method_name') if isinstance(method, dict) else method
                for method in metadata.get('methods') or []
            ) if name
        ]
        if method_names:
            lines.append(f"\n Methods:")
            lines.extend(f"- {name}" for name in method_names)

        if config:
            extension = os.path.splitext(file_path)[1]
            begin, end = code_utils.get_comment_delimiters_for_file_extension(extension)
        else:
            begin, end = code_utils.get_comment_delimiters_for_language(language)

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
                                 external_metadata: dict = None, source_path: str | None = None):
    """Writes annotated code and flattened metadata files to target_path.

    ``source_path`` is the checkout root. A config file's libraries are read
    from it by the manifest parsers, and without it none are claimed.
    """
    import os
    from pathlib import Path
    from utils import json_utils
    from loaders.default_asset_loader import DefaultAssetLoader
    from provenance import facts, symbols

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
                metadata = {}

            # Unconditional, not a backfill. These five are known before the
            # model is called, so whatever it returned for them is overwritten
            # rather than consulted. The empty-only guards this replaces caught
            # a model that answered nothing and let one that answered wrongly
            # straight through, which is the failure an echoed field produces.
            multi_repo = bool(row.get("multi_repo", False))
            metadata['file_path'] = rel_file_path
            metadata['git_repo'] = git_repo
            metadata['git_slug'] = git_slug
            metadata['language'] = language
            metadata['multi_repo'] = multi_repo

            # The symbol sections, on the same terms. A parser settles what a
            # file declares and imports exactly, so the model is not consulted
            # about it; for a language no parser here reads, nothing is
            # claimed and the two evidence keys say which case this was.
            metadata.update(symbols.sections_for(rel_file_path, code))

            # A config file's libraries, on the same terms again. The manifest
            # parsers read requirements files and pyproject.toml; for anything
            # else, a pom.xml or a package.json, no library line is written,
            # and the extraction model still reads the file itself below.
            if config:
                declared = (
                    facts.declared_packages(Path(source_path, rel_file_path), Path(source_path))
                    if source_path else []
                )
                metadata['libraries'] = [
                    {"library_name": p.name, "library_version": p.constraint or p.version or ""}
                    for p in declared
                ]

            target_file_path = os.path.join(target_path, Path(rel_file_path).with_suffix(".txt"))

            code_header_comment = generate_code_comment(
                metadata=metadata, file_path=rel_file_path, config=config,
                external_metadata=external_metadata,
                git_repo=git_repo, git_slug=git_slug, language=language,
                multi_repo=multi_repo,
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
                            multi_repo: bool = False, external_metadata: dict = None,
                            ignore_policy=None):
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
                                           language=language, config=config, multi_repo=multi_repo,
                                           ignore_policy=ignore_policy)

        if code_df is None:
            logging.info(f"No {language} files found (config={config}).")
            result["status"] = "skipped"
            return

        code_and_metadata_df = code_df
        if purpose_enabled():
            try:
                llm_cm = dur_tracker.measure(stage="Data Generation", step=f"LLM Metadata Extraction ({step_label})") if dur_tracker else nullcontext()
                with llm_cm:
                    code_and_metadata_df = get_parsed_code_metadata(code_df, language=language, config=config)
            except Exception as e:
                logging.warning(
                    f"LLM Metadata Extraction failed for {language} (config={config}): {e}. "
                    f"Falling back to saving raw code files without LLM metadata."
                )

        save_cm = dur_tracker.measure(stage="Data Generation", step=f"Save Metadata Files ({step_label})") if dur_tracker else nullcontext()
        with save_cm:
            save_code_and_metadata_files(code_and_metadata_df, target_path, git_repo=git_repo,
                                         git_slug=git_slug, language=language, config=config,
                                         external_metadata=external_metadata,
                                         source_path=source_path)

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
        try:
            from utils.code_metrics_tracker import CodeMetricsTracker
            c_tr = CodeMetricsTracker.get_instance()
            c_tr.save_to_file(os.path.join(target_path, "project_metrics.json"))
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


def detect_languages(source_path: str, ignore_policy=None) -> list:
    """Returns the list of programming languages detected in source_path."""
    from utils import code_utils

    ignore_policy = ignore_policy or RepositoryIgnorePolicy.from_repository(source_path)
    languages = code_utils.get_detected_languages_for_repo(source_path, ignore_policy)

    if not languages:
        if ignore_policy.has_custom_exclusions:
            raise RepositoryIgnoreError(
                "No supported source or configuration files remain after exclusions."
            )
        raise Exception(f"No languages detected in source_path='{source_path}'.")

    return languages


def should_reraise_processing_error(error: Exception, *, multi_repo: bool) -> bool:
    """Return whether a repository-processing error must fail the task."""
    return not multi_repo or isinstance(error, RepositoryIgnoreError)


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
            from utils.code_metrics_tracker import CodeMetricsTracker
            CodeMetricsTracker.reset_instance()
            code_tracker = CodeMetricsTracker.get_instance(git_slug=git_slug, git_repo=git_repo)
        except Exception:
            code_tracker = None

        try:
            if dur_tracker:
                try:
                    dur_tracker.record_pod_creation_overhead(stage="Data Generation")
                except Exception as e:
                    logging.debug(f"Failed to record pod creation overhead in data generation: {e}")
                with dur_tracker.measure(stage="Data Generation", step="Data Generation Total", metadata={"is_aggregate": True}):
                    prepare_environment(source_path=source_path, target_path=target_path,
                                        git_repo=git_repo, git_branch=git_branch)
                    ignore_policy = RepositoryIgnorePolicy.from_repository(source_path)

                    with dur_tracker.measure(stage="Data Generation", step="Detect Languages"):
                        languages = detect_languages(source_path, ignore_policy)

                    if code_tracker:
                        try:
                            with dur_tracker.measure(stage="Data Generation", step="Measure Code Metrics"):
                                code_tracker.measure_repository(source_path, languages=languages)
                        except Exception as e:
                            logging.debug(f"Code metrics measurement failed: {e}")

                    with dur_tracker.measure(stage="Data Generation", step="Load External Data"):
                        external_metadata = load_external_data(source_path, ignore_policy)

                    for language in languages:
                        for config in [False, True]:
                            generate_code_and_meta(
                                git_repo=git_repo, git_branch=git_branch,
                                language=language, source_path=source_path, target_path=target_path,
                                config=config, multi_repo=multi_repo, external_metadata=external_metadata,
                                ignore_policy=ignore_policy,
                            )
                    ignore_policy.log_summary()
            else:
                prepare_environment(source_path=source_path, target_path=target_path,
                                    git_repo=git_repo, git_branch=git_branch)
                ignore_policy = RepositoryIgnorePolicy.from_repository(source_path)
                languages = detect_languages(source_path, ignore_policy)
                if code_tracker:
                    try:
                        code_tracker.measure_repository(source_path, languages=languages)
                    except Exception as e:
                        logging.debug(f"Code metrics measurement failed: {e}")
                external_metadata = load_external_data(source_path, ignore_policy)
                for language in languages:
                    for config in [False, True]:
                        generate_code_and_meta(
                            git_repo=git_repo, git_branch=git_branch,
                            language=language, source_path=source_path, target_path=target_path,
                            config=config, multi_repo=multi_repo, external_metadata=external_metadata,
                            ignore_policy=ignore_policy,
                        )
                ignore_policy.log_summary()

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

            try:
                from utils.code_metrics_tracker import CodeMetricsTracker
                code_tr = CodeMetricsTracker.get_instance()
                if target_path:
                    code_tr.save_to_file(os.path.join(target_path, "project_metrics.json"))
                code_tr.log_to_mlflow()
                code_tr.upload_to_mlflow(git_slug=git_slug, stage="Data Generation", multi_repo=multi_repo)
                logging.info("\n" + code_tr.format_summary())
            except Exception as e:
                logging.debug("Code metrics handling in data generation: %s", e)

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
