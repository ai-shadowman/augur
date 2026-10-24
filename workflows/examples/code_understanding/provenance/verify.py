"""Check a finished GraphRAG index against facts established before the run.

Two directions, and both are needed.

Forward: every fact we proved beforehand should still be in the graph, intact.
A repository URL that AUGUR injected into every file header should come back
byte-identical, and a constraint such as ``>=2.1.8,<2.2.0`` should survive the
trip through extraction and description summarisation. When it does not, the
loss is silent today; these checks make it loud.

Inverse: everything the graph asserts about the package layer should trace back
to a manifest. This is the direction that catches invention. A model that
contributes a plausible dependency the project never declared produces an
entity with no grounding, and no amount of reading the graph will reveal that
unless something compares it against the manifests.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import declarations
from .models import (
    Finding,
    FindingKind,
    Layer,
    RepoFacts,
    VerificationReport,
    distribution_name,
    normalize_name,
)

logger = logging.getLogger(__name__)

#: Entity types that assert a third-party distribution. These are the ones the
#: inverse check holds to a manifest.
#:
#: ``package`` is deliberately absent, despite the name. The extraction prompt
#: defines it as "a fully qualified language-specific namespace or directory
#: that groups related modules", which is application structure. Counting it
#: here held the repository's own namespaces to the manifests and reported
#: each as a layer error the model never made.
#:
#: ``library`` is the configured type: ``entity_types`` is ``[module, class,
#: function, package, library, stdlib_module]``. ``dependency`` was the flat
#: type that split into ``library`` and ``stdlib_module``, and it is kept here
#: so graphs built before the split still verify.
PACKAGE_TYPES = frozenset({"library", "dependency"})

#: Entity types that assert a module shipped with the language runtime.
STDLIB_TYPES = frozenset({"stdlib_module"})

#: Python 2 stdlib modules absent from a modern interpreter's module list. The
#: repositories this pipeline targets are old, so checking only against the
#: running interpreter would flag correct answers as invented.
PY2_STDLIB = frozenset({
    "commands", "cpickle", "cstringio", "stringio", "urllib2", "urlparse",
    "httplib", "httpstate", "cookielib", "queue", "quopri", "repr", "thread",
    "dummy-thread", "copy-reg", "configparser", "socketserver", "basehttpserver",
    "simplehttpserver", "cgihttpserver", "htmlentitydefs", "htmlparser",
    "md5", "sha", "sets", "new", "exceptions", "types", "imp", "whichdb",
    "anydbm", "dbhash", "dumbdbm", "gdbm", "dbm", "bsddb", "user", "unicodedata",
})

#: Builtins and language keywords that are not modules at all. An entity typed
#: as a stdlib module with one of these names is a category error.
NOT_MODULES = frozenset({
    "isinstance", "basestring", "map", "len", "setattr", "getattr", "property",
    "staticmethod", "classmethod", "object", "dict", "list", "set", "tuple",
    "str", "int", "float", "bool", "reduce", "filter", "zip", "range", "xrange",
    "super", "type", "print", "open", "iter", "next", "repr", "hash", "id",
})


def _stdlib_names() -> frozenset[str]:
    modern = {normalize_name(n) for n in getattr(sys, "stdlib_module_names", frozenset())}
    return frozenset(modern | PY2_STDLIB)


def load_entities(output_dir: Path):
    """Read the entity table from a GraphRAG output directory."""
    import pandas as pd

    path = Path(output_dir) / "entities.parquet"
    if not path.exists():
        raise FileNotFoundError(f"no entity table at {path}; was the index built?")
    return pd.read_parquet(path)


def _text_of(frame, column: str = "description") -> str:
    """All of a column's text joined, for substring checks across the graph."""
    if column not in frame:
        return ""
    return "\n".join(frame[column].fillna("").astype(str).tolist())


def _typed(frame, types: Iterable[str]):
    """Rows whose type is in ``types``, compared case-insensitively."""
    wanted = {t.lower() for t in types}
    kinds = frame["type"].fillna("").astype(str).str.strip().str.lower()
    return frame[kinds.isin(wanted)]


def check_git_identity(facts: RepoFacts, entities) -> list[Finding]:
    """The repository URL and slug are injected into every header, so they must survive.

    Titles are searched as well as descriptions, and the match is folded to
    lower case. Both concessions are forced by GraphRAG rather than chosen:
    ``graph_extractor`` calls ``.upper()`` on every entity name before the
    table is written, so a slug can never appear in a title in its real case,
    and a model that echoes its own title carries that uppercasing into the
    description too. Holding the graph to exact case would report a defect on
    every run that no prompt could clear.
    """
    findings = []
    corpus = (_text_of(entities, "title") + "\n" + _text_of(entities)).lower()
    for label, value in (("git_repo", facts.git_repo), ("git_slug", facts.git_slug)):
        if not value:
            continue
        if value.lower() in corpus:
            continue
        # Distinguish "dropped entirely" from "present but altered", because
        # a paraphrased URL and a missing one have different causes. The stem
        # loses its .git suffix, since a model that mentions the repository by
        # name writes "celery", not "celery.git".
        stem = value.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git").lower()
        kind = FindingKind.MANGLED if stem and stem in corpus else FindingKind.MISSING
        findings.append(
            Finding(
                kind=kind,
                subject=label,
                detail=(
                    "present in altered form only"
                    if kind is FindingKind.MANGLED
                    else "absent from every entity description"
                ),
                expected=value,
            )
        )
    return findings


def constraint_renderings(package) -> list[str]:
    """Every spelling of a constraint that should count as surviving.

    The manifest parser sorts specifier clauses, so a file that says
    ``kombu>=2.1.8,<2.2.0`` yields the constraint ``<2.2.0,>=2.1.8``. A model
    reading the source emits the source ordering, so matching on the parsed
    form alone would report a loss that did not happen. Both orderings are
    accepted, as is the raw line with the distribution name stripped off,
    which is how a description usually carries it.
    """
    candidates = []
    if package.constraint:
        candidates.append(package.constraint)
        clauses = [c.strip() for c in package.constraint.split(",") if c.strip()]
        if len(clauses) > 1:
            candidates.append(",".join(reversed(clauses)))
    line = package.declared_line
    if line:
        candidates.append(line)
        # "kombu>=2.1.8,<2.2.0" -> ">=2.1.8,<2.2.0"
        stripped = line[len(package.name):].strip() if line.startswith(package.name) else ""
        if stripped:
            candidates.append(stripped)
    return [c for c in candidates if c]


def check_declared_constraints(
    facts: RepoFacts, entities, corpus=None
) -> tuple[list[Finding], dict[str, Any]]:
    """Every declared version constraint should reach the graph intact.

    Reported per declaration rather than per package, since a project can pin
    the same distribution differently in different manifests and losing either
    is a real loss.

    A declaration whose manifest never reached the corpus is held out of the
    score entirely. celery declares ``bundle>=1.1.0`` in
    ``requirements/pkgutils.txt`` and the run indexed four manifests, not that
    one, so the absent entity measures the corpus filter and says nothing
    about extraction. Scoring it would move the surviving-constraint ratio for
    reasons no prompt and no stitch can touch.
    """
    findings: list[Finding] = []
    titles = {
        distribution_name(str(t)): str(d or "")
        for t, d in zip(
            entities["title"], entities.get("description", []), strict=False
        )
    }
    indexed_paths = {f.path for f in corpus.files} if corpus is not None else None
    declared = [p for p in facts.packages if p.constraint and p.layer is Layer.PACKAGE]
    survived = 0
    unindexed = 0

    for package in declared:
        key = normalize_name(package.name)
        if indexed_paths is not None and package.source_file not in indexed_paths:
            unindexed += 1
            findings.append(
                Finding(
                    kind=FindingKind.UNINDEXED,
                    subject=package.name,
                    detail=f"declared in {package.source_file}, which this run "
                           "never indexed; no model was shown it",
                    expected=package.declared_line or package.constraint,
                    source_file=package.source_file,
                )
            )
            continue
        if key not in titles:
            findings.append(
                Finding(
                    kind=FindingKind.MISSING,
                    subject=package.name,
                    detail="declared dependency has no entity in the graph",
                    expected=package.declared_line or package.constraint,
                    source_file=package.source_file,
                )
            )
            continue
        description = titles[key]
        if any(rendering in description for rendering in constraint_renderings(package)):
            survived += 1
            continue
        findings.append(
            Finding(
                kind=FindingKind.MANGLED,
                subject=package.name,
                detail="entity exists but the declared constraint did not survive",
                expected=package.constraint,
                actual=description[:160] or "(empty description)",
                source_file=package.source_file,
            )
        )

    stats = {
        "declared_constraints": len(declared) - unindexed,
        "constraints_survived": survived,
        "constraints_unindexed": unindexed,
    }
    return findings, stats


def _root_of(key: str) -> str:
    """The distribution a dotted or slashed name belongs to.

    Models write the same module three ways: ``celery.worker.state``,
    ``celery/worker/state`` and ``CELERY.WORKER.STATE``. normalize_name folds
    dots and case but leaves the path separator alone, since a slash is not a
    PEP 503 separator. Both have to be cut here or a path-shaped name falls
    through to "invented" when it is plainly the project's own code.
    """
    return re.split(r"[-/]", key, maxsplit=1)[0]


def check_package_layer_grounded(
    facts: RepoFacts, entities
) -> tuple[list[Finding], dict[str, Any]]:
    """Every third-party entity should trace back to the repository.

    Four distinct defects fall out of one comparison, separated because they
    have different fixes. A name under the repository's own top-level packages
    is a layer error. A name whose root is a known distribution is a
    granularity error. A name the source imports but no manifest declares is
    an undeclared dependency, which is a finding about the repository rather
    than about the model. Only a name that appears nowhere at all is
    invention.
    """
    grounded = {normalize_name(p.name) for p in facts.packages}
    own = {normalize_name(m) for m in facts.own_modules}
    imported = {normalize_name(n) for n in facts.imported_names}
    stdlib = _stdlib_names()
    findings: list[Finding] = []
    counts = {"invented": 0, "undeclared": 0, "granularity": 0, "internal": 0, "grounded": 0}

    for _, row in _typed(entities, PACKAGE_TYPES).iterrows():
        title = str(row["title"])
        key = distribution_name(title)
        root = _root_of(key)

        if key in grounded:
            counts["grounded"] += 1
            continue
        if root in own or key in own:
            counts["internal"] += 1
            findings.append(
                Finding(
                    kind=FindingKind.UNGROUNDED,
                    subject=title,
                    detail=f"typed as third-party but defined in this repository (own module {root!r})",
                )
            )
            continue
        if root in grounded:
            counts["granularity"] += 1
            findings.append(
                Finding(
                    kind=FindingKind.UNGROUNDED,
                    subject=title,
                    detail=f"a symbol inside {root!r}, not a distribution; the distribution is the entity",
                )
            )
            continue
        # ``root in stdlib`` catches the dotted form: os.path is os, and a
        # model that names a submodule has still named the standard library.
        if key in stdlib or root in stdlib:
            counts["granularity"] += 1
            findings.append(
                Finding(
                    kind=FindingKind.UNGROUNDED,
                    subject=title,
                    detail="typed as third-party but ships with the language runtime",
                )
            )
            continue
        if key in imported or root in imported:
            # Real, and a finding about the repository rather than the model.
            # Optional imports behind try/except and requirements appended to
            # install_requires at runtime both land here.
            counts["undeclared"] += 1
            findings.append(
                Finding(
                    kind=FindingKind.UNDECLARED,
                    subject=title,
                    detail="imported by the source but declared in no manifest",
                )
            )
            continue
        counts["invented"] += 1
        findings.append(
            Finding(
                kind=FindingKind.UNGROUNDED,
                subject=title,
                detail="no manifest declares it and no source file imports it",
            )
        )

    return findings, {"package_layer": counts}


def check_stdlib_layer(facts: RepoFacts, entities) -> tuple[list[Finding], dict[str, Any]]:
    """Entities typed as stdlib modules should be modules, and should be stdlib."""
    stdlib = _stdlib_names()
    own = {normalize_name(m) for m in facts.own_modules}
    declared = {normalize_name(p.name) for p in facts.packages if p.layer is Layer.PACKAGE}
    findings: list[Finding] = []
    counts = {"ok": 0, "not_a_module": 0, "internal": 0, "shadowed": 0, "unknown": 0}

    for _, row in _typed(entities, STDLIB_TYPES).iterrows():
        title = str(row["title"])
        key = distribution_name(title)
        root = _root_of(key)
        # A declared distribution shadows the standard library name it shares.
        # celery 2.5.5 pins ``multiprocessing==2.6.2.1``, a backport, so the
        # entity is a third-party dependency even though ``multiprocessing``
        # is in ``sys.stdlib_module_names``. Checked before the stdlib branch,
        # which would otherwise pass it.
        if key in declared:
            counts["shadowed"] += 1
            findings.append(
                Finding(
                    kind=FindingKind.UNGROUNDED,
                    subject=title,
                    detail="typed as stdlib but declared as a dependency in a "
                           "manifest; a declared distribution shadows the "
                           "runtime module of the same name",
                )
            )
        elif key in NOT_MODULES:
            counts["not_a_module"] += 1
            findings.append(
                Finding(
                    kind=FindingKind.UNGROUNDED,
                    subject=title,
                    detail="a language builtin, not a module; it should not be an entity",
                )
            )
        elif root in own:
            counts["internal"] += 1
            findings.append(
                Finding(
                    kind=FindingKind.UNGROUNDED,
                    subject=title,
                    detail=f"typed as stdlib but defined in this repository (own module {root!r})",
                )
            )
        elif key in stdlib or root in stdlib:
            counts["ok"] += 1
        else:
            counts["unknown"] += 1
            findings.append(
                Finding(
                    kind=FindingKind.UNGROUNDED,
                    subject=title,
                    detail="not in the standard library of any supported Python",
                )
            )
    return findings, {"stdlib_layer": counts}


#: Entity types the model is asked to name with a path from a document header.
MODULE_TYPES = frozenset({"module", "package"})


def check_module_layer_grounded(
    facts: RepoFacts, entities, corpus
) -> tuple[list[Finding], dict[str, Any]]:
    """Every module entity should name a file that was actually indexed.

    AUGUR writes each file's path into that document's header, so the correct
    set of module entities is known exactly before the run. Four outcomes, and
    they have four different causes:

    ``indexed``         names a file in the corpus. Correct.
    ``package``         names a directory proved to exist by an indexed path.
    ``package_on_disk`` names a directory the checkout has but the corpus
                        cannot prove, so it is real but unevidenced by what
                        the model was shown.
    ``leaked``          names a real file that was never indexed. The model is
                        recalling the project from training rather than
                        reading the document, which is the failure that looks
                        most like success and is the reason the checkout
                        inventory is kept.
    ``invented``        names nothing in the repository at all.

    A leading-dot title is resolved before any of that. The extraction prompt
    asks for relative imports with their dots intact, so ``.STATE`` from
    ``celery/worker/heartbeat.py`` is a correct answer to the question that
    was asked, and counting it as an invention charged the model for obeying
    the prompt.
    """
    from .corpus import canonical_key, prefixes_of

    if corpus is None or not len(corpus):
        return [], {}

    indexed = corpus.keys
    prefixes = corpus.package_prefixes()
    on_disk = {canonical_key(p) for p in facts.source_files}
    on_disk_prefixes = prefixes_of(on_disk) - indexed - prefixes
    real = indexed | prefixes | on_disk | on_disk_prefixes
    paths = [f.path for f in corpus.files]

    findings: list[Finding] = []
    buckets = ("indexed", "package", "package_on_disk", "leaked", "invented")
    counts = dict.fromkeys(buckets, 0)
    resolved_count = 0
    unresolved: list[str] = []

    for _, row in _typed(entities, MODULE_TYPES).iterrows():
        title = str(row["title"])
        if declarations.is_relative(title):
            # No text_units table here, so only the corpus-wide fallback is
            # available: one candidate landing on a real path, or nothing.
            resolved = declarations.resolve_unique(title, paths, real)
            if resolved is None:
                unresolved.append(title)
            else:
                resolved_count += 1
                title = resolved.upper()
        key = canonical_key(title)

        if key in indexed:
            counts["indexed"] += 1
        elif key in prefixes:
            counts["package"] += 1
        elif key in on_disk:
            counts["leaked"] += 1
            findings.append(
                Finding(
                    kind=FindingKind.UNGROUNDED,
                    subject=title,
                    detail="names a real file that was not in the indexed "
                           "corpus; recalled from training, not read",
                )
            )
        elif key in on_disk_prefixes:
            counts["package_on_disk"] += 1
        else:
            counts["invented"] += 1
            findings.append(
                Finding(
                    kind=FindingKind.UNGROUNDED,
                    subject=title,
                    detail="names no file in the repository and no indexed document",
                )
            )

    total = sum(counts[b] for b in buckets)
    # Deliberately unchanged by ``package_on_disk``: the headline figure has a
    # published history and means "grounded in what the model was shown".
    counts["precision"] = (
        round((counts["indexed"] + counts["package"]) / total, 3) if total else None
    )
    counts["relative_resolved"] = resolved_count
    counts["relative_unresolved"] = len(unresolved)
    counts["corpus_files"] = len(corpus)
    return findings, {"module_layer": counts}


def load_documents(output_dir: Path):
    """The indexed documents table, or ``None`` if it is absent."""
    import pandas as pd

    path = Path(output_dir) / "documents.parquet"
    if not path.exists():
        logger.warning("no documents table at %s; module grounding not checked", path)
        return None
    return pd.read_parquet(path)


def load_corpus(output_dir: Path):
    """Read the document headers back, or ``None`` if the table is absent."""
    from .corpus import from_documents

    documents = load_documents(output_dir)
    return None if documents is None else from_documents(documents)


def verify_index(facts: RepoFacts, output_dir: str | Path) -> VerificationReport:
    """Run every check against a finished index and collect the result."""
    from .corpus import from_documents

    entities = load_entities(Path(output_dir))
    documents = load_documents(Path(output_dir))
    corpus = None if documents is None else from_documents(documents)
    report = VerificationReport(checked=len(entities))

    # The header is what the model was shown, so it is the only identity the
    # graph can be held to. ``facts`` derives its own slug from the git remote
    # and the two disagreed on celery, which made the verifier report a defect
    # that was its own.
    if corpus is not None and len(corpus):
        from .pipeline import reconcile

        for note in reconcile(facts, corpus):
            report.stats.setdefault("reconciled", []).append(note)

    report.findings.extend(check_git_identity(facts, entities))

    for check in (check_package_layer_grounded, check_stdlib_layer):
        findings, stats = check(facts, entities)
        report.findings.extend(findings)
        report.stats.update(stats)

    for check in (check_declared_constraints, check_module_layer_grounded):
        findings, stats = check(facts, entities, corpus)
        report.findings.extend(findings)
        report.stats.update(stats)

    # Two thirds of a run's entities are classes and functions, and until this
    # check they were the part nothing looked at. The documents themselves say
    # which names are real, so the claim is settled by reading rather than by
    # anybody's judgement.
    if documents is not None:
        findings, stats = declarations.check_symbol_layer(facts, entities, documents)
        report.findings.extend(findings)
        report.stats.update(stats)

    report.stats["entities"] = len(entities)
    return report


def render(report: VerificationReport, limit: int = 20) -> str:
    """Human-readable summary, for a pipeline log or a terminal."""
    verdict = "clean" if report.ok else f"{len(report.defects)} defect(s)"
    aside = [
        f"{len(report.of_kind(kind))} {label}"
        for kind, label in (
            (FindingKind.UNDECLARED, "undeclared dependency"),
            (FindingKind.UNINDEXED, "unindexed declaration"),
        )
        if report.of_kind(kind)
    ]
    if aside:
        verdict += f", plus {' and '.join(aside)} finding(s)"
    lines = [f"entities checked: {report.checked}", f"verdict: {verdict}"]
    constraints = report.stats.get("declared_constraints")
    if constraints:
        survived = report.stats.get("constraints_survived", 0)
        line = f"declared constraints surviving into the graph: {survived}/{constraints}"
        held_out = report.stats.get("constraints_unindexed", 0)
        if held_out:
            line += f" ({held_out} held out: manifest not indexed)"
        lines.append(line)
    for section in ("package_layer", "stdlib_layer", "module_layer", "symbol_layer"):
        counts = report.stats.get(section)
        if counts:
            body = ", ".join(f"{k}={v}" for k, v in counts.items())
            lines.append(f"{section}: {body}")
    for kind in FindingKind:
        subset = report.of_kind(kind)
        if not subset:
            continue
        lines.append(f"\n--- {kind.value} ({len(subset)}) ---")
        lines.extend(f"  {finding}" for finding in subset[:limit])
        if len(subset) > limit:
            lines.append(f"  ... and {len(subset) - limit} more")
    return "\n".join(lines)
