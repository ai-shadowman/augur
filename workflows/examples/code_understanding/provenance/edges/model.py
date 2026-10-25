"""What an edge is, and the evidence set it is resolved against.

An edge is a pair the source states: this file imports that module, this class
inherits from that one. The target is classified against the **whole
checkout**, never the indexed corpus. The corpus is a subset of the repository,
and a resolver that accepts "the only candidate my evidence knows about"
answers confidently and wrongly whenever that subset omits the real target,
which is how ``..ABSTRACT`` resolved uniquely and wrongly on the node layer.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path, PurePosixPath

from ..dialects import PYTHON

IMPORTS, INHERITS = "IMPORTS", "INHERITS"
KINDS = (IMPORTS, INHERITS)

#: The target is a file, or a name declared in a file, that the checkout holds.
INTERNAL = "internal"
#: The target is outside the checkout: a dependency, the standard library, a
#: builtin. Recorded as written, folded.
EXTERNAL = "external"
#: The source states a target this oracle cannot settle, recorded with the
#: text and the reason rather than dropped, so it stays countable.
UNRESOLVED = "unresolved"
TARGET_CLASSES = (INTERNAL, EXTERNAL, UNRESOLVED)

#: The only directories left out of the inventory. A committed ``build/`` or
#: vendored tree stays in: the inventory decides whether a target exists, and a
#: file the repository tracks exists.
VCS_DIRS = frozenset({".git", ".hg", ".svn"})


@dataclass(frozen=True)
class Edge:
    """One typed pair, keyed the way the graph keys its nodes."""

    kind: str
    #: Folded with :func:`provenance.corpus.dotted_key`. An IMPORTS source is
    #: the file's module key, an INHERITS source the class's symbol key.
    source: str
    #: The target as the source spells it.
    written: str
    #: Folded. ``None`` exactly when :attr:`target_class` is ``UNRESOLVED``.
    target: str | None
    target_class: str
    #: Repository-relative path of the file that states the edge.
    path: str
    #: Why the target is unresolved. ``None`` otherwise.
    reason: str | None = None
    #: ``extends`` or ``implements`` where the grammar distinguishes them.
    detail: str | None = None


def checkout_files(root: Path) -> frozenset[str]:
    """Every file in the checkout, repository-relative and POSIX style.

    The same rule as the clone-time inventory in ``facts.checkout_files`` on
    the analysis branch, restated because that branch is not this one's base.
    """
    found = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in VCS_DIRS]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.is_file():
                found.add(path.relative_to(root).as_posix())
    return frozenset(found)


@dataclass(frozen=True)
class Resolution:
    """Where one written name lands: a class, a folded key, and why if nowhere."""

    target_class: str
    key: str | None
    reason: str | None = None

    def edge(self, kind: str, source: str, written: str, path: str, detail: str | None = None) -> Edge:
        return Edge(kind, source, written, self.key, self.target_class, path, self.reason, detail)


def unresolved(reason: str) -> Resolution:
    return Resolution(UNRESOLVED, None, reason)


#: Directories an absolute Python import is always tried from. ``src/`` is
#: the packaging layout's root. Others are discovered; see
#: :attr:`Inventory.python_roots`.
PYTHON_ROOTS = ("", "src/")


class Inventory:
    """The checkout's files, with the per-language indexes resolvers need.

    ``root`` is where the files are, for a resolver that has to read one
    (Java's declared packages, a Python package's ``__init__``). ``None`` for
    an inventory built from paths alone, which such a resolver refuses.
    """

    def __init__(self, paths: Iterable[str], root: Path | None = None):
        self.paths = frozenset(PurePosixPath(p).as_posix() for p in paths)
        self.root = root

    def read(self, path: str) -> str | None:
        if self.root is None or path not in self.paths:
            return None
        try:
            return (self.root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    @cached_property
    def _python_stems(self) -> dict[str, str]:
        """Stem to the one file an import of it loads.

        ``x.py`` beside its ``x.pyi`` stub is one module, and the earlier suffix
        in the dialect is the one loaded.
        """
        stems: dict[str, str] = {}
        for path in sorted(self.paths):
            lowered = path.lower()
            for rank, suffix in enumerate(PYTHON.suffixes):
                if lowered.endswith(suffix):
                    stem = path[: -len(suffix)]
                    held = stems.get(stem)
                    if held is None or rank < _suffix_rank(held):
                        stems[stem] = path
                    break
        return stems

    @cached_property
    def python_roots(self) -> tuple[str, ...]:
        """Directories a top-level package sits directly under.

        :data:`PYTHON_ROOTS`, plus the parent of every package (a directory
        with an ``__init__``) that has no package anywhere above it. graphite
        keeps its package at ``webapp/graphite/``, so without this every
        ``graphite.*`` import read as external. A directory under
        :data:`provenance.facts.SKIP_DIRS` is not a root: ``build/lib/celery``
        is a copy. Neither is ``pkg/compat/`` inside the package ``pkg``,
        which is a subpackage path that only reads as a root because
        ``compat`` itself has no ``__init__``.

        A discovered root is evidence that code exists there, not that it is
        on ``sys.path``, so :meth:`python_modules` records which root a hit
        came through and a resolver honours a discovered one only for a file
        beneath it. A fixture at ``tests/fixtures/proj/requests/`` otherwise
        shadows the library for the whole repository.
        """
        from ..facts import SKIP_DIRS

        packages = {
            PurePosixPath(stem).parent.as_posix()
            for stem in self._python_stems
            if PurePosixPath(stem).name == "__init__"
        }
        roots = set(PYTHON_ROOTS)
        for package in packages:
            parent = PurePosixPath(package).parent
            if parent.as_posix() in ("", "."):
                continue
            if any(ancestor.as_posix() in packages for ancestor in (parent, *parent.parents)):
                continue
            if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in parent.parts):
                continue
            roots.add(f"{parent.as_posix()}/")
        return tuple(sorted(roots))

    def _under_roots(self, parts: list[str], roots: Iterable[str]) -> Iterator[tuple[str, str]]:
        """``(root, dotted name)`` for each root ``parts`` sits beneath."""
        for root in roots:
            prefix = root.rstrip("/").split("/") if root else []
            if parts[: len(prefix)] == prefix and len(parts) > len(prefix):
                yield root, ".".join(parts[len(prefix):])

    @cached_property
    def python_modules(self) -> dict[str, dict[str, str]]:
        """Dotted module name, real case, to each file it could name and its root.

        Case is kept because Python's import is case-sensitive: trac's
        ``import SilverCity`` is a library, not its own ``silvercity.py``. A
        package is its ``__init__``, and wins over a same-named module beside
        it, as Python's finder does. Keyed under each root the file sits
        beneath, so a name two roots satisfy with different files is
        ambiguous rather than chosen.
        """
        index: dict[str, dict[str, tuple[str, str]]] = {}
        for stem, path in self._python_stems.items():
            parts = stem.split("/")
            package = parts[-1] == "__init__"
            if package:
                parts = parts[:-1]
            if not parts:
                continue
            directory = "/".join(parts)
            for root, key in self._under_roots(parts, self.python_roots):
                by_dir = index.setdefault(key, {})
                if directory not in by_dir or package:
                    by_dir[directory] = (path, root)
        return {key: dict(by_dir.values()) for key, by_dir in index.items()}

    @cached_property
    def python_directories(self) -> frozenset[str]:
        """Dotted names, under a standard root, of directories holding Python files.

        A namespace package has no ``__init__`` and so no module key; this is
        what tells ``import ns.gone`` apart from an import of something outside
        the checkout. Only under :data:`PYTHON_ROOTS`: a discovered root exists
        because a real package sits under it, and its other directories are
        usually not packages at all. celery's ``examples/eventlet/`` holds
        example scripts, and keyed under ``examples/`` it shadowed the
        ``eventlet`` library for every import of it.
        """
        found = set()
        for stem in self._python_stems:
            parts = stem.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                found.update(key for _root, key in self._under_roots(parts[:i], PYTHON_ROOTS))
        return frozenset(found)


def _suffix_rank(path: str) -> int:
    lowered = path.lower()
    return next(i for i, s in enumerate(PYTHON.suffixes) if lowered.endswith(s))
