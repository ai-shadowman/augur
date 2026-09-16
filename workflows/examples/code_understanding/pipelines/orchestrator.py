"""KFP pipeline definitions spanning all three pipeline stages, plus the compile entry point.

Run directly to compile all pipelines to YAML:

    PIPELINE_COMPILE_ONLY=1 \\
    KFP_PIPELINE_OUTPUT_DIR=compiled_pipelines \\
    PYTHONPATH=<code_understanding_dir> \\
    python3 pipelines/orchestrator.py
"""
import os
import sys
import json
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
            pipeline_name="single-repo-pipeline",
        )

        idx = IndexingPipeline.run(
            codebase_dir=dg.output,
            git_repo=git_repo,
            git_branch=git_branch,
            multi_repo=multi_repo,
            pipeline_name="single-repo-pipeline",
        )

        AnalysisPipeline.run(
            graphrag_dir=idx.output,
            git_repo=git_repo,
            git_branch=git_branch,
            multi_repo=multi_repo,
            pipeline_name="single-repo-pipeline",
        )

    else:

        from pipelines.base.data_generation import generate_git_slug
        from utils.metrics_tracker import PipelineMetricsTracker

        git_slug = generate_git_slug(git_repo, git_branch)
        source_path = f"{parent_source_path}/{git_slug}"
        target_path = f"{parent_target_path}/{git_slug}"
        graphrag_source_path = os.path.join(
            os.getenv("KFP_DATA_INDEXING_OUTPUT_PATH", "graph_rag_app/source"), git_slug
        )

        tracker = PipelineMetricsTracker("single-repo-pipeline", multi_repo=multi_repo)

        with tracker.track_pipeline():

            with tracker.track_stage("Data Generation"):
                DataGenerationPipeline().run(
                    git_repo=git_repo,
                    git_branch=git_branch,
                    source_path=source_path,
                    target_path=target_path,
                    multi_repo=multi_repo,
                    metrics_tracker=tracker,
                )

            with tracker.track_stage("Indexing"):
                IndexingPipeline().run(
                    codebase_path=target_path,
                    graphrag_source_path=graphrag_source_path,
                    git_repo=git_repo,
                    git_branch=git_branch,
                    multi_repo=multi_repo,
                    metrics_tracker=tracker,
                )

            with tracker.track_stage("Analysis"):
                AnalysisPipeline().run(
                    graphrag_source_path=graphrag_source_path,
                    git_repo=git_repo,
                    git_branch=git_branch,
                    multi_repo=multi_repo,
                    metrics_tracker=tracker,
                )

        tracker.log_summary()

        metrics_file = f"pipeline_metrics_{git_slug}.json" if git_slug else "pipeline_metrics.json"
        tracker.save_to_file(metrics_file)

        try:
            from loaders.default_asset_loader import DefaultAssetLoader
            DefaultAssetLoader().log_results(
                metrics_file,
                artifact_path=DefaultAssetLoader.get_log_results_artifact_path(
                    DefaultAssetLoader.RESULTS_PATH_PREFIX_PIPELINES,
                    git_slug=git_slug,
                    multi_repo=False,
                ),
                content=json.dumps(tracker.to_dict(), indent=2),
                tags={"git_slug": git_slug, "category": "metrics"},
            )
        except Exception:
            pass

        tracker.log_to_mlflow()


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
        from utils.metrics_tracker import PipelineMetricsTracker

        #git_repos = DefaultAssetLoader().download("repos/repo_list.json")
        git_repos_env = os.getenv("GIT_REPO_LIST_CONTENTS")
        git_repos = json.loads(git_repos_env) if git_repos_env else []

        tracker = PipelineMetricsTracker("multi-repo-pipeline", multi_repo=True)

        with tracker.track_pipeline():

            with tracker.track_stage("Data Generation"):
                DataGenerationPipeline().run_multi_repo(git_repos, metrics_tracker=tracker)

            with tracker.track_stage("Indexing"):
                IndexingPipeline().run_multi_repo(parent_target_path=parent_target_path, metrics_tracker=tracker)

            with tracker.track_stage("Analysis"):
                AnalysisPipeline().run_multi_repo(metrics_tracker=tracker)

        tracker.log_summary()

        metrics_file = "pipeline_metrics_multi_repo.json"
        tracker.save_to_file(metrics_file)

        try:
            DefaultAssetLoader().log_results(
                metrics_file,
                artifact_path=DefaultAssetLoader.get_log_results_artifact_path(
                    DefaultAssetLoader.RESULTS_PATH_PREFIX_PIPELINES,
                    multi_repo=True,
                ),
                content=json.dumps(tracker.to_dict(), indent=2),
                tags={"multi_repo": True, "category": "metrics"},
            )
        except Exception:
            pass

        tracker.log_to_mlflow()


##############################################################################
# Compile entry point
##############################################################################

if __name__ == "__main__":
    import argparse

    if os.getenv("PIPELINE_COMPILE_ONLY"):

        compile_all_and_exit({
            "data_generation": DataGenerationPipeline.run,
            "single_repo":     single_repo_pipeline,
            "multi_repo":      multi_repo_pipeline,
            "indexing":        IndexingPipeline.run,
            "analysis":        AnalysisPipeline.run,
        })

    else:

        parser = argparse.ArgumentParser(description="Run or compile pipelines.")
        parser.add_argument("--single-repo", action="store_true", help="Run single-repo pipeline")
        parser.add_argument("--multi-repo", action="store_true", help="Run multi-repo pipeline")
        parser.add_argument("--compile", action="store_true", help="Compile pipelines to YAML")
        args = parser.parse_args()

        if args.compile:
            compile_all_and_exit({
                "data_generation": DataGenerationPipeline.run,
                "single_repo":     single_repo_pipeline,
                "multi_repo":      multi_repo_pipeline,
                "indexing":        IndexingPipeline.run,
                "analysis":        AnalysisPipeline.run,
            })
        elif args.single_repo:
            single_repo_pipeline()
        elif args.multi_repo:
            multi_repo_pipeline()
        else:
            parser.print_help()
