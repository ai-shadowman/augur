"""Kind-aware declarations: the classes, functions and methods a file declares.

:mod:`provenance.declarations` answers *whether* a name is declared, which is
what the grounding check needs, so it returns one lowercased set with no kind
attached::

    {'localcache', 'localcache.get', 'logger'}

An AUGUR header has three separate symbol sections, so writing one needs the
kind as well, and it needs the real casing: the header is read by an extraction
model and the source says ``LocalCache.get``.

This module adds only the kind. :mod:`provenance.declarations` stays the
authority on what counts as a declaration and on which parser answers, and the
traversal helpers are imported from it rather than restated, so the nesting
rules cannot drift. ``tests/test_provenance_symbols.py`` asserts that the
case-folded union of the four kinds here equals ``declarations_in`` on every
file in the candidate checkout; a disagreement fails rather than passing
quietly.

The fourth kind, ``bindings``, is module-level assignment. No header section
takes it and nothing renders it, but it is collected because the oracle counts
it, and dropping it would turn the equality check into a subset check.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from . import treesitter
from .declarations import (
    PYTHON_SUFFIXES,
    _assigned_names,
    _nested_bodies,
    module_key,
)
from .imports import imports_in

# The whole point of this module is to agree with ``declarations``, so its
# traversal rules are reused rather than re-derived. Copying them would be two
# copies of the subtle part: which nodes open a namespace, which are descended
# into with the prefix unchanged, and which targets an assignment binds. The
# kind names come from ``treesitter`` for the same reason: it is the module
# that assigns them on the fallback path, and defining them here instead would
# be a cycle.
from .treesitter import BINDING, CLASS, FUNCTION, METHOD


@dataclass(frozen=True)
class Symbols:
    """What one source file declares, by kind, in source order."""

    classes: tuple[str, ...] = ()
    functions: tuple[str, ...] = ()
    methods: tuple[str, ...] = ()
    bindings: tuple[str, ...] = ()
    #: False when ``ast`` refused the file and tree-sitter answered instead.
    #: A record of which parser ran, not of how much to trust the answer: the
    #: two were measured identical on every file both can read.
    used_ast: bool = True

    @property
    def names(self) -> set[str]:
        """Every name, folded the way ``declarations_in`` folds them."""
        return {
            name.lower()
            for name in (*self.classes, *self.functions, *self.methods, *self.bindings)
        }

    @property
    def declared(self) -> int:
        return len(self.classes) + len(self.functions) + len(self.methods) + len(self.bindings)

    def __bool__(self) -> bool:
        return bool(self.declared)


def _walk_ast(nodes, prefix: tuple[str, ...], out: list[tuple[str, str]]) -> None:
    """Mirror of ``declarations._collect``, recording the kind as it goes.

    Only a class body is descended into with a wider prefix, so a name is a
    method exactly when something encloses it, and a closure's locals never
    reach the header.
    """
    for node in nodes:
        if isinstance(node, ast.ClassDef):
            qualified = (*prefix, node.name)
            out.append((CLASS, ".".join(qualified)))
            _walk_ast(node.body, qualified, out)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            out.append((METHOD if prefix else FUNCTION, ".".join((*prefix, node.name))))
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            for name in _assigned_names(node):
                out.append((BINDING, ".".join((*prefix, name))))
        else:
            for body in _nested_bodies(node):
                _walk_ast(body, prefix, out)


def _classify(found: list[tuple[str, str]], used_ast: bool) -> Symbols:
    """Bucket ``(kind, name)`` pairs, collapsing duplicates on the folded name.

    First occurrence wins, because the oracle is a set and ``def f`` followed
    by ``f = wrap(f)`` is one declaration to it. Keeping the first occurrence
    keeps the more informative kind.
    """
    buckets: dict[str, list[str]] = {CLASS: [], FUNCTION: [], METHOD: [], BINDING: []}
    seen: set[str] = set()
    for kind, name in found:
        folded = name.lower()
        if folded in seen:
            continue
        seen.add(folded)
        buckets[kind].append(name)

    return Symbols(
        classes=tuple(buckets[CLASS]),
        functions=tuple(buckets[FUNCTION]),
        methods=tuple(buckets[METHOD]),
        bindings=tuple(buckets[BINDING]),
        used_ast=used_ast,
    )


def symbols_in_source(source: str) -> Symbols:
    """Classify by parsing with ``ast``, the exact answer. Raises if it refuses.

    The counterpart of :func:`declarations.declarations_in_source`, and the
    oracle the fallback is measured against: for Python 3 the stdlib AST is
    the language's own definition of the syntax, so a disagreement is the
    other parser's.
    """
    found: list[tuple[str, str]] = []
    _walk_ast(ast.parse(source).body, (), found)
    return _classify(found, used_ast=True)


def symbols_in_tree(source: str) -> Symbols:
    """Classify by parsing with tree-sitter, the recovered answer. Never raises.

    The counterpart of :func:`symbols_in_source` for a file it refuses, which
    on this corpus means Python 2. Public so the two parsers can be run
    against the same source and compared without reaching past an underscore;
    ``tests/test_provenance_treesitter_agreement.py`` is what that seam exists
    for, and it is the only reason the equality is a measurement rather than a
    hope.
    """
    return _classify(treesitter.symbols_in_tree(source, treesitter.PYTHON), used_ast=False)


def symbols_in(source: str) -> Symbols:
    """Classify everything one Python source string declares.

    Parses with ``ast`` when it can and with tree-sitter when it cannot, the
    same pairing :func:`declarations.declarations_in` makes.
    """
    try:
        return symbols_in_source(source)
    except (SyntaxError, ValueError, RecursionError):
        return symbols_in_tree(source)


# --------------------------------------------------------------------------
# The header sections
# --------------------------------------------------------------------------

#: How a section's contents were obtained. Both parsers are exact and were
#: measured equal, so the distinction is a record of which one ran rather than
#: a quality grade. The ``-empty`` pair are kept apart on purpose: a file that
#: parsed and declared nothing is a confident answer, and the distinction is
#: what lets a reader tell silence from absence.
AST, AST_EMPTY = "ast", "ast-empty"
TREE_SITTER, TREE_SITTER_EMPTY = "tree-sitter", "tree-sitter-empty"

#: No parser in this tree reads the language, so nothing is claimed about the
#: file. Not an error and not an empty answer, which is why it is neither
#: ``tree-sitter-empty`` nor absent.
UNSUPPORTED = "unsupported-language"


def evidence(used_ast: bool, found: bool) -> str:
    """Which of the four outcome classes one section landed in."""
    if used_ast:
        return AST if found else AST_EMPTY
    return TREE_SITTER if found else TREE_SITTER_EMPTY


def is_supported(path: str) -> bool:
    """Whether the parsers here can read the file at ``path`` at all.

    The suffix decides, not the pipeline's ``language`` argument.
    ``assets/mappings/language_mappings.json`` maps extensions to languages
    many-to-many and is not a classifier; ``declarations.PYTHON_SUFFIXES`` is
    the list this module can actually parse.
    """
    return str(path).lower().endswith(PYTHON_SUFFIXES)


def sections_for(path: str, source: str) -> dict:
    """The header fields a parser settles for one file, plus their provenance.

    Returns the metadata keys ``generate_code_comment`` renders, so a caller
    assigns the result rather than interpreting it. Imports are sorted because
    the underlying module returns a set and a corpus has to rebuild
    byte-identically; declarations keep source order, which is how the model
    used to emit them and how the harness reproduces them.

    A file in a language no parser here reads gets the two evidence keys and
    nothing else. Declining is deliberate: claiming a ``Classes:`` section
    from a reader tuned for a different language would put invented names in
    front of the extraction model. Registering a dialect in
    :data:`provenance.treesitter.DIALECTS` is what lifts this, one language at
    a time and each with its own measurement.

    Every kind a parser reports is now written, whichever parser ran. There
    used to be a precision gate here that withheld a kind from a recovered
    file, because the fallback was a regex line scan and some of what it
    reported was invented. The fallback is a parser now and measured exact, so
    the gate had nothing left to withhold and was removed rather than left
    standing as an inert filter.
    """
    if not is_supported(path):
        return {"symbol_evidence": UNSUPPORTED, "import_evidence": UNSUPPORTED}

    found = symbols_in(source)
    imported, imports_used_ast = imports_in(source)

    return {
        "package": module_key(path),
        "imports": sorted(imported),
        "classes": list(found.classes),
        "functions": list(found.functions),
        "methods": list(found.methods),
        "symbol_evidence": evidence(found.used_ast, bool(found)),
        "import_evidence": evidence(imports_used_ast, bool(imported)),
    }
