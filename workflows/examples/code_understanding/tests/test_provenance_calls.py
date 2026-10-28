"""The Python CALLS oracle: call sites held to ``ast``, resolution to constructed cases.

The same two layers as ``tests/test_provenance_edges.py``. The capture (where
the calls are) is compared with ``ast`` on every celery file both parse, and
must agree exactly. The resolution (which declaration a call names) is held to
hand-built cases, one per rule, because no corpus pin exercises the rules that
produce confident wrong edges: a local shadowing a module function, an opaque
``try: import`` binding, a diamond whose C3 order differs from a depth-first
walk, a class beside a same-named module.

The celery totals pin only that nothing moved, and are compared only on the
checkout they were measured on.
"""

import ast
import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from provenance.dialects import PYTHON
from provenance.edges import call_targets as ct
from provenance.edges.calls import CallResolver, scan_calls
from provenance.edges.model import EXTERNAL, INTERNAL, Inventory
from provenance.treesitter import parse, symbols_in_tree

CANDIDATES = Path(
    os.environ.get("AUGUR_CANDIDATES", Path.home() / "Developer" / "augur-test" / "candidates")
)
CELERY = CANDIDATES / "celery"
CELERY_FILES = 237
WHY_SKIPPED = f"no celery 2.5.5 checkout at {CELERY}"

#: What the walk produced on celery 2.5.5 the day it was written. ``ast`` and
#: tree-sitter agreed on all 7,253 call sites in the 187 files ``ast`` reads.
CELERY_SITES = 12360
CELERY_AST_SITES = 7253
CELERY_CATEGORIES = {
    "internal": 2998, "external": 1421, "builtin": 1635, "local": 2320, "self-attribute": 996, "attribute": 52, "dynamic": 340, "literal": 84, "binding": 617, "star": 90, "unbound": 1, "inherited": 1762, "ambiguous": 18, "import": 19, "module": 0, "not-declared": 7,
}
CELERY_INTERNAL_PAIRS = 2338


def calls_of(files: dict, path: str) -> list:
    """Every call in ``path``, resolved against a checkout of ``files``."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(textwrap.dedent(text))
        resolver = CallResolver(Inventory(files, root))
        found, _ = resolver.calls(path, textwrap.dedent(files[path]))
    return found


def call(files: dict, path: str, written: str):
    matches = [c for c in calls_of(files, path) if c.written == written]
    assert len(matches) == 1, f"{written!r}: {len(matches)} sites in {[c.written for c in calls_of(files, path)]}"
    return matches[0]


#: ``(case, files, file, callee as written, category, target)``. One row per
#: rule; the files are laid out in a real directory, since landing reads them.
CASES = [
    ("module function", {"m.py": "def f(): pass\ndef g():\n    f()\n"},
     "m.py", "f", INTERNAL, "m.f"),
    ("local shadows the module function", {"m.py": "def f(): pass\ndef g():\n    f = 1\n    f()\n"},
     "m.py", "f", ct.LOCAL, None),
    ("parameter shadows", {"m.py": "def f(): pass\ndef g(f):\n    f()\n"},
     "m.py", "f", ct.LOCAL, None),
    ("comprehension variable shadows", {"m.py": "def f(): pass\nx = [f() for f in []]\n"},
     "m.py", "f", ct.LOCAL, None),
    ("lambda parameter shadows", {"m.py": "def f(): pass\nh = lambda f: f()\n"},
     "m.py", "f", ct.LOCAL, None),
    # ``global`` skips the local scope, and declaring it says the function may
    # rebind the module's name, which the INHERITS resolver declines on too.
    ("global may rebind the module name", {"m.py": "def f(): pass\ndef g():\n    global f\n    f()\n"},
     "m.py", "f", ct.BINDING_REASON, None),
    ("closure belongs to its declaration", {"m.py": "def f(): pass\ndef g():\n    def h():\n        f()\n"},
     "m.py", "f", INTERNAL, "m.f"),
    ("opaque try-import binding", {"m.py": "try:\n    import pwd\nexcept ImportError:\n    pwd = None\n"
                                            "def g():\n    pwd.getpwnam('x')\n"},
     "m.py", "pwd.getpwnam", ct.BINDING_REASON, None),
    ("instantiation is a call to the class", {"m.py": "class C(object): pass\nC()\n"},
     "m.py", "C", INTERNAL, "m.c"),
    ("from-import", {"p/__init__.py": "", "p/a.py": "def f(): pass\n", "m.py": "from p.a import f\nf()\n"},
     "m.py", "f", INTERNAL, "p.a.f"),
    ("re-export followed to its declaration",
     {"p/__init__.py": "from p.impl import f\n", "p/impl.py": "def f(): pass\n", "m.py": "from p import f\nf()\n"},
     "m.py", "f", INTERNAL, "p.impl.f"),
    ("a class beside a same-named module",
     {"p/__init__.py": "from p.signal import Signal\n", "p/signal.py": "class Signal(object): pass\n",
      "m.py": "from p import Signal\nSignal()\n"},
     "m.py", "Signal", INTERNAL, "p.signal.signal"),
    ("dotted module path", {"p/__init__.py": "", "p/a.py": "def f(): pass\n", "m.py": "import p.a\np.a.f()\n"},
     "m.py", "p.a.f", INTERNAL, "p.a.f"),
    ("a submodule the package also binds",
     {"p/__init__.py": "a = 1\n", "p/a.py": "def f(): pass\n", "m.py": "import p.a\np.a.f()\n"},
     "m.py", "p.a.f", ct.AMBIGUOUS, None),
    ("function-local import", {"p/__init__.py": "", "p/a.py": "def f(): pass\n",
                               "m.py": "def g():\n    from p.a import f\n    f()\n"},
     "m.py", "f", INTERNAL, "p.a.f"),
    ("builtin", {"m.py": "len([])\n"}, "m.py", "len", ct.BUILTIN, "len"),
    ("Python 2 builtin", {"m.py": "print 'x'\nreduce(None, [])\n"}, "m.py", "reduce", ct.BUILTIN, "reduce"),
    ("external module function", {"m.py": "import os\nos.path.join('a')\n"},
     "m.py", "os.path.join", EXTERNAL, "os.path.join"),
    ("star import with no literal __all__", {"m.py": "from os.path import *\njoin('a')\n"},
     "m.py", "join", ct.STAR, None),
    ("module function bound twice", {"m.py": "def f(): pass\nf = 2\nf()\n"},
     "m.py", "f", ct.BINDING_REASON, None),
    ("self method", {"m.py": "class C(object):\n    def a(self):\n        self.b()\n    def b(self): pass\n"},
     "m.py", "self.b", INTERNAL, "m.c.b"),
    ("self method inherited from an internal base",
     {"m.py": "class B(object):\n    def b(self): pass\nclass C(B):\n    def a(self):\n        self.b()\n"},
     "m.py", "self.b", INTERNAL, "m.b.b"),
    ("an external base comes first",
     {"m.py": "import threading\nclass B(object):\n    def run(self): pass\n"
              "class C(threading.Thread, B):\n    def a(self):\n        self.run()\n"},
     "m.py", "self.run", ct.INHERITED, None),
    ("C3, not depth first",
     {"m.py": "class A(object):\n    def m(self): pass\nclass B(A): pass\nclass C(A):\n    def m(self): pass\n"
              "class D(B, C):\n    def a(self):\n        self.m()\n"},
     "m.py", "self.m", INTERNAL, "m.c.m"),
    ("an instance attribute shadows the method",
     {"m.py": "class C(object):\n    def __init__(self):\n        self.b = None\n"
              "    def a(self):\n        self.b()\n    def b(self): pass\n"},
     "m.py", "self.b", ct.SELF_ATTRIBUTE, None),
    ("staticmethod has no receiver",
     {"m.py": "class C(object):\n    @staticmethod\n    def a(self):\n        self.b()\n    def b(self): pass\n"},
     "m.py", "self.b", ct.LOCAL, None),
    ("rebound receiver",
     {"m.py": "class C(object):\n    def a(self):\n        self = 1\n        self.b()\n    def b(self): pass\n"},
     "m.py", "self.b", ct.LOCAL, None),
    ("a receiver's attribute", {"m.py": "class C(object):\n    def a(self):\n        self.x.y()\n"},
     "m.py", "self.x.y", ct.SELF_ATTRIBUTE, None),
    ("super()",
     {"m.py": "class B(object):\n    def a(self): pass\nclass C(B):\n    def a(self):\n        super(C, self).a()\n"},
     "m.py", "super(C, self).a", INTERNAL, "m.b.a"),
    ("super() past an opaque base",
     {"m.py": "import threading\nclass C(threading.Thread):\n    def run(self):\n        super(C, self).run()\n"},
     "m.py", "super(C, self).run", ct.INHERITED, None),
    ("class body helper", {"m.py": "class C(object):\n    def h(): pass\n    x = h()\n"},
     "m.py", "h", INTERNAL, "m.c.h"),
    ("a class body does not enclose its methods",
     {"m.py": "def h(): pass\nclass C(object):\n    def h(self): pass\n    def a(self):\n        h()\n"},
     "m.py", "h", INTERNAL, "m.h"),
    ("method of a literal", {"m.py": "', '.join([])\n"}, "m.py", "', '.join", ct.LITERAL, None),
    ("call result", {"m.py": "def f(): pass\nf()()\n"}, "m.py", "f()", ct.DYNAMIC, None),
    ("abstract method", {"m.py": "class C(object):\n    def a(self):\n        self.b()\n"},
     "m.py", "self.b", ct.NOT_DECLARED, None),
    ("attribute of a function", {"m.py": "def f(): pass\nf.delay()\n"}, "m.py", "f.delay", ct.ATTRIBUTE, None),
    ("a local alias is followed", {"m.py": "class Request(object): pass\ndef f():\n    Req = Request\n    Req()\n"},
     "m.py", "Req", INTERNAL, "m.request"),
    ("an alias of a method through self",
     {"m.py": "class C(object):\n    def run(self): pass\n    def go(self):\n        fn = self.run\n        fn()\n"},
     "m.py", "fn", INTERNAL, "m.c.run"),
    ("an alias bound twice is a local",
     {"m.py": "class A(object): pass\nclass B(object): pass\ndef f(x):\n    k = A\n    if x:\n        k = B\n    k()\n"},
     "m.py", "k", ct.LOCAL, None),
    ("an alias of a parameter is a local",
     {"m.py": "def f(cls):\n    k = cls\n    k()\n"}, "m.py", "k", ct.LOCAL, None),
    ("self.__class__ is the enclosing class",
     {"m.py": "class C(object):\n    def copy(self):\n        return self.__class__()\n"},
     "m.py", "self.__class__", INTERNAL, "m.c"),
    # From the adversarial review, 2026-10-28: each was a confident wrong edge.
    ("an instance attribute set in a base shadows a subclass method",
     {"m.py": "class B(object):\n    def __init__(self):\n        self.run = None\n"
              "class C(B):\n    def run(self): pass\n    def go(self):\n        self.run()\n"},
     "m.py", "self.run", ct.SELF_ATTRIBUTE, None),
    ("an instance attribute set later in the order still shadows",
     {"m.py": "class A(object):\n    def run(self): pass\nclass B(object):\n    def __init__(self):\n"
              "        self.run = None\nclass C(A, B):\n    def go(self):\n        self.run()\n"},
     "m.py", "self.run", ct.SELF_ATTRIBUTE, None),
    ("another file's global rebinding",
     {"m.py": "def f(): pass\ndef install():\n    global f\n    f = len\n", "n.py": "import m\nm.f()\n"},
     "n.py", "m.f", ct.BINDING_REASON, None),
    ("another file's star import may rebind",
     {"m.py": "def f(): pass\ntry:\n    from _speed import *\nexcept ImportError:\n    pass\n",
      "n.py": "import m\nm.f()\n"},
     "n.py", "m.f", ct.STAR, None),
    ("another file's with target",
     {"m.py": "def f(): pass\nwith open('x') as f:\n    pass\n", "n.py": "import m\nm.f()\n"},
     "n.py", "m.f", ct.BINDING_REASON, None),
    ("a star-imported name its module rebinds",
     {"a.py": "__all__ = ['f']\ndef f(): pass\nfor f in [len]:\n    pass\n", "n.py": "from a import *\nf()\n"},
     "n.py", "f", ct.BINDING_REASON, None),
    ("a module-level call before the def reaches the builtin",
     {"m.py": "x = open('f')\ndef open(p): pass\n"}, "m.py", "open", ct.BUILTIN, "open"),
    ("a call before any binding",
     {"m.py": "@deco\ndef g(): pass\ndef deco(f): return f\n"}, "m.py", "deco", ct.UNBOUND, None),
    ("a class-body call before the class's def reaches the module",
     {"m.py": "def f(): pass\nclass C(object):\n    x = f()\n    def f(self): pass\n"},
     "m.py", "f", INTERNAL, "m.f"),
    ("super(Inner, self) in a nested class names the module's Inner",
     {"m.py": "class A(object):\n    def m(self): pass\nclass Inner(A): pass\nclass Mid(Inner):\n"
              "    def m(self): pass\nclass Outer(object):\n    class Inner(Mid):\n        def m(self):\n"
              "            super(Inner, self).m()\n"},
     "m.py", "super(Inner, self).m", ct.DYNAMIC, None),
    ("a Python 2 list comprehension in a class body sees the class",
     {"m.py": "print 'x'\ndef f(): pass\nclass C:\n    def f(): pass\n    xs = [f() for i in range(2)]\n"},
     "m.py", "f", INTERNAL, "m.c.f"),
    ("a class-body import shadows an inherited method",
     {"util.py": "def run(): pass\n", "m.py": "class B(object):\n    def run(self): pass\nclass C(B):\n"
                                              "    from util import run\n    def go(self):\n        self.run()\n"},
     "m.py", "self.run", ct.BINDING_REASON, None),
    ("a class-body for target shadows an inherited method",
     {"m.py": "class B(object):\n    def run(self): pass\nclass C(B):\n    for run in [len]:\n        pass\n"
              "    def go(self):\n        self.run()\n"},
     "m.py", "self.run", ct.BINDING_REASON, None),
    ("a method rewrapped by assignment has no receiver",
     {"m.py": "class C(object):\n    def go(self): pass\n    def helper(x):\n        x.go()\n"
              "    helper = staticmethod(helper)\n"},
     "m.py", "x.go", ct.LOCAL, None),
    ("starred unpacking rebinds", {"m.py": "def f(): pass\na, *f = [1, len]\nf()\n"},
     "m.py", "f", ct.BINDING_REASON, None),
    ("del unbinds", {"m.py": "def f(): pass\ndel f\nf()\n"}, "m.py", "f", ct.BINDING_REASON, None),
    ("nonlocal rebinds the receiver",
     {"m.py": "class C(object):\n    def run(self): pass\n    def go(self):\n        def g():\n"
              "            nonlocal self\n            self = 1\n        g()\n        self.run()\n"},
     "m.py", "self.run", ct.LOCAL, None),
    ("a Python 2 backtick repr is an unreadable site", {"m.py": "print 'x'\ndef f():\n    x = `len([])`\n"},
     "m.py", "`len([])`", ct.DYNAMIC, None),
    ("Python 2 implicit relative import",
     {"p/__init__.py": "", "p/a.py": "def f(): pass\n", "p/m.py": "print 'x'\nfrom a import f\nf()\n"},
     "p/m.py", "f", INTERNAL, "p.a.f"),
]


class TestResolution(unittest.TestCase):
    def test_cases(self):
        for case, files, path, written, category, target in CASES:
            with self.subTest(case):
                found = call(files, path, written)
                self.assertEqual((found.category, found.target), (category, target),
                                 f"{case}: {found.reason}")

    def test_caller_is_the_innermost_declaration(self):
        files = {"m.py": """\
            def deco(f): return f
            def d(): pass
            @deco
            def f(x=d()):
                def inner():
                    len([])
            class C(object):
                def m(self):
                    len([])
            len([])
            """}
        callers = {(c.written, c.caller) for c in calls_of(files, "m.py")}
        self.assertEqual(callers, {
            ("deco", "m"),        # a decorator runs in the enclosing scope
            ("d", "m"),           # so does a default value
            ("len", "m.f"),       # a closure's call belongs to its declaration
            ("len", "m.c.m"),
            ("len", "m"),
        })

    def test_a_lambda_bound_in_a_class_calls_on_behalf_of_its_name(self):
        files = {"m.py": "def g(): pass\nclass P(object):\n    __len__ = lambda x: g()\n    h = g()\n"}
        callers = {(c.written, c.caller) for c in calls_of(files, "m.py")}
        self.assertEqual(callers, {("g", "m.p.__len__"), ("g", "m.p")})

    def test_binds_names_what_the_call_runs_on_behalf_of(self):
        files = {"m.py": """\
            def deco(f): return f
            def make(): pass
            @deco
            def f(): pass
            route = make()
            a = b = make()
            class C(object):
                attr = make()
                @deco
                def m(self):
                    make()
            """}
        binds = {(c.written, c.caller, c.binds) for c in calls_of(files, "m.py")}
        self.assertEqual(binds, {
            ("deco", "m", ("m.f",)),
            ("make", "m", ("m.route",)),
            ("make", "m", ("m.a", "m.b")),
            ("make", "m.c", ("m.c.attr",)),
            ("deco", "m.c", ("m.c.m",)),
            ("make", "m.c.m", ()),
        })

    def test_properties_are_recorded(self):
        files = {"m.py": """\
            class C(object):
                @property
                def a(self): pass
                @a.setter
                def a(self, v): pass
                @cached_property
                def b(self): pass
                @staticmethod
                def c(): pass
            """}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "m.py").write_text(textwrap.dedent(files["m.py"]))
            found = CallResolver(Inventory(files, Path(tmp))).declarations.properties
        self.assertEqual(found, {"m.c.a", "m.c.b"})

    def test_every_caller_is_a_node(self):
        files = {"m.py": "class C(object):\n    class D(object):\n        def m(self):\n            len([])\n"
                         "    if True:\n        def n(self):\n            len([])\n"}
        source = textwrap.dedent(files["m.py"])
        declared = {f"m.{n}".lower() for _, n in symbols_in_tree(source, PYTHON)} | {"m"}
        self.assertLessEqual({c.caller for c in calls_of(files, "m.py")}, declared)


@unittest.skipUnless(CELERY.is_dir(), WHY_SKIPPED)
class TestCelery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = sorted(p for p in CELERY.rglob("*.py") if ".git" not in p.parts)
        if len(cls.files) != CELERY_FILES:
            raise unittest.SkipTest(f"{CELERY} holds {len(cls.files)} Python files, not {CELERY_FILES}")

    def test_capture_agrees_with_ast(self):
        total, disagreeing = 0, []
        for path in self.files:
            source = path.read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            expected = {(n.lineno, n.col_offset) for n in ast.walk(tree) if isinstance(n, ast.Call)}
            found, stack = set(), [parse(source, PYTHON).root_node]
            while stack:
                node = stack.pop()
                if node.type == "call" and (fn := node.child_by_field_name("function")) is not None:
                    found.add((fn.start_point[0] + 1, fn.start_point[1]))
                stack.extend(node.named_children)
            total += len(expected)
            if found != expected:
                disagreeing.append((str(path.relative_to(CELERY)), sorted(expected ^ found)[:3]))
        self.assertEqual(disagreeing, [])
        self.assertEqual(total, CELERY_AST_SITES)

    def test_resolution_rate_is_pinned(self):
        scan = scan_calls(CELERY)
        internal = {(c.caller, c.target) for c in scan.calls if c.category == INTERNAL}
        self.assertEqual(
            (len(scan.calls), scan.by_category(), len(internal)),
            (CELERY_SITES, CELERY_CATEGORIES, CELERY_INTERNAL_PAIRS),
        )

    def test_every_internal_target_is_declared(self):
        scan = scan_calls(CELERY)
        resolver = CallResolver(Inventory([str(p.relative_to(CELERY)) for p in self.files], CELERY))
        declared = resolver.declarations.kinds
        stray = {c.target for c in scan.calls if c.category == INTERNAL and c.target not in declared}
        self.assertEqual(stray, set())


if __name__ == "__main__":
    unittest.main()
