import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))


##############################################################################
# Pipeline stage
##############################################################################

class AnalysisPipeline:

    def run(self, graphrag_source_path: str, git_repo: str = "", git_branch: str = "",
            multi_repo: bool = False, metrics_tracker=None):
        """Generates a migration report from the GraphRAG index and returns the result."""
        import asyncio, logging
        from loaders.default_asset_loader import DefaultAssetLoader
        from utils.graphrag_utils import DependencyAnalyzer
        from pipelines.base.data_generation import generate_git_slug
        import os
        from utils.metrics_tracker import PipelineMetricsTracker

        logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

        git_slug = generate_git_slug(git_repo, git_branch) if git_repo else None

        tracker = metrics_tracker
        own_tracker = False
        if tracker is None:
            tracker = PipelineMetricsTracker("analysis-pipeline", multi_repo=multi_repo)
            own_tracker = True
            tracker.start_stage("Analysis")

        try:

            with tracker.track_step("generate_migration_report", stage="Analysis"):
                analyzer = DependencyAnalyzer(
                    graphrag_source_path,
                    git_slug=git_slug or "",
                    multi_repo=multi_repo,
                    metrics_tracker=tracker,
                )
                report = asyncio.run(analyzer.generate_migration_report())

            result_file = f"migration_report_{git_slug}.md" if git_slug else "migration_report.md"

            with tracker.track_step("log_results", stage="Analysis"):
                DefaultAssetLoader().log_results(
                    result_file,
                    artifact_path=DefaultAssetLoader.get_log_results_artifact_path(
                        DefaultAssetLoader.RESULTS_PATH_PREFIX_PIPELINES,
                        git_slug=git_slug,
                        multi_repo=multi_repo,
                    ),
                    content=report,
                    tags={"git_slug": git_slug, "multi_repo": multi_repo, "category": "analysis"},
                )

            if own_tracker:
                tracker.stop_stage("Analysis", status="COMPLETED")
                tracker.stop_pipeline(status="COMPLETED")
                tracker.log_summary()

            return report

        except Exception as e:
            if own_tracker:
                tracker.stop_stage("Analysis", status="FAILED", error_message=str(e))
                tracker.stop_pipeline(status="FAILED", error_message=str(e))
                tracker.log_summary()
            raise

    def run_multi_repo(self, metrics_tracker=None):
        """Runs migration report generation across the combined multi-repo GraphRAG index."""
        import os, logging
        from loaders.default_asset_loader import DefaultAssetLoader
        from utils.loader_utils import download_result_directory
        from utils.metrics_tracker import PipelineMetricsTracker

        logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

        tracker = metrics_tracker
        own_tracker = False
        if tracker is None:
            tracker = PipelineMetricsTracker("multi-repo-analysis", multi_repo=True)
            own_tracker = True
            tracker.start_stage("Analysis")

        graphrag_source_path = os.getenv("KFP_DATA_INDEXING_OUTPUT_PATH", "graph_rag_app/source")

        logging.info("Downloading multi-repo GraphRAG index...")
        with tracker.track_step("download_graphrag_index", stage="Analysis"):
            download_result_directory(
                git_slug=None,
                download_dir=os.path.join(graphrag_source_path, "output"),
                results_prefix=DefaultAssetLoader.RESULTS_PATH_PREFIX_REPO_DATASETS,
                multi_repo=True,
                asset_tags={"multi_repo": True, "category": "indexing"},
            )

        report = self.run(graphrag_source_path=graphrag_source_path, multi_repo=True, metrics_tracker=tracker)

        if own_tracker:
            tracker.stop_stage("Analysis", status="COMPLETED")
            tracker.stop_pipeline(status="COMPLETED")
            tracker.log_summary()

        return report

    def run_adhoc_query(
        self,
        question: str,
        retry_count: int = 3,
        use_global: bool = True,
        git_repo: str = "",
        git_branch: str = "main",
        multi_repo: bool = False,
    ):
        """Queries the GraphRAG index with an LLM and returns the result."""
        import asyncio, logging
        from datetime import datetime
        from loaders.default_asset_loader import DefaultAssetLoader
        from utils.graphrag_utils import DependencyAnalyzer
        from pipelines.base.data_generation import generate_git_slug
        import os

        logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

        use_multi_repo = multi_repo or not git_repo

        git_slug = generate_git_slug(git_repo, git_branch) if git_repo else ""

        graphrag_source_path = "graph_rag_app/source"

        adhoc_results_header = "\n---\n\n# ##################ADHOC RESULTS##################\n"

        try:
            DependencyAnalyzer.download_graphrag_directory(
                download_dir=graphrag_source_path,
                git_slug=git_slug,
                multi_repo=use_multi_repo,
                git_repo=git_repo,
            )
        except Exception:
            msg = (
                "Could not perform query: "
                + ("no multi-repository index was found" if use_multi_repo else
                   f"no index was found (git_repo='{git_repo}')")
                + ". Maybe you need to generate it first?"
            )
            logging.error(msg)
            print(adhoc_results_header, flush=True)
            return msg

        analyzer = DependencyAnalyzer(graphrag_source_path, git_slug=git_slug, multi_repo=use_multi_repo)

        postamble = "Provide as much detail as possible. Include the git repo url(s) in the report."

        if use_multi_repo:
            postamble += (" Include ALL the git repo urls that you can find."
                          " Group git repositories by their git repo url.")

        result = asyncio.run(analyzer.query_with_llm(
            question + postamble,
            retry_count=retry_count,
            use_global=use_global,
            response_type="Multiple Paragraphs, plain text, no markdown formatting",
        ))

        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

        result_file = f"adhoc_query_{timestamp}.txt"

        DefaultAssetLoader().log_results(

            result_file,

            artifact_path=(

                DefaultAssetLoader.get_log_results_artifact_path(

                    DefaultAssetLoader.RESULTS_PATH_PREFIX_ADHOC_QUERIES,

                    git_slug=git_slug,

                    multi_repo=use_multi_repo,

                )

            ),

            content=f"Question: {question}\n\nAnswer:\n{result}",

            tags={"category": "analysis",
                  "adhoc_query": "true",
                  "git_slug": git_slug,
                  "multi_repo": use_multi_repo},

        )

        print(adhoc_results_header, flush=True)
        return f"{result}\n\n"


##############################################################################
# Helpers
##############################################################################

def write_migration_report(graphrag_source_path: str, report_path: str,
                           git_repo: str = "", git_branch: str = "",
                           multi_repo: bool = False, metrics_tracker=None):
    """Run the migration report and write the result to report_path."""
    import os
    migration_report = AnalysisPipeline().run(graphrag_source_path, git_repo=git_repo,
                                              git_branch=git_branch, multi_repo=multi_repo,
                                              metrics_tracker=metrics_tracker)
    if dirname := os.path.dirname(report_path):
        os.makedirs(dirname, exist_ok=True)
    with open(report_path, "w") as f:
        f.write(migration_report)


##############################################################################
# Module-level aliases for external callers (notebooks, scripts)
##############################################################################

def run_full_pipeline(*args, **kwargs):
    return AnalysisPipeline().run(*args, **kwargs)

def run_full_pipeline_multi_repo(*args, **kwargs):
    return AnalysisPipeline().run_multi_repo(*args, **kwargs)

def run_adhoc_query_pipeline(*args, **kwargs):
    return AnalysisPipeline().run_adhoc_query(*args, **kwargs)
