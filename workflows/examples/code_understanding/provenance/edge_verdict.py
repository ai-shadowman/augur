"""Sort every model edge into a four-way verdict against the edge oracle.

The relationship layer's counterpart of the symbol split. Each edge the model
wrote lands in exactly one bucket:

``confirmed``    the oracle states the claimed kind, claimant to object, at
                 the keys (``exact``), between their files (``lifted``),
                 through the oracle's own INHERITS chain (``transitive``),
                 to a module enclosing the oracle's target (``ancestor``,
                 ``container``), to a name the claimant's file imports
                 through a re-export (``re-export``), or to an external
                 target the claimant's file imports (``external``).
``contradicted`` both endpoints ground and the oracle does not state it:
                 ``reversed``, ``other-type`` (the files are joined by the
                 claimed kind, from another type), ``wrong-kind``,
                 ``same-file`` or ``no-edge``.
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

#: The verdict for a row AUGUR wrote from a join (``BELONGS_TO``,
#: ``DEFINED_IN``). Not a model claim, so not judged, but kept in the table so
#: every row is accounted for.
WRITTEN = "written"

#: The side table's columns, keyed on the finalised relationship ``id``.
TABLE_COLUMNS = ("id", "source", "target", edge_types.TYPE_COLUMN,
                 edge_types.SUBJECT_COLUMN, VERDICT_COLUMN, REASON_COLUMN)

#: The contradictions grounding removes from the graph, decided 2026-10-26.
#: Each says the pair is not related the way the row claims at all.
#: ``wrong-kind`` and ``other-type`` stay, since the oracle does relate the
#: pair or its files; so do ``unchecked`` and ``out-of-reach``, which were
#: never contradictions.
DROPPED_REASONS = ("reversed", "same-file", "no-edge")

#: The side table's flag for a row grounding removed. Such a row has no
#: finalised ``id``, so it carries its description instead, which is
#: otherwise reached through the id.
DROPPED_COLUMN = "dropped"
DROPPED_ROW_COLUMNS = (*TABLE_COLUMNS[1:], "description")


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


def _inherits_into(ev: EdgeEvidence, a: str, b: str, fa: set, fb: set) -> bool:
    """Whether an oracle INHERITS edge from ``fa`` to ``fb`` runs from the claimant to ``b``.

    Matched by name and file rather than by key, because a re-export
    (``rx.Worker`` for ``rx.impl.Worker``) and a member named as the claimant
    (``BackgroundInitializer.BackgroundInitializer``, a constructor) are keys
    the oracle records no parents under. A member's enclosing types count;
    a module prefix ends the walk, since in ``celery.bin.celeryctl.Command``
    the ``celeryctl`` is the file and the class of that name is another type.
    The parent is matched on its last segment, since ``errors.py`` declares
    ``ClientError`` and ``ServerError`` and inheriting one is not the other,
    or, for an object bound by import under another name (``Command as
    CeleryCommand``), on the file the binding leads to.
    """
    target = b.rpartition(".")[2]
    landing = ev.files(b) - ev.binders[b] if b in ev.binders else set()
    names, parts = set(), a.split(".")
    for i in range(len(parts), 0, -1):
        if _module(ev, ".".join(parts[:i])) is not None:
            break
        names.add(parts[i - 1])
    return any(
        child.rpartition(".")[2] in names and ev.files(child) & fa and ev.files(parent) & fb
        and (parent.rpartition(".")[2] == target or ev.files(parent) & landing)
        for child, parents in ev.parents.items() for parent in parents
    )


def _rebinds(ev: EdgeEvidence, b: str, fa: set) -> bool:
    """Whether a file of ``fa`` binds ``b``'s name by an import that leads to ``b``.

    ``from pkg import Thing`` reaches ``pkg/impl.py``'s ``Thing`` through
    ``pkg/__init__.py``, so the oracle joins the file to the ``__init__``
    alone, and "pkg.a IMPORTS pkg.impl.Thing" would read as no edge. The
    binding's chase already reaches the declaring file. A module re-exported
    by name (``from pkg import helpers``, bound in the ``__init__`` from
    ``pkg.impl``) is the bound object itself, so it qualifies too. A claim
    about ``pkg/impl.py`` after ``from pkg import Thing`` does not: the file
    binds ``Thing``, not ``impl``, and the transitive load is not a stated
    import.
    """
    name = b.rpartition(".")[2]
    declarers = ev.files(b) - ev.binders.get(b, set())
    return any(
        f in ev.binders.get(bound := f"{ev.namespaces.get(f)}.{name}", ()) and declarers & (ev.files(bound) - {f})
        for f in fa
    )


def _states(ev: EdgeEvidence, kind: str, a: str, b: str, fa: set, fb: set,
            enclosing: bool = True) -> str | None:
    """Where the oracle states ``kind`` from ``a`` to ``b``, or ``None``.

    ``enclosing=False`` asks for the pair itself, which is what ``reversed``
    means: an ancestor or container match read backwards says nothing about
    which way the model got the edge round.

    A re-exported claimant (``pkg.Thing``) speaks for its declaring file, not
    for the ``__init__`` that binds it, whose imports are its own.
    """
    fa = fa - ev.binders.get(a, set()) or fa
    if kind in ev.pairs.get((a, b), ()):
        return "exact"
    # Between two types the files' INHERITS edge has to be the claimant's own:
    # ``class celeryctl(CeleryCommand)`` joins celeryctl.py to base.py, and
    # says nothing about ``celeryctl.Command``, which inherits from object.
    if kind in {k for x in fa for y in fb for k in ev.internal.get((x, y), ())} and (
        kind == IMPORTS or _module(ev, a) is not None or _module(ev, b) is not None or _inherits_into(ev, a, b, fa, fb)
    ):
        return "lifted"
    if kind == IMPORTS and _rebinds(ev, b, fa):
        return "re-export"
    # Kept apart from ``exact``: a reader who wants direct supertypes only
    # can drop these, and one who wants the type hierarchy keeps them.
    if kind == INHERITS and b in ev.supertypes(a):
        return "transitive"
    return _encloses(ev, kind, a, b, fa) if enclosing else None


def _bare(title: str) -> bool:
    return "." not in dotted_key(title)


def _ground(ev: EdgeEvidence, kind: str, source: str, target: str,
            subject: str) -> tuple[str | None, str | None]:
    """Both endpoints' node keys, a bare one looked up where the other uses it.

    Not where the bare one is the claim's subject: the subject's name is its
    own, and the object's scope says nothing about it. And not where the
    other's file imports the name from outside the checkout: ``try: import
    pwd`` / ``except ImportError: pwd = None`` declares a local ``pwd``, and
    "platforms IMPORTS pwd" still means the library.

    Failing scope, a bare name falls back to an exact top-level key only where
    the dialect has such a scope, and for INHERITS never onto a module, since
    a base is a type: ``Config`` is not the root ``config.py``. A bare name
    no scope looked up, the subject's included, takes the same fallback on
    the same terms, and not onto a module when another key ends in the name.
    """
    ks, kt = ev.ground(source), ev.ground(target)

    def top_level(fallback: str | None, files: set[str]) -> str | None:
        if fallback is None or not ev.global_bare(files):
            return None
        if kind == INHERITS and _module(ev, fallback) is not None:
            return None
        return fallback

    def unscoped(fallback: str | None) -> str | None:
        # No scope says which declaration a bare subject means, so a root
        # module is only one candidate: ``Config`` is ``config.py`` or
        # ``app.settings.Config``, and taking the only exact match is the
        # partial-evidence mistake. A top-level declaration is what the bare
        # name means in a language with a top level, so it stays.
        if fallback is None or (_module(ev, fallback) is not None and ev.named_elsewhere(fallback)):
            return None
        return top_level(fallback, ev.files(fallback))

    def scoped(bare: str, key: str, title: str, fallback: str | None) -> str | None:
        files = ev.title_files(title, key)
        if ev.external_kinds(files, dotted_key(bare)):
            return fallback
        if found := ev.in_scope(dotted_key(bare), key, files, imported=kind == IMPORTS):
            return found
        return top_level(fallback, files)

    if _bare(source) and kt and subject != edge_types.SOURCE:
        return scoped(source, kt, target, ks), kt
    if _bare(source):
        ks = unscoped(ks)
    if _bare(target) and ks and subject != edge_types.TARGET:
        return ks, scoped(target, ks, source, kt)
    return ks, unscoped(kt) if _bare(target) else kt


def verdict(ev: EdgeEvidence, kind: str, source: str, target: str, subject: str) -> tuple[str, str]:
    """``(verdict, reason)`` for one edge of type ``kind``."""
    if kind not in CHECKABLE:
        return OUT_OF_REACH, "type"
    ks, kt = _ground(ev, kind, source, target, subject)
    fs, ft = ev.title_files(source, ks), ev.title_files(target, kt)
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
    # ``require "jekyll"`` loads the gem even where the file reopens
    # ``module Jekyll`` and the title grounds on it. Only the name or what is
    # under it: a required gem ``foo`` says nothing about ``Foo::Bar`` here.
    for _, _, fc, _, obj_title in orders:
        key = dotted_key(obj_title)
        if kind == IMPORTS and any(
            t == key or t.startswith(key + ".") for f in fc for t, k in ev.external.get(f, ()) if k == IMPORTS
        ):
            return CONFIRMED, "external" + unoriented
    # Ruby's ``include`` changes ancestors where the oracle does not look.
    near = any(ev.unresolved[f] for f in fs | ft) or (kind == INHERITS and any(
        ev.mixes_in(fc, title) for _, _, fc, _, title in orders))
    claimant, obj, fc, fo, _ = orders[0]
    joined = {k for x in fs for y in ft for k in ev.internal.get((x, y), set()) | ev.internal.get((y, x), set())}
    if len(orders) == 1 and _states(ev, kind, obj, claimant, fo, fc, enclosing=False):
        reason = "reversed"
    elif kind in joined:
        reason = "other-type"
    elif ev.pairs.get((ks, kt)) or ev.pairs.get((kt, ks)) or joined:
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


def assess(ev: EdgeEvidence, relationships: Any) -> Any:
    """:func:`score` over every row, with AUGUR-written rows marked rather than judged.

    Model rows are told apart by type, not by id, because the grounding pass
    runs before ``finalize_graph`` assigns ids. No model row on the measured
    corpora names an AUGUR-written type, and a merged row carrying both is
    typed by the model's claim (:data:`provenance.edge_types.PRECEDENCE`).
    """
    out = score(ev, relationships)
    written = out[edge_types.TYPE_COLUMN].isin(edge_types.AUGUR_WRITTEN)
    out.loc[written, VERDICT_COLUMN] = WRITTEN
    out.loc[written, REASON_COLUMN] = ""
    return out


def _evidence(facts: Any) -> EdgeEvidence | None:
    data = getattr(facts, "edge_evidence", None) if facts is not None else None
    return EdgeEvidence.from_dict(data) if data else None


def record(relationships: Any, facts: Any, report: Any) -> Any:
    """Put the verdict split on a :class:`GroundingReport`, or note why there is none.

    Facts written before the evidence was captured carry none. That is
    reported as the verdict not having run, never as every edge out of reach,
    which would read as a measurement. Returns the scored rows, or ``None``.
    """
    ev = _evidence(facts)
    if ev is None or relationships is None or not len(relationships):
        report.edge_verdicts = {}
        return None
    scored = assess(ev, relationships)
    model = scored[scored[VERDICT_COLUMN] != WRITTEN]
    report.edge_verdicts = split(model)
    report.edge_contradictions = dict(sorted(Counter(
        model.loc[model[VERDICT_COLUMN] == CONTRADICTED, REASON_COLUMN]).items()))
    report.edges_written = len(scored) - len(model)
    return scored


def judge(relationships: Any, facts: Any, report: Any) -> Any:
    """:func:`record`, then remove the contradictions :data:`DROPPED_REASONS` names.

    The report keeps the split as the model wrote it, the drops by reason,
    and the dropped rows themselves for ``output/edge_verdicts``. The index
    is reset because ``finalize_relationships`` assigns ``combined_degree``
    positionally and misplaces it on a gapped index.
    """
    scored = record(relationships, facts, report)
    if scored is None:
        return relationships
    drop = (scored[VERDICT_COLUMN] == CONTRADICTED) & scored[REASON_COLUMN].isin(DROPPED_REASONS)
    if not drop.any():
        return relationships
    dropped = scored.loc[drop]
    report.edges_dropped = dict(sorted(Counter(dropped[REASON_COLUMN]).items()))
    report.dropped_edges = dropped[[c for c in DROPPED_ROW_COLUMNS if c in dropped.columns]].to_dict("records")
    return relationships.loc[~drop.to_numpy()].reset_index(drop=True)


def dropped_frame(like: Any, rows: Any) -> Any:
    """``rows`` (:attr:`GroundingReport.dropped_edges`) as a table flagged dropped."""
    return type(like)([{**r, DROPPED_COLUMN: True} for r in rows],
                      columns=[*DROPPED_ROW_COLUMNS, DROPPED_COLUMN])


def table(relationships: Any, facts: Any, dropped: Any = ()) -> Any:
    """The per-row side table for ``output/``, or ``None`` without evidence.

    ``dropped`` holds the rows grounding removed, which ``relationships``
    no longer has; they are appended with the flag set, so the table
    accounts for every row the model wrote.
    """
    ev = _evidence(facts)
    if ev is None or relationships is None:
        return None
    scored = assess(ev, relationships)
    out = scored[[c for c in TABLE_COLUMNS if c in scored.columns]].reset_index(drop=True)
    out[DROPPED_COLUMN] = False
    rows = list(dropped)
    if not rows:
        return out
    return type(out)(out.to_dict("records") + [{**r, DROPPED_COLUMN: True} for r in rows])


def split(scored: Any) -> dict[str, int]:
    """Verdict counts, every verdict present."""
    counts = Counter(scored[VERDICT_COLUMN])
    return {v: counts[v] for v in VERDICTS}


def precision(counts: dict[str, int]) -> float | None:
    """confirmed / (confirmed + contradicted), or ``None`` with nothing checked."""
    checked = counts[CONFIRMED] + counts[CONTRADICTED]
    return counts[CONFIRMED] / checked if checked else None
