"""The header a Java, TypeScript or Ruby file gets, now that one is written.

Until this commit ``symbols.sections_for`` declined every non-Python suffix
and the three languages reached the extraction model with no ``Package:``,
``Dependencies:``, ``Classes:``, ``Functions:`` or ``Methods:`` at all. The
sibling ``test_data_generation_symbol_sections`` pins the Python rendering and
``test_provenance_dialects`` measures each grammar over a real checkout; this
is the seam between them, and the only place the two meet in bytes.

Byte-exact on purpose. Every claim this change makes about a non-Python
repository is a claim about what the extraction model reads, and a dict that
looks right can still render wrong: the sections are emitted in a fixed order,
``Dependencies:`` is flush left where the other three are indented by one
space, and an empty list is omitted rather than left headed. Java exercises
that last rule, since it declares no free functions and so gets no
``Functions:`` heading.

A ``.ts`` file is rendered here with ``language="javascript"``, and that is
the real language key rather than a stand-in. ``pygments_mappings`` folds the
``TypeScript`` lexer onto ``javascript`` the way it already folds ``bash``
onto ``shell``, so a pure-TypeScript checkout detects one language whose
extension list already claims ``.ts``, whose excluded directories already
name ``node_modules`` and ``dist``, and whose delimiters are already ``/* */``.
The alternative, a ``typescript`` key of its own, would have both language
keys claiming ``.ts`` and so walk every such file twice, calling the
extraction model twice and leaving ``metadata['language']`` decided by set
iteration order. ``TestAPureTypeScriptCheckoutDetects`` below pins the fold.

Which language key a file is ingested under does not decide which grammar
reads it. ``sections_for`` gates on the suffix, so the ``.ts`` files in that
pass get the TypeScript dialect and the ``.js`` files beside them get
``unsupported-language``.
"""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

if "pygments" not in sys.modules:
    pygments = types.ModuleType("pygments")
    lexers = types.ModuleType("pygments.lexers")
    lexers.guess_lexer_for_filename = lambda *_: None
    util = types.ModuleType("pygments.util")
    util.ClassNotFound = type("ClassNotFound", (Exception,), {})
    sys.modules.update({"pygments": pygments, "pygments.lexers": lexers, "pygments.util": util})

from pipelines.base.data_generation import generate_code_comment  # noqa: E402  stubs first
from provenance import graph, symbols  # noqa: E402
from provenance.corpus import Corpus, parse_header  # noqa: E402
from provenance.declarations import module_key  # noqa: E402
from utils import code_utils  # noqa: E402

#: ``conftest.real_dataframes`` swaps the MagicMock pandas out for the real one
#: on request. The grounding tests at the bottom of this module call
#: ``graph.ground``, which builds and indexes frames, and the module name does
#: not carry the ``test_provenance`` prefix the fixture otherwise keys on.
REAL_DATAFRAMES = True

JAVA_PATH = "src/main/java/org/apache/commons/lang3/StringUtils.java"

#: Maven layout, a declared package the directory does not spell, a
#: constructor, a static method, a constant and a nested enum carrying a
#: method of its own. The enum is there for the engine: its methods hang off
#: an ``enum_body_declarations`` node that ``_walk`` only reaches through the
#: direct-child-is-a-block branch.
JAVA = """package org.apache.commons.lang3;

import java.io.Serializable;
import java.util.Locale;

public class StringUtils implements Serializable {

    public static final String EMPTY = "";

    public StringUtils() {
        super();
    }

    public static boolean isEmpty(final CharSequence cs) {
        return cs == null || cs.length() == 0;
    }

    public enum Case {
        UPPER, LOWER;

        public String apply(final String value) {
            return this == UPPER ? value.toUpperCase(Locale.ROOT) : value.toLowerCase(Locale.ROOT);
        }
    }
}
"""

TYPESCRIPT_PATH = "packages/core/nest-application.ts"

#: A scoped npm import, a subpath import of a builtin, a relative import that
#: names no dependency, an interface, a class implementing it and a free
#: function. The interface and the free function are the two shapes Java
#: does not have.
TYPESCRIPT = """import { Logger } from "@nestjs/common";
import { readFile } from "node:fs/promises";
import { Loader } from "./loader";

export interface Boot {
  listen(port: number): void;
}

export class NestApplication implements Boot {
  private readonly logger = new Logger();

  listen(port: number): void {
    this.logger.log(port);
  }
}

export function bootstrap(): NestApplication {
  return new NestApplication();
}
"""

RUBY_PATH = "lib/jekyll/site.rb"

#: A module wrapping a class, which is how most of jekyll is written, plus a
#: ``require`` that names a gem and a ``require_relative`` that names a file
#: in the same repository.
RUBY = '''require "jekyll/reader"
require_relative "drops/site_drop"

module Jekyll
  class Site
    ATTRIBUTES = %w(safe).freeze

    def initialize(config)
      @config = config
    end

    def process
      reset
    end
  end
end
'''

GIT_REPO = "https://github.com/apache/commons-lang"
GIT_SLUG = "commons-lang-3-12-0"


def header_for(path: str, source: str, language: str) -> str:
    """Renders one file's header through the production formatter.

    The sibling's ``comment`` helper fixes the path and the language, because
    everything it renders is the same Python file. Here the path is the input
    and the language picks the comment delimiters, so both are arguments.
    """
    return generate_code_comment(
        metadata=dict(symbols.sections_for(path, source)), file_path=path, config=False,
        external_metadata=None, git_repo=GIT_REPO, git_slug=GIT_SLUG, language=language,
        multi_repo=False,
    )


#: ``(path, source, language)`` per language, with the language the pipeline
#: would pass. ``javascript`` for the ``.ts`` file: see the module docstring.
CASES = {
    "java": (JAVA_PATH, JAVA, "java"),
    "typescript": (TYPESCRIPT_PATH, TYPESCRIPT, "javascript"),
    "ruby": (RUBY_PATH, RUBY, "ruby"),
}


class TestTheHeaderEachLanguageNowGets(unittest.TestCase):
    """Three headers, byte for byte, delimiters included."""

    EXPECTED = {
        # No Functions: heading. commons-lang declares its free functions as
        # static methods because Java has nowhere else to put them, so the
        # section is empty and the formatter omits it.
        "java": (
            "/*This file is located at src/main/java/org/apache/commons/lang3/StringUtils.java "
            f"from repository url {GIT_REPO}, repository slug {GIT_SLUG}, multi_repo false\n"
            "\n Package: org.apache.commons.lang3.StringUtils\n"
            "\nDependencies:\n"
            "- [import] java.io.Serializable\n"
            "- [import] java.util.Locale\n"
            "\n Classes:\n"
            "- StringUtils\n"
            "- StringUtils.Case\n"
            "\n Methods:\n"
            "- StringUtils.StringUtils\n"
            "- StringUtils.isEmpty\n"
            "- StringUtils.Case.apply*/"
        ),
        "typescript": (
            "/*This file is located at packages/core/nest-application.ts "
            f"from repository url {GIT_REPO}, repository slug {GIT_SLUG}, multi_repo false\n"
            "\n Package: packages.core.nest-application\n"
            "\nDependencies:\n"
            "- [import] @nestjs/common\n"
            "- [import] node:fs\n"
            "\n Classes:\n"
            "- Boot\n"
            "- NestApplication\n"
            "\n Functions:\n"
            "- bootstrap\n"
            "\n Methods:\n"
            "- Boot.listen\n"
            "- NestApplication.listen*/"
        ),
        # =begin and =end, which MRI requires at the start of a line. The
        # formatter concatenates without a newline, as it does for Python's
        # triple quote, so the delimiter runs into the first word.
        "ruby": (
            "=beginThis file is located at lib/jekyll/site.rb "
            f"from repository url {GIT_REPO}, repository slug {GIT_SLUG}, multi_repo false\n"
            "\n Package: lib.jekyll.site\n"
            "\nDependencies:\n"
            "- [import] jekyll\n"
            "\n Classes:\n"
            "- Jekyll\n"
            "- Jekyll.Site\n"
            "\n Methods:\n"
            "- Jekyll.Site.initialize\n"
            "- Jekyll.Site.process=end"
        ),
    }

    def test_each_language_renders_exactly_this(self):
        for language, (path, source, delimiters) in CASES.items():
            with self.subTest(language=language):
                self.assertEqual(self.EXPECTED[language], header_for(path, source, delimiters))

    def test_no_section_is_claimed_from_a_parser_that_did_not_run(self):
        """Prediction 4: ``ast`` is Python's, so these three cannot report it.

        A single reader means two of the four outcome classes are unreachable
        here, which is what lets a header reader tell a recovered Python 2
        file from a Java one that simply parsed. Asserted with the sections
        non-empty, so it cannot pass by finding nothing.
        """
        for language, (path, source, _) in CASES.items():
            with self.subTest(language=language):
                metadata = symbols.sections_for(path, source)

                self.assertEqual(symbols.TREE_SITTER, metadata["symbol_evidence"])
                self.assertEqual(symbols.TREE_SITTER, metadata["import_evidence"])
                self.assertTrue(metadata["classes"])
                self.assertTrue(metadata["methods"])


class TestWhatThePackageLineAddressesTheFileBy(unittest.TestCase):
    """The one field whose answer is not the path for every language."""

    def test_java_states_its_package_and_the_directory_disagrees(self):
        """Maven puts the file four directories above the name it declares."""
        metadata = symbols.sections_for(JAVA_PATH, JAVA)

        self.assertEqual("org.apache.commons.lang3.StringUtils", metadata["package"])
        self.assertEqual("src.main.java.org.apache.commons.lang3.stringutils", module_key(JAVA_PATH))

    def test_the_fallback_is_the_path_fold_where_the_source_states_nothing(self):
        """TypeScript and Ruby name a file by where it is, as Python does."""
        for language in ("typescript", "ruby"):
            with self.subTest(language=language):
                path, source, _ = CASES[language]

                self.assertEqual(module_key(path), symbols.sections_for(path, source)["package"])


class TestTheBucketingDecisionsEachLanguageForces(unittest.TestCase):
    """Three choices the four header sections do not make for themselves."""

    def test_a_named_type_that_is_not_a_class_is_still_written_as_one(self):
        """Java ``enum``, TypeScript ``interface`` and Ruby ``module``.

        ``Classes:`` is the closest of the four buckets and omitting them
        would drop most of what a Java or Ruby file declares. Recorded here
        and in each dialect's docstring rather than left to be rediscovered
        from the output.
        """
        expected = {
            "java": ["StringUtils", "StringUtils.Case"],
            "typescript": ["Boot", "NestApplication"],
            "ruby": ["Jekyll", "Jekyll.Site"],
        }

        for language, (path, source, _) in CASES.items():
            with self.subTest(language=language):
                self.assertEqual(expected[language], symbols.sections_for(path, source)["classes"])

    def test_a_java_constructor_is_a_method_and_java_has_no_free_functions(self):
        """Prediction 1, on a file small enough to check by eye."""
        metadata = symbols.sections_for(JAVA_PATH, JAVA)

        self.assertIn("StringUtils.StringUtils", metadata["methods"])
        self.assertEqual([], metadata["functions"])

    def test_a_relative_specifier_names_a_file_rather_than_a_dependency(self):
        """``require_relative`` and ``./loader`` are the ``level`` rule again.

        Python's ``from . import x`` names this package, and ``imports_in_source``
        drops it for that reason. The same distinction has to hold per language
        or the grounding inventory fills with repository-internal paths that no
        manifest will ever declare.
        """
        self.assertEqual(["jekyll"], symbols.sections_for(RUBY_PATH, RUBY)["imports"])
        self.assertEqual(
            ["@nestjs/common", "node:fs"],
            symbols.sections_for(TYPESCRIPT_PATH, TYPESCRIPT)["imports"],
        )

    def test_a_suffix_the_java_mapping_claims_is_still_declined(self):
        """``file_extensions["java"]`` is ``['.java', '.kt']`` and Kotlin is not Java.

        The sharpest available case for why the suffix decides and the mapping
        does not: one entry names both, registering the Java dialect lifted
        one of them, and reading a Kotlin file with a Java grammar would put
        invented names in front of the extraction model. Registering a dialect
        is what lifts a language, one at a time and each arriving with its own
        measurement.
        """
        self.assertEqual(
            {"symbol_evidence": symbols.UNSUPPORTED, "import_evidence": symbols.UNSUPPORTED},
            symbols.sections_for("src/main/kotlin/Main.kt", "fun main() {}\n"),
        )


class TestAPureTypeScriptCheckoutDetects(unittest.TestCase):
    """Upstream of every parser, and the last thing between here and an index.

    A grammar that reads ``.ts`` perfectly is worth nothing if the file never
    reaches it. ``detect_languages`` raises outright on a repository where no
    file produces a mapped language, so before the ``typescript`` entry landed
    a checkout of nothing but ``.ts`` failed at the first stage with ``No
    languages detected``. A mixed checkout escaped it by accident: one ``.js``
    file detected ``javascript``, whose extension list already claims ``.ts``.
    The entry makes that accident the rule.

    Every assertion here is about the mapping rather than about pygments, so
    the lexer is patched the way the sibling ignore-policy tests patch it. The
    one claim that does depend on pygments is pinned separately and skipped
    where the stub at the top of this module is what got imported.
    """

    #: The three questions ``generate_raw_dataset`` asks of a detected
    #: language, in the order it asks them. Each raises ``ValueError`` on a
    #: language the corresponding mapping section omits, so a key that
    #: detection emits and these cannot answer is worse than no key at all:
    #: it turns a silent zero into a crash.
    INGESTION_QUESTIONS = (
        ("exclude_dirs", code_utils.get_exclude_dirs_for_language),
        ("file_extensions", code_utils.get_file_extensions_for_language),
        ("comment_delimiters", code_utils.get_comment_delimiters_for_language),
    )

    def detect(self, *filenames, lexer_name):
        with tempfile.TemporaryDirectory() as directory:
            for name in filenames:
                Path(directory, name).write_text("export const X = 1;\n", encoding="utf-8")
            with patch("utils.code_utils.guess_lexer_for_filename") as lexer:
                lexer.return_value.name = lexer_name
                return code_utils.get_detected_languages_for_repo(directory)

    def test_the_typescript_lexer_folds_onto_the_javascript_key(self):
        """``bash`` folds onto ``shell`` and ``transact-sql`` onto ``sql``.

        Folding a lexer name onto another language key is what this section
        is for, and six entries already did it before this one.
        """
        self.assertEqual(["javascript"], self.detect("a.ts", lexer_name="TypeScript"))

    def test_a_mixed_checkout_detects_the_same_single_language(self):
        """Which is why no file is walked twice and no model call is doubled.

        A ``typescript`` key of its own would collide with ``javascript`` over
        ``.ts``, and the pipeline runs one pass per detected language.
        """
        self.assertEqual(
            ["javascript"], self.detect("a.ts", "b.js", lexer_name="TypeScript"),
        )

    def test_the_detected_key_answers_what_ingestion_asks_next(self):
        for section, ask in self.INGESTION_QUESTIONS:
            with self.subTest(section):
                ask("javascript")

        self.assertIn(".ts", code_utils.get_file_extensions_for_language("javascript"))
        self.assertIn("node_modules", code_utils.get_exclude_dirs_for_language("javascript"))
        self.assertEqual(("/*", "*/"), code_utils.get_comment_delimiters_for_language("javascript"))

    def test_the_key_the_fold_avoids_would_answer_none_of_them(self):
        """The negative control, and the reason the fold is the cheaper fix.

        ``config_file_extensions`` is the single section that does name
        ``typescript``, which is how the omission stayed invisible: the config
        pass over such a repository would have worked.
        """
        for section, ask in self.INGESTION_QUESTIONS:
            with self.subTest(section), self.assertRaises(ValueError):
                ask("typescript")

        self.assertEqual(
            [".json", ".yaml", ".yml"],
            code_utils.get_config_file_extensions_for_language("typescript"),
        )

    def test_pygments_names_the_lexer_what_the_mapping_keys_on(self):
        """The one claim above that is not ours to decide.

        ``.mts`` has no lexer at all, which is why those files stay out of
        ``file_extensions``: detection could not see a repository made only of
        them, so claiming the extension would promise more than this delivers.
        """
        from pygments.lexers import guess_lexer_for_filename
        from pygments.util import ClassNotFound

        if guess_lexer_for_filename("a.ts", "") is None:
            self.skipTest("pygments is stubbed in this run, so the lexer name is ours")

        self.assertEqual(
            "typescript", guess_lexer_for_filename("a.ts", "").name.lower(),
        )
        with self.assertRaises(ClassNotFound):
            guess_lexer_for_filename("a.mts", "")


def ground(titles, *languages):
    """Index the named languages' headers and reconcile entities against them.

    The headers are the ones rendered above, parsed back by the same reader
    indexing uses, so this exercises the whole round trip rather than a
    hand-built :class:`Corpus`.
    """
    import pandas as pd

    corpus = Corpus(files=[
        parse_header(header_for(*CASES[language])) for language in languages
    ])
    entities = pd.DataFrame(
        [(t, "MODULE", f"The {t} module.") for t in titles],
        columns=["title", "type", "description"],
    )
    relationships = pd.DataFrame([], columns=["source", "target", "description", "weight"])
    entities, _, report = graph.ground(corpus, entities, relationships)
    return set(entities["title"].astype(str)), report


class TestTheGroundingGateNoLongerFiresForTheseThree(unittest.TestCase):
    """The definition of done, in its positive form.

    A gate that stopped warning because nothing reached it would pass a
    silence check, so each case asserts what was grounded as well as what was
    not declined: the model's entity keeps its place under the header's
    spelling, and no second entity is created beside it.
    """

    SLUG = GIT_SLUG.upper()

    JAVA_PACKAGES = {"ORG", "ORG.APACHE", "ORG.APACHE.COMMONS",
                     "ORG.APACHE.COMMONS.LANG3"}

    def test_a_java_entity_grounds_onto_the_package_the_header_declares(self):
        """The one language where the path fold alone would not have matched.

        The model is shown ``Package: org.apache.commons.lang3.StringUtils``
        and writes that back. Maven's four leading directories mean the path
        folds to something else entirely, so the declared name is the only
        key this can meet.
        """
        titles, report = ground(["ORG.APACHE.COMMONS.LANG3.STRINGUTILS"], "java")

        self.assertEqual([], report.unsupported_files)
        self.assertEqual([], report.ungrounded_modules)
        self.assertEqual([self.SLUG], report.created)
        self.assertEqual({JAVA_PATH.upper(), self.SLUG} | self.JAVA_PACKAGES, titles)
        # Rule 3 of ``packages.synthesize``, on the layout that motivated it.
        # The path fold proves ``src``, ``src.main`` and ``src.main.java``;
        # the declared name is what an import uses, so those three are not
        # written and the four that an import would name are.
        self.assertEqual(self.JAVA_PACKAGES, set(report.packages_created))
        self.assertFalse({t for t in report.packages_created if t.startswith("SRC")})

    def test_the_same_java_file_grounds_on_the_path_the_model_may_write_instead(self):
        """Two spellings, one file, and no duplicate entity between them."""
        titles, report = ground(
            ["ORG.APACHE.COMMONS.LANG3.STRINGUTILS",
             "src/main/java/org/apache/commons/lang3/StringUtils.java"],
            "java",
        )

        self.assertEqual([], report.ungrounded_modules)
        self.assertEqual({JAVA_PATH.upper(), self.SLUG} | self.JAVA_PACKAGES, titles)

    def test_typescript_and_ruby_ground_on_the_path_fold_as_python_does(self):
        """Neither states a package, so there is one key and it is the path."""
        titles, report = ground(
            ["PACKAGES.CORE.NEST-APPLICATION", "LIB.JEKYLL.SITE"], "typescript", "ruby",
        )

        self.assertEqual([], report.unsupported_files)
        self.assertEqual([], report.ungrounded_modules)
        self.assertEqual(
            {TYPESCRIPT_PATH.upper(), RUBY_PATH.upper(), self.SLUG}
            | {"PACKAGES", "PACKAGES.CORE", "LIB", "LIB.JEKYLL"},
            titles,
        )

    def test_a_java_file_whose_header_states_no_package_is_the_negative_control(self):
        """What the declared key buys, measured by taking it away.

        A header written before the symbol sections existed has only the path
        fold, so the model's declared spelling matches nothing, the entity
        stays ungrounded and a second one is created for the file beside it.
        That is the three-entity outcome the second key avoids.
        """
        import pandas as pd

        from provenance.corpus import CorpusFile

        corpus = Corpus(files=[CorpusFile(
            path=JAVA_PATH, git_repo=GIT_REPO, git_slug=GIT_SLUG, multi_repo=False,
        )])
        entities = pd.DataFrame(
            [("ORG.APACHE.COMMONS.LANG3.STRINGUTILS", "MODULE", "The utils module.")],
            columns=["title", "type", "description"],
        )
        _, _, report = graph.ground(
            corpus, entities,
            pd.DataFrame([], columns=["source", "target", "description", "weight"]),
        )

        self.assertEqual(["ORG.APACHE.COMMONS.LANG3.STRINGUTILS"], report.ungrounded_modules)
        # The module pass runs before the repository pass, so the invented
        # second entity is recorded first. Two created where the three tests
        # above create one, and the extra one is the duplicate.
        self.assertEqual([JAVA_PATH.upper(), self.SLUG], report.created)


if __name__ == "__main__":
    unittest.main()
