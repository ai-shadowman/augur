"""Which file extensions the key folding can actually strip.

:mod:`provenance.corpus` folds a path to the form a model's dotted spelling
shares with it, which means taking the extension off: ``celery/worker/state.py``
has to meet ``celery.worker.state``. The suffix list that did the stripping was
hand-written and Python-only, while ``assets/mappings/language_mappings.json``
is the shipped answer to the same question for 28 languages. The two disagreed
and nothing noticed, so a Java or TypeScript corpus folded to keys no entity
could ever match:

```
src/main/java/com/example/Foo.java -> src.main.java.com.example.foo.java
src/app/service.ts                 -> src.app.service.ts
celery/worker/state.py             -> celery.worker.state   (matches)
```

Every entity then landed in ``ungrounded_modules`` and the module layer grew a
fresh entity per corpus file, coming out at roughly twice its real size with no
warning. Measured on two-file synthetic corpora: Java 2 entities to 5,
TypeScript 2 to 5, Python 2 to 3.

The first two of those three lines are now historical. A language folds once
something can read it, so :mod:`provenance.dialects` is the second source the
suffix set unions in, and ``.java``, ``.ts``, ``.mts``, ``.cts`` and ``.rb``
joined when their grammars did. ``.kt`` and ``.go`` still fold to nothing and
still reach the gate, which is the point of deriving the set rather than
declaring it: registering a parser is what lifts a language, one at a time.

So this module answers one question, and deliberately not a larger one. It does
**not** decide what language a file is written in: extension to language is
many-to-many in the shipped mapping (``.c`` and ``.h`` belong to both ``c`` and
``c++``; ``.yaml`` is claimed by twelve languages, as a source extension for
``ansible`` and a config extension for the other eleven), so that question has
no single answer and nothing here needs one. It asks whether an extension is
one the folding handles, which is single-valued and is a set membership test.

The suffix set is computed from the mapping rather than declared, so a language
added to the mapping tomorrow cannot silently recreate the bug.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath

from .dialects import suffixes as dialect_suffixes

#: Mapping entries whose *configuration* extensions the folding also strips.
#: A dialect covers a language's source files; this covers the rest of what a
#: repository in that language puts in the corpus, which for ``python`` is
#: ``setup.cfg``, ``tox.ini``, ``pyproject.toml`` and ``requirements.txt``, and
#: for ``ansible`` is the playbooks themselves.
#:
#: It is not the list of languages the pipeline can read. That question is
#: :func:`provenance.dialects.dialect_for`, and the two are unioned below
#: rather than reconciled: ``.ts`` is claimed by the mapping's ``javascript``
#: and parsed by the ``typescript`` dialect, and folding it is correct under
#: either name.
FOLDED_LANGUAGES = ("python", "ansible")

#: Shipped alongside ``provenance/``, under the same package root the asset
#: loader uses. Read directly rather than through ``DefaultAssetLoader``, which
#: would pull the MLflow loader into the import graph of a leaf module.
MAPPING_PATH = (
    Path(__file__).resolve().parent.parent / "assets" / "mappings" / "language_mappings.json"
)

#: Mapping sections naming extensions. Both count: a ``setup.cfg`` is as much a
#: corpus file as a ``state.py``, and the folding strips its suffix too.
_EXTENSION_SECTIONS = ("file_extensions", "config_file_extensions")


@cache
def mapping() -> dict:
    """The shipped language mapping, read once.

    No fallback on a missing file. A hardcoded default here would be the exact
    silent failure this module exists to remove: the folding would carry on
    producing keys nothing can match and report that everything was fine.
    """
    try:
        return json.loads(MAPPING_PATH.read_text())
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"language mapping not found at {MAPPING_PATH}; provenance cannot "
            "decide which file extensions its key folding handles without it"
        ) from exc


@cache
def languages_by_extension() -> dict[str, tuple[str, ...]]:
    """Every mapped extension, lowercased, to the languages claiming it.

    Many-to-many on purpose. ``.yaml`` comes back with twelve languages and
    that is the honest answer; callers that want a single language want
    something this data cannot give them.
    """
    claims: dict[str, set[str]] = {}
    data = mapping()
    for section in _EXTENSION_SECTIONS:
        for language, extensions in data.get(section, {}).items():
            for extension in extensions:
                claims.setdefault(extension.lower(), set()).add(language.lower())
    return {extension: tuple(sorted(names)) for extension, names in claims.items()}


@cache
def source_suffixes() -> tuple[str, ...]:
    """Extensions :func:`provenance.corpus.canonical_key` strips, longest first.

    Two sources, unioned. The mapping supplies what :data:`FOLDED_LANGUAGES`
    claims, and :func:`provenance.dialects.suffixes` supplies every extension
    some registered grammar reads. Neither is a subset of the other: the
    mapping has ``.cfg`` and ``.toml``, which no parser reads and the folding
    must still strip, and the dialects have ``.pyx``, ``.mts`` and ``.cts``,
    which the mapping has never heard of.

    Deriving both halves is what keeps this honest. The original defect was a
    hand-written tuple drifting away from the shipped mapping; a hand-written
    tuple could now drift away from the dialect registry in exactly the same
    way, and registering a grammar would quietly leave its language gated.

    Longest first so a compound extension wins over its own tail. That matters
    now rather than hypothetically: ``.mts`` has to be taken before the
    ``.ts`` it ends with.
    """
    data = mapping()
    derived = {
        extension.lower()
        for section in _EXTENSION_SECTIONS
        for language, extensions in data.get(section, {}).items()
        if language.lower() in FOLDED_LANGUAGES
        for extension in extensions
    }
    derived.update(suffix.lower() for suffix in dialect_suffixes())
    return tuple(sorted(derived, key=lambda s: (-len(s), s)))


def suffix_of(path: str) -> str:
    """The extension of ``path``, preferring the longest the mapping knows.

    ``foo.blade.php`` is ``.blade.php`` because the mapping says so, and
    ``state.v2.py`` is ``.py`` because it does not. A dotfile has no extension:
    ``.gitignore`` returns ``""``, matching :mod:`pathlib`, and that is the
    answer the gate wants, since a name with nothing to strip folds to itself
    and so is handled.
    """
    suffixes = [s.lower() for s in PurePosixPath(str(path).strip()).suffixes]
    if not suffixes:
        return ""
    known = languages_by_extension()
    for i in range(len(suffixes)):
        candidate = "".join(suffixes[i:])
        if candidate in known:
            return candidate
    return suffixes[-1]


@dataclass(frozen=True)
class Verdict:
    """Whether one file's extension is one the key folding can strip."""

    #: As :func:`suffix_of` found it. ``""`` for a name with no extension.
    extension: str
    #: True when :func:`provenance.corpus.canonical_key` strips this extension,
    #: so the folded key can meet a model's dotted spelling of the same path.
    folded: bool
    #: Languages the mapping associates with the extension, possibly several
    #: and possibly none. Empty is a fact, not a failure to look.
    languages: tuple[str, ...] = ()

    @property
    def reason(self) -> str | None:
        """Why the folding declined, in a form a log or a report can print.

        Three cases, and the third is the common one. Note what the third does
        not say: an extension in no list gets named without a language being
        claimed for it. ``.go``, ``.rs``, ``.cs``, ``.tsx`` and ``.php`` are all
        absent from the mapping, and "I do not handle .go" is true where "this
        is Go" would be an inference the gate has no business making.
        """
        if self.folded:
            return None
        if not self.extension:
            return None
        if self.languages:
            return (
                f"{self.extension} belongs to {', '.join(self.languages)}, whose "
                "paths this pipeline's key folding cannot reduce to the spelling "
                "an entity would carry"
            )
        return (
            f"{self.extension} is in no language mapping, so the key folding "
            "cannot reduce it to the spelling an entity would carry"
        )


def classify(path: str) -> Verdict:
    """Decide whether the folding can match anything against ``path``."""
    extension = suffix_of(path)
    return Verdict(
        extension=extension,
        folded=not extension or extension in source_suffixes(),
        languages=languages_by_extension().get(extension, ()),
    )
