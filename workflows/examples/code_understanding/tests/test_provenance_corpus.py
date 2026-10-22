"""Reading back the headers AUGUR itself wrote into every document.

The header is a record of what this pipeline put in front of the model, so
every value in it is known exactly before any call is made. These tests pin
the parse and the folding rules that let a model's spelling of a path be
recognised as the path.
"""

import unittest

from provenance.corpus import (
    Corpus,
    CorpusFile,
    canonical_key,
    from_documents,
    parse_header,
)

HEADER = ('This file is located at celery/worker/state.py from repository url '
          'https://github.com/celery/celery.git, repository slug celery-v2-5-5, '
          'multi_repo false')


def document_frame(rows):
    """GraphRAG's ``documents`` table, reduced to what the parser reads."""
    import pandas as pd

    return pd.DataFrame(rows, columns=["title", "text"])


def corpus_file(path, slug="celery-v2-5-5", repo="https://github.com/celery/celery.git",
                multi_repo=False, runtime=None, package=None):
    return CorpusFile(path=path, git_repo=repo, git_slug=slug, multi_repo=multi_repo,
                      runtime=runtime, package=package)


class TestCanonicalKey(unittest.TestCase):
    """Three spellings of one file, all produced in a single celery run."""

    CASES = [
        ("a path", "CELERY/WORKER/STATE.PY", "celery.worker.state"),
        ("a dotted name", "celery.worker.state", "celery.worker.state"),
        ("a path without its extension", "CELERY/WORKER/STATE", "celery.worker.state"),
        ("surrounding space", "  celery/worker/state.py  ", "celery.worker.state"),
        ("a leading separator", "/celery/worker/state.py", "celery.worker.state"),
        ("a doubled separator", "celery//worker/state.py", "celery.worker.state"),
        ("a non-source extension", "requirements/default.txt", "requirements.default"),
    ]

    def test_every_spelling_folds_to_one_key(self):
        for label, spelling, expected in self.CASES:
            with self.subTest(label):
                self.assertEqual(canonical_key(spelling), expected)

    def test_underscores_are_not_folded(self):
        # Folding "_" would collide celery.app.__init__ with celery.app.init,
        # and __init__ is a real and very common path segment.
        self.assertNotEqual(
            canonical_key("celery/app/__init__.py"), canonical_key("celery/app/init.py")
        )

    def test_only_the_trailing_extension_comes_off(self):
        self.assertEqual(canonical_key("docs/conf.py.txt"), "docs.conf.py")


class TestParseHeader(unittest.TestCase):
    def test_the_four_required_fields_are_read(self):
        parsed = parse_header(HEADER)
        self.assertEqual(parsed.path, "celery/worker/state.py")
        self.assertEqual(parsed.git_repo, "https://github.com/celery/celery.git")
        self.assertEqual(parsed.git_slug, "celery-v2-5-5")
        self.assertFalse(parsed.multi_repo)
        self.assertIsNone(parsed.runtime)

    def test_the_optional_runtime_is_read_when_present(self):
        self.assertEqual(parse_header(HEADER + ", runtime Python 2.7").runtime, "Python 2.7")

    def test_a_docstring_delimiter_does_not_end_up_in_the_runtime(self):
        text = f'"""{HEADER}, runtime Python 2.7"""\nimport os\n'
        self.assertEqual(parse_header(text).runtime, "Python 2.7")

    def test_multi_repo_is_read_as_a_boolean(self):
        self.assertTrue(parse_header(HEADER.replace("multi_repo false", "multi_repo true")).multi_repo)

    def test_a_document_with_no_header_parses_to_nothing(self):
        self.assertIsNone(parse_header("import os\n\ndef f():\n    pass\n"))

    def test_the_phrase_appearing_late_in_a_file_is_not_treated_as_its_header(self):
        # A file documenting the convention would otherwise claim to be some
        # other file, and every entity in it would be grounded onto that one.
        self.assertIsNone(parse_header(("# padding\n" * 200) + HEADER))

    def test_a_redundant_segment_is_normalised_out_of_the_path(self):
        header = HEADER.replace("celery/worker/state.py", "celery//worker/state.py")
        self.assertEqual(parse_header(header).path, "celery/worker/state.py")

    def test_the_package_section_is_read_where_the_formatter_writes_it(self):
        text = f'"""{HEADER}\n\n Package: celery.worker.state\n\nDependencies:\n- [import] sys"""\n'
        self.assertEqual(parse_header(text).package, "celery.worker.state")

    def test_a_header_written_before_the_section_existed_declares_nothing(self):
        """The ``positional`` corpus variant, which is still rebuilt and read."""
        self.assertIsNone(parse_header(f'"""{HEADER}, runtime Python 2.7"""\nimport os\n').package)

    def test_the_word_appearing_in_the_file_body_is_not_read_as_the_section(self):
        """Anchored to the end of the located-at line, not searched for.

        A module docstring that writes ``Package: celery`` as prose would
        otherwise give the file a second key naming something else, and every
        entity spelled that way would ground onto it.
        """
        text = f'"""{HEADER}"""\n\nREADME = """\nPackage: some.other.thing\n"""\n'
        self.assertIsNone(parse_header(text).package)

    def test_an_empty_package_section_does_not_borrow_the_line_below_it(self):
        text = f'"""{HEADER}\n\n Package: \n\nDependencies:\n- [import] sys"""\n'
        self.assertIsNone(parse_header(text).package)


class TestCorpusFile(unittest.TestCase):
    def test_the_title_is_uppercased_to_match_what_graphrag_stores(self):
        # graph_extractor calls .upper() on every name as it parses the
        # model's output, so a title built here has to arrive uppercased or
        # it will never match a row in the table.
        self.assertEqual(corpus_file("celery/worker/state.py").title,
                         "CELERY/WORKER/STATE.PY")

    def test_the_key_is_the_folded_form_of_the_path(self):
        self.assertEqual(corpus_file("celery/worker/state.py").key, "celery.worker.state")


JAVA_PATH = "src/main/java/org/apache/commons/lang3/StringUtils.java"
JAVA_PACKAGE = "org.apache.commons.lang3.StringUtils"


class TestTheSecondKeyAHeaderCanDeclare(unittest.TestCase):
    """``Package:`` as a key, which turns out to matter for Python too.

    Java was the reason for it: the declared type name is what Maven's four
    leading directories cannot produce, and a model shown the header writes
    the declared one. The expectation going in was that Python would be a
    no-op, because a module's package line is its path folded.

    It is a no-op for 202 of celery's 237 files and not for the other 35,
    which are its ``__init__.py``. There the package line is the directory,
    ``celery.app`` for ``celery/app/__init__.py``, and the path fold keeps the
    ``__init__`` segment. The two names are the same importable thing in
    Python, so the second key is correct rather than merely convenient, and
    :mod:`tests.test_provenance_graph` measures what it is worth.
    """

    def test_a_python_module_declares_nothing_the_path_does_not_already_say(self):
        stated = corpus_file("celery/worker/state.py", package="celery.worker.state")

        self.assertEqual(("celery.worker.state",), stated.keys)
        self.assertEqual((stated.key,), stated.keys)

    def test_a_python_package_init_is_the_second_key_nobody_predicted(self):
        """``declarations.module_key`` drops ``__init__``; ``canonical_key`` keeps it.

        Both are right about their own question. The path fold has to keep the
        segment, because ``celery/app/__init__.py`` and ``celery/app.py`` are
        different files and folding them together would misattribute. The
        package line has to drop it, because ``import celery.app`` is what
        runs the file and no Python source ever spells ``celery.app.__init__``.
        Carrying both is what lets each answer stand.
        """
        stated = corpus_file("celery/app/__init__.py", package="celery.app")

        self.assertEqual(("celery.app.__init__", "celery.app"), stated.keys)

    def test_a_java_header_declares_a_name_the_path_cannot_produce(self):
        stated = corpus_file(JAVA_PATH, package=JAVA_PACKAGE)

        self.assertEqual(
            ("src.main.java.org.apache.commons.lang3.stringutils",
             "org.apache.commons.lang3.stringutils"),
            stated.keys,
        )

    def test_a_header_with_no_package_line_keeps_the_one_key_it_always_had(self):
        self.assertEqual(("celery.worker.state",), corpus_file("celery/worker/state.py").keys)

    def test_both_keys_reach_the_same_file(self):
        corpus = Corpus(files=[corpus_file(JAVA_PATH, package=JAVA_PACKAGE)])
        by_key = corpus.by_key()

        self.assertEqual(set(corpus.keys), set(by_key))
        self.assertEqual({JAVA_PATH}, {f.path for f in by_key.values()})

    def test_the_declared_name_proves_its_parent_packages_exist(self):
        """Which is the point of a second key beyond the match itself.

        ``prefixes_of`` turns ``org.apache.commons.lang3.stringutils`` into
        four real PACKAGE entities. The path fold proves ``src``,
        ``src.main`` and ``src.main.java`` instead, which name directories
        Java has no word for.
        """
        corpus = Corpus(files=[corpus_file(JAVA_PATH, package=JAVA_PACKAGE)])

        self.assertLessEqual(
            {"org", "org.apache", "org.apache.commons", "org.apache.commons.lang3"},
            corpus.package_prefixes(),
        )

    def test_a_declared_name_two_files_claim_identifies_neither(self):
        """Maven's parallel main and test trees, which is where this bites.

        Both files declare ``org.ex.Cache`` and their paths differ, so the
        declared key is a coin flip between them. Dropped rather than
        resolved: the partial-evidence failure is a resolver that accepts the
        only candidate it can see and answers confidently.
        """
        corpus = Corpus(files=[
            corpus_file("src/main/java/org/ex/Cache.java", package="org.ex.Cache"),
            corpus_file("src/test/java/org/ex/Cache.java", package="org.ex.Cache"),
        ])

        with self.assertLogs("provenance.corpus", level="WARNING") as logged:
            by_key = corpus.by_key()

        self.assertNotIn("org.ex.cache", by_key)
        self.assertEqual(
            {"src.main.java.org.ex.cache", "src.test.java.org.ex.cache"}, set(by_key),
        )
        self.assertIn("org.ex.cache", logged.output[0])

    def test_a_declared_name_that_is_another_files_path_loses_to_the_path(self):
        """A path fold is this file's own identity; a declared name is a claim."""
        corpus = Corpus(files=[
            corpus_file("org/ex/cache.py"),
            corpus_file("src/main/java/org/ex/Cache.java", package="org.ex.Cache"),
        ])

        self.assertEqual("org/ex/cache.py", corpus.by_key()["org.ex.cache"].path)


class TestCorpus(unittest.TestCase):
    def setUp(self):
        self.corpus = Corpus(files=[
            corpus_file("celery/app/base.py"),
            corpus_file("celery/worker/state.py"),
            corpus_file("setup.py"),
        ])

    def test_length_is_the_number_of_indexed_documents(self):
        self.assertEqual(len(self.corpus), 3)

    def test_keys_and_by_key_agree(self):
        self.assertEqual(self.corpus.keys, set(self.corpus.by_key()))

    def test_a_directory_proved_by_an_indexed_path_is_a_package_prefix(self):
        # celery/app/base.py being in the corpus is proof that celery and
        # celery/app exist, which is what separates a legitimate package
        # entity from an invented module.
        self.assertEqual(self.corpus.package_prefixes(), {"celery", "celery.app", "celery.worker"})

    def test_a_single_segment_path_contributes_no_prefix(self):
        self.assertNotIn("setup", self.corpus.package_prefixes())

    def test_the_identity_is_the_value_the_headers_agree_on(self):
        self.assertEqual(self.corpus.git_slug, "celery-v2-5-5")
        self.assertEqual(self.corpus.git_repo, "https://github.com/celery/celery.git")
        self.assertFalse(self.corpus.multi_repo)
        self.assertEqual(self.corpus.slugs(), {"celery-v2-5-5"})

    def test_a_runtime_no_header_states_is_none(self):
        self.assertIsNone(self.corpus.runtime)

    def test_disagreeing_headers_take_the_majority_and_say_so(self):
        corpus = Corpus(files=[
            corpus_file("a.py", slug="celery-v2-5-5"),
            corpus_file("b.py", slug="celery-v2-5-5"),
            corpus_file("c.py", slug="celery-main"),
        ])
        with self.assertLogs("provenance.corpus", level="WARNING") as logged:
            self.assertEqual(corpus.git_slug, "celery-v2-5-5")
        self.assertIn("disagree on git_slug", logged.output[0])

    def test_an_empty_corpus_has_no_identity_rather_than_a_guess(self):
        empty = Corpus()
        self.assertIsNone(empty.git_slug)
        self.assertIsNone(empty.git_repo)
        self.assertEqual(len(empty), 0)

    def test_one_multi_repo_header_makes_the_whole_index_multi_repo(self):
        corpus = Corpus(files=[corpus_file("a.py"), corpus_file("b.py", multi_repo=True)])
        self.assertTrue(corpus.multi_repo)


class TestFromDocuments(unittest.TestCase):
    def test_every_parseable_header_becomes_a_file(self):
        corpus = from_documents(document_frame([
            ("d0", f'"""{HEADER}"""\nimport os\n'),
            ("d1", f'"""{HEADER.replace("worker/state", "app/base")}"""\n'),
        ]))
        self.assertEqual([f.path for f in corpus.files],
                         ["celery/worker/state.py", "celery/app/base.py"])
        self.assertEqual(corpus.unparsed, [])

    def test_an_unparseable_document_is_named_rather_than_dropped(self):
        # A document with no header is one the grounding pass cannot speak
        # for, and that is worth knowing before concluding its entities were
        # invented.
        with self.assertLogs("provenance.corpus", level="WARNING"):
            corpus = from_documents(document_frame([
                ("good.py", HEADER),
                ("mystery.py", "import os\n"),
            ]))
        self.assertEqual(corpus.unparsed, ["mystery.py"])
        self.assertEqual(len(corpus), 1)

    def test_a_table_without_a_text_column_yields_an_empty_corpus(self):
        import pandas as pd

        self.assertEqual(len(from_documents(pd.DataFrame({"id": [1]}))), 0)

    def test_documents_are_numbered_when_the_table_has_no_titles(self):
        import pandas as pd

        with self.assertLogs("provenance.corpus", level="WARNING"):
            corpus = from_documents(pd.DataFrame({"text": ["no header here"]}))
        self.assertEqual(corpus.unparsed, ["0"])


class TestTheSuffixesComeFromTheMapping(unittest.TestCase):
    """The folding strips what ``language_mappings.json`` says it strips.

    ``_SOURCE_SUFFIXES`` used to be a tuple written out beside this function,
    and the shipped mapping was the same answer given elsewhere. They drifted,
    and a Java corpus folded to keys nothing could match with no warning. The
    tuple is now derived, so the drift has nowhere to happen; these pin what
    the derivation currently produces.

    :mod:`tests.test_provenance_languages` holds the full per-language table.
    """

    def test_an_extension_of_a_folded_language_comes_off(self):
        for path, expected in [
            ("celery/worker/state.py", "celery.worker.state"),
            ("celery/worker/state.pyi", "celery.worker.state"),
            ("celery/worker/state.pyx", "celery.worker.state"),
            ("setup.cfg", "setup"),
            ("tox.ini", "tox"),
            ("pyproject.toml", "pyproject"),
            ("requirements.txt", "requirements"),
            ("roles/web/tasks.yaml", "roles.web.tasks"),
            ("roles/web/tasks.yml", "roles.web.tasks"),
        ]:
            with self.subTest(path=path):
                self.assertEqual(canonical_key(path), expected)

    def test_an_extension_the_folding_does_not_handle_stays_on(self):
        """Which is why the key never meets the model's dotted spelling.

        Left deliberately: stripping an extension the pipeline cannot parse
        would manufacture a match it has no grounds for.
        ``provenance.graph`` gates on the same question instead.
        """
        for path, expected in [
            ("src/main/kotlin/com/example/Foo.kt", "src.main.kotlin.com.example.foo.kt"),
            ("cmd/server/main.go", "cmd.server.main.go"),
            ("src/App.tsx", "src.app.tsx"),
        ]:
            with self.subTest(path=path):
                self.assertEqual(canonical_key(path), expected)

    def test_an_extension_a_grammar_reads_is_stripped_like_pythons(self):
        """``.java``, ``.ts`` and ``.rb`` joined the set with their dialects.

        The path fold on its own is not enough to ground a Java file, since
        Maven's layout is not the declared package; that is what the second
        key below is for. It is enough to stop the fold manufacturing a key
        ending in ``.java`` that nothing could ever match.
        """
        for path, expected in [
            ("src/main/java/com/example/Foo.java", "src.main.java.com.example.foo"),
            ("src/app/service.ts", "src.app.service"),
            ("src/app/service.mts", "src.app.service"),
            ("lib/thing.rb", "lib.thing"),
        ]:
            with self.subTest(path=path):
                self.assertEqual(canonical_key(path), expected)

    def test_a_name_with_no_extension_folds_to_itself(self):
        self.assertEqual(canonical_key("Makefile"), "makefile")
        self.assertEqual(canonical_key("bin/mytool"), "bin.mytool")

    def test_the_longest_matching_suffix_wins(self):
        """Ordering is by length, not by however the mapping happened to list.

        No folded language has a compound extension today, so this costs
        nothing now and stops being a latent bug the moment one does.
        """
        from provenance.languages import source_suffixes

        lengths = [len(s) for s in source_suffixes()]
        self.assertEqual(lengths, sorted(lengths, reverse=True))


if __name__ == "__main__":
    unittest.main()
