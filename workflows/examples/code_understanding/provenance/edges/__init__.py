"""An edge oracle: the relationships the source itself states.

Every edge in the GraphRAG graph is an unchecked model claim. The entity layer
was fixed by giving each node something to be checked against; this is the
same denominator for edges, read from source with no model.

Two kinds, with honestly different reach:

- **IMPORTS** is in the syntax. Each dialect gets an edge capture beside its
  dependency capture, because the dependency captures answer "which
  distribution" and collapse every internal edge onto one node: on celery,
  ``imports.imports_in`` turns ``from celery.utils import x`` into ``celery``.
- **INHERITS** is in the syntax for the base as written. Which class that
  names is a resolution through the file's own bindings, and each dialect's
  resolver says where that stops.

**CALLS is out of scope** and should not be inferred from the absence of an
entry here. The callee's identity is not syntactically present, and resolving
it against any evidence set smaller than the program is the partial-evidence
failure in its purest form.

Coverage, by dialect. Only a dialect whose capture is written, tested and
measured is claimed; tree-sitter reading a language is not the same as
extracting its edges.

=========== ======= ======== ===========================================
dialect     IMPORTS INHERITS oracle
=========== ======= ======== ===========================================
python      yes     yes      ``ast``, exact agreement with tree-sitter
java        yes     yes      none; constructed cases and a commons-lang pin
typescript  gap     gap      none
ruby        gap     gap      none
=========== ======= ======== ===========================================

Gaps that stay gaps whatever lands: CALLS everywhere; ``__future__`` counted
as an import, as it is in every other import capture this package has.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ..dialects import dialect_for
from ..facts import SKIP_DIRS
from ..treesitter import has_error
from .java import JavaResolver
from .model import (
    EXTERNAL,
    IMPORTS,
    INHERITS,
    INTERNAL,
    KINDS,
    TARGET_CLASSES,
    UNRESOLVED,
    Edge,
    Inventory,
    checkout_files,
)
from .python import PythonResolver

logger = logging.getLogger(__name__)

__all__ = [
    "EXTERNAL",
    "IMPORTS",
    "INHERITS",
    "INTERNAL",
    "RESOLVERS",
    "UNRESOLVED",
    "Edge",
    "EdgeScan",
    "Inventory",
    "checkout_files",
    "scan_checkout",
]

#: Dialect name to resolver class. A dialect absent here has no edge capture
#: and its files are counted under ``no-capture``, never silently skipped.
RESOLVERS = {"python": PythonResolver, "java": JavaResolver}

AST, TREE_SITTER, RECOVERED, UNREADABLE, NO_CAPTURE = (
    "ast", "tree-sitter", "tree-sitter-recovered", "unreadable", "no-capture",
)


@dataclass
class EdgeScan:
    """Every edge one checkout states, with the denominator beside it."""

    edges: set[Edge] = field(default_factory=set)
    #: ``(dialect, evidence)`` to file count. Evidence is ``ast``,
    #: ``tree-sitter``, ``tree-sitter-recovered`` (the grammar had to recover
    #: from malformed source, and whatever it understood is still read),
    #: ``unreadable`` or ``no-capture``.
    files: Counter = field(default_factory=Counter)

    def by_class(self, dialect: str | None = None) -> dict[tuple[str, str], int]:
        """``(kind, target class)`` to edge count, every cell present."""
        counts = Counter(
            (e.kind, e.target_class) for e in self.edges
            if dialect is None or _dialect_of(e.path) == dialect
        )
        return {(k, c): counts[(k, c)] for k in KINDS for c in TARGET_CLASSES}


def _dialect_of(path: str) -> str | None:
    dialect = dialect_for(path)
    return dialect.name if dialect else None


def scan_checkout(root: Path, inventory: Inventory | None = None) -> EdgeScan:
    """Every edge in every file a registered dialect reads under ``root``.

    The files *read* skip :data:`provenance.facts.SKIP_DIRS`, as the import
    scan does, so a vendored dependency's imports are not this repository's
    edges. The *inventory* targets resolve against is the whole checkout, so
    an import of a file under a skipped directory is still internal.
    """
    inventory = inventory or Inventory(checkout_files(root), root)
    resolvers = {name: cls(inventory) for name, cls in RESOLVERS.items()}
    scan = EdgeScan()
    for path in sorted(inventory.paths):
        dialect = dialect_for(path)
        if dialect is None:
            continue
        parts = path.split("/")[:-1]
        if any(p in SKIP_DIRS or p.endswith(".egg-info") for p in parts):
            continue
        resolver = resolvers.get(dialect.name)
        if resolver is None:
            scan.files[(dialect.name, NO_CAPTURE)] += 1
            continue
        try:
            source = (root / path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.debug("could not read %s: %s", path, exc)
            scan.files[(dialect.name, UNREADABLE)] += 1
            continue
        found, used_ast = resolver.edges(path, source)
        scan.edges |= found
        evidence = AST if used_ast else RECOVERED if has_error(source, dialect) else TREE_SITTER
        scan.files[(dialect.name, evidence)] += 1
    return scan
