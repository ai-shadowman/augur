"""Run the deterministic repairs inside GraphRAG's own pipeline, not after it.

:mod:`provenance.stitch` can rewrite a finished output directory, but by then
most of the pipeline has already read what it corrects. ``create_communities``
partitions the graph, ``create_community_reports`` summarises the descriptions
and ``generate_text_embeddings`` vectorises them, so a post-hoc pass leaves the
communities, the reports and the vector store describing a graph that no longer
exists. Registering the work as a workflow immediately after ``extract_graph``
puts it in front of all three, and the staleness problem stops existing instead
of being reported.

Two repairs run here, both of them joins against things AUGUR already knows.
:mod:`provenance.graph` reconciles the graph's shape against the documents that
were actually indexed, and :mod:`provenance.stitch` writes declared version
constraints into the descriptions.

Getting the facts to that point takes three steps, because the checkout the
facts come from is gone by indexing time: ``reset_environment`` deletes it, and
under Kubeflow indexing is a different pod entirely.

1. :func:`capture` runs at clone time and writes ``provenance.json`` beside the
   generated code metadata, which is the artifact that already travels between
   stages.
2. :func:`consolidate` runs at index time, merges every sidecar it finds in the
   copied input tree, and leaves one file at the project root.
3. :func:`install` adds the workflow to the pipeline, and :func:`run_workflow`
   reads that file and stitches the entity table in place.

Every step degrades to a no-op with a log line. A missing sidecar means the
graph keeps the descriptions the model wrote, which is exactly where it was
before this module existed; failing a multi-hour index over it would trade a
recoverable gap for an unrecoverable one.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from . import facts as facts_module
from . import stitch
from .edge_evidence import EdgeEvidence
from .models import RepoFacts

logger = logging.getLogger(__name__)

#: Sidecar written next to the generated code metadata and carried forward.
FACTS_FILENAME = "provenance.json"

#: Overrides where :func:`load_facts` looks, for a run assembled by hand.
FACTS_ENV = "AUGUR_PROVENANCE_FACTS"

#: The name the stitch is registered under in GraphRAG's workflow registry.
WORKFLOW_NAME = "stitch_provenance"

#: The stitch runs directly after this workflow, which is the earliest point
#: where the entity and relationship tables exist.
#:
#: Running before ``finalize_graph`` rather than after it is deliberate, and it
#: is what keeps :mod:`provenance.graph` short. ``finalize_entities``
#: regenerates ``id`` and ``human_readable_id``, computes ``degree`` and the
#: layout, and drops duplicate titles; ``finalize_relationships`` dedupes
#: ``(source, target)`` and recomputes ``combined_degree``. Correcting the
#: graph first means none of that has to be maintained by hand, and community
#: detection, the community reports and the embeddings are all built from the
#: corrected graph instead of from the extracted one.
AFTER_WORKFLOW = "extract_graph"

#: The edge verdict's per-row table is written by a second workflow, after
#: ``finalize_graph``, because that is where relationships get the ``id`` the
#: table is keyed on. The split itself is computed in the grounding pass and
#: lands in its report; this re-scores the finalised rows with the same
#: function, which sees the same rows since grounding already folded the
#: duplicate pairs finalisation would drop.
VERDICT_WORKFLOW_NAME = "edge_verdicts"
VERDICT_AFTER_WORKFLOW = "finalize_graph"
#: The table's name in output storage, so it uploads with everything else.
VERDICT_TABLE = "edge_verdicts"


def facts_path(root: str | Path) -> Path:
    """Where the consolidated facts for an indexing run live."""
    override = os.getenv(FACTS_ENV)
    return Path(override) if override else Path(root) / FACTS_FILENAME


def capture(checkout: str | Path, destination: str | Path, **kwargs: Any) -> Path | None:
    """Extract facts from a fresh checkout and write the sidecar.

    Called while the clone still exists. Extraction failure is logged and
    swallowed: provenance is an enrichment, and taking down a data generation
    run over it would cost more than the facts are worth.
    """
    try:
        collected = facts_module.collect(checkout, **kwargs)
    except Exception:  # noqa: BLE001 - enrichment; no failure here is worth the run
        logger.warning("provenance extraction failed for %s; indexing will "
                       "proceed without declared constraints", checkout, exc_info=True)
        return None

    path = Path(destination) / FACTS_FILENAME
    facts_module.write(collected, path)
    logger.info(
        "provenance: %d declaration(s) from %s captured at %s",
        len(collected.packages), checkout, path,
    )
    return path


def merge_facts(records: list[RepoFacts], root: str) -> RepoFacts:
    """Combine per-repository facts into one record for a multi-repo index.

    The git identity is deliberately dropped. Each record names a different
    remote, and a graph built from several checkouts has no single repository
    for one URL to belong to, so keeping any of them would attach the wrong
    remote to every repository entity but one. Declarations survive the merge
    because a constraint is true of its own manifest regardless of how many
    other repositories are in the index.
    """
    if len(records) == 1:
        return records[0]

    merged = RepoFacts(root=root, multi_repo=True)
    symbols: dict[str, set[str]] = {}
    for record in records:
        merged.packages.extend(record.packages)
        merged.base_images.extend(record.base_images)
        merged.own_modules.extend(record.own_modules)
        merged.imported_names.extend(record.imported_names)
        # The two inventories decide whether an entity naming something real
        # but unindexed was recalled rather than read. Dropping them here left
        # every multi-repo index unable to tell leakage from invention.
        merged.source_files.extend(record.source_files)
        for key, names in record.declared_symbols.items():
            symbols.setdefault(key, set()).update(names)
        merged.warnings.extend(record.warnings)
    merged.own_modules = sorted(set(merged.own_modules))
    merged.imported_names = sorted(set(merged.imported_names))
    merged.source_files = sorted(set(merged.source_files))
    merged.declared_symbols = {key: sorted(names) for key, names in symbols.items()}
    merged.edge_evidence = EdgeEvidence.merge(r.edge_evidence for r in records)
    return merged


def consolidate(input_dir: str | Path, root: str | Path,
                multi_repo: bool = False) -> Path | None:
    """Move sidecars out of the indexed corpus and merge them at the root.

    The sidecars arrive by being copied along with the code metadata, so they
    are sitting in the directory GraphRAG is about to read. They are removed
    from it for the same reason ``durations.json`` is: the input tree should
    contain the corpus and nothing else.
    """
    input_dir = Path(input_dir)
    sidecars = sorted(input_dir.rglob(FACTS_FILENAME))
    if not sidecars:
        logger.info("provenance: no %s under %s; the stitch will be skipped",
                    FACTS_FILENAME, input_dir)
        return None

    records = []
    for sidecar in sidecars:
        try:
            records.append(facts_module.read(sidecar))
        except Exception:  # noqa: BLE001 - one bad sidecar must not sink the rest
            logger.warning("provenance: could not read %s", sidecar, exc_info=True)
        sidecar.unlink(missing_ok=True)

    if not records:
        return None

    merged = merge_facts(records, root=str(root))
    merged.multi_repo = merged.multi_repo or multi_repo
    destination = facts_module.write(merged, Path(root) / FACTS_FILENAME)
    logger.info("provenance: merged %d sidecar(s) into %s (%d declarations)",
                len(records), destination, len(merged.packages))
    return destination


def load_facts(root: str | Path) -> RepoFacts | None:
    """Read the consolidated facts for a run, or ``None`` if there are none."""
    path = facts_path(root)
    if not path.exists():
        return None
    try:
        return facts_module.read(path)
    except Exception:  # noqa: BLE001 - a bad facts file costs the stitch, not the index
        logger.warning("provenance: could not read %s; skipping the stitch",
                       path, exc_info=True)
        return None


def reconcile(repo_facts: RepoFacts, corpus_record) -> list[str]:
    """Make the facts agree with the headers the model was actually shown.

    ``facts`` derives a slug from the git remote and branch; data generation
    writes its own slug into every document header. For celery those were
    ``celery-celery-master`` and ``celery-v2-5-5``, and the verifier compared
    the graph against the derived one and reported a defect of its own making.

    The header wins. It is what was put in front of the model, so it is the
    only value the graph can be held to. Disagreements are returned rather
    than swallowed, because two slugs for one repository is worth seeing.

    ``runtime`` moves in the same direction for a different reason: nothing
    derives it from a checkout, so the header is not merely authoritative, it
    is the only source there is.
    """
    notes: list[str] = []
    for attribute in ("git_repo", "git_slug", "runtime"):
        shown = getattr(corpus_record, attribute)
        derived = getattr(repo_facts, attribute)
        if not shown or shown == derived:
            continue
        if derived:
            notes.append(
                f"{attribute}: headers say {shown!r}, facts derived {derived!r}; "
                "using the header"
            )
        setattr(repo_facts, attribute, shown)
    return notes


async def run_workflow(config, context):
    """GraphRAG workflow entry point for the grounding pass and the stitch.

    Signature and return type are dictated by GraphRAG: the pipeline calls
    every workflow as ``run_workflow(config, context)`` and expects the official
    output to be written back to storage rather than returned.

    Two passes, in order. :mod:`provenance.graph` reconciles the graph's shape
    against the documents that were indexed, then :mod:`provenance.stitch`
    writes the declared constraints into the descriptions. Shape first, so the
    constraint lands on the surviving entity rather than on one that is about
    to be merged away.
    """
    from graphrag.index.typing.workflow import WorkflowFunctionOutput
    from graphrag.utils.storage import load_table_from_storage, write_table_to_storage

    from . import corpus as corpus_module
    from . import graph as graph_module
    from . import grounding_report

    result: dict[str, Any] = {}
    entities = await load_table_from_storage("entities", context.output_storage)
    relationships = await load_table_from_storage("relationships", context.output_storage)

    repo_facts = load_facts(config.root_dir)

    documents = await load_table_from_storage("documents", context.output_storage)
    corpus_record = corpus_module.from_documents(documents)
    if len(corpus_record):
        if repo_facts is not None:
            for note in reconcile(repo_facts, corpus_record):
                logger.warning("provenance: %s", note)
        # Optional: it only feeds the relative-import resolution, and the
        # resolver falls back to the corpus when a producing document cannot
        # be identified.
        try:
            text_units = await load_table_from_storage("text_units", context.output_storage)
        except Exception as exc:  # noqa: BLE001 - the table is a bonus, not a dependency
            logger.debug("provenance: no text_units table (%s); "
                         "relative imports resolve against the corpus alone", exc)
            text_units = None
        entities, relationships, grounding = graph_module.ground(
            corpus_record, entities, relationships, text_units, repo_facts, documents
        )
        logger.info("provenance grounding:\n%s", grounding_report.render(grounding))
        result["grounding"] = grounding.to_dict()
        await write_table_to_storage(entities, "entities", context.output_storage)
        await write_table_to_storage(relationships, "relationships", context.output_storage)
    else:
        logger.warning("provenance: no document headers parsed; graph not grounded")

    if repo_facts is None:
        logger.info("provenance: no facts for %s; entity descriptions left as "
                    "the model wrote them", config.root_dir)
        return WorkflowFunctionOutput(result=result or None)

    # The vocabulary the extraction prompt was actually given, read off the
    # same config the run used rather than restated here, so the two cannot
    # drift apart.
    entity_types = getattr(
        getattr(config, "extract_graph", None), "entity_types", None,
    )
    stitched, report = stitch.stitch_entities(repo_facts, entities, entity_types)
    if report.changes:
        await write_table_to_storage(stitched, "entities", context.output_storage)
    logger.info("provenance stitch:\n%s", stitch.render(report))
    result["stitch"] = report.to_dict()

    return WorkflowFunctionOutput(result=result)


async def run_verdict_workflow(config, context):
    """GraphRAG workflow entry point that writes ``edge_verdicts`` to ``output/``."""
    from graphrag.index.typing.workflow import WorkflowFunctionOutput
    from graphrag.utils.storage import load_table_from_storage, write_table_to_storage

    from . import edge_verdict

    relationships = await load_table_from_storage("relationships", context.output_storage)
    table = edge_verdict.table(relationships, load_facts(config.root_dir))
    if table is None:
        logger.info("provenance: no edge evidence in the facts; %s not written", VERDICT_TABLE)
        return WorkflowFunctionOutput(result=None)
    await write_table_to_storage(table, VERDICT_TABLE, context.output_storage)
    counts = table[edge_verdict.VERDICT_COLUMN].value_counts().to_dict()
    logger.info("provenance: %s written, %d row(s): %s", VERDICT_TABLE, len(table), counts)
    return WorkflowFunctionOutput(result={"edge_verdicts": counts})


def install(config, method: str = "standard") -> bool:
    """Insert the stitch into ``config``'s workflow list, after ``finalize_graph``.

    The list is derived from whatever the run would otherwise have used rather
    than written out here, so a GraphRAG upgrade that adds or reorders a
    workflow keeps working and only this one stage stays pinned to its
    position. Setting ``config.workflows`` covers both cases, since
    ``PipelineFactory.create_pipeline`` prefers it over the named pipeline.
    """
    from graphrag.index.workflows.factory import PipelineFactory

    PipelineFactory.register(WORKFLOW_NAME, run_workflow)
    PipelineFactory.register(VERDICT_WORKFLOW_NAME, run_verdict_workflow)

    names = list(config.workflows or PipelineFactory.pipelines.get(method, []))
    if WORKFLOW_NAME in names:
        return True
    if AFTER_WORKFLOW not in names:
        logger.warning(
            "provenance: %r is not in the %s pipeline (%s), so the stitch has "
            "no defined position and was not installed",
            AFTER_WORKFLOW, method, ", ".join(names) or "empty",
        )
        return False

    names.insert(names.index(AFTER_WORKFLOW) + 1, WORKFLOW_NAME)
    _install_verdict(names)
    config.workflows = names
    logger.info("provenance: stitch installed after %s", AFTER_WORKFLOW)
    return True


def _install_verdict(names: list[str]) -> None:
    """Place the verdict table after finalisation, or say why it is absent.

    Missing it costs the per-row table only; the split is still in the
    grounding report, so the stitch installs either way.
    """
    if VERDICT_WORKFLOW_NAME in names:
        return
    if VERDICT_AFTER_WORKFLOW not in names:
        logger.warning("provenance: no %r in the pipeline, so %s is not written",
                       VERDICT_AFTER_WORKFLOW, VERDICT_TABLE)
        return
    names.insert(names.index(VERDICT_AFTER_WORKFLOW) + 1, VERDICT_WORKFLOW_NAME)
