import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))


##############################################################################
# Pipeline stage
##############################################################################

class AnalysisPipeline:

    def run(self, graphrag_source_path: str, git_repo: str = "", git_branch: str = "",
            multi_repo: bool = False):
        """Generates a migration report from the GraphRAG index and returns the result."""
        import asyncio, logging
        from loaders.default_asset_loader import DefaultAssetLoader
        from utils.graphrag_utils import DependencyAnalyzer
        from pipelines.base.data_generation import generate_git_slug
        import os

        logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

        git_slug = generate_git_slug(git_repo, git_branch) if git_repo else None

        analyzer = DependencyAnalyzer(graphrag_source_path, git_slug=git_slug or "", multi_repo=multi_repo)

        report = asyncio.run(analyzer.generate_migration_report())

        result_file = f"migration_report_{git_slug}.txt" if git_slug else "migration_report.txt"

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

        return report

    def run_multi_repo(self):
        """Runs migration report generation across the combined multi-repo GraphRAG index."""
        import os, logging
        from loaders.default_asset_loader import DefaultAssetLoader
        from utils.loader_utils import download_result_directory

        logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

        graphrag_source_path = os.getenv("KFP_DATA_INDEXING_OUTPUT_PATH", "graph_rag_app/source")

        logging.info("Downloading multi-repo GraphRAG index...")
        download_result_directory(
            git_slug=None,
            download_dir=os.path.join(graphrag_source_path, "output"),
            results_prefix=DefaultAssetLoader.RESULTS_PATH_PREFIX_REPO_DATASETS,
            multi_repo=True,
            asset_tags={"multi_repo": True, "category": "indexing"},
        )

        self.run(graphrag_source_path=graphrag_source_path, multi_repo=True)

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
        from utils.loader_utils import download_result_directory
        from pipelines.base.data_generation import generate_git_slug
        import os

        logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

        git_slug = generate_git_slug(git_repo, git_branch) if git_repo else ""

        graphrag_source_path = "graph_rag_app/source"

        try:
            download_result_directory(
                git_slug=git_slug,
                download_dir=os.path.join(graphrag_source_path, "output"),
                results_prefix=DefaultAssetLoader.RESULTS_PATH_PREFIX_REPO_DATASETS,
                multi_repo=multi_repo,
                asset_tags={"git_slug": git_slug, "multi_repo": multi_repo, "category": "indexing"},
            )
        except Exception as e:
            import traceback
            msg = (
                "Could not perform query: "
                + ("no multi-repository index was found" if multi_repo else f"no index was found for git_repo='{git_repo}'")
                + ". Maybe you need to generate it first?"
            )
            logging.error(msg)
            print(msg, flush=True)
            logging.debug(traceback.format_exc())
            return ""

        analyzer = DependencyAnalyzer(graphrag_source_path, git_slug=git_slug, multi_repo=multi_repo)

        result = asyncio.run(analyzer.query_with_llm(question, retry_count=retry_count, use_global=use_global))

        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

        result_file = f"adhoc_query_{timestamp}.txt"

        DefaultAssetLoader().log_results(

            result_file,

            artifact_path=(

                DefaultAssetLoader.get_log_results_artifact_path(

                    DefaultAssetLoader.RESULTS_PATH_PREFIX_ADHOC_QUERIES,

                    git_slug=git_slug,

                    multi_repo=multi_repo,

                )

            ),

            content=f"Question: {question}\n\nAnswer:\n{result}",

            tags={"category": "analysis",
                  "adhoc_query": "true",
                  "git_slug": git_slug,
                  "multi_repo": multi_repo},

        )

        return result


##############################################################################
# Module-level aliases for external callers (notebooks, scripts)
##############################################################################

def run_full_pipeline(*args, **kwargs):
    return AnalysisPipeline().run(*args, **kwargs)

def run_full_pipeline_multi_repo(*args, **kwargs):
    return AnalysisPipeline().run_multi_repo(*args, **kwargs)

def run_adhoc_query_pipeline(*args, **kwargs):
    return AnalysisPipeline().run_adhoc_query(*args, **kwargs)
