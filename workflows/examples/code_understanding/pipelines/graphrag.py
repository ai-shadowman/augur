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

    logging.info("Starting Graphic Rag Indexing")
    root_path = Path(root_dir)

    try:
        from utils.duration_tracker import (
            DurationTracker,
            find_all_telemetry_files,
            extract_graphrag_indexing_durations,
        )
        dur_tracker = DurationTracker.get_instance()
        candidate_dirs = [
            str(root_path),
            str(root_path / "output"),
            str(root_path / "input"),
            str(root_path.parent),
        ]
        if root_path.parent and root_path.parent.parent:
            candidate_dirs.append(str(root_path.parent.parent / "target"))
            candidate_dirs.append(str(root_path.parent.parent / "target" / root_path.name))
        existing_dur_files = find_all_telemetry_files(candidate_dirs, "durations.json")
        for fpath in existing_dur_files:
            dur_tracker.load_and_merge(fpath, current_stage="Indexing")
    except Exception as e:
        log.debug(f"Failed to initialize/load durations in run_graphrag: {e}")
        dur_tracker = None

    try:
        from utils.token_tracker import TokenCostTracker
        token_tracker = TokenCostTracker.get_instance()
        token_tracker.enable_litellm_callbacks(category="GraphRAG Indexing")
        token_tracker.enable_openai_tracking(category="GraphRAG Indexing")
        existing_tok_files = find_all_telemetry_files(candidate_dirs, "tokens.json")
        for fpath in existing_tok_files:
            token_tracker.load_and_merge(fpath, current_stage="Indexing")
    except Exception as e:
        log.debug(f"Failed to enable token tracking in run_graphrag: {e}")

    log.info("Initializing GraphRAG index...")
    if dur_tracker:
        with dur_tracker.measure(stage="Indexing", step="Initialize GraphRAG Project"):
            initialize_project_at(root_path, force=True)
            log.info("Copying settings.yaml...")
            shutil.copy("templates/settings.yaml", root_path / "settings.yaml")
    else:
        initialize_project_at(root_path, force=True)
        log.info("Copying settings.yaml...")
        shutil.copy("templates/settings.yaml", root_path / "settings.yaml")

    log.info("Populating GraphRAG index...")
    config = load_config(root_path)
    if dur_tracker:
        with dur_tracker.measure(stage="Indexing", step="GraphRAG Indexing"):
            results = asyncio.run(graphrag_api.build_index(config=config, verbose=True))
    else:
        results = asyncio.run(graphrag_api.build_index(config=config, verbose=True))

    errors = [r for r in results if r.errors]
    if errors:
        raise RuntimeError(f"GraphRAG indexing failed: {errors}")

    log.info("GraphRAG indexing complete.")

    try:
        from utils.token_tracker import extract_graphrag_indexing_tokens, TokenCostTracker
        tracker = TokenCostTracker.get_instance()
        extract_graphrag_indexing_tokens(str(root_path), tracker)
        try:
            existing_tok_files = find_all_telemetry_files(candidate_dirs, "tokens.json")
            for fpath in existing_tok_files:
                tracker.load_and_merge(fpath, current_stage="Indexing")
        except Exception:
            pass
        for d in [str(root_path), str(root_path / "output")]:
            try:
                os.makedirs(d, exist_ok=True)
                tracker.save_to_file(os.path.join(d, "tokens.json"))
            except Exception:
                pass
    except Exception as e:
        log.debug(f"Failed to save tokens in run_graphrag: {e}")

    try:
        if dur_tracker:
            extract_graphrag_indexing_durations(str(root_path), dur_tracker)
            for d in [str(root_path), str(root_path / "output")]:
                try:
                    os.makedirs(d, exist_ok=True)
                    dur_tracker.save_to_file(os.path.join(d, "durations.json"))
                except Exception:
                    pass
    except Exception as e:
        log.debug(f"Failed to save durations in run_graphrag: {e}")


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
