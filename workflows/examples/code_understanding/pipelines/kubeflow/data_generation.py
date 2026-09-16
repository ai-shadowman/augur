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
    source_dir: Output[Dataset],
    pipeline_name: str = "data-generation-pipeline"):
    """Clones the repository and archives it as a gzip tarball."""

    from pipelines.base.data_generation import prepare_environment
    from utils.kubeflow_utils import setup_logging, write_to_output_artifact, use_ephemeral_space
    from utils.metrics_tracker import PipelineMetricsTracker
    setup_logging()

    with write_to_output_artifact(source_dir) as tmp_source, use_ephemeral_space() as tmp_target:
        tracker = PipelineMetricsTracker(pipeline_name, multi_repo=False)
        tracker.start_stage("Data Generation")
        with tracker.track_step("prepare_environment", stage="Data Generation"):
            prepare_environment(
                source_path=tmp_source,
                target_path=tmp_target,
                git_repo=git_repo,
                git_branch=git_branch,
                git_username=git_username,
                git_token=git_token,
            )
        tracker.save_and_log(
            target_dir=tmp_source,
            git_repo=git_repo,
            git_branch=git_branch,
            multi_repo=False,
        )


@inject_secret_as_env(secret_name="code-understanding-env")
@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def generate_code_and_meta_op(
    git_repo: str, 
    git_branch: str,
    source_dir: Input[Dataset], 
    target_dir: Output[Dataset],
    multi_repo: bool = False,
    pipeline_name: str = "data-generation-pipeline"):
    """Detects languages and generates code metadata for all detected languages."""

    from pipelines.base.data_generation import (
        detect_languages, generate_code_and_meta, generate_git_slug
    )
    from utils.kubeflow_utils import setup_logging, read_from_input_artifact, write_to_output_artifact
    from utils.metrics_tracker import PipelineMetricsTracker
    setup_logging()

    import logging

    with read_from_input_artifact(source_dir) as tmp_source, write_to_output_artifact(target_dir) as tmp_target:
        default_name = pipeline_name or ("single-repo-pipeline" if not multi_repo else "multi-repo-pipeline")
        tracker = PipelineMetricsTracker.load_or_create(
            search_paths=[tmp_source],
            pipeline_name=default_name,
            multi_repo=multi_repo,
            git_repo=git_repo,
            git_branch=git_branch,
        )
        tracker.start_stage("Data Generation")

        try:

            from pipelines.base.data_generation import load_external_data

            with tracker.track_step("load_external_data", stage="Data Generation"):
                external_metadata = load_external_data(tmp_source)

            with tracker.track_step("detect_languages", stage="Data Generation"):
                languages = detect_languages(tmp_source)

            for language in languages:

                for config in [False, True]:

                    step_desc = f"generate_code_and_meta ({language}{' config' if config else ''})"
                    with tracker.track_step(
                        step_desc,
                        stage="Data Generation",
                        details={"language": language, "config": config},
                    ):
                        generate_code_and_meta(
                            git_repo=git_repo, git_branch=git_branch,
                            language=language, source_path=tmp_source, target_path=tmp_target,
                            config=config, multi_repo=multi_repo,
                            external_metadata=external_metadata,
                        )

            tracker.stop_stage("Data Generation", status="COMPLETED")
            tracker.save_and_log(
                target_dir=tmp_target,
                git_repo=git_repo,
                git_branch=git_branch,
                multi_repo=multi_repo,
            )

        except Exception as e:
            tracker.stop_stage("Data Generation", status="FAILED", error_message=str(e))
            tracker.save_and_log(
                target_dir=tmp_target,
                git_repo=git_repo,
                git_branch=git_branch,
                multi_repo=multi_repo,
            )

            if type(e).__name__ == "RateLimitError" or "429" in str(e):
                logging.error(
                    f"Rate limit exceeded for repo '{git_repo}' (branch='{git_branch}'). "
                    f"Consider reducing GRAPHRAG_PARALLEL_REPOS: {e}"
                )
                raise

            logging.error(
                f"Skipping repo '{git_repo}' (branch='{git_branch}'): {e}"
            )


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
    git_username: str = "",
    git_token: str = "",
    multi_repo: bool = False,
    pipeline_name: str = "data-generation-pipeline",
) -> Dataset:

    prep = prepare_environment_op(
        git_repo=git_repo,
        git_branch=git_branch,
        git_username=git_username,
        git_token=git_token,
        pipeline_name=pipeline_name,
    )

    gen = generate_code_and_meta_op(
        git_repo=git_repo,
        git_branch=git_branch,
        source_dir=prep.outputs["source_dir"],
        multi_repo=multi_repo,
        pipeline_name=pipeline_name,
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
