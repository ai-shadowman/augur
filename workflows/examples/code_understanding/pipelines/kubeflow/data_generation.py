import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

from kfp import dsl
from kfp.dsl import Dataset, Input, Output
from utils.kubeflow_utils import DATA_GENERATION_BASE_IMAGE, get_pip_installable_git_url, inject_secret_as_env

_AGENTMESH_INSTALLABLE_URL = get_pip_installable_git_url(
    git_username=os.getenv("GIT_USERNAME"),
    git_token=os.getenv("GIT_TOKEN"),
    repo_url=os.getenv("AGENTMESH_REPO_URL", ""),
    repo_ref=os.getenv("AGENTMESH_REPO_REF", "main"),
    subdirectory="workflows/examples/code_understanding",
)


##############################################################################
# Components
##############################################################################

@inject_secret_as_env(secret_name="git-credentials")
@dsl.component(
    base_image=DATA_GENERATION_BASE_IMAGE, 
    packages_to_install=[_AGENTMESH_INSTALLABLE_URL] if _AGENTMESH_INSTALLABLE_URL else None,
    pip_index_urls=[os.getenv("PYPI_MIRROR")] if os.getenv("PYPI_MIRROR") else None
)
def prepare_environment_op(git_repo: str, git_branch: str, source_dir: Output[Dataset]):
    """Clones the repository and archives it as a gzip tarball."""

    from pipelines.base.data_generation import prepare_environment
    from utils.kubeflow_utils import setup_logging, write_to_output_artifact, use_ephemeral_space
    setup_logging()

    with write_to_output_artifact(source_dir) as tmp_source, use_ephemeral_space() as tmp_target:

        prepare_environment(
            source_path=tmp_source,
            target_path=tmp_target,
            git_repo=git_repo,
            git_branch=git_branch,
        )


@inject_secret_as_env(secret_name="code-understanding-env")
@inject_secret_as_env(secret_name="git-credentials")
@dsl.component(
    base_image=DATA_GENERATION_BASE_IMAGE, 
    packages_to_install=[_AGENTMESH_INSTALLABLE_URL] if _AGENTMESH_INSTALLABLE_URL else None,
    pip_index_urls=[os.getenv("PYPI_MIRROR")] if os.getenv("PYPI_MIRROR") else None
)
def generate_code_and_meta_op(git_repo: str, git_branch: str,
                               source_dir: Input[Dataset], target_dir: Output[Dataset],
                               multi_repo: bool = False):
    """Detects languages and generates code metadata for all detected languages."""

    from pipelines.base.data_generation import (
        detect_languages, generate_code_and_meta, generate_git_slug
    )
    from utils.kubeflow_utils import setup_logging, read_from_input_artifact, write_to_output_artifact
    setup_logging()

    import logging

    with read_from_input_artifact(source_dir) as tmp_source, write_to_output_artifact(target_dir) as tmp_target:

        try:

            from pipelines.base.data_generation import load_external_data

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
                f"Skipping repo '{git_repo}' (branch='{git_branch}'): {e}"
            )


@inject_secret_as_env(secret_name="code-understanding-env")
@dsl.component(
    base_image=DATA_GENERATION_BASE_IMAGE, 
    packages_to_install=[_AGENTMESH_INSTALLABLE_URL] if _AGENTMESH_INSTALLABLE_URL else None,
    pip_index_urls=[os.getenv("PYPI_MIRROR")] if os.getenv("PYPI_MIRROR") else None
)
def get_repo_list_op() -> list:
    """Downloads and returns the repo list from the asset loader."""

    from loaders.default_asset_loader import DefaultAssetLoader
    from utils.kubeflow_utils import setup_logging

    import os
    import json
    
    setup_logging()
    import logging
    logging.info(os.getenv("GIT_REPO_LIST_CONTENTS"))

    return json.loads(os.getenv("GIT_REPO_LIST_CONTENTS"))

    #return DefaultAssetLoader().download("repos/repo_list.json")


##############################################################################
# Pipelines
##############################################################################

@dsl.pipeline(name="data-generation-pipeline")
def _run_pipeline(
    git_repo: str = os.getenv("GIT_REPO", ""),
    git_branch: str = os.getenv("GIT_BRANCH", "main"),
    multi_repo: bool = False,
) -> Dataset:

    prep = prepare_environment_op(
        git_repo=git_repo,
        git_branch=git_branch,
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
                         parallelispm=int(os.getenv("GRAPHRAG_PARALLEL_REPOS", "2"))) as repo:

        _run_pipeline(
            git_repo=repo.git_repo,
            git_branch=repo.git_branch,
            multi_repo=True,
        )


##############################################################################
# Pipeline stage
##############################################################################

class DataGenerationPipeline:
    run = staticmethod(_run_pipeline)
    run_multi_repo = staticmethod(_run_pipeline_multi_repo)
