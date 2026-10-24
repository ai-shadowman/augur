"""Check the file paths a migration plan claims against what was indexed.

``enhanced/1`` and ``enhanced/2`` ask for ``relevant_files`` as "verbatim file
paths from source data only". Those prompts run with the index bypassed, so
the paths come from an earlier answer's prose rather than from retrieval, and
nothing downstream used to look at them. Whether a path exists is set
membership, so this module answers it rather than trusting the model.

Each entry gets one of three verdicts:

``confirmed``
    an indexed document carries this path.
``not-indexed``
    the path is a file in the checkout, but no document carries it. Kept apart
    from the next verdict because a real file outside the evidence set is the
    partial-evidence trap: graded against the index alone it would read as
    invented.
``matches-nothing``
    neither the index nor the checkout holds it.

An entry that matches nothing is further given a :class:`Shape`, read off the
entry text alone. Only a ``repo-path`` is a file the model invented inside the
repository; a ``host-path`` such as ``/etc/fstab`` names the machine rather
than the checkout, and a ``glob`` names no file at all. All three break the
"verbatim from source data" contract, but they are different failures, and a
hallucinated-path rate should count the first.

A match is exact on the repo-relative path first. Failing that, both sides are
folded with :func:`provenance.corpus.canonical_key`, so ``celery.app.base`` or
``CELERY/APP/BASE`` still resolve to ``celery/app/base.py``; such a match is
flagged ``folded``, since the prompt asked for the path verbatim and did not
get it, but the file it names is real.

The JSON is taken from the first fenced block that parses, else from the
outermost braces of the answer. An answer where neither parses is counted as
malformed, never skipped: a truncated plan is a result about the model.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from .corpus import Corpus, canonical_key

FIELD = "relevant_files"

#: Directories under a checkout that hold no source a document could carry.
_SKIP_DIRS = frozenset({".git", ".hg", ".svn"})

_FENCE = re.compile(r"```[a-zA-Z0-9_-]*[ \t]*\n(.*?)```", re.S)


class Verdict(StrEnum):
    CONFIRMED = "confirmed"
    NOT_INDEXED = "not-indexed"
    MATCHES_NOTHING = "matches-nothing"


class Shape(StrEnum):
    """What an entry that matches nothing looks like. Checked in this order."""

    NOT_A_PATH = "not-a-path"
    URL = "url"
    GLOB = "glob"
    HOST_PATH = "host-path"
    REPO_PATH = "repo-path"


_GLOB_CHARS = frozenset("*?[")


class Form(StrEnum):
    """How the plan JSON was found in the answer text."""

    FENCED = "fenced"
    BARE = "bare"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class Resolution:
    entry: str
    verdict: Verdict
    #: The path the entry resolved to. More than one where the fold is
    #: ambiguous, empty where nothing matched.
    resolved: tuple[str, ...] = ()
    folded: bool = False
    #: Set only on ``matches-nothing``.
    shape: Shape | None = None


@dataclass
class Answer:
    label: str
    form: Form
    #: Plan rows carrying the field at all, whatever its type.
    rows: int = 0
    #: Rows whose field is not a list, so no entry can be read from it.
    off_schema_rows: int = 0
    resolutions: list[Resolution] = field(default_factory=list)

    def counts(self) -> Counter:
        return Counter(r.verdict for r in self.resolutions)

    def shapes(self) -> Counter:
        return Counter(r.shape for r in self.resolutions if r.shape)


@dataclass(frozen=True)
class Evidence:
    """Every path a plan could legitimately name, sized by the checkout walk."""

    indexed: frozenset[str]
    checkout: frozenset[str]

    @classmethod
    def from_checkout(cls, corpus: Corpus, root: Path) -> Evidence:
        if corpus.multi_repo:
            raise ValueError(
                "the corpus is multi-repo; one checkout cannot size its evidence set"
            )
        root = Path(root)
        if not root.is_dir():
            raise FileNotFoundError(f"checkout {root} is not a directory")
        checkout = frozenset(_walk(root))
        indexed = frozenset(f.path for f in corpus.files)
        missing = sorted(indexed - checkout)
        if missing:
            raise ValueError(
                f"{len(missing)} of {len(indexed)} indexed paths are not files under "
                f"{root}, so it is not the checkout this index was built from: "
                + ", ".join(missing[:5])
            )
        return cls(indexed=indexed, checkout=checkout)

    def resolve(self, entry: Any) -> Resolution:
        text = normalize(entry)
        for paths, verdict in (
            (self.indexed, Verdict.CONFIRMED),
            (self.checkout, Verdict.NOT_INDEXED),
        ):
            if text in paths:
                return Resolution(str(entry), verdict, (text,))
            key = canonical_key(text)
            folded = tuple(sorted(p for p in paths if canonical_key(p) == key))
            if folded:
                return Resolution(str(entry), verdict, folded, folded=True)
        return Resolution(str(entry), Verdict.MATCHES_NOTHING, shape=shape_of(entry))


def _walk(root: Path) -> Iterator[str]:
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if path.is_file() and not _SKIP_DIRS.intersection(rel.parts):
            yield rel.as_posix()


def shape_of(entry: Any) -> Shape:
    """Classify an unmatched entry by its text, before normalization strips a
    leading slash that is the whole of the difference between host and repo."""
    if not isinstance(entry, str):
        return Shape.NOT_A_PATH
    text = entry.strip().strip("`'\"").strip()
    if not text:
        return Shape.NOT_A_PATH
    if "://" in text:
        return Shape.URL
    if _GLOB_CHARS.intersection(text):
        return Shape.GLOB
    if text.startswith(("/", "~")):
        return Shape.HOST_PATH
    return Shape.REPO_PATH


def normalize(entry: Any) -> str:
    """The entry as a repo-relative POSIX path, with only quoting stripped."""
    text = str(entry).strip().strip("`'\"").strip()
    while text.startswith("./"):
        text = text[2:]
    return PurePosixPath(text.lstrip("/")).as_posix() if text else ""


def extract(answer: str) -> tuple[Any, Form]:
    """The plan JSON in an answer, and how it was found."""
    for block in _FENCE.findall(answer):
        try:
            return json.loads(block), Form.FENCED
        except json.JSONDecodeError:
            continue
    start, end = answer.find("{"), answer.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(answer[start : end + 1]), Form.BARE
        except json.JSONDecodeError:
            pass
    return None, Form.MALFORMED


def _rows(node: Any) -> Iterator[dict]:
    """Every object carrying the field, at any depth, in document order."""
    if isinstance(node, dict):
        if FIELD in node:
            yield node
        for value in node.values():
            yield from _rows(value)
    elif isinstance(node, list):
        for item in node:
            yield from _rows(item)


def check(label: str, answer: str, evidence: Evidence) -> Answer:
    plan, form = extract(answer)
    result = Answer(label=label, form=form)
    for row in _rows(plan):
        result.rows += 1
        files = row[FIELD]
        if not isinstance(files, list):
            result.off_schema_rows += 1
            continue
        result.resolutions.extend(evidence.resolve(f) for f in files)
    return result


def totals(answers: Iterable[Answer]) -> Counter:
    total: Counter = Counter()
    for a in answers:
        total.update(a.counts())
        total.update(a.shapes())
        total[a.form] += 1
    return total


def clean(answers: Iterable[Answer]) -> bool:
    """True when every answer parsed and every entry names an indexed file."""
    return all(
        a.form is not Form.MALFORMED
        and not a.off_schema_rows
        and all(r.verdict is Verdict.CONFIRMED for r in a.resolutions)
        for a in answers
    )


def to_dict(answers: list[Answer], evidence: Evidence) -> dict:
    return {
        "indexed": len(evidence.indexed),
        "checkout": len(evidence.checkout),
        "answers": [asdict(a) for a in answers],
    }


def render(answers: list[Answer], evidence: Evidence) -> str:
    lines = [
        f"evidence: {len(evidence.indexed)} indexed of {len(evidence.checkout)} checkout files"
    ]
    for a in answers:
        counts = a.counts()
        folded = sum(r.folded for r in a.resolutions)
        lines.append(
            f"{a.label}: {a.form.value}, {a.rows} rows, {len(a.resolutions)} entries: "
            + ", ".join(f"{counts[v]} {v.value}" for v in Verdict)
            + (f" ({folded} by folded spelling)" if folded else "")
            + (
                " [" + ", ".join(f"{n} {k.value}" for k, n in sorted(a.shapes().items())) + "]"
                if a.shapes()
                else ""
            )
            + (f"; {a.off_schema_rows} rows with a non-list field" if a.off_schema_rows else "")
        )
        for r in a.resolutions:
            if r.verdict is Verdict.CONFIRMED and not r.folded:
                continue
            target = f" -> {', '.join(r.resolved)}" if r.resolved else ""
            shape = f" ({r.shape.value})" if r.shape else ""
            lines.append(f"  {r.verdict.value:15} {r.entry}{target}{shape}")
    return "\n".join(lines)
