"""Write deterministic facts into the graph instead of asking a model for them back.

A version constraint and a git URL are already known exactly, before any model
runs. Passing one into an LLM and asking for it back unchanged turns a certainty
into a probability: do it often enough and some fraction comes back reworded,
reordered or dropped. The measured rate on celery 2.5.5 was 3 of 16 constraints
surviving. No prompt fixes that, because the failure is not a misunderstanding.

So this module stops asking. ``facts`` parsed the constraint out of the manifest
and read the remote out of git; this joins that record to the finished entity
table by normalised name and writes the authoritative text in. What the model
contributes is the part only it can: what the dependency is for, how it is used,
what breaks without it. What the manifest states, the manifest supplies.

The stitch is idempotent. It strips any ``Version:`` field already present in a
description before appending its own, so running it twice leaves the same text
as running it once, and a re-indexed graph can be stitched again without
accumulating duplicates.

One limit worth stating plainly: a description that paraphrases a version in
prose ("required at version 0.7.0 or above") keeps that prose. Detecting every
rephrasing is the same unreliable problem this module exists to avoid, so the
authoritative field is appended alongside it rather than attempting a rewrite.

Position in the pipeline matters. Communities, community reports and entity
description embeddings are all built from this graph, so a stitch that runs
after them leaves the reports and the vector store under ``lancedb`` describing
text that no longer exists. :mod:`provenance.pipeline` registers the join as a
GraphRAG workflow ahead of all three, which is the position to prefer.

:func:`apply` remains for stitching a finished output directory, where that
drift is unavoidable. It is reported rather than hidden: the report names every
vector store the rewrite invalidated.
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .models import Layer, Package, RepoFacts, distribution_name, normalize_name

logger = logging.getLogger(__name__)

#: Entity types that name the repository itself, matched case-insensitively.
REPOSITORY_TYPES = frozenset({"repository"})

#: A ``Version:`` field and everything to the end of that sentence. Matches what
#: this module writes and what a model writes unprompted, so one pass removes
#: either before the authoritative field is appended.
VERSION_FIELD = re.compile(r"\s*Version:\s*[^\n]*?(?=$|\n)", re.IGNORECASE)

#: The sentence carrying the repository remote and slug. The slug clause is
#: optional so one pass removes a sentence written before the slug was stitched
#: as well as one written after, which keeps older graphs re-stitchable.
REPO_SENTENCE = re.compile(
    r"\s*Repository hosted at\s+\S+?(?:,\s*slug\s+\S+?)?\.?(?=$|\s|\n)",
    re.IGNORECASE,
)

#: The runtime sentence, written by this module and also by the model under
#: the older prompt. Removed before the authoritative one is appended so the
#: two spellings cannot accumulate.
#:
#: The terminator is a period followed by a space or the end of the text, not
#: any period: a runtime is spelled "Python 2.7", so stopping at the first
#: period would leave "7." stranded at the front of the description.
RUNTIME_SENTENCE = re.compile(
    r"\s*Runtime stack:\s*.*?(?:\.(?=\s|$)|$)", re.IGNORECASE
)


@dataclass(frozen=True)
class Change:
    """One description rewritten, with enough context to audit the rewrite."""

    title: str
    entity_type: str
    field_name: str
    #: What the model produced, truncated. Kept so a reviewer can see whether
    #: the stitch corrected a paraphrase or filled a gap.
    before: str
    after: str
    source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __str__(self) -> str:
        return f"{self.title}: {self.field_name} <- {self.after!r} (from {self.source})"


@dataclass
class StitchReport:
    """What a stitch pass changed, and what it could not reach."""

    changes: list[Change] = field(default_factory=list)
    #: Declared packages with a constraint that have no entity to stitch onto.
    #: A missing entity is an extraction failure and no join can repair it.
    unmatched: list[str] = field(default_factory=list)
    #: Facts deliberately not written, with the reason. A fact this stage
    #: declined to place is worth more as a visible gap than as a guess.
    skipped: list[str] = field(default_factory=list)
    #: Vector stores whose embeddings no longer match the stitched text.
    stale_embeddings: list[str] = field(default_factory=list)
    #: ``type -> count`` for entities carrying a type the run does not
    #: configure, the empty string included. Reported and not corrected:
    #: the configured list says which types were offered, not which one a
    #: given entity should have had, so retyping would be a guess. On the
    #: restored celery run this is 67 blanks and 5 ``variable``.
    off_vocabulary: dict[str, int] = field(default_factory=dict)
    entities: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "entities": self.entities,
            "changed": len(self.changes),
            "unmatched": list(self.unmatched),
            "skipped": list(self.skipped),
            "stale_embeddings": list(self.stale_embeddings),
            "off_vocabulary": dict(self.off_vocabulary),
            "changes": [c.to_dict() for c in self.changes],
        }


def source_ordered_constraint(package: Package) -> str | None:
    """The constraint as the manifest wrote it, not as the parser sorted it.

    ``pip-requirements-parser`` sorts specifier clauses, so a file reading
    ``kombu>=2.1.8,<2.2.0`` yields the constraint ``<2.2.0,>=2.1.8``. Both are
    the same set of versions, but only one of them is what a reader will find
    when they open the file, so the declared line is preferred where it exists.
    """
    line = package.declared_line
    if line:
        stripped = line.strip()
        # "kombu>=2.1.8,<2.2.0" -> ">=2.1.8,<2.2.0"; an extras or marker suffix
        # is left alone, since dropping it would change what was declared.
        if stripped.lower().startswith(package.name.lower()):
            remainder = stripped[len(package.name):].strip()
            if remainder and remainder[0] in "<>=!~":
                return remainder
    return package.constraint


def version_text(packages: list[Package]) -> str | None:
    """Build the authoritative version field for one distribution.

    A project may declare the same distribution differently in different
    manifests, which is a fact about the project rather than a contradiction to
    resolve. Distinct constraints are therefore all reported, each attributed to
    the files that state it; a single constraint is written bare.
    """
    by_constraint: OrderedDict[str, list[str]] = OrderedDict()
    for package in packages:
        constraint = source_ordered_constraint(package)
        if not constraint:
            continue
        by_constraint.setdefault(constraint, [])
        if package.source_file not in by_constraint[constraint]:
            by_constraint[constraint].append(package.source_file)

    if not by_constraint:
        return None
    if len(by_constraint) == 1:
        return next(iter(by_constraint))
    return "; ".join(
        f"{constraint} ({', '.join(sorted(files))})"
        for constraint, files in by_constraint.items()
    )


def declared_by_name(facts: RepoFacts) -> dict[str, list[Package]]:
    """Group constrained package-layer declarations by normalised name."""
    grouped: dict[str, list[Package]] = {}
    for package in facts.packages:
        if package.layer is not Layer.PACKAGE or not package.constraint:
            continue
        grouped.setdefault(normalize_name(package.name), []).append(package)
    return grouped


#: The type a declared distribution should carry. The prompt's flat
#: ``dependency`` was split into ``library`` and ``stdlib_module``, and
#: ``settings.yaml.in`` now configures ``[module, class, function, package,
#: library, stdlib_module]``. A manifest entry is a third-party distribution by
#: definition, so ``library`` is the half of that split it belongs to.
#: Retyping to ``dependency`` wrote a type the run does not configure: on the
#: short-prompt run that was 254 entities carrying a type nothing downstream
#: knows about.
DEPENDENCY_TYPE = "library"

#: Types a declared distribution is demonstrably not. A manifest entry and
#: "ships with the language runtime" cannot both be true, and the manifest is
#: the one that was read rather than inferred. celery 2.5.5 pins
#: ``multiprocessing==2.6.2.1`` and ``importlib==1.0.2``, both Python 2.5
#: backports, and the model typed both as standard library because that is
#: what those names are on any interpreter it knows.
CONTRADICTED_BY_MANIFEST = frozenset({"stdlib_module"})

#: Types this pipeline writes itself rather than asking the model for, so they
#: are legitimate whatever ``entity_types`` offers. ``repository`` is created
#: by :mod:`provenance.graph` from the header slug; ``module`` and ``package``
#: are written there too, onto files and directories the corpus proves exist.
JOIN_CREATED_TYPES = frozenset({"repository", "module", "package"})


def declared_names(facts: RepoFacts) -> dict[str, Package]:
    """Every package-layer declaration by normalised name, constrained or not.

    Separate from :func:`declared_by_name`, which exists to write versions and
    so needs a constraint to write. Retyping needs only the fact that a
    manifest names the distribution at all.
    """
    return {
        normalize_name(p.name): p
        for p in facts.packages
        if p.layer is Layer.PACKAGE
    }


def _with_version(description: str, version: str) -> str:
    """Replace any version field in ``description`` with the authoritative one."""
    body = VERSION_FIELD.sub("", description).rstrip()
    if body and not body.endswith((".", "!", "?")):
        body += "."
    return f"{body} Version: {version}".strip()


def repository_sentence(
    url: str, slug: str | None = None, runtime: str | None = None
) -> str:
    """The authoritative identity sentence for a repository entity.

    The slug is written in for the same reason the URL is. AUGUR generates it,
    puts it in every document header and uses it as the key tying the graph to
    the rest of the pipeline, so there is nothing for a model to decide. It
    matters more than the URL here: GraphRAG uppercases every entity title, so
    a description is the only place a slug can survive in its real case.

    The runtime follows, in the sentence form the extraction prompt used to
    ask for, so a graph built either way reads the same.
    """
    sentence = f"Repository hosted at {url}, slug {slug}." if slug else f"Repository hosted at {url}."
    if runtime:
        sentence += f" Runtime stack: {runtime}."
    return sentence


def _with_repository(description: str, sentence: str) -> str:
    """Replace any repository and runtime sentence with ``sentence``."""
    body = RUNTIME_SENTENCE.sub("", REPO_SENTENCE.sub("", description)).strip()
    if body and not body.endswith((".", "!", "?")):
        body += "."
    return f"{body} {sentence}".strip()


def count_off_vocabulary(types, entity_types) -> dict[str, int]:
    """Tally entities whose type is outside the run's configured vocabulary.

    ``entity_types`` is the list the extraction prompt was given, so a type
    outside it is one the model was never offered and nothing downstream is
    built to read. A blank type counts: it is the commonest case by far, 67 of
    651 on the restored celery run, and a count is the only honest thing to
    report, since the configured list says what was offered and not what the
    entity should have been.
    """
    wanted = {str(t).strip().lower() for t in entity_types if str(t).strip()}
    if not wanted or types is None:
        return {}
    counts: dict[str, int] = {}
    for value in types:
        name = str(value).strip().lower()
        if name in wanted or name in JOIN_CREATED_TYPES:
            continue
        counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items()))


def stitch_entities(
    facts: RepoFacts, entities, entity_types: Sequence[str] | None = None,
) -> tuple[Any, StitchReport]:
    """Return a copy of the entity table with deterministic facts written in.

    Pure with respect to the filesystem, so the join can be tested without
    building an index. Matching is by title alone and ignores the entity type:
    a dependency misfiled as a standard library module is still that
    dependency, and withholding its declared version would hide one defect
    behind another. The type is recorded on each change so the mismatch stays
    visible.

    ``entity_types`` is the run's configured vocabulary, used only to report
    how many entities fall outside it. Omitting it skips the check rather than
    assuming a default, since the wrong vocabulary would make every entity
    look off-vocabulary.
    """
    frame = entities.copy()
    report = StitchReport(entities=len(frame))
    if "description" not in frame:
        frame["description"] = ""
    descriptions = frame["description"].fillna("").astype(str)
    types = frame["type"].fillna("").astype(str) if "type" in frame else None

    if entity_types:
        report.off_vocabulary = count_off_vocabulary(types, entity_types)

    grouped = declared_by_name(facts)
    declared = declared_names(facts)
    matched: set[str] = set()
    stitch_repo = bool(facts.git_repo) and not facts.multi_repo
    if facts.git_repo and facts.multi_repo:
        report.skipped.append(
            "git_repo: the index spans several repositories and these facts "
            "describe one, so no repository entity can be identified"
        )

    for position, (index, title) in enumerate(frame["title"].items()):
        key = distribution_name(str(title))
        entity_type = str(types.iloc[position]) if types is not None else ""
        before = descriptions.iloc[position]

        # A manifest naming the distribution settles the layer, so the type is
        # corrected rather than merely reported. Narrow on purpose: only types
        # the manifest directly contradicts are touched, because a function
        # entity that happens to share a distribution's name is not a
        # dependency and retyping it would invent a defect to fix another.
        if key in declared and entity_type.strip().lower() in CONTRADICTED_BY_MANIFEST:
            frame.at[index, "type"] = DEPENDENCY_TYPE
            report.changes.append(
                Change(
                    title=str(title),
                    entity_type=entity_type,
                    field_name="type",
                    before=entity_type,
                    after=DEPENDENCY_TYPE,
                    source=declared[key].source_file,
                )
            )

        if key in grouped:
            matched.add(key)
            version = version_text(grouped[key])
            if version:
                after = _with_version(before, version)
                if after != before:
                    frame.at[index, "description"] = after
                    report.changes.append(
                        Change(
                            title=str(title),
                            entity_type=entity_type,
                            field_name="version",
                            before=before[:160],
                            after=f"Version: {version}",
                            source=", ".join(
                                sorted({p.source_file for p in grouped[key]})
                            ),
                        )
                    )
                before = after

        # Only safe in a single-repository index. ``facts`` describes one
        # checkout, so in a multi-repo graph there is no way to tell which
        # repository entity this URL belongs to, and writing it to all of them
        # would attach the wrong remote to every repository but one.
        if stitch_repo and entity_type.strip().lower() in REPOSITORY_TYPES:
            sentence = repository_sentence(facts.git_repo, facts.git_slug, facts.runtime)
            after = _with_repository(before, sentence)
            if after != before:
                frame.at[index, "description"] = after
                report.changes.append(
                    Change(
                        title=str(title),
                        entity_type=entity_type,
                        field_name="git_identity",
                        before=before[:160],
                        after=sentence,
                        source="git config remote.origin.url",
                    )
                )

    report.unmatched = sorted(
        f"{packages[0].name} ({packages[0].source_file})"
        for key, packages in grouped.items()
        if key not in matched
    )
    return frame, report


def apply(
    facts: RepoFacts,
    output_dir: str | Path,
    backup: bool = True,
    dry_run: bool = False,
) -> StitchReport:
    """Stitch a finished GraphRAG output directory in place.

    The original table is copied to ``entities.parquet.orig`` on the first run
    and never overwritten afterwards, so repeated stitches stay reversible back
    to what the model actually produced.
    """
    import pandas as pd

    output_dir = Path(output_dir)
    path = output_dir / "entities.parquet"
    if not path.exists():
        raise FileNotFoundError(f"no entity table at {path}; was the index built?")

    frame, report = stitch_entities(facts, pd.read_parquet(path))
    report.stale_embeddings = _embedding_stores(output_dir) if report.changes else []

    if dry_run or not report.changes:
        return report

    original = output_dir / "entities.parquet.orig"
    if backup and not original.exists():
        original.write_bytes(path.read_bytes())
        logger.info("kept the pre-stitch table at %s", original)
    frame.to_parquet(path, index=False)
    logger.info("stitched %d description(s) in %s", len(report.changes), path)
    return report


def _embedding_stores(output_dir: Path) -> list[str]:
    """Vector stores built from entity descriptions, which a stitch invalidates."""
    lancedb = Path(output_dir) / "lancedb"
    if not lancedb.is_dir():
        return []
    return sorted(
        str(child.relative_to(output_dir))
        for child in lancedb.iterdir()
        if "entity-description" in child.name
    )


def render(report: StitchReport, limit: int = 20) -> str:
    """Human-readable summary, for a pipeline log or a terminal."""
    lines = [
        f"entities:    {report.entities}",
        f"stitched:    {len(report.changes)}",
    ]
    if report.unmatched:
        lines.append(
            f"unmatched:   {len(report.unmatched)} declared constraint(s) with no entity"
        )
        lines.extend(f"  {name}" for name in report.unmatched[:limit])
    if report.off_vocabulary:
        total = sum(report.off_vocabulary.values())
        lines.append(
            f"off-vocabulary: {total} entity(ies) typed outside entity_types"
        )
        lines.extend(
            f"  {name or '(blank)'}: {count}"
            for name, count in report.off_vocabulary.items()
        )
    for reason in report.skipped:
        lines.append(f"skipped:     {reason}")
    for change in report.changes[:limit]:
        lines.append(f"  {change}")
    if len(report.changes) > limit:
        lines.append(f"  ... and {len(report.changes) - limit} more")
    if report.stale_embeddings:
        lines.append(
            "\nthese vector stores were built from the pre-stitch text and are now stale:"
        )
        lines.extend(f"  {name}" for name in report.stale_embeddings)
        lines.append("re-run the embedding step, or stitch before it next time.")
    return "\n".join(lines)
