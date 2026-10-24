"""Every comparison this epic has registered predictions for, by name.

:mod:`provenance.compare` holds the arithmetic and the first experiment's
predictions, positional against parser header. Each later experiment is a
second set of bounds against a second baseline, and the bounds have to exist
before the treatment's numbers do, so they are written here as constants and
selected by name rather than edited into place when the run finishes.

An :class:`Arm` keeps the bounds next to the description of the population
they were written about. ``compare`` against the wrong pair of indexes then
reads as a mislabelled run rather than as a broken or confirmed prediction.
"""
from __future__ import annotations

from dataclasses import dataclass

from .compare import EXPERIMENT, PREDICTIONS, Bound, Prediction


@dataclass(frozen=True)
class Arm:
    """One registered experiment: what was varied, on what, and what was expected."""

    name: str
    experiment: str
    labels: tuple[str, str]
    predictions: tuple[Prediction, ...]


HEADER = Arm(
    name="header",
    experiment=EXPERIMENT,
    labels=("positional", "parser"),
    predictions=PREDICTIONS,
)


# ---------------------------------------------------------------------------
# Arm A: does the one prompt left in stage one earn its place?
#
# Measured 2026-10-24: 15 of 16 bounds held, and the treatment did not beat
# ``parser``. The block below is left as registered. Its volume premise was
# false: "208 chunks" was GraphRAG's progress counter, which counts failed
# chunks, and the finished treatment wrote 25.6 entity records per chunk, not
# 21.4. The one broken bound, unjoined misattribution's delta, is that
# premise failing. Figures in ``augur-test/corpus/MANIFEST.md``.
#
# Registered 2026-10-23, before ``purpose-full-120b`` finished and before any
# reading of its graph. Stage one now makes exactly one model call per file,
# for ``Purpose:``. The ``purpose`` header is the ``parser`` header plus that
# one line, read from ``augur-test/corpus/purposes.json``, which
# ``harness/write_purposes.py`` filled by sending AUGUR's own
# ``data_from_code.yaml`` to gpt-oss-120b. Every other byte of the 116 input
# documents is identical between the arms.
#
# The decision rule is the operating rule's: the prompt stays only if the
# treatment beats ``parser``. Every prediction below says it will not, so a
# set that holds is a vote for a stage one making zero model calls.
#
# Three things were known at registration and are stated so the bounds can be
# discounted for them.
#
# The treatment's extraction cache had been read, though not its graph. Over
# the 208 chunks it finished before the daily cap stopped it, it wrote 21.4
# raw entity records per chunk against the baseline's 26.2, and 8.3
# relationship records per extract call against 9.8. None of the four metrics
# is a raw record count, but the volume moves two of them mechanically, which
# the misattribution and joined module bounds name.
#
# OpenRouter routed the arms to different providers. The baseline went
# mostly to Parasail (472 of 780 calls), the treatment mostly to DigitalOcean
# (502 of 622). That is why the treatment extracted at half the cost per
# chunk: 35% cheaper per token and 23% fewer tokens per chunk. It is also a
# confound no bound can separate from the header, since a provider can serve a
# differently quantised or configured model under the same name.
#
# No replicate of any arm exists, so run-to-run variance at fixed inputs is
# unmeasured. The tolerances below are judgement, sized against the smallest
# movements this epic has seen survive a second model (+0.064 symbol
# precision unjoined), not against a measured noise floor.
#
# And the four metrics measure naming: whether an entity's title lands on
# something the checkout declares. A purpose line carries prose, not
# identifiers, so whatever value it has would show in descriptions and the
# community reports built from them, which nothing here grades. A held set
# says the prompt does not move the graph's naming. It does not say the line
# is worthless to a reader of the report, and the result should not be quoted
# as if it did.

PURPOSE_EXPERIMENT = (
    "celery 2.5.5 full, parser vs purpose header (parser plus one model-written "
    "Purpose line per document), openai/gpt-oss-120b, unstitched; providers "
    "differed by arm"
)

PURPOSE_PREDICTIONS = (
    Prediction(
        metric="symbol_precision",
        reason=(
            "Baseline 0.548. The symbol names the model copies come from the Classes, "
            "Functions and Methods lines, which are byte-identical across the arms; the "
            "purpose line names concepts, not declarations. Predicted flat: no gain worth "
            "the call, and no loss beyond noise either, since the line adds nothing the "
            "model could misqualify."
        ),
        treatment=Bound("<", 0.60),
        delta=Bound(">=", -0.04),
    ),
    Prediction(
        metric="misattributed_symbols",
        reason=(
            "Baseline 773. Misattribution unjoined is a qualification error the purpose "
            "line gives no occasion to fix, so the share of symbols misattributed should "
            "hold and the count should follow volume. The treatment wrote about 18% fewer "
            "raw entity records per chunk, which was known at registration, so a fall to "
            "roughly 630 is predicted from volume alone and is not evidence for the line. "
            "A fall below 550 would mean the line changed how the model qualifies names."
        ),
        treatment=Bound(">=", 550),
        delta=Bound("<=", 0),
    ),
    Prediction(
        metric="invented_package_entities",
        reason=(
            "Baseline 10, with little room below it. The purpose lines name backend "
            "technologies by product rather than by import: AMQP five times, and Redis, "
            "MongoDB, Memcached, Cassandra and Tokyo Tyrant once each. A package entity "
            "minted from that prose has no import line to ground on and lands in invented. "
            "Predicted not to fall, and not to rise past twice the baseline."
        ),
        treatment=Bound("<=", 20),
        delta=Bound(">=", 0),
    ),
    Prediction(
        metric="module_precision",
        reason=(
            "Baseline 0.760. The invented modules on both 120b arms are celery 3.x and 4.x "
            "layout recalled from training, and prose about what a file does is more "
            "likely to prompt that recall than to suppress it. Predicted no gain, and no "
            "collapse, since the Package line that grounds the indexed bucket is unchanged."
        ),
        treatment=Bound(">", 0.70),
        delta=Bound("<", 0.03),
    ),
)

PURPOSE = Arm(
    name="purpose",
    experiment=PURPOSE_EXPERIMENT,
    labels=("parser", "purpose"),
    predictions=PURPOSE_PREDICTIONS,
)

#: The same comparison after ``apply_provenance.py --refinalise``, which is
#: the state the product's pipeline leaves an index in and so the reading a
#: user would get. Baseline is ``parser-full-120b-dedup``.
PURPOSE_JOINED_PREDICTIONS = (
    Prediction(
        metric="symbol_precision",
        reason=(
            "Baseline 0.785. The join re-files symbols onto the module that declares them "
            "and has no input the purpose line changes. Predicted flat."
        ),
        treatment=Bound("<", 0.83),
        delta=Bound(">=", -0.04),
    ),
    Prediction(
        metric="misattributed_symbols",
        reason=(
            "Baseline 53. What survives the join is names declared in several modules, "
            "which the join correctly declines to pick between; that is a property of the "
            "checkout, not of the header. Predicted to stay small and not to collapse."
        ),
        treatment=Bound("<=", 75),
        delta=Bound(">=", -20),
    ),
    Prediction(
        metric="invented_package_entities",
        reason=(
            "Baseline 10, the same as unjoined, because the join leaves the package "
            "layer's invented bucket alone: it read identically joined and unjoined on both "
            "120b celery arms, 10 and 11. Predicted to match the unjoined treatment, so the "
            "bounds are the unjoined ones."
        ),
        treatment=Bound("<=", 20),
        delta=Bound(">=", 0),
    ),
    Prediction(
        metric="module_precision",
        reason=(
            "Baseline 0.606, and the bound most exposed to volume. Joined, indexed is "
            "pinned at the 116 corpus files and package at 4, so precision is close to "
            "120 / (120 + invented). If invented modules fall with the 18% fewer raw "
            "records, 78 becomes about 64 and precision about 0.65 with no change in "
            "what the model believes. A rise up to 0.68 is therefore predicted, and is "
            "not evidence for the line; only a rise past it would be."
        ),
        treatment=Bound("<", 0.68),
        delta=Bound(">=", -0.03),
    ),
)

PURPOSE_JOINED = Arm(
    name="purpose-joined",
    experiment=PURPOSE_EXPERIMENT + "; both arms joined and refinalised",
    labels=("parser", "purpose"),
    predictions=PURPOSE_JOINED_PREDICTIONS,
)


ARMS = {arm.name: arm for arm in (HEADER, PURPOSE, PURPOSE_JOINED)}
