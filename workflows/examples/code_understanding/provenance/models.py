"""Typed records for ground truth that was established without a model.

The provenance package exists so that facts which can be derived deterministically
are derived deterministically, and so that anything a model later claims about
them can be checked. These records are the "before" half of that comparison;
``verify`` holds the "after" half.

A fact only belongs here if it can be recomputed from the repository on disk.
Anything requiring judgement stays with the LLM and stays out of this module.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

_NAME_SEPARATORS = re.compile(r"[-_.]+")


def normalize_name(name: str) -> str:
    """Fold a package name to its PEP 503 form.

    ``python_dateutil``, ``Python-DateUtil`` and ``python.dateutil`` are the
    same distribution; ``kombu.pools`` is not the same as ``kombu``. Both
    properties matter when matching a name a model produced against a name a
    manifest declared.
    """
    return _NAME_SEPARATORS.sub("-", name.strip()).lower()


#: A distribution name carrying a version specifier, optional extras and an
#: optional parenthesis, which is how a model writes an entity title when it
#: copies a requirements line whole instead of naming the distribution.
#: Deliberately strict about the tail: it has to open with a PEP 440 operator,
#: so a title that is a phrase rather than a requirement is left alone and
#: cannot satisfy a declared constraint by accident.
_REQUIREMENT = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*\(?\s*(?:===?|[<>!~]=|[<>])"
)


def distribution_name(title: str) -> str:
    """Normalize an entity title, dropping any version specifier it carries.

    ``COVERAGE>=3.0`` names the ``coverage`` distribution. The short-prompt
    celery run emitted exactly that title, and keying it whole made a declared
    constraint look absent from a graph that contained it, which scored the
    prompt for a defect it did not have.

    Matching only. The entity keeps the title the model wrote, because writing
    a version into a name is itself worth seeing.
    """
    match = _REQUIREMENT.match(str(title).strip())
    return normalize_name(match.group(1) if match else str(title))


class Layer(StrEnum):
    """The three layers a migration analysis has to keep separate.

    Conflating these is what sent application modules into the dependency list
    in earlier indexing runs, so the layer is carried explicitly rather than
    inferred from the ecosystem at read time.
    """

    OS = "os"
    PACKAGE = "package"
    APPLICATION = "application"


#: Package ecosystems that represent operating-system level packages. Used to
#: assign a layer to Syft artifacts, which report ecosystem but not layer.
OS_ECOSYSTEMS = frozenset({"rpm", "deb", "apk", "alpine", "debian", "redhat"})


class FindingKind(StrEnum):
    """Why a verification check failed.

    ``MISSING`` and ``MANGLED`` are forward failures: a fact we established did
    not survive into the graph intact. ``UNGROUNDED`` is the inverse failure:
    the graph asserts something nothing in the repository supports, which is
    the shape a hallucinated dependency takes.

    ``UNDECLARED`` is not a defect in the index at all. The model named
    something the source really imports but no manifest declares, so the gap
    is in the repository's packaging. It is reported separately because
    treating it as a model error would hide a genuine migration risk and would
    also make the hallucination count look worse than it is.

    ``UNINDEXED`` is also not a model defect. The fact was real, but the file
    stating it never reached the corpus, so no model could have produced it.
    celery declares ``bundle>=1.1.0`` in ``requirements/pkgutils.txt``, which
    the run never indexed. Charging that to extraction hides the actual cause,
    which is what the corpus filter selected.
    """

    MISSING = "missing"
    MANGLED = "mangled"
    UNGROUNDED = "ungrounded"
    UNDECLARED = "undeclared"
    UNINDEXED = "unindexed"


@dataclass(frozen=True)
class Package:
    """One dependency, either declared in a manifest or resolved by Syft.

    ``constraint`` is what the manifest literally said (``>=2.1.8,<2.2.0``) and
    ``version`` is a single resolved version. A package can have one, the other,
    or both: Syft reports resolved versions and skips unpinned ranges, while the
    manifest parsers report constraints and rarely resolve. Keeping the two
    apart is what lets the verifier tell "the model dropped the version" from
    "there was never a single version to drop".
    """

    name: str
    ecosystem: str
    layer: Layer
    source_file: str
    version: str | None = None
    constraint: str | None = None
    purl: str | None = None
    #: The manifest line verbatim, for byte-exact verification. Parsers
    #: normalise specifier ordering, so the raw line is kept separately.
    declared_line: str | None = None

    @property
    def key(self) -> str:
        """Canonical identity for matching against names a model produced.

        Models vary the case and swap ``-``/``_`` freely, so both are folded.
        The ecosystem is part of the key because ``redis`` the PyPI client and
        ``redis`` the RPM are different entities on different layers.
        """
        return f"{self.ecosystem}/{normalize_name(self.name)}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["layer"] = self.layer.value
        return d


@dataclass(frozen=True)
class BaseImage:
    """A container base image, which is where the OS layer actually comes from.

    Syft catalogs OS packages from built images. A Containerfile only names the
    image, so this records the reference and leaves resolution to whoever can
    pull it.
    """

    reference: str
    source_file: str
    stage: str | None = None
    line_number: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RepoFacts:
    """Everything established about one repository without calling a model."""

    root: str
    git_repo: str | None = None
    git_slug: str | None = None
    git_commit: str | None = None
    git_branch: str | None = None
    #: The runtime stack AUGUR wrote into every document header, such as
    #: "Python 2.7". Nothing derives this from the checkout; it is decided
    #: during data generation and recovered from the headers at index time,
    #: which makes it a fact to join rather than one to ask a model for.
    runtime: str | None = None
    multi_repo: bool = False
    packages: list[Package] = field(default_factory=list)
    base_images: list[BaseImage] = field(default_factory=list)
    #: Top-level importable names this repository defines itself, derived from
    #: the directory layout. Anything under one of these is application code,
    #: so a graph that files it as a third-party library is wrong and can be
    #: shown to be wrong without asking anyone.
    own_modules: list[str] = field(default_factory=list)
    #: Top-level names the source imports, from the AST. Needed to tell an
    #: undeclared dependency from an invented one: celery 2.5.5 imports
    #: ``pyinotify`` and appends ``threadpool`` to install_requires at runtime,
    #: so no manifest mentions either and both are real.
    imported_names: list[str] = field(default_factory=list)
    #: Repository-relative paths of every source file, POSIX style. The corpus
    #: a run indexes is usually a subset of these, which is what separates two
    #: very different failures: a module entity naming a file that exists but
    #: was never indexed came from the model's training data, while one naming
    #: no file at all was assembled on the spot. Both are wrong and only the
    #: full inventory can tell them apart.
    source_files: list[str] = field(default_factory=list)
    #: Every name each module in the checkout declares, keyed by the dotted
    #: module name. The same separation ``source_files`` gives the module
    #: layer, one level down: a function entity naming a symbol that exists in
    #: an unindexed file was recalled from training, while one naming a symbol
    #: no file declares was assembled on the spot. Kept as a mapping rather
    #: than a flat list because the owner is the whole point. A symbol that is
    #: real but filed under the wrong module is the commonest defect at this
    #: layer, and a flat list cannot see it.
    declared_symbols: dict[str, list[str]] = field(default_factory=dict)
    #: Parser limits hit while extracting, such as a setup.py whose
    #: install_requires is computed at runtime. Recorded rather than silently
    #: dropped, because "we could not determine this" and "there is nothing
    #: here" lead to different migration decisions.
    warnings: list[str] = field(default_factory=list)

    def packages_for_layer(self, layer: Layer) -> list[Package]:
        return [p for p in self.packages if p.layer is layer]

    @property
    def package_keys(self) -> set[str]:
        return {p.key for p in self.packages}

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "git_repo": self.git_repo,
            "git_slug": self.git_slug,
            "git_commit": self.git_commit,
            "git_branch": self.git_branch,
            "runtime": self.runtime,
            "multi_repo": self.multi_repo,
            "packages": [p.to_dict() for p in self.packages],
            "base_images": [b.to_dict() for b in self.base_images],
            "own_modules": list(self.own_modules),
            "imported_names": list(self.imported_names),
            "source_files": list(self.source_files),
            "declared_symbols": {k: sorted(v) for k, v in self.declared_symbols.items()},
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class Finding:
    """One verification failure, in a form that can be rendered or counted."""

    kind: FindingKind
    subject: str
    detail: str
    expected: str | None = None
    actual: str | None = None
    source_file: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d

    def __str__(self) -> str:
        parts = [f"[{self.kind.value}] {self.subject}: {self.detail}"]
        if self.expected is not None:
            parts.append(f"expected={self.expected!r}")
        if self.actual is not None:
            parts.append(f"actual={self.actual!r}")
        return " ".join(parts)


@dataclass
class VerificationReport:
    """Result of checking a finished index against the facts."""

    findings: list[Finding] = field(default_factory=list)
    checked: int = 0
    #: Counts keyed by check name, so a run can be compared against a previous
    #: run without re-reading every finding.
    stats: dict[str, Any] = field(default_factory=dict)

    #: Finding kinds that describe the repository or the corpus rather than
    #: the index, and so do not count against it.
    NOT_DEFECTS = (FindingKind.UNDECLARED, FindingKind.UNINDEXED)

    @property
    def defects(self) -> list[Finding]:
        """Findings that indict the index rather than the repository.

        UNDECLARED and UNINDEXED are excluded. An undeclared-but-imported
        dependency is a correct observation about the project, and a fact
        whose manifest never reached the corpus was never put to the model at
        all. Counting either would make a working index look broken.
        """
        return [f for f in self.findings if f.kind not in self.NOT_DEFECTS]

    @property
    def ok(self) -> bool:
        return not self.defects

    def of_kind(self, kind: FindingKind) -> list[Finding]:
        return [f for f in self.findings if f.kind is kind]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "counts": {k.value: len(self.of_kind(k)) for k in FindingKind},
            "stats": self.stats,
            "findings": [f.to_dict() for f in self.findings],
        }
