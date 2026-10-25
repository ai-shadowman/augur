"""The parser engine: one traversal, one table per language.

``ast`` is the definition of Python 3 syntax and stays the primary reader for
Python. It has one gap, and on this corpus the gap is a third of the files:
celery 2.5.5 is Python 2, so ``except E, e`` and ``print x`` make 50 of 237
files a ``SyntaxError``. Until Phase 6 those fell to a regex line scan that
tracked scope by indentation, could not tell source from a docstring, and had
to be measured and gated before any of its output was allowed into a header.

tree-sitter closes that gap with a parser instead of a gate, and it closes a
second one the line scan could never have reached: a language that is not
Python at all. Everything below is language-neutral. What one language calls a
class, a function, a statement list and a binding is a :class:`Dialect`, and
the dialects live in :mod:`provenance.dialects` so that adding a language is a
table rather than a second traversal.

The traversal mirrors :func:`declarations._collect` node for node, because the
whole value of this module is that it answers the same question the oracle
does. Only a class body is descended into with a widened prefix, so a name is a
method exactly when something encloses it and a closure's locals never reach a
header. A statement that opens no namespace is descended into with the prefix
unchanged, which is how a class behind ``if sys.version_info`` or inside
``try:`` still counts.

That the two readers answer the same question is a measurement, not a claim.
``tests/test_provenance_treesitter_agreement.py`` recomputes it from source on
every run: on the 187 celery files ``ast`` accepts, both return 371 classes,
226 functions, 1,661 methods, 824 bindings and 621 imports, every one of them,
with no disagreeing file.

The grammar itself is loaded on first use rather than at import, so this module
can be imported in an environment without ``tree-sitter-language-pack``
installed. Parsing there raises, naming the package, rather than silently
degrading to something less accurate.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from functools import cached_property
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from tree_sitter import Node, Parser, Tree

#: The kinds a header distinguishes. Defined here rather than in
#: :mod:`provenance.symbols` because this is the module that assigns them and
#: the other way round would be a cycle; ``symbols`` re-exports them.
CLASS, FUNCTION, METHOD, BINDING = "class", "function", "method", "binding"


# --------------------------------------------------------------------------
# What an edge capture reads, before anything resolves it
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ImportCapture:
    """One import as the source writes it, unresolved.

    Kept apart from resolution so the two Python readers can be compared on
    what they read rather than on what a shared resolver made of it: a
    resolution bug applied to both sides would otherwise agree with itself.
    """

    #: The module, in full and with any leading dots: ``celery.utils``,
    #: ``..datastructures``, ``.`` for ``from . import x``.
    written: str
    #: ``import a.b as x`` binds ``x``.
    alias: str | None = None
    #: ``from m import a, b as c`` as ``(("a", None), ("b", "c"))``, in source
    #: order; ``("*", None)`` for a star import. Empty for a plain import.
    names: tuple[tuple[str, str | None], ...] = ()
    #: Java's ``import static``: :attr:`written` names a type's member, or with
    #: a star every static member of the type, rather than a type or package.
    static: bool = False
    #: Inside a function or class body. Still an edge, since the file depends
    #: on the module either way, but the name it binds is local to that body
    #: and is not a module-level name a class statement can refer to.
    scoped: bool = False


@dataclass(frozen=True)
class BaseCapture:
    """One base a class names, unresolved."""

    #: The walker's qualified class name, real casing, as :func:`symbols_in_tree`
    #: reports it.
    owner: str
    #: The base's source text with whitespace removed, so ``a . B`` and ``a.B``
    #: are one spelling and an expression base is still recorded verbatim.
    written: str
    #: ``extends`` or ``implements`` where the grammar distinguishes them.
    detail: str | None = None


def squeeze(text: str) -> str:
    """Source text with every run of whitespace removed."""
    return "".join(text.split())


# --------------------------------------------------------------------------
# The per-language table
# --------------------------------------------------------------------------

@dataclass(frozen=True, eq=False)
class Dialect:
    """What one language's grammar calls the four things a header needs.

    Identity-compared on purpose: these are module-level singletons, and the
    callable fields would make structural equality meaningless anyway.
    """

    #: The key ``tree_sitter_language_pack.get_parser`` is asked for.
    name: str
    #: File suffixes this dialect claims, lowercased and dot-prefixed.
    suffixes: tuple[str, ...]
    #: Node types that declare a class, and whose ``body`` widens the prefix.
    classes: frozenset[str]
    #: Node types that declare a function or method, and whose body is skipped.
    functions: frozenset[str]
    #: Node types holding a statement list.
    blocks: frozenset[str]
    #: Node types that may bind a name, read by :attr:`bound_names`.
    bindings: frozenset[str]
    #: Node type to the field holding the definition it decorates or wraps.
    unwrap: Mapping[str, str]
    #: ``(kind, name)`` for each name bound by one node of a type in
    #: :attr:`bindings`. The kind is nearly always ``BINDING``; a language
    #: where a callable is declared by binding one, as TypeScript's
    #: ``const f = () => {}`` is, says ``FUNCTION`` and the walk applies the
    #: same enclosing-scope rule it applies to a ``def``.
    bound_names: Callable[[Node], Iterator[tuple[str, str]]]
    #: Top-level module names imported by one node, called on every node.
    imported_names: Callable[[Node], Iterator[str]]
    #: The dotted name the file is addressed by, read from the source rather
    #: than computed from the path. ``None`` where the path is the authority,
    #: as it is in Python; Java states the package in the file and its
    #: directory layout says something else entirely.
    package_of: Callable[[str, Node], str | None] | None = None
    #: Every import one node makes, as an edge needs it: the full module as
    #: written, not the dependency :attr:`imported_names` reduces it to.
    #: ``None`` for a dialect whose edge capture is not written yet.
    import_edges: Callable[[Node], Iterator[ImportCapture]] | None = None
    #: ``(written, detail)`` for each base one class node names. ``None`` for
    #: a dialect whose edge capture is not written yet.
    base_names: Callable[[Node], Iterator[tuple[str, str | None]]] | None = None

    @cached_property
    def namespaces(self) -> frozenset[str]:
        """Nodes that open a scope. Never crossed when hunting for blocks."""
        return self.classes | self.functions


def node_text(node: Node) -> str:
    """One node's source, decoded. Public because every dialect needs it."""
    return node.text.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

_PARSERS: dict[str, Parser] = {}


def parser_for(dialect: Dialect) -> Parser:
    """The grammar for ``dialect``, loaded once and cached.

    Loaded here rather than at import so that this module can be imported
    without the dependency present. A caller that actually parses gets a
    failure naming the package instead of a quiet fallback to something less
    accurate.
    """
    if dialect.name not in _PARSERS:
        try:
            from tree_sitter_language_pack import get_parser
        except ImportError as exc:  # pragma: no cover - depends on the env
            raise RuntimeError(
                f"parsing {dialect.name} needs tree-sitter-language-pack, which is not "
                f"installed; it is declared in pyproject.toml and the environment is "
                f"missing it"
            ) from exc
        _PARSERS[dialect.name] = get_parser(dialect.name)
    return _PARSERS[dialect.name]


def parse(source: str, dialect: Dialect) -> Tree:
    """Parse ``source``. Error-tolerant: this does not raise on bad syntax."""
    return parser_for(dialect).parse(source.encode("utf-8", "replace"))


def has_error(source: str, dialect: Dialect) -> bool:
    """Whether the grammar had to recover anywhere in the file.

    Not a Python 2 detector. The grammar accepts Python 2 outright, so all 50
    celery files ``ast`` refuses come back clean here, and what remains to set
    this flag is source that is malformed in any version of the language.
    Reported rather than acted on: a file that needed recovery still yields
    whatever the grammar did understand, and suppressing those symbols would
    cost more than the uncertainty is worth. A quality signal for a caller
    that wants one, not a gate.
    """
    return parse(source, dialect).root_node.has_error


# --------------------------------------------------------------------------
# Walking
# --------------------------------------------------------------------------

def _unwrap(node: Node, dialect: Dialect) -> Node:
    if (field := dialect.unwrap.get(node.type)) is None:
        return node
    return node.child_by_field_name(field) or node


def _blocks(node: Node, dialect: Dialect) -> Iterator[Node]:
    """Immediate statement lists under ``node``, in source order.

    The counterpart of ``declarations._nested_bodies``, which yields one level
    and lets the caller recurse. A block is yielded without being entered, so
    the traversal stays level by level and a nested ``if`` inside a block is
    reached by the next :func:`_walk`, not by this. Clause wrappers
    (``else_clause``, ``except_clause`` and the rest) hold their block one
    level down, which is why this descends at all.

    Iterative because it crosses expressions on the way, and a deeply nested
    literal would otherwise put the recursion limit between a file and its
    symbols.
    """
    stack = list(reversed(node.named_children))
    while stack:
        child = stack.pop()
        if child.type in dialect.blocks:
            yield child
        elif child.type not in dialect.namespaces:
            stack.extend(reversed(child.named_children))


def _walk(node: Node, prefix: tuple[str, ...], dialect: Dialect, out: list[tuple[str, str]]) -> None:
    """Mirror of ``declarations._collect``, recording the kind as it goes."""
    for raw in node.named_children:
        child = _unwrap(raw, dialect)
        if child.type in dialect.classes:
            name = child.child_by_field_name("name")
            if name is None:
                continue  # a recovered node with no name is not a declaration
            qualified = (*prefix, node_text(name))
            out.append((CLASS, ".".join(qualified)))
            # Only a class body widens the prefix. A function's locals are not
            # addressable from outside it, so its body is never entered.
            if body := child.child_by_field_name("body"):
                _walk(body, qualified, dialect, out)
        elif child.type in dialect.functions:
            if (name := child.child_by_field_name("name")) is not None:
                out.append((METHOD if prefix else FUNCTION, ".".join((*prefix, node_text(name)))))
        elif child.type in dialect.bindings:
            for kind, bound in dialect.bound_names(child):
                # A binding whose value is a function is a function, and the
                # same rule decides whether it is a method: enclosed by a
                # class, it is one. Settled here rather than in the dialect
                # because only the walk knows the prefix.
                if kind == FUNCTION and prefix:
                    kind = METHOD
                out.append((kind, ".".join((*prefix, bound))))
        elif child.type in dialect.blocks:
            # A block directly under a walked node, which :func:`_blocks`
            # cannot reach because it inspects grandchildren. Java puts
            # ``enum_body_declarations`` here, so without this an enum's
            # methods are unreachable. Never fires for Python, where a block
            # is only ever a body field.
            _walk(child, prefix, dialect, out)
        else:
            for block in _blocks(child, dialect):
                _walk(block, prefix, dialect, out)


def symbols_in_tree(source: str, dialect: Dialect) -> list[tuple[str, str]]:
    """Every ``(kind, qualified name)`` the source declares, in source order."""
    found: list[tuple[str, str]] = []
    _walk(parse(source, dialect).root_node, (), dialect, found)
    return found


def declarations_in_tree(source: str, dialect: Dialect) -> set[str]:
    """The same names folded to one lowercased set, as the oracle returns them."""
    return {name.lower() for _kind, name in symbols_in_tree(source, dialect)}


def imports_in_tree(source: str, dialect: Dialect) -> set[str]:
    """Top-level module names imported anywhere in the source.

    Every node is visited, not only the top level, because
    ``imports.imports_in_source`` uses ``ast.walk`` and finds the import inside
    a ``try:`` that celery uses for optional dependencies. Declarations are
    scoped and imports are not, which is a real difference between the two
    questions rather than an inconsistency.
    """
    names: set[str] = set()
    stack = [parse(source, dialect).root_node]
    while stack:
        node = stack.pop()
        names.update(dialect.imported_names(node))
        stack.extend(node.named_children)
    return names


# --------------------------------------------------------------------------
# Edge captures
# --------------------------------------------------------------------------

def _classes(node: Node, prefix: tuple[str, ...], dialect: Dialect) -> Iterator[tuple[str, Node]]:
    """``(qualified name, class node)`` for every class :func:`_walk` records.

    The same branches in the same order, keeping only the class branch's
    output. An edge's owner therefore always names a class the symbol
    inventory also holds, so a class nested in a function, which ``_walk``
    never enters, has no edge for the same reason it has no node.
    """
    for raw in node.named_children:
        child = _unwrap(raw, dialect)
        if child.type in dialect.classes:
            name = child.child_by_field_name("name")
            if name is None:
                continue
            qualified = (*prefix, node_text(name))
            yield ".".join(qualified), child
            if body := child.child_by_field_name("body"):
                yield from _classes(body, qualified, dialect)
        elif child.type in dialect.functions or child.type in dialect.bindings:
            continue
        elif child.type in dialect.blocks:
            yield from _classes(child, prefix, dialect)
        else:
            for block in _blocks(child, dialect):
                yield from _classes(block, prefix, dialect)


def _require(dialect: Dialect, field: str) -> Callable:
    capture = getattr(dialect, field)
    if capture is None:
        raise ValueError(
            f"no edge capture written for dialect {dialect.name!r} ({field}); "
            f"tree-sitter reading a language is not the same as extracting its edges"
        )
    return capture


def base_captures_in_tree(source: str, dialect: Dialect) -> set[BaseCapture]:
    """Every base every recorded class names."""
    base_names = _require(dialect, "base_names")
    return {
        BaseCapture(owner, written, detail)
        for owner, node in _classes(parse(source, dialect).root_node, (), dialect)
        for written, detail in base_names(node)
    }


def import_captures_in_tree(source: str, dialect: Dialect) -> set[ImportCapture]:
    """Every import anywhere in the source, as written.

    Every node is visited, for the reason :func:`imports_in_tree` gives.
    """
    import_edges = _require(dialect, "import_edges")
    found: set[ImportCapture] = set()
    stack = [(parse(source, dialect).root_node, False)]
    while stack:
        node, scoped = stack.pop()
        found.update(replace(c, scoped=scoped) for c in import_edges(node))
        inside = scoped or node.type in dialect.functions or node.type in dialect.classes
        stack.extend((child, inside) for child in node.named_children)
    return found


def class_nodes_in_tree(source: str, dialect: Dialect) -> list[tuple[str, Node]]:
    """``(qualified name, node)`` for every class :func:`symbols_in_tree` records.

    For a caller that needs more of a declaration than its name, as Java's
    edge resolver needs a member type's access modifiers.
    """
    return list(_classes(parse(source, dialect).root_node, (), dialect))
