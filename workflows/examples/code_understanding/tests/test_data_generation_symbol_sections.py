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
from provenance.declarations import declarations_in, module_key  # noqa: E402
from provenance.imports import imports_in_lines, imports_in_source  # noqa: E402

PROMPT = Path(BASE_DIR) / "assets" / "sdghub" / "code_generation" / "prompts" / "data_from_code.yaml"

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

    def test_module_key_answers_the_package_field_the_model_is_still_asked_for(self):
        self.assertEqual("celery.worker.state", module_key(PATH))
        self.assertEqual("celery.app", module_key("celery/app/__init__.py"))
        self.assertIn("**package**", PROMPT.read_text(encoding="utf-8"))


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

    def test_the_prompt_still_asks_for_all_four_sections(self):
        """Phase 3's deletion target, so the test fails once that lands and says why."""
        schema = PROMPT.read_text(encoding="utf-8").split("- role: user")[0]
        asked = re.findall(r"^\s*\d+\.\s+\*\*(?P<name>[^*]+)\*\*", schema, re.MULTILINE)

        self.assertEqual(["purpose", "package", "imports", "classes", "methods"], asked)


if __name__ == "__main__":
    unittest.main()
