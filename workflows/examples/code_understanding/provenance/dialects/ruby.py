"""Ruby, where a module is a namespace and ``require`` is a method call.

Modules go in ``Classes:``. A Ruby module both namespaces and mixes in, so it
is the nearest thing the header's four buckets have, and ``Jekyll`` would
otherwise be absent from a repository where it encloses almost everything.

Names are qualified with ``.``, not ``::``. ``corpus.dotted_key`` folds ``.``
and ``/`` and does not fold ``::``, so a key spelled ``Jekyll::Site`` would
not match the one the path produces and every Ruby file would read as
ungrounded.

``class << self`` is deliberately **absent** from :attr:`Dialect.classes`.
The grammar gives ``singleton_class`` no ``name`` field, so the class branch
would find nothing to name and skip the body with it, losing every method
inside: nineteen occurrences in jekyll alone. Left out, it falls through to
``_blocks``, which yields its ``body_statement`` and walks it with the
enclosing prefix. ``class << self; def helper; end; end`` inside ``Site``
therefore yields ``Jekyll.Site.helper``, which is where the method is
actually addressable from.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from ..treesitter import BINDING, Dialect, ImportCapture, node_text, squeeze

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from tree_sitter import Node

#: The four loaders that name something outside the file. ``require_relative``
#: is absent on purpose: it names a path in this repository, which is the same
#: distinction a Python relative import draws and the same reason it is not
#: recorded as a dependency.
_LOADERS = frozenset({"require", "load", "autoload"})


def _bound_names(statement: Node) -> Iterator[tuple[str, str]]:
    """Names bound by one assignment.

    Constants and plain locals both count; ``@ivar`` and ``@@cvar`` do not,
    because they are state on an instance rather than a name the file
    declares, which is the same line Python's ``self.conf = x`` exclusion
    draws. ``a, b = 1, 2`` binds through a ``left_assignment_list``.

    Always ``BINDING``. Ruby declares a method with ``def``, and a lambda
    assigned to a constant is called with ``.call`` rather than as a method,
    so reclassifying one would put a name in ``Methods:`` that no caller
    spells that way.
    """
    left = statement.child_by_field_name("left")
    if left is None:
        return
    if left.type in ("constant", "identifier"):
        yield BINDING, node_text(left)
    elif left.type == "left_assignment_list":
        for element in left.named_children:
            if element.type in ("constant", "identifier"):
                yield BINDING, node_text(element)


def _imported_names(node: Node) -> Iterator[str]:
    """The gem one ``require``, ``load`` or ``autoload`` call names.

    All three are ordinary method calls in the grammar, so the method name is
    matched rather than a statement type. ``autoload :Foo, 'foo/bar'`` names
    its path second, which is why the arguments are scanned for the first
    string rather than indexed.

    ``require 'active_support/core_ext'`` records ``active_support``: the
    first path segment is what the load path resolves against. That it
    differs from the gem name ``activesupport`` is a lookup rather than a
    transform, the same gap Java's Maven artifacts have, and guessing at it
    is not something a parser can do.
    """
    if node.type != "call":
        return
    method = node.child_by_field_name("method")
    if method is None or node_text(method) not in _LOADERS:
        return
    arguments = node.child_by_field_name("arguments")
    if arguments is None:
        return
    for argument in arguments.named_children:
        if argument.type != "string":
            continue
        content = next(
            (c for c in argument.named_children if c.type == "string_content"), None
        )
        if content is None:
            continue
        # ``load 'x.rb'`` names a file; every other form names a load path.
        path = node_text(content).removesuffix(".rb")
        if first := path.split("/")[0]:
            yield first
        return


#: Loaders an edge records: the dependency capture's, plus ``require_relative``,
#: which names a file in this repository and is exactly the edge it omits.
_EDGE_LOADERS = _LOADERS | {"require_relative"}


def _import_edges(node: Node) -> Iterator[ImportCapture]:
    """One loader call as an edge needs it: the path in full, and which loader.

    The dependency capture keeps a path's first segment and skips
    ``require_relative``, which is right for "which gem" and loses every edge
    inside the repository. A path that is not a plain string literal
    (``File.expand_path("../x", __FILE__)``, ``"a/#{name}"``) is recorded as the
    expression with ``literal`` false, so it is counted rather than dropped.
    """
    if node.type != "call" or node.child_by_field_name("receiver") is not None:
        return
    method = node.child_by_field_name("method")
    if method is None or (loader := node_text(method)) not in _EDGE_LOADERS:
        return
    arguments = node.child_by_field_name("arguments")
    if arguments is None:
        return
    candidates = list(arguments.named_children)
    # ``autoload :Foo, "foo"`` and ``autoload "Foo", "foo"`` both name the
    # constant first and the path second.
    if loader == "autoload":
        candidates = candidates[1:2]
    if not candidates:
        return
    argument = candidates[0]
    parts = list(argument.named_children) if argument.type == "string" else None
    if parts is not None and all(c.type == "string_content" for c in parts):
        yield ImportCapture("".join(node_text(c) for c in parts), loader=loader)
    else:
        yield ImportCapture(squeeze(node_text(argument)), loader=loader, literal=False)


def _base_names(node: Node) -> Iterator[tuple[str, str | None]]:
    """``class Foo < Bar``: the superclass expression, the only inheritance syntax.

    ``include`` and ``extend`` are method calls that mix a module in at
    runtime, not inheritance syntax, and are left out on purpose.
    """
    superclass = node.child_by_field_name("superclass")
    if superclass is not None and superclass.named_children:
        yield squeeze(node_text(superclass.named_children[0])), "extends"


RUBY = Dialect(
    name="ruby",
    suffixes=(".rb",),
    classes=frozenset({"class", "module"}),
    functions=frozenset({"method", "singleton_method"}),
    # ``then``, ``else``, ``ensure`` and the rest are how a compatibility
    # branch holds the declarations inside it, which is the same shape
    # ``if sys.version_info`` has in a Python 2 codebase.
    blocks=frozenset({"body_statement", "then", "else", "ensure", "do", "do_block"}),
    bindings=frozenset({"assignment"}),
    unwrap={},
    bound_names=_bound_names,
    imported_names=_imported_names,
    import_edges=_import_edges,
    base_names=_base_names,
)
