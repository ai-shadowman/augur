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

from . import imports, symbols
from .corpus import canonical_key, dotted_key
from .declarations import namespace_for
from .dialects import dialect_for
from .edges import EXTERNAL, IMPORTS, INHERITS, INTERNAL, UNRESOLVED, scan_checkout
from .edges.model import Edge, Inventory, checkout_files

#: One serialised edge: ``(kind, source, target, target class, path)``.
#: ``target`` is ``None`` exactly when the class is unresolved.
EdgeRow = tuple[str, str, "str | None", str, str]


#: Whether a module-level import binds a name that other modules can import
#: back out of this one, per dialect. Python's does: ``from optparse import
#: make_option as Option`` makes ``celery.bin.base.Option`` importable, and
#: every ``from .x import Y`` in a package ``__init__`` is a re-export. Java's
#: ``import`` only abbreviates a type name; a TypeScript ``import`` binds
#: locally and re-exports only through ``export … from``, which the oracle
#: already reads as an edge; Ruby's ``require`` binds nothing. An unrecorded
#: dialect raises, as ``declarations.NAMESPACE_RULES`` does.
IMPORT_BINDS = {"python": True, "java": False, "typescript": False, "ruby": False}


#: Where a bare name used in a file is looked up before the outermost scope,
#: per dialect. ``module``: the file's own namespace, which since Python's
#: import-bound names are keyed there covers ``from x import Name`` too.
#: ``package``: the claimant's enclosing types down to the file's declared
#: package, then the file's imports. ``lexical``: Ruby's ``Module.nesting``,
#: the claimant's enclosing modules innermost first. A TypeScript import
#: names a file, not the binding, so only the file's own names are reached.
#: An unrecorded dialect raises.
SCOPES = {"python": "module", "typescript": "module", "java": "package", "ruby": "lexical"}


def _rule(table: dict, dialect, what: str):
    if dialect.name not in table:
        raise ValueError(
            f"no {what} rule recorded for dialect {dialect.name!r}; "
            f"add it to edge_evidence with the reason"
        )
    return table[dialect.name]


def _binds(dialect) -> bool:
    return _rule(IMPORT_BINDS, dialect, "import-binding")


def _import_bindings(source: str, dialect, prefix: str, written_to: dict[str, str]):
    """``(bound key, imported key or None)`` for each module-level name an import binds.

    ``written_to`` maps a module as this file writes it (``.base``, ``..``)
    to the internal key the oracle resolved it to, so nothing is resolved
    twice and the two cannot disagree. An external or unresolved module
    gives ``None``: the name is still bound here, there is just nowhere
    inside the checkout to follow it.
    """
    captures, _ = imports.import_captures_in(source, dialect)
    for capture in captures:
        if capture.scoped:
            continue
        module = written_to.get(capture.written)
        if not capture.names:
            # ``import a.b as x`` binds ``x`` to ``a.b``; ``import a.b`` binds ``a``.
            name = capture.alias or capture.written.split(".", 1)[0]
            target = module if capture.alias else written_to.get(name)
            yield dotted_key(f"{prefix}.{name}"), target
            continue
        for imported, alias in capture.names:
            if imported != "*":
                target = f"{module}.{dotted_key(imported)}" if module else None
                yield dotted_key(f"{prefix}.{alias or imported}"), target


def node_files(
    root: Path, inventory: Inventory, edges: Iterable[Edge],
) -> tuple[dict[str, set[str]], dict[str, str], set[str]]:
    """Every groundable node key in the checkout, to the files that declare it.

    Also returns each file's namespace (``declarations.namespace_for``),
    which is where a bare name the file uses is looked up first, and the
    **opaque** keys: names whose value the oracle cannot see, because an
    assignment (``pwd = try_import("pwd")``) or an import from outside the
    checkout (``import os``) binds them and nothing declares them. A bare
    name must not resolve to one, since what it holds is unknown.

    A name bound by import is keyed to the file that binds it and, chased
    through the import, to the file that declares it. ``celery.app.app_or_default``
    is both ``celery/app/__init__.py``, which re-exports it, and the module
    that defines it, since a claim about either spelling is a claim about one
    function.
    """
    edges = list(edges)
    written_to: dict[str, dict[str, str]] = collections.defaultdict(dict)
    for e in edges:
        if e.kind == IMPORTS and e.target_class == INTERNAL:
            written_to[e.path][e.written] = e.target

    found: dict[str, set[str]] = collections.defaultdict(set)
    namespaces: dict[str, str] = {}
    declared: set[str] = set()
    opaque: set[str] = set()
    chase: list[tuple[str, str]] = []
    for path in sorted(inventory.paths):
        dialect = dialect_for(path)
        if dialect is None:
            continue
        source = (root / path).read_text(encoding="utf-8", errors="replace")
        found[canonical_key(path)].add(path)
        prefix = namespaces[path] = namespace_for(path, source, dialect)
        read = symbols.symbols_in(source, dialect)
        for name in (*read.classes, *read.functions, *read.methods):
            key = dotted_key(f"{prefix}.{name}" if prefix else name)
            found[key].add(path)
            declared.add(key)
        for name in read.bindings:
            key = dotted_key(f"{prefix}.{name}" if prefix else name)
            found[key].add(path)
            opaque.add(key)
        if _binds(dialect):
            for bound, target in _import_bindings(source, dialect, prefix, written_to[path]):
                found[bound].add(path)
                if target:
                    chase.append((bound, target))
                else:
                    opaque.add(bound)
    # Oracle edge sources (Java's type key, Python's module key) are node keys
    # too, and resolve to the file that states them.
    for e in edges:
        found[e.source].add(e.path)
    # A re-export of a re-export needs more than one pass; stop when none adds.
    changed = True
    while changed:
        changed = False
        for bound, target in chase:
            extra = found.get(target, set()) - found[bound]
            if extra:
                found[bound] |= extra
                changed = True
    return found, namespaces, opaque - declared - {b for b, _ in chase}


class EdgeEvidence:
    """The oracle's edges, indexed the ways the verdict asks of them."""

    def __init__(self, edges: Iterable[EdgeRow], files: dict[str, Iterable[str]],
                 namespaces: dict[str, str] | None = None, opaque: Iterable[str] = ()):
        self.namespaces: dict[str, str] = dict(namespaces or {})
        self.opaque: frozenset[str] = frozenset(opaque)
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
        return cls(rows, *node_files(root, inventory, scan.edges))

    def __bool__(self) -> bool:
        return bool(self.edges)

    def to_dict(self) -> dict[str, Any]:
        return {
            "edges": [list(e) for e in self.edges],
            "node_files": {k: sorted(v) for k, v in sorted(self.file_of.items())},
            "namespaces": dict(sorted(self.namespaces.items())),
            "opaque": sorted(self.opaque),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> EdgeEvidence:
        data = data or {}
        return cls((tuple(e) for e in data.get("edges", ())), data.get("node_files", {}),
                   data.get("namespaces", {}), data.get("opaque", ()))

    @classmethod
    def merge(cls, parts: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """Union serialised evidence from several repositories, as ``merge_facts`` does."""
        edges: set[tuple] = set()
        files: dict[str, set[str]] = collections.defaultdict(set)
        namespaces: dict[str, str] = {}
        opaque: set[str] = set()
        for part in parts:
            edges.update(tuple(e) for e in (part or {}).get("edges", ()))
            for key, paths in (part or {}).get("node_files", {}).items():
                files[key].update(paths)
            namespaces.update((part or {}).get("namespaces", {}))
            opaque.update((part or {}).get("opaque", ()))
        return cls(edges, files, namespaces, opaque).to_dict()

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

    def in_scope(self, name: str, claimant: str, files: Iterable[str]) -> str | None:
        """The node key a bare ``name`` means where ``claimant`` uses it, or ``None``.

        Tried innermost first per :data:`SCOPES`. ``None`` leaves the caller to
        the outermost scope, an exact global key, which is a top-level module
        or constant in every dialect here. What it never does is take the one
        declaration anywhere that happens to share the name: jekyll's
        ``StaticFileDrop`` inherits ``Jekyll::Drops::Drop``, not the
        top-level ``Drop`` a benchmark script declares. Nor an opaque name: a
        bare ``PWD`` where ``pwd = try_import("pwd")`` ran means the library,
        and the oracle cannot see that.
        """
        for path in sorted(files):
            dialect = dialect_for(path)
            if dialect is None:
                continue
            rule = _rule(SCOPES, dialect, "scope")
            namespace = self.namespaces.get(path, "")
            candidates = []
            if rule == "module":
                candidates.append(f"{namespace}.{name}" if namespace else name)
            else:
                # Only a symbol has enclosing types; a file's key is a path.
                if claimant != canonical_key(path) and (
                        not namespace or claimant.startswith(namespace + ".")):
                    parts = claimant.split(".")
                    floor = len(namespace.split(".")) if namespace else 1
                    candidates += [".".join([*parts[:i], name]) for i in range(len(parts), floor - 1, -1)]
                if rule == "package":
                    if namespace:
                        candidates.append(f"{namespace}.{name}")
                    candidates += sorted(
                        t for k, _, t, _, p in self.edges
                        if p == path and k == IMPORTS and t and t.endswith("." + name)
                    )
            for key in candidates:
                if key in self.file_of and key not in self.opaque:
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
