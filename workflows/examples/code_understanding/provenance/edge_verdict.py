"""Sort every model edge into a four-way verdict against the edge oracle.

The relationship layer's counterpart of the symbol split. Each edge the model
wrote lands in exactly one bucket:

``confirmed``    the oracle states the claimed kind, claimant to object, at
                 the keys (``exact``), between their files (``lifted``),
                 through the oracle's own INHERITS chain (``transitive``),
                 to a module enclosing the oracle's target (``ancestor``,
                 ``container``), or to an external target the claimant's
                 file imports (``external``).
``contradicted`` both endpoints ground and the oracle does not state it:
                 ``reversed``, ``wrong-kind``, ``same-file`` or ``no-edge``.
``out-of-reach`` an unchecked type, or an endpoint the checkout does not
                 declare and the claimant's file does not import. That is the
                 partial-evidence case, and it is never a contradiction.
``unchecked``    a would-be contradiction beside an unresolved oracle edge:
                 the oracle declined, so it did not say no.

Only ``IMPORTS`` and ``INHERITS`` are checkable, since only those have an
oracle. A row is oriented claimant to object by
:func:`provenance.edge_types.subject_of`; when that says ``unknown`` or
``both`` either orientation is accepted and the reason says ``unoriented``.

The rule was prototyped in ``augur-test/harness/edge_verdict.py`` and scored
there first; MANIFEST has it under "Relationship layer Phase 5
(harness-side)".
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from . import edge_types
from .corpus import canonical_key, dotted_key
from .dialects import dialect_for
from .edge_evidence import EdgeEvidence
from .edges import IMPORTS, INHERITS

CHECKABLE = (IMPORTS, INHERITS)
CONFIRMED, CONTRADICTED, OUT_OF_REACH, UNCHECKED = (
    "confirmed", "contradicted", "out-of-reach", "unchecked",
)
VERDICTS = (CONFIRMED, CONTRADICTED, OUT_OF_REACH, UNCHECKED)
INIT = "__init__"

#: The columns :func:`score` adds.
VERDICT_COLUMN, REASON_COLUMN = "verdict", "reason"


#: Whether importing ``a.b.c`` imports the package ``a`` too, per dialect.
#: Python executes every ancestor package's ``__init__`` on the way down, so
#: ``from celery.utils import x`` really does import ``celery``. Java's
#: ``import`` names a type and loads no package; a TypeScript specifier is a
#: file; Ruby's ``require`` loads one file. An unrecorded dialect raises
#: rather than defaulting either way, as ``declarations.NAMESPACE_RULES`` does.
ANCESTOR_IMPORTS = {"python": True, "java": False, "typescript": False, "ruby": False}


def imports_ancestors(path: str) -> bool:
    dialect = dialect_for(path)
    if dialect is None:
        return False
    if dialect.name not in ANCESTOR_IMPORTS:
        raise ValueError(
            f"no ancestor-import rule recorded for dialect {dialect.name!r} ({path}); "
            "add it to edge_verdict.ANCESTOR_IMPORTS with the reason"
        )
    return ANCESTOR_IMPORTS[dialect.name]


def _module(ev: EdgeEvidence, key: str) -> str | None:
    """``key`` as a module key, ``.__init__`` folded, or ``None`` if it names no file."""
    if not any(canonical_key(f) in (key, f"{key}.{INIT}") for f in ev.files(key)):
        return None
    return key.removesuffix(f".{INIT}")


def _encloses(ev: EdgeEvidence, kind: str, a: str, b: str, fa: set) -> str | None:
    """Whether ``b`` is a module enclosing what the oracle says ``a`` reaches.

    ``ancestor``: a Python file imports a module inside the package ``b``.
    ``container``: ``a`` inherits from a type declared inside the module or
    package ``b``, which names where the base lives rather than the base.
    The external rule already accepts both shapes for a target outside the
    checkout; these are the internal counterparts.
    """
    module = _module(ev, b)
    if module is None:
        return None
    inside = module + "."
    if kind == INHERITS:
        if any(t.startswith(inside) for t in ev.supertypes(a)):
            return "container"
        return None
    for f in fa:
        if imports_ancestors(f) and any(
            t.startswith(inside) for t, kinds in ev.imported_from(f) if IMPORTS in kinds
        ):
            return "ancestor"
    return None


def _states(ev: EdgeEvidence, kind: str, a: str, b: str, fa: set, fb: set,
            enclosing: bool = True) -> str | None:
    """Where the oracle states ``kind`` from ``a`` to ``b``, or ``None``.

    ``enclosing=False`` asks for the pair itself, which is what ``reversed``
    means: an ancestor or container match read backwards says nothing about
    which way the model got the edge round.
    """
    if kind in ev.pairs.get((a, b), ()):
        return "exact"
    if kind in {k for x in fa for y in fb for k in ev.internal.get((x, y), ())}:
        return "lifted"
    # Kept apart from ``exact``: a reader who wants direct supertypes only
    # can drop these, and one who wants the type hierarchy keeps them.
    if kind == INHERITS and b in ev.supertypes(a):
        return "transitive"
    return _encloses(ev, kind, a, b, fa) if enclosing else None


def _bare(title: str) -> bool:
    return "." not in dotted_key(title)


def _ground(ev: EdgeEvidence, source: str, target: str, subject: str) -> tuple[str | None, str | None]:
    """Both endpoints' node keys, a bare one looked up where the other uses it.

    Not where the bare one is the claim's subject: the subject's name is its
    own, and the object's scope says nothing about it. And not where the
    other's file imports the name from outside the checkout: ``try: import
    pwd`` / ``except ImportError: pwd = None`` declares a local ``pwd``, and
    "platforms IMPORTS pwd" still means the library.
    """
    ks, kt = ev.ground(source), ev.ground(target)

    def scoped(bare: str, key: str, fallback: str | None) -> str | None:
        files = ev.files(key)
        if ev.external_kinds(files, dotted_key(bare)):
            return fallback
        return ev.in_scope(dotted_key(bare), key, files) or fallback

    if _bare(source) and kt and subject != edge_types.SOURCE:
        ks = scoped(source, kt, ks)
    elif _bare(target) and ks and subject != edge_types.TARGET:
        kt = scoped(target, ks, kt)
    return ks, kt


def verdict(ev: EdgeEvidence, kind: str, source: str, target: str, subject: str) -> tuple[str, str]:
    """``(verdict, reason)`` for one edge of type ``kind``."""
    if kind not in CHECKABLE:
        return OUT_OF_REACH, "type"
    ks, kt = _ground(ev, source, target, subject)
    fs, ft = ev.files(ks), ev.files(kt)
    if subject == edge_types.TARGET:
        orders = [(kt, ks, ft, fs, source)]
    elif subject == edge_types.SOURCE:
        orders = [(ks, kt, fs, ft, target)]
    else:
        orders = [(ks, kt, fs, ft, target), (kt, ks, ft, fs, source)]
    unoriented = "" if len(orders) == 1 else ", unoriented"

    # One endpoint grounds: confirmed only if its file imports the other.
    for claimant, obj, fc, _, obj_title in orders:
        if claimant and not obj and kind in ev.external_kinds(fc, dotted_key(obj_title)):
            return CONFIRMED, "external" + unoriented
    if not (ks and kt):
        return OUT_OF_REACH, "ungrounded"

    for claimant, obj, fc, fo, _ in orders:
        if where := _states(ev, kind, claimant, obj, fc, fo):
            return CONFIRMED, where + unoriented
    near = any(ev.unresolved[f] for f in fs | ft)
    claimant, obj, fc, fo, _ = orders[0]
    if len(orders) == 1 and _states(ev, kind, obj, claimant, fo, fc, enclosing=False):
        reason = "reversed"
    elif ev.pairs.get((ks, kt)) or ev.pairs.get((kt, ks)) or any(
        ev.internal.get((x, y)) or ev.internal.get((y, x)) for x in fs for y in ft
    ):
        reason = "wrong-kind"
    elif fs & ft:
        reason = "same-file"
    else:
        reason = "no-edge"
    return (UNCHECKED if near else CONTRADICTED), reason


def score(ev: EdgeEvidence, relationships: Any) -> Any:
    """A copy of ``relationships`` with ``type``, ``subject``, ``verdict`` and ``reason``.

    Takes and returns a pandas DataFrame without importing pandas, as
    :mod:`provenance.edge_types` does.
    """
    out = edge_types.with_subject(edge_types.with_type(relationships))
    found = [
        verdict(ev, k, s, t, d)
        for k, s, t, d in zip(out[edge_types.TYPE_COLUMN], out["source"], out["target"],
                              out[edge_types.SUBJECT_COLUMN], strict=True)
    ]
    out[VERDICT_COLUMN] = [v for v, _ in found]
    out[REASON_COLUMN] = [r for _, r in found]
    return out


def split(scored: Any) -> dict[str, int]:
    """Verdict counts, every verdict present."""
    counts = Counter(scored[VERDICT_COLUMN])
    return {v: counts[v] for v in VERDICTS}


def precision(counts: dict[str, int]) -> float | None:
    """confirmed / (confirmed + contradicted), or ``None`` with nothing checked."""
    checked = counts[CONFIRMED] + counts[CONTRADICTED]
    return counts[CONFIRMED] / checked if checked else None
