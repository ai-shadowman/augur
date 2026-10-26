"""Ruby: resolving tree-sitter's captures into edges, with no oracle.

There is no second Ruby reader, so ``tests/test_provenance_edges_ruby.py`` is
constructed cases and a regression pin on jekyll, and says so.

**Loads.** ``require_relative`` names a file beside the requiring one.
``require`` and ``autoload`` name a path on the load path, which the source
does not state: a gem's own ``lib/`` is on it (the checkout root's, or one
beside a ``.gemspec``), so a path one such ``lib/`` holds is internal and one
two hold is ambiguous. ``require "./x"`` and ``load`` resolve against the
current directory, which the source does not state either, so a path the
checkout holds is unresolved for them.
A path the checkout holds somewhere else, with any suffix (``test/helper.rb``,
put on the load path by ``rake test``'s ``-Itest``; ``ext/x/x.c``, a C
extension it builds), is unresolved rather than external,
because whether its directory is on the load path is exactly what the source
does not say. Anything the checkout does not hold is external: a gem or the
standard library. A computed path is unresolved.

**Superclasses.** ``class Link < Liquid::Tag`` is evaluated where the class
statement sits, and Ruby looks a constant up lexically: each enclosing
``module``/``class`` innermost first (``Module.nesting``), then the
innermost scope's ancestors, then the top level. The checkout holds only its
own part of each of those constant tables: inside a reopened gem namespace
(``module Kramdown``) a bare name the gem defines is invisible here. So a bare
name looked up from a nested scope that falls through to the top level is
unresolved unless it is core, and so is a top-level one wherever the checkout
``include``s a module at the top level, which puts that module's constants
there too. A namespace-qualified head (``Liquid::Tag`` inside ``module
Jekyll``) is still taken to name the top-level namespace, except where the
innermost scope has ancestors of its own (a superclass that is not core, or an
``include``/``prepend``) and the checkout declares some other ``X::Liquid``
those ancestors may hold: ``class Child < Parent; class Inner < Base::Thing``
finds ``Parent::Base``. That case is unresolved, not walked. A plugin defining
``Jekyll::Liquid`` from outside the checkout would still make the top-level
reading wrong, and the code does not try to rule it out. A class declared only outside every load-path ``lib/`` does not
count for code inside one, since lib code does not load benchmarks or
fixtures. A name found as a constant assignment (``Error = Class.new``) is
unresolved, because its value is not syntax. A name the checkout never
declares is external, and so is a core class even where the checkout reopens
it (``class Hash; ...; end``): reopening adds methods and does not move the
class. Core is read from ruby 2.6.10p210 with ``--disable-gems``.

**Not resolved.** ``include`` and ``extend`` are method calls, not
inheritance syntax, and are a named gap. A superclass written as an expression
(``Struct.new(:a)``, ``DelegateClass(Foo)``) is unresolved. Constants a
module gets from its own ancestors, and constants defined by metaprogramming
(``const_set``, ``Foo ||= ...``), are seen only as names that make a match
unresolved. ``require_paths`` in a gemspec is not read:
``lib/`` is assumed, which is the default.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import PurePosixPath

from ..corpus import dotted_key
from ..declarations import module_key
from ..dialects import RUBY
from ..facts import SKIP_DIRS
from ..treesitter import (
    CLASS,
    base_captures_in_tree,
    class_nodes_in_tree,
    import_captures_in_tree,
    node_text,
    parse,
    symbols_in_tree,
)
from .model import EXTERNAL, IMPORTS, INHERITS, INTERNAL, Edge, Inventory, Resolution, unresolved

#: Ruby's core classes and modules, from ``Object.constants`` in ruby 2.6.10p210
#: started with ``--disable-gems``, so nothing RubyGems loads is counted.
RUBY_CORE = frozenset({
    "ArgumentError", "Array", "BasicObject", "Bignum", "Binding", "Class",
    "ClosedQueueError", "Comparable", "Complex", "ConditionVariable", "Data",
    "Dir", "EOFError", "Encoding", "EncodingError", "Enumerable", "Enumerator",
    "Errno", "Exception", "FalseClass", "Fiber", "FiberError", "File",
    "FileTest", "Fixnum", "Float", "FloatDomainError", "FrozenError", "GC",
    "Hash", "IO", "IOError", "IndexError", "Integer", "Interrupt", "Kernel",
    "KeyError", "LoadError", "LocalJumpError", "Marshal", "MatchData", "Math",
    "Method", "Module", "Mutex", "NameError", "NilClass", "NoMemoryError",
    "NoMethodError", "NotImplementedError", "Numeric", "Object", "ObjectSpace",
    "Proc", "Process", "Queue", "Random", "Range", "RangeError", "Rational",
    "Regexp", "RegexpError", "RubyVM", "RuntimeError", "ScriptError",
    "SecurityError", "Signal", "SignalException", "SizedQueue",
    "StandardError", "StopIteration", "String", "Struct", "Symbol",
    "SyntaxError", "SystemCallError", "SystemExit", "SystemStackError",
    "Thread", "ThreadError", "ThreadGroup", "Time", "TracePoint", "TrueClass",
    "TypeError", "UnboundMethod", "UncaughtThrowError", "UnicodeNormalize",
    "Warning", "ZeroDivisionError",
})

#: What ``require "x"`` can load, including the C source of an extension the
#: checkout builds into ``x.so``.
_LOADABLE = (".rb", ".so", ".bundle", ".c", ".cc", ".cpp")

_CONSTANT_PATH = re.compile(r"(?:::)?[A-Z]\w*(?:::[A-Z]\w*)*")


#: Receiverless calls that put a module into a class's ancestors: the gap the
#: module docstring names under *Not resolved*.
MIXIN_CALLS = ("include", "extend", "prepend")

#: Nodes whose body is another scope, so a call inside one is not the
#: enclosing class's.
_SCOPES = ("class", "module", "method", "singleton_method", "singleton_class")


def mixin_names(source: str) -> set[str]:
    """The last segment of each constant an ``include``, ``extend`` or ``prepend`` names.

    Not edges: they are method calls, and which class's ancestors they change
    is not read here. The edge verdict uses them so that "``Foo`` INHERITS
    ``Mixin``" is not contradicted by an absence this resolver never looked for.
    """
    names: set[str] = set()
    stack = [parse(source, RUBY).root_node]
    while stack:
        node = stack.pop()
        if node.type == "call" and node.child_by_field_name("receiver") is None:
            method, arguments = node.child_by_field_name("method"), node.child_by_field_name("arguments")
            if method is not None and arguments is not None and node_text(method) in MIXIN_CALLS:
                names.update(node_text(a).rpartition("::")[2] for a in arguments.named_children
                             if _CONSTANT_PATH.fullmatch(node_text(a)))
        stack.extend(node.named_children)
    return names


def _has_ancestors(node) -> bool:
    """Whether constant lookup inside this class/module reaches anything before ``Object``.

    A superclass that is not core, or an ``include``/``prepend`` in the body
    (``extend`` changes the singleton class, which lookup does not consult).
    """
    if any(written.removeprefix("::") not in RUBY_CORE for written, _ in RUBY.base_names(node)):
        return True
    body = node.child_by_field_name("body")
    stack = list(body.named_children) if body is not None else []
    while stack:
        child = stack.pop()
        if child.type == "call" and child.child_by_field_name("receiver") is None:
            method = child.child_by_field_name("method")
            if method is not None and node_text(method) in ("include", "prepend"):
                return True
        if child.type not in _SCOPES:
            stack.extend(child.named_children)
    return False


def _norm(path: str) -> str | None:
    normal = posixpath.normpath(path)
    if normal == ".." or normal.startswith(("../", "/")):
        return None
    return "" if normal == "." else normal


class RubyResolver:
    """Edges for one Ruby file, resolved against one checkout."""

    def __init__(self, inventory: Inventory):
        if inventory.root is None:
            raise ValueError("Ruby resolution reads every file's declarations; the inventory needs a root")
        self.paths = inventory.paths
        gem_dirs = {posixpath.dirname(p) for p in self.paths if p.endswith(".gemspec")}
        self.libs = sorted({
            "/".join(parts[: parts.index("lib") + 1])
            for parts in {tuple(PurePosixPath(p).parent.parts) for p in self.paths}
            if "lib" in parts
            and not any(part in SKIP_DIRS or part == "vendor" for part in parts[: parts.index("lib")])
            and ("/".join(parts[: parts.index("lib")]) in gem_dirs or parts.index("lib") == 0)
        })
        #: Constant path (``Jekyll::Tags::Link``) the checkout declares by
        #: ``class``/``module``, and by assignment.
        #: Constant path to the files declaring it by ``class``/``module``.
        self.classes: dict[str, set[str]] = {}
        self.assigned: set[str] = set()
        #: Prefixes a compact ``class A::B::C`` names without declaring here.
        self.namespaces: set[str] = set()
        #: Names bound where the walker does not look: ``Foo ||=`` and
        #: ``const_set(:Foo, ...)``. A match on one is never settled.
        self.dynamic: set[str] = set()
        #: Where a top-level ``include`` sits: ``lib`` if in a file under a
        #: load-path ``lib/``, which every program loading the library sees,
        #: otherwise the file's top directory (``test``), whose programs are
        #: the ones that plausibly load it. ``test/helper.rb``'s ``include
        #: Jekyll`` reaches the tests and not ``benchmark/``.
        self.top_level_include: set[str] = set()
        #: Classes and modules some declaration gives ancestors beyond ``Object``.
        self.ancestry: set[str] = set()
        self.redefines_loader: set[str] = set()
        for path in sorted(self.paths):
            if not path.lower().endswith(RUBY.suffixes):
                continue
            source = inventory.read(path)
            if source is None:
                continue
            for kind, name in symbols_in_tree(source, RUBY):
                constant = name.replace(".", "::")
                if kind == CLASS:
                    self.classes.setdefault(constant, set()).add(path)
                    parts = constant.split("::")
                    self.namespaces.update("::".join(parts[:i]) for i in range(1, len(parts)))
                elif constant.rpartition("::")[2][:1].isupper():
                    self.assigned.add(constant)
                elif name.rpartition(".")[2] in ("load", "require"):
                    self.redefines_loader.add(path)
            self._scan_dynamic(parse(source, RUBY).root_node, self._include_scope(path))
            self.ancestry.update(
                name.replace(".", "::") for name, node in class_nodes_in_tree(source, RUBY) if _has_ancestors(node)
            )
        #: Last segment to every nested constant ending in it.
        self.nested: dict[str, set[str]] = {}
        for constant in (*self.classes, *self.assigned, *self.namespaces):
            if "::" in constant:
                self.nested.setdefault(constant.rpartition("::")[2], set()).add(constant)

    def _in_lib(self, path: str) -> bool:
        return any(path.startswith(f"{lib}/") for lib in self.libs)

    def _include_scope(self, path: str) -> str:
        return "lib" if self._in_lib(path) else path.split("/", 1)[0]

    def _scan_dynamic(self, root, scope: str) -> None:
        stack = [(root, True)]
        while stack:
            node, top = stack.pop()
            if node.type == "operator_assignment":
                left = node.child_by_field_name("left")
                if left is not None and node_text(left)[:1].isupper():
                    self.dynamic.add(node_text(left).rpartition("::")[2])
            elif node.type == "call":
                method = node.child_by_field_name("method")
                name = node_text(method) if method is not None else ""
                arguments = node.child_by_field_name("arguments")
                first = arguments.named_children[0] if arguments is not None and arguments.named_children else None
                if name == "const_set" and first is not None:
                    self.dynamic.add(node_text(first).lstrip(":").strip("\"'"))
                elif name == "include" and top and node.child_by_field_name("receiver") is None:
                    self.top_level_include.add(scope)
            inside = top and node.type not in ("class", "module", "method", "singleton_method", "singleton_class")
            stack.extend((child, inside) for child in node.named_children)

    # ---------------------------------------------------------------- loads

    def _load(self, written: str, loader: str, importer: str) -> Resolution:
        if loader == "load" and importer in self.redefines_loader:
            return unresolved(f"{importer} defines its own load, so this call may not load anything")
        file = written if written.endswith(".rb") or loader == "load" else f"{written}.rb"
        if loader == "require_relative":
            target = _norm(posixpath.join(posixpath.dirname(importer), file))
            if target in self.paths:
                return Resolution(INTERNAL, dotted_key(module_key(target)))
            return unresolved(f"{written} is relative and names no file in the checkout")
        current_directory = loader == "load" or written.startswith(("./", "../"))
        hits = [] if current_directory else [f"{lib}/{file}" for lib in self.libs if f"{lib}/{file}" in self.paths]
        if len(hits) == 1:
            return Resolution(INTERNAL, dotted_key(module_key(hits[0])))
        if hits:
            return unresolved(f"{written} is under {len(hits)} lib directories: {', '.join(hits)}")
        stem = posixpath.normpath(written).removesuffix(".rb")
        stem = stem[2:] if stem.startswith("./") else stem
        names = (stem,) if loader == "load" else tuple(f"{stem}{s}" for s in _LOADABLE)
        elsewhere = sorted(p for p in self.paths if any(p == n or p.endswith(f"/{n}") for n in names))
        if elsewhere:
            return unresolved(
                f"{written} is in the checkout at {', '.join(elsewhere[:3])}, and whether that is where "
                f"{loader} looks is not in the source"
            )
        if current_directory:
            return unresolved(f"{written} is resolved against the current directory, which the source does not state")
        return Resolution(EXTERNAL, dotted_key(written.removesuffix(".rb")))

    # ---------------------------------------------------------- constants

    def _counts(self, constant: str, importer: str) -> bool:
        """Whether a declaration of ``constant`` is one ``importer`` would load."""
        paths = self.classes.get(constant, set())
        if not self._in_lib(importer):
            return bool(paths)
        return any(self._in_lib(p) for p in paths)

    def _constant(self, owner: str, written: str, importer: str) -> Resolution:
        if not _CONSTANT_PATH.fullmatch(written):
            return unresolved("superclass is an expression, not a constant")
        absolute = written.startswith(("::", "Object::"))
        name = written.removeprefix("::").removeprefix("Object::")
        head, _, rest = name.partition("::")
        if head in self.dynamic:
            return unresolved(f"{head} is bound by ||= or const_set somewhere, which is not syntax")
        levels = owner.split(".")[:-1]
        crefs = [] if absolute else ["::".join(levels[:i]) for i in range(len(levels), 0, -1)]
        for cref in crefs:
            base = f"{cref}::{head}"
            if base in self.classes or base in self.assigned or base in self.namespaces:
                return self._member(base, rest, importer)
        # The innermost scope's ancestors come before the top level.
        if rest and crefs and crefs[0] in self.ancestry and (namesakes := self.nested.get(head)):
            return unresolved(
                f"{crefs[0]} has ancestors searched before the top level, and the checkout declares "
                f"{', '.join(sorted(namesakes)[:3])}"
            )
        # The top level. Core first: a reopened core class is still core.
        if head in RUBY_CORE and not rest:
            return Resolution(EXTERNAL, dotted_key(head))
        if not rest and crefs:
            return unresolved(f"{head} is looked up from a nested scope whose gem-defined constants are invisible here")
        if head in self.classes or head in self.assigned or head in self.namespaces:
            return self._member(head, rest, importer)
        # Object's own constants come before the modules it includes, so an
        # include matters only for a name the top level does not declare.
        included = "lib" in self.top_level_include or (
            not self._in_lib(importer) and self._include_scope(importer) in self.top_level_include
        )
        if included:
            return unresolved(f"{head} is not declared at the top level, where the checkout includes a module")
        return Resolution(EXTERNAL, dotted_key(name))

    def _member(self, base: str, rest: str, importer: str) -> Resolution:
        full = f"{base}::{rest}" if rest else base
        if full in self.classes:
            if not self._counts(full, importer):
                where = ", ".join(sorted(self.classes[full])[:3])
                return unresolved(f"{full} is declared only in {where}, which {importer} does not load")
            return Resolution(INTERNAL, dotted_key(full))
        if full in self.assigned:
            return unresolved(f"{full} is assigned, and what it holds is not syntax")
        return unresolved(f"{base} is declared here and {full} is not; it may be defined elsewhere")

    # ---------------------------------------------------------------- edges

    def edges(self, path: str, source: str) -> tuple[set[Edge], bool]:
        """Every IMPORTS and INHERITS edge one file states. Never read by ``ast``."""
        file_key = dotted_key(module_key(path))
        edges: set[Edge] = set()
        for capture in import_captures_in_tree(source, RUBY):
            if capture.literal:
                resolved = self._load(capture.written, capture.loader or "require", path)
            else:
                resolved = unresolved(f"the path is computed: {capture.written}")
            edges.add(resolved.edge(IMPORTS, file_key, capture.written, path))
        for capture in base_captures_in_tree(source, RUBY):
            resolved = self._constant(capture.owner, capture.written, path)
            edges.add(resolved.edge(INHERITS, dotted_key(capture.owner), capture.written, path,
                                    detail=capture.detail))
        return edges, False
