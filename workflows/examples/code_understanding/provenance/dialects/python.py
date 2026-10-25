"""Python, the dialect the engine was measured against.

The oracle for this language is ``ast``, so every rule here has an exact
counterpart in :mod:`provenance.declarations` and a disagreement between the
two is a bug in this file. ``tests/test_provenance_treesitter_agreement.py``
recomputes the comparison on all 237 celery files every run.

The grammar still carries the Python 2 productions: ``print x`` is a
``print_statement`` node and ``except E, e`` is an ``except_clause`` with two
values, so none of the 50 files ``ast`` refuses parse as damaged. They parse as
the language they are written in, which is why ``has_error`` stays a signal
about genuinely malformed source rather than a proxy for "old Python".
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from ..treesitter import BINDING, Dialect, ImportCapture, node_text, squeeze

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from tree_sitter import Node

#: The destructuring targets ``declarations._assigned_names`` reads plain names
#: out of. It ignores attribute and subscript targets, because ``self.conf = x``
#: binds no module-level name, and this has to ignore the same ones.
_PATTERNS = frozenset({"pattern_list", "list_pattern", "tuple_pattern"})


def _bound_names(statement: Node) -> Iterator[tuple[str, str]]:
    """Mirror of ``declarations._assigned_names`` over one statement.

    Always ``BINDING``. ``f = lambda x: x`` binds a callable, and the oracle
    counts it as an assignment rather than a function, so this has to as well.

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
                yield BINDING, node_text(left)
            elif left.type in _PATTERNS:
                for element in left.named_children:
                    if element.type == "identifier":
                        yield BINDING, node_text(element)
            node = node.child_by_field_name("right")


def _dotted_top(node: Node | None) -> str | None:
    """``kombu.pools`` and ``kombu.pools as p`` both import ``kombu``."""
    if node is not None and node.type == "aliased_import":
        node = node.child_by_field_name("name")
    if node is None:
        return None
    if node.type == "dotted_name":
        first = node.named_children[0] if node.named_children else None
        return node_text(first) if first is not None else None
    if node.type == "identifier":
        return node_text(node)
    return None


def _imported_names(node: Node) -> Iterator[str]:
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
            if top := _dotted_top(child):
                yield top
    elif node.type == "import_from_statement":
        module = node.child_by_field_name("module_name")
        # A relative import names this package, not a dependency, so there is
        # nothing external to record. ``ast`` says this with ``level``.
        if module is None or module.type == "relative_import":
            return
        if top := _dotted_top(module):
            yield top


def _dotted(node: Node) -> str:
    """``a.b.c`` from a ``dotted_name``, ignoring any whitespace between parts."""
    return ".".join(node_text(c) for c in node.named_children if c.type == "identifier")


def _alias_pair(node: Node) -> tuple[str, str | None] | None:
    """``("a.b", "x")`` for ``a.b as x``, ``("a.b", None)`` for a bare name."""
    if node.type == "aliased_import":
        name, alias = node.child_by_field_name("name"), node.child_by_field_name("alias")
        if name is None:
            return None
        return _dotted(name), node_text(alias) if alias is not None else None
    if node.type == "dotted_name":
        return _dotted(node), None
    return None


def _import_edges(node: Node) -> Iterator[ImportCapture]:
    """Mirror of ``imports.import_captures_in_source`` over one node.

    Unlike :func:`_imported_names` this keeps the whole module, keeps a
    relative import with its dots, and keeps the names a ``from`` import
    binds, because an edge needs to know which file and which name.
    """
    if node.type == "import_statement":
        for child in node.children_by_field_name("name"):
            if pair := _alias_pair(child):
                yield ImportCapture(pair[0], alias=pair[1])
        return
    if node.type == "future_import_statement":
        module = "__future__"
    elif node.type == "import_from_statement":
        source = node.child_by_field_name("module_name")
        if source is None:
            return
        if source.type == "relative_import":
            prefix = next((c for c in source.named_children if c.type == "import_prefix"), None)
            dotted = next((c for c in source.named_children if c.type == "dotted_name"), None)
            module = squeeze(node_text(prefix)) if prefix is not None else ""
            module += _dotted(dotted) if dotted is not None else ""
        else:
            module = _dotted(source)
    else:
        return
    names: list[tuple[str, str | None]] = []
    for child in node.named_children:
        if child.type == "wildcard_import":
            names.append(("*", None))
    names.extend(pair for c in node.children_by_field_name("name") if (pair := _alias_pair(c)))
    yield ImportCapture(module, names=tuple(names))


#: ``class A(metaclass=M)`` and ``**kw`` configure the class rather than name
#: a base, and ``ast`` files them under ``keywords``. Comments are named nodes
#: in this grammar and name nothing.
_NOT_BASES = frozenset({"keyword_argument", "dictionary_splat", "comment"})


def _base_names(node: Node) -> Iterator[tuple[str, str | None]]:
    """Mirror of ``declarations.base_captures_in_source`` over one class.

    Every positional base, as written. ``class A:`` and ``class A():`` name
    none; a base that is an expression rather than a name (``f(E)``,
    ``*bases``) is still yielded verbatim and the resolver calls it
    unresolved, so it is counted rather than lost.
    """
    arguments = node.child_by_field_name("superclasses")
    if arguments is None:
        return
    for child in arguments.named_children:
        if child.type in _NOT_BASES:
            continue
        # ``class A((B))``: ``ast`` drops the parentheses and so must this.
        while child.type == "parenthesized_expression":
            inner = [c for c in child.named_children if c.type != "comment"]
            if len(inner) != 1:
                break
            child = inner[0]
        yield squeeze(node_text(child)), None


PYTHON = Dialect(
    name="python",
    suffixes=(".py", ".pyx", ".pyi"),
    classes=frozenset({"class_definition"}),
    functions=frozenset({"function_definition"}),
    blocks=frozenset({"block"}),
    bindings=frozenset({"expression_statement", "assignment"}),
    # ``@task`` wraps the def it decorates; the definition is a named field.
    unwrap={"decorated_definition": "definition"},
    bound_names=_bound_names,
    imported_names=_imported_names,
    import_edges=_import_edges,
    base_names=_base_names,
)
