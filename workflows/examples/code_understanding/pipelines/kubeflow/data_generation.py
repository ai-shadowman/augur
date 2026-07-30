import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

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
def prepare_environment_op(git_repo: str, git_branch: str, source_dir: Output[Dataset]):
    """Clones the repository and archives it as a gzip tarball."""

    import os
    import shutil
    import tarfile
    import tempfile

    from pipelines.base.data_generation import prepare_environment

    tmp_source = tempfile.mkdtemp()
    tmp_target = tempfile.mkdtemp()

    try:

        prepare_environment(
            source_path=tmp_source,
            target_path=tmp_target,
            git_repo=git_repo,
            git_branch=git_branch,
        )

        os.makedirs(os.path.dirname(source_dir.path), exist_ok=True)

        # compresslevel=1: fastest gzip, ~3-5x quicker than default at ~5-10% size cost.
        with tarfile.open(source_dir.path, "w:gz", compresslevel=1) as tar:
            tar.add(tmp_source, arcname=".")

    finally:

        shutil.rmtree(tmp_source, ignore_errors=True)
        shutil.rmtree(tmp_target, ignore_errors=True)


@inject_git_creds(secret_name="git-credentials", username_key="GIT_USERNAME", password_key="GIT_TOKEN")
@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def generate_code_and_meta_op(git_repo: str, git_branch: str,
                               source_dir: Input[Dataset], target_dir: Output[Dataset]):
    """Detects languages and generates code metadata for all detected languages."""

    import os
    import shutil
    import tarfile
    import tempfile

    from pipelines.base.data_generation import detect_languages, generate_code_and_meta, generate_git_slug

    tmp_source = tempfile.mkdtemp()
    tmp_target = tempfile.mkdtemp()

    try:

        with tarfile.open(source_dir.path, "r:gz") as tar:
            tar.extractall(tmp_source)

        git_slug = generate_git_slug(git_repo, git_branch)

        languages = detect_languages(tmp_source)

        for language in languages:

            for config in [False, True]:

                generate_code_and_meta(
                    git_repo=git_repo, git_branch=git_branch, git_slug=git_slug,
                    language=language, source_path=tmp_source, target_path=tmp_target,
                    config=config,
                )

        os.makedirs(os.path.dirname(target_dir.path), exist_ok=True)

        with tarfile.open(target_dir.path, "w:gz", compresslevel=1) as tar:
            tar.add(tmp_target, arcname=".")

    finally:

        shutil.rmtree(tmp_source, ignore_errors=True)
        shutil.rmtree(tmp_target, ignore_errors=True)


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
