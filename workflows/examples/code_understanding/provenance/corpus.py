"""Read back the facts AUGUR wrote into every document header.

Data generation prefixes each document with a line naming the file's path, the
repository URL, the slug and the runtime. Those values are placed there by this
pipeline, so at indexing time they are already known exactly: the header is a
record of what we put in, not a claim anyone has to evaluate.

The extraction prompt nonetheless asks the model to read them back out and
retype them into entity names and descriptions. That is a transcription, and it
is measurably lossy. In the celery run the corpus held 22 files and the graph
came back with 100 module-shaped entities, of which 26 named a file that was
actually indexed. Recall was fine, 21 of the 22 files were found. Precision was
not.

This module is the deterministic half of the repair. It parses the headers back
into a :class:`Corpus`, which is the authoritative list of what was indexed and
what each document's identity is. :mod:`provenance.graph` uses it to fold the
invented module entities into the real ones.

It also settles a disagreement the verifier used to lose. ``facts`` derives a
slug from the git remote and branch, and for celery got ``celery-celery-master``
while data generation had written ``celery-v2-5-5`` into every header. The
verifier compared the graph against the derived value and reported a defect that
was its own. Where the header and the derived value disagree, the header wins:
it is what the model was actually shown.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath

logger = logging.getLogger(__name__)

#: Closing comment delimiters that may immediately follow the header line when
#: a file has no extracted metadata lines below the header.
_CLOSING_DELIMITERS = (
    r'(?:"""|\*/|-->|--%>|\*#|#\}|--\}\}|%>|\}\}|=end|COMMENT|\{%\s*endcomment\s*%\})'
)

#: The header data generation writes. ``runtime`` is optional, and the trailing
#: group stops at a quote, comment delimiter, or newline so a closing delimiter
#: is not swallowed.
HEADER = re.compile(
    r"This file is located at (?P<path>\S+?)\s+"
    r"from repository url (?P<repo>\S+?),\s*"
    r"repository slug (?P<slug>\S+?),\s*"
    r"multi_repo (?P<multi>true|false)"
    r"(?:,\s*runtime\s+(?P<runtime>[^\n]+?))?\s*"
    rf"(?:{_CLOSING_DELIMITERS}|\n|$)",
    re.IGNORECASE,
)

#: Identity fields written into flattened ``*_metadata.txt`` YAML sidecar files.
_YAML_FIELD = {
    name: re.compile(rf"^{name}:\s*['\"]?([^\n'\"]+?)['\"]?\s*$", re.MULTILINE)
    for name in ("file_path", "git_repo", "git_slug", "multi_repo")
}

#: Suffixes stripped before comparing a model's entity name to a real path. A
#: model writes ``celery.worker.state`` for ``celery/worker/state.py``, so the
#: extension has to come off for the two to meet.
_SOURCE_SUFFIXES = (
    ".blade.php",
    ".py", ".pyx", ".pyi", ".java", ".kt", ".js", ".ts",
    ".c", ".cpp", ".cc", ".cxx", ".h", ".i",
    ".rb", ".sh", ".sql", ".pp", ".epp", ".tf", ".tfvars",
    ".jsp", ".ftl", ".ftlh", ".vm", ".vtl", ".xsl", ".xslt",
    ".html", ".htm", ".j2", ".jinja", ".jinja2", ".hbs", ".handlebars",
    ".pug", ".jade", ".ejs", ".njk", ".mustache", ".erb", ".liquid", ".twig",
    ".xml", ".gradle", ".properties", ".json", ".cmake", ".mk", ".gemspec",
    ".env", ".conf", ".ini", ".txt", ".cfg", ".toml", ".yaml", ".yml",
)

#: Separators a model uses interchangeably for the same path. ``_`` is
#: deliberately absent: folding it would make ``celery.app.__init__`` collide
#: with ``celery.app.init``, and ``__init__`` is a real and common path segment.
_SEPARATORS = re.compile(r"[/.]+")


def dotted_key(name: str) -> str:
    """Fold separators and case, leaving any file suffix in place.

    Separate from :func:`canonical_key` because a symbol is not a path. A
    function entity named ``celery.utils.setup.cfg`` would lose its last
    segment to the suffix strip, and a symbol's last segment is the part that
    identifies it.
    """
    return _SEPARATORS.sub(".", str(name).strip().lower()).strip(".")


def canonical_key(name: str) -> str:
    """Fold a path or dotted name to the form both spellings share.

    ``CELERY/WORKER/STATE.PY``, ``celery.worker.state`` and
    ``CELERY/WORKER/STATE`` are three spellings the model produced for one
    file in a single run. All three reduce to ``celery.worker.state`` here,
    which is what lets them be recognised as one entity.
    """
    text = str(name).strip().lower()
    for suffix in _SOURCE_SUFFIXES:
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return dotted_key(text)


def prefixes_of(keys: Iterable[str]) -> set[str]:
    """Every dotted directory implied by a set of file keys.

    ``celery.app.base`` proves ``celery`` and ``celery.app`` exist. That is
    worth separating from a name with no referent at all, because a package
    entity is legitimate where an invented module is not.
    """
    prefixes: set[str] = set()
    for key in keys:
        parts = key.split(".")
        for i in range(1, len(parts)):
            prefixes.add(".".join(parts[:i]))
    return prefixes


@dataclass(frozen=True)
class CorpusFile:
    """One indexed document, as its own header describes it."""

    path: str
    git_repo: str
    git_slug: str
    multi_repo: bool
    runtime: str | None = None
    #: GraphRAG's id for this document, where the table carried one. It is the
    #: link back from an entity to the file that produced it, by way of
    #: ``text_unit_ids``, and the only thing that can settle a relative import
    #: whose resolution depends on how deep the importing file sits.
    document_id: str | None = None

    @property
    def key(self) -> str:
        return canonical_key(self.path)

    @property
    def title(self) -> str:
        """The entity title for this file.

        GraphRAG uppercases every entity name as it parses the model's output
        (``graph_extractor`` calls ``.upper()`` on the name and the type), so a
        title written here has to arrive already uppercased or it will not
        match the rest of the table.
        """
        return self.path.upper()


@dataclass
class Corpus:
    """Every document that was indexed, keyed by what the header said."""

    files: list[CorpusFile] = field(default_factory=list)
    #: Documents whose header could not be parsed. Kept rather than dropped:
    #: a file with no header is one the grounding pass cannot speak for, and
    #: that is worth knowing before concluding its entities were invented.
    unparsed: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.files)

    @property
    def keys(self) -> set[str]:
        return {f.key for f in self.files}

    def by_key(self) -> dict[str, CorpusFile]:
        return {f.key: f for f in self.files}

    def by_document_id(self) -> dict[str, CorpusFile]:
        """Documents keyed by GraphRAG's id, for tracing an entity to its file."""
        return {f.document_id: f for f in self.files if f.document_id}

    def package_prefixes(self) -> set[str]:
        """Directories proved to exist by the paths that were indexed."""
        return prefixes_of(self.keys)

    def _majority(self, attribute: str) -> str | None:
        """The value the headers agree on, or the most common one if they differ.

        Disagreement is a real possibility in a multi-repo index and a sign of
        a corrupted header otherwise, so it is logged rather than assumed away.
        """
        values = [getattr(f, attribute) for f in self.files if getattr(f, attribute)]
        if not values:
            return None
        counts = Counter(values)
        if len(counts) > 1:
            logger.warning(
                "corpus headers disagree on %s: %s; using the most common",
                attribute, dict(counts),
            )
        return counts.most_common(1)[0][0]

    @property
    def git_repo(self) -> str | None:
        return self._majority("git_repo")

    @property
    def git_slug(self) -> str | None:
        return self._majority("git_slug")

    @property
    def runtime(self) -> str | None:
        return self._majority("runtime")

    @property
    def multi_repo(self) -> bool:
        return any(f.multi_repo for f in self.files)

    def slugs(self) -> set[str]:
        return {f.git_slug for f in self.files if f.git_slug}


def _parse_yaml_metadata(text: str) -> CorpusFile | None:
    """Read a flattened ``*_metadata.txt`` YAML sidecar if all identity keys are present."""
    sample = str(text)[:8192]
    matches = {name: pat.search(sample) for name, pat in _YAML_FIELD.items()}
    if not all(matches.values()):
        return None
    path = matches["file_path"].group(1).strip()
    repo = matches["git_repo"].group(1).strip()
    slug = matches["git_slug"].group(1).strip()
    multi = matches["multi_repo"].group(1).strip().lower()
    if not (path and repo and slug and multi in ("true", "false")):
        return None
    return CorpusFile(
        path=PurePosixPath(path).as_posix(),
        git_repo=repo.rstrip(","),
        git_slug=slug,
        multi_repo=multi == "true",
        runtime=None,
    )


def parse_header(text: str) -> CorpusFile | None:
    """Read one document's header, or ``None`` if it has none.

    Only the head of the document is searched. The phrase can legitimately
    appear later in a file that documents the convention, and a match there
    would describe some other file.
    """
    head = str(text)[:1024]
    match = HEADER.search(head)
    if not match:
        return _parse_yaml_metadata(text)
    raw_runtime = (match.group("runtime") or "").strip()
    runtime = re.sub(rf"\s*{_CLOSING_DELIMITERS}\s*$", "", raw_runtime, flags=re.IGNORECASE).strip() or None
    return CorpusFile(
        path=PurePosixPath(match.group("path")).as_posix(),
        git_repo=match.group("repo").rstrip(","),
        git_slug=match.group("slug"),
        multi_repo=match.group("multi").lower() == "true",
        runtime=runtime,
    )


def from_documents(documents) -> Corpus:
    """Build the corpus record from GraphRAG's ``documents`` table."""
    corpus = Corpus()
    if "text" not in documents:
        return corpus

    titles = (
        documents["title"].astype(str).tolist()
        if "title" in documents
        else [str(i) for i in range(len(documents))]
    )
    ids = (
        documents["id"].astype(str).tolist()
        if "id" in documents
        else [None] * len(documents)
    )
    by_key_idx: dict[str, int] = {}
    sidecar_keys: set[str] = set()
    for title, document_id, text in zip(titles, ids, documents["text"], strict=False):
        parsed = parse_header(text)
        if parsed is None:
            corpus.unparsed.append(title)
            continue
        entry = replace(parsed, document_id=document_id)
        is_sidecar = str(title).endswith("_metadata.txt") or HEADER.search(str(text)[:1024]) is None
        if entry.key in by_key_idx:
            idx = by_key_idx[entry.key]
            if not is_sidecar and entry.key in sidecar_keys:
                corpus.files[idx] = entry
                sidecar_keys.discard(entry.key)
            elif is_sidecar:
                continue
            else:
                corpus.files.append(entry)
            continue
        by_key_idx[entry.key] = len(corpus.files)
        if is_sidecar:
            sidecar_keys.add(entry.key)
        corpus.files.append(entry)

    if corpus.unparsed:
        logger.warning(
            "%d of %d documents carry no parseable header; their entities "
            "cannot be grounded: %s",
            len(corpus.unparsed), len(documents), ", ".join(corpus.unparsed[:10]),
        )
    return corpus
