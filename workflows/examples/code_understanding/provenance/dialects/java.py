"""Java, where the file states its own address and the directory lies.

Two things make this dialect different from Python's, and both are in the
grammar rather than in a convention anyone has to be told about.

**The package is declared, not derived.** Maven puts
``org.apache.commons.lang3.StringUtils`` at
``src/main/java/org/apache/commons/lang3/StringUtils.java``, so folding the
path gives ``src.main.java.org.apache...``, which is not a name any Java
entity will ever be spelled with. :func:`package_of` reads the
``package`` statement instead and appends the file's own stem, giving the
fully-qualified *type* name. That is the counterpart of Python's ``Package:``,
which is the fully-qualified module and not the directory, and it is unique
per file, which is what makes it safe as a corpus key.

**Most named types are not classes.** An interface, an enum, a record and an
``@interface`` all declare a name, hold methods and are what a model will emit
as an entity. The header offers four buckets and ``Classes:`` is the closest
one, so they go there. Omitting them would drop the majority of what a Java
repository declares; inventing a fifth bucket would change the header schema
for one language. Recorded here and in
:data:`provenance.decidability.TABLE` so re-litigating it has to argue past a
row rather than past a silence.

Constructors are emitted as methods, qualified by their type
(``Range.Range``). A constructor is part of the API and the grounding
inventory should vouch for it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from ..treesitter import BINDING, Dialect, node_text

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from tree_sitter import Node


def _bound_names(statement: Node) -> Iterator[tuple[str, str]]:
    """Field names declared by one ``field_declaration``.

    ``private static final int LIMIT = 3, OTHER = 4;`` is one node with two
    ``variable_declarator`` children, so the comma form is not a special case.
    An enum constant is its own node type and is read the same way.

    Always ``BINDING``: Java has no form that declares a callable by binding
    one, so there is nothing here to reclassify.
    """
    if statement.type == "enum_constant":
        if (name := statement.child_by_field_name("name")) is not None:
            yield BINDING, node_text(name)
        return
    for child in statement.named_children:
        if child.type != "variable_declarator":
            continue
        if (name := child.child_by_field_name("name")) is not None:
            yield BINDING, node_text(name)


def _imported_names(node: Node) -> Iterator[str]:
    """The dotted name one ``import`` statement names.

    Reported in full (``java.io.IOException``) rather than reduced to a
    leading segment the way Python's is. Python's top-level name *is* the
    distribution; Java's is ``org`` or ``com``, which identifies nothing. The
    Maven artifact is not derivable from an import either, since
    ``org.apache.commons.lang3`` ships in ``commons-lang3`` and
    ``org.apache.commons.io`` in ``commons-io``: that mapping is a lookup, not
    a transform. Keeping the name as written loses nothing and guesses
    nothing.

    Two forms are reduced, both because the grammar says so and not because of
    a naming convention. ``import java.util.*`` carries an ``asterisk`` node
    and the scoped name already stops at the package. ``import static
    org.junit.Assert.assertEquals`` names a member of a type, and the member
    is dropped so the name reaching the header is the type.
    """
    if node.type != "import_declaration":
        return
    scoped = next((c for c in node.named_children if c.type in _NAME_NODES), None)
    if scoped is None:
        return
    name = node_text(scoped)
    if any(child.type == "static" for child in node.children):
        name = name.rpartition(".")[0] or name
    if name:
        yield name


def package_of(path: str, root: Node) -> str | None:
    """The fully-qualified type name: declared package plus the file stem.

    ``None`` when the file declares no package, which is the unnamed package
    and leaves the path fold as the only address available.
    """
    declaration = next(
        (c for c in root.named_children if c.type == "package_declaration"), None
    )
    if declaration is None:
        return None
    named = next((c for c in declaration.named_children if c.type in _NAME_NODES), None)
    if named is None:
        return None
    stem = PurePosixPath(str(path)).name
    for suffix in JAVA_SUFFIXES:
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return f"{node_text(named)}.{stem}" if stem else node_text(named)


#: Nodes that spell a dotted name. A single-segment package is a bare
#: ``identifier``; anything deeper is a ``scoped_identifier``.
_NAME_NODES = frozenset({"scoped_identifier", "identifier"})

JAVA_SUFFIXES = (".java",)

JAVA = Dialect(
    name="java",
    suffixes=JAVA_SUFFIXES,
    # Every named type, for the reason in the module docstring.
    classes=frozenset({
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
        "annotation_type_declaration",
    }),
    functions=frozenset({
        "method_declaration",
        "constructor_declaration",
        # ``record Pt(int x) { Pt { ... } }``: a constructor with no parameter
        # list of its own, which the grammar gives a separate type.
        "compact_constructor_declaration",
        # ``String value();`` inside an ``@interface``.
        "annotation_type_element_declaration",
    }),
    # ``enum_body_declarations`` is the one that needs the direct-block branch
    # in ``treesitter._walk``: it sits immediately under ``enum_body``, and
    # ``_blocks`` only reaches grandchildren.
    blocks=frozenset({
        "class_body",
        "interface_body",
        "enum_body",
        "enum_body_declarations",
        "annotation_type_body",
    }),
    bindings=frozenset({"field_declaration", "constant_declaration", "enum_constant"}),
    unwrap={},
    bound_names=_bound_names,
    imported_names=_imported_names,
    package_of=package_of,
)
