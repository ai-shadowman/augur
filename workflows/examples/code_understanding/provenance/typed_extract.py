"""AUGUR's ``extract_graph``: the relationship type in a field, kept to the end.

GraphRAG's relationship tuple is ``(source, target, description, strength)``,
so the prompt's seven types had nowhere to live but the description, and
:mod:`provenance.edge_types` read them back out of prose. A field cannot ride
the stock code: ``GraphExtractor._process_results`` reads field four as the
description and the last as the weight, builds an undirected ``nx.Graph`` that
folds A->B into B->A, and ``_merge_relationships`` keeps only description,
chunks and weight. So the tuple is now

    ("relationship"<|>source<|>target<|>TYPE<|>description<|>strength)

and this module owns the three places the field would be lost: the parser
records every claim with its direction, the per-chunk strategy turns those
into tokens relative to the row's own columns (``IMPORTS>``, ``CALLS<``), and
the cross-chunk merge unions them into a ``claims`` column. Summarisation is
graphrag's own and keeps the column, and the grounding pass carries it
(``provenance.tables.merge_duplicate_edges``). ``finalize_relationships``
keeps a fixed column list, so the verdict workflow joins it back afterwards.

A five-field record (an index built from the old prompt, or a model that
drops the field) still parses, with no claim unless its fourth field is
exactly a type, which is a model that kept the type and dropped the prose, and :mod:`provenance.edge_types`
falls back to the prose for it. The field is kept raw here and validated where
it is read, so a type outside the vocabulary stays countable.

**Pinned to graphrag 2.6.0.** The strategy, the merge and the workflow body are
adapted from that version's ``index/operations/extract_graph`` and
``index/workflows/extract_graph.py``; :data:`GRAPHRAG_VERSION` is asserted by a
test, so an upgrade fails there rather than silently diverging from upstream.
Relationship layer Phase 9, MANIFEST.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .edge_types import BACKWARD, CLAIMS_COLUMN, FORWARD, PRECEDENCE

logger = logging.getLogger(__name__)

#: The graphrag release the adapted code below was read from.
GRAPHRAG_VERSION = "2.6.0"

#: The name the workflow replaces in GraphRAG's registry.
WORKFLOW_NAME = "extract_graph"

#: Separates a claim's parts while it is still absolute (source, field, target).
_SEP = "\x1f"


def relative_claims(claims: Any, source: str, target: str) -> list[str]:
    """Absolute ``source\\x1ffield\\x1ftarget`` claims as tokens relative to a row.

    ``IMPORTS>`` where the claim's subject is the row's source, ``IMPORTS<``
    where it is the target. A claim naming neither column cannot come from
    this row's edge and is dropped.
    """
    out = []
    for claim in claims or ():
        subject, field, obj = [*str(claim).split(_SEP), "", "", ""][:3]
        if (subject, obj) == (source, target):
            out.append(f"{field}{FORWARD}")
        elif (subject, obj) == (target, source):
            out.append(f"{field}{BACKWARD}")
    return out


def _graph_extractor():
    """The subclass, built on first use so this module imports without graphrag."""
    from graphrag.index.operations.extract_graph.graph_extractor import (
        GraphExtractor,
        _unpack_descriptions,
        _unpack_source_ids,
    )
    from graphrag.index.utils.string import clean_str

    class TypedGraphExtractor(GraphExtractor):
        """``GraphExtractor`` whose relationship records carry a type field."""

        async def _process_results(self, results, tuple_delimiter, record_delimiter):
            import networkx as nx

            graph = nx.Graph()
            for source_doc_id, extracted_data in results.items():
                for record in (r.strip() for r in extracted_data.split(record_delimiter)):
                    attributes = re.sub(r"^\(|\)$", "", record.strip()).split(tuple_delimiter)
                    if attributes[0] == '"entity"' and len(attributes) >= 4:
                        _add_entity(graph, attributes, source_doc_id, self._join_descriptions)
                    elif attributes[0] == '"relationship"' and len(attributes) >= 5:
                        _add_relationship(graph, attributes, source_doc_id, self._join_descriptions)
            return graph

    def _add_entity(graph, attributes, source_doc_id, join):
        # graphrag 2.6.0's entity branch, unchanged.
        name = clean_str(attributes[1].upper())
        entity_type = clean_str(attributes[2].upper())
        description = clean_str(attributes[3])
        if name in graph.nodes():
            node = graph.nodes[name]
            if join:
                node["description"] = "\n".join(list({*_unpack_descriptions(node), description}))
            elif len(description) > len(node["description"]):
                node["description"] = description
            node["source_id"] = ", ".join(list({*_unpack_source_ids(node), str(source_doc_id)}))
            node["type"] = entity_type if entity_type != "" else node["type"]
        else:
            graph.add_node(name, type=entity_type, description=description, source_id=str(source_doc_id))

    def _add_relationship(graph, attributes, source_doc_id, join):
        source = clean_str(attributes[1].upper())
        target = clean_str(attributes[2].upper())
        # Six fields carry the type at four. Five are the old tuple, untyped,
        # unless field four is exactly a type and nothing else: the model kept
        # the type and dropped the description, 25 of 606 smoke rows.
        typed = len(attributes) >= 6
        bare = clean_str(attributes[3]).strip().upper()
        if not typed and bare in PRECEDENCE:
            field, description, typed = bare, clean_str(attributes[3]), True
        else:
            field = bare if typed else ""
            description = clean_str(attributes[4] if typed else attributes[3])
        edge_source_id = clean_str(str(source_doc_id))
        claims = [f"{source}{_SEP}{field}{_SEP}{target}"] if typed else []
        try:
            weight = float(attributes[-1])
        except ValueError:
            weight = 1.0
        for node in (source, target):
            if node not in graph.nodes():
                graph.add_node(node, type="", description="", source_id=edge_source_id)
        if graph.has_edge(source, target):
            data = graph.get_edge_data(source, target)
            if data is not None:
                weight += data["weight"]
                if join:
                    description = "\n".join(list({*_unpack_descriptions(data), description}))
                edge_source_id = ", ".join(list({*_unpack_source_ids(data), str(source_doc_id)}))
                claims = [*data.get(CLAIMS_COLUMN, []), *claims]
        graph.add_edge(source, target, weight=weight, description=description,
                       source_id=edge_source_id, **{CLAIMS_COLUMN: claims})

    return TypedGraphExtractor


async def run_typed(docs, entity_types, cache, args):
    """graphrag 2.6.0's ``run_graph_intelligence`` with the typed extractor.

    The relationships come back with ``claims`` relative to each row's columns,
    which is only knowable once ``to_pandas_edgelist`` has chosen them.
    """
    import networkx as nx
    from graphrag.config.defaults import graphrag_config_defaults
    from graphrag.config.models.language_model_config import LanguageModelConfig
    from graphrag.index.operations.extract_graph.typing import EntityExtractionResult
    from graphrag.language_model.manager import ModelManager

    llm_config = LanguageModelConfig(**args["llm"])
    model = ModelManager().get_or_create_chat_model(
        name="extract_graph", model_type=llm_config.type, config=llm_config, cache=cache,
    )
    extractor = _graph_extractor()(
        model_invoker=model,
        prompt=args.get("extraction_prompt", None),
        max_gleanings=args.get("max_gleanings", graphrag_config_defaults.extract_graph.max_gleanings),
        on_error=lambda e, s, d: logger.error("Entity Extraction Error", exc_info=e,
                                              extra={"stack": s, "details": d}),
    )
    results = await extractor([doc.text.strip() for doc in docs], {
        "entity_types": entity_types,
        "tuple_delimiter": args.get("tuple_delimiter", None),
        "record_delimiter": args.get("record_delimiter", None),
        "completion_delimiter": args.get("completion_delimiter", None),
    })
    graph = results.output
    for _, node in graph.nodes(data=True):
        if node is not None:
            node["source_id"] = ",".join(docs[int(i)].id for i in node["source_id"].split(","))
    for _, _, edge in graph.edges(data=True):
        if edge is not None:
            edge["source_id"] = ",".join(docs[int(i)].id for i in edge["source_id"].split(","))
    entities = [{"title": item[0], **(item[1] or {})} for item in graph.nodes(data=True) if item is not None]
    relationships = nx.to_pandas_edgelist(graph)
    if CLAIMS_COLUMN not in relationships:
        relationships[CLAIMS_COLUMN] = [[] for _ in range(len(relationships))]
    relationships[CLAIMS_COLUMN] = [
        relative_claims(c, s, t)
        for c, s, t in zip(relationships[CLAIMS_COLUMN], relationships["source"], relationships["target"],
                           strict=True)
    ]
    return EntityExtractionResult(entities, relationships, graph)


def merge_relationships(relationship_dfs):
    """graphrag 2.6.0's ``_merge_relationships``, keeping every chunk's claims."""
    import pandas as pd

    frames = [f if CLAIMS_COLUMN in f else f.assign(**{CLAIMS_COLUMN: [[]] * len(f)})
              for f in relationship_dfs]
    return (
        pd.concat(frames, ignore_index=False)
        .groupby(["source", "target"], sort=False)
        .agg(
            description=("description", list),
            text_unit_ids=("source_id", list),
            weight=("weight", "sum"),
            **{CLAIMS_COLUMN: (CLAIMS_COLUMN, lambda cs: [t for c in cs for t in (c or ())])},
        )
        .reset_index()
    )


async def extract(text_units, callbacks, cache, text_column, id_column, strategy,
                  async_mode, entity_types, num_threads):
    """graphrag 2.6.0's ``operations.extract_graph.extract_graph``, typed."""
    import pandas as pd
    from graphrag.index.operations.extract_graph.extract_graph import (
        DEFAULT_ENTITY_TYPES,
        _merge_entities,
    )
    from graphrag.index.operations.extract_graph.typing import Document
    from graphrag.index.utils.derive_from_rows import derive_from_rows

    entity_types = entity_types if entity_types is not None else DEFAULT_ENTITY_TYPES
    config = {**(strategy or {})}

    async def run_strategy(row):
        result = await run_typed([Document(text=row[text_column], id=row[id_column])],
                                 entity_types, cache, config)
        return [result.entities, result.relationships, result.graph]

    results = await derive_from_rows(text_units, run_strategy, callbacks, async_type=async_mode,
                                     num_threads=num_threads, progress_msg="extract graph progress: ")
    entity_dfs = [pd.DataFrame(r[0]) for r in results if r]
    relationship_dfs = [pd.DataFrame(r[1]) for r in results if r]
    return _merge_entities(entity_dfs), merge_relationships(relationship_dfs)


async def run_workflow(config, context):
    """graphrag 2.6.0's ``extract_graph`` workflow, with :func:`extract` in place."""
    from graphrag.index.typing.workflow import WorkflowFunctionOutput
    from graphrag.index.workflows.extract_graph import (
        _validate_data,
        get_summarized_entities_relationships,
    )
    from graphrag.utils.storage import load_table_from_storage, write_table_to_storage

    logger.info("Workflow started: extract_graph (AUGUR typed)")
    text_units = await load_table_from_storage("text_units", context.output_storage)
    llm = config.get_language_model_config(config.extract_graph.model_id)
    summary_llm = config.get_language_model_config(config.summarize_descriptions.model_id)

    entities, relationships = await extract(
        text_units, context.callbacks, context.cache, "text", "id",
        config.extract_graph.resolved_strategy(config.root_dir, llm),
        llm.async_mode, config.extract_graph.entity_types, llm.concurrent_requests,
    )
    if not _validate_data(entities):
        raise ValueError("Entity Extraction failed. No entities detected during extraction.")
    if not _validate_data(relationships):
        raise ValueError("Entity Extraction failed. No relationships detected during extraction.")
    raw_entities, raw_relationships = entities.copy(), relationships.copy()

    entities, relationships = await get_summarized_entities_relationships(
        extracted_entities=entities,
        extracted_relationships=relationships,
        callbacks=context.callbacks,
        cache=context.cache,
        summarization_strategy=config.summarize_descriptions.resolved_strategy(config.root_dir, summary_llm),
        summarization_num_threads=summary_llm.concurrent_requests,
    )
    await write_table_to_storage(entities, "entities", context.output_storage)
    await write_table_to_storage(relationships, "relationships", context.output_storage)
    if config.snapshots.raw_graph:
        await write_table_to_storage(raw_entities, "raw_entities", context.output_storage)
        await write_table_to_storage(raw_relationships, "raw_relationships", context.output_storage)
    logger.info("Workflow completed: extract_graph (AUGUR typed)")
    return WorkflowFunctionOutput(result={"entities": entities, "relationships": relationships})


def register() -> None:
    """Put the typed workflow in GraphRAG's registry under the stock name."""
    from graphrag.index.workflows.factory import PipelineFactory

    PipelineFactory.register(WORKFLOW_NAME, run_workflow)
