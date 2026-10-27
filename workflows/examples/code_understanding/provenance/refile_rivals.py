"""Whether the checkout could have refiled a symbol somewhere else.

``graph._corrected_owner`` moves a misattributed symbol onto the one indexed
module that declares it. The indexed corpus is usually a subset, so a
namesake unique inside it is only unique evidence once the whole checkout has
failed to offer another. The cluster smoke refiled ``abstract.Component``
onto ``celery.worker.consumer.Component``, the only ``Component`` among its
18 documents, while the real base, ``celery.abstract.Component``, was not
indexed.
"""

from __future__ import annotations

from collections.abc import Mapping

from . import declarations


def _could_land(owner: str, key: str, symbol: str, relative: bool) -> bool:
    """A relative title lands only on a module ending in the path it spells."""
    spelled = key[: -len(symbol)].strip(". ")
    return not (relative and spelled) or f".{owner}".endswith(f".{spelled}")


def rival_elsewhere(
    key: str, symbol: str, owners: set[str], checkout_index: Mapping[str, set[str]], relative: bool = False,
) -> bool:
    """Whether a module outside ``owners`` could equally be what ``key`` names.

    Two ways. The checkout declares the same ``symbol`` in another module; or
    it declares a longer suffix of ``key`` than the corpus does, which is
    more specific evidence than the landing (``AMQShell.default`` is a
    method in ``celery.bin.camqadm``, not ``celery.worker.strategy.default``).
    Either way a rival must be one the landing's own rules would accept.
    """
    longer = declarations.attribution_of(key, checkout_index)
    if (
        longer is not None and len(longer[0]) > len(symbol)
        and any(_could_land(o, key, longer[0], relative) for o in longer[1])
    ):
        return True
    rivals = set(checkout_index.get(symbol, ())) - owners
    return any(_could_land(o, key, symbol, relative) for o in rivals)
