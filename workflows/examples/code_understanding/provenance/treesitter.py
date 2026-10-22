"""A real parser for the files ``ast`` refuses.

``ast`` is the definition of Python 3 syntax and stays the primary reader here.
It has one gap, and on this corpus the gap is a third of the files: celery
2.5.5 is Python 2, so ``except E, e`` and ``print x`` make 50 of 237 files a
``SyntaxError``. Until now those fell to a regex line scan that tracked scope
by indentation, could not tell source from a docstring, and had to be measured
and gated before any of its output was allowed into a header.

tree-sitter closes the gap with a parser instead of a gate, and it closes it
more completely than error tolerance alone would. The grammar still carries the
Python 2 productions: ``print x`` is a ``print_statement`` node and
``except E, e`` is an ``except_clause`` with two values, so none of the 50
files parse as damaged. They parse as the language they are written in. On the
187 files ``ast`` does accept the two readers agree exactly:
371 classes, 226 functions, 1,661 methods, 824 bindings and 621 imports, every
one of them, precision and recall 1.000 with no disagreeing file. That
agreement is recomputed from source by
``tests/test_provenance_treesitter_agreement.py`` rather than quoted from a
commit message, and it is what retired :data:`symbols.SCAN_PRECISION` and the
precision gate built on it.

The traversal mirrors :func:`declarations._collect` node for node, because the
whole value of this module is that it answers the same question the oracle
does. Only a class body is descended into with a widened prefix, so a name is a
method exactly when something encloses it and a closure's locals never reach a
header. A statement that opens no namespace is descended into with the prefix
unchanged, which is how a class behind ``if sys.version_info`` or inside
``try:`` still counts.

:class:`Dialect` is the per-language seam. It carries the node types that
declare a class, declare a function, hold a statement list and bind a name, so
adding a language is a table rather than a second traversal. Python is the only
entry today; Java, TypeScript and Ruby land on top of it.

The grammar itself is loaded on first use rather than at import, so this module
can be imported in an environment without ``tree-sitter-language-pack``
installed. Parsing there raises, naming the package, rather than silently
degrading to something less accurate.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from tree_sitter import Node, Parser, Tree

#: The kinds a header distinguishes. Defined here rather than in
#: :mod:`provenance.symbols` because this is the module that assigns them and
#: the other way round would be a cycle; ``symbols`` re-exports them.
CLASS, FUNCTION, METHOD, BINDING = "class", "function", "method", "binding"


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
    #: Names bound by one node of a type in :attr:`bindings`.
    bound_names: Callable[[Node], Iterator[str]]
    #: Top-level module names imported by one node, called on every node.
    imported_names: Callable[[Node], Iterator[str]]

    @cached_property
    def namespaces(self) -> frozenset[str]:
        """Nodes that open a scope. Never crossed when hunting for blocks."""
        return self.classes | self.functions


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------

#: The destructuring targets ``declarations._assigned_names`` reads plain names
#: out of. It ignores attribute and subscript targets, because ``self.conf = x``
#: binds no module-level name, and this has to ignore the same ones.
_PYTHON_PATTERNS = frozenset({"pattern_list", "list_pattern", "tuple_pattern"})


def _python_bound_names(statement: Node) -> Iterator[str]:
    """Mirror of ``declarations._assigned_names`` over one statement.

    ``x: int = 0`` and ``x: int`` are both ``assignment`` with a ``type``
    child, matching ``ast.AnnAssign``, which the oracle counts. ``x += 1`` is
    ``augmented_assignment``, a different node type, so it is excluded here
    exactly as ``ast.AugAssign`` is excluded there.

    Grammar versions disagree about whether a statement-position assignment is
    wrapped in ``expression_statement`` or exposed directly: the standalone
    ``tree-sitter-python`` 0.25.0 wheel wraps it, the grammar bundled with
    ``tree-sitter-language-pack`` 1.22.1 inlines the wrapper away. Both shapes
    are accepted rather than pinning a grammar build, because the difference
    is invisible everywhere else and silently returned no bindings at all when
    only one was handled.
    """
    nodes = (statement,) if statement.type == "assignment" else statement.named_children
    for node in nodes:
        if node.type != "assignment":
            continue
        # ``a = b = 1`` nests rightward in this grammar, where ``ast.Assign``
        # carries both targets on one node. Walking the chain recovers them.
        while node is not None and node.type == "assignment":
            left = node.child_by_field_name("left")
            if left is None:
                break
            if left.type == "identifier":
                yield _text(left)
            elif left.type in _PYTHON_PATTERNS:
                for element in left.named_children:
                    if element.type == "identifier":
                        yield _text(element)
            node = node.child_by_field_name("right")


def _python_dotted_top(node: Node | None) -> str | None:
    """``kombu.pools`` and ``kombu.pools as p`` both import ``kombu``."""
    if node is not None and node.type == "aliased_import":
        node = node.child_by_field_name("name")
    if node is None:
        return None
    if node.type == "dotted_name":
        first = node.named_children[0] if node.named_children else None
        return _text(first) if first is not None else None
    if node.type == "identifier":
        return _text(node)
    return None


def _python_imported_names(node: Node) -> Iterator[str]:
    """Mirror of ``imports.imports_in_source`` over one node.

    ``from __future__ import x`` gets its own node type in this grammar, so
    excluding the language directive would be free here. It is counted anyway,
    because ``imports_in_source`` counts it and ``harness/build_corpus.py``
    does not filter it either, and the corpus rebuild is checked byte for byte
    against both. Dropping it is a real improvement and a separate change that
    has to move all three in one commit.
    """
    if node.type == "future_import_statement":
        yield "__future__"
    elif node.type == "import_statement":
        for child in node.named_children:
            if top := _python_dotted_top(child):
                yield top
    elif node.type == "import_from_statement":
        module = node.child_by_field_name("module_name")
        # A relative import names this package, not a dependency, so there is
        # nothing external to record. ``ast`` says this with ``level``.
        if module is None or module.type == "relative_import":
            return
        if top := _python_dotted_top(module):
            yield top


PYTHON = Dialect(
    name="python",
    suffixes=(".py", ".pyx", ".pyi"),
    classes=frozenset({"class_definition"}),
    functions=frozenset({"function_definition"}),
    blocks=frozenset({"block"}),
    bindings=frozenset({"expression_statement", "assignment"}),
    # ``@task`` wraps the def it decorates; the definition is a named field.
    unwrap={"decorated_definition": "definition"},
    bound_names=_python_bound_names,
    imported_names=_python_imported_names,
)

#: Every dialect this module can read, keyed by the grammar's own name.
DIALECTS: dict[str, Dialect] = {PYTHON.name: PYTHON}


def dialect_for(path: str) -> Dialect | None:
    """The dialect claiming ``path``'s suffix, or ``None``.

    The suffix decides, not the pipeline's ``language`` argument.
    ``assets/mappings/language_mappings.json`` maps extensions to languages
    many-to-many and is not a classifier.
    """
    lowered = str(path).lower()
    for dialect in DIALECTS.values():
        if lowered.endswith(dialect.suffixes):
            return dialect
    return None


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
    and lets the caller recurse. A ``block`` is yielded without being entered,
    so the traversal stays level by level and a nested ``if`` inside a block is
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
            qualified = (*prefix, _text(name))
            out.append((CLASS, ".".join(qualified)))
            # Only a class body widens the prefix. A function's locals are not
            # addressable from outside it, so its body is never entered.
            if body := child.child_by_field_name("body"):
                _walk(body, qualified, dialect, out)
        elif child.type in dialect.functions:
            if (name := child.child_by_field_name("name")) is not None:
                out.append((METHOD if prefix else FUNCTION, ".".join((*prefix, _text(name)))))
        elif child.type in dialect.bindings:
            for bound in dialect.bound_names(child):
                out.append((BINDING, ".".join((*prefix, bound))))
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
