"""The four symbol sections Phase 3 moves from the model to the parser.

Phase 3 changes where ``Dependencies:``, ``Classes:``, ``Functions:`` and
``Methods:`` come from. It does not change how they render, and two readers
depend on that: ``provenance`` parses these sections back out of the written
corpus, and ``augur-test/harness`` reproduces them by hand to build its
fixtures. Neither is exercised by the data-generation tests, so the rendering
is pinned here byte for byte before the source changes underneath it.

The second half records what the oracle returns today, measured rather than
assumed. Three details decide how much work Phase 3 is, and none of them is
visible from the function signatures.
"""

import os
import re
import sys
import types
import unittest
import warnings
from pathlib import Path

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
from provenance.declarations import declarations_in, module_key  # noqa: E402
from provenance.imports import imports_in_lines, imports_in_source  # noqa: E402

PROMPT = Path(BASE_DIR) / "assets" / "sdghub" / "code_generation" / "prompts" / "data_from_code.yaml"

#: The candidate checkout the corpus-wide case runs over. Not part of this
#: repository, so that case skips when it is absent.
CANDIDATES = Path(
    os.environ.get("AUGUR_CANDIDATES", Path.home() / "Developer" / "augur-test" / "candidates")
)
CHECKOUT = CANDIDATES / "celery"

PATH = "celery/worker/state.py"

#: A Python 3 source. Chosen so one file exercises every section: two imports
#: that are dependencies and one that is not, a class, two methods, a
#: module-level function and a module-level binding.
SOURCE = '''from __future__ import absolute_import

import time
from celery import current_app

SOFTWARE = "celery"


class LocalCache(dict):
    """A dict with a bounded size."""

    def __init__(self, limit=None):
        self.limit = limit

    def get(self, key, default=None):
        return dict.get(self, key, default)


def revoke(task_id):
    return current_app.control.revoke(task_id)
'''

#: The same module as celery 2.5.5 actually ships it: ``except X, e`` and a
#: print statement, neither of which is Python 3. A third of the indexed
#: corpus looks like this.
PYTHON_2_SOURCE = '''import sys

class Worker(object):
    def start(self):
        try:
            self.run()
        except Exception, exc:
            print "failed: %s" % exc

def main():
    Worker().start()
'''


def comment(metadata):
    """Formats a header for ``PATH``, varying only the metadata."""
    return generate_code_comment(
        metadata=metadata, file_path=PATH, config=False, external_metadata=None,
        git_repo="https://github.com/celery/celery", git_slug="celery-v2-5-5",
        language="python", multi_repo=False,
    )


HEADER_LINE = (
    "This file is located at celery/worker/state.py "
    "from repository url https://github.com/celery/celery, "
    "repository slug celery-v2-5-5, multi_repo false"
)


class TestTheRenderingPhaseThreeMustNotChange(unittest.TestCase):
    """Byte-exact, because two consumers outside this file read these sections."""

    FULL = {
        "package": "celery.worker.state",
        "purpose": "Holds revoked task ids.",
        "imports": ["celery", "time"],
        "classes": ["localcache"],
        "functions": ["revoke"],
        "methods": ["localcache.__init__", "localcache.get"],
    }

    def test_the_sections_render_in_order_with_their_exact_spacing(self):
        # Note that Dependencies: is flush left and the other three headings
        # are indented by one space. That is not a typo here; it is what the
        # formatter emits, and the harness reproduces the inconsistency.
        expected = (
            '"""' + HEADER_LINE + "\n"
            "\n Package: celery.worker.state\n"
            "\n Purpose: Holds revoked task ids.\n"
            "\nDependencies:\n"
            "- [import] celery\n"
            "- [import] time\n"
            "\n Classes:\n"
            "- localcache\n"
            "\n Functions:\n"
            "- revoke\n"
            "\n Methods:\n"
            "- localcache.__init__\n"
            "- localcache.get" + '"""'
        )

        self.assertEqual(expected, comment(dict(self.FULL)))

    def test_an_empty_section_is_omitted_rather_than_left_headed(self):
        """A parser that finds nothing must say nothing, not head an empty list."""
        sparse = dict(self.FULL, imports=[], classes=[], functions=[], methods=[])

        result = comment(sparse)

        for heading in ("Dependencies:", "Classes:", "Functions:", "Methods:"):
            with self.subTest(heading=heading):
                self.assertNotIn(heading, result)
        self.assertIn("Purpose: Holds revoked task ids.", result)

    def test_a_method_may_be_a_bare_name_or_a_dict(self):
        """The parser will emit bare names; the model emits dicts. Both render alike."""
        as_dicts = dict(self.FULL, methods=[{"method_name": name} for name in self.FULL["methods"]])

        self.assertEqual(comment(dict(self.FULL)), comment(as_dicts))


class TestTheOracleFeedsThatFormatterUnchanged(unittest.TestCase):
    """What Phase 3 wires up, proven at the formatter boundary without wiring it."""

    def test_parser_output_renders_through_the_existing_formatter(self):
        declarations, used_ast = declarations_in(SOURCE)
        self.assertTrue(used_ast)

        result = comment({
            "package": module_key(PATH),
            "imports": sorted(imports_in_source(SOURCE)),
            "classes": sorted(declarations),
        })

        self.assertEqual(
            '"""' + HEADER_LINE + "\n"
            "\n Package: celery.worker.state\n"
            "\nDependencies:\n"
            "- [import] __future__\n"
            "- [import] celery\n"
            "- [import] time\n"
            "\n Classes:\n"
            "- localcache\n"
            "- localcache.__init__\n"
            "- localcache.get\n"
            "- revoke\n"
            "- software" + '"""',
            result,
        )

    def test_module_key_answers_the_package_field_the_model_used_to_be_asked_for(self):
        self.assertEqual("celery.worker.state", module_key(PATH))
        self.assertEqual("celery.app", module_key("celery/app/__init__.py"))


class TestWhatPhaseThreeStillHasToDecide(unittest.TestCase):
    """Three measured gaps between the oracle's output and the header's shape."""

    def test_declarations_in_attaches_no_kind_so_one_call_cannot_fill_three_sections(self):
        """The premise that did not survive Phase 1: a flat set, not a classification."""
        declarations, _ = declarations_in(SOURCE)

        self.assertEqual(
            {"localcache", "localcache.__init__", "localcache.get", "revoke", "software"},
            declarations,
        )
        # A class, two methods, a function and a module-level binding, with
        # nothing in the return value telling them apart. Qualification is a
        # hint, not a kind: localcache.get is a method and celery.app would be
        # a module. Something must classify before three sections can be written.
        self.assertTrue(all(isinstance(name, str) for name in declarations))

    def test_imports_in_source_raises_on_python_2_where_declarations_in_falls_back(self):
        """``declarations_in`` pairs the two scanners; imports has no such pair."""
        declarations, used_ast = declarations_in(PYTHON_2_SOURCE)
        self.assertFalse(used_ast)
        self.assertEqual({"main", "worker", "worker.start"}, declarations)

        with self.assertRaises(SyntaxError):
            imports_in_source(PYTHON_2_SOURCE)

        # The fallback exists, but only inlined in scan_tree, which walks a
        # tree and so cannot be called on one source string. Phase 3 needs the
        # combinator declarations_in already has, or it crashes on roughly a
        # third of celery 2.5.5.
        self.assertEqual({"sys"}, imports_in_lines(PYTHON_2_SOURCE))

    def test_future_is_returned_as_an_import_though_it_is_not_a_dependency(self):
        """A language directive would render as ``- [import] __future__``."""
        self.assertIn("__future__", imports_in_source(SOURCE))

    def test_the_prompt_no_longer_asks_for_what_the_parser_settles(self):
        """Phase 3's deletion, asserted so a re-added field fails rather than wins."""
        schema = PROMPT.read_text(encoding="utf-8").split("- role: user")[0]
        asked = re.findall(r"^\s*\d+\.\s+\*\*(?P<name>[^*]+)\*\*", schema, re.MULTILINE)

        self.assertEqual(["purpose", "methods"], asked)
        for gone in ("**package**", "**imports**", "**classes**", "**method_name**"):
            with self.subTest(field=gone):
                self.assertNotIn(gone, schema)


# --------------------------------------------------------------------------
# The whole checkout, not a sample
# --------------------------------------------------------------------------

EXCLUDE_PARTS = {".git", "__pycache__"}


def candidate_files():
    if not CHECKOUT.is_dir():
        return []
    return [
        path
        for path in sorted(CHECKOUT.rglob("*.py"))
        if not (EXCLUDE_PARTS & set(path.relative_to(CHECKOUT).parts))
    ]


FILES = candidate_files()

#: The split published alongside this change, measured on celery 2.5.5. The
#: 187 files ``ast`` accepts are these two classes together: 173 that declare
#: something and 14 that parse cleanly and declare nothing.
EXPECTED_FILES = 237
EXPECTED_SPLIT = {"ast": 173, "ast-empty": 14, "line-scan": 50, "line-scan-empty": 0}


def parse_sections(header: str) -> dict:
    """Read the symbol sections back out of a rendered header.

    The extraction model is not the only consumer; ``provenance`` parses these
    sections out of the written corpus too. Reading them back here is what
    makes "the header equals the oracle" a claim about the bytes rather than
    about the dict that produced them.
    """
    found = {"package": "", "imports": [], "classes": [], "functions": [], "methods": []}
    labels = {"Dependencies:": "imports", "Classes:": "classes",
              "Functions:": "functions", "Methods:": "methods"}
    section = None

    for line in header.strip('"').splitlines():
        stripped = line.strip()
        if stripped in labels:
            section = labels[stripped]
        elif stripped.startswith("Package: "):
            found["package"] = stripped[len("Package: "):]
        elif stripped.startswith("- ") and section:
            item = stripped[2:]
            found[section].append(item[len("[import] "):] if section == "imports" else item)
    return found


def oracle_imports(source: str) -> list:
    """Imports the way ``scan_tree`` has always chosen the scanner.

    Spelled out rather than calling ``imports_in``, so this stays an
    independent check on the combinator the header now goes through.
    """
    try:
        return sorted(imports_in_source(source))
    except SyntaxError:
        return sorted(imports_in_lines(source))


@unittest.skipIf(not FILES, f"no candidate checkout at {CHECKOUT}")
class TestEveryHeaderInTheCheckoutMatchesTheOracle(unittest.TestCase):
    """237 files through the production formatter, compared to the parsers.

    Two claims, and they chain. ``test_provenance_symbols`` proves the kinds
    fold back to ``declarations_in``; this proves the header carries those
    kinds without losing or inventing one on the way through
    ``generate_code_comment``.
    """

    @classmethod
    def setUpClass(cls):
        # Everything that compiles celery source happens here, under the
        # filter: 237 Python 2 files carry invalid escape sequences in their
        # docstrings and would bury the suite's real warnings.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            warnings.simplefilter("ignore", SyntaxWarning)
            cls.cases = {}
            for path in FILES:
                relative = path.relative_to(CHECKOUT).as_posix()
                source = path.read_text(encoding="utf-8", errors="replace")
                metadata = symbols.sections_for(relative, source)
                header = generate_code_comment(
                    metadata=dict(metadata), file_path=relative, config=False,
                    external_metadata=None, git_repo="https://github.com/celery/celery",
                    git_slug="celery-v2-5-5", language="python", multi_repo=False,
                )
                declared, _ = declarations_in(source)
                cls.cases[relative] = {
                    "metadata": metadata,
                    "rendered": parse_sections(header),
                    "declared": declared,
                    "bindings": {n.lower() for n in symbols.symbols_in(source).bindings},
                    "imports": oracle_imports(source),
                }

    def test_the_three_symbol_sections_are_exactly_what_the_oracle_declares(self):
        """Folded, and with bindings added back, the header is ``declarations_in``.

        Bindings are the one kind no section takes, so they are the one thing
        that has to be added back before the comparison. Without them this
        would be a subset check and a dropped class would pass it.
        """
        for relative, case in self.cases.items():
            with self.subTest(file=relative):
                declared, bindings = case["declared"], case["bindings"]
                in_header = {
                    name.lower()
                    for section in ("classes", "functions", "methods")
                    for name in case["rendered"][section]
                }

                self.assertEqual(
                    declared,
                    in_header | bindings,
                    f"{len(in_header - declared)} name(s) in the header the oracle does "
                    f"not declare ({sorted(in_header - declared)[:5]}), "
                    f"{len(declared - in_header - bindings)} declared but not written "
                    f"({sorted(declared - in_header - bindings)[:5]})",
                )

    def test_the_dependencies_section_is_exactly_what_the_file_imports(self):
        for relative, case in self.cases.items():
            with self.subTest(file=relative):
                self.assertEqual(case["imports"], case["rendered"]["imports"])

    def test_the_package_line_is_the_path_folded_not_a_model_answer(self):
        for relative, case in self.cases.items():
            with self.subTest(file=relative):
                self.assertEqual(module_key(relative), case["rendered"]["package"])

    def test_the_sections_reach_the_header_in_source_order(self):
        """Source order, not sorted order. The harness reproduces it that way."""
        for relative, case in self.cases.items():
            with self.subTest(file=relative):
                for section in ("classes", "functions", "methods"):
                    self.assertEqual(case["metadata"][section], case["rendered"][section])

    def test_the_parser_split_is_what_the_commit_message_published(self):
        if len(FILES) != EXPECTED_FILES:
            self.skipTest(
                f"{CHECKOUT} has {len(FILES)} files, not the {EXPECTED_FILES} of "
                f"celery 2.5.5 this split was measured on"
            )

        split = dict.fromkeys(EXPECTED_SPLIT, 0)
        for case in self.cases.values():
            split[case["metadata"]["symbol_evidence"]] += 1

        self.assertEqual(EXPECTED_SPLIT, split)


if __name__ == "__main__":
    unittest.main()
