"""Compare two finished indexes built from the same repository.

:func:`provenance.verify.verify_index` answers one question about one index:
how much of what the repository declares survived into the graph, and how much
the graph asserts that nothing supports. Phase 7 asks a second question no
single report can answer, which is whether a change to the ingest step moved
those numbers and in which direction.

That is arithmetic over two reports rather than a new check. It lives here
rather than in ``verify.py`` because that module is already past the size
guideline and a differ needs nothing from it but the dataclass.

Every metric below is read from ``report.stats`` as ``verify_index`` already
writes it. If a metric cannot be computed from a :class:`VerificationReport`
alone it does not belong here, and the honest move is to say which and why
rather than to widen ``verify_index`` to suit the comparison. ``EXCLUDED`` is
where that saying-which-and-why lives. It also carries the harder case, a
number ``verify_index`` does write and this comparison still should not quote,
because the reading would not mean what a reader takes it to mean.

The predictions are registered before the measurement they describe. Phase 7
is the only measurement in this epic whose numbers someone watches arrive, so
a prediction written afterwards is worth nothing. :func:`render` prints each
one beside what was measured, which makes a run that contradicted its
prediction impossible to read as a run that confirmed it.
"""
from __future__ import annotations

import operator
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .models import VerificationReport

#: Comparisons a bound may assert, by the symbol it renders as.
_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


@dataclass(frozen=True)
class Metric:
    """One number a comparison can move, and where to read it.

    ``population`` names the buckets whose sum is the denominator. It is not
    ``sum(counts.values())``: ``verify_index`` appends derived entries such as
    ``precision`` and ``corpus_files`` to the same dict, and summing those
    would report a population no entity belongs to. Naming the buckets also
    makes the population printable, which matters more here than usual. Every
    metric is computed over whatever entities an index happens to hold, and
    this epic has three times mistaken a figure measured on one population for
    a property of the code.
    """

    name: str
    section: str
    field: str
    higher_is_better: bool
    population: tuple[str, ...]
    description: str

    @property
    def unit(self) -> str:
        return "rate" if self.field == "precision" else "count"


@dataclass(frozen=True)
class Reading:
    """What one index said about one metric, with the population behind it."""

    metric: Metric
    value: float | int | None = None
    population: int = 0
    #: Why the value is absent. Empty when it is present.
    missing: str = ""

    def __str__(self) -> str:
        if self.missing:
            return f"unavailable ({self.missing})"
        return f"{self.value} (n={self.population})"


@dataclass(frozen=True)
class Bound:
    """One checkable claim about a number."""

    op: str
    value: float

    def holds(self, measured: float | None) -> bool | None:
        """Whether the claim survived. ``None`` when nothing was measured."""
        if measured is None:
            return None
        return _OPS[self.op](measured, self.value)

    def __str__(self) -> str:
        return f"{self.op} {self.value}"


@dataclass(frozen=True)
class Prediction:
    """What the run is expected to show, written down before it runs.

    Three optional bounds rather than one, because they fail differently and
    the difference is the finding. A wrong ``delta`` means the ingest change
    did not do what the epic claims. A right ``delta`` with a wrong
    ``treatment`` means it did, and something else is also wrong.
    """

    metric: str
    reason: str
    baseline: Bound | None = None
    treatment: Bound | None = None
    #: Bound on ``treatment - baseline``, in the metric's own units.
    delta: Bound | None = None

    def bounds(self) -> list[tuple[str, Bound]]:
        return [
            (name, bound)
            for name, bound in (
                ("baseline", self.baseline),
                ("treatment", self.treatment),
                ("delta", self.delta),
            )
            if bound is not None
        ]


@dataclass(frozen=True)
class Movement:
    """One metric, both readings, and the verdicts on every bound it carries."""

    metric: Metric
    baseline: Reading
    treatment: Reading
    prediction: Prediction | None = None
    #: ``(bound name, bound, measured, held)`` per registered bound.
    verdicts: tuple[tuple[str, Bound, float | None, bool | None], ...] = ()

    @property
    def delta(self) -> float | None:
        if self.baseline.value is None or self.treatment.value is None:
            return None
        return round(self.treatment.value - self.baseline.value, 3)

    @property
    def improved(self) -> bool | None:
        if self.delta is None or self.delta == 0:
            return None if self.delta is None else False
        return self.delta > 0 if self.metric.higher_is_better else self.delta < 0


@dataclass
class Comparison:
    """Two reports, read side by side against what was predicted of them."""

    labels: tuple[str, str]
    movements: list[Movement] = field(default_factory=list)
    #: Metrics neither report could answer, and why.
    unavailable: list[str] = field(default_factory=list)

    @property
    def broken_predictions(self) -> list[tuple[Movement, str, Bound, float | None]]:
        """Every bound that was measured and did not hold.

        The epic's standing instruction is to publish the wrong prediction,
        so this is the list the renderer leads with rather than buries.
        """
        return [
            (movement, name, bound, measured)
            for movement in self.movements
            for name, bound, measured, held in movement.verdicts
            if held is False
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "labels": list(self.labels),
            "metrics": [
                {
                    "metric": m.metric.name,
                    "unit": m.metric.unit,
                    self.labels[0]: m.baseline.value,
                    self.labels[1]: m.treatment.value,
                    f"{self.labels[0]}_population": m.baseline.population,
                    f"{self.labels[1]}_population": m.treatment.population,
                    "delta": m.delta,
                    "improved": m.improved,
                    "predictions": [
                        {"bound": name, "claim": str(bound), "measured": measured, "held": held}
                        for name, bound, measured, held in m.verdicts
                    ],
                }
                for m in self.movements
            ],
            "unavailable": list(self.unavailable),
        }


# ---------------------------------------------------------------------------
# What gets measured

SYMBOL_BUCKETS = ("declared", "misattributed", "leaked", "invented", "bare")
PACKAGE_BUCKETS = ("invented", "undeclared", "granularity", "internal", "grounded")
MODULE_BUCKETS = ("indexed", "package", "package_on_disk", "leaked", "invented")

SYMBOL_PRECISION = Metric(
    name="symbol_precision",
    section="symbol_layer",
    field="precision",
    higher_is_better=True,
    population=SYMBOL_BUCKETS,
    description="symbol entities landing on a declaration the checkout really makes",
)

MISATTRIBUTED_SYMBOLS = Metric(
    name="misattributed_symbols",
    section="symbol_layer",
    field="misattributed",
    higher_is_better=False,
    population=SYMBOL_BUCKETS,
    description="symbols that exist, qualified onto a module that does not declare them",
)

INVENTED_PACKAGES = Metric(
    name="invented_package_entities",
    section="package_layer",
    field="invented",
    higher_is_better=False,
    population=PACKAGE_BUCKETS,
    description="dependencies no manifest declares and no source file imports",
)

MODULE_PRECISION = Metric(
    name="module_precision",
    section="module_layer",
    field="precision",
    higher_is_better=True,
    population=MODULE_BUCKETS,
    description="module entities resolving to a file or package the corpus holds",
)

#: The three the epic has always quoted, plus the module layer. The module
#: layer is here because Phase 6b changed it most directly: ``module_key`` now
#: folds a package ``__init__`` onto its parent, which removed 35 duplicate
#: module entities on celery alone. A differ that could not see that would
#: miss the layer this epic moved most recently.
METRICS = (SYMBOL_PRECISION, MISATTRIBUTED_SYMBOLS, INVENTED_PACKAGES, MODULE_PRECISION)


# ---------------------------------------------------------------------------
# What deliberately is not measured

#: No :class:`VerificationReport` carries the number at all.
UNMEASURABLE = "unmeasurable"
#: A report does carry it, and the reading would be read as something it is not.
UNINFORMATIVE = "uninformative"


@dataclass(frozen=True)
class Exclusion:
    """A metric left out on purpose, kept with the argument that left it out.

    The same move ``decidability`` makes for a prompt field. A metric that
    quietly stops being quoted invites the next person to re-add it on the
    strength of the gap; one that carries a verdict has to be argued past.
    """

    name: str
    #: Keys in ``report.stats`` it would have been read from. Named so a test
    #: can check they still exist, which is what makes this a retirement
    #: rather than a note about code that has since moved.
    stats: tuple[str, ...]
    verdict: str
    reason: str


CONSTRAINT_SURVIVAL = Exclusion(
    name="declared_constraints_surviving",
    stats=("declared_constraints", "constraints_survived", "constraints_unindexed"),
    verdict=UNINFORMATIVE,
    reason=(
        "Retires the '7/7 constraints surviving' acceptance line, on three "
        "measurements. The denominator belongs to the corpus rather than to "
        "celery: the checkout declares 16 constrained dependencies across 8 "
        "manifests, and the 7 is default.txt's three plus test.txt's three "
        "plus py25.txt's one, which is whichever subset one run happened to "
        "index. The numerator measured the stitch rather than the model: 7/7 "
        "came from the stitched artifact in /tmp/stitch-run, while verifying "
        "entities.parquet directly gives 1/7, identically with the folding "
        "gate on and off. And the check is vacuous on both Phase 7 corpora, "
        "whose 116 input documents all sit under celery/ with no manifest "
        "among them, so every declaration is held out as unindexed and "
        "declared_constraints reads 0 whichever way the run goes. "
        "Under the operating rule the 1/7 is the expected reading and not a "
        "defect. A constraint is a verbatim value AUGUR already holds in "
        "facts.packages[].constraint, so asking a model to carry it through "
        "extraction and description summarisation is the echo failure the "
        "rule's first corollary names. check_declared_constraints stays in "
        "verify.py, earning its place as the stitch's own regression test: a "
        "stitch that silently stops writing constraints shows up there and "
        "nowhere else."
    ),
)

EXCLUDED = (CONSTRAINT_SURVIVAL,)


def excluded_for(name: str, exclusions=EXCLUDED) -> Exclusion | None:
    for exclusion in exclusions:
        if exclusion.name == name:
            return exclusion
    return None


# ---------------------------------------------------------------------------
# What is predicted of it, before it runs

#: The experiment these describe, named so a later reader cannot mistake the
#: population: celery 2.5.5, the ``full`` subset of 237 files, indexed twice
#: from ``~/Developer/augur-test/corpus/{positional,parser}/full/input`` with
#: ``openai/gpt-oss-20b`` through OpenRouter. Baseline is the ``positional``
#: header, which is one line naming the path. Treatment is the ``parser``
#: header, which adds ``Package:``, ``Dependencies:``, ``Classes:``,
#: ``Functions:`` and ``Methods:`` from the parsers. **No stitch step on
#: either side**: ``provenance stitch`` writes deterministic entities into a
#: finished index, which would move all four of these numbers by construction
#: and mask the only thing being measured.
EXPERIMENT = "celery 2.5.5 full, positional vs parser header, openai/gpt-oss-20b, unstitched"

PREDICTIONS = (
    Prediction(
        metric="symbol_precision",
        reason=(
            "The parser header states every declaration module-qualified, so the model "
            "copies a spelling instead of reconstructing one. The only prior is 0.390 to "
            "0.606 on this corpus, from the run that died with /tmp/stitch-run; it used a "
            "larger model and a stitched artifact, so both bounds sit below it and the "
            "delta is the claim actually being made."
        ),
        baseline=Bound("<", 0.45),
        treatment=Bound(">", 0.55),
        delta=Bound(">=", 0.15),
    ),
    Prediction(
        metric="misattributed_symbols",
        reason=(
            "Misattribution is a symbol qualified onto the wrong module, which is exactly "
            "what a header listing module and symbols together removes the occasion for. "
            "The prior is 102 to 4; header-only should be weaker than that, since the "
            "prior included stitching."
        ),
        baseline=Bound(">=", 40),
        treatment=Bound("<=", 15),
        delta=Bound("<=", -30),
    ),
    Prediction(
        metric="invented_package_entities",
        reason=(
            "The strongest of the four, and close to mechanical. ``invented`` means no "
            "manifest declares it and no source file imports it, while every name in the "
            "parser header's Dependencies section comes from the same import scanner that "
            "fills facts.imported_names. A package entity derived from a header line can "
            "therefore land in grounded, internal, granularity or undeclared, but not in "
            "invented. What is left is hallucination with no header line behind it."
        ),
        baseline=Bound(">=", 30),
        treatment=Bound("<=", 10),
        delta=Bound("<=", -20),
    ),
    Prediction(
        metric="module_precision",
        reason=(
            "The positional header names the path and the parser header also names the "
            "dotted module, which is the spelling the graph is keyed on. Predicted to "
            "improve least of the four: the path is in both headers, so the baseline has "
            "something to work from rather than nothing."
        ),
        baseline=Bound("<", 0.75),
        treatment=Bound(">", 0.85),
        delta=Bound(">=", 0.08),
    ),
)


def prediction_for(metric: str, predictions=PREDICTIONS) -> Prediction | None:
    for prediction in predictions:
        if prediction.metric == metric:
            return prediction
    return None


# ---------------------------------------------------------------------------
# Reading and comparing

def read(report: VerificationReport, metric: Metric) -> Reading:
    """One metric out of one report, or the reason it is not there."""
    counts = report.stats.get(metric.section)
    if not counts:
        return Reading(metric, missing=f"{metric.section} absent from stats")
    if metric.field not in counts:
        return Reading(metric, missing=f"{metric.section}.{metric.field} absent")

    population = sum(counts.get(bucket, 0) for bucket in metric.population)
    value = counts[metric.field]
    if value is None:
        # verify_index writes None for a rate over an empty population.
        return Reading(metric, population=population, missing="no entities in this layer")
    return Reading(metric, value=value, population=population)


def compare(
    baseline: VerificationReport,
    treatment: VerificationReport,
    labels: tuple[str, str] = ("positional", "parser"),
    metrics=METRICS,
    predictions=PREDICTIONS,
) -> Comparison:
    """Read both reports against each metric and grade the predictions."""
    result = Comparison(labels=labels)
    for metric in metrics:
        before, after = read(baseline, metric), read(treatment, metric)
        if before.missing and after.missing:
            result.unavailable.append(f"{metric.name}: {before.missing}")
            continue

        movement = Movement(metric=metric, baseline=before, treatment=after)
        prediction = prediction_for(metric.name, predictions)
        if prediction is None:
            result.movements.append(movement)
            continue

        measured = {
            "baseline": before.value,
            "treatment": after.value,
            "delta": movement.delta,
        }
        verdicts = tuple(
            (name, bound, measured[name], bound.holds(measured[name]))
            for name, bound in prediction.bounds()
        )
        result.movements.append(
            Movement(metric, before, after, prediction=prediction, verdicts=verdicts)
        )
    return result


MARKS = {True: "held", False: "BROKEN", None: "not measured"}


def render(comparison: Comparison, experiment: str = EXPERIMENT) -> str:
    """Human-readable comparison, predictions included rather than optional."""
    before_label, after_label = comparison.labels
    lines = [f"experiment: {experiment}", f"{before_label} -> {after_label}", ""]

    for movement in comparison.movements:
        # ``improved`` is False for no movement as well as for a regression, and
        # None when a side is missing, so the label reads the delta first.
        if movement.delta == 0:
            arrow = "flat"
        else:
            arrow = {True: "improved", False: "regressed", None: "not comparable"}[
                movement.improved
            ]
        lines.append(f"{movement.metric.name} ({movement.metric.unit})")
        lines.append(f"  {before_label:<12} {movement.baseline}")
        lines.append(f"  {after_label:<12} {movement.treatment}")
        lines.append(f"  delta        {movement.delta} ({arrow})")
        for name, bound, measured, held in movement.verdicts:
            lines.append(
                f"  predicted {name} {bound}, measured {measured} -> {MARKS[held]}"
            )
        lines.append("")

    for note in comparison.unavailable:
        lines.append(f"unavailable: {note}")

    broken = comparison.broken_predictions
    if broken:
        lines.append(f"--- predictions that did not hold ({len(broken)}) ---")
        for movement, name, bound, measured in broken:
            lines.append(f"  {movement.metric.name} {name}: predicted {bound}, got {measured}")
            lines.append(f"    why it was predicted: {movement.prediction.reason}")
    else:
        lines.append("every registered prediction held, which is itself worth doubting")
    return "\n".join(lines)
