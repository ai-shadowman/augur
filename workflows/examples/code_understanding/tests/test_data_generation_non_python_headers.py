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

One wart is pinned rather than hidden. A ``.ts`` file is rendered here with
``language="javascript"`` because ``comment_delimiters_by_language`` in
``assets/mappings/language_mappings.json`` has no ``typescript`` entry and
``get_comment_delimiters_for_language`` raises on a language it has no mapping
for. The delimiters are the right ones either way, and a mixed checkout
detects ``javascript`` and reaches them; a pure-TypeScript checkout does not
get that far, because ``pygments_mappings`` has no ``typescript`` entry either
and the language is dropped before data generation runs a pass over it. That
is a gap in the mapping, not in the parser, and it is the one thing standing
between this and an indexed TypeScript repository.
"""

import os
import sys
import types
import unittest

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
from provenance import symbols  # noqa: E402
from provenance.declarations import module_key  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
