import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

from kfp import dsl
from kfp.dsl import Dataset, Input, Output
from utils.kubeflow_utils import DATA_GENERATION_BASE_IMAGE, get_pip_installable_git_url, inject_secret_as_env

_AGENTMESH_INSTALLABLE_URL = get_pip_installable_git_url(
    git_username=os.getenv("AUGUR_GIT_REPO_USERNAME"),
    git_token=os.getenv("AUGUR_GIT_REPO_TOKEN"),
    repo_url=os.getenv("AUGUR_GIT_REPO_URL", ""),
    repo_ref=os.getenv("AUGUR_GIT_REPO_BRANCH", "main"),
    subdirectory="workflows/examples/code_understanding",
)

##############################################################################
# Components
##############################################################################

@inject_secret_as_env(secret_name="code-understanding-env")
@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def prepare_environment_op(git_repo: str, 
    git_branch: str, 
    git_username: str, 
    git_token: str, 
    source_dir: Output[Dataset]):
    """Clones the repository and archives it as a gzip tarball."""

    from pipelines.base.data_generation import prepare_environment, generate_git_slug
    from utils.kubeflow_utils import setup_logging, write_to_output_artifact, use_ephemeral_space
    setup_logging()
    import logging, os

    with write_to_output_artifact(source_dir) as tmp_source, use_ephemeral_space() as tmp_target:
        git_slug = generate_git_slug(git_repo, git_branch) if git_repo else None

        dur_tracker = None
        try:
            from utils.duration_tracker import DurationTracker
            dur_tracker = DurationTracker.get_instance()
        except Exception as e:
            logging.debug(f"DurationTracker initialization skipped in prepare_environment_op: {e}")

        prepare_environment(
            source_path=tmp_source,
            target_path=tmp_target,
            git_repo=git_repo,
            git_branch=git_branch,
            git_username=git_username,
            git_token=git_token,
        )

        if dur_tracker:
            try:
                dur_tracker.save_to_file(os.path.join(tmp_source, "durations.json"))
            except Exception as e:
                logging.debug(f"Failed to save durations.json in prepare_environment_op: {e}")
            try:
                dur_tracker.upload_to_mlflow(git_slug=git_slug, stage="Data Generation")
            except Exception as e:
                logging.debug(f"Failed to upload durations to MLflow in prepare_environment_op: {e}")

        try:
            from utils.token_tracker import TokenCostTracker
            TokenCostTracker.reset_instance()
            token_tracker = TokenCostTracker.get_instance()
            token_tracker.save_to_file(os.path.join(tmp_source, "tokens.json"))
            token_tracker.upload_to_mlflow(git_slug=git_slug, stage="Data Generation")
        except Exception as e:
            logging.debug(f"TokenCostTracker handling in prepare_environment_op: {e}")


@inject_secret_as_env(secret_name="code-understanding-env")
@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def generate_code_and_meta_op(
    git_repo: str, 
    git_branch: str,
    source_dir: Input[Dataset], 
    target_dir: Output[Dataset],
    multi_repo: bool = False):
    """Detects languages and generates code metadata for all detected languages."""

    from pipelines.base.data_generation import (
        detect_languages, generate_code_and_meta, generate_git_slug
    )
    from utils.kubeflow_utils import setup_logging, read_from_input_artifact, write_to_output_artifact
    setup_logging()

    import logging, os

    with read_from_input_artifact(source_dir) as tmp_source, write_to_output_artifact(target_dir) as tmp_target:
        git_slug = generate_git_slug(git_repo, git_branch) if git_repo else None

        dur_tracker = None
        try:
            from utils.duration_tracker import DurationTracker
            dur_tracker = DurationTracker.get_instance()
            try:
                dur_tracker.download_from_mlflow(git_slug=git_slug, multi_repo=multi_repo)
            except Exception as e:
                logging.debug(f"MLflow download durations skipped in generate_code_and_meta_op: {e}")
            dur_file = os.path.join(tmp_source, "durations.json")
            if os.path.exists(dur_file):
                dur_tracker.load_and_merge(dur_file)
        except Exception as e:
            logging.debug(f"DurationTracker handling in generate_code_and_meta_op: {e}")

        token_tracker = None
        try:
            from utils.token_tracker import TokenCostTracker
            TokenCostTracker.reset_instance()
            token_tracker = TokenCostTracker.get_instance()
            token_tracker.enable_litellm_callbacks(category="Data Generation")
            token_tracker.enable_openai_tracking(category="Data Generation")
            tokens_file = os.path.join(tmp_source, "tokens.json")
            if os.path.exists(tokens_file):
                token_tracker.merge(TokenCostTracker.load_from_file(tokens_file))
        except Exception as e:
            logging.debug(f"TokenCostTracker handling in generate_code_and_meta_op: {e}")

        try:
            from pipelines.base.data_generation import load_external_data

            # Immediately write initial telemetry files to tmp_target
            if dur_tracker:
                try:
                    dur_tracker.save_to_file(os.path.join(tmp_target, "durations.json"))
                except Exception:
                    pass
            if token_tracker:
                try:
                    token_tracker.save_to_file(os.path.join(tmp_target, "tokens.json"))
                except Exception:
                    pass

            if dur_tracker:
                with dur_tracker.measure(stage="Data Generation", step="Load External Data"):
                    external_metadata = load_external_data(tmp_source)

                with dur_tracker.measure(stage="Data Generation", step="Detect Languages"):
                    languages = detect_languages(tmp_source)

                for language in languages:
                    for config in [False, True]:
                        generate_code_and_meta(
                            git_repo=git_repo, git_branch=git_branch,
                            language=language, source_path=tmp_source, target_path=tmp_target,
                            config=config, multi_repo=multi_repo,
                            external_metadata=external_metadata,
                        )
            else:
                external_metadata = load_external_data(tmp_source)
                languages = detect_languages(tmp_source)

                for language in languages:
                    for config in [False, True]:
                        generate_code_and_meta(
                            git_repo=git_repo, git_branch=git_branch,
                            language=language, source_path=tmp_source, target_path=tmp_target,
                            config=config, multi_repo=multi_repo,
                            external_metadata=external_metadata,
                        )
        except Exception as e:
            if type(e).__name__ == "RateLimitError" or "429" in str(e):
                logging.error(
                    f"Rate limit exceeded for repo '{git_repo}' (branch='{git_branch}'). "
                    f"Consider reducing GRAPHRAG_PARALLEL_REPOS: {e}"
                )
                raise
            logging.error(
                f"Error processing repo '{git_repo}' (branch='{git_branch}'): {e}"
            )
            if not multi_repo:
                raise
        else:
            has_txt = any(f.endswith(".txt") for _, _, files in os.walk(tmp_target) for f in files)
            if not has_txt:
                err_msg = f"No text or code files were generated in target directory for repo '{git_repo}'."
                logging.error(err_msg)
                if not multi_repo:
                    raise RuntimeError(err_msg)
        finally:
            if dur_tracker:
                try:
                    dur_tracker.save_to_file(os.path.join(tmp_target, "durations.json"))
                except Exception as e:
                    logging.debug(f"Failed to save durations.json in generate_code_and_meta_op: {e}")
                try:
                    dur_tracker.upload_to_mlflow(git_slug=git_slug, stage="Data Generation", multi_repo=multi_repo)
                except Exception as e:
                    logging.debug(f"Failed to upload durations to MLflow in generate_code_and_meta_op: {e}")
                try:
                    summary = dur_tracker.format_summary()
                    logging.info("\n" + summary)
                    print("\n" + summary, flush=True)
                except Exception as e:
                    logging.debug(f"Failed to print duration summary in data generation pod: {e}")

            if token_tracker:
                try:
                    token_tracker.save_to_file(os.path.join(tmp_target, "tokens.json"))
                except Exception as e:
                    logging.debug(f"Failed to save tokens.json in generate_code_and_meta_op: {e}")
                try:
                    token_tracker.upload_to_mlflow(git_slug=git_slug, stage="Data Generation", multi_repo=multi_repo)
                except Exception as e:
                    logging.debug(f"Failed to upload tokens to MLflow in generate_code_and_meta_op: {e}")
                try:
                    summary = token_tracker.format_summary()
                    logging.info("\n" + summary)
                    print("\n" + summary, flush=True)
                except Exception as e:
                    logging.debug(f"Failed to print token summary in data generation pod: {e}")


@inject_secret_as_env(secret_name="code-understanding-env")
@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def get_repo_list_op() -> list:
    """Downloads and returns the repo list from the asset loader."""

    from loaders.default_asset_loader import DefaultAssetLoader
    from utils.kubeflow_utils import setup_logging

    import os
    import json
    
    setup_logging()
    import logging
    logging.info(os.getenv("GIT_REPO_LIST_CONTENTS"))

    repos = json.loads(os.getenv("GIT_REPO_LIST_CONTENTS"))

    # Ensure missing optional fields exist in every item before sending to KFP ParallelFor
    for repo in repos:
        repo.setdefault("git_branch", "main")
        repo.setdefault("git_username", "")
        repo.setdefault("git_token", "")

    return repos    

    #return DefaultAssetLoader().download("repos/repo_list.json")


##############################################################################
# Pipelines
##############################################################################

@dsl.pipeline(name="data-generation-pipeline")
def _run_pipeline(
    git_repo: str = os.getenv("GIT_REPO", ""),
    git_branch: str = os.getenv("GIT_BRANCH", "main"),
    git_username: str = os.getenv("GIT_USERNAME", ""),
    git_token: str = os.getenv("GIT_TOKEN", ""),
    multi_repo: bool = False,
) -> Dataset:

    prep = prepare_environment_op(
        git_repo=git_repo,
        git_branch=git_branch,
        git_username=git_username,
        git_token=git_token
    )

    gen = generate_code_and_meta_op(
        git_repo=git_repo,
        git_branch=git_branch,
        source_dir=prep.outputs["source_dir"],
        multi_repo=multi_repo,
    )

    return gen.outputs["target_dir"]

@dsl.pipeline(name="data-generation-multi-repo-pipeline")
def _run_pipeline_multi_repo():
    """Generates code metadata for all repositories in the asset-loader repo list."""

    repo_list_task = get_repo_list_op()

    with dsl.ParallelFor(items=repo_list_task.output,
                         parallelism=int(os.getenv("GRAPHRAG_PARALLEL_REPOS", "2"))) as repo:

        _run_pipeline(
            git_repo=repo.git_repo,
            git_branch=repo.git_branch,
            git_username=repo.git_username,
            git_token=repo.git_token,
            multi_repo=True,
        )


##############################################################################
# Pipeline stage
##############################################################################

class DataGenerationPipeline:
    run = staticmethod(_run_pipeline)
    run_multi_repo = staticmethod(_run_pipeline_multi_repo)
