import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

from kfp import dsl
from kfp.dsl import Output, Input, Dataset, Artifact
from utils.pipeline_utils import DATA_GENERATION_BASE_IMAGE


##############################################################################
# Components
##############################################################################

@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE)
def clone_from_repo_op(repo_url: str, destination_path: str, branch: str = "master"):

    from pipelines.base.data_generation import clone_from_repo

    clone_from_repo(repo_url, destination_path, branch)


@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE)
def prepare_environment_op(git_repo: str, git_branch: str, source_path: str, target_path: str):

    from pipelines.base.data_generation import prepare_environment

    prepare_environment(source_path=source_path, target_path=target_path,
                        git_repo=git_repo, git_branch=git_branch)


@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE)
def generate_raw_dataset_op(source_path: str, target_path: str, git_repo: str,
                             git_slug: str, result: Output[Dataset],
                             language: str = "python", config: bool = False,
                             multi_repo: bool = False):

    from pipelines.base.data_generation import generate_raw_dataset

    df = generate_raw_dataset(source_path, target_path, git_repo, git_slug,
                              language=language, config=config, multi_repo=multi_repo)

    if df is not None:

        df.to_parquet(result.path)


@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE)
def get_parsed_code_metadata_op(raw_data: Input[Dataset], result: Output[Dataset],
                                 language: str = "python", config: bool = False):

    import pandas as pd

    from pipelines.base.data_generation import get_parsed_code_metadata

    df = pd.read_parquet(raw_data.path)

    result_df = get_parsed_code_metadata(df, language=language, config=config)

    result_df.to_parquet(result.path)


@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE)
def generate_code_comment_op(metadata_json: str, file_path: str,
                              config: bool = False) -> str:

    import json

    from pipelines.base.data_generation import generate_code_comment

    metadata = json.loads(metadata_json)

    return generate_code_comment(metadata=metadata, file_path=file_path, config=config)


@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE)
def save_code_and_metadata_files_op(target_path: str, data: Input[Dataset],
                                     config: bool = False):

    import pandas as pd

    from pipelines.base.data_generation import save_code_and_metadata_files

    df = pd.read_parquet(data.path)

    save_code_and_metadata_files(df, target_path, config=config)


@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE)
def generate_code_and_meta_op(git_repo: str, git_branch: str,
                               source_path: str, target_path: str):
    """Detects all languages and generates code metadata."""

    from pipelines.base.data_generation import generate_all_code_and_meta

    generate_all_code_and_meta(git_repo=git_repo, git_branch=git_branch,
                               source_path=source_path, target_path=target_path)


@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE)
def generate_all_code_and_meta_op(git_repo: str, git_branch: str,
                                   source_path: str, target_path: str,
                                   git_slug: str = "", multi_repo: bool = False):

    from pipelines.base.data_generation import generate_all_code_and_meta

    generate_all_code_and_meta(git_repo=git_repo, git_branch=git_branch,
                               source_path=source_path, target_path=target_path,
                               git_slug=git_slug or None, multi_repo=multi_repo)


@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE)
def run_single_repo_pipeline_op(git_repo: str, git_branch: str, source_path: str,
                                 target_path: str, result: Output[Artifact],
                                 git_slug: str = "", multi_repo: bool = False):

    import json

    from pipelines.base.data_generation import run_full_pipeline as _run

    pipeline_result = _run(git_repo=git_repo, git_branch=git_branch,
                           source_path=source_path, target_path=target_path,
                           git_slug=git_slug or None, multi_repo=multi_repo)

    with open(result.path, "w") as f:

        json.dump(pipeline_result, f)


@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE)
def run_multi_repo_op(git_repos_json: str, result: Output[Artifact]):

    import json

    from pipelines.base.data_generation import run_full_pipeline_multi_repo

    git_repos = json.loads(git_repos_json)

    pipeline_results = run_full_pipeline_multi_repo(git_repos)

    with open(result.path, "w") as f:

        json.dump(pipeline_results, f)


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

    generate_code_and_meta_op(
        git_repo=git_repo,
        git_branch=git_branch,
        source_path=source_path,
        target_path=target_path,
    ).after(prep)
