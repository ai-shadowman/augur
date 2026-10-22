"""What each registered dialect reads, recomputed from source on every run.

Python has an oracle: ``ast`` is the language's own definition of its syntax,
so ``tests/test_provenance_treesitter_agreement.py`` can assert equality and
mean it. The other three have no stdlib parser to check against, so the claim
here is weaker on purpose and said plainly rather than dressed up: the totals
below are what the tables produced the day they were written, on a named
checkout, and a change to any of them is a change in behaviour that has to be
explained.

That makes this a regression pin, not a correctness proof. Two things stop it
from being vacuous. The shape assertions below encode what each language is
*structurally* required to produce, independently of any count: Java declares
no free functions, Ruby qualifies with ``.``, every language reads something.
And the per-file-count guard means the totals are only compared when the
checkout is the one they were measured on, so a different version of jekyll
skips rather than failing with a number nobody can interpret.

The unit-level cases at the bottom need no checkout at all, and they are where
the decisions that are easy to get wrong are pinned: the Java constructor, the
enum body the walk would otherwise never enter, Ruby's ``class << self``, and
the TypeScript arrow-function binding.
"""

import os
import unittest
from pathlib import Path

from provenance import treesitter
from provenance.declarations import module_key
from provenance.dialects import DIALECTS, JAVA, PYTHON, RUBY, TYPESCRIPT, dialect_for, suffixes
from provenance.symbols import _classify

CANDIDATES = Path(
    os.environ.get("AUGUR_CANDIDATES", Path.home() / "Developer" / "augur-test" / "candidates")
)
EXCLUDE_PARTS = {".git", "node_modules", "__pycache__", "vendor"}
KINDS = ("classes", "functions", "methods", "bindings")

#: One checkout per language, and the figures it produced. ``files`` is the
#: guard: the rest is only asserted when the checkout on disk is the one these
#: came from.
MEASURED = {
    "java": {
        "checkout": "commons-lang",
        "files": 629,
        "errors": 1,
        "classes": 1139,
        "functions": 0,
        "methods": 9160,
        "bindings": 2335,
        "imports": 3569,
    },
    "typescript": {
        "checkout": "nest",
        "files": 2141,
        "errors": 2,
        "classes": 2054,
        "functions": 552,
        "methods": 3654,
        "bindings": 2771,
        "imports": 2946,
    },
    "ruby": {
        "checkout": "jekyll",
        "files": 160,
        "errors": 0,
        "classes": 330,
        "functions": 32,
        "methods": 1027,
        "bindings": 350,
        "imports": 135,
    },
}

#: Java states its package and Maven puts the file somewhere else entirely, so
#: the declared name and the path fold disagree on every file in the checkout.
#: Pinned as the total rather than as "most", because a single agreement would
#: mean a file escaped ``src/main/java`` and the layout assumption moved.
JAVA_PACKAGE_DISAGREEMENTS = 629


def files_for(dialect) -> list[Path]:
    root = CANDIDATES / MEASURED[dialect.name]["checkout"]
    if not root.is_dir():
        return []
    return sorted(
        path
        for suffix in dialect.suffixes
        for path in root.rglob(f"*{suffix}")
        if not (EXCLUDE_PARTS & set(path.relative_to(root).parts))
    )


def tally(dialect) -> dict:
    """Per-kind totals over one checkout, plus the files that needed recovery."""
    counts = dict.fromkeys((*KINDS, "imports"), 0)
    errors: list[str] = []
    root = CANDIDATES / MEASURED[dialect.name]["checkout"]

    for path in files_for(dialect):
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = treesitter.parse(source, dialect)
        if tree.root_node.has_error:
            errors.append(path.relative_to(root).as_posix())
        found = _classify(treesitter.symbols_in_tree(source, dialect), used_ast=False)
        for kind in KINDS:
            counts[kind] += len(getattr(found, kind))
        counts["imports"] += len(treesitter.imports_in_tree(source, dialect))

    counts["errors"] = len(errors)
    counts["error_files"] = errors
    return counts


class TestTheRegistry(unittest.TestCase):
    """Properties of the set of dialects, which need no checkout."""

    def test_no_two_dialects_claim_the_same_suffix(self):
        """``dialect_for`` returns the first match, so an overlap is a silent
        precedence rule nobody wrote down."""
        claimed: dict[str, str] = {}
        for dialect in DIALECTS.values():
            for suffix in dialect.suffixes:
                self.assertNotIn(
                    suffix,
                    claimed,
                    f"{suffix} is claimed by both {claimed.get(suffix)} and {dialect.name}",
                )
                claimed[suffix] = dialect.name

    def test_the_suffix_list_is_derived_from_the_registry(self):
        """Anything else drifts the moment a dialect is added."""
        every = {s for d in DIALECTS.values() for s in d.suffixes}
        self.assertEqual(every, set(suffixes()))
        self.assertEqual(list(suffixes()), sorted(suffixes(), key=len, reverse=True))

    def test_every_suffix_resolves_to_its_own_dialect(self):
        for dialect in DIALECTS.values():
            for suffix in dialect.suffixes:
                with self.subTest(suffix=suffix):
                    self.assertIs(dialect, dialect_for(f"a/b/thing{suffix}"))
                    self.assertIs(dialect, dialect_for(f"A/B/THING{suffix.upper()}"))

    def test_an_unregistered_suffix_resolves_to_nothing(self):
        """The negative case that keeps ``sections_for`` declining. ``.tsx``
        and ``.js`` are named rather than invented: both exist in the wild and
        neither has a corpus here to measure a table against."""
        for path in ("a.tsx", "a.js", "a.go", "a.kt", "a.cs", "Makefile"):
            with self.subTest(path=path):
                self.assertIsNone(dialect_for(path))

    def test_only_java_reads_its_address_out_of_the_file(self):
        """``package_of`` exists for the one language whose directory layout
        says something other than its package. Adding a second should be a
        deliberate act, not a copied line."""
        with_package = {d.name for d in DIALECTS.values() if d.package_of is not None}
        self.assertEqual({"java"}, with_package)


class TestTheShapeEachLanguageMustHave(unittest.TestCase):
    """Structural claims, true of any checkout, asserted on synthetic source.

    These are the ones worth keeping if every count above went stale.
    """

    def symbols(self, dialect, source):
        return _classify(treesitter.symbols_in_tree(source, dialect), used_ast=False)

    def test_java_has_no_free_functions_and_counts_the_constructor(self):
        found = self.symbols(JAVA, "package p;\nclass C { C() {} void m() {} }\n")

        self.assertEqual((), found.functions)
        self.assertEqual(("C.C", "C.m"), found.methods)

    def test_a_java_enum_body_is_entered(self):
        """``enum_body_declarations`` sits directly under ``enum_body``, and
        ``_blocks`` only reaches grandchildren. Without the direct-block
        branch in ``treesitter._walk`` every method on every Java enum is
        invisible, and nothing else in the suite would notice."""
        found = self.symbols(JAVA, "enum Color { RED, GREEN;\n  void shade() {}\n}\n")

        self.assertEqual(("Color",), found.classes)
        self.assertEqual(("Color.shade",), found.methods)
        self.assertEqual(("Color.RED", "Color.GREEN"), found.bindings)

    def test_every_java_named_type_lands_in_classes(self):
        """The decision the module docstring records, as an assertion. Four of
        the five are not classes in Java's sense and the header has no bucket
        that fits them better."""
        source = (
            "interface I { void m(); }\n"
            "enum E { A }\n"
            "record R(int x) {}\n"
            "@interface A { String v(); }\n"
            "class C {}\n"
        )

        self.assertEqual(("I", "E", "R", "A", "C"), self.symbols(JAVA, source).classes)

    def test_java_reads_the_declared_package_rather_than_the_path(self):
        source = "package org.apache.commons.lang3;\nclass StringUtils {}\n"
        root = treesitter.parse(source, JAVA).root_node
        path = "src/main/java/org/apache/commons/lang3/StringUtils.java"

        self.assertEqual("org.apache.commons.lang3.StringUtils", JAVA.package_of(path, root))
        self.assertNotEqual(JAVA.package_of(path, root).lower(), module_key(path))

    def test_a_java_file_in_the_unnamed_package_has_no_declared_address(self):
        """``None`` leaves the path fold as the only key, which is correct:
        there is nothing else to read."""
        root = treesitter.parse("class Loose {}\n", JAVA).root_node

        self.assertIsNone(JAVA.package_of("tools/Loose.java", root))

    def test_java_imports_keep_the_type_and_drop_the_static_member(self):
        source = (
            "import java.io.IOException;\n"
            "import java.util.*;\n"
            "import static org.junit.Assert.assertEquals;\n"
            "class C {}\n"
        )

        self.assertEqual(
            {"java.io.IOException", "java.util", "org.junit.Assert"},
            treesitter.imports_in_tree(source, JAVA),
        )

    def test_typescript_reads_a_bound_arrow_function_as_a_function(self):
        """168 module-level names in nest are declared this way against 384
        with the ``function`` keyword, so reading them as bindings would keep
        a third of the callables out of ``Functions:``."""
        source = "export const makeId = () => 1;\nconst n = 2;\nfunction plain() {}\n"
        found = self.symbols(TYPESCRIPT, source)

        self.assertEqual(("makeId", "plain"), found.functions)
        self.assertEqual(("n",), found.bindings)

    def test_a_bound_arrow_function_inside_a_class_is_a_method(self):
        """The prefix rule is the walk's, not the dialect's, so a callable
        binding gets the same enclosing-scope treatment a ``def`` gets."""
        found = self.symbols(TYPESCRIPT, "class C {\n  handle = () => 1;\n  size = 2;\n}\n")

        self.assertEqual(("C.handle",), found.methods)
        self.assertEqual(("C.size",), found.bindings)
        self.assertEqual((), found.functions)

    def test_typescript_unwraps_export(self):
        """``export`` wraps the declaration, the job ``decorated_definition``
        does in Python."""
        found = self.symbols(TYPESCRIPT, "export class A {}\nexport interface B { x: number }\n")

        self.assertEqual(("A", "B"), found.classes)

    def test_typescript_imports_reduce_to_the_npm_package(self):
        source = (
            "import { X } from '@nestjs/common/utils';\n"
            "import { Y } from 'rxjs/operators';\n"
            "import { Z } from './local';\n"
            "export { W } from 'iterare';\n"
            "const a = require('side');\n"
            "const b = await import('dyn');\n"
        )

        self.assertEqual(
            {"@nestjs/common", "rxjs", "iterare", "side", "dyn"},
            treesitter.imports_in_tree(source, TYPESCRIPT),
        )

    def test_a_typescript_overload_signature_does_not_duplicate_the_name(self):
        source = "export function f(a: number): void;\nexport function f(a: any) {}\n"

        self.assertEqual(("f",), self.symbols(TYPESCRIPT, source).functions)

    def test_ruby_qualifies_with_a_dot_so_the_key_folds(self):
        """``dotted_key`` folds ``.`` and ``/`` and does not fold ``::``, so a
        name spelled the Ruby way would never match the path's."""
        found = self.symbols(RUBY, "module Jekyll\n  class Site\n    def process; end\n  end\nend\n")

        self.assertEqual(("Jekyll", "Jekyll.Site"), found.classes)
        self.assertEqual(("Jekyll.Site.process",), found.methods)

    def test_ruby_singleton_class_keeps_the_enclosing_prefix(self):
        """``class << self`` has no name, so listing it as a class would make
        the walk skip its whole body. Left out, ``_blocks`` yields the body
        and the methods arrive where they are actually addressable from."""
        source = "class Site\n  class << self\n    def helper; end\n  end\nend\n"
        found = self.symbols(RUBY, source)

        self.assertEqual(("Site",), found.classes)
        self.assertEqual(("Site.helper",), found.methods)

    def test_ruby_reaches_a_class_behind_a_version_check(self):
        """The same compatibility-branch shape Python 2 codebases are full of."""
        source = 'if RUBY_VERSION > "2"\n  class Old\n    def go; end\n  end\nend\n'
        found = self.symbols(RUBY, source)

        self.assertEqual(("Old",), found.classes)
        self.assertEqual(("Old.go",), found.methods)

    def test_ruby_require_relative_names_no_gem(self):
        source = (
            "require 'json'\n"
            "require 'active_support/core_ext'\n"
            "require_relative 'jekyll/site'\n"
            "autoload :Foo, 'foo/bar'\n"
            "load 'script.rb'\n"
        )

        self.assertEqual(
            {"json", "active_support", "foo", "script"},
            treesitter.imports_in_tree(source, RUBY),
        )

    def test_no_dialect_enters_a_function_body(self):
        """The rule the Python oracle fixes, which every table has to match.

        A name declared inside a function is not addressable from outside it,
        and reporting it qualified would vouch for something nobody can
        import. Each source below declares ``Inner`` and ``deep`` where only a
        function body can reach them; neither may appear under any kind.
        """
        cases = {
            JAVA: "class C { void m() { class Inner { void deep() {} } } }\n",
            TYPESCRIPT: "function outer() {\n  class Inner { deep() {} }\n}\n",
            RUBY: "def outer\n  class Inner\n    def deep; end\n  end\nend\n",
            PYTHON: "def outer():\n    class Inner:\n        def deep(self):\n            pass\n",
        }
        for dialect, source in cases.items():
            with self.subTest(language=dialect.name):
                found = self.symbols(dialect, source)
                every = [name for kind in KINDS for name in getattr(found, kind)]
                self.assertNotIn("Inner", " ".join(every))
                self.assertNotIn("deep", " ".join(every))
                # Anti-vacuity: the enclosing declaration still arrives, so an
                # empty result cannot pass this.
                self.assertTrue(every, f"{dialect.name} read nothing at all")


class TestWhatTheCheckoutsProduce(unittest.TestCase):
    """The pinned totals. Skips rather than fails when the checkout is absent."""

    def tally_for(self, dialect):
        expected = MEASURED[dialect.name]
        found = files_for(dialect)
        if not found:
            self.skipTest(f"no {expected['checkout']} checkout under {CANDIDATES}")
        if len(found) != expected["files"]:
            self.skipTest(
                f"{expected['checkout']} has {len(found)} {dialect.name} file(s), not the "
                f"{expected['files']} these figures were measured on"
            )
        return tally(dialect), expected

    def test_the_totals_have_not_moved(self):
        for dialect in (JAVA, TYPESCRIPT, RUBY):
            counts, expected = self.tally_for(dialect)
            for kind in (*KINDS, "imports"):
                with self.subTest(language=dialect.name, kind=kind):
                    self.assertEqual(expected[kind], counts[kind])

    def test_every_language_reads_something(self):
        """Anti-vacuity. A table of node types that match nothing returns
        empty for every file and would otherwise pass the shape tests above,
        which are all about synthetic source."""
        for dialect in (JAVA, TYPESCRIPT, RUBY):
            counts, _ = self.tally_for(dialect)
            with self.subTest(language=dialect.name):
                self.assertGreater(counts["classes"], 0)
                self.assertGreater(counts["methods"], 0)
                self.assertGreater(counts["imports"], 0)

    def test_java_declares_no_free_functions_at_checkout_scale(self):
        """Zero is the interesting value here, and a zero is only meaningful
        beside the nine thousand methods the same pass found."""
        counts, _ = self.tally_for(JAVA)

        self.assertEqual(0, counts["functions"])
        self.assertGreater(counts["methods"], 1000)

    def test_the_grammars_understand_what_they_are_given(self):
        """Error recovery is reported, not acted on, so a grammar that stopped
        understanding a language would quietly return less rather than fail.
        The error files are named so a jump is readable rather than a number."""
        for dialect in (JAVA, TYPESCRIPT, RUBY):
            counts, expected = self.tally_for(dialect)
            with self.subTest(language=dialect.name):
                self.assertEqual(expected["errors"], counts["errors"], counts["error_files"])

    def test_the_declared_java_package_disagrees_with_the_path_everywhere(self):
        """The whole reason ``package_of`` exists. If this ever dropped to
        zero the second corpus key would be dead weight; if it dropped
        partway, some files would key one way and some the other."""
        files = files_for(JAVA)
        if len(files) != MEASURED["java"]["files"]:
            self.skipTest(f"no commons-lang checkout of the measured size under {CANDIDATES}")

        root = CANDIDATES / "commons-lang"
        differing = 0
        for path in files:
            relative = path.relative_to(root).as_posix()
            tree = treesitter.parse(path.read_text(encoding="utf-8", errors="replace"), JAVA)
            declared = JAVA.package_of(relative, tree.root_node)
            if declared and declared.lower() != module_key(relative):
                differing += 1

        self.assertEqual(JAVA_PACKAGE_DISAGREEMENTS, differing)


if __name__ == "__main__":
    unittest.main()
