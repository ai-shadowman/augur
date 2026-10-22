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

from ..treesitter import BINDING, Dialect, node_text

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
)
