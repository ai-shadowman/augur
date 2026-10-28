"""The CALLS oracle's answer for one checkout, in a form that survives the trip.

The counterpart of :mod:`provenance.edge_evidence` for :mod:`provenance.edges.calls`.
The verdict runs at index time with no checkout, so everything it asks of the
call graph is read here, at clone time, and travels in the facts sidecar:

``internal``/``external``  settled callees per caller key.
``unsettled``              how many of each caller's sites did not settle. A
                           caller with any is never evidence of absence.
``binds``                  settled callees per declaration a decorator or a
                           binding's initialiser runs on behalf of.
``by_name``                callers that reach a dynamic import at any depth.
                           A settled call graph still reaches a class by name
                           through ``instantiate("pkg.mod:Cls")``.
``properties``             method keys whose read is a call no site states.
``bindings``               keys bound only by assignment: what they hold is
                           not syntax, so they are neither caller nor callee.
``references``             settled callables per caller that it names
                           without calling: an argument or a returned value.
``members``                each module key's own top-level names, so a module
                           claim counts its own file and not its subpackages.
``files``                  the files whose calls were captured. Merged with
                           facts from before the oracle, a claimant outside
                           them is uncovered, never call-free. ``None`` for
                           evidence written before the field: coverage
                           unknown, so nothing is held uncovered.
``rebound``                a name a module binds by import, to where it lands.
                           A bare title grounds on the binding; the oracle
                           names the declaration.

Read from the **whole checkout**, as every oracle in this package is.
MANIFEST "Relationship layer Phase 8" measures what each field buys.
"""

from __future__ import annotations

import collections
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .corpus import dotted_key
from .dialects import PYTHON
from .edges.call_targets import MODULE
from .edges.calls import CallResolver, CallScan, scan_calls
from .edges.model import INTERNAL, Inventory, checkout_files
from .treesitter import BINDING, has_error, node_text, parse

#: Names that import a module named by a value rather than by syntax. Read as
#: text, because ``imp = imp or importlib.import_module`` then ``imp(name)``
#: is a local call the oracle declines, and it is the import.
DYNAMIC_IMPORT_NAMES = ("__import__", "import_module", "load_source", "load_module")

_SETS = ("by_name", "properties", "bindings")
_MAPS = ("internal", "external", "binds", "members", "references")


class CallEvidence:
    """The call graph, indexed the ways the CALLS verdict asks of it."""

    def __init__(self, internal=None, external=None, unsettled=None, binds=None, by_name=(),
                 properties=(), bindings=(), members=None, rebound=None, references=None,
                 files: Iterable[str] | None = ()):
        self.internal: dict[str, set[str]] = _sets(internal)
        self.external: dict[str, set[str]] = _sets(external)
        self.unsettled: dict[str, int] = dict(unsettled or {})
        self.binds: dict[str, set[str]] = _sets(binds)
        self.by_name: frozenset[str] = frozenset(by_name)
        self.properties: frozenset[str] = frozenset(properties)
        self.bindings: frozenset[str] = frozenset(bindings)
        self.members: dict[str, set[str]] = _sets(members)
        self.rebound: dict[str, str] = dict(rebound or {})
        self.references: dict[str, set[str]] = _sets(references)
        self.files: frozenset[str] | None = None if files is None else frozenset(files)
        self.callers: frozenset[str] = frozenset(self.internal) | frozenset(self.external) | frozenset(
            self.unsettled)
        self._within: dict[str, list[str]] = {}

    def __bool__(self) -> bool:
        return bool(self.callers)

    # ------------------------------------------------------------------ capture

    @classmethod
    def from_checkout(cls, root: str | Path) -> CallEvidence:
        root = Path(root)
        inventory = Inventory(checkout_files(root), root)
        return cls.from_scan(scan_calls(root, inventory), CallResolver(inventory))

    @classmethod
    def from_scan(cls, scan: CallScan, resolver: CallResolver) -> CallEvidence:
        decl = resolver.declarations
        internal, external, binds = (collections.defaultdict(set) for _ in range(3))
        for c in scan.calls:
            if c.category == INTERNAL:
                internal[c.caller].add(c.target)
            elif c.target:
                external[c.caller].add(c.target)
            for b in c.binds:
                if c.target:
                    binds[b].add(c.target)
        members = {ns: {dotted_key(n) for n in decl.symbols.get(path, ()) if "." not in n}
                   for path, ns in decl.namespaces.items() if ns}
        references = collections.defaultdict(set)
        for r in scan.references:
            references[r.caller].add(r.target)
        evidence = cls(internal, external, scan.unsettled_callers(), binds, (),
                       decl.properties, (k for k, kinds in decl.kinds.items() if kinds == {BINDING}),
                       members, _rebound(decl), references, decl.namespaces)
        # A file tree-sitter had to recover may have lost calls (a Python 2
        # backtick); nothing declared in it is evidence of absence.
        for path in decl.namespaces:
            if has_error(decl.inventory.read(path) or "", PYTHON):
                for key in {decl.key(path, n) for n in decl.symbols.get(path, ())} | {decl.namespaces[path]}:
                    evidence.unsettled[key] = evidence.unsettled.get(key, 0) + 1
        evidence.by_name = frozenset(evidence._reach(_mentions_dynamic_import(decl)))
        return evidence

    def _reach(self, seeds: set[str]) -> set[str]:
        """``seeds`` and every caller whose settled calls reach one, at any depth."""
        callers_of: dict[str, set[str]] = collections.defaultdict(set)
        for caller, targets in self.internal.items():
            for target in targets:
                for inner in self.within(target):
                    callers_of[inner].add(caller)
        found, todo = set(seeds), list(seeds)
        while todo:
            for caller in callers_of.get(todo.pop(), ()):
                if caller not in found:
                    found.add(caller)
                    todo.append(caller)
        return found

    # ------------------------------------------------------------------ reading

    def follow(self, key: str) -> str:
        """``key`` through an import binding to what it names, or ``key`` itself."""
        return self.rebound.get(key, key)

    def declared_in(self, obj: str, target: str) -> bool:
        """Whether ``target`` is a member of ``obj``; of its own file for a module.

        Unlike an import, a call into ``celery.utils`` does not call ``celery``.
        """
        if not target.startswith(obj + "."):
            return False
        head = target[len(obj) + 1:].split(".")[0]
        if f"{obj}.{head}" in self.members:
            return False  # a submodule, even where the package binds its name
        own = self.members.get(obj)
        return own is None or target[len(obj) + 1:].split(".")[0] in own

    def within(self, key: str) -> list[str]:
        """Callers at ``key`` or declared inside it."""
        if key not in self._within:
            self._within[key] = sorted(c for c in self.callers if c == key or self.declared_in(key, c))
        return self._within[key]

    def targets(self, key: str, external: bool = True) -> set[str]:
        """What the callers within ``key`` reach."""
        return {t for c in self.within(key) for t in self.internal.get(c, set())
                | (self.external.get(c, set()) if external else set())}

    # ------------------------------------------------------------ serialisation

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {name: {k: sorted(v) for k, v in sorted(getattr(self, name).items())}
                               for name in _MAPS}
        out.update({name: sorted(getattr(self, name)) for name in _SETS})
        out["unsettled"] = dict(sorted(self.unsettled.items()))
        out["rebound"] = dict(sorted(self.rebound.items()))
        if self.files is not None:
            out["files"] = sorted(self.files)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> CallEvidence:
        data = data or {}
        return cls(data.get("internal"), data.get("external"), data.get("unsettled"), data.get("binds"),
                   data.get("by_name", ()), data.get("properties", ()), data.get("bindings", ()),
                   data.get("members"), data.get("rebound"), data.get("references"),
                   data.get("files", () if not data.get("internal") else None))

    @classmethod
    def merge(cls, parts: Iterable[dict[str, Any] | None]) -> dict[str, Any]:
        """Union serialised evidence from several repositories."""
        merged, conflicts = cls(), set()
        for part in parts:
            other = cls.from_dict(part)
            for name in _MAPS:
                for k, v in getattr(other, name).items():
                    getattr(merged, name).setdefault(k, set()).update(v)
            for name in _SETS:
                setattr(merged, name, getattr(merged, name) | getattr(other, name))
            for k, n in other.unsettled.items():
                merged.unsettled[k] = merged.unsettled.get(k, 0) + n
            # Coverage unknown in any part carrying calls leaves it unknown.
            if merged.files is not None:
                merged.files = None if other.files is None else merged.files | other.files
            # Two repositories binding one key differently: neither is followed.
            conflicts |= {k for k, v in other.rebound.items() if merged.rebound.get(k, v) != v}
            merged.rebound.update(other.rebound)
        for k in conflicts:
            del merged.rebound[k]
        return merged.to_dict()


def _sets(mapping) -> dict[str, set[str]]:
    return {k: set(v) for k, v in (mapping or {}).items()}


def _rebound(decl) -> dict[str, str]:
    """Each ``module.name`` a module binds by import, to the folded key it lands on.

    A module itself is a landing too (``from ..utils import timeutils``),
    folded from the real name; a binding that lands nowhere is left out.
    """
    found: dict[str, str] = {}
    for path, namespace in decl.namespaces.items():
        bound = decl.bindings(path)
        if bound is None or not namespace:
            continue
        declared = {n.lower() for n in decl.symbols.get(path, ())}
        for name, entries in bound.bound.items():
            if name.lower() in declared or len(set(entries)) != 1:
                continue
            entry = entries[0]
            landed = decl.through(entry, [], path)
            if landed.category == MODULE:
                key = dotted_key(".".join(p for p in (entry[1], entry[2]) if p))
            else:
                key = landed.key
            if key:
                found[dotted_key(f"{namespace}.{name}")] = key
    return found


def _mentions_dynamic_import(decl) -> set[str]:
    """Declarations whose text names a dynamic import, called or held as a value.

    A mention in a string or a comment over-counts, which only moves a claim
    from contradicted to unchecked.
    """
    found = set()
    for path, namespace in decl.namespaces.items():
        source = decl.inventory.read(path) or ""
        if not any(name in source for name in DYNAMIC_IMPORT_NAMES):
            continue
        stack = [(parse(source, PYTHON).root_node, ())]
        while stack:
            node, chain = stack.pop()
            if node.type in ("class_definition", "function_definition"):
                name = node.child_by_field_name("name")
                chain = (*chain, node_text(name)) if name is not None else chain
                if node.type == "function_definition" and any(
                        n in node_text(node) for n in DYNAMIC_IMPORT_NAMES):
                    found.add(dotted_key(".".join(p for p in (namespace, *chain) if p)))
                    continue
            stack.extend((c, chain) for c in node.named_children)
    return found
