import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

from kfp import dsl
from kfp.dsl import Dataset, Input, Metrics, Output
from utils.kubeflow_utils import get_pip_installable_git_url, INDEXING_BASE_IMAGE, inject_secret_as_env

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

@inject_secret_as_env(secret_name="code-understanding-env")
@inject_secret_as_env(secret_name="git-credentials")
@dsl.component(base_image=INDEXING_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def graphrag_indexing_op(codebase_dir: Input[Dataset],
                          graphrag_dir: Output[Dataset], result: Output[Metrics],
                          git_repo: str = "", git_branch: str = "", multi_repo: bool = False):

    from pipelines.base.indexing import generate_graphrag_index
    from utils.kubeflow_utils import setup_logging, read_from_input_artifact, write_to_output_artifact
    setup_logging()

    with read_from_input_artifact(codebase_dir) as tmp_codebase, \
         write_to_output_artifact(graphrag_dir) as tmp_graphrag:

        generate_graphrag_index(
            codebase_path=tmp_codebase,
            graphrag_source_path=tmp_graphrag,
            git_repo=git_repo,
            git_branch=git_branch,
            multi_repo=multi_repo,
        )

        result.log_metric("success", 1)


@inject_secret_as_env(secret_name="code-understanding-env")
@inject_secret_as_env(secret_name="git-credentials")
@dsl.component(base_image=INDEXING_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def graphrag_evaluation_op(graphrag_dir: Input[Dataset],
                            git_repo: str = "", git_branch: str = "",
                            multi_repo: bool = False):

    from pipelines.base.indexing import evaluate_graphrag_index
    from utils.kubeflow_utils import setup_logging, read_from_input_artifact
    setup_logging()

    with read_from_input_artifact(graphrag_dir) as tmp_graphrag:

        evaluate_graphrag_index(
            graphrag_source_path=tmp_graphrag,
            git_repo=git_repo,
            git_branch=git_branch,
            multi_repo=multi_repo,
        )


@inject_secret_as_env(secret_name="code-understanding-env")
@dsl.component(base_image=INDEXING_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def get_eval_repo_list_op(git_repo: str, git_branch: str, multi_repo: bool) -> list:
    """Returns the list of repos to evaluate: all repos if multi_repo, otherwise just the given repo."""

    from loaders.default_asset_loader import DefaultAssetLoader
    from utils.kubeflow_utils import setup_logging
    setup_logging()

    if multi_repo:
        return DefaultAssetLoader().download("repos/repo_list.json")
    else:
        return [{"git_repo": git_repo, "git_branch": git_branch}]


@inject_secret_as_env(secret_name="code-understanding-env")
@inject_secret_as_env(secret_name="git-credentials")
@dsl.component(base_image=INDEXING_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def run_indexing_multi_repo_op(parent_target_path: str):
    """Runs GraphRAG indexing and evaluation across the combined multi-repo codebase."""

    from pipelines.base.indexing import run_full_pipeline_multi_repo
    from utils.kubeflow_utils import setup_logging
    setup_logging()

    run_full_pipeline_multi_repo(parent_target_path)


##############################################################################
# Pipeline
##############################################################################

@dsl.pipeline(name="graphrag-indexing-pipeline")
def run_full_pipeline(
    codebase_dir: Input[Dataset],
    git_repo: str = "",
    git_branch: str = "",
    multi_repo: bool = False,
) -> Dataset:

    task = graphrag_indexing_op(
        codebase_dir=codebase_dir,
        git_repo=git_repo,
        git_branch=git_branch,
        multi_repo=multi_repo,
    )

    repo_list_task = get_eval_repo_list_op(
        git_repo=git_repo,
        git_branch=git_branch,
        multi_repo=multi_repo,
    )

    with dsl.ParallelFor(items=repo_list_task.output) as repo:

        graphrag_evaluation_op(
            graphrag_dir=task.outputs["graphrag_dir"],
            git_repo=repo.git_repo,
            git_branch=repo.git_branch,
            multi_repo=multi_repo,
        )

    return task.outputs["graphrag_dir"]
