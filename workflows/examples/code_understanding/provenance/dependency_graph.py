"""The report's dependency diagram, rendered from the index rather than asked for.

``migration-report/0.txt`` used to pay a model to do this: pick entity types in
priority order, keep a running total under twenty nodes, draw the edges between
them, and emit Mermaid. That is a sort and a truncation over two tables already
on disk, so it is done here, and two renders of one index are byte-identical.

The rule is 0.txt's with one change. 0.txt stopped before a level that would
overflow the cap, which on a Python index renders almost nothing: a package
is a document there, so parser-full-120b-dedup has 4 ``PACKAGE`` entities
against 194 ``MODULE``, and the literal rule draws four nodes with no edge
between them. Here the overflowing level fills the remaining slots by degree,
which is what 0.txt's own "most architecturally significant components" asked
for.

Edges are every relationship between two rendered nodes. The relationships
table carries no type, so the diagram cannot be narrowed to imports until the
relationship layer types its edges.
"""

import re
from collections.abc import Iterable

#: Entity types in the order a level is added, highest first.
PRIORITY = ("PACKAGE", "MODULE", "CLASS", "FUNCTION")

#: The node cap, as 0.txt stated it.
MAX_NODES = 20

_WHITESPACE = re.compile(r"\s+")


def _degrees(relationships) -> dict[str, int]:
    """Each title's endpoint count over the whole relationships table.

    Computed rather than read from ``entities.degree``, so the ranking and the
    drawn edges come from the same table even after grounding has rewritten
    titles.
    """
    counts: dict[str, int] = {}
    for column in ("source", "target"):
        for title in relationships[column]:
            counts[title] = counts.get(title, 0) + 1
    return counts


def select_nodes(entities, relationships, cap: int = MAX_NODES) -> list[tuple[str, str]]:
    """``(title, type)`` for every rendered node, in rendering order.

    Levels are taken whole while they fit; the first that does not fit
    contributes its highest-degree members until the cap is reached, and
    nothing below it is considered. Within a level the order is degree
    descending, then title, which is total because titles are unique.
    """
    degree = _degrees(relationships)
    types = entities["type"].fillna("").astype(str).str.upper()
    chosen: list[tuple[str, str]] = []
    seen: set[str] = set()
    for kind in PRIORITY:
        titles = {t for t in entities.loc[types == kind, "title"] if t not in seen}
        level = sorted(titles, key=lambda t: (-degree.get(t, 0), t))
        room = cap - len(chosen)
        chosen.extend((title, kind) for title in level[:room])
        seen.update(level[:room])
        if len(level) > room:
            break
    return chosen


def _label(text: str) -> str:
    """A quoted Mermaid label. ``#quot;`` is Mermaid's own escape for ``"``."""
    return '"' + _WHITESPACE.sub(" ", str(text)).strip().replace('"', "#quot;") + '"'


def _block(lines: Iterable[str]) -> str:
    return "\n".join(["```mermaid", "graph TD", *lines, "```"])


def render(entities, relationships, cap: int = MAX_NODES) -> str:
    """The single-repository diagram as a fenced Mermaid block."""
    nodes = select_nodes(entities, relationships, cap)
    if not nodes:
        levels = ", ".join(k.lower() for k in PRIORITY)
        return f"_No {levels} entities in the index; nothing to draw._"
    index = {title: i for i, (title, _) in enumerate(nodes)}
    edges = sorted(
        {
            (index[s], index[t])
            for s, t in zip(relationships["source"], relationships["target"], strict=True)
            if s in index and t in index
        }
    )
    return _block(
        [f"    n{i}[{_label(title)}]" for i, (title, _) in enumerate(nodes)]
        + [f"    n{s} --> n{t}" for s, t in edges]
    )


def render_repositories(git_repos: Iterable[str]) -> str:
    """The multi-repository diagram: one node per repository, no edges.

    The repositories come from document headers, which AUGUR wrote, so the
    node set is exact. An edge between two repositories would need to know
    which repository declares the target, and an entity seen only in one
    repository's documents may be a library that repository merely mentions.
    That is partial evidence, so no edge is drawn.
    """
    repos = sorted({r for r in git_repos if r})
    if not repos:
        return "_No repository URLs in the document headers; nothing to draw._"
    return _block(f"    n{i}[{_label(repo)}]" for i, repo in enumerate(repos))
