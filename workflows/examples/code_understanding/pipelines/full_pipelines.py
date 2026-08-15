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

from kfp import dsl
from utils.kubeflow_utils import compile_all_and_exit

from utils.pipeline_utils import uses_kfp

if uses_kfp():

    from pipelines.kubeflow.data_generation import run_full_pipeline as data_generation_pipeline
    from pipelines.kubeflow.indexing import run_full_pipeline as graphrag_indexing_pipeline
    from pipelines.kubeflow.analysis import run_full_pipeline as graphrag_analysis_pipeline

else:

    from pipelines.base.data_generation import run_full_pipeline as data_generation_pipeline
    from pipelines.base.indexing import run_full_pipeline as graphrag_indexing_pipeline
    from pipelines.base.analysis import run_full_pipeline as graphrag_analysis_pipeline

##############################################################################
# Pipeline definitions
##############################################################################

@dsl.pipeline(name="single-repo-pipeline")
def single_repo_pipeline(
    git_repo: str = os.getenv("GIT_REPO", ""),
    git_branch: str = os.getenv("GIT_BRANCH", "main"),
    parent_source_path: str = os.getenv("PARENT_SOURCE_PATH", "source"),
    parent_target_path: str = os.getenv("PARENT_TARGET_PATH", "target"),
    multi_repo: bool = False,
):

    if uses_kfp():

        dg = data_generation_pipeline(
            git_repo=git_repo,
            git_branch=git_branch,
            multi_repo=multi_repo,
        )

        idx = graphrag_indexing_pipeline(
            codebase_dir=dg.output,
            git_repo=git_repo,
            git_branch=git_branch,
            multi_repo=multi_repo,
        )

        graphrag_analysis_pipeline(
            graphrag_dir=idx.output,
            git_repo=git_repo,
            git_branch=git_branch,
            multi_repo=multi_repo,
        )

    else:

        from pipelines.base.data_generation import generate_git_slug as _gen_slug

        git_slug = _gen_slug(git_repo, git_branch)

        source_path = f"{parent_source_path}/{git_slug}"
        target_path = f"{parent_target_path}/{git_slug}"
        graphrag_source_path = os.path.join(
            os.getenv("KFP_DATA_INDEXING_OUTPUT_PATH", "graph_rag_app/source"), git_slug
        )

        data_generation_pipeline(
            git_repo=git_repo,
            git_branch=git_branch,
            source_path=source_path,
            target_path=target_path,
            multi_repo=multi_repo,
        )

        graphrag_indexing_pipeline(
            codebase_path=target_path,
            graphrag_source_path=graphrag_source_path,
            git_repo=git_repo,
            git_branch=git_branch,
            multi_repo=multi_repo,
        )

        graphrag_analysis_pipeline(
            graphrag_source_path=graphrag_source_path,
            git_repo=git_repo,
            git_branch=git_branch,
            multi_repo=multi_repo,
        )


@dsl.pipeline(name="multi-repo-pipeline")
def multi_repo_pipeline(
    parent_source_path: str = os.getenv("PARENT_SOURCE_PATH", "source"),
    parent_target_path: str = os.getenv("PARENT_TARGET_PATH", "target"),
):

    if uses_kfp():

        from pipelines.kubeflow.data_generation import run_full_pipeline_multi_repo_op as run_data_generation_multi_repo_op
        from pipelines.kubeflow.indexing import run_indexing_multi_repo_op
        from pipelines.kubeflow.analysis import run_analysis_multi_repo_op

        dg = run_data_generation_multi_repo_op()

        idx = run_indexing_multi_repo_op(
            parent_target_path=parent_target_path,
        ).after(dg)

        run_analysis_multi_repo_op().after(idx)

    else:

        from pipelines.base.data_generation import run_full_pipeline_multi_repo
        from pipelines.base.indexing import run_full_pipeline_multi_repo as run_indexing_multi_repo
        from pipelines.base.analysis import run_full_pipeline_multi_repo as run_analysis_multi_repo
        from loaders.default_asset_loader import DefaultAssetLoader

        git_repos = DefaultAssetLoader().download("repos/repo_list.json")

        run_full_pipeline_multi_repo(git_repos)

        run_indexing_multi_repo(parent_target_path=parent_target_path)

        run_analysis_multi_repo()


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
