"""KFP pipeline definitions spanning all three pipeline stages, plus the compile entry point.

Run directly to compile all pipelines to YAML:

    PIPELINE_COMPILE_ONLY=1 \
    KFP_PIPELINE_OUTPUT_DIR=compiled_pipelines \
    PYTHONPATH=<code_understanding_dir> \
    python3 pipelines/orchestrator.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from kfp import dsl
from utils.kubeflow_utils import compile_all_and_exit, setup_logging
from utils.pipeline_utils import uses_kfp
from utils.repo_catalog import SCAN_MODE

setup_logging()

if uses_kfp():
    from pipelines.kubeflow.data_generation import DataGenerationPipeline
    from pipelines.kubeflow.indexing import IndexingPipeline
    from pipelines.kubeflow.analysis import AnalysisPipeline
    from pipelines.kubeflow.single_multi_repo import annotate_focal_repo_op, validate_focal_repo_op
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

        env_repos = os.getenv("GIT_REPO_LIST_CONTENTS")
        git_repos = json.loads(env_repos) if env_repos else (DefaultAssetLoader().download("repos/repo_list.json") or [])

        DataGenerationPipeline().run_multi_repo(git_repos)
        IndexingPipeline().run_multi_repo(parent_target_path=parent_target_path)
        AnalysisPipeline().run_multi_repo()


@dsl.pipeline(name="single-multi-repo-pipeline")
def single_multi_repo_pipeline(
    git_repo: str = os.getenv("GIT_REPO", ""),
    git_branch: str = os.getenv("GIT_BRANCH", "main"),
    parent_source_path: str = os.getenv("PARENT_SOURCE_PATH", "source"),
    parent_target_path: str = os.getenv("PARENT_TARGET_PATH", "target"),
):
    if uses_kfp():
        check = validate_focal_repo_op(git_repo=git_repo, git_branch=git_branch)
        dg = DataGenerationPipeline.run_multi_repo().after(check)
        idx = IndexingPipeline.run_multi_repo(
            parent_target_path=parent_target_path,
        ).after(dg)
        AnalysisPipeline.run_multi_repo(
            graphrag_dir=idx.outputs["graphrag_dir"],
        )
        focal = annotate_focal_repo_op(
            git_repo=git_repo,
            git_branch=git_branch,
            parent_target_path=parent_target_path,
        ).after(dg)
        fidx = IndexingPipeline.run(
            codebase_dir=focal.outputs["codebase_dir"],
            git_repo=git_repo,
            git_branch=git_branch,
            multi_repo=False,
            scan_mode=SCAN_MODE,
        )
        AnalysisPipeline.run(
            graphrag_dir=fidx.output,
            git_repo=git_repo,
            git_branch=git_branch,
            multi_repo=False,
            scan_mode=SCAN_MODE,
        )
    else:
        from loaders.default_asset_loader import DefaultAssetLoader
        from utils.repo_catalog import prepare_focal_codebase, require_focal_in_list

        env_repos = os.getenv("GIT_REPO_LIST_CONTENTS")
        git_repos = json.loads(env_repos) if env_repos else (DefaultAssetLoader().download("repos/repo_list.json") or [])
        focal_slug = require_focal_in_list(git_repo, git_branch, git_repos)

        DataGenerationPipeline().run_multi_repo(git_repos)
        IndexingPipeline().run_multi_repo(parent_target_path=parent_target_path)
        AnalysisPipeline().run_multi_repo()

        # A sibling of parent_target_path, never inside it, so a later overview index cannot pick it up.
        annotated_path = f"{os.path.normpath(parent_target_path)}-single-multi/{focal_slug}"
        prepare_focal_codebase(git_repo, git_branch, git_repos, parent_target_path, annotated_path)
        graphrag_source_path = os.path.join(
            os.getenv("KFP_DATA_INDEXING_OUTPUT_PATH", "graph_rag_app/source"), focal_slug
        )

        result = IndexingPipeline().run(
            codebase_path=annotated_path,
            graphrag_source_path=graphrag_source_path,
            git_repo=git_repo,
            git_branch=git_branch,
            multi_repo=False,
            scan_mode=SCAN_MODE,
        )
        if result.get("status") != "success":
            raise RuntimeError(result.get("fail_message"))
        AnalysisPipeline().run(
            graphrag_source_path=graphrag_source_path,
            git_repo=git_repo,
            git_branch=git_branch,
            multi_repo=False,
            scan_mode=SCAN_MODE,
        )


##############################################################################
# Compile entry point
##############################################################################

if __name__ == "__main__":
    compile_all_and_exit({
        "data_generation": DataGenerationPipeline.run,
        "single_repo":     single_repo_pipeline,
        "multi_repo":      multi_repo_pipeline,
        "single_multi_repo": single_multi_repo_pipeline,
        "indexing":        IndexingPipeline.run,
        "analysis":        AnalysisPipeline.run,
    })
