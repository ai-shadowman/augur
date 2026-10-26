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
from contextlib import nullcontext
from pathlib import Path

log = logging.getLogger(__name__)

#: Where ``utils.prompt_utils.prepare_indexing_config`` stages AUGUR's prompts,
#: relative to the working directory, as ``templates/`` is for the settings.
STAGED_PROMPTS = Path("prompts")


def restore_prompts(root_path: Path) -> None:
    """Put AUGUR's staged prompts back over the ones GraphRAG's init wrote.

    ``initialize_project_at(force=True)`` writes GraphRAG's stock prompts into
    ``root/prompts/``, over the ones ``prepare_indexing_config`` copied there
    first. Until 2026-10-25 nothing re-copied them, so every deployed index
    was extracted with GraphRAG's stock ``extract_graph.txt`` and
    ``community_report.txt`` while ``settings.yaml``, re-copied just above,
    was AUGUR's. Found on the cluster: 100% of relationships untyped, against
    2% for the same corpus through AUGUR's prompt.
    """
    if not STAGED_PROMPTS.is_dir():
        log.warning(
            "No staged prompts at %s; indexing with GraphRAG's stock prompts. "
            "Run utils.prompt_utils.prepare_indexing_config first.",
            STAGED_PROMPTS.resolve(),
        )
        return
    shutil.copytree(STAGED_PROMPTS, root_path / "prompts", dirs_exist_ok=True)
    log.info("Restored AUGUR prompts over GraphRAG's defaults: %s",
             ", ".join(sorted(p.name for p in STAGED_PROMPTS.iterdir())))


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
    from utils.request_timing import timing_span
    from graphrag.cli.initialize import initialize_project_at
    from graphrag.config.load_config import load_config
    import graphrag.api as graphrag_api

    logging.info("Starting GraphRAG Indexing")
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
    cm_init = dur_tracker.measure(stage="Indexing", step="Initialize GraphRAG Project") if dur_tracker else nullcontext()
    with cm_init:
        initialize_project_at(root_path, force=True)
        log.info("Copying settings.yaml...")
        shutil.copy("templates/settings.yaml", root_path / "settings.yaml")
        restore_prompts(root_path)

    log.info("Populating GraphRAG index...")
    config = load_config(root_path)

    # Declared versions and the git remote are known exactly before any model
    # runs, so they are written into the entity table directly. This has to
    # happen inside the pipeline rather than after it: community reports and
    # the entity embeddings are both built from these descriptions.
    try:
        from provenance.pipeline import install as install_provenance_stitch
        install_provenance_stitch(config)
    except Exception as e:
        log.warning(f"Provenance stitch not installed, indexing without it: {e}")

    cm_build = dur_tracker.measure(stage="Indexing", step="GraphRAG Indexing") if dur_tracker else nullcontext()
    with cm_build, timing_span("graphrag.build_index", "batch"):
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
    from utils.kubeflow_utils import setup_logging
    setup_logging()

    if len(sys.argv) < 2:
        log.error("Usage: graphrag.py <root_dir>")
        sys.exit(1)

    try:
        run_graphrag(sys.argv[1])
    except RuntimeError as e:
        log.error(str(e))
        sys.exit(1)
