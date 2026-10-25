"""The names each source file declares, so an entity claiming one can be checked.

Two thirds of the entities a run produces are classes and functions, and
nothing looked at them. The module layer is held to the document headers and
the package layer to the manifests, but a claim like
``celery.worker.state.task_accepted`` had no check at all, even though the file
that settles it is sitting in the corpus.

It is settled by reading. ``celery/worker/state.py`` either declares
``task_accepted`` or it does not, and ``ast`` answers that with no model and no
calibration argument. Where the answer is no, the symbol usually still exists
somewhere else in the project, which is the module layer's misattribution one
level down and is repairable by the same kind of join.

Nothing here executes project code. Files that will not parse as Python 3 fall
back to :mod:`provenance.treesitter`, as in :mod:`provenance.imports` and for
the same reason: celery 2.5.5 is Python 2, and 10 of the 22 documents a run
indexes are syntax errors to a modern ``ast``. Dropping them would leave half
the corpus with no declarations recorded and turn real symbols into apparent
inventions.

The fallback is a parser rather than the regex line scan it replaced, and it
is held to the oracle: on the 187 celery files both read, tree-sitter and
``ast`` return the same 4,781 names with no disagreement anywhere. So the two
paths differ in which files they can answer and not in what they say.

``ast`` stops at Python, and the same parser reads the other languages
:mod:`provenance.dialects` registers. :func:`declarations_in` therefore takes
the dialect rather than assuming one, and the inventory scanners pick it from
the suffix, so a Java or Ruby checkout contributes its declarations instead of
being skipped as unreadable.

This module also resolves the leading dots on a relative import. The extraction
prompt tells the model to keep them, because the package a document sits in is
a calculation on the header path rather than something to infer from a chunk.
:func:`resolve_relative` is that calculation.
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from .corpus import dotted_key, parse_header
from .dialects import PYTHON, Dialect, dialect_for, suffixes
from .models import Finding, FindingKind, RepoFacts
from .treesitter import BaseCapture, base_captures_in_tree, declarations_in_tree, parse, squeeze

logger = logging.getLogger(__name__)

#: Entity types that name something a file declares. ``variable`` is absent
#: because it is outside the prompt's configured vocabulary; the stitch reports
#: those separately rather than this check quietly adjudicating them.
SYMBOL_TYPES = frozenset({"class", "function"})


# --------------------------------------------------------------------------
# Reading declarations out of source
# --------------------------------------------------------------------------

def _assigned_names(node: ast.Assign | ast.AnnAssign) -> Iterator[str]:
    """Names bound by one assignment statement, ignoring attribute targets.

    ``self.conf = x`` binds no module-level name, and the prompt already says
    an attribute read is not an entity, so only plain names count.
    """
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    for target in targets:
        if isinstance(target, ast.Name):
            yield target.id
        elif isinstance(target, ast.Tuple | ast.List):
            for element in target.elts:
                if isinstance(element, ast.Name):
                    yield element.id


def _nested_bodies(node: ast.AST) -> Iterator[list]:
    """Statement lists hanging off a node that does not open a namespace.

    A class defined inside ``try:`` or behind ``if sys.version_info`` is still
    declared by the module, and both shapes are common in a Python 2 codebase
    carrying compatibility branches.
    """
    for name in ("body", "orelse", "finalbody"):
        value = getattr(node, name, None)
        if isinstance(value, list):
            yield value
    for handler in getattr(node, "handlers", None) or ():
        body = getattr(handler, "body", None)
        if isinstance(body, list):
            yield body


def _collect(nodes: Iterable[ast.AST], prefix: tuple[str, ...], found: set[str]) -> None:
    for node in nodes:
        if isinstance(node, ast.ClassDef):
            qualified = (*prefix, node.name)
            found.add(".".join(qualified).lower())
            # Only a class body is descended into with a wider prefix. A
            # function's locals are not addressable from outside it, so
            # recording them would let a closure vouch for an invented name.
            _collect(node.body, qualified, found)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found.add(".".join((*prefix, node.name)).lower())
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            for name in _assigned_names(node):
                found.add(".".join((*prefix, name)).lower())
        else:
            for body in _nested_bodies(node):
                _collect(body, prefix, found)


def declarations_in_source(source: str) -> set[str]:
    """Every name one Python source string declares, lowercased.

    Methods arrive qualified by their class (``localcache.get``), because that
    is how the prompt asks for them and how the model spells them.
    """
    found: set[str] = set()
    _collect(ast.parse(source).body, (), found)
    return found


def declarations_in(source: str, dialect: Dialect) -> tuple[set[str], bool]:
    """Declarations in one source string, and whether ``ast`` refused it.

    ``ast`` is reserved for Python, where it is the language's own definition
    of the syntax. Every other dialect goes straight to tree-sitter and comes
    back ``False``, which is the honest record rather than a quality grade:
    there is no second reader for Java to disagree with.

    The dialect is required rather than defaulted to :data:`PYTHON`. A default
    would read a ``.java`` file as Python at whichever call site forgot to
    pass one, and returning nothing confidently is the failure mode the whole
    ``unsupported-language`` path exists to prevent.
    """
    if dialect is PYTHON:
        try:
            return declarations_in_source(source), True
        except (SyntaxError, ValueError, RecursionError):
            pass  # Python 2, or nested past the recursion limit.
    return declarations_in_tree(source, dialect), False


def _collect_bases(
    nodes: Iterable[ast.AST], prefix: tuple[str, ...], source: str, found: set[BaseCapture]
) -> None:
    """:func:`_collect`'s walk, keeping each class's positional bases.

    The same branches, so a base is recorded exactly for the classes the
    declaration inventory holds. ``metaclass=`` and ``**kw`` are keywords
    and name no base; a base that is not a name is kept verbatim, because
    dropping it would make an unresolvable base indistinguishable from none.
    """
    for node in nodes:
        if isinstance(node, ast.ClassDef):
            qualified = (*prefix, node.name)
            owner = ".".join(qualified)
            for base in node.bases:
                text = ast.get_source_segment(source, base) or ast.unparse(base)
                found.add(BaseCapture(owner, squeeze(text)))
            _collect_bases(node.body, qualified, source, found)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Assign | ast.AnnAssign):
            continue
        else:
            for body in _nested_bodies(node):
                _collect_bases(body, prefix, source, found)


def base_captures_in_source(source: str) -> set[BaseCapture]:
    """Every base every declared class names, read by ``ast``."""
    found: set[BaseCapture] = set()
    _collect_bases(ast.parse(source).body, (), source, found)
    return found


def base_captures_in(source: str, dialect: Dialect) -> tuple[set[BaseCapture], bool]:
    """Base captures in one source string, and whether ``ast`` read it."""
    if dialect is PYTHON:
        try:
            return base_captures_in_source(source), True
        except (SyntaxError, ValueError, RecursionError):
            pass  # Python 2, or nested past the recursion limit.
    return base_captures_in_tree(source, dialect), False


# --------------------------------------------------------------------------
# Building the inventory
# --------------------------------------------------------------------------

def _unparsed_warning(unparsed: list[str], subject: str) -> list[str]:
    """One warning naming the count, as :mod:`provenance.imports` does.

    A Python 2 repository produces hundreds of these and the count is the
    useful part, so they are summarised rather than logged one by one.

    Only Python files are ever counted. A Java file has no ``ast`` path to
    fall back from, so reporting every one of them as a parse failure would
    turn the normal case into a warning and bury the real ones.
    """
    if not unparsed:
        return []
    sample = ", ".join(sorted(unparsed)[:3])
    return [
        f"{len(unparsed)} {subject} did not parse as Python 3; their declared "
        f"names came from tree-sitter instead (for example: {sample})"
    ]


def scan_files(root: Path, paths: Iterable[Path]) -> tuple[dict[str, set[str]], list[str]]:
    """Map every readable source file under ``root`` to the names it declares.

    Takes the paths rather than walking, so a caller that already has the file
    list does not pay for a second traversal of the tree.

    A file in a language no dialect is registered for is skipped rather than
    read with the nearest parser. A requirements file or a ``setup.cfg`` is in
    the checkout too, and reading one as Python would turn every
    ``key = value`` into an apparent declaration.
    """
    symbols: dict[str, set[str]] = {}
    unparsed: list[str] = []

    for path in paths:
        dialect = dialect_for(path.name)
        if dialect is None:
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.debug("could not read %s: %s", path, exc)
            continue
        relative = path.relative_to(root).as_posix()
        found, parsed = declarations_in(source, dialect)
        if dialect is PYTHON and not parsed:
            unparsed.append(relative)
        if found:
            key = namespace_for(relative, source, dialect)
            symbols.setdefault(key, set()).update(found)

    return symbols, _unparsed_warning(unparsed, "Python file(s)")


def scan_corpus(documents) -> dict[str, set[str]]:
    """Map each indexed document to the names it declares.

    The header names the file, which is what makes the result addressable:
    an entity called ``celery.worker.state.task_accepted`` is a claim about the
    document whose header says ``celery/worker/state.py``. A document whose
    header will not parse is skipped, since there is nothing to attribute its
    declarations to.

    The document text carries the AUGUR header, and every dialect parses it:
    the delimiters are per-language, so a Java document opens with ``/*`` and
    a Ruby one with ``=begin``, and the symbols found are the same with the
    header as without it.

    The key is :func:`namespace_for`, not the path fold, because what a caller
    wants back is a key that composes with a stored symbol into the name the
    model would write. Those differ wherever a language states its own
    namespace: a Maven Java file folds to ``src.main.java.…`` and is addressed
    as ``org.apache.commons.lang3``, and a Ruby file's symbols carry their whole
    ``module`` chain already and so are keyed under ``""``.
    """
    symbols: dict[str, set[str]] = {}
    if documents is None or "text" not in documents:
        return symbols

    unparsed: list[str] = []
    for text in documents["text"]:
        header = parse_header(text)
        if header is None or (dialect := dialect_for(header.path)) is None:
            continue
        found, parsed = declarations_in(str(text), dialect)
        if dialect is PYTHON and not parsed:
            unparsed.append(header.path)
        if found:
            key = namespace_for(header.path, str(text), dialect)
            symbols.setdefault(key, set()).update(found)

    for note in _unparsed_warning(unparsed, "indexed document(s)"):
        logger.info("provenance: %s", note)
    return symbols


def module_key(path: str) -> str:
    """The dotted name a file is addressed by, with its suffix removed.

    ``celery/worker/state.py`` is ``celery.worker.state``, and
    ``celery/app/__init__.py`` is ``celery.app``, because that is the name an
    import statement uses for it.

    The suffixes stripped are the registered dialects', longest first, so
    ``.mts`` is taken before the ``.ts`` it ends with. Deliberately not
    :func:`provenance.languages.source_suffixes`, which is the wider question
    of what the corpus key folding handles: it carries ``.cfg``, ``.toml`` and
    ``.txt``, and stripping those here would turn ``setup.cfg`` into
    ``setup`` and collide it with ``setup.py``.
    """
    posix = PurePosixPath(str(path))
    parent = posix.parent.as_posix()
    stem = posix.name
    for suffix in suffixes():
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    parts = [] if parent in ("", ".") else parent.split("/")
    if stem != "__init__":
        parts.append(stem)
    return dotted_key(".".join(parts))


#: How each dialect's walker addresses a symbol, which decides what has to be
#: prepended to reach a fully-qualified name. Keyed by the dialect's registered
#: name and read by :func:`namespace_for`.
#:
#: A table rather than four ``if`` branches, for the reason
#: :mod:`provenance.decidability` is one: a per-language claim that something
#: downstream depends on should be importable and assertable, so the argument is
#: settled once instead of re-derived every time a dialect is added.
PATH_FOLD = "path-fold"
DECLARED_PACKAGE = "declared-package"
NO_PREFIX = "none"

NAMESPACE_RULES = {
    # The walker qualifies by enclosing class, and a module's name is its path.
    # This is the composition the join assumed for every language.
    "python": PATH_FOLD,
    "typescript": PATH_FOLD,
    # Qualified by enclosing type, whose name the language requires to equal the
    # file stem. ``dialect.package_of`` returns the package *plus* that stem,
    # because that is what makes a unique corpus key; composing a type-qualified
    # symbol against it names the type twice.
    "java": DECLARED_PACKAGE,
    # The walker returns the whole ``module``/``class`` chain, which is already
    # the address a caller uses. There is nothing to prepend.
    "ruby": NO_PREFIX,
}


def namespace_for(path: str, source: str, dialect: Dialect) -> str:
    """The prefix a fully-qualified name for this file's symbols carries.

    :func:`module_key` answers the same question for the file itself; this
    answers it for what the file declares, and the two differ wherever a
    language states its own namespace. ``""`` where the walker's symbol is
    already the whole address, so callers join with a dot only when the result
    is non-empty.

    Folded, unlike :func:`provenance.symbols.package_for`, which keeps the
    source's casing because a header is rendered from it and a model reads that
    header back. Nothing renders this: it only builds inventory keys, which are
    compared against :func:`provenance.corpus.dotted_key` of an entity title,
    so folding here is one place instead of every call site.

    Raises on a dialect :data:`NAMESPACE_RULES` does not record, rather than
    defaulting to the path fold. Defaulting is how the doubling arrived: Phase
    6b registered Java and Ruby against a join that silently assumed Python's
    rule, and the symbol layer then scored 0.0 on arms where the model had
    answered correctly. ``tests/test_provenance_declarations.py`` asserts the
    table covers every registered dialect, so this is unreachable in the product
    and fires only for whoever adds the next one.
    """
    rule = NAMESPACE_RULES.get(dialect.name)
    if rule is None:
        raise ValueError(
            f"no symbol-composition rule recorded for dialect {dialect.name!r}; "
            f"provenance.declarations.NAMESPACE_RULES knows "
            f"{sorted(NAMESPACE_RULES)}. Registering a dialect means deciding how "
            f"its walker's symbols compose into a qualified name."
        )
    if rule == NO_PREFIX:
        return ""
    if rule == PATH_FOLD:
        return module_key(path)

    # Declared package, stem removed. ``package_of`` is asked directly rather
    # than through ``symbols.package_for``, because the question here is whether
    # the file declares a package at all and ``package_for``'s fallback answers
    # a different one. Inferring it from ``declared == module_key(path)`` looks
    # equivalent and is not: a Java file at ``org/apache/foo/Bar.java`` that
    # declares ``package org.apache.foo`` folds to exactly its own path, so the
    # equality fires on the ordinary non-Maven layout and keeps the stem.
    #
    # No package declared is the unnamed package, where the type name is the
    # whole address and there is nothing to prepend.
    if dialect.package_of is None:
        return ""
    declared = dialect.package_of(path, parse(source, dialect).root_node)
    if not declared:
        return ""
    head, _, _ = dotted_key(declared).rpartition(".")
    return head


def owners_of(symbols: Mapping[str, Iterable[str]]) -> dict[str, set[str]]:
    """Invert the inventory: which modules declare each name."""
    index: dict[str, set[str]] = {}
    for owner, names in symbols.items():
        for name in names:
            index.setdefault(name, set()).add(owner)
    return index


def _splits(key: str) -> Iterator[tuple[str, str]]:
    """Every ``(owner, symbol)`` division of a dotted name, longest symbol first.

    Starts at ``i = 0``, so the first division is an owner of ``""`` against
    the whole key. That is the division a Ruby symbol needs:
    :func:`namespace_for` keys those under ``""`` because the walker already
    returned the full ``module`` chain. :func:`declared_in` walks this
    reversed, so ``""`` is tried last and a Python key still prefers its
    longest real owner.
    """
    parts = key.split(".")
    for i in range(len(parts)):
        yield ".".join(parts[:i]), ".".join(parts[i:])


def declared_in(key: str, symbols: Mapping[str, Iterable[str]]) -> str | None:
    """The module named by ``key`` that also declares the rest of it.

    Longest owner first, so ``celery.worker.state.task_accepted`` is tried
    against the module ``celery.worker.state`` before the class
    ``celery.worker.state`` inside ``celery.worker``.
    """
    for owner, symbol in reversed(list(_splits(key))):
        if symbol in (symbols.get(owner) or ()):
            return owner
    return None


def attribution_of(
    key: str, index: Mapping[str, set[str]]
) -> tuple[str, set[str]] | None:
    """The symbol ``key`` ends in, and every module that declares it.

    Longest symbol first, returning at the first hit. A model that misfiles
    ``task_accepted`` under the wrong module still spelled the symbol right,
    so the last segments are the part worth matching on, and the longest match
    is the most specific evidence available.

    The symbol comes back alongside its owners because a caller correcting the
    attribution has to rebuild the title from both halves, and splitting the
    key a second time at the call site would be a second chance to split it
    differently.

    The whole key is the first thing tried, because :func:`_splits` now opens
    with the ``i = 0`` division. It used to be a fall-through after the loop,
    which tried it last and so preferred a shorter suffix match over an exact
    one.
    """
    for _owner, symbol in _splits(key):
        if owners := index.get(symbol):
            return symbol, set(owners)
    return None


def declared_elsewhere(key: str, index: Mapping[str, set[str]]) -> set[str]:
    """Modules declaring the symbol ``key`` names, wherever it really lives."""
    found = attribution_of(key, index)
    return found[1] if found else set()


# --------------------------------------------------------------------------
# Relative imports
# --------------------------------------------------------------------------

def is_relative(name: str) -> bool:
    return str(name).strip().startswith(".")


def resolve_relative(name: str, document_path: str) -> str | None:
    """Resolve a dotted relative import against the document that produced it.

    PEP 328: the dots count upwards from the package the importing module sits
    in, so ``..datastructures`` in ``celery/app/base.py`` is
    ``celery.datastructures``. Returns ``None`` when the name is not relative
    or when the dots climb past the top of the tree, which is a spelling no
    path can satisfy.
    """
    text = str(name).strip()
    level = len(text) - len(text.lstrip("."))
    if not level:
        return None

    package = PurePosixPath(str(document_path)).parent.as_posix()
    parts = [] if package in ("", ".") else package.lower().split("/")
    if level - 1 >= len(parts):
        return None

    base = parts[: len(parts) - (level - 1)]
    remainder = dotted_key(text[level:])
    return ".".join([*base, remainder]) if remainder else ".".join(base)


def resolve_candidates(name: str, document_paths: Iterable[str]) -> set[str]:
    """Every resolution of one relative name across the documents in a corpus.

    Used where the producing document is not known. A caller keeps the answer
    only when exactly one candidate lands on a path that exists, so an
    ambiguous name such as ``..abstract``, which celery can satisfy from two
    different packages, resolves to nothing rather than to a guess.
    """
    found = set()
    for path in document_paths:
        if resolved := resolve_relative(name, path):
            found.add(resolved)
    return found


def resolve_unique(name: str, document_paths: Iterable[str], real: set[str]) -> str | None:
    """The one resolution of ``name`` that names something in ``real``."""
    matches = resolve_candidates(name, document_paths) & real
    return matches.pop() if len(matches) == 1 else None


# --------------------------------------------------------------------------
# The check
# --------------------------------------------------------------------------

def _symbol_rows(entities):
    """Entity rows typed as a class or a function."""
    if "type" not in entities:
        return entities.iloc[0:0]
    kinds = entities["type"].fillna("").astype(str).str.strip().str.lower()
    return entities[kinds.isin(SYMBOL_TYPES)]


def _sample(owners: set[str], limit: int = 3) -> str:
    listed = sorted(owners)
    shown = ", ".join(listed[:limit])
    return shown if len(listed) <= limit else f"{shown} and {len(listed) - limit} more"


def check_symbol_layer(
    facts: RepoFacts, entities, documents
) -> tuple[list[Finding], dict[str, Any]]:
    """Every class and function entity should name something a file declares.

    Five outcomes, with five different causes:

    ``declared``      the document its name points at declares it. Correct.
    ``misattributed`` a different indexed document declares it. The symbol is
                      real and the owner is wrong, which is the module layer's
                      failure one level down and the same join repairs it.
    ``leaked``        declared somewhere in the checkout that was never
                      indexed, so the model recalled it rather than read it.
    ``invented``      declared nowhere in the repository.
    ``bare``          no owner in the name at all, which the prompt forbids
                      and which leaves nothing to check the claim against.
    """
    corpus_symbols = scan_corpus(documents)
    if not corpus_symbols:
        return [], {}

    checkout = {key: set(names) for key, names in (facts.declared_symbols or {}).items()}
    corpus_index = owners_of(corpus_symbols)
    checkout_index = owners_of(checkout)

    findings: list[Finding] = []
    counts = {"declared": 0, "misattributed": 0, "leaked": 0, "invented": 0, "bare": 0}

    for title in _symbol_rows(entities)["title"].astype(str):
        key = dotted_key(title)
        if not key:
            continue

        if "." not in key:
            counts["bare"] += 1
            findings.append(Finding(
                kind=FindingKind.UNGROUNDED,
                subject=title,
                detail="a bare name with no module to own it; the prompt asks "
                       "for a fully qualified name so the claim can be checked",
            ))
            continue

        # ``is not None`` rather than truthiness: ``""`` is a real owner. Ruby
        # keys its whole inventory there, because the walker already returned
        # the ``module`` chain and :func:`namespace_for` has nothing to
        # prepend, so a correctly qualified Ruby title comes back ``""`` and
        # reading that for truth would bucket every one of them as wrong.
        if declared_in(key, corpus_symbols) is not None:
            counts["declared"] += 1
            continue

        if owners := declared_elsewhere(key, corpus_index):
            counts["misattributed"] += 1
            findings.append(Finding(
                kind=FindingKind.UNGROUNDED,
                subject=title,
                detail="the symbol is real but this module does not declare it",
                actual=f"declared in {_sample(owners)}",
            ))
            continue

        owner = declared_in(key, checkout)
        owners = declared_elsewhere(key, checkout_index)
        if owner is not None or owners:
            counts["leaked"] += 1
            findings.append(Finding(
                kind=FindingKind.UNGROUNDED,
                subject=title,
                detail="declared in a file the run never indexed; recalled "
                       "from training, not read",
                # ``key`` is the last resort for the empty owner, where the
                # name carries its own namespace and there is no module to
                # name beside it.
                actual=f"declared in {owner or _sample(owners) or key}",
            ))
            continue

        counts["invented"] += 1
        findings.append(Finding(
            kind=FindingKind.UNGROUNDED,
            subject=title,
            detail="no file in the repository declares this name",
        ))

    total = sum(counts.values())
    counts["precision"] = round(counts["declared"] / total, 3) if total else None
    counts["indexed_modules"] = len(corpus_symbols)
    # Without the checkout inventory there is no way to tell a leaked symbol
    # from an invented one, and the invented count absorbs both. Said plainly
    # rather than left to be inferred from a zero.
    counts["checkout_inventory"] = len(checkout) or "absent"
    return findings, {"symbol_layer": counts}
