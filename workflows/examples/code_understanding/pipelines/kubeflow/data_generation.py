import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

from kfp import dsl
from kfp.dsl import Dataset, Input, Output
from utils.kubeflow_utils import DATA_GENERATION_BASE_IMAGE, get_pip_installable_git_url, inject_secret_as_env, setup_logging

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
@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def prepare_environment_op(git_repo: str, git_branch: str, source_dir: Output[Dataset]):
    """Clones the repository and archives it as a gzip tarball."""

    setup_logging()
    from pipelines.base.data_generation import prepare_environment
    from utils.kubeflow_utils import write_to_output_artifact, use_ephemeral_space

    with write_to_output_artifact(source_dir) as tmp_source, use_ephemeral_space() as tmp_target:

        prepare_environment(
            source_path=tmp_source,
            target_path=tmp_target,
            git_repo=git_repo,
            git_branch=git_branch,
        )


@inject_secret_as_env(secret_name="code-understanding-env")
@inject_secret_as_env(secret_name="git-credentials")
@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def generate_code_and_meta_op(git_repo: str, git_branch: str,
                               source_dir: Input[Dataset], target_dir: Output[Dataset]):
    """Detects languages and generates code metadata for all detected languages."""

    setup_logging()
    from pipelines.base.data_generation import detect_languages, generate_code_and_meta, generate_git_slug
    from utils.kubeflow_utils import read_from_input_artifact, write_to_output_artifact

    with read_from_input_artifact(source_dir) as tmp_source, write_to_output_artifact(target_dir) as tmp_target:

        git_slug = generate_git_slug(git_repo, git_branch)

        languages = detect_languages(tmp_source)

        for language in languages:

            for config in [False, True]:

                generate_code_and_meta(
                    git_repo=git_repo, git_branch=git_branch, git_slug=git_slug,
                    language=language, source_path=tmp_source, target_path=tmp_target,
                    config=config,
                )


##############################################################################
# Pipeline
##############################################################################

@dsl.pipeline(name="data-generation-pipeline")
def run_full_pipeline(
    git_repo: str = os.getenv("GIT_REPO", ""),
    git_branch: str = os.getenv("GIT_BRANCH", "main"),
) -> Dataset:

    prep = prepare_environment_op(
        git_repo=git_repo,
        git_branch=git_branch,
    )

    gen = generate_code_and_meta_op(
        git_repo=git_repo,
        git_branch=git_branch,
        source_dir=prep.outputs["source_dir"],
    )

    return gen.outputs["target_dir"]
