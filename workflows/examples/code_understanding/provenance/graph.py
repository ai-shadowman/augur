"""Fold the graph's invented structure back onto the files that were indexed.

The extraction prompt asks the model to name each module by the path in the
document header. AUGUR wrote that header, so the answer is known before the
call. Asking for it back is the transcription problem again, and on celery it
cost roughly three quarters of the module layer: 22 files indexed, 100
module-shaped entities returned, 26 of them naming a file that exists. The rest
were spellings of real files under a different separator convention
(``CELERY.WORKER.STATE`` beside ``CELERY/WORKER/STATE.PY``) or paths assembled
from two directories the model had seen in different chunks
(``celery/worker/app.py``, which is ``celery/app/base.py`` and
``celery/worker/__init__.py`` run together).

So this module stops asking. :mod:`provenance.corpus` reads the headers back,
and these functions reconcile the graph against them: duplicate spellings merge
into the header path, the repository entity takes the slug the header declared,
and ``BELONGS_TO`` is emitted from the join rather than requested in the prompt.

Everything here runs **before** ``finalize_graph``, which is what keeps it
simple. ``finalize_entities`` regenerates ``id`` and ``human_readable_id``,
computes ``degree`` and the layout, and drops duplicate titles;
``finalize_relationships`` dedupes ``(source, target)`` and recomputes
``combined_degree``. None of that has to be maintained here, and the corrected
graph is what community detection and the embeddings are built from.
"""

from __future__ import annotations

import logging
from typing import Any

from . import containment, declarations, edge_verdict, evidence, languages, packages, symbols
from .corpus import Corpus, canonical_key, dotted_key, prefixes_of
from .edge_evidence import EdgeEvidence
from .grounding_report import GroundingReport
from .tables import (
    collapse,
    document_for,
    merge_duplicate_edges,
    rewrite_edges,
    text_unit_documents,
    types_of,
)

logger = logging.getLogger(__name__)

#: Entity types that name the repository itself, compared case-insensitively.
REPOSITORY_TYPES = frozenset({"repository"})

#: Types the model is asked to spell as a path from the document header. These
#: are the ones a corpus file can claim.
MODULE_TYPES = frozenset({"module", "package", ""})

#: Types that get a ``BELONGS_TO`` edge to the repository. Classes and
#: functions are excluded because they are reachable through their parent
#: module, and dependencies because an external library belongs to nobody here.
#: This mirrors the exceptions the prompt used to state in prose.
BELONGS_TO_TYPES = frozenset({"module", "package", "config_file", "service", "variable"})

#: Weight for a generated ``BELONGS_TO``. Matches the value the prompt's own
#: example used, so generated edges are not ranked differently from the model's.
BELONGS_TO_WEIGHT = 10.0


def known_paths(corpus: Corpus, facts=None) -> set[str]:
    """Every module key something proves exists, for judging a fallback guess.

    The corpus on its own is not enough, and the celery run shows why.
    ``..ABSTRACT`` has two candidate resolutions, ``celery.abstract`` and
    ``celery.app.abstract``, and only the second was indexed, so a
    corpus-only answer looks unique and is wrong: every file that writes
    ``from ..abstract import`` lives in ``celery/worker/`` and means the
    first. With the checkout inventory both candidates are real, the name is
    correctly ambiguous, and nothing is renamed.

    ``facts`` is therefore the difference between a guess that can be checked
    and one that cannot. Without it the corpus is all there is, which is
    better than refusing every fallback but is not the same evidence.
    """
    keys = set(corpus.by_key()) | corpus.package_prefixes()
    if facts is not None and facts.source_files:
        on_disk = {canonical_key(p) for p in facts.source_files}
        keys |= on_disk | prefixes_of(on_disk)
    return keys


def resolve_relative_titles(
    corpus: Corpus, frame, rows, report: GroundingReport, text_units=None, facts=None,
) -> dict[str, str]:
    """Rewrite leading-dot titles to the absolute name the dots mean.

    The extraction prompt tells the model to keep the dots on a relative
    import, because resolving them needs the path of the document doing the
    importing and the model only ever sees a chunk. Two attempts at having it
    resolve them anyway produced ``celery.app.base.datastructures`` and
    ``src/celery/app/base.py``. So the dots arrive intact and the arithmetic
    happens here, which is the half of that promise nothing implemented.

    Resolution is a spelling correction, not a claim that the file exists.
    Where the producing document is known the answer is exact and is taken as
    it comes; the rest of this module then decides whether the resolved name
    names anything indexed.
    """
    by_document = corpus.by_document_id()
    text_unit_to_document = text_unit_documents(text_units) if len(by_document) else {}
    paths = [f.path for f in corpus.files]
    # A fallback resolution is only credible if it lands on something known to
    # exist, since without the producing document the depth is a guess and a
    # wrong guess invents a path.
    real = known_paths(corpus, facts)

    renames: dict[str, str] = {}
    has_units = "text_unit_ids" in frame
    for i in rows:
        title = str(frame.at[i, "title"])
        if not declarations.is_relative(title):
            continue

        resolved = None
        document = (
            document_for(frame.at[i, "text_unit_ids"], text_unit_to_document)
            if has_units else None
        )
        if document and (owner := by_document.get(document)):
            resolved = declarations.resolve_relative(title, owner.path)
        if resolved is None:
            resolved = declarations.resolve_unique(title, paths, real)

        if resolved is None:
            report.relative_unresolved.append(title)
            continue
        spelling = resolved.upper()
        if spelling != title:
            frame.at[i, "title"] = spelling
            renames[title] = spelling
        report.relative_resolved.append((title, spelling))
    return renames


def _drop_unfoldable(missing, report: GroundingReport):
    """Keep only the corpus files an entity could have been matched against.

    Creating an entity for a file is a repair when the file genuinely had no
    entity. It is damage when the file had one and the folding simply could
    not recognise it, because then the graph carries both and nothing says
    they are the same thing.

    Only the creation half is gated, not the matching half above. A model that
    happens to spell a ``.kt`` path exactly as the header does still grounds
    onto it, which is right: that match is evidence, not a guess. What the gate
    removes is the inference drawn from a *failure* to match, which on an
    unhandled extension carries no information at all.

    Java, TypeScript and Ruby reached this gate until their grammars landed
    and :func:`provenance.languages.source_suffixes` started stripping their
    extensions. They now fold, so they pass through here rather than being
    waved through: the condition is unchanged and the languages moved.

    One warning per distinct reason rather than per file, so a Java repository
    with five hundred source files logs the one true thing once.
    """
    keepers = []
    gated: dict[str, list[str]] = {}
    for corpus_file in missing:
        verdict = languages.classify(corpus_file.path)
        if verdict.folded:
            keepers.append(corpus_file)
            continue
        report.unsupported_files.append((corpus_file.path, verdict.reason))
        gated.setdefault(verdict.reason, []).append(corpus_file.path)

    for reason, paths in sorted(gated.items()):
        logger.warning(
            "grounding declined for %d corpus file(s): %s; no module entity "
            "created for them (for example %s)",
            len(paths), reason, paths[0],
        )
    return keepers


def ground_modules(
    corpus: Corpus,
    entities,
    relationships,
    report: GroundingReport,
    text_units=None,
    facts=None,
):
    """Reconcile module-shaped entities against the files actually indexed."""
    import pandas as pd

    by_key = corpus.by_key()
    if not by_key:
        return entities, relationships

    frame = entities.reset_index(drop=True)
    types = types_of(frame)
    module_like = types.isin(MODULE_TYPES)

    # Before anything is matched, so a resolved name meets the corpus on the
    # same terms as a name the model spelled absolutely in the first place.
    renames: dict[str, str] = resolve_relative_titles(
        corpus, frame, list(frame.index[module_like]), report, text_units, facts,
    )
    keys = frame["title"].astype(str).map(canonical_key)

    drop: list[int] = []

    # Per file rather than per key, because a file can answer to two of them
    # and collapsing each separately would leave two surviving rows under one
    # title. ``keyed_files`` is the same rule ``by_key`` applies, the way
    # round this loop needs it: a declared name two files claim, or one that
    # is another file's path fold, has already been dropped, so the row sets
    # gathered here cannot overlap. ``corpus_file.keys`` would not do, since
    # it is the raw claim before any of that is settled.
    for corpus_file, file_keys in corpus.keyed_files():
        rows = list(frame.index[module_like & keys.isin(file_keys)])
        if not rows:
            continue
        # Prefer a row already carrying the header spelling, so the surviving
        # row is the one the model got right where there was one.
        rows.sort(key=lambda i: str(frame.at[i, "title"]) != corpus_file.title)
        originals = [str(frame.at[i, "title"]) for i in rows]
        keep = collapse(frame, rows, corpus_file.title, "MODULE")
        for old in originals:
            if old != corpus_file.title:
                renames[old] = corpus_file.title
        drop.extend(i for i in rows if i != keep)
        absorbed = sorted(t for t in originals[1:])
        if absorbed:
            report.merged.append((corpus_file.title, absorbed))

    # A name that is a prefix of an indexed path is a real package directory:
    # celery/app/base.py being in the corpus proves celery/app exists. Those
    # are genuine, so they keep their place and are only retyped and given one
    # spelling. Dotted is canonical here, since a package is imported rather
    # than opened.
    for key in sorted(corpus.package_prefixes() - set(by_key)):
        rows = list(frame.index[module_like & (keys == key) & ~frame.index.isin(drop)])
        if not rows:
            continue
        originals = [str(frame.at[i, "title"]) for i in rows]
        canonical = key.upper()
        rows.sort(key=lambda i: str(frame.at[i, "title"]) != canonical)
        keep = collapse(frame, rows, canonical, "PACKAGE")
        for old in originals:
            if old != canonical:
                renames[old] = canonical
        drop.extend(i for i in rows if i != keep)
        absorbed = sorted(t for t in originals if t != canonical)
        if absorbed:
            report.merged.append((canonical, absorbed))

    # Module-shaped names the corpus cannot confirm. Left in place on purpose:
    # they carry relationships, and the corpus is a subset of the repository,
    # so "not indexed" is not the same as "does not exist". ``verify`` splits
    # these against the checkout into leakage and invention.
    confirmed = set(by_key) | corpus.package_prefixes()
    report.ungrounded_modules = sorted(
        str(frame.at[i, "title"])
        for i in frame.index[module_like]
        if keys[i] not in confirmed and i not in set(drop)
    )

    if drop:
        frame = frame.drop(index=drop).reset_index(drop=True)

    # A file with no entity at all is a recall failure, and the one thing a
    # join can repair outright: the path is known and the type is not in doubt.
    #
    # Unless the folding could not have matched it in the first place. A
    # ``.kt`` path folds to ``src.main.kotlin.com.example.foo.kt``, which no
    # entity will ever carry, so every corpus file looks missing and each one
    # gets a duplicate created beside the model's own entity for it. That is
    # how a two-file Java corpus went from 2 entities to 5 before ``.java``
    # folded. Declining here is the difference between a graph that is wrong
    # and one that says so.
    present = set(frame["title"].astype(str))
    missing = [f for f in corpus.files if f.title not in present]
    missing = _drop_unfoldable(missing, report)
    if missing:
        new_rows = pd.DataFrame([
            {
                "title": f.title,
                "type": "MODULE",
                "description": f"Defined in the repository at {f.git_repo}. "
                               f"Source file {f.path}.",
                "text_unit_ids": [],
                "frequency": 1,
            }
            for f in missing
        ])
        frame = pd.concat([frame, new_rows], ignore_index=True)
        report.created.extend(f.title for f in missing)

    relationships = rewrite_edges(relationships, renames, report)
    return frame, relationships


def ground_repository(corpus: Corpus, entities, relationships, report: GroundingReport):
    """Make the repository entity the one the headers declared, and only one.

    The model is asked to emit exactly one repository entity named with the
    slug. On celery it emitted three: the correct ``CELERY-V2-5-5`` plus
    ``UNKNOWN`` and ``UNKNOWN_REPO`` from chunks where it lost the header.
    The slug is in the header, so the right count and the right name are both
    known without asking.
    """
    import pandas as pd

    slug = corpus.git_slug
    if not slug:
        logger.warning("no slug in any document header; repository entity left alone")
        return entities, relationships

    title = slug.upper()
    frame = entities.reset_index(drop=True)
    types = types_of(frame)
    rows = list(frame.index[types.isin(REPOSITORY_TYPES)])

    sentences = [f"Repository hosted at {corpus.git_repo}."]
    if corpus.runtime:
        sentences.append(f"Runtime stack: {corpus.runtime}.")

    if rows:
        rows.sort(key=lambda i: str(frame.at[i, "title"]) != title)
        renames = {
            str(frame.at[i, "title"]): title
            for i in rows
            if str(frame.at[i, "title"]) != title
        }
        report.repository_absorbed = sorted(renames)
        keep = collapse(frame, rows, title, "REPOSITORY")
        body = str(frame.at[keep, "description"] or "")
        for sentence in sentences:
            body = body.replace(sentence, "").strip()
        frame.at[keep, "description"] = " ".join([*sentences, body]).strip()
        frame = frame.drop(index=[i for i in rows if i != keep]).reset_index(drop=True)
        relationships = rewrite_edges(relationships, renames, report)
    else:
        frame = pd.concat([frame, pd.DataFrame([{
            "title": title,
            "type": "REPOSITORY",
            "description": " ".join(sentences),
            "text_unit_ids": [],
            "frequency": 1,
        }])], ignore_index=True)
        report.created.append(title)

    report.repository = title
    relationships = _belongs_to(frame, relationships, title, report)
    return frame, relationships


def _corrected_owner(
    key: str, corpus_symbols, corpus_index, checkout, edges=None, relative: bool = False,
    bindings: frozenset[str] | set[str] = frozenset(),
) -> str | None:
    """The one indexed module that declares what ``key`` names, or ``None``.

    Four reasons to decline, and each is a case where a rename would make the
    graph worse rather than better:

    * the name has no owner in it at all, so there is nothing to correct;
    * the module the name already points at declares it, so it is right;
    * the checkout says the stated owner declares it, so the module is simply
      one this run did not index. Renaming it onto a namesake elsewhere in the
      corpus would turn recalled-but-true into filed-under-the-wrong-module;
    * the edge evidence grounds the name as written, as a name bound by
      import, and the namesake is not where that import leads.
      ``celery.bin.base.Option`` is ``optparse.make_option`` and was refiled
      onto the unrelated ``celery.app.defaults.Option``. A re-export still
      collapses onto the module that declares it, since that is one object;
    * the title was relative (``relative``; ``dotted_key`` has already
      dropped the dots) and the namesake's module does not end with the
      path it spells. ``.BASE.OPTION`` is ``from .base import Option``
      copied out of the source, so wherever the dots lead, the module is a
      ``base``, and ``celery.app.defaults`` is not one;
    * the landing is a module-level binding in a module that does not end
      with the path between the stated module and the symbol. That path is
      a class, and a binding is an alias: ``celery.app.base.Celery.tasks`` is
      not ``celery.registry.tasks = TaskRegistry()``;
    * more than one indexed module declares the name, so the right answer is
      not determined and the caller records it instead.

    The third check is the ``..ABSTRACT`` lesson applied one layer down: the
    corpus is a subset, so a unique match inside it is only unique evidence
    once the full checkout has failed to contradict it.

    Both ``declared_in`` calls are compared against ``None`` rather than read
    for truth, because ``""`` is a real answer: Ruby's inventory is keyed under
    the empty owner, so a correctly spelled Ruby title gets ``""`` back and
    truthiness would read that as undeclared and rename it.
    """
    if "." not in key or declarations.declared_in(key, corpus_symbols) is not None:
        return None
    if declarations.declared_in(key, checkout) is not None:
        return None
    found = declarations.attribution_of(key, corpus_index)
    if found is None:
        return None
    symbol, owners = found
    if len(owners) != 1:
        return ""
    owner = next(iter(owners))
    landing = f"{owner}.{symbol}" if owner else symbol
    spelled = key[: -len(symbol)].strip(". ")
    if relative and spelled and not f".{owner}".endswith(f".{spelled}"):
        return None
    stated = next((o for o, _ in reversed(list(declarations._splits(key))) if o in corpus_symbols or o in checkout), "")
    path = key[len(stated):].strip(".").rpartition(".")[0] if stated else ""
    if path and landing in bindings and not f".{owner}".endswith(f".{path}"):
        return None
    bound = edges.files(edges.ground(key)) if edges is not None else set()
    # A module's own key is a file, not a name bound by import.
    if any(canonical_key(f) in (key, f"{key}.__init__") for f in bound):
        bound = set()
    if bound and not bound & edges.files(edges.ground(landing)):
        return None
    # An owner of ``""`` is Ruby, where ``declarations.namespace_for`` prepends
    # nothing because the walker already returned the whole ``module`` chain.
    # Joining with a dot regardless would emit ``.JEKYLL.TAGS.LINK.RENDER``.
    # The symbol is then the entity's whole correct title, so the caller's
    # ``corrected != titles[i]`` is what makes this a no-op on a model that
    # already spelled it right.
    return landing.upper()


def ground_symbols(entities, relationships, report: GroundingReport, documents, facts=None):
    """Re-file class and function entities onto the module that declares them.

    The same defect as the module layer, one level down: the symbol is real
    and the owner is wrong. On the restored celery run 102 of 421 symbol
    entities were attributed to a module that does not declare them, and for
    most of those the indexed corpus names exactly one module that does. That
    is a join, not a judgement, so it happens here rather than being reported
    and left.

    What it will not do is invent an owner. A symbol declared nowhere in the
    corpus is left exactly as the model wrote it, because ``verify`` still has
    to tell leakage from invention and a rename would destroy the evidence it
    reads.
    """
    corpus_symbols = declarations.scan_corpus(documents)
    if not corpus_symbols:
        return entities, relationships

    corpus_index = declarations.owners_of(corpus_symbols)
    checkout = (facts.declared_symbols if facts else None) or {}
    edges = EdgeEvidence.from_dict(facts.edge_evidence if facts else None)
    bindings = symbols.module_bindings(documents)

    frame = entities.reset_index(drop=True)
    types = types_of(frame)
    symbol_like = types.isin(declarations.SYMBOL_TYPES)
    titles = frame["title"].astype(str)

    targets: dict[str, list[int]] = {}
    for i in frame.index[symbol_like]:
        corrected = _corrected_owner(
            dotted_key(titles[i]), corpus_symbols, corpus_index, checkout, edges,
            relative=titles[i].lstrip().startswith("."), bindings=bindings,
        )
        if corrected == "":
            report.symbols_ambiguous.append(titles[i])
        elif corrected and corrected != titles[i]:
            targets.setdefault(corrected, []).append(i)

    if not targets:
        return frame, relationships

    renames: dict[str, str] = {}
    drop: list[int] = []
    for corrected, moved in sorted(targets.items()):
        # A correction can land on a title the model also emitted correctly,
        # so the already-right row joins the group and wins the tie-break.
        group = [j for j in frame.index[symbol_like] if titles[j] == corrected] + moved
        group.sort(key=lambda j: titles[j] != corrected)
        keep = collapse(frame, group, corrected, None)
        drop.extend(j for j in group if j != keep)
        for j in moved:
            renames[titles[j]] = corrected
            report.symbols_refiled.append((titles[j], corrected))

    frame = frame.drop(index=drop).reset_index(drop=True)
    return frame, rewrite_edges(relationships, renames, report)


def _belongs_to(entities, relationships, repository: str, report: GroundingReport):
    """Emit the ``BELONGS_TO`` edges the prompt used to ask for.

    This is a join, not a judgement: the membership of an entity in the only
    repository being indexed is not something a model needs to decide.
    """
    import pandas as pd

    types = types_of(entities)
    wanted = {
        str(t) for t, k in zip(entities["title"].astype(str), types, strict=False)
        if k in BELONGS_TO_TYPES and str(t) != repository
    }
    if not wanted:
        return relationships

    # Either direction counts as already linked. The model emits the edge both
    # ways round and a duplicate pair would only be dropped later anyway.
    existing: set[str] = set()
    if relationships is not None and len(relationships):
        for source, target in zip(
            relationships["source"].astype(str),
            relationships["target"].astype(str),
            strict=False,
        ):
            if target == repository:
                existing.add(source)
            elif source == repository:
                existing.add(target)

    new = sorted(wanted - existing)
    if not new:
        return relationships

    rows = pd.DataFrame([
        {
            "source": title,
            "target": repository,
            "description": f"{title} BELONGS_TO the {repository} repository",
            "weight": BELONGS_TO_WEIGHT,
            "text_unit_ids": [],
        }
        for title in new
    ])
    report.belongs_to_added = len(new)
    if relationships is None or not len(relationships):
        return rows
    return pd.concat([relationships, rows], ignore_index=True)


def ground(
    corpus: Corpus,
    entities,
    relationships,
    text_units=None,
    facts=None,
    documents=None,
) -> tuple[Any, Any, GroundingReport]:
    """Run every reconciliation and return the corrected tables.

    ``text_units`` and ``facts`` are both optional and both feed the
    relative-import resolution. ``text_units`` ties an entity to the document
    that produced it, which settles the dots exactly; ``facts`` supplies the
    checkout inventory the fallback guess is judged against. A caller with
    neither still gets corpus-only resolution, which is weaker evidence and is
    counted the same way in the report.

    ``documents`` carries the indexed text, which is what the symbol layer is
    checked against. Without it the module layer is still reconciled and the
    symbol layer is skipped, since the declarations have to be read from
    somewhere and no other table holds them.
    """
    report = GroundingReport(
        entities_before=len(entities),
        relationships_before=0 if relationships is None else len(relationships),
    )
    if not len(corpus):
        logger.warning("no parseable document headers; graph left as extracted")
        return entities, relationships, report

    entities, relationships = ground_modules(
        corpus, entities, relationships, report, text_units, facts,
    )
    # After the collapse, so a directory the model did name is recognised and
    # not written twice; before ``ground_repository``, whose ``_belongs_to``
    # pass is what keeps a synthesized package from landing as an isolated
    # node.
    entities = packages.synthesize(corpus, entities, report)
    if documents is not None:
        entities, relationships = ground_symbols(
            entities, relationships, report, documents, facts,
        )
        relationships = containment.synthesize(entities, relationships, documents, report)
    entities, relationships = ground_repository(corpus, entities, relationships, report)
    entities, relationships = evidence.backfill(entities, relationships, documents, report)
    relationships = merge_duplicate_edges(relationships, report)
    relationships = edge_verdict.judge(relationships, facts, report)

    report.entities_after = len(entities)
    report.relationships_after = 0 if relationships is None else len(relationships)
    return entities, relationships, report


