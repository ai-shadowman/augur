"""PEP 440 specifiers as sets of versions, so two of them can be intersected.

``packaging.specifiers`` answers "does this one version satisfy this
specifier", which is the question an installer asks. A vulnerability join asks
a different one: does *any* version a manifest permits fall inside an
advisory's affected range, and does *every* one. Enumerating candidate versions
would need the release history from an index, which is a network call and an
evidence set of unknown completeness. Treating each specifier as a union of
intervals over the version order answers both questions from the two strings
alone.

One simplification is deliberate. PEP 440 excludes a version's pre-releases
from ``<V`` and its post-releases from ``>V``; here every version is an
ordinary point in the order, so ``<2.2.0`` contains ``2.2.0rc1``. The effect is
confined to pre- and post-release boundaries and runs one way: a declared range
can look like it admits a version it does not, so ``all-affected`` may be
reported as ``some-affected`` and never the reverse.

    >>> intersects(parse(">=2.1.8,<2.2.0"), parse("<2.1.9"))
    True
    >>> intersects(parse("<2.2.0"), parse(">=2.2.0"))
    False
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from packaging.specifiers import InvalidSpecifier, Specifier
from packaging.version import InvalidVersion, Version


class UnsupportedSpecifier(ValueError):
    """A specifier this module cannot turn into a set of versions."""


@dataclass(frozen=True, order=False)
class Bound:
    """One end of an interval. ``version`` of ``None`` is infinity on that side."""

    version: Version | None
    inclusive: bool


@dataclass(frozen=True)
class Interval:
    low: Bound
    high: Bound

    @property
    def empty(self) -> bool:
        lo, hi = self.low.version, self.high.version
        if lo is None or hi is None:
            return False
        return lo > hi or (lo == hi and not (self.low.inclusive and self.high.inclusive))


#: A set of versions, as sorted disjoint non-empty intervals.
VersionSet = tuple[Interval, ...]

_NEG_INF = Bound(None, False)
_POS_INF = Bound(None, False)
EVERYTHING: VersionSet = (Interval(_NEG_INF, _POS_INF),)
NOTHING: VersionSet = ()

#: Grype writes an exact version with a single ``=``, which PEP 440 does not
#: accept. Only a bare leading ``=`` is rewritten, never ``==``, ``>=`` or ``!=``.
_SINGLE_EQUALS = re.compile(r"^=(?!=)")


def _tighter_low(a: Bound, b: Bound) -> Bound:
    if a.version is None:
        return b
    if b.version is None:
        return a
    if a.version != b.version:
        return a if a.version > b.version else b
    return a if not a.inclusive else b


def _tighter_high(a: Bound, b: Bound) -> Bound:
    if a.version is None:
        return b
    if b.version is None:
        return a
    if a.version != b.version:
        return a if a.version < b.version else b
    return a if not a.inclusive else b


def _sort_key(interval: Interval) -> tuple:
    low = interval.low
    # -inf first; at an equal version an inclusive low starts earlier.
    return (low.version is not None, low.version or Version("0"), low.inclusive is False)


def intersect(a: VersionSet, b: VersionSet) -> VersionSet:
    pieces = (
        Interval(_tighter_low(x.low, y.low), _tighter_high(x.high, y.high))
        for x in a
        for y in b
    )
    return tuple(sorted((p for p in pieces if not p.empty), key=_sort_key))


def complement(a: VersionSet) -> VersionSet:
    gaps: list[Interval] = []
    low = _NEG_INF
    for interval in a:
        if interval.low.version is not None:
            gaps.append(Interval(low, Bound(interval.low.version, not interval.low.inclusive)))
        if interval.high.version is None:
            low = None
            break
        low = Bound(interval.high.version, not interval.high.inclusive)
    if low is not None:
        gaps.append(Interval(low, _POS_INF))
    return tuple(g for g in gaps if not g.empty)


def union(*sets: VersionSet) -> VersionSet:
    result = EVERYTHING
    for s in sets:
        result = intersect(result, complement(s))
    return complement(result)


def intersects(a: VersionSet, b: VersionSet) -> bool:
    return bool(intersect(a, b))


def contains(outer: VersionSet, inner: VersionSet) -> bool:
    """Whether every version in ``inner`` is also in ``outer``."""
    return not intersect(inner, complement(outer))


def _prefix_set(release: tuple[int, ...], epoch: int) -> VersionSet:
    """Every version whose release starts with ``release``: ``==1.5.*``."""
    stem = f"{epoch}!" if epoch else ""
    following = (*release[:-1], release[-1] + 1)
    # ``.dev0`` of a release sorts below all its pre-, post- and dev releases.
    low = Version(f"{stem}{'.'.join(map(str, release))}.dev0")
    high = Version(f"{stem}{'.'.join(map(str, following))}.dev0")
    return (Interval(Bound(low, True), Bound(high, False)),)


def _clause(spec: Specifier) -> VersionSet:
    op, raw = spec.operator, spec.version
    if op == "===":
        raise UnsupportedSpecifier(f"arbitrary equality {spec} has no version order")
    if raw.endswith(".*"):
        version = Version(raw[:-2])
        prefix = _prefix_set(version.release, version.epoch)
        return prefix if op == "==" else complement(prefix)
    version = Version(raw)
    if op == "~=":
        return intersect(
            (Interval(Bound(version, True), _POS_INF),),
            _prefix_set(version.release[:-1], version.epoch),
        )
    point = Bound(version, True)
    open_ = Bound(version, False)
    return {
        "==": (Interval(point, point),),
        "!=": (Interval(_NEG_INF, open_), Interval(open_, _POS_INF)),
        "<": (Interval(_NEG_INF, open_),),
        "<=": (Interval(_NEG_INF, point),),
        ">": (Interval(open_, _POS_INF),),
        ">=": (Interval(point, _POS_INF),),
    }[op]


def parse(constraint: str) -> VersionSet:
    """The versions a comma-separated specifier string permits.

    Accepts manifest syntax (``>=2.1.8,<2.2.0``) and Grype's (``=1.9.3``,
    ``< 5.1``). An empty string permits everything. Raises
    :class:`UnsupportedSpecifier` naming the clause it could not read, rather
    than returning a set that silently omits it.
    """
    result = EVERYTHING
    for part in constraint.split(","):
        part = _SINGLE_EQUALS.sub("==", part.strip().replace(" ", ""))
        if not part:
            continue
        try:
            result = intersect(result, _clause(Specifier(part)))
        except (InvalidSpecifier, InvalidVersion) as exc:
            raise UnsupportedSpecifier(f"cannot read {part!r} in {constraint!r}: {exc}") from exc
    return result
