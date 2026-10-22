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

from .declarations import (
    _BIND_LINE,
    _CLASS_LINE,
    _DEF_LINE,
    PYTHON_SUFFIXES,
    _assigned_names,
    _nested_bodies,
    module_key,
)
from .imports import imports_in

#: The whole point of this module is to agree with ``declarations``, so its
#: traversal rules are reused rather than re-derived. Copying them would be two
#: copies of the subtle part: which nodes open a namespace, which are descended
#: into with the prefix unchanged, and which targets an assignment binds.
CLASS, FUNCTION, METHOD, BINDING = "class", "function", "method", "binding"


@dataclass(frozen=True)
class Symbols:
    """What one source file declares, by kind, in source order."""

    classes: tuple[str, ...] = ()
    functions: tuple[str, ...] = ()
    methods: tuple[str, ...] = ()
    bindings: tuple[str, ...] = ()
    #: False when ``ast`` refused the file and the line scan answered instead.
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


def _scan_lines(source: str) -> list[tuple[str, str]]:
    """Mirror of ``declarations.declarations_in_lines``, recording the kind.

    Scope comes from indentation, so unlike the AST path this one can see
    inside a function body. Everything it finds there is dropped, which is
    what :func:`_walk_ast` does by never descending, and is where nearly all
    of the scan's measured imprecision used to come from: a real
    ``inner_create_task_cls`` arriving qualified as ``app.task.inner_...``
    reads to the extraction model as a method of a class that does not exist.
    """
    out: list[tuple[str, str]] = []
    #: (indent, name, is_class) for each enclosing block, outermost first.
    stack: list[tuple[int, str, bool]] = []

    for raw in source.expandtabs(8).splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        match = _CLASS_LINE.match(raw) or _DEF_LINE.match(raw) or _BIND_LINE.match(raw)
        if not match:
            continue
        indent = len(match.group("indent"))
        while stack and indent <= stack[-1][0]:
            stack.pop()
        name = match.group("name")
        is_class = match.re is _CLASS_LINE
        is_block = is_class or match.re is _DEF_LINE
        if any(not frame[2] for frame in stack):
            # Inside a function body. Skipping before the push is safe: the
            # enclosing function frame has a smaller indent than anything
            # nested in it, so it stays on the stack and catches the deeper
            # levels too.
            continue
        if is_class:
            kind = CLASS
        elif is_block:
            kind = METHOD if (stack and stack[-1][2]) else FUNCTION
        else:
            kind = BINDING
        out.append((kind, ".".join([*(frame[1] for frame in stack), name])))
        if is_block:
            stack.append((indent, name, is_class))
    return out


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
    """Classify by parsing, the exact answer. Raises if ``ast`` refuses.

    The counterpart of :func:`declarations.declarations_in_source`, and the
    oracle the line scan is measured against: for Python the AST is ground
    truth, so a disagreement is the line scan's.
    """
    found: list[tuple[str, str]] = []
    _walk_ast(ast.parse(source).body, (), found)
    return _classify(found, used_ast=True)


def symbols_in_lines(source: str) -> Symbols:
    """Classify by indentation, the recovered answer. Never raises.

    The counterpart of :func:`declarations.declarations_in_lines`. Public so
    the two paths can be run against the same file and compared without
    reaching past an underscore; the fidelity table in
    ``tests/test_provenance_scan_fidelity.py`` is what that seam exists for.
    """
    return _classify(_scan_lines(source), used_ast=False)


def symbols_in(source: str) -> Symbols:
    """Classify everything one Python source string declares.

    Parses when it can and falls back to the line scan when it cannot, the
    same pairing :func:`declarations.declarations_in` makes.
    """
    try:
        return symbols_in_source(source)
    except (SyntaxError, ValueError, RecursionError):
        return symbols_in_lines(source)


# --------------------------------------------------------------------------
# The header sections
# --------------------------------------------------------------------------

#: How a section's contents were obtained. ``ast`` is exact, ``line-scan`` is
#: the indentation fallback and can be fooled by a docstring, and the
#: ``-empty`` pair are kept apart on purpose: a file that parsed and declared
#: nothing is a confident answer, a file that would not parse and yielded
#: nothing is no answer at all. Phase 4 decides what a header should say about
#: each; this pass only has to keep them distinguishable.
AST, AST_EMPTY, SCAN, SCAN_EMPTY = "ast", "ast-empty", "line-scan", "line-scan-empty"

#: No parser in this tree reads the language, so nothing is claimed about the
#: file. Not an error and not an empty answer, which is why it is neither
#: ``line-scan-empty`` nor absent.
UNSUPPORTED = "unsupported-language"

#: Precision of :func:`symbols_in_lines` against :func:`symbols_in_source`,
#: measured on the 187 celery 2.5.5 files both parsers answer. Micro-averaged:
#: the question is what fraction of emitted names are real, which is what the
#: extraction model sees, so a 400-name file should not weigh the same as a
#: 3-name one. ``tests/test_provenance_scan_fidelity.py`` recomputes these and
#: fails if the scanner drifts away from the figures the gate was set on.
#:
#: ``bindings`` did not move when the three rendered kinds did — 799 true, 65
#: false and 25 missed, before and after, to the name — which is the evidence
#: that its errors are a different mechanism. They are: a keyword argument
#: inside a multi-line call, ``Column("id", Integer,\n    primary_key=True)``,
#: which ``_BIND_LINE`` reads as an assignment because the scan tracks
#: indentation and not bracket depth. Nothing renders ``bindings``, so this
#: costs no header anything, and it is why one kind is still below the bar.
SCAN_PRECISION = {"classes": 0.997, "functions": 0.995, "methods": 0.999, "bindings": 0.925}

#: A kind at or above this is emitted on line-scanned files exactly as it is on
#: parsed ones; a kind below it is withheld there. Precision decides rather than
#: recall because the two failures are not symmetric: a name the scan invents
#: reaches the extraction model as a symbol, and a name it misses is merely
#: absent, which is the failure this pipeline already tolerates everywhere else.
PRECISION_BAR = 0.95

#: The kinds that clear the bar: every kind a header renders, and not
#: ``bindings``, which no section renders anyway. Phase 4 measured 0.834 /
#: 0.683 / 0.957 here and emitted ``methods`` alone.
#:
#: What moved is the scanner, not the bar. Nearly all of Phase 4's loss was one
#: mechanism — ``_walk_ast`` never descends into a function body, deliberately,
#: while ``_scan_lines`` tracked scope by indentation and reported what it
#: found there, so a real ``inner_create_task_cls`` arrived qualified as
#: ``app.task.inner_create_task_cls`` and read to the extraction model as a
#: method of a class that does not exist. Phase 5 made the scan skip function
#: bodies too, and 245 of the 249 false positives in the three rendered kinds
#: went with it.
#:
#: So the gate now withholds nothing the renderer would have written. It is
#: kept because it is what makes that a measurement rather than an assumption:
#: ``tests/test_provenance_scan_fidelity.py`` recomputes these figures from
#: source every run, and a kind that regresses below the bar stops being
#: emitted without anyone having to notice.
SCAN_EMITS = frozenset(kind for kind, p in SCAN_PRECISION.items() if p >= PRECISION_BAR)

#: Imports are scanned by :mod:`provenance.imports`, not by ``_scan_lines``,
#: and were measured the same way: precision 0.992, recall 1.000, 5 false
#: positives in 626 names. ``Dependencies:`` therefore renders on line-scanned
#: files unchanged. Recorded here because the gate's claim is about every
#: section a line scan fills, not only the declaration kinds.
SCAN_IMPORT_PRECISION = 0.992


def evidence(used_ast: bool, found: bool) -> str:
    """Which of the four outcome classes one section landed in."""
    if used_ast:
        return AST if found else AST_EMPTY
    return SCAN if found else SCAN_EMPTY


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
    nothing else. Declining is deliberate: writing a ``Classes:`` section from
    a line scan tuned for Python would put invented names in front of the
    extraction model, and the gate belongs with the parser rather than with
    the caller. tree-sitter lifts this per language in Phase 6.

    A file the line scan recovered gets the same treatment one kind at a time:
    only the kinds in :data:`SCAN_EMITS` are returned, because the rest
    measured below :data:`PRECISION_BAR` on the files both parsers answer. A
    withheld kind is absent rather than empty, which is what the renderer
    needs to drop the section, and ``symbol_evidence`` still records that the
    scan is what ran.
    """
    if not is_supported(path):
        return {"symbol_evidence": UNSUPPORTED, "import_evidence": UNSUPPORTED}

    found = symbols_in(source)
    imported, imports_used_ast = imports_in(source)

    declared = {
        "classes": list(found.classes),
        "functions": list(found.functions),
        "methods": list(found.methods),
    }
    if not found.used_ast:
        declared = {kind: names for kind, names in declared.items() if kind in SCAN_EMITS}

    return {
        "package": module_key(path),
        "imports": sorted(imported),
        **declared,
        "symbol_evidence": evidence(found.used_ast, bool(found)),
        "import_evidence": evidence(imports_used_ast, bool(imported)),
    }
