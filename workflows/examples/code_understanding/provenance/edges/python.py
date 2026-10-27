"""Python: resolving what ``ast`` and tree-sitter read into edges.

The two readers are compared on their captures, before anything here runs, so
this module is shared by both and a mistake in it cannot hide behind their
agreement. Every rule below is Python's own import and scoping semantics.
Where the source alone does not settle one, the edge is ``unresolved`` with
the reason, never a guess, and "settled" means settled against evidence that
covers the answer: a reading that only fails to find an alternative inside the
checkout has not ruled out the standard library or a dependency.

**Imports.** ``import a.b`` is an edge to ``a.b``; ``from m import x`` is an
edge to ``m``, plus an edge to ``m.x`` when the checkout holds that file and
``m``'s ``__init__`` does not bind ``x`` itself, since then the import gets the
binding and not the file. Names match case-sensitively, as the import system
does. A name absent from every root is external only when nothing of it is in
the checkout: a directory on its path makes it unresolved, and so does a file
reached only through a discovered root the importing file is not beneath,
since whether that root is on ``sys.path`` is not in the source.

**Python 2.** ``import states`` inside ``celery/`` means ``celery.states`` first
in Python 2 and ``states`` alone in Python 3, unless the file imports
``absolute_import``. The file's version decides, so it is read from the file:
Python 2 when ``ast`` refuses it *and* tree-sitter finds syntax only Python 2
has (``print x``, ``exec code``, ``except E, e``). A file that parses as
Python 3 and shows neither is not evidence of either, so where a sibling would
be shadowed the edge is unresolved, never "the only file I know of".

**Bases.** A base resolves through the names the file binds at module level,
or in the enclosing class body: a class declared there, a name a module-level
import binds, or a builtin. A name bound any other way (an assignment, a
``for``/``with``/``except`` target, ``global``) is unresolved, because what it
holds is not syntax. A star import binds what its module's ``__all__`` lists,
which is syntax when the module is in the checkout and states ``__all__`` once
as a literal: trac's ``from trac.core import *`` is 213 of its bases. Any other
star import may rebind any name, so every base in that file is unresolved, and
so is a name a star binds and the file binds again, since which comes last is
not tracked. Re-exports are not chased: ``Task`` imported from ``celery.task`` is
``celery.task.Task`` even if that package re-exports it from elsewhere, so an
internal target is not always a class node.

**Known gaps, both wrong edges rather than undercounts and both rare.**
Statement order inside an enclosing class body is ignored, so a class-level
name bound *after* the nested class statement still shadows the module's.
The ``match`` statement is walked by tree-sitter and not by ``ast``, a gap
inherited from ``declarations._collect`` and left with it; no corpus here
reaches it. The builtin set and which files ``ast`` accepts are those of the
interpreter running the scan, 3.11 in the pinned environment.
"""

from __future__ import annotations

import builtins
import posixpath
import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from ..corpus import dotted_key
from ..declarations import base_captures_in, module_key, namespace_for
from ..dialects import PYTHON
from ..imports import import_captures_in
from ..treesitter import CLASS, ImportCapture, node_text, parse, symbols_in_tree
from .model import (
    EXTERNAL,
    IMPORTS,
    INHERITS,
    INTERNAL,
    PYTHON_ROOTS,
    UNRESOLVED,
    Edge,
    Inventory,
    Resolution,
    unresolved,
)

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from tree_sitter import Node

#: Builtins Python 2 had and Python 3 does not, which celery-era code
#: subclasses (``class TimeoutError(StandardError)``). Read off the
#: interpreter for the rest, so this lists only the difference.
PYTHON2_BUILTINS = frozenset({
    "StandardError", "basestring", "unicode", "long", "file", "buffer", "xrange",
})

_DOTTED = re.compile(r"[^\W\d]\w*(?:\.[^\W\d]\w*)*")

#: How a file's imports are read. ``absolute_import`` states it; Python 2
#: syntax evidences it; anything else leaves it open.
ABSOLUTE, PYTHON2, UNKNOWN = "absolute", "python2", "unknown"

#: Nodes whose identifiers a ``for``/``with``/``except`` target binds. An
#: attribute or subscript target binds no name, which is why the descent stops
#: at anything not listed.
_TARGET_PATTERNS = frozenset({
    "pattern_list", "tuple_pattern", "list_pattern", "tuple", "list",
    "parenthesized_expression", "list_splat_pattern", "as_pattern_target",
})


def _target_names(node: Node) -> set[str]:
    if node.type == "identifier":
        return {node_text(node)}
    if node.type in _TARGET_PATTERNS:
        return {n for child in node.named_children for n in _target_names(child)}
    return set()


def _python2_syntax(root: Node) -> bool:
    """Whether the tree holds a statement only Python 2 has."""
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in ("print_statement", "exec_statement"):
            return True
        if node.type == "except_clause" and len(node.children_by_field_name("value")) > 1:
            return True
        stack.extend(node.named_children)
    return False


def _literal_all(root: Node) -> frozenset[str] | None:
    """A module-level ``__all__`` stated once, as a list or tuple of strings."""
    found: list[frozenset[str] | None] = []
    stack = list(root.named_children)
    while stack:
        node = stack.pop()
        if node.type in ("function_definition", "class_definition"):
            continue
        if node.type in ("assignment", "augmented_assignment"):
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is not None and node_text(left) == "__all__":
                strings = right.named_children if right is not None and right.type in ("list", "tuple") else None
                if node.type == "assignment" and strings is not None and all(c.type == "string" for c in strings):
                    found.append(frozenset(_string(c) for c in strings))
                else:
                    found.append(None)
                continue
        stack.extend(node.named_children)
    if len(found) != 1:
        return None
    # ``__all__.extend(...)``, ``.append``, a slice assignment: any mention
    # beyond the one assignment may change what it lists.
    mentions, stack = 0, [root]
    while stack:
        node = stack.pop()
        mentions += node.type == "identifier" and node_text(node) == "__all__"
        stack.extend(node.named_children)
    return found[0] if mentions == 1 else None


def _string(node: Node) -> str:
    return "".join(node_text(c) for c in node.named_children if c.type == "string_content")


def _other_bindings(root: Node, python2: bool = False) -> set[str]:
    """Module-level names bound by anything but ``class``, ``def`` and ``=``.

    Those three are what the declaration walker records; these are the forms
    it does not, any of which may rebind a base's name. ``global`` is read
    inside functions too, since that is the one way a function binds a
    module-level name.
    """
    found: set[str] = set()
    stack = [(root, False)]
    while stack:
        node, in_scope = stack.pop()
        kind = node.type
        if kind == "global_statement":
            found.update(node_text(c) for c in node.named_children if c.type == "identifier")
        elif not in_scope:
            if kind == "for_statement" and (left := node.child_by_field_name("left")):
                found |= _target_names(left)
            elif kind == "as_pattern_target":
                found |= _target_names(node)
            elif kind == "except_clause" and len(values := node.children_by_field_name("value")) > 1:
                found |= _target_names(values[1])
            elif kind == "named_expression" and (name := node.child_by_field_name("name")):
                found |= _target_names(name)
            elif kind == "augmented_assignment" and (left := node.child_by_field_name("left")):
                found |= _target_names(left)
            elif python2 and kind == "for_in_clause" and (left := node.child_by_field_name("left")):
                # A Python 2 list comprehension leaks its variable into the
                # enclosing scope; a Python 3 one does not.
                found |= _target_names(left)
        inside = in_scope or kind in ("function_definition", "class_definition", "lambda")
        stack.extend((child, inside) for child in node.named_children)
    return found


class PythonResolver:
    """Edges for one file, resolved against one checkout."""

    def __init__(self, inventory: Inventory):
        self.inventory = inventory
        self.modules = inventory.python_modules
        self.directories = inventory.python_directories
        self._init_names: dict[str, set[str]] = {}
        self._importer = ""
        self._all: dict[str, frozenset[str] | None] = {}
        #: Directories holding a ``conftest.py``, which pytest's default
        #: import mode prepends to ``sys.path``.
        self._conftest_dirs = sorted(
            posixpath.dirname(p) for p in inventory.paths if posixpath.basename(p) == "conftest.py")

    # ----------------------------------------------------------------- modules

    def _hit(self, name: str) -> frozenset[str]:
        """Files ``name`` names through a root that applies to the current file."""
        return frozenset(
            path for path, root in self.modules.get(name, {}).items()
            if root in PYTHON_ROOTS or self._importer.startswith(root)
        )

    def _elsewhere(self, name: str) -> list[str]:
        """Files ``name`` names only through a discovered root the file is not under."""
        return sorted(
            f"{path} (root {root})" for path, root in self.modules.get(name, {}).items()
            if root not in PYTHON_ROOTS and not self._importer.startswith(root)
        )

    def _landed(self, paths: frozenset[str], name: str) -> Resolution:
        if len(paths) == 1:
            return Resolution(INTERNAL, dotted_key(module_key(next(iter(paths)))))
        return unresolved(f"{name} matches {len(paths)} files: {', '.join(sorted(paths))}")

    def _absent(self, name: str) -> Resolution:
        """A name no file answers: unresolved if any of it is in the checkout."""
        if name in self.directories:
            return unresolved(f"{name} is a directory with no __init__ (a namespace package)")
        parts = name.split(".")
        for i in range(len(parts) - 1, 0, -1):
            prefix = ".".join(parts[:i])
            if prefix in self.modules or prefix in self.directories:
                return unresolved(f"{prefix} is in the checkout but no file is {name}")
        if where := self._elsewhere(name):
            return unresolved(
                f"{name} is in the checkout at {', '.join(where)}, which this file is not beneath, "
                f"so whether it is on the path is not in the source"
            )
        # pytest prepends a ``conftest.py``'s directory, so ``import helpers``
        # anywhere under ``tests/`` may be ``tests/helpers.py``. Whether pytest
        # runs it, and in which import mode, is not in the source.
        written = name.replace(".", "/")
        if found := [f"{d}/{written}" for d in self._conftest_dirs if self._importer.startswith(f"{d}/") and any(
                f"{d}/{written}{tail}" in self.inventory.paths for tail in (".py", "/__init__.py"))]:
            return unresolved(f"{name} is {found[0]} if pytest puts {found[0].rpartition('/')[0]}/ on the path")
        return Resolution(EXTERNAL, dotted_key(name))

    def _found(self, name: str) -> Resolution:
        hits = self._hit(name)
        return self._landed(hits, name) if hits else self._absent(name)

    def absolute(self, name: str, importer: str, mode: str) -> tuple[Resolution, str | None]:
        """A level-0 module name, and the dotted name it turned out to mean."""
        package = PurePosixPath(importer).parent.as_posix()
        if mode != ABSOLUTE and package not in ("", "."):
            dotted_package = package.replace("/", ".")
            sibling_top = f"{dotted_package}.{name.split('.', 1)[0]}"
            # A real package, not a bare directory: Python 2's implicit
            # relative import does not reach a namespace directory.
            if self._hit(sibling_top):
                if mode == PYTHON2:
                    sibling = f"{dotted_package}.{name}"
                    return self._found(sibling), sibling
                return unresolved(
                    f"{name} may be a Python 2 implicit relative import of {sibling_top}: the "
                    f"file parses as Python 3, shows no Python 2 syntax, and does not import "
                    f"absolute_import"
                ), None
        return self._found(name), name

    def relative(self, written: str, importer: str) -> tuple[Resolution, str | None]:
        """A relative name, and the absolute name it stands for, case kept.

        :func:`provenance.declarations.resolve_relative` computes the same
        thing lowercased, for comparing against folded entity titles; an import
        lookup has to keep case, so this does not call it.
        """
        level = len(written) - len(written.lstrip("."))
        package = PurePosixPath(importer).parent.as_posix()
        parts = [] if package in ("", ".") else package.split("/")
        if level - 1 >= len(parts):
            return unresolved(f"{written} climbs past the checkout root"), None
        remainder = [p for p in written[level:].split(".") if p]
        name = ".".join([*parts[: len(parts) - (level - 1)], *remainder])
        return self._found(name), name

    def _init_binds(self, package: str, name: str) -> bool:
        """Whether ``package``'s ``__init__`` binds ``name`` at module level."""
        paths = self._hit(package)
        if len(paths) != 1:
            return False
        path = next(iter(paths))
        if PurePosixPath(path).stem != "__init__":
            return False
        if path not in self._init_names:
            source = self.inventory.read(path) or ""
            names = {n for _kind, n in symbols_in_tree(source, PYTHON) if "." not in n}
            captures, _ = import_captures_in(source, PYTHON)
            for capture in captures:
                # ``from . import x`` binds the submodule itself, which is the
                # edge, not something that hides it; so do ``from pkg import
                # x`` and ``import pkg.x as x`` spelled from inside ``pkg``.
                if capture.scoped or capture.written in (".", package):
                    continue
                names |= {alias or n for n, alias in capture.names if n != "*"}
                if not capture.names and capture.written != f"{package}.{capture.alias}":
                    names.add(capture.alias or capture.written.split(".", 1)[0])
            self._init_names[path] = names
        return name in self._init_names[path]

    def _dunder_all(self, module: str) -> frozenset[str] | None:
        """The names a module's ``__all__`` lists, when it states them once as a literal.

        ``None`` when the module is not one file, has no ``__all__``, assigns it
        more than once, extends it, or builds it from anything but string
        literals. Each of those leaves the star's bindings outside the syntax.
        """
        paths = self._hit(module)
        if len(paths) != 1:
            return None
        path = next(iter(paths))
        if path not in self._all:
            source = self.inventory.read(path)
            self._all[path] = None if source is None else _literal_all(parse(source, PYTHON).root_node)
        return self._all[path]

    # ------------------------------------------------------------------- edges

    def edges(self, path: str, source: str) -> tuple[set[Edge], bool]:
        """Every IMPORTS and INHERITS edge one file states, and whether ``ast`` read it."""
        self._importer = path
        imports, used_ast = import_captures_in(source, PYTHON)
        bases, _ = base_captures_in(source, PYTHON)
        tree = parse(source, PYTHON).root_node
        file_key = dotted_key(module_key(path))
        if any(c.written == "__future__" and any(n == "absolute_import" for n, _ in c.names)
               for c in imports):
            mode = ABSOLUTE
        elif not used_ast and _python2_syntax(tree):
            mode = PYTHON2
        else:
            mode = UNKNOWN

        edges: set[Edge] = set()
        bound: dict[str, list[tuple[Resolution, str | None, str]]] = {}
        stars: list[tuple[str, Resolution, str | None]] = []
        for capture in sorted(imports, key=_order):
            resolved, name = self._import(capture, path, mode, file_key, edges)
            if capture.scoped:
                continue
            if not capture.names:
                # ``import a.b`` binds ``a``; ``import a.b as x`` binds ``x``.
                if capture.alias:
                    bound.setdefault(capture.alias, []).append((resolved, name, ""))
                else:
                    top, top_name = self.absolute(capture.written.split(".", 1)[0], path, mode)
                    bound.setdefault(capture.written.split(".", 1)[0], []).append((top, top_name, ""))
                continue
            for imported, alias in capture.names:
                if imported == "*":
                    stars.append((capture.written, resolved, name))
                else:
                    bound.setdefault(alias or imported, []).append((resolved, name, imported))

        if bases:
            namespace = namespace_for(path, source, PYTHON)
            symbols: dict[str, set[str]] = {}
            for kind, name in symbols_in_tree(source, PYTHON):
                symbols.setdefault(name, set()).add(kind)
            other = _other_bindings(tree, python2=mode == PYTHON2)
            for capture in bases:
                owner = f"{namespace}.{capture.owner}" if namespace else capture.owner
                resolved = self._base(capture.owner, capture.written, namespace, symbols,
                                      other, bound, stars)
                edges.add(resolved.edge(INHERITS, dotted_key(owner), capture.written, path))
        return edges, used_ast

    def _import(self, capture: ImportCapture, path, mode, file_key, edges) -> tuple[Resolution, str | None]:
        if capture.written.startswith("."):
            resolved, name = self.relative(capture.written, path)
        else:
            resolved, name = self.absolute(capture.written, path, mode)
        edges.add(resolved.edge(IMPORTS, file_key, capture.written, path))
        if name is None or resolved.target_class == EXTERNAL:
            return resolved, name
        for imported, _alias in capture.names:
            if imported == "*" or self._init_binds(name, imported):
                continue
            if sub := self._hit(f"{name}.{imported}"):
                joiner = "" if capture.written.endswith(".") else "."
                edges.add(self._landed(sub, f"{name}.{imported}").edge(
                    IMPORTS, file_key, f"{capture.written}{joiner}{imported}", path))
        return resolved, name

    def _base(self, owner, written, namespace, symbols, other, bound, stars) -> Resolution:
        if not _DOTTED.fullmatch(written):
            return unresolved("base is an expression, not a name")
        head, _, rest = written.partition(".")

        # Bases are evaluated in the scope enclosing the class statement: the
        # enclosing class body first, which shadows everything at module level
        # including a star import, and then the module.
        enclosing = owner.rpartition(".")[0]
        if enclosing and f"{enclosing}.{head}" in symbols:
            return self._local(f"{enclosing}.{head}", head, rest, namespace, symbols)

        starred: list[Resolution] = []
        for star_written, resolved, name in stars:
            listed = self._dunder_all(name) if resolved.target_class == INTERNAL and name else None
            if listed is None:
                return unresolved(
                    f"the file star-imports {star_written}, which states no literal __all__ "
                    f"this checkout holds, so it may rebind {head}"
                )
            if head in listed:
                starred.append(self._through(resolved, name, head, rest))
        if starred:
            if head in symbols or head in bound or head in other or len(set(starred)) > 1:
                return unresolved(f"{head} is bound by a star import and again, and order is not tracked")
            return starred[0]

        if head in symbols:
            if head in bound or head in other:
                return unresolved(f"{head} is bound both in this file and by an import or another statement")
            return self._local(head, head, rest, namespace, symbols)

        if head in other:
            return unresolved(f"{head} is bound by a for, with, except or global statement")
        if head in bound:
            found = {self._through(resolved, name, imported, rest) for resolved, name, imported in bound[head]}
            if len(found) > 1:
                return unresolved(f"{head} is bound by {len(bound[head])} imports to different targets")
            return found.pop()
        if head in PYTHON2_BUILTINS or hasattr(builtins, head):
            return Resolution(EXTERNAL, dotted_key(written))
        return unresolved(f"{head} is not bound in this file")

    def _local(self, local: str, head: str, rest: str, namespace: str, symbols) -> Resolution:
        if symbols[local] != {CLASS}:
            kinds = " and a ".join(sorted(symbols[local] - {CLASS})) or "class"
            return unresolved(f"{head} is a {kinds} here, and what it holds is not syntax")
        return Resolution(INTERNAL, dotted_key(".".join(p for p in (namespace, local, rest) if p)))

    def _through(self, resolved: Resolution, name: str | None, imported: str, rest: str) -> Resolution:
        """The base reached through one import binding."""
        if resolved.target_class == UNRESOLVED:
            return resolved
        if imported:
            tail = ".".join(dotted_key(p) for p in (imported, rest) if p)
            return Resolution(resolved.target_class, f"{resolved.key}.{tail}")
        if resolved.target_class == EXTERNAL or not rest or name is None:
            return Resolution(resolved.target_class, ".".join(p for p in (resolved.key, dotted_key(rest)) if p))
        # ``import pkg`` then ``pkg.sub.Class``: the longest file wins.
        parts = rest.split(".")
        for i in range(len(parts) - 1, 0, -1):
            candidate = ".".join([name, *parts[:i]])
            if hits := self._hit(candidate):
                landed = self._landed(hits, candidate)
                if landed.target_class != INTERNAL:
                    return landed
                return Resolution(INTERNAL, ".".join([landed.key, *map(dotted_key, parts[i:])]))
        return Resolution(INTERNAL, ".".join([resolved.key, dotted_key(rest)]))


def _order(capture: ImportCapture) -> tuple:
    """Captures arrive as a set; resolve them in a fixed order."""
    return (capture.written, capture.alias or "", capture.names, capture.scoped)
