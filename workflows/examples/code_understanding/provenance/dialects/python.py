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

from ..treesitter import BINDING, Dialect, node_text

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
)
