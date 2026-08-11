import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))


def run_full_pipeline(graphrag_source_path: str, git_repo: str = "", git_branch: str = "",
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


def run_full_pipeline_multi_repo():
    """Runs migration report generation across the combined multi-repo GraphRAG index."""
    import os

    graphrag_source_path = os.getenv("KFP_DATA_INDEXING_OUTPUT_PATH", "graph_rag_app/source")

    run_full_pipeline(graphrag_source_path=graphrag_source_path, multi_repo=True)


def run_adhoc_query_pipeline(
    graphrag_source_path: str,
    question: str,
    retry_count: int = 3,
    use_global: bool = True,
    git_slug: str = "",
    multi_repo: bool = False,
):
    """Queries the GraphRAG index with an LLM and returns the result."""
    import asyncio, logging
    from datetime import datetime
    from loaders.default_asset_loader import DefaultAssetLoader
    from utils.graphrag_utils import DependencyAnalyzer
    import os

    logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

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

            ) if git_slug else DefaultAssetLoader.RESULTS_PATH_PREFIX_ADHOC_QUERIES

        ),

        content=f"Question: {question}\n\nAnswer:\n{result}",

        tags={"category": "analysis"},

    )

    return result


