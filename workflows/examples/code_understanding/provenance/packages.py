"""Write the package layer rather than hoping the model infers it.

A package directory is implied exactly by the paths that were indexed:
``celery/worker/state.py`` being in the corpus proves ``celery/worker``
exists, and no model is needed to notice. Phase 7 measured what happens when
one is asked anyway. The positional header, which states only the path,
leaves the model to infer the directory and it does so 17 times out of 18;
the parser header states ``Package: celery.worker.state`` on the document, so
the model names that and stops inferring the level above, managing 4. Module
precision fell 0.679 to 0.606 across the two, on both models tested. The
header did not introduce an error. It removed the pressure that was producing
a correct answer by accident, which is a bad thing to be relying on either
way.

So this module stops relying on it. :func:`synthesize` is the other half of
the creation :func:`provenance.graph.ground_modules` already performs for an
indexed file that drew no entity, on the same grounds it states there: the
name is known and the type is not in doubt.

Four rules decide what gets written, and each is here because of a case that
would otherwise be wrong. Two live in
:meth:`provenance.corpus.Corpus.importable_prefixes` because they are
properties of the corpus rather than of the entity table — declared names
beat path folds, and an unfoldable extension is skipped. The two below are
properties of what the model already produced.
"""

from __future__ import annotations

import logging
from typing import Any

from .corpus import Corpus
from .grounding_report import GroundingReport

logger = logging.getLogger(__name__)

#: The type written on a synthesized row. Uppercase to match what GraphRAG's
#: extractor does to every type it parses, and in ``settings.yaml.in``'s
#: configured vocabulary, so nothing downstream sees a type it was not
#: offered.
PACKAGE_TYPE = "PACKAGE"


def _description(members: int, git_repo: str | None) -> str:
    """The row's text, assembled from counts rather than asked for.

    Deliberately thin. Everything in it is a fact the corpus states, and the
    one thing a model could add here — what the package is *for* — is not
    something this pass has any evidence about.
    """
    where = f" in the repository at {git_repo}" if git_repo else ""
    files = "file" if members == 1 else "files"
    return (
        f"Package directory{where}, proved to exist by the paths that were "
        f"indexed. Contains {members} indexed source {files}."
    )


def synthesize(
    corpus: Corpus, entities, report: GroundingReport | None = None
) -> Any:
    """Append a PACKAGE entity for every proved directory that has none.

    Call it after :func:`provenance.graph.ground_modules`, which collapses the
    rows a model did emit onto one canonical dotted spelling, and before
    :func:`provenance.graph.ground_repository`, whose ``_belongs_to`` pass
    gives every ``package``-typed row its repository edge. A row added later
    than that lands isolated.

    Two rules on top of the corpus's own:

    * a prefix any surviving row already carries is skipped, matched on the
      exact title rather than the folded key and regardless of that row's
      type. ``finalize_entities`` collapses duplicate titles by position, so
      a second row under a title the model already used would decide by
      iteration order which of the two descriptions survives.
    * a prefix that is itself a corpus key is skipped. ``celery/app/task/
      __init__.py`` declares ``celery.app.task``, which makes it an indexed
      document; writing a package beside it would duplicate the file.

    Idempotent by construction: the second call sees the first call's titles
    in the frame and writes nothing.
    """
    import pandas as pd

    if not len(corpus):
        return entities

    wanted = sorted(corpus.importable_prefixes() - corpus.keys)
    if not wanted:
        return entities

    present = set(entities["title"].astype(str)) if len(entities) else set()
    # The same key per file the prefixes were derived from, so the member
    # count of a Java package counts the files that declare it rather than
    # the ones whose Maven path happens to fold underneath it.
    members_of = [keys[-1] for _, keys in corpus.keyed_files()]
    git_repo = corpus.git_repo

    rows = []
    for key in wanted:
        title = key.upper()
        if title in present:
            continue
        members = sum(1 for k in members_of if k.startswith(f"{key}."))
        rows.append(
            {
                "title": title,
                "type": PACKAGE_TYPE,
                "description": _description(members, git_repo),
                "text_unit_ids": [],
                "frequency": 1,
            }
        )

    if not rows:
        return entities

    if report is not None:
        report.packages_created.extend(row["title"] for row in rows)
    logger.info(
        "provenance: wrote %d package entity(ies) the extraction did not "
        "emit (for example %s)", len(rows), rows[0]["title"],
    )
    return pd.concat([entities, pd.DataFrame(rows)], ignore_index=True)
