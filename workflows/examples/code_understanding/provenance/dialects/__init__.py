"""Every language :mod:`provenance.treesitter` can read, keyed by name.

A dialect is a table, not code: the node types one grammar uses for the four
kinds a header distinguishes. Registering one here is what makes
:func:`provenance.symbols.sections_for` stop declining a language, so a table
arrives with the measurement that justifies it rather than ahead of it.

The suffix decides which dialect reads a file, not the pipeline's ``language``
argument. ``assets/mappings/language_mappings.json`` maps extensions to
languages many-to-many (``.yaml`` is claimed by twelve of them) and is not a
classifier; this registry is the narrower question of what there is a parser
for.
"""

from __future__ import annotations

from ..treesitter import Dialect
from .python import PYTHON

#: In registration order. :func:`dialect_for` scans it, so a suffix claimed by
#: two dialects would resolve to whichever is listed first; none overlap today
#: and ``tests/test_provenance_dialects.py`` asserts that stays true.
DIALECTS: dict[str, Dialect] = {d.name: d for d in (PYTHON,)}

__all__ = ["DIALECTS", "PYTHON", "Dialect", "dialect_for", "suffixes"]


def dialect_for(path: str) -> Dialect | None:
    """The dialect claiming ``path``'s suffix, or ``None``."""
    lowered = str(path).lower()
    for dialect in DIALECTS.values():
        if lowered.endswith(dialect.suffixes):
            return dialect
    return None


def suffixes() -> tuple[str, ...]:
    """Every suffix some registered dialect reads, longest first.

    Longest first so that a strip loop takes ``.d.ts`` over ``.ts`` where one
    is a suffix of the other. Derived rather than declared, so a dialect
    cannot be registered and then silently left out of the key folding.
    """
    every = {suffix for dialect in DIALECTS.values() for suffix in dialect.suffixes}
    return tuple(sorted(every, key=len, reverse=True))
