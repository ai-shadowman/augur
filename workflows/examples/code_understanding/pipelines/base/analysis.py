import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))


def run_full_pipeline(graphrag_source_path: str, git_slug: str = None, multi_repo: bool = False):
    """Generates a migration report from the GraphRAG index and returns the result."""
    import asyncio, logging
    from pathlib import Path
    from loaders.default_asset_loader import DefaultAssetLoader
    from utils.graphrag_utils import DependencyAnalyzer
    import os

    logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

    analyzer = DependencyAnalyzer(graphrag_source_path)

    report = asyncio.run(analyzer.generate_migration_report())

    result_file = f"migration_report_{Path(graphrag_source_path).name}.txt"

    DefaultAssetLoader().log_results(result_file,
                                     artifact_path="results/pipelines",
                                     content=report,
                                     tags={"git_slug":git_slug,
                                           "multi_repo":multi_repo,
                                           "category":"analysis"})

    return report


def run_adhoc_query_pipeline(
    graphrag_source_path: str,
    question: str,
    retry_count: int = 3,
    use_global: bool = True,
):
    """Queries the GraphRAG index with an LLM and returns the result."""
    import asyncio, logging
    from datetime import datetime
    from loaders.default_asset_loader import DefaultAssetLoader
    from utils.graphrag_utils import DependencyAnalyzer
    import os

    logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

    analyzer = DependencyAnalyzer(graphrag_source_path)

    result = asyncio.run(analyzer.query_with_llm(question, retry_count=retry_count, use_global=use_global))

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    result_file = f"adhoc_query_{timestamp}.txt"

    DefaultAssetLoader().log_results(result_file, artifact_path="results/adhoc_queries",
                                     content=f"Question: {question}\n\nAnswer:\n{result}",
                                     tags={"category": "analysis"})

    return result


