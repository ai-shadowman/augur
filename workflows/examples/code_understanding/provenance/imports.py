"""Top-level modules the source actually imports, recovered from the AST.

A manifest says what a project declares. An import says what it uses, and the
two disagree often enough to matter. Celery 2.5.5 appends ``threadpool`` to
``install_requires`` at runtime and imports ``pyinotify`` inside a try/except,
so neither appears in any requirements file; both are nonetheless real
dependencies.

Without this, the grounding check cannot tell a hallucinated distribution from
an undeclared one. They look identical from the manifests alone, and they lead
to opposite actions: one is a model error to suppress, the other is a packaging
gap to fix.

Nothing here executes project code. Files that will not parse as Python 3 fall
back to :mod:`provenance.treesitter`, which matters more than it sounds:
celery 2.5.5 is Python 2 and 50 of its files are syntax errors to a modern
``ast``. Dropping them would leave most of the codebase's imports unrecorded
and turn real dependencies into apparent inventions.

The fallback used to be a regex line scan that could not tell an import from
the word "import" inside a docstring. It is now a parser, measured against
this module's own AST path on the 187 celery files both read: 621 names, the
same 621, with nothing invented and nothing missed.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

from .dialects import PYTHON
from .treesitter import imports_in_tree

logger = logging.getLogger(__name__)


def _top_level(name: str | None) -> str | None:
    """``kombu.pools`` imports the ``kombu`` distribution."""
    if not name:
        return None  # a bare relative import, which names nothing external
    return name.split(".", 1)[0]


def imports_in_source(source: str) -> set[str]:
    """Top-level module names imported by one Python source string."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if top := _top_level(alias.name):
                    names.add(top)
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import: it names this package, not a
            # dependency, so there is nothing external to record.
            if node.level:
                continue
            if top := _top_level(node.module):
                names.add(top)
    return names


def imports_in(source: str) -> tuple[set[str], bool]:
    """Imports in one source string, and whether ``ast`` refused it.

    The pair :func:`declarations.declarations_in` has had all along. Without
    it every caller holding a single source string has to spell the fallback
    itself, and the one that forgets crashes on roughly a third of a Python 2
    repository rather than degrading to the parser.
    """
    try:
        return imports_in_source(source), True
    except (SyntaxError, ValueError, RecursionError):
        return imports_in_tree(source, PYTHON), False


def scan_tree(root: Path, skip_dirs: frozenset[str]) -> tuple[set[str], list[str]]:
    """Collect every imported top-level name in a tree.

    Returns the names and a single warning summarising files that needed the
    fallback parser, rather than one warning per file: a Python 2 repository
    produces hundreds and the count is the useful part.
    """
    import os

    names: set[str] = set()
    unparsed: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.endswith(".egg-info")]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = Path(dirpath) / filename
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.debug("could not read %s: %s", path, exc)
                continue
            found, used_ast = imports_in(source)
            names |= found
            if not used_ast:
                unparsed.append(str(path.relative_to(root)))

    warnings = []
    if unparsed:
        sample = ", ".join(sorted(unparsed)[:3])
        warnings.append(
            f"{len(unparsed)} Python file(s) did not parse as Python 3; their imports came "
            f"from tree-sitter instead (for example: {sample})"
        )
    return names, warnings
