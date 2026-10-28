"""The four-way verdict for a CALLS row, against :class:`provenance.call_evidence.CallEvidence`.

Endpoints ground exactly as an IMPORTS row's do (:func:`provenance.edge_verdict._ground`);
what the oracle is then asked differs, because a call graph has blind spots
an import graph does not.

``confirmed``    the oracle states the call: at the keys (``exact``), from a
                 declaration inside a module or class claimant (``contained``),
                 to a member of a module or class object (``container``),
                 through a decorator or a binding's initialiser (``binds``),
                 to ``C`` for ``C.__init__`` (``init``), or outside the
                 checkout (``external``).
``contradicted`` both endpoints ground, every site in the claimant settled,
                 nothing it calls imports by name, and the object is not
                 reached: ``reversed``, ``transitive`` (within three settled
                 hops), ``no-calls`` or ``no-edge``. Reported, never dropped:
                 decided 2026-10-28.
``out-of-reach`` an endpoint does not ground or sits in a file of another
                 dialect (``dialect``: the oracle reads Python only), the
                 claimant's file had no calls captured (``uncovered``: facts
                 from before the oracle, merged with newer ones), or no
                 call-site oracle can see the claim: a property read
                 (``property``), an implicit protocol call such as ``self[k]``
                 (``protocol``), an endpoint bound by assignment
                 (``binding``), or a callable the claimant passes or returns
                 without calling (``reference``; decided 2026-10-28).
``unchecked``    a site in the claimant did not settle (``unsettled``),
                 something it calls imports by name (``imports-by-name``), or
                 the object overrides the method the oracle resolved, or is
                 overridden by it (``dispatch``): true at run time, not the
                 static target.

Prototyped as ``augur-test/harness/call_reach.py``; MANIFEST "Relationship
layer Phase 8" registers the split it predicts and reads every contradiction.
"""

from __future__ import annotations

from . import edge_types
from .call_evidence import CallEvidence
from .corpus import dotted_key
from .dialects import PYTHON, dialect_for
from .edge_evidence import EdgeEvidence

CALLS = "CALLS"
CONFIRMED, CONTRADICTED, OUT_OF_REACH, UNCHECKED = (
    "confirmed", "contradicted", "out-of-reach", "unchecked",
)
INIT = "__init__"
#: Settled hops a ``transitive`` contradiction is looked for within.
TRANSITIVE_DEPTH = 3


def _reaches(cev: CallEvidence, claimant: str, obj: str) -> str | None:
    obj = cev.follow(obj)
    exact = cev.internal.get(claimant, set()) | cev.external.get(claimant, set())
    if obj in exact:
        return "exact"
    if obj.endswith("." + INIT) and obj.removesuffix("." + INIT) in exact:
        return "init"
    if obj in cev.binds.get(claimant, ()):
        return "binds"
    targets = cev.targets(claimant)
    if obj in targets:
        return "contained"
    if any(cev.declared_in(obj, t) for t in targets):
        return "container"
    return None


def _transitive(cev: CallEvidence, claimant: str, obj: str) -> bool:
    obj, frontier, seen = cev.follow(obj), set(cev.within(claimant)), set()
    for _ in range(TRANSITIVE_DEPTH):
        reached = {t for c in frontier for t in cev.internal.get(c, ())}
        if obj in reached or any(cev.declared_in(obj, t) for t in reached):
            return True
        seen |= frontier
        frontier = {c for t in reached for c in cev.within(t)} - seen
    return False


def _dispatch(ev: EdgeEvidence, cev: CallEvidence, claimant: str, obj: str) -> bool:
    owner, _, name = cev.follow(obj).rpartition(".")
    for t in cev.targets(claimant, external=False):
        # ``C()`` runs ``Base.__init__`` when C inherits it.
        if name == INIT and owner in ev.supertypes(t):
            return True
        t_owner, _, t_name = t.rpartition(".")
        if t_name == name and t_owner != owner and (
                t_owner in ev.supertypes(owner) or owner in ev.supertypes(t_owner)):
            return True
    return False


def _calls_external(cev: CallEvidence, claimant: str, title: str) -> bool:
    key = dotted_key(title)
    return any(t == key or t.startswith(key + ".") or t.endswith("." + key)
               for c in cev.within(claimant) for t in cev.external.get(c, ()))


def _blind(cev: CallEvidence, claimant: str, obj: str) -> str | None:
    name = obj.rpartition(".")[2]
    if obj in cev.properties:
        return "property"
    if name.startswith("__") and name.endswith("__") and name != INIT:
        return "protocol"
    if (claimant in cev.bindings and claimant not in cev.binds) or cev.follow(obj) in cev.bindings:
        return "binding"
    return None


def _module_key(ev: EdgeEvidence, key: str | None) -> str | None:
    from .edge_verdict import _module

    if key and key.endswith("." + INIT) and _module(ev, key) is not None:
        return key.removesuffix("." + INIT)
    return key


def _scoped(ev: EdgeEvidence, cev: CallEvidence, claimant: str | None, obj: str | None,
            title: str) -> str | None:
    """A bare object title through the claimant's own import of that name, if it has one.

    ``from logging import config`` then ``config.dictConfig()``: the claim
    means the library's ``config``, not a root ``config.py`` the global
    fallback grounds it on.
    """
    if claimant is None or obj is None or "." in dotted_key(title):
        return obj  # an ungrounded object already gets the external rule
    for f in sorted(ev.files(claimant)):
        bound = dotted_key(f"{ev.namespaces.get(f, '')}.{title}")
        if bound in cev.rebound:
            return cev.rebound[bound]
    return obj


def verdict(ev: EdgeEvidence, cev: CallEvidence, source: str, target: str, subject: str) -> tuple[str, str]:
    """``(verdict, reason)`` for one CALLS row."""
    from .edge_verdict import _ground

    ks, kt = (_module_key(ev, k) for k in _ground(ev, CALLS, source, target, subject))
    if subject == edge_types.TARGET:
        raw = [(kt, ks, target)]
    elif subject == edge_types.SOURCE:
        raw = [(ks, kt, target)]
    else:
        raw = [(ks, kt, target), (kt, ks, source)]
    unoriented = "" if len(raw) == 1 else ", unoriented"
    # A claimant spelled as a re-export (``celery.app.BaseApp``) speaks for
    # the declaration it binds; an object gets the claimant's own import.
    orders = [(cev.follow(c) if c else c, _scoped(ev, cev, c, o, title), title) for c, o, title in raw]

    for claimant, obj, obj_title in orders:
        if claimant and not obj and _calls_external(cev, claimant, obj_title):
            return CONFIRMED, "external" + unoriented
    if not (ks and kt):
        return OUT_OF_REACH, "ungrounded"
    claimants = {c for c, _, _ in orders}
    if any(c not in ev.file_of for c in claimants):
        return OUT_OF_REACH, "ungrounded"  # a re-export of something outside the checkout
    files = {f for c, o, _ in orders for k in (c, o) for f in ev.files(k)}
    if any(dialect_for(f) is not PYTHON for f in files):
        return OUT_OF_REACH, "dialect"
    if any(not ev.files(c) <= cev.files for c in claimants):
        return OUT_OF_REACH, "uncovered"
    for claimant, obj, _ in orders:
        if where := _reaches(cev, claimant, obj):
            return CONFIRMED, where + unoriented
    for claimant, obj, _ in orders:
        if _dispatch(ev, cev, claimant, obj):
            return UNCHECKED, "dispatch"
    for claimant, obj, _ in orders:
        if why := _blind(cev, claimant, obj):
            return OUT_OF_REACH, why
    for claimant, obj, _ in orders:
        # The claimant itself too: ``__reduce__`` returns a callable and makes
        # no call, so it is no caller.
        held = {t for c in {claimant, *cev.within(claimant)} for t in cev.references.get(c, ())}
        if cev.follow(obj) in held:
            return OUT_OF_REACH, "reference"
    # Absence is evidence only once every site settled, ``reversed`` included:
    # an unresolved site may be the call back.
    if any(cev.unsettled.get(c) for cl, _, _ in orders for c in {cl, *cev.within(cl)}):
        return UNCHECKED, "unsettled"
    if any(c in cev.by_name for cl, _, _ in orders for c in cev.within(cl)):
        return UNCHECKED, "imports-by-name"
    claimant, obj, _ = orders[0]
    if len(orders) == 1 and _reaches(cev, obj, claimant) in ("exact", "init"):
        return CONTRADICTED, "reversed"
    if any(_transitive(cev, cl, ob) for cl, ob, _ in orders):
        return CONTRADICTED, "transitive" + unoriented
    if len(orders) == 1 and not cev.within(claimant):
        return CONTRADICTED, "no-calls"
    return CONTRADICTED, "no-edge" + unoriented
