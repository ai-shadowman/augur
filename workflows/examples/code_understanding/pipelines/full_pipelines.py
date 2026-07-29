"""KFP pipeline definitions spanning all three pipeline stages, plus the compile entry point.

Run directly to compile all pipelines to YAML:

    PIPELINE_COMPILE_ONLY=1 \\
    KFP_PIPELINE_OUTPUT_DIR=compiled_pipelines \\
    PYTHONPATH=<code_understanding_dir> \\
    python3 pipelines/full_pipelines.py
"""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from typing import NamedTuple

from kfp import dsl
from utils.pipeline_utils import DATA_GENERATION_BASE_IMAGE, compile_all_and_exit, get_pip_installable_git_url, inject_git_creds

_AGENTMESH_INSTALLABLE_URL = get_pip_installable_git_url(
    git_username=os.getenv("GIT_USERNAME"),
    git_token=os.getenv("GIT_TOKEN"),
    repo_url=os.getenv("AGENTMESH_REPO_URL", ""),
    repo_ref=os.getenv("AGENTMESH_REPO_REF", "main"),
    subdirectory="workflows/examples/code_understanding",
)
from loaders.default_asset_loader import DefaultAssetLoader

from utils.pipeline_utils import uses_jupyter_runtime, uses_kfp

if uses_kfp():

    from pipelines.kubeflow.data_generation import run_full_pipeline as data_generation_pipeline
    from pipelines.kubeflow.indexing import run_full_pipeline as graphrag_indexing_pipeline
    from pipelines.kubeflow.analysis import run_full_pipeline as graphrag_analysis_pipeline

else:

    from pipelines.base.data_generation import run_full_pipeline as data_generation_pipeline
    from pipelines.base.indexing import run_full_pipeline as graphrag_indexing_pipeline
    from pipelines.base.analysis import run_full_pipeline as graphrag_analysis_pipeline

##############################################################################
# Shared defaults
##############################################################################

_DEFAULT_PARENT_SOURCE_PATH = os.getenv("SOURCE_PATH", "source")
_DEFAULT_PARENT_TARGET_PATH = os.getenv("TARGET_PATH", "target")
_DEFAULT_GRAPHRAG_BASE_PATH = "graph_rag_app/source"

##############################################################################
# Multi-repo pipeline repo list
##############################################################################

_GIT_REPOS = DefaultAssetLoader().download("repos/repo_list.json")

@inject_git_creds(secret_name="git-credentials", username_key="GIT_USERNAME", password_key="GIT_TOKEN")
@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def inject_pipeline_params_op(
    git_repo: str,
    git_branch: str,
    parent_source_path: str,
    parent_target_path: str,
    graphrag_base_path: str,
) -> NamedTuple("Outputs", [("source_path", str), ("target_path", str), ("graphrag_source_path", str)]):

    from collections import namedtuple

    from utils import code_utils

    repo_slug = code_utils.generate_slug_from_repo(git_repo, git_branch)

    Outputs = namedtuple("Outputs", ["source_path", "target_path", "graphrag_source_path"])

    return Outputs(
        source_path=f"{parent_source_path}/{repo_slug}",
        target_path=f"{parent_target_path}/{repo_slug}",
        graphrag_source_path=f"{graphrag_base_path}/{repo_slug}",
    )


##############################################################################
# Pipeline definitions
##############################################################################

@dsl.pipeline(name="single-repo-pipeline")
def single_repo_pipeline(
    git_repo: str = os.getenv("GIT_REPO", ""),
    git_branch: str = os.getenv("GIT_BRANCH", "main"),
    parent_source_path: str = _DEFAULT_PARENT_SOURCE_PATH,
    parent_target_path: str = _DEFAULT_PARENT_TARGET_PATH,
    graphrag_base_path: str = _DEFAULT_GRAPHRAG_BASE_PATH,
):

    params = inject_pipeline_params_op(
        git_repo=git_repo,
        git_branch=git_branch,
        parent_source_path=parent_source_path,
        parent_target_path=parent_target_path,
        graphrag_base_path=graphrag_base_path,
    )

    dg = data_generation_pipeline(
        git_repo=git_repo,
        git_branch=git_branch,
        source_path=params.outputs["source_path"],
        target_path=params.outputs["target_path"],
    )

    idx = graphrag_indexing_pipeline(
        codebase_path=dg.output,
        graphrag_source_path=params.outputs["graphrag_source_path"],
    )

    graphrag_analysis_pipeline(
        graphrag_source_path=idx.output,
    )


@dsl.pipeline(name="multi-repo-pipeline")
def multi_repo_pipeline(
    parent_source_path: str = _DEFAULT_PARENT_SOURCE_PATH,
    parent_target_path: str = _DEFAULT_PARENT_TARGET_PATH,
    graphrag_base_path: str = _DEFAULT_GRAPHRAG_BASE_PATH,
):

    with dsl.ParallelFor(items=_GIT_REPOS) as repo:

        single_repo_pipeline(
            git_repo=repo.git_repo,
            git_branch=repo.git_branch,
            parent_source_path=parent_source_path,
            parent_target_path=parent_target_path,
            graphrag_base_path=graphrag_base_path,
        )


##############################################################################
# Compile entry point
##############################################################################

if __name__ == "__main__":

    compile_all_and_exit({
        "data_generation": data_generation_pipeline,
        "single_repo":     single_repo_pipeline,
        "multi_repo":      multi_repo_pipeline,
        "indexing":        graphrag_indexing_pipeline,
        "analysis":        graphrag_analysis_pipeline,
    })
