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
back to a line scan, which matters more than it sounds: celery 2.5.5 is Python
2 and 50 of its files are syntax errors to a modern ``ast``. Dropping them
would leave most of the codebase's imports unrecorded and turn real
dependencies into apparent inventions.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

#: ``import a.b, c as d`` and ``from a.b import c``, anchored to the start of a
#: stripped line so that prose mentioning the word "import" does not match.
_IMPORT_LINE = re.compile(r"^import\s+(?P<targets>[\w.,\s]+)$")
_FROM_LINE = re.compile(r"^from\s+(?P<module>[\w.]*)\s+import\s")


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


def imports_in_lines(source: str) -> set[str]:
    """Top-level imports recovered without parsing, for Python 2 sources.

    Used only when ``ast`` refuses the file. It reads one line at a time and
    has no idea what is a string literal, so an import statement quoted inside
    a docstring counts. That bias is deliberate: the result feeds the
    distinction between an undeclared dependency and an invented one, and
    calling a real dependency invented is the worse error.
    """
    names: set[str] = set()
    for raw in source.splitlines():
        line = raw.split("#", 1)[0].strip()
        if match := _FROM_LINE.match(line):
            if top := _top_level(match.group("module")):
                names.add(top)
            continue
        if match := _IMPORT_LINE.match(line):
            for target in match.group("targets").split(","):
                if top := _top_level(target.strip().split(" as ")[0].strip()):
                    names.add(top)
    return names


def scan_tree(root: Path, skip_dirs: frozenset[str]) -> tuple[set[str], list[str]]:
    """Collect every imported top-level name in a tree.

    Returns the names and a single warning summarising files that needed the
    line scan, rather than one warning per file: a Python 2 repository
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
            try:
                names |= imports_in_source(source)
            except (SyntaxError, ValueError):
                unparsed.append(str(path.relative_to(root)))
                names |= imports_in_lines(source)

    warnings = []
    if unparsed:
        sample = ", ".join(sorted(unparsed)[:3])
        warnings.append(
            f"{len(unparsed)} Python file(s) did not parse as Python 3; their imports came "
            f"from a line scan instead (for example: {sample})"
        )
    return names, warnings
