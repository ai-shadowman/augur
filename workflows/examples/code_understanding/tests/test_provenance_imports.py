import tempfile
import unittest
from pathlib import Path

from provenance import imports

SKIP_DIRS = frozenset({".git", "build"})

#: Python 2 source. ``except ... , e`` and the print statement both make this a
#: syntax error to a Python 3 parser, which is the case the line scan exists
#: for: celery 2.5.5 is full of them.
PY2_SOURCE = """\
import os
try:
    import pyinotify
except ImportError, exc:
    pyinotify = None
print "loaded", pyinotify
"""


class TestAstScan(unittest.TestCase):
    CASES = [
        ("import os", {"os"}),
        ("import os, sys", {"os", "sys"}),
        ("import kombu.pools", {"kombu"}),
        ("import numpy as np", {"numpy"}),
        ("from kombu.pools import get", {"kombu"}),
        ("from . import sibling", set()),
        ("from .relative.deep import thing", set()),
        ("def f():\n    import billiard\n", {"billiard"}),
        ("x = 1", set()),
    ]

    def test_top_level_names_are_recovered(self):
        for source, expected in self.CASES:
            with self.subTest(source=source):
                self.assertEqual(imports.imports_in_source(source), expected)

    def test_python2_source_is_a_syntax_error(self):
        # Pins the premise of the fallback. If this ever stops raising, the
        # line scan is dead code and should go.
        with self.assertRaises(SyntaxError):
            imports.imports_in_source(PY2_SOURCE)


class TestLineScan(unittest.TestCase):
    CASES = [
        ("import os", {"os"}),
        ("import os, sys", {"os", "sys"}),
        ("    import pyinotify", {"pyinotify"}),
        ("import kombu.pools as pools", {"kombu"}),
        ("from kombu.pools import get", {"kombu"}),
        ("from . import sibling", set()),
        ("from .relative import thing", set()),
        ("import os  # trailing comment", {"os"}),
        ("# import commented_out", set()),
        ("result = importlib.import_module('x')", set()),
        ("Use this to import data from a file.", set()),
    ]

    def test_lines_are_classified_without_parsing(self):
        for source, expected in self.CASES:
            with self.subTest(source=source):
                self.assertEqual(imports.imports_in_lines(source), expected)

    def test_python2_source_still_yields_its_imports(self):
        self.assertEqual(imports.imports_in_lines(PY2_SOURCE), {"os", "pyinotify"})


class TestScanTree(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write(self, relative, contents):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def test_names_are_collected_across_the_tree(self):
        self.write("pkg/__init__.py", "import kombu\n")
        self.write("pkg/sub/mod.py", "from anyjson import loads\n")
        names, warnings = imports.scan_tree(self.root, SKIP_DIRS)
        self.assertEqual(names, {"kombu", "anyjson"})
        self.assertEqual(warnings, [])

    def test_skipped_directories_are_not_walked(self):
        self.write("build/stale.py", "import ghost\n")
        names, _ = imports.scan_tree(self.root, SKIP_DIRS)
        self.assertEqual(names, set())

    def test_egg_info_is_skipped_without_being_listed(self):
        self.write("celery.egg-info/stale.py", "import ghost\n")
        names, _ = imports.scan_tree(self.root, SKIP_DIRS)
        self.assertEqual(names, set())

    def test_non_python_files_are_ignored(self):
        self.write("README.rst", "import ghost\n")
        names, _ = imports.scan_tree(self.root, SKIP_DIRS)
        self.assertEqual(names, set())

    def test_unparseable_files_contribute_names_and_one_warning(self):
        self.write("old.py", PY2_SOURCE)
        self.write("older.py", PY2_SOURCE)
        self.write("new.py", "import kombu\n")
        names, warnings = imports.scan_tree(self.root, SKIP_DIRS)
        self.assertEqual(names, {"os", "pyinotify", "kombu"})
        self.assertEqual(len(warnings), 1)
        self.assertIn("2 Python file(s)", warnings[0])
        self.assertIn("line scan", warnings[0])


if __name__ == "__main__":
    unittest.main()
