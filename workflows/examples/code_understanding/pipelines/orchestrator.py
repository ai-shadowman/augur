"""KFP pipeline definitions spanning all three pipeline stages, plus the compile entry point.

Run directly to compile all pipelines to YAML:

    PIPELINE_COMPILE_ONLY=1 \\
    KFP_PIPELINE_OUTPUT_DIR=compiled_pipelines \\
    PYTHONPATH=<code_understanding_dir> \\
    python3 pipelines/orchestrator.py
"""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from kfp import dsl
from utils.kubeflow_utils import compile_all_and_exit

from utils.pipeline_utils import uses_kfp

if uses_kfp():

    from pipelines.kubeflow.data_generation import DataGenerationPipeline
    from pipelines.kubeflow.indexing import IndexingPipeline
    from pipelines.kubeflow.analysis import AnalysisPipeline

else:

    from pipelines.base.data_generation import DataGenerationPipeline
    from pipelines.base.indexing import IndexingPipeline
    from pipelines.base.analysis import AnalysisPipeline

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

        dg = DataGenerationPipeline.run(
            git_repo=git_repo,
            git_branch=git_branch,
            multi_repo=multi_repo,
        )

        idx = IndexingPipeline.run(
            codebase_dir=dg.output,
            git_repo=git_repo,
            git_branch=git_branch,
            multi_repo=multi_repo,
        )

        AnalysisPipeline.run(
            graphrag_dir=idx.output,
            git_repo=git_repo,
            git_branch=git_branch,
            multi_repo=multi_repo,
        )

    else:

        from pipelines.base.data_generation import generate_git_slug

        git_slug = generate_git_slug(git_repo, git_branch)
        source_path = f"{parent_source_path}/{git_slug}"
        target_path = f"{parent_target_path}/{git_slug}"
        graphrag_source_path = os.path.join(
            os.getenv("KFP_DATA_INDEXING_OUTPUT_PATH", "graph_rag_app/source"), git_slug
        )

        DataGenerationPipeline().run(
            git_repo=git_repo,
            git_branch=git_branch,
            source_path=source_path,
            target_path=target_path,
            multi_repo=multi_repo,
        )

        IndexingPipeline().run(
            codebase_path=target_path,
            graphrag_source_path=graphrag_source_path,
            git_repo=git_repo,
            git_branch=git_branch,
            multi_repo=multi_repo,
        )

        AnalysisPipeline().run(
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

        dg = DataGenerationPipeline.run_multi_repo()

        idx = IndexingPipeline.run_multi_repo(
            parent_target_path=parent_target_path,
        ).after(dg)

        AnalysisPipeline.run_multi_repo(
            graphrag_dir=idx.outputs["graphrag_dir"],
        )

    else:

        from loaders.default_asset_loader import DefaultAssetLoader

        git_repos = DefaultAssetLoader().download("repos/repo_list.json")

        DataGenerationPipeline().run_multi_repo(git_repos)

        IndexingPipeline().run_multi_repo(parent_target_path=parent_target_path)

        AnalysisPipeline().run_multi_repo()


##############################################################################
# Compile entry point
##############################################################################

if __name__ == "__main__":

    compile_all_and_exit({
        "data_generation": DataGenerationPipeline.run,
        "single_repo":     single_repo_pipeline,
        "multi_repo":      multi_repo_pipeline,
        "indexing":        IndexingPipeline.run,
        "analysis":        AnalysisPipeline.run,
    })
