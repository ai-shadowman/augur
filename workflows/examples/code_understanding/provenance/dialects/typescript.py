"""TypeScript, where a declaration can be a value, a type, or both.

``.ts`` only. ``.tsx`` and plain ``.js`` are deliberately unregistered: no
candidate checkout holds a ``.tsx`` file, and every ``.js`` in reach is
vendored third-party code inside a Python or Ruby repository. Registering a
dialect with nothing to measure it against is the thing
``symbols.sections_for``'s docstring refuses to do, and each is one table
entry away once a corpus exists.

Interfaces, enums and type aliases go in ``Classes:``, for the same reason
Java's interfaces do: the header has four buckets, that is the closest one,
and a model naming ``interface Shape`` as an entity should ground against
something.

Two shapes are worth knowing about. Overload declarations
(``function_signature``) repeat a name the implementation also declares, and
``_classify`` collapses them on the folded name with the first occurrence
winning. And ``export`` wraps the declaration it exports, which
:attr:`Dialect.unwrap` undoes, the same job ``decorated_definition`` does in
Python.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from ..treesitter import BINDING, FUNCTION, Dialect, ImportCapture, node_text, squeeze

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from tree_sitter import Node

#: ``import()`` and ``require()`` read the same way; the difference is only
#: which of them the module system resolves at load time.
_DYNAMIC = frozenset({"import", "require"})


def _string_value(node: Node | None) -> str | None:
    """The contents of a string literal, without its quotes.

    Read from the ``string_fragment`` child rather than by stripping quotes
    off the text, so an escaped quote inside the specifier cannot shift the
    result by a character.
    """
    if node is None or node.type != "string":
        return None
    fragment = next((c for c in node.named_children if c.type == "string_fragment"), None)
    return node_text(fragment) if fragment is not None else ""


def package_name(specifier: str) -> str | None:
    """The npm package a module specifier names, or ``None`` if it names none.

    ``rxjs/operators`` is the ``rxjs`` package and ``@nestjs/common/utils`` is
    ``@nestjs/common``: a scoped name keeps two segments and an unscoped one
    keeps one. That is npm's own rule for where a package name ends, not an
    inference from how the string looks.

    A relative or absolute specifier names a file in this repository rather
    than a dependency, which is the same distinction
    ``imports.imports_in_source`` draws with ``level`` on a Python relative
    import.
    """
    if not specifier or specifier.startswith((".", "/")):
        return None
    parts = specifier.split("/")
    if specifier.startswith("@"):
        return "/".join(parts[:2]) if len(parts) >= 2 else None
    return parts[0]


#: A value of one of these types makes the name it is bound to a callable.
_CALLABLE_VALUES = frozenset({"arrow_function", "function_expression", "generator_function"})


def _kind_of(holder: Node) -> str:
    """Whether a declarator or field binds a function or an ordinary value.

    ``export const makeId = () => ...`` declares a function by binding one,
    and TypeScript codebases declare a great many that way: 168 at module
    level in nest, against 384 written with the ``function`` keyword. Reading
    them as bindings would keep them out of ``Functions:`` while leaving them
    in the grounding inventory, so the header would omit a third of a
    module's callables. The grammar settles it, so no convention is being
    guessed at.
    """
    value = holder.child_by_field_name("value")
    return FUNCTION if value is not None and value.type in _CALLABLE_VALUES else BINDING


def _bound_names(statement: Node) -> Iterator[tuple[str, str]]:
    """Names bound by a declaration statement or a class field.

    ``const a = 1, b = 2`` is one node with two declarators, as in Java.
    Destructuring (``const { a, b } = x``) binds through an
    ``object_pattern``, whose own named children carry the names; anything
    else in declarator position is a target this does not read, matching
    Python's rule that ``self.conf = x`` binds no addressable name.
    """
    if statement.type in ("public_field_definition", "property_signature"):
        if (name := statement.child_by_field_name("name")) is not None:
            yield _kind_of(statement), node_text(name)
        return
    for child in statement.named_children:
        if child.type != "variable_declarator":
            continue
        name = child.child_by_field_name("name")
        if name is None:
            continue
        if name.type == "identifier":
            yield _kind_of(child), node_text(name)
        elif name.type in ("object_pattern", "array_pattern"):
            # Destructuring never binds a bare function to a name; the value
            # is the object being taken apart.
            for element in name.named_children:
                if element.type in ("shorthand_property_identifier_pattern", "identifier"):
                    yield BINDING, node_text(element)


def _imported_names(node: Node) -> Iterator[str]:
    """The npm package named by one import, re-export or dynamic require.

    Four forms, all present in nest: ``import … from 'x'``, ``export … from
    'x'``, ``await import('x')`` and ``require('x')``. The last two are
    expressions rather than statements, which is why
    ``treesitter.imports_in_tree`` visits every node instead of only the top
    level.
    """
    if node.type in ("import_statement", "export_statement"):
        specifier = _string_value(node.child_by_field_name("source"))
    elif node.type == "call_expression":
        function = node.child_by_field_name("function")
        if function is None or node_text(function) not in _DYNAMIC:
            return
        arguments = node.child_by_field_name("arguments")
        first = arguments.named_children[0] if arguments and arguments.named_children else None
        specifier = _string_value(first)
    else:
        return

    if specifier and (package := package_name(specifier)):
        yield package


def _clause_names(clause: Node) -> list[tuple[str, str | None]]:
    """What one import clause binds: ``default``, ``*`` with its alias, or names."""
    names: list[tuple[str, str | None]] = []
    for child in clause.named_children:
        if child.type == "identifier":
            names.append(("default", node_text(child)))
        elif child.type == "namespace_import":
            alias = next((c for c in child.named_children if c.type == "identifier"), None)
            names.append(("*", node_text(alias) if alias is not None else None))
        elif child.type == "named_imports":
            for spec in child.named_children:
                if spec.type == "import_specifier" and (name := spec.child_by_field_name("name")):
                    alias = spec.child_by_field_name("alias")
                    names.append((node_text(name), node_text(alias) if alias is not None else None))
    return names


def _export_names(node: Node) -> list[tuple[str, str | None]]:
    """What ``export … from`` passes on: ``*``, ``* as ns``, or named specifiers."""
    clause = next((c for c in node.named_children if c.type == "export_clause"), None)
    if clause is not None:
        names = []
        for spec in clause.named_children:
            if spec.type == "export_specifier" and (name := spec.child_by_field_name("name")):
                alias = spec.child_by_field_name("alias")
                names.append((node_text(name), node_text(alias) if alias is not None else None))
        return names
    namespace = next((c for c in node.named_children if c.type == "namespace_export"), None)
    if namespace is not None:
        alias = next((c for c in namespace.named_children if c.type in ("identifier", "string")), None)
        return [("*", node_text(alias) if alias is not None else None)]
    return [("*", None)]


def _import_edges(node: Node) -> Iterator[ImportCapture]:
    """Every module one node names, as an edge needs it.

    Unlike :func:`_imported_names` this keeps a relative specifier, which is
    the in-repository edge the dependency capture discards, and keeps what
    each form binds: ``import D``, ``import * as E``, ``import { A as B }``,
    ``import G = require(...)``. ``export … from`` binds nothing and is marked
    as a re-export, since that is what a barrel file is made of and what the
    resolver follows. ``import()`` and ``require()`` are recorded as edges
    with no binding.
    """
    if node.type == "import_statement":
        require = next((c for c in node.named_children if c.type == "import_require_clause"), None)
        if require is not None:
            alias = next((c for c in require.named_children if c.type == "identifier"), None)
            specifier = _string_value(require.child_by_field_name("source"))
            if specifier is not None:
                yield ImportCapture(specifier, names=(("*", node_text(alias) if alias else None),))
            return
        specifier = _string_value(node.child_by_field_name("source"))
        if specifier is None:
            return
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        yield ImportCapture(specifier, names=tuple(_clause_names(clause)) if clause else ())
    elif node.type == "export_statement":
        specifier = _string_value(node.child_by_field_name("source"))
        if specifier is not None:
            yield ImportCapture(specifier, names=tuple(_export_names(node)), reexport=True)
    elif node.type == "call_expression":
        function = node.child_by_field_name("function")
        if function is None or node_text(function) not in _DYNAMIC:
            return
        arguments = node.child_by_field_name("arguments")
        first = arguments.named_children[0] if arguments and arguments.named_children else None
        if (specifier := _string_value(first)) is not None:
            yield ImportCapture(specifier)


#: Clause node to the relation it states, as in Java.
_CLAUSES = {"extends_clause": "extends", "implements_clause": "implements", "extends_type_clause": "extends"}


def _base_names(node: Node) -> Iterator[tuple[str, str | None]]:
    """Each type a class or interface extends or implements, as written.

    A class's ``extends`` is an expression (``mixin(Base)`` is legal), kept
    verbatim so the resolver can call it unresolved; its type arguments are a
    separate field and are not part of the name. ``implements`` and an
    interface's ``extends`` are types, written with their arguments.
    """
    clauses = list(node.named_children)
    heritage = next((c for c in clauses if c.type == "class_heritage"), None)
    if heritage is not None:
        clauses = list(heritage.named_children)
    for clause in clauses:
        relation = _CLAUSES.get(clause.type)
        if relation is None:
            continue
        if clause.type == "extends_clause":
            for value in clause.children_by_field_name("value"):
                yield squeeze(node_text(value)), relation
        else:
            for child in clause.named_children:
                if child.type != "comment":
                    yield squeeze(node_text(child)), relation


TYPESCRIPT = Dialect(
    name="typescript",
    suffixes=(".ts", ".mts", ".cts"),
    classes=frozenset({
        "class_declaration",
        "abstract_class_declaration",
        "class",
        "interface_declaration",
        "enum_declaration",
        "type_alias_declaration",
    }),
    # The unnamed forms (``arrow_function``, ``function_expression``) declare
    # nothing themselves, so the name branch skips them. They are listed
    # anyway because membership here is also what stops ``_blocks`` from
    # descending into a function body, which is the rule the Python oracle
    # fixes and the one this has to match.
    functions=frozenset({
        "function_declaration",
        "generator_function_declaration",
        "function_expression",
        "generator_function",
        "arrow_function",
        "method_definition",
        "method_signature",
        "abstract_method_signature",
        "function_signature",
    }),
    blocks=frozenset({"class_body", "interface_body", "statement_block", "enum_body"}),
    bindings=frozenset({
        "lexical_declaration",
        "variable_declaration",
        "public_field_definition",
        "property_signature",
    }),
    unwrap={"export_statement": "declaration"},
    bound_names=_bound_names,
    imported_names=_imported_names,
    import_edges=_import_edges,
    base_names=_base_names,
)
