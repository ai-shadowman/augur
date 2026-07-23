import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def run_full_pipeline(graphrag_source_path: str):
    """Generates a migration report from the GraphRAG index and returns the result."""
    import asyncio, logging
    from pathlib import Path
    from loaders.default_asset_loader import DefaultAssetLoader
    from utils.graphrag_utils import DependencyAnalyzer

    logging.basicConfig(level=logging.INFO)

    analyzer = DependencyAnalyzer(graphrag_source_path)

    report = asyncio.run(analyzer.generate_migration_report())

    result_file = f"migration_report_{Path(graphrag_source_path).name}.txt"

    Path(result_file).write_text(report)

    DefaultAssetLoader().log_results(result_file, artifact_path="results/pipelines")

    return report
