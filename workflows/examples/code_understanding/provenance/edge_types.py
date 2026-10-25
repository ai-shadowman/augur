"""Read a relationship's type out of its description, since nowhere else holds it.

The relationship tuple in ``assets/graphrag/extract_graph.txt`` is
``(source, target, description, strength)``. The prompt declares seven
relationship types and then gives them nowhere to live but the description,
so a type is only data once something reads it back out. This module is that
reader, and it reads conservatively: a description names a type when the
type's token appears uppercase, as a whole word. Lowercase prose ("calls
getCause", "imports the module") does not count, because the vocabulary the
prompt offers is the uppercase token and lowercase verbs turn up inside
descriptions of other relations.

GraphRAG merges duplicate edges, so one description can carry several claims
and several types. :func:`types_in` returns all of them; :func:`type_of`
picks one by :data:`PRECEDENCE`, most specific first, so the types an oracle
can check outrank the ones it cannot. A description naming none is
:data:`UNTYPED` rather than empty, so the gap stays countable downstream.

The rule was registered before it was counted; the measured split is in
``augur-test/corpus/MANIFEST.md`` under Relationship layer Phase 2.
"""

from __future__ import annotations

import re
from typing import Any

#: The prompt's vocabulary, most specific first. ``type_of`` takes the first
#: of these a description names. ``INHERITS`` and ``IMPORTS`` lead the
#: in-repository types because ``provenance.edges`` can check them.
PRECEDENCE = (
    "CROSS_REPO_IMPORTS",
    "CROSS_REPO_DEPENDS_ON",
    "INHERITS",
    "IMPORTS",
    "CALLS",
    "USES",
    "DEPENDS_ON",
)

#: Types AUGUR writes itself, from a join rather than a model call:
#: ``BELONGS_TO`` in ``provenance.graph._belongs_to`` and ``DEFINED_IN`` in
#: ``provenance.containment``. Read after the prompt's vocabulary, so a model
#: row is typed by what the model claimed. No model row on the three measured
#: corpora names either token.
AUGUR_WRITTEN = ("BELONGS_TO", "DEFINED_IN")

#: The value for a description that names no type.
UNTYPED = "UNTYPED"

#: Every value the derived column can hold.
VOCABULARY = frozenset(PRECEDENCE + AUGUR_WRITTEN) | {UNTYPED}

#: The column :func:`with_type` adds.
TYPE_COLUMN = "type"

# Case-sensitive, and ``\b`` treats ``_`` as a word character, so
# ``DEPENDS_ON`` cannot match inside ``CROSS_REPO_DEPENDS_ON``.
_TOKEN = re.compile(r"\b(" + "|".join(PRECEDENCE + AUGUR_WRITTEN) + r")\b")


def types_in(description: str | None) -> frozenset[str]:
    """Every vocabulary type the description names, repeats folded."""
    if not description:
        return frozenset()
    return frozenset(_TOKEN.findall(description))


def type_of(description: str | None) -> str:
    """The one type the column carries: the most specific named, or UNTYPED."""
    named = types_in(description)
    return next((t for t in PRECEDENCE + AUGUR_WRITTEN if t in named), UNTYPED)


def with_type(relationships: Any) -> Any:
    """A copy of a relationships frame with the derived ``type`` column added.

    Takes and returns a pandas DataFrame; pandas is not imported here so the
    module stays importable where the test suite stubs it.
    """
    out = relationships.copy()
    out[TYPE_COLUMN] = [type_of(d) for d in out["description"]]
    return out
