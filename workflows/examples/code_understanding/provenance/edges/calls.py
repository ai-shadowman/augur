"""Python CALLS: every call site, the callee resolved only where the source settles it.

CALLS is the third edge kind and the one :mod:`provenance.edges` declined. The
call site is syntax; the callee is a resolution, and resolving it against any
evidence smaller than the program is the partial-evidence failure. So this
module emits a CALLS edge only where the callee's identity follows from the
file's own bindings and a declaration the **whole checkout** holds, and counts
every other call site by reason (:data:`.call_targets.UNRESOLVED_REASONS`),
never rounding one up.

**The caller** is the innermost declaration :func:`provenance.treesitter.symbols_in_tree`
records around the call: a method, a function, or a class for a call in the
class body. A closure's calls belong to the declaration enclosing it, since a
closure has no node. A call at module level belongs to the module. Decorators,
default values and base expressions run in the enclosing scope and are
attributed there, as Python evaluates them. A bare decorator (``@task``) is a
call site too, since applying it calls it.

**The callee's head** resolves by Python's scoping: the caller's own bindings
first (any binding at all makes the name ``local``, since what a local holds is
not syntax, except an import inside the function, which resolves like a
module-level one), then each enclosing function, then the class body only for
a call directly in it, then the module through
:meth:`.python.PythonResolver._base`, so an opaque binding (``try: import pwd``
/ ``except ImportError: pwd = None``), a star import or a Python 2 implicit
relative import declines exactly as it does for a base. A comprehension is its
own scope.

**Receivers.** ``self.m()`` and ``cls.m()``, where the receiver is the
method's first parameter and is never rebound, look ``m`` up on the enclosing
class (:meth:`.call_targets.Declarations.member`). ``super().m()`` and
``super(C, self).m()`` look past it. A call on any other receiver resolves
only when the receiver is a bound module or class; an attribute of a local or
of ``self.x`` is counted, not guessed.

**What a CALLS edge means here** is the static target: ``self.run()`` in
``Base`` is ``Base.run`` even where a subclass overrides it. Instantiation
(``Foo()``) is a call to the class.

The walk reads every file with tree-sitter, which reads the Python 2 files
``ast`` refuses. ``tests/test_provenance_calls.py`` checks its call sites
against ``ast`` on every file ``ast`` accepts.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..corpus import dotted_key
from ..declarations import module_key, namespace_for
from ..dialects import PYTHON, dialect_for
from ..treesitter import CLASS, FUNCTION, METHOD, has_error, node_text, symbols_in_tree
from . import call_targets as ct
from .model import EXTERNAL, INTERNAL, UNRESOLVED, Edge, Inventory, checkout_files
from .python import _DOTTED, PYTHON2, FileBindings, PythonResolver, _other_bindings, _target_names

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from tree_sitter import Node

logger = logging.getLogger(__name__)

CALLS = "CALLS"

_FUNCTION_SCOPE, _CLASS_SCOPE, _COMPREHENSION_SCOPE = "function", "class", "comprehension"
_COMPREHENSIONS = frozenset({"list_comprehension", "set_comprehension", "dictionary_comprehension",
                             "generator_expression"})
_LITERALS = frozenset({"string", "concatenated_string", "list", "dictionary", "set", "tuple",
                       "integer", "float", "list_comprehension", "dictionary_comprehension",
                       "set_comprehension"})
#: Every kind a call's head may name at module level, or in a class body.
_MODULE_KINDS = frozenset({CLASS, FUNCTION})
_CLASS_KINDS = frozenset({CLASS, FUNCTION, METHOD})

#: Python 2 builtins a call names and a base never does. Read off the
#: interpreter for the rest, as :data:`.python.PYTHON2_BUILTINS` is.
PYTHON2_CALLABLES = frozenset({
    "reduce", "cmp", "raw_input", "unichr", "execfile", "reload", "apply", "intern", "coerce",
})

#: How :meth:`PythonResolver._base` words each decline, to the reason counted.
#: An unmatched message is an import that did not resolve.
_BASE_REASONS = (
    ("star", ct.STAR),
    ("bound both in this file", ct.BINDING_REASON),
    (" here, and what it holds is not syntax", ct.BINDING_REASON),
    ("bound by a for, with, except or global", ct.BINDING_REASON),
    ("imports to different targets", ct.AMBIGUOUS),
    ("is not bound in this file", ct.UNBOUND),
)


@dataclass(frozen=True)
class Call:
    """One call site and where its callee landed."""

    #: Folded key of the declaration the call is in, or the file's module key.
    caller: str
    #: The callee as written, whitespace squeezed: ``self.run``, ``utils.f``.
    written: str
    path: str
    line: int
    #: One of :data:`.call_targets.CATEGORIES`.
    category: str
    #: Folded callee key when ``category`` is internal, external or builtin.
    target: str | None = None
    reason: str | None = None

    @property
    def target_class(self) -> str:
        if self.category == INTERNAL:
            return INTERNAL
        return EXTERNAL if self.category in (EXTERNAL, ct.BUILTIN) else UNRESOLVED

    def edge(self) -> Edge:
        detail = ct.BUILTIN if self.category == ct.BUILTIN else None
        reason = None if self.target else f"{self.category}: {self.reason}"
        return Edge(CALLS, self.caller, self.written, self.target, self.target_class, self.path,
                    reason, detail)


@dataclass
class CallScan:
    """Every call site one checkout's Python states, with the denominator beside it."""

    calls: list[Call] = field(default_factory=list)
    #: Evidence to file count, as :class:`provenance.edges.EdgeScan` counts it.
    files: Counter = field(default_factory=Counter)

    def by_category(self) -> dict[str, int]:
        counts = Counter(c.category for c in self.calls)
        return {k: counts[k] for k in ct.CATEGORIES}

    def edges(self) -> set[Edge]:
        """Distinct ``(caller, callee)`` edges, resolved and not."""
        return {c.edge() for c in self.calls}

    def pairs(self) -> set[tuple[str, str]]:
        """Distinct settled ``(caller, callee)`` pairs, internal and external."""
        return {(c.caller, c.target) for c in self.calls if c.target}

    def unsettled_callers(self) -> Counter:
        """Caller key to how many of its call sites did not settle."""
        return Counter(c.caller for c in self.calls if c.target is None)


@dataclass
class _Scope:
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


@dataclass
class _File:
    path: str
    namespace: str
    module: str
    found: FileBindings
    symbols: dict[str, set[str]]
    other: set[str]


class CallResolver:
    """Call sites for one file at a time, resolved against one checkout."""

    def __init__(self, inventory: Inventory, resolver: PythonResolver | None = None):
        self.resolver = resolver or PythonResolver(inventory)
        self.declarations = ct.Declarations(inventory, self.resolver)

    def calls(self, path: str, source: str) -> tuple[list[Call], bool]:
        """Every call site in one file, and whether ``ast`` read it."""
        found = self.resolver.bindings(path, source)
        symbols: dict[str, set[str]] = {}
        for kind, name in symbols_in_tree(source, PYTHON):
            symbols.setdefault(name, set()).add(kind)
        namespace = namespace_for(path, source, PYTHON)
        f = _File(path, namespace, dotted_key(module_key(path)), found, symbols,
                  _other_bindings(found.tree, python2=found.mode == PYTHON2))
        out: list[Call] = []
        self._walk(f, found.tree, (), "", False, out)
        return out, found.used_ast

    # -------------------------------------------------------------------- walk

    def _walk(self, f: _File, root: Node, scopes: tuple[_Scope, ...], caller: str,
              in_function: bool, out: list[Call]) -> None:
        stack: list[tuple[Node, tuple[_Scope, ...], str, bool, tuple[str, ...]]] = [
            (root, scopes, caller, in_function, ())]
        while stack:
            node, scopes, caller, in_function, prefix = stack.pop()
            kind = node.type
            if kind == "decorator":
                inner = next((c for c in node.named_children if c.type != "comment"), None)
                if inner is not None and inner.type != "call":
                    out.append(self._site(f, inner, scopes, caller))
            if kind == "call":
                fn = node.child_by_field_name("function")
                if fn is not None:
                    out.append(self._site(f, fn, scopes, caller))
            if kind == "function_definition":
                name = node_text(node.child_by_field_name("name"))
                inner_caller = caller if in_function else ".".join((*prefix, name))
                scope = self._function_scope(f, node, scopes, prefix, in_function)
                body = node.child_by_field_name("body")
                for child in node.named_children:
                    if child == body:
                        stack.append((child, (*scopes, scope), inner_caller, True, prefix))
                    elif child.type != "identifier":
                        stack.append((child, scopes, caller, in_function, prefix))
                continue
            if kind == "class_definition":
                name = node_text(node.child_by_field_name("name"))
                body = node.child_by_field_name("body")
                inner_prefix = prefix if in_function else (*prefix, name)
                inner_caller = caller if in_function else ".".join(inner_prefix)
                scope = _Scope(_CLASS_SCOPE, qualified=None if in_function else ".".join(inner_prefix))
                for child in node.named_children:
                    if child == body:
                        stack.append((child, (*scopes, scope), inner_caller, in_function, inner_prefix))
                    elif child.type != "identifier":
                        stack.append((child, scopes, caller, in_function, prefix))
                continue
            if kind == "lambda":
                scope = _Scope(_FUNCTION_SCOPE)
                params = node.child_by_field_name("parameters")
                scope.bound |= _parameter_names(params)
                for child in node.named_children:
                    inside = child != params
                    stack.append((child, (*scopes, scope) if inside else scopes, caller, in_function, prefix))
                continue
            if kind in _COMPREHENSIONS:
                scope = _Scope(_COMPREHENSION_SCOPE)
                for clause in node.named_children:
                    if clause.type == "for_in_clause":
                        scope.bound |= _target_names(clause.child_by_field_name("left"))
                stack.extend((c, (*scopes, scope), caller, in_function, prefix) for c in node.named_children)
                continue
            stack.extend((c, scopes, caller, in_function, prefix) for c in reversed(node.named_children))

    def _function_scope(self, f: _File, node: Node, scopes, prefix, in_function) -> _Scope:
        scope = _Scope(_FUNCTION_SCOPE)
        params = node.child_by_field_name("parameters")
        names = _ordered_parameters(params)
        scope.bound |= set(names)
        rebound = _bind_body(self, f, node.child_by_field_name("body"), scope)
        enclosing = scopes[-1] if scopes else None
        first = params.named_children[0] if params is not None and params.named_children else None
        if (not in_function and enclosing is not None and enclosing.kind == _CLASS_SCOPE
                and first is not None and first.type == "identifier"):
            decorators = _decorators(node)
            if "staticmethod" not in decorators and names[0] not in rebound:
                scope.cls = dotted_key(f"{f.namespace}.{'.'.join(prefix)}" if f.namespace else ".".join(prefix))
                scope.receiver = names[0]
        return scope

    # --------------------------------------------------------------- resolving

    def _site(self, f: _File, fn: Node, scopes: tuple[_Scope, ...], caller: str) -> Call:
        written = " ".join(node_text(fn).split())
        key = dotted_key(f"{f.namespace}.{caller}" if f.namespace and caller else caller or f.module)
        landing = self._callee(f, fn, written, scopes)
        return Call(key, written, f.path, fn.start_point[0] + 1, landing.category, landing.key, landing.reason)

    def _callee(self, f: _File, fn: Node, written: str, scopes: tuple[_Scope, ...]) -> ct.Landing:
        if fn.type == "attribute":
            obj, attr = fn.child_by_field_name("object"), fn.child_by_field_name("attribute")
            if obj is not None and obj.type == "call" and attr is not None:
                return self._super(obj, node_text(attr), scopes)
            if obj is not None and obj.type in _LITERALS:
                return ct.declined(ct.LITERAL, f"{written} is a method of a literal")
        if fn.type not in ("identifier", "attribute") or not _DOTTED.fullmatch(written):
            return ct.declined(ct.DYNAMIC, f"{written} is an expression, not a name")
        head, _, rest = written.partition(".")

        for i in range(len(scopes) - 1, -1, -1):
            scope = scopes[i]
            if scope.kind == _CLASS_SCOPE:
                if i != len(scopes) - 1:
                    continue  # a class body does not enclose its methods
                if scope.qualified is None:
                    return ct.declined(ct.LOCAL, f"{head} is read in the body of a class inside a function")
                return self._module(f, head, rest, written, owner=f"{scope.qualified}.<call>")
            if scope.opaque:
                return ct.declined(ct.STAR, f"a star import or exec in the caller may bind {head}")
            if head in scope.globals:
                break
            if head == scope.receiver and scope.cls:
                return self._receiver(scope.cls, rest, written)
            if head in scope.bound:
                return ct.declined(ct.LOCAL, f"{head} is bound inside the caller")
            if head in scope.imports:
                return self._imported(f, scope.imports[head], head, rest)
        return self._module(f, head, rest, written, owner="")

    def _module(self, f: _File, head: str, rest: str, written: str, owner: str) -> ct.Landing:
        """The head through the class body (when ``owner`` names one) and the module.

        :meth:`PythonResolver._base` decides whether the name is settled, so a
        call and a base cannot disagree about it; the landing is then redone on
        the real-case name, because the resolver's keys are folded and
        ``Signal`` beside ``signal.py`` is not ``signal``.
        """
        kinds = _CLASS_KINDS if owner else _MODULE_KINDS
        resolved = self.resolver._base(owner, written, f.namespace, f.symbols, f.other,
                                       f.found.bound, f.found.stars, accept=kinds)
        members = written.split(".")
        if resolved.target_class == UNRESOLVED:
            if resolved.reason and "is not bound in this file" in resolved.reason and head in PYTHON2_CALLABLES:
                return ct.Landing(ct.BUILTIN, dotted_key(written))
            return self._declined(resolved.reason or "")
        enclosing = owner.rpartition(".")[0]
        if enclosing and f"{enclosing}.{head}" in f.symbols:
            return self.declarations.in_file(f.path, [*enclosing.split("."), *members])
        if head in f.symbols:
            return self.declarations.in_file(f.path, members)
        if resolved.target_class == EXTERNAL and head not in f.found.bound:
            return ct.Landing(ct.BUILTIN, resolved.key)
        if head in f.found.bound:
            return self.declarations.through(f.found.bound[head][0], members[1:], f.path)
        for _written, star, name in f.found.stars:
            if name and head in (self.resolver._dunder_all(name) or ()):
                return self.declarations.through((star, name, head), members[1:], f.path)
        return ct.declined(ct.UNBOUND, f"{written} settled with no binding this walk can follow")

    def _imported(self, f: _File, bindings: list, head: str, rest: str) -> ct.Landing:
        entries = set(bindings)
        if len(entries) > 1:
            return ct.declined(ct.AMBIGUOUS, f"{head} is bound by {len(entries)} local imports")
        return self.declarations.through(next(iter(entries)), rest.split(".") if rest else [], f.path)

    @staticmethod
    def _declined(reason: str) -> ct.Landing:
        for phrase, category in _BASE_REASONS:
            if phrase in reason:
                return ct.declined(category, reason)
        return ct.declined(ct.IMPORT, reason)

    def _receiver(self, cls: str, rest: str, written: str) -> ct.Landing:
        if not rest:
            return ct.declined(ct.LOCAL, f"{written} calls the instance itself")
        parts = rest.split(".")
        if len(parts) > 1:
            return ct.declined(ct.SELF_ATTRIBUTE, f"{written} calls an attribute of an attribute")
        return self.declarations.member(cls, [dotted_key(parts[0])], instance=True)

    def _super(self, obj: Node, attr: str, scopes: tuple[_Scope, ...]) -> ct.Landing:
        fn = obj.child_by_field_name("function")
        if fn is None or node_text(fn) != "super":
            return ct.declined(ct.DYNAMIC, f"{node_text(obj)}.{attr} calls an attribute of a call")
        method = next((s for s in reversed(scopes) if s.kind == _FUNCTION_SCOPE), None)
        if method is None or not method.cls or method is not _innermost_function(scopes):
            return ct.declined(ct.DYNAMIC, "super() outside a method of a recorded class")
        args = obj.child_by_field_name("arguments")
        given = [node_text(a) for a in args.named_children] if args is not None else []
        if given and (len(given) != 2 or dotted_key(given[0]) != method.cls.rpartition(".")[2]
                      or given[1] != method.receiver):
            return ct.declined(ct.DYNAMIC, f"super({', '.join(given)}) names another class or receiver")
        return self.declarations.after(method.cls, dotted_key(attr))


def _innermost_function(scopes: tuple[_Scope, ...]) -> _Scope | None:
    return next((s for s in reversed(scopes) if s.kind != _COMPREHENSION_SCOPE), None)


# ------------------------------------------------------------------ bindings

def _ordered_parameters(params: Node | None) -> list[str]:
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


def _parameter_names(params: Node | None) -> set[str]:
    return set(_ordered_parameters(params))


def _identifiers(node: Node, stop: set[str]) -> set[str]:
    found, stack = set(), [node]
    while stack:
        n = stack.pop()
        if n.type == "identifier":
            found.add(node_text(n))
        elif n.type not in stop:
            stack.extend(n.named_children)
    return found


def _decorators(function: Node) -> set[str]:
    parent = function.parent
    if parent is None or parent.type != "decorated_definition":
        return set()
    return {" ".join(node_text(d).lstrip("@").split()) for d in parent.named_children if d.type == "decorator"}


def _bind_body(resolver: CallResolver, f: _File, body: Node | None, scope: _Scope) -> set[str]:
    """Fill ``scope`` from a function body; return the names bound other than as parameters.

    Nested functions, classes and lambdas are not entered, but their names are
    bound here. Comprehension targets are bound here too, which over-declines
    in Python 3 and is exact for a Python 2 list comprehension, whose
    variable leaks into the function.
    """
    rebound: set[str] = set()
    stack = list(body.named_children) if body is not None else []
    while stack:
        node = stack.pop()
        kind = node.type
        if kind == "decorated_definition":
            stack.extend(c for c in node.named_children if c.type == "decorator")
            node = node.child_by_field_name("definition") or node
            kind = node.type
        if kind in ("function_definition", "class_definition"):
            rebound.add(node_text(node.child_by_field_name("name")))
            stack.extend(c for c in node.named_children if c.type in ("parameters", "argument_list"))
            continue
        if kind == "lambda":
            continue
        if kind in ("assignment", "augmented_assignment", "for_statement", "for_in_clause"):
            rebound |= _target_names(node.child_by_field_name("left"))
        elif kind == "as_pattern_target" or kind == "case_pattern":
            rebound |= _identifiers(node, stop=set()) if kind == "case_pattern" else _target_names(node)
        elif kind == "except_clause" and len(values := node.children_by_field_name("value")) > 1:
            rebound |= _target_names(values[1])
        elif kind == "named_expression" and (name := node.child_by_field_name("name")):
            rebound |= _target_names(name)
        elif kind == "delete_statement":
            rebound |= {n for c in node.named_children for n in _target_names(c)}
        elif kind in ("global_statement", "nonlocal_statement"):
            names = {node_text(c) for c in node.named_children if c.type == "identifier"}
            if kind == "global_statement":
                scope.globals |= names
            else:
                rebound |= names
        elif kind == "exec_statement":
            scope.opaque = True
        elif kind in ("import_statement", "import_from_statement", "future_import_statement"):
            _bind_import(resolver, f, node, scope)
            continue
        stack.extend(node.named_children)
    rebound -= scope.globals
    scope.bound |= rebound
    scope.bound -= scope.globals
    return rebound


def _bind_import(resolver: CallResolver, f: _File, node: Node, scope: _Scope) -> None:
    res = resolver.resolver
    stars: list = []
    for capture in PYTHON.import_edges(node):
        if capture.written.startswith("."):
            resolved, name = res.relative(capture.written, f.path)
        else:
            resolved, name = res.absolute(capture.written, f.path, f.found.mode)
        res.bind(capture, resolved, name, f.path, f.found.mode, scope.imports, stars)
    scope.opaque |= bool(stars)


# ---------------------------------------------------------------------- scan

def scan_calls(root: Path, inventory: Inventory | None = None) -> CallScan:
    """Every call site in every Python file under ``root``.

    Files read skip :data:`provenance.facts.SKIP_DIRS`, as
    :func:`provenance.edges.scan_checkout` does; targets resolve against the
    whole checkout.
    """
    inventory = inventory or Inventory(checkout_files(root), root)
    resolver = CallResolver(inventory)
    scan = CallScan()
    for path in sorted(inventory.paths):
        if dialect_for(path) is not PYTHON or ct._skipped(path):
            continue
        source = inventory.read(path)
        if source is None:
            scan.files[("python", "unreadable")] += 1
            continue
        found, used_ast = resolver.calls(path, source)
        scan.calls.extend(found)
        evidence = "ast" if used_ast else "tree-sitter-recovered" if has_error(source, PYTHON) else "tree-sitter"
        scan.files[("python", evidence)] += 1
    return scan
