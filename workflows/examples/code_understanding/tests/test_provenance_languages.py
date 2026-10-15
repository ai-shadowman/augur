"""Which extensions the key folding handles, and what it does with the rest.

The table below is walked from ``assets/mappings/language_mappings.json`` as it
is on this run, not from a copy taken when these tests were written. That
matters: the whole defect was a hand-maintained suffix tuple drifting away from
the shipped mapping with nothing comparing the two.

Reading the mapping to enumerate does mean the test and the gate share a source,
so the enumeration is not an independent check of the gate's verdict. The
independent part is the entity count, which comes from running the real
:func:`provenance.graph.ground` over a synthetic corpus and counting what comes
out. A gated language that quietly started creating entities again would pass
the enumeration and fail the count.
"""

import unittest

from provenance import graph, languages
from provenance.corpus import Corpus, CorpusFile
from provenance.grounding_report import GroundingReport

REPO = "https://github.com/example/app.git"
SLUG = "example-app"

#: Extensions the folding handles, as :mod:`provenance.languages` derives them.
#: Spelled out once so a change to the derivation has to be deliberate.
EXPECTED_SUFFIXES = frozenset(
    {".cfg", ".ini", ".py", ".pyi", ".pyx", ".toml", ".txt", ".yaml", ".yml"}
)


def extension_table():
    """Every ``(language, extension)`` the mapping names, from both sections.

    Pairs rather than languages, because the verdict is per extension and a
    language can straddle: ``cloudformation`` folds ``.yaml`` and ``.yml`` and
    does not fold ``.json`` or ``.template``. Rolling that up to one verdict
    per language would have to pick a side and would be wrong either way.
    """
    mapping = languages.mapping()
    pairs = set()
    for section in ("file_extensions", "config_file_extensions"):
        for language, extensions in mapping.get(section, {}).items():
            for extension in extensions:
                pairs.add((language.lower(), extension.lower()))
    return sorted(pairs)


def synthetic(extension):
    """A two-file corpus and the entities a model typically returns for it.

    The model's spelling is the dotted, extension-stripped form, which is what
    the extraction prompt asks for and what every celery run produced. Whether
    that meets the corpus is exactly the question the folding decides.
    """
    paths = [f"pkg/mod_a{extension}", f"pkg/mod_b{extension}"]
    titles = ["PKG.MOD_A", "PKG.MOD_B"]
    corpus = Corpus(files=[
        CorpusFile(path=p, git_repo=REPO, git_slug=SLUG, multi_repo=False)
        for p in paths
    ])
    return corpus, paths, titles


def run_ground(corpus, titles):
    """Ground a synthetic corpus and return the entities with the report."""
    import pandas as pd

    entities = pd.DataFrame(
        [(t, "MODULE", f"The {t} module.") for t in titles],
        columns=["title", "type", "description"],
    )
    relationships = pd.DataFrame(
        [], columns=["source", "target", "description", "weight"]
    )
    entities, _, report = graph.ground(corpus, entities, relationships)
    return entities, report


class TestDerivedSuffixes(unittest.TestCase):
    """The suffix list is computed from the mapping, not declared beside it."""

    def test_the_derived_set_is_what_the_folding_uses(self):
        self.assertEqual(set(languages.source_suffixes()), EXPECTED_SUFFIXES)

    def test_every_derived_suffix_traces_to_the_mapping_or_a_named_extra(self):
        """No suffix may appear without a source. This is the drift guard.

        A suffix that is neither claimed by a folded language nor listed in
        ``_ORACLE_EXTRA`` is the old bug returning: a value someone added by
        hand that the shipped mapping knows nothing about.
        """
        claims = languages.languages_by_extension()
        for suffix in languages.source_suffixes():
            with self.subTest(suffix=suffix):
                if suffix in languages._ORACLE_EXTRA:
                    continue
                folded = set(claims.get(suffix, ())) & set(languages.FOLDED_LANGUAGES)
                self.assertTrue(
                    folded,
                    f"{suffix} is folded but no folded language claims it; it is "
                    "either a hand-added suffix or the mapping moved underneath it",
                )

    def test_every_extension_of_a_folded_language_is_folded(self):
        """The other direction: the mapping cannot name one the folding misses."""
        for language, extension in extension_table():
            if language not in languages.FOLDED_LANGUAGES:
                continue
            with self.subTest(language=language, extension=extension):
                self.assertIn(extension, languages.source_suffixes())

    def test_the_oracle_extra_is_still_needed(self):
        """``.pyx`` is kept by hand only because the mapping omits it.

        If a later edit adds ``.pyx`` to the mapping's ``python`` entry, the
        extra becomes dead weight and this says so rather than letting it sit
        there implying the mapping is still wrong.
        """
        claims = languages.languages_by_extension()
        for extension in languages._ORACLE_EXTRA:
            with self.subTest(extension=extension):
                self.assertNotIn(
                    extension, claims,
                    f"{extension} is now in the mapping; drop it from _ORACLE_EXTRA",
                )


class TestSuffixOf(unittest.TestCase):
    """Finding the extension, including the cases that are not the last dot."""

    CASES = [
        ("a plain source file", "celery/worker/state.py", ".py"),
        ("a compound the mapping knows", "views/home.blade.php", ".blade.php"),
        ("a compound it does not", "celery/state.v2.py", ".py"),
        ("a dotted directory", "celery-v2.5.5/setup.py", ".py"),
        ("an uppercase extension", "src/Main.C", ".c"),
        ("a dotfile, which has no extension", ".gitignore", ""),
        ("a name with no dot at all", "Makefile", ""),
        ("surrounding space", "  src/app/service.ts  ", ".ts"),
    ]

    def test_cases(self):
        for label, path, expected in self.CASES:
            with self.subTest(label):
                self.assertEqual(languages.suffix_of(path), expected)


class TestClassify(unittest.TestCase):
    """The three cases, and what each is allowed to claim."""

    def test_a_handled_extension_folds(self):
        verdict = languages.classify("celery/worker/state.py")
        self.assertTrue(verdict.folded)
        self.assertIsNone(verdict.reason)

    def test_a_name_with_no_extension_folds(self):
        """Nothing to strip means the fold is the identity, so it can match."""
        for path in ("Makefile", ".gitignore", "bin/mytool"):
            with self.subTest(path=path):
                self.assertTrue(languages.classify(path).folded)

    def test_a_mapped_but_unhandled_extension_names_its_language(self):
        verdict = languages.classify("src/main/java/com/example/Foo.java")
        self.assertFalse(verdict.folded)
        self.assertEqual(verdict.languages, ("java",))
        self.assertIn("java", verdict.reason)

    def test_an_unmapped_extension_names_the_extension_and_claims_no_language(self):
        """``.go`` is absent from both sections, so the gate must not guess.

        Saying "I do not handle .go" is true. Saying "this is Go" is an
        inference from an extension the shipped mapping does not describe, and
        the gate has no business making it.
        """
        verdict = languages.classify("cmd/server/main.go")
        self.assertFalse(verdict.folded)
        self.assertEqual(verdict.languages, ())
        self.assertIn(".go", verdict.reason)

    def test_an_extension_claimed_by_several_languages_reports_all_of_them(self):
        """Extension to language is many-to-many and the verdict says so."""
        verdict = languages.classify("src/thing.h")
        self.assertFalse(verdict.folded)
        self.assertEqual(verdict.languages, ("c", "c++"))


class TestEveryMappedLanguage(unittest.TestCase):
    """The verdict table, walked from the mapping and checked by entity count.

    One row per ``(language, extension)``. The assertion is the measurement:
    a gated extension must leave the entity count at ``before + 1``, the ``+1``
    being the repository entity, which comes from the header's slug and has
    nothing to do with suffix stripping. Before the gate these ran at
    ``before + 3`` -- two duplicate modules and the repository.
    """

    def test_every_mapped_extension_has_a_verdict(self):
        table = extension_table()
        self.assertTrue(table, "the mapping named no extensions at all")
        for language, extension in table:
            with self.subTest(language=language, extension=extension):
                verdict = languages.classify(f"pkg/mod_a{extension}")
                self.assertEqual(verdict.folded, extension in EXPECTED_SUFFIXES)

    def test_a_gated_extension_creates_nothing_but_the_repository(self):
        for language, extension in extension_table():
            if extension in EXPECTED_SUFFIXES:
                continue
            with self.subTest(language=language, extension=extension):
                corpus, paths, titles = synthetic(extension)
                entities, report = run_ground(corpus, titles)

                self.assertEqual(
                    report.entities_after, report.entities_before + 1,
                    f"{language}{extension}: expected only the repository entity, "
                    f"got {report.entities_after - report.entities_before} new "
                    f"(created={report.created})",
                )
                self.assertEqual(report.created, [SLUG.upper()])
                self.assertEqual(
                    [p for p, _ in report.unsupported_files], paths,
                    f"{language}{extension}: every corpus file should be recorded "
                    "as not grounded",
                )
                for _, reason in report.unsupported_files:
                    self.assertTrue(reason, "a skip must say why")
                    self.assertIn(extension, reason)
                self.assertNotIn(
                    paths[0].upper(), set(entities["title"].astype(str)),
                    "a gated file must not get an entity of its own",
                )

    def test_a_folded_extension_still_grounds(self):
        """python and ansible keep today's behaviour: retitled, not duplicated."""
        for language, extension in extension_table():
            if extension not in EXPECTED_SUFFIXES:
                continue
            with self.subTest(language=language, extension=extension):
                corpus, paths, titles = synthetic(extension)
                entities, report = run_ground(corpus, titles)
                present = set(entities["title"].astype(str))

                self.assertEqual(report.entities_after, report.entities_before + 1)
                self.assertEqual(report.unsupported_files, [])
                self.assertEqual(report.ungrounded_modules, [])
                for path in paths:
                    self.assertIn(
                        path.upper(), present,
                        f"{language}{extension}: the model's entity should have "
                        "been retitled onto the header spelling",
                    )


class TestTheDefectThisPrevents(unittest.TestCase):
    """The measured before/after, kept as a test so it cannot come back."""

    def test_a_java_corpus_does_not_double_its_module_layer(self):
        corpus, _, titles = synthetic(".java")
        entities, report = run_ground(corpus, titles)
        self.assertEqual((report.entities_before, report.entities_after), (2, 3))
        self.assertEqual(sorted(entities["title"].astype(str)),
                         ["EXAMPLE-APP", "PKG.MOD_A", "PKG.MOD_B"])

    def test_a_typescript_corpus_does_not_double_its_module_layer(self):
        corpus, _, titles = synthetic(".ts")
        _, report = run_ground(corpus, titles)
        self.assertEqual((report.entities_before, report.entities_after), (2, 3))

    def test_the_python_control_is_unmoved(self):
        """The control the whole gate is measured against."""
        corpus, _, titles = synthetic(".py")
        entities, report = run_ground(corpus, titles)
        self.assertEqual((report.entities_before, report.entities_after), (2, 3))
        self.assertEqual(sorted(entities["title"].astype(str)),
                         ["EXAMPLE-APP", "PKG/MOD_A.PY", "PKG/MOD_B.PY"])


class TestGroundingReportRecordsTheSkip(unittest.TestCase):
    """A skip that is not reported is the silent failure wearing a gate."""

    def test_the_rendered_report_names_the_files_and_the_reason(self):
        from provenance.grounding_report import render

        corpus, _, titles = synthetic(".java")
        _, report = run_ground(corpus, titles)
        text = render(report)
        self.assertIn("not grounded:  2 corpus file(s)", text)
        self.assertIn("pkg/mod_a.java", text)
        self.assertIn("java", text)

    def test_an_empty_report_says_nothing_about_unsupported_files(self):
        from provenance.grounding_report import render

        self.assertNotIn("not grounded", render(GroundingReport()))
