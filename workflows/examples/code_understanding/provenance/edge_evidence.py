"""The edge oracle's answer for one checkout, in a form that survives the trip.

The edge verdict runs at index time, where the checkout is gone: data
generation reads it, and everything later sees the documents and the facts
sidecar. So the evidence the verdict needs is captured beside
``declared_symbols`` in :class:`provenance.models.RepoFacts`, and this module
is both the capture and the reader.

Two things travel. ``edges`` is :func:`provenance.edges.scan_checkout`'s
output, every IMPORTS and INHERITS pair with its target class. ``node_files``
maps every node key the checkout can ground (a file's canonical key, each
symbol under its dialect's namespace, each oracle edge source) to the files
that declare it, which is what lets a method claim its module's import.

Both are built from the **whole checkout**, never the indexed corpus, for the
reason :mod:`provenance.edges.model` gives: a resolver that accepts the only
candidate a subset knows about answers confidently and wrongly.
"""

from __future__ import annotations

import collections
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import symbols
from .corpus import canonical_key, dotted_key
from .declarations import namespace_for
from .dialects import dialect_for
from .edges import EXTERNAL, INHERITS, INTERNAL, UNRESOLVED, scan_checkout
from .edges.model import Edge, Inventory, checkout_files

#: One serialised edge: ``(kind, source, target, target class, path)``.
#: ``target`` is ``None`` exactly when the class is unresolved.
EdgeRow = tuple[str, str, "str | None", str, str]


def node_files(root: Path, inventory: Inventory, edges: Iterable[Edge]) -> dict[str, set[str]]:
    """Every groundable node key in the checkout, to the files that declare it."""
    found: dict[str, set[str]] = collections.defaultdict(set)
    for path in sorted(inventory.paths):
        dialect = dialect_for(path)
        if dialect is None:
            continue
        source = (root / path).read_text(encoding="utf-8", errors="replace")
        found[canonical_key(path)].add(path)
        prefix = namespace_for(path, source, dialect)
        read = symbols.symbols_in(source, dialect)
        for name in (*read.classes, *read.functions, *read.methods):
            found[dotted_key(f"{prefix}.{name}" if prefix else name)].add(path)
    # Oracle edge sources (Java's type key, Python's module key) are node keys
    # too, and resolve to the file that states them.
    for e in edges:
        found[e.source].add(e.path)
    return found


class EdgeEvidence:
    """The oracle's edges, indexed the ways the verdict asks of them."""

    def __init__(self, edges: Iterable[EdgeRow], files: dict[str, Iterable[str]]):
        self.edges: list[EdgeRow] = sorted(edges, key=lambda e: tuple(x or "" for x in e))
        self.file_of: dict[str, set[str]] = {k: set(v) for k, v in files.items()}
        self.internal: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
        self.pairs: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
        self.external: dict[str, set[tuple[str, str]]] = collections.defaultdict(set)
        self.unresolved: collections.Counter = collections.Counter()
        #: Direct internal INHERITS targets of each type key.
        self.parents: dict[str, set[str]] = collections.defaultdict(set)
        #: Internal targets each file's edges reach, with their kinds.
        self.targets: dict[str, dict[str, set[str]]] = collections.defaultdict(
            lambda: collections.defaultdict(set))
        self._supertypes: dict[str, set[str]] = {}
        for kind, source, target, target_class, path in self.edges:
            if target_class == INTERNAL:
                self.pairs[(source, target)].add(kind)
                self.targets[path][target].add(kind)
                if kind == INHERITS:
                    self.parents[source].add(target)
                for tf in self.file_of.get(target, ()):
                    self.internal[(path, tf)].add(kind)
            elif target_class == EXTERNAL:
                self.external[path].add((target, kind))
            elif target_class == UNRESOLVED:
                self.unresolved[path] += 1

    @classmethod
    def from_checkout(cls, root: str | Path) -> EdgeEvidence:
        root = Path(root)
        inventory = Inventory(checkout_files(root), root)
        scan = scan_checkout(root, inventory)
        rows = [(e.kind, e.source, e.target, e.target_class, e.path) for e in scan.edges]
        return cls(rows, node_files(root, inventory, scan.edges))

    def __bool__(self) -> bool:
        return bool(self.edges)

    def to_dict(self) -> dict[str, Any]:
        return {
            "edges": [list(e) for e in self.edges],
            "node_files": {k: sorted(v) for k, v in sorted(self.file_of.items())},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> EdgeEvidence:
        data = data or {}
        return cls((tuple(e) for e in data.get("edges", ())), data.get("node_files", {}))

    @classmethod
    def merge(cls, parts: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """Union serialised evidence from several repositories, as ``merge_facts`` does."""
        edges: set[tuple] = set()
        files: dict[str, set[str]] = collections.defaultdict(set)
        for part in parts:
            edges.update(tuple(e) for e in (part or {}).get("edges", ()))
            for key, paths in (part or {}).get("node_files", {}).items():
                files[key].update(paths)
        return cls(edges, files).to_dict()

    def imported_from(self, path: str):
        """``(target key, kinds)`` for every internal edge ``path`` states."""
        return self.targets.get(path, {}).items()

    def supertypes(self, key: str) -> set[str]:
        """Every internal type ``key`` inherits from, directly or through others."""
        if key not in self._supertypes:
            seen: set[str] = set()
            todo = [key]
            while todo:
                for parent in self.parents.get(todo.pop(), ()):
                    if parent not in seen:
                        seen.add(parent)
                        todo.append(parent)
            self._supertypes[key] = seen
        return self._supertypes[key]

    def ground(self, title: str) -> str | None:
        """The node key a title names in the checkout, or ``None``."""
        for key in (dotted_key(title), canonical_key(title)):
            if key in self.file_of:
                return key
        return None

    def files(self, key: str | None) -> set[str]:
        return self.file_of.get(key, set()) if key else set()

    def external_kinds(self, files: set[str], key: str) -> set[str]:
        """Kinds of the external edges from ``files`` whose target is, contains or is inside ``key``."""
        return {
            kind for f in files for t, kind in self.external.get(f, ())
            if t == key or key.startswith(t + ".") or t.startswith(key + ".")
        }
