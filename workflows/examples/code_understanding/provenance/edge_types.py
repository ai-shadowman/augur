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

The row's ``source`` and ``target`` carry no direction either: graphrag
builds each chunk's edges in an undirected ``nx.Graph``, so the columns come
out in node insertion order. The description keeps it, since the prompt asks
for ``<source> <TYPE> <target>``. :func:`subject_of` reads which column the
claim's subject names, one line at a time, because a per-chunk merge joins
A->B and B->A on a newline and such a row really does point both ways.

**The type field, since Relationship layer Phase 9.** An index extracted by
:mod:`provenance.typed_extract` carries a ``claims`` column: one token per
claim the model made, its type field and its direction relative to the row's
own columns (``IMPORTS>``: the source imports the target; ``CALLS<``: the
target calls the source). Where a row has a valid claim, the type and subject
are read from it and the prose is not consulted; a row without one (an
AUGUR-written row, an index from before the field, a field outside the
vocabulary) falls back to the description, so old indexes read as before.
"""

from __future__ import annotations

import re
from typing import Any

from .corpus import dotted_key

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


#: The column :mod:`provenance.typed_extract` writes, and its direction marks.
CLAIMS_COLUMN = "claims"
FORWARD, BACKWARD = ">", "<"


def claims_of(value: Any) -> list[tuple[str, str]]:
    """The valid ``(type, direction)`` claims in one ``claims`` cell.

    A cell is a list or array of tokens, or missing (``None``, NaN) on a row
    the model did not write. A field outside the prompt's vocabulary is not a
    claim: the model was offered seven types, and an eighth says nothing.
    """
    if value is None or isinstance(value, float | str):
        return []
    found = []
    for token in value:
        token = str(token)
        claimed, direction = token[:-1], token[-1:]
        if direction in (FORWARD, BACKWARD) and claimed in PRECEDENCE:
            found.append((claimed, direction))
    return found


def claimed_type(value: Any) -> str | None:
    """The most specific valid claimed type, or ``None`` with none."""
    named = {t for t, _ in claims_of(value)}
    return next((t for t in PRECEDENCE if t in named), None)


def claimed_subject(value: Any) -> str | None:
    """Which column the chosen type's claims make the subject, or ``None``."""
    chosen = claimed_type(value)
    if chosen is None:
        return None
    sides = {SOURCE if d == FORWARD else TARGET for t, d in claims_of(value) if t == chosen}
    return sides.pop() if len(sides) == 1 else BOTH


def with_type(relationships: Any) -> Any:
    """A copy of a relationships frame with the ``type`` column added.

    From the claims where a row has a valid one, else from the description.
    Takes and returns a pandas DataFrame; pandas is not imported here so the
    module stays importable where the test suite stubs it.
    """
    out = relationships.copy()
    claims = out[CLAIMS_COLUMN] if CLAIMS_COLUMN in out else [None] * len(out)
    out[TYPE_COLUMN] = [claimed_type(c) or type_of(d) for c, d in zip(claims, out["description"], strict=True)]
    return out


#: The column :func:`with_subject` adds, and the values it can hold.
SUBJECT_COLUMN = "subject"
SOURCE = "source"
TARGET = "target"
BOTH = "both"
UNKNOWN = "unknown"

# Punctuation that ends the object name in "X CALLS Y, to ..." or "... Y."
_TRAILING = ",.;:()"

# Skipped before the object: "IMPORTS the java.util.concurrent module".
_ARTICLES = frozenset({"the", "a", "an"})


def _related(name: str, column: str) -> bool:
    """Whether a name in the text plausibly spells a column, by whole segments.

    Covers the three shapes exact folding misses: a bare name for a qualified
    column (``run`` for ``celery.app.shell.shell.run``), a qualified name for
    a bare column (``FileUtils.mkdir_p`` for ``FILEUTILS``), and the reverse
    of each.
    """
    if not name or not column:
        return False
    return (
        name == column
        or column.endswith("." + name) or name.endswith("." + column)
        or name.startswith(column + ".") or column.startswith(name + ".")
    )


def _side(line: str, source: str, target: str) -> str | None:
    """Which column one line's subject names, or ``None`` if it cannot say.

    The subject is the text before the line's first type token, and the
    object the first word after it. Exact folding is tried first, subject
    then object; failing both, :func:`_related` is tried the same way and
    counts only when it fits exactly one column. The prompt makes the subject
    one of the two endpoints, so the two columns are the whole candidate set
    and a unique fit is not partial evidence. A row whose two columns fold to
    one key says nothing about direction.
    """
    match = _TOKEN.search(line)
    if match is None or source == target:
        return None
    subject = dotted_key(line[:match.start()].strip())
    rest = [w for w in line[match.end():].split() if w.lower() not in _ARTICLES]
    obj = dotted_key(rest[0].strip(_TRAILING)) if rest else ""
    for fits in (lambda a, b: a == b, _related):
        s_src, s_tgt = fits(subject, source), fits(subject, target)
        if s_src != s_tgt:
            return SOURCE if s_src else TARGET
        o_src, o_tgt = fits(obj, source), fits(obj, target)
        if o_src != o_tgt:
            return TARGET if o_src else SOURCE
    return None


def subject_of(source: str, target: str, description: str | None) -> str:
    """Which column the description makes the subject of its claim.

    :data:`SOURCE` or :data:`TARGET` when every line that says anything
    agrees, :data:`BOTH` when lines disagree, :data:`UNKNOWN` when none says
    anything, which covers every untyped row and every prose summary.
    """
    if not description:
        return UNKNOWN
    src, tgt = dotted_key(str(source)), dotted_key(str(target))
    sides = {side for line in str(description).splitlines() if (side := _side(line, src, tgt))}
    if len(sides) > 1:
        return BOTH
    return next(iter(sides), UNKNOWN)


def with_subject(relationships: Any) -> Any:
    """A copy of a relationships frame with the derived ``subject`` column added."""
    out = relationships.copy()
    claims = out[CLAIMS_COLUMN] if CLAIMS_COLUMN in out else [None] * len(out)
    out[SUBJECT_COLUMN] = [
        claimed_subject(c) or subject_of(s, t, d)
        for s, t, d, c in zip(out["source"], out["target"], out["description"], claims, strict=True)
    ]
    return out
