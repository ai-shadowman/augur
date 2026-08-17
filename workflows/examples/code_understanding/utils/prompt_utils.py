import logging
import os

logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())


def prepare_indexing_config(graphrag_source_path: str, git_slug: str, git_repo: str, multi_repo: bool):
    """Downloads GraphRAG prompt files and copies them to graphrag_source_path.
    """
    import shutil
    from loaders.default_asset_loader import DefaultAssetLoader

    loader = DefaultAssetLoader()
    loader.download("graphrag/extract_graph.txt", download_dir="prompts")
    loader.download("graphrag/community_report.txt", download_dir="prompts")
    loader.download("graphrag/summarize_descriptions.txt", download_dir="prompts")
    loader.download("graphrag/global_search_reduce_system_prompt.txt", download_dir="prompts")

    shutil.copytree("prompts", f"{graphrag_source_path}/prompts", dirs_exist_ok=True)

    logging.info(f"GraphRAG prompts prepared for git_slug={git_slug}, multi_repo={multi_repo}.")
