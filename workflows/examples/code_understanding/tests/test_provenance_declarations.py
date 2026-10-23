import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from provenance import declarations
from provenance.dialects import DIALECTS, PYTHON, dialect_for
from provenance.models import RepoFacts

#: Python 2 source. ``except ... , e`` and the print statement both make this a
#: syntax error to a Python 3 parser, which is the case the fallback parser
#: exists for: 10 of the 22 documents a celery 2.5.5 run indexes look like this.
PY2_SOURCE = """\
TASK_STATES = frozenset(["PENDING"])

class LocalCache(dict):
    limit = 100

    def get(self, key):
        try:
            return dict.get(self, key)
        except KeyError, exc:
            print "missing", exc
            return None

def noop(*args):
    unused = 1
    return None
"""

HEADER = (
    '"""This file is located at celery/worker/state.py from repository url '
    "https://github.com/celery/celery, repository slug celery-v2-5-5, "
    'multi_repo false, runtime Python 2.7"""\n'
)


def document_frame(rows):
    """A minimal GraphRAG ``documents`` table: a header plus a body.

    pandas is imported here rather than at module scope because
    ``conftest.real_dataframes`` only swaps the stub out once the module is
    already imported.
    """
    import pandas as pd

    return pd.DataFrame([
        {"title": path, "text": HEADER.replace("celery/worker/state.py", path) + body}
        for path, body in rows
    ])


#: What ``generate_code_comment`` wraps a header in, per language. A Java
#: document opens with ``/*`` and a Ruby one with ``=begin``, so a frame built
#: with Python's delimiters would put a syntax error in front of the parser.
DELIMITERS = {"java": ("/*", "*/"), "ruby": ("=begin", "=end")}


def language_frame(language, rows):
    """A documents table for a language other than Python."""
    import pandas as pd

    begin, end = DELIMITERS[language]
    inner = HEADER.strip().strip('"')
    return pd.DataFrame([
        {
            "title": path,
            "text": f"{begin}{inner.replace('celery/worker/state.py', path)}{end}\n{body}",
        }
        for path, body in rows
    ])


class TestBothParsers(unittest.TestCase):
    """One table of cases, asserted against both readers.

    There used to be two tables, because the fallback was a regex line scan
    that got a different answer on half of them and had its own weaker
    expectations. Both readers are parsers now and were measured identical on
    every file the oracle accepts, so a single table is the honest shape: a
    case either describes what a declaration is or it does not, and the answer
    cannot depend on which one ran.
    """

    CASES = [
        ("def noop(): pass", {"noop"}),
        ("async def fetch(): pass", {"fetch"}),
        ("class LocalCache: pass", {"localcache"}),
        ("class LocalCache:\n    def get(self): pass\n", {"localcache", "localcache.get"}),
        ("TASK_STATES = frozenset()", {"task_states"}),
        ("count: int = 0", {"count"}),
        ("READY, FAILED = 1, 2", {"ready", "failed"}),
        # ``a = b = 1`` carries two targets on one ast node and nests rightward
        # in the grammar. Both spellings have to reach the same pair.
        ("a = b = 1", {"a", "b"}),
        # Augmented assignment rebinds a name that was already bound, so it is
        # not a declaration to either reader.
        ("x += 1", set()),
        # A decorator leaves a FunctionDef in place, so nothing special is needed.
        ("@task\ndef add(x): pass\n", {"add"}),
        # Compatibility branches declare real names, and a Python 2 codebase is
        # full of them.
        ("try:\n    class Queue: pass\nexcept ImportError:\n    Queue = None\n",
         {"queue"}),
        ("if True:\n    def maybe(): pass\n", {"maybe"}),
        # Nothing inside a function body is importable, so a local, a nested
        # def and a nested class are all dropped, while a class attribute at
        # the same indent is kept. A closure must never vouch for a name the
        # model invented.
        ("def outer():\n    def inner(): pass\n    local = 1\n", {"outer"}),
        ("def make():\n    class Inner:\n        def go(self): pass\n", {"make"}),
        ("class C:\n    attr = 1\n", {"c", "c.attr"}),
        ("class C:\n    def m(self): pass\nTOP = 1\n", {"c", "c.m", "top"}),
        # An attribute target binds no module-level name.
        ("self.conf = {}", set()),
        ("import os", set()),
        # A comparison is not a binding.
        ("if x == 1: pass", set()),
        ("# def commented_out(): pass", set()),
        # The case no line scan could ever get right: it had no way to know it
        # was inside a string.
        ('"""Docstring mentioning class Foo and def bar."""', set()),
    ]

    def test_ast_recovers_the_declared_names(self):
        for source, expected in self.CASES:
            with self.subTest(source=source):
                self.assertEqual(declarations.declarations_in_source(source), expected)

    def test_tree_sitter_recovers_the_same_names(self):
        for source, expected in self.CASES:
            with self.subTest(source=source):
                self.assertEqual(declarations.declarations_in_tree(source, PYTHON), expected)

    def test_python2_source_is_a_syntax_error(self):
        # Pins the premise of the fallback. If this ever stops raising, the
        # fallback is dead code and should go.
        with self.assertRaises(SyntaxError):
            declarations.declarations_in_source(PY2_SOURCE)

    def test_python2_source_falls_back(self):
        found, parsed = declarations.declarations_in(PY2_SOURCE, PYTHON)
        self.assertFalse(parsed)
        self.assertEqual(
            found,
            {"task_states", "localcache", "localcache.limit", "localcache.get", "noop"},
        )
        # ``unused`` is a function local, so it is not a declaration anyone
        # could import and must not be offered as proof that a name is real.
        self.assertNotIn("noop.unused", found)


class TestModuleKey(unittest.TestCase):
    CASES = [
        ("celery/worker/state.py", "celery.worker.state"),
        ("celery/app/__init__.py", "celery.app"),
        ("__init__.py", ""),
        ("setup.py", "setup"),
        ("celery/utils/compat.pyx", "celery.utils.compat"),
    ]

    def test_paths_fold_to_the_name_an_import_uses(self):
        for path, expected in self.CASES:
            with self.subTest(path=path):
                self.assertEqual(declarations.module_key(path), expected)


class TestResolveRelative(unittest.TestCase):
    #: (name, producing document, expected). The arithmetic is PEP 328: one dot
    #: is the package the document sits in, each further dot climbs one level.
    CASES = [
        ("..datastructures", "celery/app/base.py", "celery.datastructures"),
        (".state", "celery/worker/heartbeat.py", "celery.worker.state"),
        ("..utils.timer2", "celery/worker/job.py", "celery.utils.timer2"),
        ("...execute.trace", "celery/worker/consumer/base.py", "celery.execute.trace"),
        # A bare dot names the package itself.
        (".", "celery/worker/job.py", "celery.worker"),
        # An absolute name is not this function's business.
        ("celery.app", "celery/worker/job.py", None),
        # Over the top of the tree: no path can satisfy it.
        ("...state", "celery/worker.py", None),
        ("..state", "setup.py", None),
        # A document at the repository root sits in no package at all, so even
        # one dot has nothing to climb to. Refusing beats inventing a
        # root-level ``state``, which would be a candidate no file backs.
        (".state", "setup.py", None),
    ]

    def test_dots_resolve_against_the_producing_package(self):
        for name, document, expected in self.CASES:
            with self.subTest(name=name, document=document):
                self.assertEqual(declarations.resolve_relative(name, document), expected)

    def test_ambiguity_is_refused_rather_than_guessed(self):
        # The celery case: ``..abstract`` means celery.abstract from a document
        # two deep and celery.app.abstract from one three deep, and both files
        # exist. Only the producing document settles it, so with the document
        # unknown the answer has to be no answer.
        documents = ["celery/app/base.py", "celery/app/task/base.py"]
        real = {"celery.abstract", "celery.app.abstract", "celery.datastructures"}
        self.assertIsNone(declarations.resolve_unique("..abstract", documents, real))
        self.assertEqual(
            declarations.resolve_unique("..datastructures", documents, real),
            "celery.datastructures",
        )

    def test_relative_names_are_recognised(self):
        self.assertTrue(declarations.is_relative("..utils"))
        self.assertFalse(declarations.is_relative("celery.utils"))


class TestLookup(unittest.TestCase):
    SYMBOLS = {
        "celery.worker.state": {"task_accepted", "localcache", "localcache.get"},
        "celery.utils": {"noop", "qualname"},
    }

    def test_declared_in_prefers_the_longest_owner(self):
        found = declarations.declared_in("celery.worker.state.task_accepted", self.SYMBOLS)
        self.assertEqual(found, "celery.worker.state")

    def test_declared_in_finds_a_qualified_method(self):
        found = declarations.declared_in("celery.worker.state.localcache.get", self.SYMBOLS)
        self.assertEqual(found, "celery.worker.state")

    def test_declared_in_rejects_the_wrong_owner(self):
        self.assertIsNone(declarations.declared_in("celery.app.task_accepted", self.SYMBOLS))

    def test_declared_elsewhere_prefers_the_longest_symbol(self):
        index = declarations.owners_of(self.SYMBOLS)
        # ``get`` alone is noise; ``localcache.get`` is the specific evidence.
        self.assertEqual(
            declarations.declared_elsewhere("celery.app.localcache.get", index),
            {"celery.worker.state"},
        )

    def test_declared_elsewhere_reports_nothing_for_an_invention(self):
        index = declarations.owners_of(self.SYMBOLS)
        self.assertEqual(declarations.declared_elsewhere("celery.app.nonesuch", index), set())

    def test_attribution_names_the_symbol_as_well_as_its_owners(self):
        # The repair rebuilds the title from both halves, so the half that
        # matched has to come back with the half that owns it.
        index = declarations.owners_of(self.SYMBOLS)
        self.assertEqual(
            declarations.attribution_of("celery.app.localcache.get", index),
            ("localcache.get", {"celery.worker.state"}),
        )

    def test_attribution_reports_every_owner_of_a_shared_name(self):
        index = declarations.owners_of({
            "celery.worker.state": {"reset"}, "celery.utils": {"reset"},
        })
        symbol, owners = declarations.attribution_of("celery.app.reset", index)
        self.assertEqual(symbol, "reset")
        self.assertEqual(owners, {"celery.worker.state", "celery.utils"})

    def test_attribution_reports_nothing_for_an_invention(self):
        index = declarations.owners_of(self.SYMBOLS)
        self.assertIsNone(declarations.attribution_of("celery.app.nonesuch", index))


class TestTheEmptyOwner(unittest.TestCase):
    """Ruby keys its inventory under ``""``, so the lookups have to reach it.

    ``namespace_for`` returns ``""`` for a dialect whose walker already
    produced the whole address. Without the ``i = 0`` division in ``_splits``
    nothing can ever match such a key, and the arm with the best symbol
    performance this project has recorded grades 0.0.
    """

    #: Ruby, as ``scan_corpus`` now keys it, beside Python as it always was.
    SYMBOLS = {
        "": {"jekyll.tags.link", "jekyll.tags.link.render"},
        "celery.worker.state": {"task_accepted", "localcache", "localcache.get"},
    }

    def test_splits_opens_with_the_whole_key_under_no_owner(self):
        self.assertEqual(
            list(declarations._splits("a.b.c")),
            [("", "a.b.c"), ("a", "b.c"), ("a.b", "c")],
        )

    def test_a_single_segment_key_still_divides(self):
        # It used to yield nothing at all, so a bare name reached no lookup.
        self.assertEqual(list(declarations._splits("render")), [("", "render")])

    def test_declared_in_finds_a_symbol_that_carries_its_own_namespace(self):
        found = declarations.declared_in("jekyll.tags.link.render", self.SYMBOLS)
        self.assertEqual(found, "")

    def test_python_still_prefers_its_longest_real_owner(self):
        # ``declared_in`` walks the splits reversed, so ``""`` is tried last.
        # This is what keeps the empty owner from shadowing every Python key.
        found = declarations.declared_in("celery.worker.state.task_accepted", self.SYMBOLS)
        self.assertEqual(found, "celery.worker.state")

    def test_attribution_prefers_the_exact_title_over_a_suffix_of_it(self):
        # The whole key is now the first division tried rather than a
        # fall-through after the loop, so an exact match beats a shorter one.
        index = declarations.owners_of({
            "": {"jekyll.tags.link.render"}, "celery.app": {"render"},
        })
        symbol, owners = declarations.attribution_of("jekyll.tags.link.render", index)
        self.assertEqual(symbol, "jekyll.tags.link.render")
        self.assertEqual(owners, {""})


#: One Maven file, one Ruby file: the two languages whose symbols the walker
#: already qualifies, and so the two the path fold doubles.
JAVA_SOURCE = """\
package org.apache.commons.lang3.tuple;

public class Pair {
    public void accept(Object o) {}
}
"""

RUBY_SOURCE = """\
module Jekyll
  module Tags
    class Link
      def render(context); end
    end
  end
end
"""


class TestNamespaceFor(unittest.TestCase):
    """What has to be prepended to a walker's symbol to reach a qualified name.

    Python's answer is the path fold, which the join assumed for every
    language. Getting it wrong on the other three is what scored the symbol
    layer 0.0 on arms where the model had answered correctly, so each case
    here is a figure that moved.
    """

    CASES = [
        (
            "python qualifies by class and takes its module name from the path",
            "celery/app/task/sets.py", "class TaskSet:\n    def apply(self): pass\n",
            "celery.app.task.sets",
        ),
        (
            "a python package __init__ is addressed by its directory",
            "celery/app/__init__.py", "def default_app(): pass\n",
            "celery.app",
        ),
        (
            "typescript composes against the path the same way",
            "src/foo/bar.ts", "export class Baz { go() {} }\n",
            "src.foo.bar",
        ),
        (
            # The reason this project indexed commons-lang: the declared name
            # and the path disagree on all 629 files.
            "maven java takes the declared package with the type stem off",
            "src/main/java/org/apache/commons/lang3/tuple/Pair.java", JAVA_SOURCE,
            "org.apache.commons.lang3.tuple",
        ),
        (
            # Inferring "no package declared" from ``declared ==
            # module_key(path)`` looks equivalent and fires here, keeping the
            # stem and doubling the type name. ``package_of`` is asked instead.
            "java laid out flat still has its stem removed",
            "org/apache/foo/Pair.java",
            JAVA_SOURCE.replace("org.apache.commons.lang3.tuple", "org.apache.foo"),
            "org.apache.foo",
        ),
        (
            "the unnamed package leaves the type name as the whole address",
            "Pair.java", "public class Pair {}\n",
            "",
        ),
        (
            "ruby's walker already returned the whole module chain",
            "lib/jekyll/tags/link.rb", RUBY_SOURCE,
            "",
        ),
    ]

    def test_each_dialect_composes_its_own_way(self):
        for label, path, source, expected in self.CASES:
            with self.subTest(label):
                dialect = dialect_for(path)
                self.assertIsNotNone(dialect, path)
                self.assertEqual(declarations.namespace_for(path, source, dialect), expected)

    def test_every_registered_dialect_has_a_rule(self):
        # What makes the raise below unreachable in the product. Registering a
        # dialect means deciding how its symbols compose, which is a question
        # with a measurement behind it rather than a default.
        self.assertEqual(set(DIALECTS), set(declarations.NAMESPACE_RULES))

    def test_an_unrecorded_dialect_raises_rather_than_guessing(self):
        # Silently taking the path fold is exactly how the doubling arrived.
        unknown = replace(PYTHON, name="kotlin")
        with self.assertRaises(ValueError) as caught:
            declarations.namespace_for("a/b.kt", "", unknown)
        self.assertIn("kotlin", str(caught.exception))

    def test_the_prefix_is_folded_so_call_sites_do_not_have_to_be(self):
        # Compared against ``dotted_key`` of an entity title, which is folded.
        path = "src/main/java/org/apache/commons/lang3/tuple/Pair.java"
        found = declarations.namespace_for(path, JAVA_SOURCE, dialect_for(path))
        self.assertEqual(found, found.lower())


class TestScanFiles(unittest.TestCase):
    def test_only_python_files_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "celery").mkdir()
            (root / "celery" / "state.py").write_text("def task_accepted(): pass\n")
            (root / "celery" / "legacy.py").write_text(PY2_SOURCE)
            # A manifest is full of ``key = value`` lines, and reading them as
            # declarations would make real inventions look grounded.
            (root / "setup.cfg").write_text("[metadata]\nname = celery\n")

            symbols, warnings = declarations.scan_files(root, sorted(root.rglob("*")))

        self.assertEqual(symbols["celery.state"], {"task_accepted"})
        self.assertIn("noop", symbols["celery.legacy"])
        self.assertNotIn("setup", symbols)
        self.assertEqual(len(warnings), 1)
        self.assertIn("celery/legacy.py", warnings[0])

    def test_the_checkout_inventory_keys_per_dialect_too(self):
        # Two inventories exist, not one: this is the checkout, read through
        # ``facts.declared_symbols`` by the ``leaked`` bucket, and ``scan_corpus``
        # is the corpus. Keying them differently would compute ``leaked``
        # against a differently-composed inventory than ``misattributed``.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src/main/java/org/apache/commons/lang3/tuple").mkdir(parents=True)
            (root / "src/main/java/org/apache/commons/lang3/tuple/Pair.java").write_text(
                JAVA_SOURCE
            )
            (root / "lib/jekyll/tags").mkdir(parents=True)
            (root / "lib/jekyll/tags/link.rb").write_text(RUBY_SOURCE)

            symbols, _warnings = declarations.scan_files(root, sorted(root.rglob("*")))

        self.assertEqual(symbols["org.apache.commons.lang3.tuple"], {"pair", "pair.accept"})
        self.assertNotIn("src.main.java.org.apache.commons.lang3.tuple.pair", symbols)
        self.assertEqual(
            symbols[""],
            {"jekyll", "jekyll.tags", "jekyll.tags.link", "jekyll.tags.link.render"},
        )


class TestScanCorpus(unittest.TestCase):
    def test_declarations_are_keyed_by_the_header_path(self):
        frame = document_frame([
            ("celery/worker/state.py", "def task_accepted(): pass\n"),
            ("requirements/default.txt", "kombu = 2.1.8\n"),
        ])
        symbols = declarations.scan_corpus(frame)
        self.assertEqual(symbols, {"celery.worker.state": {"task_accepted"}})

    def test_a_java_document_is_keyed_by_what_an_import_would_name(self):
        # The defect this whole change exists for. Keyed by the path fold, a
        # model spelling ``org.apache.commons.lang3.tuple.Pair.accept`` joined
        # against nothing, and the correction rebuilt a title carrying the
        # type name twice.
        frame = language_frame("java", [
            ("src/main/java/org/apache/commons/lang3/tuple/Pair.java", JAVA_SOURCE),
        ])
        self.assertEqual(
            declarations.scan_corpus(frame),
            {"org.apache.commons.lang3.tuple": {"pair", "pair.accept"}},
        )

    def test_a_ruby_document_is_keyed_under_nothing_at_all(self):
        # Its walker already returned the whole address, so prepending the
        # path fold would name the namespace twice over.
        frame = language_frame("ruby", [("lib/jekyll/tags/link.rb", RUBY_SOURCE)])
        self.assertEqual(
            declarations.scan_corpus(frame),
            {"": {"jekyll", "jekyll.tags", "jekyll.tags.link", "jekyll.tags.link.render"}},
        )

    def test_the_header_itself_declares_nothing(self):
        # The header is a prepended string literal, so it parses and binds no
        # name. Worth pinning: if it ever became an assignment, every document
        # would start vouching for the same spurious symbol.
        frame = document_frame([("celery/worker/state.py", "")])
        self.assertEqual(declarations.scan_corpus(frame), {})


class TestCheckSymbolLayer(unittest.TestCase):
    #: Only ``celery/utils/timer2.py`` is in the checkout but not the corpus,
    #: which is what separates a leaked symbol from an invented one.
    FACTS = RepoFacts(
        root="/tmp/celery",
        declared_symbols={
            "celery.worker.state": ["task_accepted"],
            "celery.utils": ["localcache", "localcache.get"],
            "celery.utils.timer2": ["apply_at"],
        },
    )

    CASES = [
        ("CELERY.WORKER.STATE.TASK_ACCEPTED", "declared"),
        ("CELERY.UTILS.LOCALCACHE.GET", "declared"),
        ("CELERY.APP.TASK_ACCEPTED", "misattributed"),
        ("CELERY.UTILS.TIMER2.APPLY_AT", "leaked"),
        ("CELERY.WORKER.STATE.NONESUCH", "invented"),
        ("TASK_ACCEPTED", "bare"),
    ]

    def setUp(self):
        self.documents = document_frame([
            ("celery/worker/state.py", "def task_accepted(): pass\n"),
            ("celery/utils/__init__.py", "class LocalCache:\n    def get(self): pass\n"),
        ])

    def entity_frame(self, rows):
        import pandas as pd

        return pd.DataFrame(rows)

    def report(self, titles, facts=None):
        entities = self.entity_frame([{"title": t, "type": "function"} for t in titles])
        _findings, stats = declarations.check_symbol_layer(
            facts or self.FACTS, entities, self.documents,
        )
        return stats["symbol_layer"]

    def test_each_title_lands_in_the_bucket_its_evidence_supports(self):
        for title, bucket in self.CASES:
            with self.subTest(title=title):
                counts = self.report([title])
                self.assertEqual(counts[bucket], 1, counts)

    def test_every_symbol_entity_gets_a_verdict(self):
        titles = [title for title, _ in self.CASES]
        counts = self.report(titles)
        buckets = ("declared", "misattributed", "leaked", "invented", "bare")
        self.assertEqual(sum(counts[b] for b in buckets), len(titles))
        self.assertEqual(counts["precision"], round(2 / 6, 3))

    def test_only_class_and_function_entities_are_judged(self):
        entities = self.entity_frame([
            {"title": "CELERY.WORKER.STATE", "type": "module"},
            {"title": "KOMBU", "type": "library"},
            {"title": "CELERY.WORKER.STATE.TASK_ACCEPTED", "type": "function"},
        ])
        _findings, stats = declarations.check_symbol_layer(
            self.FACTS, entities, self.documents,
        )
        self.assertEqual(stats["symbol_layer"]["declared"], 1)
        self.assertEqual(stats["symbol_layer"]["bare"], 0)

    def test_a_missing_inventory_is_reported_not_hidden(self):
        # Without the checkout inventory, leakage cannot be told from
        # invention and the invented count absorbs both. The report has to say
        # so rather than let a zero be read as "no leakage".
        counts = self.report(["CELERY.UTILS.TIMER2.APPLY_AT"], RepoFacts(root="/tmp/celery"))
        self.assertEqual(counts["checkout_inventory"], "absent")
        self.assertEqual(counts["leaked"], 0)
        self.assertEqual(counts["invented"], 1)

    def test_findings_name_where_the_symbol_really_lives(self):
        entities = self.entity_frame(
            [{"title": "CELERY.APP.TASK_ACCEPTED", "type": "function"}]
        )
        findings, _stats = declarations.check_symbol_layer(
            self.FACTS, entities, self.documents,
        )
        self.assertEqual(len(findings), 1)
        self.assertIn("celery.worker.state", findings[0].actual)


if __name__ == "__main__":
    unittest.main()
