import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

from typing import List

from kfp import dsl
from utils.pipeline_utils import DATA_GENERATION_BASE_IMAGE, get_pip_installable_git_url, inject_git_creds

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

@inject_git_creds(secret_name="git-credentials", username_key="GIT_USERNAME", password_key="GIT_TOKEN")
@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def prepare_environment_op(git_repo: str, git_branch: str, source_path: str, target_path: str):

    from pipelines.base.data_generation import prepare_environment

    prepare_environment(source_path=source_path, target_path=target_path,
                        git_repo=git_repo, git_branch=git_branch)


@inject_git_creds(secret_name="git-credentials", username_key="GIT_USERNAME", password_key="GIT_TOKEN")
@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def generate_git_slug_op(git_repo: str, git_branch: str) -> str:

    from pipelines.base.data_generation import generate_git_slug

    return generate_git_slug(git_repo, git_branch)


@inject_git_creds(secret_name="git-credentials", username_key="GIT_USERNAME", password_key="GIT_TOKEN")
@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def detect_languages_op(source_path: str) -> List[str]:

    from pipelines.base.data_generation import detect_languages

    return detect_languages(source_path)


@inject_git_creds(secret_name="git-credentials", username_key="GIT_USERNAME", password_key="GIT_TOKEN")
@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def generate_code_and_meta_op(git_repo: str, git_branch: str, git_slug: str,
                               language: str, source_path: str, target_path: str):
    """Generates code metadata for a single language (both code and config files)."""

    from pipelines.base.data_generation import generate_code_and_meta

    for config in [False, True]:

        generate_code_and_meta(
            git_repo=git_repo, git_branch=git_branch, git_slug=git_slug,
            language=language, source_path=source_path, target_path=target_path,
            config=config,
        )


##############################################################################
# Pipeline
##############################################################################

@dsl.pipeline(name="data-generation-pipeline")
def run_full_pipeline(
    git_repo: str = os.getenv("GIT_REPO", ""),
    git_branch: str = os.getenv("GIT_BRANCH", "main"),
    source_path: str = os.getenv("SOURCE_PATH", "source"),
    target_path: str = os.getenv("TARGET_PATH", "target"),
):

    prep = prepare_environment_op(
        git_repo=git_repo,
        git_branch=git_branch,
        source_path=source_path,
        target_path=target_path,
    )

    slug = generate_git_slug_op(git_repo=git_repo, git_branch=git_branch)

    languages = detect_languages_op(source_path=source_path).after(prep)

    with dsl.ParallelFor(languages.output) as language:

        generate_code_and_meta_op(
            git_repo=git_repo,
            git_branch=git_branch,
            git_slug=slug.output,
            language=language,
            source_path=source_path,
            target_path=target_path,
        )
