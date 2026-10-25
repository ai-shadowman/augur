"""The edge oracle: Python's captures held to ``ast``, its resolution to known answers.

Two layers, tested separately on purpose. The captures (what the source says)
are compared between ``ast`` and tree-sitter on every celery file both parse,
and must agree exactly. The resolution (what that names) is shared by both
readers, so their agreement says nothing about it; it is held to hand-built
cases, one per rule, and to every edge of one real file checked by hand.

Registered before measuring: exact capture agreement on all 187 files; every
edge source joining a node key; INHERITS unresolved under 5%; internal IMPORTS
outnumbering external. The last one missed, 793 against 872, and the whole
difference is ``__future__``: 193 external edges to a language directive that
every import capture in this package counts. Without it, 793 against 679.

The celery pins did not catch the first pass's worst defects, because celery
does not exercise them: a package under ``webapp/`` read as external, a
case-folded lookup made trac's ``import SilverCity`` an edge to itself, and a
Python 3 file's ``import logging`` resolved onto a shadowing sibling. Each now
has a constructed case below, which is where this module's confidence comes
from; the celery totals pin only that nothing moved.
"""

import os
import tempfile
import textwrap
import unittest
import warnings
from pathlib import Path

from provenance.corpus import dotted_key
from provenance.declarations import base_captures_in_source, namespace_for
from provenance.dialects import DIALECTS, PYTHON, dialect_for
from provenance.edges import (
    EXTERNAL,
    IMPORTS,
    INHERITS,
    INTERNAL,
    RESOLVERS,
    UNRESOLVED,
    Inventory,
    scan_checkout,
)
from provenance.edges.python import PythonResolver
from provenance.imports import import_captures_in_source
from provenance.symbols import package_for
from provenance.treesitter import (
    CLASS,
    base_captures_in_tree,
    import_captures_in_tree,
    symbols_in_tree,
)

CANDIDATES = Path(
    os.environ.get("AUGUR_CANDIDATES", Path.home() / "Developer" / "augur-test" / "candidates")
)
CELERY = CANDIDATES / "celery"
CELERY_FILES = 237
WHY_SKIPPED = f"no celery 2.5.5 checkout at {CELERY}"

PKG = ("pkg/__init__.py", "pkg/m.py")
ABS = "from __future__ import absolute_import\n"
#: A statement only Python 2 has. With ``ast`` refusing the file, it is what
#: licenses reading an import the Python 2 way.
PY2 = "print 'python 2'\n"


def edges_of(files, path, source, kind=None):
    """``(kind, written, target, class)`` for one file against one checkout.

    ``files`` is paths (written empty) or a path-to-source mapping, laid out
    in a real directory because some rules read a second file.
    """
    files = files if isinstance(files, dict) else dict.fromkeys(files, "")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(textwrap.dedent(text))
        resolver = PythonResolver(Inventory(files, root))
        found, _ = resolver.edges(path, textwrap.dedent(source))
    return {
        (e.kind, e.written, e.target, e.target_class)
        for e in found
        if (kind is None or e.kind == kind) and e.written != "__future__"
    }


class TestImportResolution(unittest.TestCase):
    """One case per rule; each names the rule it pins."""

    CASES = [
        ("relative climbing past the root is unresolved",
         ("m.py",), "m.py", "from .. import x",
         {(IMPORTS, "..", None, UNRESOLVED)}),
        ("a shadowing sibling in a file of unknown version is unresolved",
         (*PKG, "pkg/states.py"), "pkg/m.py", "import states",
         {(IMPORTS, "states", None, UNRESOLVED)}),
        ("a package shadowing the standard library is not taken on Python 3",
         (*PKG, "pkg/logging.py"), "pkg/m.py", "import logging",
         {(IMPORTS, "logging", None, UNRESOLVED)}),
        ("Python 2 syntax: the sibling wins even where the absolute name exists",
         (*PKG, "pkg/states.py", "states.py"), "pkg/m.py", PY2 + "import states",
         {(IMPORTS, "states", "pkg.states", INTERNAL)}),
        ("Python 2 reads the first segment relative, then needs the rest",
         (*PKG, "pkg/a/__init__.py", "a/__init__.py", "a/b.py"), "pkg/m.py",
         PY2 + "import a.b",
         {(IMPORTS, "a.b", None, UNRESOLVED)}),
        ("Python 2's implicit relative import needs a package, not a directory",
         (*PKG, "pkg/a/b.py", "a/__init__.py", "a/b.py"), "pkg/m.py", PY2 + "import a.b",
         {(IMPORTS, "a.b", "a.b", INTERNAL)}),
        ("absolute_import settles it the other way",
         (*PKG, "pkg/states.py", "states.py"), "pkg/m.py", ABS + "import states",
         {(IMPORTS, "states", "states", INTERNAL)}),
        ("case-sensitive: a library is not the file of the same name in lowercase",
         ("tracopt/__init__.py", "tracopt/silvercity.py"), "tracopt/silvercity.py",
         "import SilverCity",
         {(IMPORTS, "SilverCity", "silvercity", EXTERNAL)}),
        ("src layout: the key is the file's own module key",
         ("src/pkg/__init__.py", "src/pkg/x.py", "src/pkg/m.py"), "src/pkg/m.py",
         ABS + "import pkg.x",
         {(IMPORTS, "pkg.x", "src.pkg.x", INTERNAL)}),
        ("a discovered root: the parent of a top-level package",
         ("webapp/graphite/__init__.py", "webapp/graphite/util.py",
          "webapp/graphite/cli/__init__.py", "webapp/graphite/cli/views.py"),
         "webapp/graphite/cli/views.py", ABS + "import graphite.util",
         {(IMPORTS, "graphite.util", "webapp.graphite.util", INTERNAL)}),
        ("a discovered root applies only to files beneath it",
         (*PKG, "tests/fixtures/proj/requests/__init__.py"), "pkg/m.py", ABS + "import requests",
         {(IMPORTS, "requests", None, UNRESOLVED)}),
        ("a directory inside a package is not a root",
         (*PKG, "pkg/compat/json/__init__.py"), "pkg/m.py", ABS + "import json",
         {(IMPORTS, "json", "json", EXTERNAL)}),
        ("a subpackage does not shadow the top-level name",
         (*PKG, "pkg/logging/__init__.py"), "pkg/m.py", ABS + "import logging",
         {(IMPORTS, "logging", "logging", EXTERNAL)}),
        ("a build copy is not a root, so it does not make the name ambiguous",
         ("celery/__init__.py", "celery/m.py", "build/lib/celery/__init__.py"),
         "celery/m.py", ABS + "import celery",
         {(IMPORTS, "celery", "celery", INTERNAL)}),
        ("a name both roots satisfy is ambiguous, not chosen",
         (*PKG, "pkg/x.py", "src/pkg/x.py"), "pkg/m.py", ABS + "import pkg.x",
         {(IMPORTS, "pkg.x", None, UNRESOLVED)}),
        ("a package wins over a module of the same name",
         ("pkg/__init__.py", "pkg.py", "m.py"), "m.py", "import pkg",
         {(IMPORTS, "pkg", "pkg", INTERNAL)}),
        ("from-import: an edge to the module, and to a name that is a file",
         (*PKG, "pkg/x.py"), "pkg/m.py", ABS + "from pkg import x, y",
         {(IMPORTS, "pkg", "pkg", INTERNAL), (IMPORTS, "pkg.x", "pkg.x", INTERNAL)}),
        ("no submodule edge where the package binds the name itself",
         {"pkg/__init__.py": "from .x import x\n", "pkg/x.py": "", "pkg/m.py": ""},
         "pkg/m.py", ABS + "from pkg import x",
         {(IMPORTS, "pkg", "pkg", INTERNAL)}),
        ("from . import x in the package binds the submodule, so the edge stays",
         {"pkg/__init__.py": "from . import x\n", "pkg/x.py": "", "pkg/m.py": ""},
         "pkg/m.py", ABS + "from pkg import x",
         {(IMPORTS, "pkg", "pkg", INTERNAL), (IMPORTS, "pkg.x", "pkg.x", INTERNAL)}),
        ("relative from-import of a submodule",
         (*PKG, "pkg/x.py"), "pkg/m.py", "from . import x",
         {(IMPORTS, ".", "pkg", INTERNAL), (IMPORTS, ".x", "pkg.x", INTERNAL)}),
        ("a missing file under an internal package is unresolved, not external",
         PKG, "pkg/m.py", ABS + "import pkg.gone",
         {(IMPORTS, "pkg.gone", None, UNRESOLVED)}),
        ("a namespace package: no file for it, and its submodules still found",
         ("ns/a/__init__.py", *PKG), "pkg/m.py", ABS + "from ns import a",
         {(IMPORTS, "ns", None, UNRESOLVED), (IMPORTS, "ns.a", "ns.a", INTERNAL)}),
        ("under a namespace package, a missing name is unresolved",
         ("ns/a/__init__.py", *PKG), "pkg/m.py", ABS + "import ns.a.gone",
         {(IMPORTS, "ns.a.gone", None, UNRESOLVED)}),
        ("outside the checkout is external, in full",
         PKG, "pkg/m.py", "import kombu.pools",
         {(IMPORTS, "kombu.pools", "kombu.pools", EXTERNAL)}),
        ("a stub beside its module is one file",
         (*PKG, "pkg/x.py", "pkg/x.pyi"), "pkg/m.py", ABS + "import pkg.x",
         {(IMPORTS, "pkg.x", "pkg.x", INTERNAL)}),
    ]

    def test_cases(self):
        for name, files, path, source, expected in self.CASES:
            with self.subTest(name):
                self.assertEqual(edges_of(files, path, source), expected)


class TestBaseResolution(unittest.TestCase):
    FILES = {
        **dict.fromkeys((*PKG, "pkg/x.py"), ""),
        "pkg/api.py": "__all__ = ['Base']\nclass Base: pass\nclass Hidden: pass\n",
        "pkg/loose.py": "class Base: pass\n",
        "pkg/grown.py": "__all__ = ['A']\n__all__.extend(['Base'])\nclass Base: pass\n",
    }

    CASES = [
        ("builtin, Python 3", "class A(object): pass",
         {(INHERITS, "object", "object", EXTERNAL)}),
        ("builtin, Python 2 only", "class E(StandardError): pass",
         {(INHERITS, "StandardError", "standarderror", EXTERNAL)}),
        ("imported name", ABS + "from pkg.x import Base\nclass A(Base): pass",
         {(INHERITS, "Base", "pkg.x.base", INTERNAL)}),
        ("imported from outside", "from kombu import Consumer\nclass A(Consumer): pass",
         {(INHERITS, "Consumer", "kombu.consumer", EXTERNAL)}),
        ("aliased module", ABS + "import pkg.x as px\nclass A(px.Base): pass",
         {(INHERITS, "px.Base", "pkg.x.base", INTERNAL)}),
        ("through the top package, longest file wins",
         ABS + "import pkg\nclass A(pkg.x.Base): pass",
         {(INHERITS, "pkg.x.Base", "pkg.x.base", INTERNAL)}),
        ("two imports of one target are one binding",
         ABS + "import pkg.x as x\nfrom pkg import x\nclass A(x.Base): pass",
         {(INHERITS, "x.Base", "pkg.x.base", INTERNAL)}),
        ("module level", "class Base: pass\nclass A(Base): pass",
         {(INHERITS, "Base", "pkg.m.base", INTERNAL)}),
        ("parenthesised, as ast reads it", "class Base: pass\nclass A((Base)): pass",
         {(INHERITS, "Base", "pkg.m.base", INTERNAL)}),
        ("enclosing class body",
         "class Outer:\n    class Inner: pass\n    class B(Inner): pass",
         {(INHERITS, "Inner", "pkg.m.outer.inner", INTERNAL)}),
        ("a binding's value is not syntax",
         "import threading\n_E = threading._Event\nclass E(_E): pass",
         {(INHERITS, "_E", None, UNRESOLVED)}),
        ("bound by an import and in the file",
         "try:\n    from k import Base\nexcept ImportError:\n    class Base: pass\n"
         "class A(Base): pass",
         {(INHERITS, "Base", None, UNRESOLVED)}),
        ("bound by two imports to different targets",
         "try:\n    from a import B\nexcept ImportError:\n    from b import B\n"
         "class A(B): pass",
         {(INHERITS, "B", None, UNRESOLVED)}),
        ("rebound by a with statement",
         "from k import Base\nwith x as Base: pass\nclass A(Base): pass",
         {(INHERITS, "Base", None, UNRESOLVED)}),
        ("an import inside a class body binds nothing at module level",
         "class O:\n    from k import Base\nclass A(Base): pass",
         {(INHERITS, "Base", None, UNRESOLVED)}),
        ("a Python 2 comprehension leaks its variable",
         PY2 + "from k import Base\n[0 for Base in range(3)]\nclass A(Base): pass",
         {(INHERITS, "Base", None, UNRESOLVED)}),
        ("an import inside a function binds nothing at module level",
         ABS + "def f():\n    from pkg.x import Base\nclass A(Base): pass",
         {(INHERITS, "Base", None, UNRESOLVED)}),
        ("an expression is unresolved; metaclass and **kw name no base",
         "class A(f(B), metaclass=M, **kw): pass",
         {(INHERITS, "f(B)", None, UNRESOLVED)}),
        ("an external star import may rebind anything",
         ABS + "from pkg.x import Base\nfrom kombu import *\nclass A(Base): pass",
         {(INHERITS, "Base", None, UNRESOLVED)}),
        ("an internal star import with a literal __all__ binds what it lists",
         ABS + "from pkg.api import *\nclass A(Base): pass\nclass B(object): pass",
         {(INHERITS, "Base", "pkg.api.base", INTERNAL),
          (INHERITS, "object", "object", EXTERNAL)}),
        ("a star-bound name bound again is unresolved",
         ABS + "from pkg.api import *\nclass Base: pass\nclass A(Base): pass",
         {(INHERITS, "Base", None, UNRESOLVED)}),
        ("the enclosing class body shadows a star import",
         ABS + "from pkg.api import *\nclass O:\n    class Base: pass\n    class B(Base): pass",
         {(INHERITS, "Base", "pkg.m.o.base", INTERNAL)}),
        ("an __all__ changed after its literal is not literal",
         ABS + "from pkg.grown import *\nclass C(Base): pass",
         {(INHERITS, "Base", None, UNRESOLVED)}),
        ("an internal star import without __all__ may rebind anything",
         ABS + "from pkg.loose import *\nclass A(Base): pass",
         {(INHERITS, "Base", None, UNRESOLVED)}),
        ("bound nowhere", "class A(Q): pass",
         {(INHERITS, "Q", None, UNRESOLVED)}),
        ("no base, empty parentheses, class inside a function",
         "class A: pass\nclass B(): pass\ndef f():\n    class C(object): pass",
         set()),
    ]

    def test_cases(self):
        for name, source, expected in self.CASES:
            with self.subTest(name):
                self.assertEqual(edges_of(self.FILES, "pkg/m.py", source, INHERITS), expected)


class TestCaptureAgreementOnShapesCeleryLacks(unittest.TestCase):
    """The two readers on syntax the corpus never exercises."""

    SOURCES = [
        ("comment inside parenthesised base", "class A((B  # c\n)): pass"),
        ("walrus base", "class A((B := D)): pass"),
        ("async function import", "async def f():\n    import x\n"),
        ("decorated function import", "@d\ndef f():\n    from y import z\n"),
        ("class body import", "class O:\n    from k import Base\n"),
        ("same import at two scopes", "import x\ndef f():\n    import x\n"),
        ("import in a class in a function", "def f():\n    class C:\n        import q\n"),
    ]

    def test_ast_and_tree_sitter_agree(self):
        for name, source in self.SOURCES:
            with self.subTest(name):
                self.assertEqual(import_captures_in_tree(source, PYTHON), import_captures_in_source(source))
                self.assertEqual(base_captures_in_tree(source, PYTHON), base_captures_in_source(source))


class TestCoverageIsClaimedOnlyWhereWritten(unittest.TestCase):
    def test_resolvers_and_captures_match(self):
        captured = {n for n, d in DIALECTS.items() if d.import_edges and d.base_names}
        self.assertEqual(set(RESOLVERS), captured)

    def test_a_dialect_without_a_capture_raises(self):
        from provenance.treesitter import Dialect

        bare = Dialect("python", (".py",), frozenset(), frozenset(), frozenset(), frozenset(), {},
                       lambda node: iter(()), lambda node: iter(()))
        with self.assertRaisesRegex(ValueError, "no edge capture written for dialect 'python'"):
            import_captures_in_tree("import x", bare)


@unittest.skipUnless(CELERY.is_dir(), WHY_SKIPPED)
class TestCeleryAgreement(unittest.TestCase):
    """The captures, ``ast`` against tree-sitter, on every file both parse."""

    #: On the 187 files ``ast`` reads. Totals, not a ratio, for the reason
    #: ``test_provenance_treesitter_agreement`` gives: only exact is interesting.
    #: An import made both at module level and inside a function is two
    #: captures, because only the first binds a name a class can see.
    AGREEMENT = {"imports": 1140, "bases": 375}
    #: What tree-sitter alone reads from the 50 files ``ast`` refuses.
    RECOVERED = {"imports": 565, "bases": 182}

    def test_exact_agreement(self):
        files = sorted(CELERY.rglob("*.py"))
        self.assertEqual(len(files), CELERY_FILES)
        measured = {"imports": 0, "bases": 0}
        recovered = {"imports": 0, "bases": 0}
        disagreeing = []
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            warnings.simplefilter("ignore", SyntaxWarning)
            for path in files:
                source = path.read_text(encoding="utf-8", errors="replace")
                tree = (import_captures_in_tree(source, PYTHON), base_captures_in_tree(source, PYTHON))
                try:
                    truth = (import_captures_in_source(source), base_captures_in_source(source))
                except (SyntaxError, ValueError, RecursionError):
                    recovered["imports"] += len(tree[0])
                    recovered["bases"] += len(tree[1])
                    continue
                measured["imports"] += len(truth[0])
                measured["bases"] += len(truth[1])
                if tree != truth:
                    disagreeing.append(
                        f"{path.relative_to(CELERY)}: {sorted(map(str, tree[0] ^ truth[0]))[:3]} "
                        f"{sorted(map(str, tree[1] ^ truth[1]))[:3]}"
                    )
        self.assertEqual(disagreeing, [], "a disagreement is a bug in a capture")
        self.assertEqual(measured, self.AGREEMENT)
        self.assertEqual(recovered, self.RECOVERED)


@unittest.skipUnless(CELERY.is_dir(), WHY_SKIPPED)
class TestCeleryScan(unittest.TestCase):
    """The resolved edges: totals pinned, sources joined, one file by hand."""

    BY_CLASS = {
        (IMPORTS, INTERNAL): 793, (IMPORTS, EXTERNAL): 872, (IMPORTS, UNRESOLVED): 5,
        (INHERITS, INTERNAL): 316, (INHERITS, EXTERNAL): 231, (INHERITS, UNRESOLVED): 9,
    }

    #: Every edge ``celery/app/base.py`` states, checked against the source by
    #: hand. ``..utils`` and ``..registry`` have no submodule edge because
    #: ``cached_property`` and ``tasks`` are names, not files; ``celery`` and
    #: ``multiprocessing.util`` are imported inside methods.
    BASE_PY = {
        (IMPORTS, "..", "celery", INTERNAL),
        (IMPORTS, "..backends", "celery.backends", INTERNAL),
        (IMPORTS, "..datastructures", "celery.datastructures", INTERNAL),
        (IMPORTS, "..exceptions", "celery.exceptions", INTERNAL),
        (IMPORTS, "..loaders", "celery.loaders", INTERNAL),
        (IMPORTS, "..platforms", "celery.platforms", INTERNAL),
        (IMPORTS, "..registry", "celery.registry", INTERNAL),
        (IMPORTS, "..result", "celery.result", INTERNAL),
        (IMPORTS, "..utils", "celery.utils", INTERNAL),
        (IMPORTS, ".defaults", "celery.app.defaults", INTERNAL),
        (IMPORTS, "celery", "celery", INTERNAL),
        (IMPORTS, "__future__", "__future__", EXTERNAL),
        *((IMPORTS, m, m, EXTERNAL) for m in (
            "contextlib", "copy", "functools", "kombu", "kombu.clocks",
            "multiprocessing.util", "os", "platform", "warnings",
        )),
        (INHERITS, "datastructures.ConfigurationView",
         "celery.datastructures.configurationview", INTERNAL),
        (INHERITS, "object", "object", EXTERNAL),
    }

    @classmethod
    def setUpClass(cls):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            warnings.simplefilter("ignore", SyntaxWarning)
            cls.scan = scan_checkout(CELERY)

    def test_files_by_evidence(self):
        self.assertEqual(dict(self.scan.files), {("python", "ast"): 187, ("python", "tree-sitter"): 50})

    def test_totals(self):
        self.assertEqual(self.scan.by_class(), self.BY_CLASS)

    def test_unresolved_carry_text_and_reason(self):
        for edge in self.scan.edges:
            with self.subTest(edge=edge):
                self.assertEqual(edge.target is None, edge.target_class == UNRESOLVED)
                self.assertEqual(edge.reason is not None, edge.target_class == UNRESOLVED)
                self.assertTrue(edge.written)

    def test_every_source_joins_a_node_key(self):
        modules, classes = set(), set()
        for path in sorted({e.path for e in self.scan.edges}):
            source = (CELERY / path).read_text(encoding="utf-8", errors="replace")
            dialect = dialect_for(path)
            modules.add(dotted_key(package_for(path, source, dialect)))
            namespace = namespace_for(path, source, dialect)
            classes |= {
                dotted_key(f"{namespace}.{name}" if namespace else name)
                for kind, name in symbols_in_tree(source, dialect) if kind == CLASS
            }
        unjoined = [
            e for e in self.scan.edges
            if e.source not in (modules if e.kind == IMPORTS else classes)
        ]
        self.assertEqual(unjoined, [])

    def test_base_py_by_hand(self):
        found = {
            (e.kind, e.written, e.target, e.target_class)
            for e in self.scan.edges if e.path == "celery/app/base.py"
        }
        self.assertEqual(found, self.BASE_PY)
