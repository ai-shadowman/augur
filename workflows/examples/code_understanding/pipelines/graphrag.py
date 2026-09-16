#!/usr/bin/env python3
"""GraphRAG project setup and indexing.

Replaces graphrag.sh. Can be imported for in-process use (enabling MLflow/OTEL
tracing of all LLM calls) or executed as a standalone script.
"""

import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def run_graphrag(root_dir: str) -> None:
    """Initialize a GraphRAG project and build the index.

    Equivalent to graphrag.sh:
      python -m graphrag init --force --root <root_dir>
      cp templates/settings.yaml <root_dir>/settings.yaml
      python -m graphrag index --root <root_dir>

    Unlike the shell script, this runs in the calling process so any
    LiteLLM callbacks registered before this call (e.g. via
    DefaultCustomTelemetry().track()) will capture all LLM calls.
    """
    from graphrag.cli.initialize import initialize_project_at
    from graphrag.config.load_config import load_config
    import graphrag.api as graphrag_api

    root_path = Path(root_dir)

    log.info("Initializing GraphRAG index...")
    initialize_project_at(root_path, force=True)

    log.info("Copying settings.yaml...")
    shutil.copy("templates/settings.yaml", root_path / "settings.yaml")

    log.info("Populating GraphRAG index...")
    config = load_config(root_path)
    results = asyncio.run(graphrag_api.build_index(config=config, verbose=True))

    errors = [r for r in results if r.errors]
    if errors:
        raise RuntimeError(f"GraphRAG indexing failed: {errors}")

    log.info("GraphRAG indexing complete.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        logging.basicConfig(level=logging.INFO)
        log.error("Usage: graphrag.py <root_dir>")
        sys.exit(1)

    logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO").upper())

    try:
        run_graphrag(sys.argv[1])
    except RuntimeError as e:
        log.error(str(e))
        sys.exit(1)
