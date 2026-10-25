"""Write where each symbol is defined, rather than asking the model.

"X is defined in Y" was the largest single share of untyped relationships on
every corpus Phase 2 measured (74 / 57 / 55% of ``UNTYPED`` rows on celery /
commons-lang / jekyll), and the seven-type vocabulary has no word for it. It
needs none: :mod:`provenance.symbols` already knows what every indexed file
declares and which class encloses each method. So this pass writes those
edges from the source, the way :func:`provenance.graph._belongs_to` writes
``BELONGS_TO``.

Measured before writing (``augur-test/corpus/MANIFEST.md``, Relationship
layer Phase 2b): the model's containment rows are 70 / 61 / 81% true and at
most 3.1% wrong, but they state only 20 to 31% of the direct pairs the indexed
files declare. The missing pairs matter downstream, because GraphRAG clusters
only the largest connected component. On celery 284 method entities had no
edge at all, so no community report could see them. Writing the pairs below
takes the clustered share from 74.8% to 92.1% of entities.

What gets written, and what deliberately does not:

* One edge per **direct** pair: a method to its innermost enclosing class, a
  class or function to that, or to its file when nothing encloses it.
  Bindings are left out. Few have an entity, the model almost never claims
  they are contained, and on celery they would add 52 edges for 11 entities.
* Only between entities that **already exist**. A method the model never
  named is not created here. That would be a second extraction, and every
  node feeds community reports, which cost a model call.
* Not where any row already links the pair, in either orientation. The
  columns carry no direction (graphrag builds each chunk's edges in an
  undirected ``nx.Graph``), so a model row naming the pair the other way round
  already says it. The model's own containment rows stay. Too few are wrong to
  justify replacing them.
* For every file that reopens a namespace. A Ruby file that opens
  ``module Jekyll`` contains that module as much as any other declaration, so
  ``JEKYLL`` gets one edge per such file (18 on the jekyll smoke index). It is
  a hub, but every file already reaches the repository through
  ``BELONGS_TO``, so it joins nothing that was apart.
* To every spelling of a name. ``JEKYLL::COMMANDS::NEW`` and
  ``JEKYLL.COMMANDS.NEW`` fold to one key, so both are the declaration and an
  edge to either is true. Choosing one would leave the other isolated, and
  would choose by iteration order.
"""

from __future__ import annotations

from typing import Any

from . import evidence, symbols
from .corpus import dotted_key, parse_header
from .declarations import namespace_for
from .dialects import dialect_for
from .grounding_report import GroundingReport
from .tables import types_of

#: The token a synthesized description carries. :mod:`provenance.edge_types`
#: reads it back as an AUGUR-written type, so these rows are not counted as
#: the model failing to type an edge.
DEFINED_IN = "DEFINED_IN"

#: The median weight the model gave its own containment rows on all three
#: measured corpora, so a written edge ranks with the model's.
DEFINED_IN_WEIGHT = 10.0


def _pairs(path: str, text: str):
    """``(child key, parent key)`` for each direct containment pair one file declares.

    The immediate prefix of a walker name is its owner when the file
    declares it under any kind, not just as a class. ``symbols`` folds names
    across kinds first-wins, so ``def command`` before ``class Command``
    leaves the class out of ``classes`` while its methods still carry the
    prefix. A prefix the file does not declare (Ruby's compact
    ``class Jekyll::Tags::Link``) is a namespace opened elsewhere, and the
    file is the owner.
    """
    dialect = dialect_for(path)
    if dialect is None:
        return
    found = symbols.symbols_in(text, dialect)
    prefix = namespace_for(path, text, dialect)
    # Compared folded, because Ruby's walker can mix separators in one name:
    # ``class Tags::Link`` inside ``module Jekyll`` is ``Jekyll.Tags::Link``,
    # and ``Jekyll.Tags`` is the prefix that has to be found undeclared.
    declared = {dotted_key(n) for n in (*found.classes, *found.functions, *found.methods, *found.bindings)}

    def qualify(name: str) -> str:
        return dotted_key(f"{prefix}.{name}" if prefix else name)

    for name in (*found.methods, *found.classes, *found.functions):
        owner = dotted_key(name).rpartition(".")[0]
        yield qualify(name), (qualify(owner) if owner in declared else None)


def _spellings(entities) -> dict[str, set[str]]:
    """Each symbol key, mapped to every entity title that spells it.

    Keyed on :func:`dotted_key` alone. :func:`canonical_key` would also fold
    ``CELERY/SCHEDULES/CRONTAB.PY`` onto the class ``celery.schedules.crontab``,
    and a file is not a class. A repository-typed row is left out: a model
    that types the gem module ``JEKYLL`` as the repository would otherwise get
    containment edges on a node ``ground_repository`` renames to the slug.
    """
    from .graph import REPOSITORY_TYPES  # graph imports this module

    spellings: dict[str, set[str]] = {}
    for title, kind in zip(entities["title"].astype(str), types_of(entities), strict=True):
        if kind not in REPOSITORY_TYPES:
            spellings.setdefault(dotted_key(title), set()).add(title)
    return spellings


def synthesize(entities, relationships, documents, report: GroundingReport | None = None) -> Any:
    """Append a ``DEFINED_IN`` edge for each direct pair no row already links.

    Call it after :func:`provenance.graph.ground_symbols`, so a symbol the
    model filed under the wrong module has already been moved to the title
    the declarations key to, and after :func:`provenance.graph.ground_modules`,
    which gives every indexed file the title :attr:`CorpusFile.title`.
    """
    import pandas as pd

    if documents is None or "text" not in documents or not len(entities):
        return relationships

    spellings = _spellings(entities)
    titles = set(entities["title"].astype(str))

    linked: set[frozenset[str]] = set()
    if relationships is not None and len(relationships):
        linked = {
            frozenset((str(s), str(t)))
            for s, t in zip(relationships["source"], relationships["target"], strict=False)
        }

    # The declaring document's chunks are the edge's evidence, and GraphRAG's
    # community workflow cannot take a row that cites none.
    units = evidence.units_by_document(documents)

    rows = []
    for text in documents["text"]:
        header = parse_header(str(text))
        if header is None:
            continue
        cited = units.get(header.path, [])
        file_titles = {header.title} & titles
        for child_key, parent_key in _pairs(header.path, str(text)):
            parents = spellings.get(parent_key, set()) if parent_key else file_titles
            for child in sorted(spellings.get(child_key, ())):
                for parent in sorted(parents):
                    pair = frozenset((child, parent))
                    if child == parent or pair in linked:
                        continue
                    linked.add(pair)
                    rows.append({
                        "source": child,
                        "target": parent,
                        "description": f"{child} {DEFINED_IN} {parent}",
                        "weight": DEFINED_IN_WEIGHT,
                        "text_unit_ids": list(cited),
                    })

    if report is not None:
        report.defined_in_added = len(rows)
    if not rows:
        return relationships
    added = pd.DataFrame(rows)
    if relationships is None or not len(relationships):
        return added
    return pd.concat([relationships, added], ignore_index=True)
