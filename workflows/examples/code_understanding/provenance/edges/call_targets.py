"""Where a resolved Python call lands, read across the whole checkout.

:mod:`.calls` resolves a call's head the way :mod:`.python` resolves a base,
which yields a folded key such as ``celery.utils.get_cls_by_name``. A key is
not yet a callee. This module checks it against what the checkout declares and
says one of three things: the declaration it names, an external target, or why
neither is settled.

**Every lookup is against the whole checkout, never the indexed corpus**, for
the reason :mod:`.model` gives. A method found on the only base this module
can see has not been found if another base is outside the checkout, so an
inherited lookup that reaches an external or unresolved base before the name
stops there and says so.

**Members.** ``M.f`` lands on ``f`` when module ``M`` declares it as a class
or function. When ``M`` binds ``f`` by import instead, the import is followed,
as Python follows it, up to :data:`MAX_CHASE` hops. A name ``M`` binds by
assignment is a ``binding``: what it holds is not syntax. ``C.m`` on a class
walks ``C``'s method resolution order, computed by C3 from the oracle's own
INHERITS edges. ``object`` ends every order and is external; any other
external or unresolved base is opaque, and a lookup that meets one before the
name declines.

**Instances.** ``self.m()`` is the same walk, except that a name the class or
any class in its order assigns through ``self`` (``self.m = handler``) may
hold anything, so it declines as ``attribute``.

**Folding.** Keys are folded by :func:`provenance.corpus.dotted_key`, as every
node key is, and Python is case-sensitive. A key that folds onto a module and
a declaration at once (``celery/app/task.py`` against ``Task`` in
``celery/app/__init__.py``) is ``ambiguous``, never one of the two.

**Known gaps**, each a wrong edge rather than an undercount and none reached
on celery as far as the tests show: ``setattr`` and assignment to an instance
attribute from outside the class (``obj.m = f``) are not read; a metaclass or
``__getattr__`` that supplies a missing name is not modelled, so such a name
is ``not-declared`` rather than resolved; and the order of opaque bases is
taken as a leaf, which matches C3 unless an internal class also inherits from
one of that base's unseen ancestors.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..corpus import dotted_key
from ..declarations import namespace_for
from ..dialects import PYTHON, dialect_for
from ..facts import SKIP_DIRS
from ..treesitter import (
    BINDING,
    CLASS,
    FUNCTION,
    METHOD,
    class_nodes_in_tree,
    node_text,
    symbols_in_tree,
)
from .model import EXTERNAL, INHERITS, INTERNAL, UNRESOLVED, Edge, Inventory
from .python import PYTHON2, FileBindings, PythonResolver, _other_bindings, _target_names

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from tree_sitter import Node

#: Import hops followed from a re-export to its declaration. celery's longest
#: is two (``celery.task.Task`` to ``celery.app.task.BaseTask``).
MAX_CHASE = 8

#: Why a call is not settled. Each is counted, never rounded up.
LOCAL = "local"                  # the head is bound inside the caller
SELF_ATTRIBUTE = "self-attribute"  # an instance attribute, or deeper than self.m
ATTRIBUTE = "attribute"          # an attribute of something that is not a module or class
DYNAMIC = "dynamic"              # the callee is an expression, not a name
LITERAL = "literal"              # a method of a literal: "".join, {}.get
BINDING_REASON = "binding"       # a name bound by assignment or by two statements
STAR = "star"                    # a star import or exec may rebind the name
UNBOUND = "unbound"              # bound nowhere this walk can see
INHERITED = "inherited"          # reached an opaque base before the name
AMBIGUOUS = "ambiguous"          # two targets, or a key that folds onto two things
IMPORT = "import"                # the import binding the head is itself unresolved
MODULE = "module"                # the key names a module, which is not callable
NOT_DECLARED = "not-declared"    # the checkout declares nothing there
#: Settled, but outside the checkout.
BUILTIN = "builtin"

UNRESOLVED_REASONS = (LOCAL, SELF_ATTRIBUTE, ATTRIBUTE, DYNAMIC, LITERAL, BINDING_REASON, STAR,
                      UNBOUND, INHERITED, AMBIGUOUS, IMPORT, MODULE, NOT_DECLARED)
CATEGORIES = (INTERNAL, EXTERNAL, BUILTIN, *UNRESOLVED_REASONS)

_CALLABLE = frozenset({CLASS, FUNCTION, METHOD})
_OBJECT = "object"


@dataclass(frozen=True)
class Landing:
    """Where one key lands: a category, a folded key when settled, and why if not."""

    category: str
    key: str | None = None
    reason: str | None = None


def declined(category: str, reason: str) -> Landing:
    return Landing(category, None, reason)


@dataclass(frozen=True)
class _Opaque:
    """A base this checkout cannot see into, standing in for its whole order."""

    written: str


class Declarations:
    """What every Python file in the checkout declares, and how its classes inherit."""

    def __init__(self, inventory: Inventory, resolver: PythonResolver):
        self.inventory = inventory
        self.resolver = resolver
        #: Folded symbol key to the kinds declared under it, for member lookup.
        self.kinds: dict[str, set[str]] = {}
        #: File to its symbols, real case, qualified within the file.
        self.symbols: dict[str, dict[str, set[str]]] = {}
        #: File to its folded namespace.
        self.namespaces: dict[str, str] = {}
        #: Class key to its bases in written order, as INHERITS edges.
        self.bases: dict[str, list[Edge]] = {}
        #: Class key to the folded names its methods assign through ``self``.
        self.assigned: dict[str, set[str]] = {}
        #: Method keys a property decorator wraps. Reading one calls it, and no
        #: call site says so, which a verdict needs to know.
        self.properties: set[str] = set()
        self._bindings: dict[str, FileBindings] = {}
        self._rebinding: dict[str, set[str]] = {}
        self._mro: dict[str, list[str | _Opaque] | None] = {}
        for path in sorted(inventory.paths):
            if dialect_for(path) is PYTHON and not _skipped(path):
                self._read(path)

    def _read(self, path: str) -> None:
        source = self.inventory.read(path)
        if source is None:
            return
        namespace = namespace_for(path, source, PYTHON)
        self.namespaces[path] = namespace
        held = self.symbols.setdefault(path, {})
        for kind, name in symbols_in_tree(source, PYTHON):
            held.setdefault(name, set()).add(kind)
            self.kinds.setdefault(self.key(path, name), set()).add(kind)
        with self.preserved():
            edges, _ = self.resolver.edges(path, source)
        by_written = {(e.source, e.written): e for e in edges if e.kind == INHERITS}
        for qualified, node in class_nodes_in_tree(source, PYTHON):
            key = self.key(path, qualified)
            self.bases[key] = [by_written[(key, w)] for w, _ in PYTHON.base_names(node)
                               if (key, w) in by_written]
            self.assigned.setdefault(key, set()).update(_self_assigned(node))
            self.properties.update(f"{key}.{name}" for name in _properties(node))
            # A class-body import or loop target rebinds the name for every
            # lookup through the class, which the symbol walk does not record.
            body = node.child_by_field_name("body")
            for name in _class_rebinding(body, self._python2(path)) if body is not None else ():
                self.kinds.setdefault(f"{key}.{dotted_key(name)}", set()).add(BINDING)

    def _python2(self, path: str) -> bool:
        found = self.bindings(path)
        return found is not None and found.mode == PYTHON2

    def rebinding(self, path: str) -> set[str]:
        """Module-level names ``path`` binds by anything but ``def``, ``class`` and ``=``."""
        if path not in self._rebinding:
            found = self.bindings(path)
            self._rebinding[path] = set() if found is None else module_rebinding(
                found.tree, found.mode == PYTHON2)
        return self._rebinding[path]

    def key(self, path: str, qualified: str) -> str:
        namespace = self.namespaces[path]
        return dotted_key(f"{namespace}.{qualified}" if namespace else qualified)

    @contextmanager
    def preserved(self) -> Iterator[None]:
        """Run a resolver call without disturbing the file it is resolving for."""
        held = self.resolver._importer
        try:
            yield
        finally:
            self.resolver._importer = held

    def bindings(self, path: str) -> FileBindings | None:
        if path not in self._bindings:
            source = self.inventory.read(path)
            if source is None:
                return None
            with self.preserved():
                self._bindings[path] = self.resolver.bindings(path, source)
        return self._bindings[path]

    # ----------------------------------------------------------------- landing

    def land(self, written: str, importer: str, depth: int = 0) -> Landing:
        """Where a real-case dotted name, as ``importer`` would import it, lands.

        The longest prefix naming one file is the module, as Python's import
        finds it. Where the package above it also binds that name, attribute
        access and the submodule disagree about what the name holds.
        """
        if depth > MAX_CHASE:
            return declined(IMPORT, f"{written}: more than {MAX_CHASE} re-export hops")
        parts = written.split(".")
        with self.preserved():
            self.resolver._importer = importer
            for i in range(len(parts), 0, -1):
                module = ".".join(parts[:i])
                hits = self.resolver._hit(module)
                if not hits:
                    continue
                if len(hits) > 1:
                    return declined(AMBIGUOUS, f"{module} is {len(hits)} files")
                if not parts[i:]:
                    return declined(MODULE, f"{written} is a module")
                if i > 1 and self.resolver._init_binds(".".join(parts[: i - 1]), parts[i - 1]):
                    return declined(AMBIGUOUS, f"{module} is a submodule and a name its package binds")
                path = next(iter(hits))
                break
            else:
                return declined(NOT_DECLARED, f"no file in the checkout is a prefix of {written}")
        return self.in_file(path, parts[i:], depth)

    def in_file(self, path: str, members: list[str], depth: int = 0) -> Landing:
        """``members`` (real case) looked up at module level in ``path``."""
        held = self.symbols.get(path)
        found = self.bindings(path)
        if held is None or found is None:
            return declined(NOT_DECLARED, f"{path} is not read")
        first, rest = members[0], [dotted_key(m) for m in members[1:]]
        if first in self.rebinding(path):
            return declined(BINDING_REASON, f"{path} rebinds {first} by a for, with, except, global, del "
                                            f"or starred statement")
        if first in held:
            if first in found.bound:
                return declined(BINDING_REASON, f"{path} binds {first} by import and by a statement")
            for written, _resolved, name in found.stars:
                with self.preserved():
                    self.resolver._importer = path
                    listed = self.resolver._dunder_all(name) if name else None
                if listed is None:
                    return declined(STAR, f"{path} star-imports {written}, which may rebind {first}")
                if first in listed:
                    return declined(BINDING_REASON, f"{path} binds {first} and star-imports it from {written}")
            return self._settle(self.key(path, first), held[first], rest, instance=False)
        entries = set(found.bound.get(first, ()))
        if not entries:
            if found.stars:
                return declined(STAR, f"{path} star-imports, and does not declare {first}")
            return declined(NOT_DECLARED, f"{path} neither declares nor imports {first}")
        if len(entries) > 1:
            return declined(AMBIGUOUS, f"{path} binds {first} by {len(entries)} imports")
        return self.through(next(iter(entries)), members[1:], path, depth + 1)

    def through(self, entry: tuple, rest: list[str], importer: str, depth: int = 0) -> Landing:
        """Follow one import binding ``(resolution, module name, imported name)``."""
        resolved, name, imported = entry
        if resolved.target_class == UNRESOLVED or name is None:
            return declined(IMPORT, f"the import binding it is unresolved: {resolved.reason}")
        if resolved.target_class == EXTERNAL:
            tail = [dotted_key(p) for p in (imported, *rest) if p]
            return Landing(EXTERNAL, ".".join([resolved.key, *tail]))
        return self.land(".".join(p for p in (name, imported, *rest) if p), importer, depth)

    def _settle(self, key: str, kinds: set[str], rest: list[str], instance: bool) -> Landing:
        if len(kinds) != 1 or not kinds <= _CALLABLE:
            held = " and ".join(sorted(kinds))
            return declined(BINDING_REASON, f"{key} is a {held}, and what it holds is not syntax")
        if not rest:
            return Landing(INTERNAL, key)
        if kinds != {CLASS}:
            return declined(ATTRIBUTE, f"{key}.{'.'.join(rest)} is an attribute of a function")
        return self.member(key, rest, instance=instance)

    # ------------------------------------------------------------- inheritance

    def mro(self, cls: str, seen: frozenset[str] = frozenset()) -> list[str | _Opaque] | None:
        """C3 over the oracle's INHERITS edges, opaque bases as leaves; ``None`` if C3 fails."""
        if cls in self._mro:
            return self._mro[cls]
        if cls in seen:
            return None
        heads: list[list[str | _Opaque]] = []
        for edge in self.bases.get(cls, []):
            if edge.target_class == EXTERNAL and edge.target == _OBJECT:
                continue
            base = self._class_of(edge)
            if isinstance(base, _Opaque):
                heads.append([base])
                continue
            order = self.mro(base, seen | {cls})
            if order is None:
                self._mro[cls] = None
                return None
            heads.append(list(order))
        merged = _c3([*heads, [h[0] for h in heads]])
        self._mro[cls] = None if merged is None else [cls, *merged]
        return self._mro[cls]

    def _class_of(self, edge: Edge) -> str | _Opaque:
        """The class an internal base names, followed through re-exports."""
        if edge.target_class != INTERNAL:
            return _Opaque(edge.written)
        if self.kinds.get(edge.target) == {CLASS}:
            return edge.target
        head, *rest = edge.written.split(".")
        found = self.bindings(edge.path)
        entries = set(found.bound.get(head, ())) if found is not None else set()
        if len(entries) == 1:
            landed = self.through(next(iter(entries)), rest, edge.path)
            if landed.category == INTERNAL and self.kinds.get(landed.key) == {CLASS}:
                return landed.key
        return _Opaque(edge.written)

    def member(self, cls: str, rest: list[str], instance: bool) -> Landing:
        """``rest`` looked up on class ``cls``, through its order."""
        name = rest[0]
        order = self.mro(cls)
        if order is None:
            return declined(INHERITED, f"{cls} has no consistent method resolution order")
        # The instance dict beats every class, so an assignment anywhere in the
        # order shadows a declaration anywhere in it.
        if instance and (by := next((e for e in order if not isinstance(e, _Opaque)
                                     and name in self.assigned.get(e, ())), None)):
            return declined(SELF_ATTRIBUTE, f"{by} assigns self.{name}")
        for entry in order:
            if isinstance(entry, _Opaque):
                return declined(INHERITED, f"{cls}.{name}: base {entry.written} is outside the checkout "
                                           f"or unresolved, and comes before any declaration")
            if (kinds := self.kinds.get(f"{entry}.{name}")) is not None:
                return self._settle(f"{entry}.{name}", kinds, rest[1:], instance=False)
        if hasattr(object, name):
            return Landing(EXTERNAL, f"{_OBJECT}.{name}")
        return declined(NOT_DECLARED, f"{cls} and its bases declare no {name}")

    def after(self, cls: str, name: str) -> Landing:
        """``super().name`` inside ``cls``: the order past ``cls`` itself."""
        order = self.mro(cls)
        if order is None:
            return declined(INHERITED, f"{cls} has no consistent method resolution order")
        for entry in order[1:]:
            if isinstance(entry, _Opaque):
                return declined(INHERITED, f"super().{name} in {cls}: base {entry.written} is opaque")
            if (kinds := self.kinds.get(f"{entry}.{name}")) is not None:
                return self._settle(f"{entry}.{name}", kinds, [], instance=False)
        if hasattr(object, name):
            return Landing(EXTERNAL, f"{_OBJECT}.{name}")
        return declined(NOT_DECLARED, f"no base of {cls} declares {name}")


def _skipped(path: str) -> bool:
    parts = posixpath.dirname(path).split("/")
    return any(p in SKIP_DIRS or p.endswith(".egg-info") for p in parts)


def _c3(sequences: list[list]) -> list | None:
    """The C3 merge, or ``None`` where no consistent order exists."""
    sequences = [list(s) for s in sequences if s]
    merged: list = []
    while sequences:
        for seq in sequences:
            head = seq[0]
            if not any(head in s[1:] for s in sequences):
                break
        else:
            return None
        merged.append(head)
        sequences = [s[1:] if s[0] == head else s for s in sequences]
        sequences = [s for s in sequences if s]
    return merged


def _self_assigned(class_node: Node) -> set[str]:
    """Folded names any method of this class assigns through its first parameter.

    Only the class's own methods, not nested classes: a nested class's
    ``self`` is another object.
    """
    found: set[str] = set()
    body = class_node.child_by_field_name("body")
    stack = list(body.named_children) if body is not None else []
    while stack:
        node = stack.pop()
        if node.type == "decorated_definition":
            node = node.child_by_field_name("definition") or node
        if node.type == "class_definition":
            continue
        if node.type != "function_definition":
            stack.extend(node.named_children)
            continue
        params = node.child_by_field_name("parameters")
        first = params.named_children[0] if params is not None and params.named_children else None
        if first is None or first.type != "identifier":
            continue
        receiver = node_text(first)
        inner = [node.child_by_field_name("body")]
        while inner:
            n = inner.pop()
            if n is None:
                continue
            if n.type in ("assignment", "augmented_assignment", "for_statement"):
                target = n.child_by_field_name("left")
                for t in _attribute_targets(target):
                    obj, attr = t.child_by_field_name("object"), t.child_by_field_name("attribute")
                    if obj is not None and attr is not None and node_text(obj) == receiver:
                        found.add(node_text(attr).lower())
            inner.extend(n.named_children)
    return found


def module_rebinding(tree: Node, python2: bool) -> set[str]:
    """Module-level names bound by anything the symbol walk does not record.

    :func:`.python._other_bindings` (``for``, ``with``, ``except``, ``global``
    and the rest), plus ``del`` and the starred target of an unpacking, which
    both the symbol walk and ``_other_bindings`` pass over. A name in here may
    hold anything by the time a call reads it.
    """
    found = _other_bindings(tree, python2=python2)
    stack = list(tree.named_children)
    while stack:
        node = stack.pop()
        if node.type in ("function_definition", "class_definition", "lambda", "decorated_definition"):
            continue
        if node.type == "delete_statement":
            found |= {n for c in node.named_children for n in _target_names(c)}
        elif node.type == "assignment" and (left := node.child_by_field_name("left")) is not None:
            found |= {node_text(i) for splat in _descendants(left, "list_splat_pattern")
                      for i in splat.named_children if i.type == "identifier"}
        stack.extend(node.named_children)
    return found


def _class_rebinding(body: Node, python2: bool) -> set[str]:
    """Names a class body binds by import or a loop-like statement, not by ``def``/``=``."""
    found = module_rebinding(body, python2)
    stack = list(body.named_children)
    while stack:
        node = stack.pop()
        if node.type in ("function_definition", "class_definition", "lambda", "decorated_definition"):
            continue
        for capture in PYTHON.import_edges(node):
            if capture.names:
                found |= {alias or n for n, alias in capture.names if n != "*"}
            else:
                found.add(capture.alias or capture.written.split(".", 1)[0])
        stack.extend(node.named_children)
    return found


def _descendants(node: Node, kind: str) -> Iterator[Node]:
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == kind:
            yield n
        stack.extend(n.named_children)


def _properties(class_node: Node) -> Iterator[str]:
    """Folded names of the methods a ``property``-like decorator wraps.

    ``property``, ``cached_property``, kombu's ``cached_property`` and the
    ``x.setter`` family: any decorator whose last segment ends in
    ``property`` or is a setter, getter or deleter.
    """
    body = class_node.child_by_field_name("body")
    stack = list(body.named_children) if body is not None else []
    while stack:
        node = stack.pop()
        if node.type in ("class_definition", "function_definition"):
            continue
        if node.type != "decorated_definition":
            stack.extend(node.named_children)
            continue
        definition = node.child_by_field_name("definition")
        name = definition.child_by_field_name("name") if definition is not None else None
        if name is None:
            continue
        for decorator in (c for c in node.named_children if c.type == "decorator"):
            last = node_text(decorator).lstrip("@").split("(")[0].strip().rpartition(".")[2]
            if last.endswith("property") or last in ("setter", "getter", "deleter"):
                yield node_text(name).lower()
                break


def _attribute_targets(node: Node | None) -> Iterator[Node]:
    if node is None:
        return
    if node.type == "attribute":
        yield node
    elif node.type in ("pattern_list", "tuple_pattern", "list_pattern", "tuple", "list"):
        for child in node.named_children:
            yield from _attribute_targets(child)
