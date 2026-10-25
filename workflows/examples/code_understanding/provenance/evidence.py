"""Give every row grounding writes the chunks it was derived from.

GraphRAG ties each entity and relationship to the text units that produced it,
and its later workflows assume every row has some. ``create_communities``
explodes a relationship's ``text_unit_ids`` and sorts the result, so a row
carrying ``[]`` explodes to NaN and the sort raises ``'<' not supported between
'str' and 'float'``. The rows grounding writes all carried ``[]``: a created
file, a synthesized package, every ``BELONGS_TO`` and every ``DEFINED_IN``. So
a grounded graph could not be clustered as soon as one community held a
``BELONGS_TO`` edge between two of its members, which on the celery 120b arm
is every time. Found by running the community stage on a grounded index for the
first time; ``augur-test/corpus/MANIFEST.md`` has the reproduction.

The rule is the honest one. A row AUGUR writes is derived from documents, so
it cites their chunks:

* an entity for an indexed file cites that file's chunks;
* a package entity cites the chunks of the documents under it;
* a relationship cites its source entity's chunks, or failing that its
  target's.

The repository entity cites nothing, because no document proves it more than
any other, and it is only ever an edge's target. ``provenance.containment``
sets its own rows, since it knows the one declaring document and the fallback
here would give a Ruby ``JEKYLL DEFINED_IN`` edge the module's chunks from
every file.
"""

from __future__ import annotations

from typing import Any

from .corpus import canonical_key, dotted_key, from_documents
from .grounding_report import GroundingReport
from .tables import _missing


def _empty(cell) -> bool:
    if _missing(cell):
        return True
    try:
        return len(cell) == 0
    except TypeError:
        return False


def units_by_document(documents) -> dict[str, list[str]]:
    """Each document's chunk ids, keyed by the path its header names."""
    if documents is None or "text_unit_ids" not in documents or "text" not in documents:
        return {}
    corpus = from_documents(documents)
    by_id = {
        str(i): list(u)
        for i, u in zip(documents["id"].astype(str), documents["text_unit_ids"], strict=True)
        if not _empty(u)
    } if "id" in documents else {}
    return {f.path: by_id[f.document_id] for f in corpus.files if f.document_id in by_id}


def backfill(entities, relationships, documents, report: GroundingReport | None = None) -> tuple[Any, Any]:
    """Fill ``text_unit_ids`` on every entity and relationship row that has none."""
    units = units_by_document(documents)
    if not units or "text_unit_ids" not in entities:
        return entities, relationships

    # Every key a document answers to, so a package prefix matches Java's
    # declared name as well as the path fold.
    corpus = from_documents(documents)
    keys = {f.path: keys for f, keys in corpus.keyed_files()}
    by_title = {f.title: units[f.path] for f in corpus.files if f.path in units}

    def for_entity(title: str) -> list[str]:
        if title in by_title:
            return by_title[title]
        key = canonical_key(title)
        prefix = key + "."
        found: list[str] = []
        for path, chunk_ids in units.items():
            if any(k == key or k.startswith(prefix) for k in keys.get(path, ())):
                found.extend(u for u in chunk_ids if u not in found)
        return found

    entities = entities.copy()
    filled = 0
    for i in entities.index:
        if _empty(entities.at[i, "text_unit_ids"]) and (found := for_entity(str(entities.at[i, "title"]))):
            entities.at[i, "text_unit_ids"] = found
            filled += 1

    edges = 0
    if relationships is not None and len(relationships) and "text_unit_ids" in relationships:
        # ``list`` because a table read back from parquet holds numpy arrays,
        # whose truth value is ambiguous.
        cited = {
            dotted_key(str(t)): list(u)
            for t, u in zip(entities["title"], entities["text_unit_ids"], strict=True)
            if not _empty(u)
        }
        relationships = relationships.copy()
        for i in relationships.index:
            if not _empty(relationships.at[i, "text_unit_ids"]):
                continue
            ends = (relationships.at[i, "source"], relationships.at[i, "target"])
            found = next((cited[k] for e in ends if (k := dotted_key(str(e))) in cited), None)
            if found:
                relationships.at[i, "text_unit_ids"] = list(found)
                edges += 1

    if report is not None:
        report.evidence_entities = filled
        report.evidence_relationships = edges
    return entities, relationships
