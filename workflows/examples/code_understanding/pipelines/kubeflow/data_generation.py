import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

from typing import List

from kfp import dsl
from kfp.dsl import Dataset, Input, Output
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
def prepare_environment_op(git_repo: str, git_branch: str, source_path: str, target_path: str,
                            source_dir: Output[Dataset]):
    """Clones the repo to source_path and cleans target_path."""

    import os

    from pipelines.base.data_generation import prepare_environment

    prepare_environment(source_path=source_path, target_path=target_path,
                        git_repo=git_repo, git_branch=git_branch)

    os.makedirs(os.path.dirname(source_dir.path), exist_ok=True)

    with open(source_dir.path, "w") as f:
        f.write(source_path)


@inject_git_creds(secret_name="git-credentials", username_key="GIT_USERNAME", password_key="GIT_TOKEN")
@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def generate_git_slug_op(git_repo: str, git_branch: str) -> str:

    from pipelines.base.data_generation import generate_git_slug

    return generate_git_slug(git_repo, git_branch)


@inject_git_creds(secret_name="git-credentials", username_key="GIT_USERNAME", password_key="GIT_TOKEN")
@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def detect_languages_op(source_dir: Input[Dataset]) -> List[str]:

    from pipelines.base.data_generation import detect_languages

    with open(source_dir.path) as f:
        source_path = f.read().strip()

    return detect_languages(source_path)


@inject_git_creds(secret_name="git-credentials", username_key="GIT_USERNAME", password_key="GIT_TOKEN")
@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def generate_code_and_meta_op(git_repo: str, git_branch: str, git_slug: str,
                               languages: List[str], source_dir: Input[Dataset],
                               target_path: str, target_dir: Output[Dataset]):
    """Generates code metadata for all detected languages."""

    import os

    from pipelines.base.data_generation import generate_code_and_meta

    with open(source_dir.path) as f:
        source_path = f.read().strip()

    for language in languages:

        for config in [False, True]:

            generate_code_and_meta(
                git_repo=git_repo, git_branch=git_branch, git_slug=git_slug,
                language=language, source_path=source_path, target_path=target_path,
                config=config,
            )

    os.makedirs(os.path.dirname(target_dir.path), exist_ok=True)

    with open(target_dir.path, "w") as f:
        f.write(target_path)


##############################################################################
# Pipeline
##############################################################################

@dsl.pipeline(name="data-generation-pipeline")
def run_full_pipeline(
    git_repo: str = os.getenv("GIT_REPO", ""),
    git_branch: str = os.getenv("GIT_BRANCH", "main"),
    source_path: str = os.getenv("SOURCE_PATH", "source"),
    target_path: str = os.getenv("TARGET_PATH", "target"),
) -> Dataset:

    prep = prepare_environment_op(
        git_repo=git_repo,
        git_branch=git_branch,
        source_path=source_path,
        target_path=target_path,
    )

    slug = generate_git_slug_op(git_repo=git_repo, git_branch=git_branch)

    languages = detect_languages_op(source_dir=prep.outputs["source_dir"])

    gen = generate_code_and_meta_op(
        git_repo=git_repo,
        git_branch=git_branch,
        git_slug=slug.output,
        languages=languages.output,
        source_dir=prep.outputs["source_dir"],
        target_path=target_path,
    )

    return gen.outputs["target_dir"]
