"""TypeScript: resolving tree-sitter's captures into edges, with no oracle.

There is no second TypeScript reader, so ``tests/test_provenance_edges_typescript.py``
is constructed cases and a regression pin on nest, and says so.

**Modules** resolve by :mod:`provenance.edges.typescript_modules`, the
compiler's order, which this subclasses. What is left here is what a name
inside a module refers to.

**Names resolve through re-exports.** ``import { Injectable } from
'@nestjs/common'`` lands on a barrel, ``packages/common/index.ts``, which
declares nothing. Following ``export … from`` and ``export *`` is syntax, and
without it nearly every internal base target would be a barrel rather than a
class node. A name two ``export *`` sources both supply is unresolved, as is
one an external ``export *`` might supply.

**Bases.** A base is a name the file declares as a class, a name an import
binds, or a JavaScript global constructor (read from Node v22.21.0's
``globalThis``; for a type-only lib name such as ``Iterable`` there is no
equivalent, so it is unresolved). ``extends mixin(Base)`` is an expression
and unresolved. A name bound to a value (``const Base = mixin(X)``) is
unresolved for the reason it is in Python. A class expression bound to a
name (``const T = class extends Model {}``) has its bases resolved as a class
declaration's are, with the binding as the owner.

**Scope is the module's top level.** Declarations, export flags and bindings
are read off the root statements, not the symbol walk, which flattens
namespaces, ``declare module`` and blocks into the top level. A local name
blocks an ``export *`` only when it is itself exported, as ES modules say.

**Not modelled:** ``declare global`` and other ambient declarations
(an ambient class has no node, so a base naming one is unresolved), and
``.tsx``, which no dialect reads.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from ..corpus import dotted_key
from ..declarations import module_key
from ..dialects import TYPESCRIPT
from ..treesitter import (
    CLASS,
    FUNCTION,
    base_captures_in_tree,
    import_captures_in_tree,
    node_text,
    parse,
)
from .model import EXTERNAL, IMPORTS, INHERITS, INTERNAL, Edge, Inventory, Resolution, unresolved
from .typescript_modules import ModuleResolver, jsonc

__all__ = ["JS_GLOBALS", "TypeScriptResolver", "jsonc"]

#: Global constructors a class may extend, from ``globalThis`` in Node v22.21.0.
JS_GLOBALS = frozenset({
    "AbortController", "AbortSignal", "AggregateError", "Array", "ArrayBuffer",
    "BigInt", "BigInt64Array", "BigUint64Array", "Blob", "Boolean",
    "BroadcastChannel", "Buffer", "ByteLengthQueuingStrategy",
    "CompressionStream", "CountQueuingStrategy", "Crypto", "CryptoKey",
    "CustomEvent", "DOMException", "DataView", "Date", "DecompressionStream",
    "Error", "EvalError", "Event", "EventTarget", "File",
    "FinalizationRegistry", "Float32Array", "Float64Array", "FormData",
    "Function", "Headers", "Int16Array", "Int32Array", "Int8Array", "Iterator",
    "Map", "MessageChannel", "MessageEvent", "MessagePort", "Navigator",
    "Number", "Object", "Performance", "PerformanceEntry", "PerformanceMark",
    "PerformanceMeasure", "PerformanceObserver",
    "PerformanceObserverEntryList", "PerformanceResourceTiming", "Promise",
    "Proxy", "RangeError", "ReadableByteStreamController", "ReadableStream",
    "ReadableStreamBYOBReader", "ReadableStreamBYOBRequest",
    "ReadableStreamDefaultController", "ReadableStreamDefaultReader",
    "ReferenceError", "RegExp", "Request", "Response", "Set",
    "SharedArrayBuffer", "String", "SubtleCrypto", "Symbol", "SyntaxError",
    "TextDecoder", "TextDecoderStream", "TextEncoder", "TextEncoderStream",
    "TransformStream", "TransformStreamDefaultController", "TypeError",
    "URIError", "URL", "URLSearchParams", "Uint16Array", "Uint32Array",
    "Uint8Array", "Uint8ClampedArray", "WeakMap", "WeakRef", "WeakSet",
    "WebSocket", "WritableStream", "WritableStreamDefaultController",
    "WritableStreamDefaultWriter",
})

_DOTTED = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")
_TYPE_ARGUMENTS = re.compile(r"<[^<>]*>")

def _assigned_classes(root) -> Iterator[tuple[str, str, str | None]]:
    """``(owner, base as written, relation)`` for each ``const T = class extends Base {}``.

    The class is real and has a base, but it is a value bound to a name, so
    the symbol walk records a binding and no class, and without an edge a
    true claim about it reads as contradicted. A function body is not entered.
    """
    stack = [(root, ())]
    while stack:
        node, prefix = stack.pop()
        for child in node.named_children:
            if child.type in TYPESCRIPT.functions:
                continue
            value = child.child_by_field_name("value") if child.type in _VALUE_HOLDERS else None
            name = child.child_by_field_name("name")
            if value is not None and value.type == "class" and name is not None:
                owner = (*prefix, node_text(name))
                for written, relation in TYPESCRIPT.base_names(value):
                    yield ".".join(owner), written, relation
                stack.append((value, owner))
            elif child.type in TYPESCRIPT.classes and name is not None:
                stack.append((child, (*prefix, node_text(name))))
            else:
                stack.append((child, prefix))


#: Nodes that bind a name to a value: ``const T = ...`` and a class field.
_VALUE_HOLDERS = ("variable_declarator", "public_field_definition")


@dataclass
class _Module:
    """What one file declares, binds and passes on."""

    #: Top-level name to its kinds: ``class``, ``binding``, ``function``, ``ambient``.
    declared: dict[str, set[str]] = field(default_factory=dict)
    #: Top-level declarations carrying ``export``.
    public: set[str] = field(default_factory=set)
    #: Local name to ``(specifier, imported name)``; ``*`` for a namespace,
    #: ``None`` where two statements bind it or an ``import X = A.B`` alias does.
    bound: dict[str, tuple[str, str] | None] = field(default_factory=dict)
    #: Exported name to the local name it exports, from ``export { a as b }``
    #: and ``export default``.
    exported: dict[str, str | None] = field(default_factory=dict)
    #: Exported name to ``(specifier, imported name)``, from ``export … from``.
    passed: dict[str, tuple[str, str]] = field(default_factory=dict)
    stars: list[str] = field(default_factory=list)

class TypeScriptResolver(ModuleResolver):
    """Edges for one TypeScript file, resolved against one checkout."""

    def __init__(self, inventory: Inventory):
        super().__init__(inventory)
        self._modules: dict[str, _Module] = {}

    # -------------------------------------------------------------- exports

    def _module(self, path: str) -> _Module:
        """Top-level declarations, exports and bindings, read off the root statements."""
        if path in self._modules:
            return self._modules[path]
        module = _Module()
        self._modules[path] = module
        source = self.inventory.read(path)
        if source is None:
            return module

        def bind(name: str, target: tuple[str, str] | None) -> None:
            if name in module.bound and module.bound[name] != target:
                target = None
            module.bound[name] = target

        def declare(node, exported: bool, default: bool) -> None:
            if node.type == "ambient_declaration":
                inner = node.named_children[0] if node.named_children else None
                if inner is not None and inner.type in TYPESCRIPT.classes and (name := inner.child_by_field_name("name")):
                    module.declared.setdefault(node_text(name), set()).add("ambient")
                    if exported:
                        module.public.add(node_text(name))
                return
            if node.type in TYPESCRIPT.classes:
                name = node.child_by_field_name("name")
                if name is None:
                    if default:
                        module.exported["default"] = None
                    return
                module.declared.setdefault(node_text(name), set()).add(CLASS)
            elif node.type in TYPESCRIPT.bindings:
                for kind, bound_name in TYPESCRIPT.bound_names(node):
                    module.declared.setdefault(bound_name, set()).add(kind)
                    if exported:
                        module.public.add(bound_name)
                return
            elif node.type in ("function_declaration", "generator_function_declaration"):
                name = node.child_by_field_name("name")
                if name is None:
                    return
                module.declared.setdefault(node_text(name), set()).add(FUNCTION)
            elif node.type == "import_alias":
                alias = next((c for c in node.named_children if c.type == "identifier"), None)
                if alias is not None:
                    bind(node_text(alias), None)
                return
            else:
                return
            if exported:
                module.public.add(node_text(name))
            if default:
                module.exported["default"] = node_text(name)

        for statement in parse(source, TYPESCRIPT).root_node.named_children:
            if statement.type == "import_statement":
                for capture in TYPESCRIPT.import_edges(statement):
                    for imported, alias in capture.names:
                        if alias or imported not in ("*", "default"):
                            bind(alias or imported, (capture.written, imported))
                continue
            if statement.type != "export_statement":
                declare(statement, exported=False, default=False)
                continue
            default = any(c.type == "default" for c in statement.children)
            if statement.child_by_field_name("source") is not None:
                for capture in TYPESCRIPT.import_edges(statement):
                    for imported, alias in capture.names:
                        if imported == "*" and alias is None:
                            module.stars.append(capture.written)
                        else:
                            module.passed[alias or imported] = (capture.written, imported)
                continue
            if (declaration := statement.child_by_field_name("declaration")) is not None:
                declare(declaration, exported=True, default=default)
            elif (value := statement.child_by_field_name("value")) is not None and default:
                if value.type == "identifier":
                    module.exported["default"] = node_text(value)
                elif value.type in TYPESCRIPT.classes:
                    declare(value, exported=False, default=True)
                else:
                    module.exported["default"] = None
            clause = next((c for c in statement.named_children if c.type == "export_clause"), None)
            for spec in clause.named_children if clause is not None else ():
                if spec.type == "export_specifier" and (name := spec.child_by_field_name("name")):
                    alias = spec.child_by_field_name("alias")
                    module.exported[node_text(alias) if alias is not None else node_text(name)] = node_text(name)
        return module

    def _local(self, path: str, name: str, seen: frozenset) -> Resolution:
        module = self._module(path)
        if name in module.declared and name in module.bound:
            return unresolved(f"{name} is declared and imported in {path}")
        if name in module.declared:
            if module.declared[name] == {CLASS}:
                return Resolution(INTERNAL, dotted_key(f"{module_key(path)}.{name}"))
            if "ambient" in module.declared[name]:
                return unresolved(f"{name} is declared ambiently in {path}, which leaves no class node")
            kinds = " and ".join(sorted(module.declared[name] - {CLASS}))
            return unresolved(f"{name} is a {kinds} in {path}, and what it holds is not syntax")
        if name in module.bound:
            if module.bound[name] is None:
                return unresolved(f"{name} is bound more than once, or by an alias, in {path}")
            spec, imported = module.bound[name]
            return self._through(spec, path, imported, seen)
        return unresolved(f"{name} is not declared or imported in {path}")

    def _through(self, spec: str, importer: str, imported: str, seen: frozenset) -> Resolution:
        resolved, path = self.resolve(spec, importer)
        if resolved.target_class == EXTERNAL:
            return Resolution(EXTERNAL, f"{resolved.key}.{dotted_key(imported)}")
        if path is None:
            return resolved
        if not path.endswith(TYPESCRIPT.suffixes):
            return unresolved(f"{spec} is {path}, which no TypeScript dialect reads")
        if imported == "*":
            return unresolved(f"a namespace of {path} is not a class")
        found = self._export(path, imported, seen)
        return found if found is not None else unresolved(f"{path} exports no {imported}")

    def _export(self, path: str, name: str, seen: frozenset = frozenset()) -> Resolution | None:
        """What ``path`` exports as ``name``, through re-exports; ``None`` if it does not."""
        if (path, name) in seen:
            return unresolved(f"{name} is re-exported in a cycle through {path}")
        seen = seen | {(path, name)}
        module = self._module(path)
        if name in module.exported:
            local = module.exported[name]
            return self._local(path, local, seen) if local else unresolved(f"the default export of {path} is anonymous")
        if name != "default" and name in module.public:
            return self._local(path, name, seen)
        if name in module.passed:
            spec, imported = module.passed[name]
            return self._through(spec, path, imported, seen)
        if name == "default":
            return None
        hits, uncertain = set(), None
        for spec in module.stars:
            _resolved, target = self.resolve(spec, path)
            if target is not None and target.endswith(TYPESCRIPT.suffixes):
                found = self._export(target, name, seen)
                if found is not None:
                    hits.add(found)
            else:
                uncertain = uncertain or spec
        if len(hits) > 1:
            return unresolved(f"{name} is supplied by {len(hits)} export * sources of {path}")
        if hits:
            return hits.pop()
        if uncertain:
            return unresolved(f"{name} may come from export * from {uncertain} in {path}")
        return None

    # ---------------------------------------------------------------- edges

    def edges(self, path: str, source: str) -> tuple[set[Edge], bool]:
        """Every IMPORTS and INHERITS edge one file states. Never read by ``ast``."""
        file_key = dotted_key(module_key(path))
        edges: set[Edge] = set()
        for capture in import_captures_in_tree(source, TYPESCRIPT):
            resolved, _ = self.resolve(capture.written, path)
            edges.add(resolved.edge(IMPORTS, file_key, capture.written, path))
        for capture in base_captures_in_tree(source, TYPESCRIPT):
            resolved = self._base(path, capture.written)
            owner = dotted_key(f"{module_key(path)}.{capture.owner}")
            edges.add(resolved.edge(INHERITS, owner, capture.written, path, detail=capture.detail))
        for owner, written, detail in _assigned_classes(parse(source, TYPESCRIPT).root_node):
            resolved = self._base(path, written)
            edges.add(resolved.edge(INHERITS, dotted_key(f"{module_key(path)}.{owner}"), written, path, detail=detail))
        return edges, False

    def _base(self, path: str, written: str) -> Resolution:
        name = written
        while (stripped := _TYPE_ARGUMENTS.sub("", name)) != name:
            name = stripped
        if not _DOTTED.fullmatch(name):
            return unresolved("base is an expression, not a name")
        head, _, rest = name.partition(".")
        module = self._module(path)
        if head in module.declared or head in module.bound:
            if not rest:
                return self._local(path, head, frozenset())
            if module.bound.get(head) and module.bound[head][1] == "*" and "." not in rest:
                return self._through(module.bound[head][0], path, rest, frozenset())
            return unresolved(f"{name} reaches through {head}, which is not a namespace import")
        if head in JS_GLOBALS and not rest:
            return Resolution(EXTERNAL, dotted_key(head))
        return unresolved(f"{head} is not declared or imported here, and is no JavaScript global constructor")
