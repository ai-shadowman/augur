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
from typing import Any, NamedTuple

from . import imports, symbols
from .corpus import canonical_key, dotted_key
from .declarations import module_key, namespace_for
from .dialects import dialect_for
from .edges import EXTERNAL, IMPORTS, INHERITS, INTERNAL, UNRESOLVED, scan_checkout
from .edges.model import Edge, Inventory, checkout_files
from .edges.ruby import mixin_names

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

#: Whether a bare name may fall back to an exact top-level key; see
#: :meth:`EdgeEvidence.global_bare`.
GLOBAL_BARE = {"python": True, "typescript": False, "java": True, "ruby": True}

#: Readers of the calls that change a class's ancestors without inheritance
#: syntax, per dialect. Only Ruby has them; every other recorded dialect
#: states all of its supertypes in syntax the oracle reads.
MIXIN_READERS = {"ruby": mixin_names}


def _names_file(title: str) -> bool:
    """Whether a title is spelled as a path (``PKG/SUB.PY``) rather than dotted."""
    return "/" in title or canonical_key(title) != dotted_key(title)


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


class NodeIndex(NamedTuple):
    """What :func:`node_files` reads, one field per :class:`EdgeEvidence` argument."""

    files: dict[str, set[str]]
    namespaces: dict[str, str]
    opaque: set[str]
    #: Keys a dotted title cannot settle: see :func:`_ambiguous`.
    ambiguous: set[str]
    #: Keys a file binds by import, to those files, where some other file
    #: declares the key. The declarer's scope is the one its own names use.
    binders: dict[str, set[str]]
    #: Per file, the lexical crefs of each class written in compact form
    #: (``class A::B::C``), innermost first. See :meth:`EdgeEvidence.in_scope`.
    nesting: dict[str, dict[str, list[str]]]
    #: A module's own files, for each key whose files hold more than the
    #: module: ``from . import sub`` in ``pkg/__init__.py`` binds ``pkg.sub``
    #: too. A title spelled as a path means the module alone.
    modules: dict[str, set[str]]
    #: Per file, the folded last segments of the modules it mixes in (Ruby's
    #: ``include``), which the oracle does not read as edges.
    mixins: dict[str, list[str]]


def _compact_nesting(classes: Iterable[str]) -> dict[str, list[str]]:
    """Each compact-form class's crefs, as ``RubyResolver._constant`` derives them.

    The walker keeps ``::`` inside a segment for ``class A::B::C``, so the
    segments between dots are the lexical scopes and ``A::B`` is one of them:
    ``Module.nesting`` there is the class alone, not ``A::B`` and ``A``.
    Folding to a key loses that, which is why it is recorded here.
    """
    nesting = {}
    for owner in classes:
        if "::" in owner:
            levels = owner.split(".")[:-1]
            nesting[dotted_key(owner)] = [
                dotted_key("::".join(levels[:i])) for i in range(len(levels), 0, -1)]
    return nesting


def _ambiguous(modules: dict[str, set[str]], bound_elsewhere: dict[str, set[str]],
               chase: list[tuple[str, str]], declared: dict[str, set[str]]) -> set[str]:
    """Keys that fold a module together with a name bound to something else.

    ``from .settings import Config`` in ``pkg/__init__.py`` binds
    ``pkg.Config``, and ``pkg/config.py`` is the module ``pkg.config``: one
    key, two things. GraphRAG uppercases titles, so a dotted title cannot
    say which it meant. ``from . import config`` is not a collision, since
    the bound name is the module. A ``class Config`` declared in the
    ``__init__`` is one, the same way. A name bound through an ambiguous key
    inherits the ambiguity, because the chase cannot tell which meaning it
    followed.
    """
    found = {k for k, binders in bound_elsewhere.items() if modules.get(k, set()) - binders}
    found |= {k for k, files in declared.items() if modules.get(k) and files - modules[k]}
    changed = True
    while changed:
        changed = False
        for bound, target in chase:
            if target in found and bound not in found:
                found.add(bound)
                changed = True
    return found


def node_files(root: Path, inventory: Inventory, edges: Iterable[Edge]) -> NodeIndex:
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
    function. A package is keyed by its ``__init__`` as well, since that is
    what ``import pkg`` names.
    """
    edges = list(edges)
    written_to: dict[str, dict[str, str]] = collections.defaultdict(dict)
    for e in edges:
        if e.kind == IMPORTS and e.target_class == INTERNAL:
            written_to[e.path][e.written] = e.target

    found: dict[str, set[str]] = collections.defaultdict(set)
    namespaces: dict[str, str] = {}
    declared: dict[str, set[str]] = collections.defaultdict(set)
    opaque: set[str] = set()
    chase: list[tuple[str, str]] = []
    modules: dict[str, set[str]] = collections.defaultdict(set)
    binders: dict[str, set[str]] = collections.defaultdict(set)
    bound_elsewhere: dict[str, set[str]] = collections.defaultdict(set)
    nesting: dict[str, dict[str, list[str]]] = {}
    mixins: dict[str, list[str]] = {}
    for path in sorted(inventory.paths):
        dialect = dialect_for(path)
        if dialect is None:
            continue
        source = (root / path).read_text(encoding="utf-8", errors="replace")
        found[canonical_key(path)].add(path)
        module = dotted_key(module_key(path))
        modules[module].add(path)
        if canonical_key(path).endswith(".__init__"):
            found[module].add(path)
        prefix = namespaces[path] = namespace_for(path, source, dialect)
        read = symbols.symbols_in(source, dialect)
        if _rule(SCOPES, dialect, "scope") == "lexical" and (compact := _compact_nesting(read.classes)):
            nesting[path] = compact
        if (reader := MIXIN_READERS.get(dialect.name)) and (mixed := reader(source)):
            mixins[path] = sorted(dotted_key(m) for m in mixed)
        for name in (*read.classes, *read.functions, *read.methods):
            key = dotted_key(f"{prefix}.{name}" if prefix else name)
            found[key].add(path)
            declared[key].add(path)
        for name in read.bindings:
            key = dotted_key(f"{prefix}.{name}" if prefix else name)
            found[key].add(path)
            opaque.add(key)
        if _binds(dialect):
            for bound, target in _import_bindings(source, dialect, prefix, written_to[path]):
                found[bound].add(path)
                binders[bound].add(path)
                if target != bound:
                    bound_elsewhere[bound].add(path)
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
    ambiguous = _ambiguous(modules, bound_elsewhere, chase, declared)
    # A file that declares the key as well as binding it is no mere binder:
    # celery's ``apps/beat.py`` declares ``class Beat`` and binds ``beat``.
    binders = {k: fs - declared.get(k, set()) for k, fs in binders.items() if k not in ambiguous}
    return NodeIndex(
        found, namespaces, opaque - declared.keys() - {b for b, _ in chase}, ambiguous,
        {k: fs for k, fs in binders.items() if fs and found[k] - fs}, nesting,
        {k: fs for k, fs in modules.items() if found.get(k, fs) != fs}, mixins,
    )


class EdgeEvidence:
    """The oracle's edges, indexed the ways the verdict asks of them."""

    def __init__(self, edges: Iterable[EdgeRow], files: dict[str, Iterable[str]],
                 namespaces: dict[str, str] | None = None, opaque: Iterable[str] = (),
                 ambiguous: Iterable[str] = (), binders: dict[str, Iterable[str]] | None = None,
                 nesting: dict[str, dict[str, list[str]]] | None = None,
                 modules: dict[str, Iterable[str]] | None = None,
                 mixins: dict[str, Iterable[str]] | None = None):
        self.namespaces: dict[str, str] = dict(namespaces or {})
        self.opaque: frozenset[str] = frozenset(opaque)
        self.ambiguous: frozenset[str] = frozenset(ambiguous)
        self.binders: dict[str, set[str]] = {k: set(v) for k, v in (binders or {}).items()}
        self.nesting: dict[str, dict[str, list[str]]] = {
            p: {k: list(c) for k, c in n.items()} for p, n in (nesting or {}).items()}
        self.modules: dict[str, set[str]] = {k: set(v) for k, v in (modules or {}).items()}
        self.mixins: dict[str, set[str]] = {p: set(v) for p, v in (mixins or {}).items()}
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
        self._tails = collections.Counter(k.rpartition(".")[2] for k in self.file_of)
        for kind, source, target, target_class, path in self.edges:
            if target_class == INTERNAL and kind == INHERITS and target in self.ambiguous:
                # The oracle reached a base through a binding whose key folds
                # onto a module, so which file it means is unknown. An IMPORTS
                # target is a module the oracle found as a file, and
                # ``_target_files`` reads the module's files for it.
                self.unresolved[path] += 1
            elif target_class == INTERNAL:
                self.pairs[(source, target)].add(kind)
                self.targets[path][target].add(kind)
                if kind == INHERITS:
                    self.parents[source].add(target)
                for tf in self._target_files(target):
                    # A file's edge to itself (``from . import sub`` in an
                    # ``__init__`` reads as pkg -> pkg) would otherwise lift
                    # every claim between two of its own names.
                    if tf != path:
                        self.internal[(path, tf)].add(kind)
            elif target_class == EXTERNAL:
                self.external[path].add((target, kind))
            elif target_class == UNRESOLVED:
                self.unresolved[path] += 1

    def _target_files(self, target: str) -> set[str]:
        """The files an internal target names, a Java on-demand import included.

        ``import com.x.util.*`` is recorded as ``com.x.util.*``, which no file
        declares: it names every type in the package, or for ``Outer.*`` the
        members of ``Outer``.
        """
        if target in self.modules:
            return self.modules[target]
        if target in self.file_of or not target.endswith(".*"):
            return self.file_of.get(target, set())
        # The package first: folding makes ``com.x.util`` the class
        # ``com.x.Util`` too, and a package is what the import usually names.
        base = target[:-2]
        return {f for f, ns in self.namespaces.items() if ns == base} or set(
            self.file_of.get(base, ()))

    @classmethod
    def from_checkout(cls, root: str | Path) -> EdgeEvidence:
        root = Path(root)
        inventory = Inventory(checkout_files(root), root)
        scan = scan_checkout(root, inventory)
        rows = [(e.kind, e.source, e.target, e.target_class, e.path) for e in scan.edges]
        return cls(rows, **node_files(root, inventory, scan.edges)._asdict())

    def __bool__(self) -> bool:
        return bool(self.edges)

    def to_dict(self) -> dict[str, Any]:
        return {
            "edges": [list(e) for e in self.edges],
            "node_files": {k: sorted(v) for k, v in sorted(self.file_of.items())},
            "namespaces": dict(sorted(self.namespaces.items())),
            "opaque": sorted(self.opaque),
            "ambiguous": sorted(self.ambiguous),
            "binders": {k: sorted(v) for k, v in sorted(self.binders.items())},
            "nesting": {p: dict(sorted(n.items())) for p, n in sorted(self.nesting.items())},
            "modules": {k: sorted(v) for k, v in sorted(self.modules.items())},
            "mixins": {p: sorted(v) for p, v in sorted(self.mixins.items())},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> EdgeEvidence:
        """Facts written before a field existed read it as empty, as the verdict did then."""
        data = data or {}
        return cls((tuple(e) for e in data.get("edges", ())), data.get("node_files", {}),
                   data.get("namespaces", {}), data.get("opaque", ()), data.get("ambiguous", ()),
                   data.get("binders", {}), data.get("nesting", {}), data.get("modules", {}),
                   data.get("mixins", {}))

    @classmethod
    def merge(cls, parts: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """Union serialised evidence from several repositories, as ``merge_facts`` does."""
        edges: set[tuple] = set()
        files: dict[str, set[str]] = collections.defaultdict(set)
        namespaces: dict[str, str] = {}
        opaque: set[str] = set()
        ambiguous: set[str] = set()
        binders: dict[str, set[str]] = collections.defaultdict(set)
        nesting: dict[str, dict[str, list[str]]] = {}
        modules: dict[str, set[str]] = collections.defaultdict(set)
        mixins: dict[str, set[str]] = collections.defaultdict(set)
        for part in parts:
            part = part or {}
            edges.update(tuple(e) for e in part.get("edges", ()))
            for key, paths in part.get("node_files", {}).items():
                files[key].update(paths)
            namespaces.update(part.get("namespaces", {}))
            opaque.update(part.get("opaque", ()))
            ambiguous.update(part.get("ambiguous", ()))
            for key, paths in part.get("binders", {}).items():
                binders[key].update(paths)
            nesting.update(part.get("nesting", {}))
            for key, paths in part.get("modules", {}).items():
                modules[key].update(paths)
            for path, names in part.get("mixins", {}).items():
                mixins[path].update(names)
        return cls(edges, files, namespaces, opaque, ambiguous, binders, nesting, modules,
                   mixins).to_dict()

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
        """The node key a title names in the checkout, or ``None``.

        An ambiguous key grounds only from a title spelled as a path, which
        names the module; a dotted title may mean the bound name as well.
        """
        for key in (dotted_key(title), canonical_key(title)):
            if key in self.file_of:
                return None if key in self.ambiguous and not _names_file(title) else key
        return None

    def title_files(self, title: str, key: str | None) -> set[str]:
        """The files ``title`` means at ``key``: the module's alone for a path."""
        if key in self.modules and _names_file(title):
            return self.modules[key]
        return self.files(key)

    def mixes_in(self, files: Iterable[str], title: str) -> bool:
        """Whether any of ``files`` includes a module named like ``title``'s last segment."""
        name = dotted_key(title).rpartition(".")[2]
        return any(name in self.mixins.get(f, ()) for f in files)

    def _crefs(self, claimant: str, path: str) -> list[str]:
        """The keys a bare constant in ``claimant`` is looked up under, innermost first.

        Ruby's lexical scopes. A superclass is evaluated before its class
        opens, so a class's own crefs leave it out, as the oracle's do; a
        method's start at its class. A class written ``class A::B::C`` has
        only the scopes its file records for it, since folding made
        ``a.b.c`` look nested in ``a.b`` and ``a``.
        """
        recorded = self.nesting.get(path, {})
        if claimant in recorded:
            return recorded[claimant]
        # Not a prefix match: ``module A::B`` records ``a.b``, and a class
        # nested ``module A; module B; class C`` must not inherit its scopes.
        parts = claimant.split(".")
        return [".".join(parts[:j]) for j in range(len(parts) - 1, 0, -1)]

    def in_scope(self, name: str, claimant: str, files: Iterable[str],
                 imported: bool = False) -> str | None:
        """The node key a bare ``name`` means where ``claimant`` uses it, or ``None``.

        Tried innermost first per :data:`SCOPES`. ``None`` leaves the caller to
        the outermost scope, an exact global key, which is a top-level module
        or constant in every dialect here. What it never does is take the one
        declaration anywhere that happens to share the name: jekyll's
        ``StaticFileDrop`` inherits ``Jekyll::Drops::Drop``, not the
        top-level ``Drop`` a benchmark script declares. Nor an opaque name: a
        bare ``PWD`` where ``pwd = try_import("pwd")`` ran means the library,
        and the oracle cannot see that.

        ``imported`` is set for an IMPORTS claim, whose object cannot be
        something the claimant's own file declares: celery's
        ``worker/__init__.py`` declares ``class Beat`` and imports the module
        ``beat``, and folding makes the two one key.
        """
        files = set(files)

        def usable(key: str) -> bool:
            if key not in self.file_of or key in self.opaque or key in self.ambiguous:
                return False
            return not (imported and self.file_of[key] <= files)

        # An INHERITS claim's bare base is first what the oracle resolved the
        # claimant's own base list to, if that names one such target: the
        # oracle applies each language's lookup (an enclosing class body, a
        # Java outer type's inherited members, Ruby's crefs), which the scope
        # walk below only approximates.
        if not imported:
            own = {t for t in self.parents.get(claimant, ())
                   if (t == name or t.endswith("." + name)) and usable(t)}
            if len(own) == 1:
                return next(iter(own))
        # A re-exported claimant's names are its declaring file's: the
        # ``__init__`` that binds it by import has a scope of its own.
        declaring = files - self.binders.get(claimant, set())
        for path in sorted(declaring or files):
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
                    # Java's walk stops above the package, which comes after
                    # the imports; Ruby's runs to the outermost module.
                    if rule == "lexical":
                        crefs = self._crefs(claimant, path)
                    else:
                        parts = claimant.split(".")
                        stop = len(namespace.split(".")) if namespace else 0
                        crefs = [".".join(parts[:i]) for i in range(len(parts), stop, -1)]
                    candidates += [f"{c}.{name}" for c in crefs]
                if rule == "package":
                    # A single-type import shadows a same-package type (JLS
                    # 6.4.1); an on-demand import ends ``.*`` and matches neither.
                    candidates += sorted(
                        t for k, _, t, _, p in self.edges
                        if p == path and k == IMPORTS and t and t.endswith("." + name)
                    )
                    if namespace:
                        candidates.append(f"{namespace}.{name}")
            for key in candidates:
                if usable(key):
                    return key
            own = f"{namespace}.{name}" if namespace else name
            if own in self.opaque:
                return None  # the name holds a value nothing here can see
            # Last, what the oracle itself resolved the name to in this file:
            # ``class A(app.models.Config)`` or a TypeScript import, which no
            # scope rule above can follow. Only if it names one target.
            resolved = {
                t for _, _, t, c, p in self.edges
                if p == path and c == INTERNAL and t and t.endswith("." + name)
            }
            resolved = {t for t in resolved if usable(t)}
            if len(resolved) == 1:
                return next(iter(resolved))
        return None

    def named_elsewhere(self, key: str) -> bool:
        """Whether another node key ends in ``key``'s last segment."""
        return self._tails[key.rpartition(".")[2]] > 1

    def global_bare(self, files: Iterable[str]) -> bool:
        """Whether a bare name used in ``files`` may mean a top-level key.

        Python's top-level module, Ruby's top-level constant and Java's default
        package are each language's outermost scope. TypeScript has none: a
        bare name never means a file at the root, so ``Base`` must not fold
        onto ``base.ts``.
        """
        dialects = {d for f in files if (d := dialect_for(f)) is not None}
        return all(_rule(GLOBAL_BARE, d, "global-scope") for d in dialects)

    def files(self, key: str | None) -> set[str]:
        return self.file_of.get(key, set()) if key else set()

    def external_kinds(self, files: set[str], key: str) -> set[str]:
        """Kinds of the external edges from ``files`` whose target is, contains or is inside ``key``."""
        return {
            kind for f in files for t, kind in self.external.get(f, ())
            if t == key or key.startswith(t + ".") or t.startswith(key + ".")
        }
