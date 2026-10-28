"""The scope bookkeeping behind :mod:`.calls`: what each function, class and
comprehension binds, so a call's head is looked up where Python looks it up.

Every helper here declines in one direction only: a name bound in a way this
cannot read precisely is still bound, which makes a call ``local`` rather
than letting it fall through to a module-level declaration it does not mean.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..dialects import PYTHON
from ..treesitter import node_text
from .python import _target_names

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from tree_sitter import Node

FUNCTION_SCOPE, CLASS_SCOPE, COMPREHENSION_SCOPE = "function", "class", "comprehension"


@dataclass
class Scope:
    kind: str
    #: Names any statement binds here, imports excluded.
    bound: set[str] = field(default_factory=set)
    #: Names a local import binds, shaped as :attr:`FileBindings.bound`.
    imports: dict = field(default_factory=dict)
    globals: set[str] = field(default_factory=set)
    #: A star import or ``exec`` here may bind anything.
    opaque: bool = False
    #: For a method: the class key, the receiver, and whether it is rebound.
    cls: str | None = None
    receiver: str | None = None
    #: For a class body: the class's qualified name in the file.
    qualified: str | None = None
    #: For a method of a class nested in another, where ``super(Inner, self)``
    #: names the module's ``Inner``, not this one.
    nested: bool = False
    #: A Python 2 list comprehension, which is no scope: a class body around
    #: it still encloses it.
    transparent: bool = False
    #: Names bound once, by ``name = dotted.name`` and nothing else, to that
    #: right side: ``Req = Request`` then ``Req(...)`` calls ``Request``.
    aliases: dict[str, str] = field(default_factory=dict)



def first_lines(tree: Node) -> dict[str, int]:
    """Qualified name to the first line a ``def``, ``class`` or ``=`` declares it.

    Walks the positions :func:`provenance.treesitter.symbols_in_tree` records:
    class bodies widen the prefix, function bodies are not entered.
    """
    found: dict[str, int] = {}
    stack: list[tuple[Node, tuple[str, ...]]] = [(tree, ())]
    while stack:
        node, prefix = stack.pop()
        line = node.start_point[0] + 1
        if node.type in ("class_definition", "function_definition"):
            if (name := node.child_by_field_name("name")) is None:
                continue
            qualified = (*prefix, node_text(name))
            found[".".join(qualified)] = min(found.get(".".join(qualified), line), line)
            if node.type == "class_definition" and (body := node.child_by_field_name("body")):
                stack.append((body, qualified))
            continue
        if node.type == "lambda":
            continue
        if node.type == "assignment" and (left := node.child_by_field_name("left")) is not None:
            for name in _target_names(left):
                key = ".".join((*prefix, name))
                found[key] = min(found.get(key, line), line)
        stack.extend((c, prefix) for c in node.named_children)
    return found


def nonlocal_names(body: Node | None) -> set[str]:
    """Names any function nested in ``body`` declares ``nonlocal``."""
    found, stack = set(), [body] if body is not None else []
    while stack:
        node = stack.pop()
        if node.type == "nonlocal_statement":
            found |= {node_text(c) for c in node.named_children if c.type == "identifier"}
        stack.extend(node.named_children)
    return found


def ordered_parameters(params: Node | None) -> list[str]:
    names: list[str] = []
    for child in params.named_children if params is not None else ():
        if child.type == "identifier":
            names.append(node_text(child))
        elif child.type in ("default_parameter", "typed_default_parameter"):
            if (name := child.child_by_field_name("name")) is not None:
                names.extend(_target_names(name) or [node_text(name)])
        elif child.type in ("typed_parameter", "list_splat_pattern", "dictionary_splat_pattern",
                            "tuple_pattern"):
            names.extend(sorted(_identifiers(child, stop={"type"})))
    return names


def parameter_names(params: Node | None) -> set[str]:
    return set(ordered_parameters(params))


def _identifiers(node: Node, stop: set[str]) -> set[str]:
    found, stack = set(), [node]
    while stack:
        n = stack.pop()
        if n.type == "identifier":
            found.add(node_text(n))
        elif n.type not in stop:
            stack.extend(n.named_children)
    return found


def decorators(function: Node) -> set[str]:
    parent = function.parent
    if parent is None or parent.type != "decorated_definition":
        return set()
    return {" ".join(node_text(d).lstrip("@").split()) for d in parent.named_children if d.type == "decorator"}


def bind_body(resolver, f, body: Node | None, scope: Scope) -> set[str]:
    """Fill ``scope`` from a function body; return the names bound other than as parameters.

    Nested functions, classes and lambdas are not entered, but their names are
    bound here. Comprehension targets are bound here too, which over-declines
    in Python 3 and is exact for a Python 2 list comprehension, whose
    variable leaks into the function.
    """
    rebound: set[str] = set()
    params = set(scope.bound)
    times: Counter = Counter()
    simple: dict[str, str] = {}
    stack = list(body.named_children) if body is not None else []
    while stack:
        node = stack.pop()
        kind = node.type
        if kind == "decorated_definition":
            stack.extend(c for c in node.named_children if c.type == "decorator")
            node = node.child_by_field_name("definition") or node
            kind = node.type
        if kind in ("function_definition", "class_definition"):
            rebound.add(name := node_text(node.child_by_field_name("name")))
            times[name] += 1
            stack.extend(c for c in node.named_children if c.type in ("parameters", "argument_list"))
            continue
        if kind == "lambda":
            continue
        if kind in ("assignment", "augmented_assignment", "for_statement", "for_in_clause"):
            left = node.child_by_field_name("left")
            names = _target_names(left)
            rebound |= names
            times.update(names)
            right = node.child_by_field_name("right")
            if (kind == "assignment" and left is not None and left.type == "identifier"
                    and right is not None and right.type in ("identifier", "attribute")):
                simple[node_text(left)] = " ".join(node_text(right).split())
        elif kind == "as_pattern_target" or kind == "case_pattern":
            names = _identifiers(node, stop=set()) if kind == "case_pattern" else _target_names(node)
            rebound |= names
            times.update(names)
        elif kind == "except_clause" and len(values := node.children_by_field_name("value")) > 1:
            rebound |= (names := _target_names(values[1]))
            times.update(names)
        elif kind == "named_expression" and (name := node.child_by_field_name("name")):
            rebound |= (names := _target_names(name))
            times.update(names)
        elif kind == "delete_statement":
            rebound |= (names := {n for c in node.named_children for n in _target_names(c)})
            times.update(names)
        elif kind in ("global_statement", "nonlocal_statement"):
            names = {node_text(c) for c in node.named_children if c.type == "identifier"}
            if kind == "global_statement":
                scope.globals |= names
            else:
                rebound |= names
                times.update(names)
        elif kind == "exec_statement":
            scope.opaque = True
        elif kind in ("import_statement", "import_from_statement", "future_import_statement"):
            _bind_import(resolver, f, node, scope)
            continue
        stack.extend(node.named_children)
    rebound -= scope.globals
    scope.bound |= rebound
    scope.bound -= scope.globals
    scope.aliases = {
        n: r for n, r in simple.items()
        if times[n] == 1 and n not in params and n not in scope.globals and n not in scope.imports
        and r.split(".", 1)[0] != n and not scope.opaque
    }
    return rebound


def _bind_import(resolver, f, node: Node, scope: Scope) -> None:
    res = resolver.resolver
    stars: list = []
    for capture in PYTHON.import_edges(node):
        if capture.written.startswith("."):
            resolved, name = res.relative(capture.written, f.path)
        else:
            resolved, name = res.absolute(capture.written, f.path, f.found.mode)
        res.bind(capture, resolved, name, f.path, f.found.mode, scope.imports, stars)
    scope.opaque |= bool(stars)
