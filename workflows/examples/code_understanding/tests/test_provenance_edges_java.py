"""Java edges: one constructed case per scoping rule, and a regression pin.

There is no second Java reader, so nothing here is an oracle. The cases are
JLS 6.4.1's precedence written as inputs with known answers; the commons-lang
totals are what the resolver produced on a named checkout, guarded by a file
count so a different release skips rather than failing with a number nobody
can interpret. Two structural assertions keep the pin from meaning "it matched
itself": every source joins a node key, and every base says extends or
implements. One file's edges were checked against the source by hand.
"""

import os
import tempfile
import textwrap
import unittest
import warnings
from pathlib import Path

from provenance.corpus import dotted_key
from provenance.declarations import namespace_for
from provenance.dialects import JAVA
from provenance.edges import (
    EXTERNAL,
    IMPORTS,
    INHERITS,
    INTERNAL,
    UNRESOLVED,
    Inventory,
    scan_checkout,
)
from provenance.edges.java import JavaResolver
from provenance.symbols import package_for
from provenance.treesitter import CLASS, symbols_in_tree

CANDIDATES = Path(
    os.environ.get("AUGUR_CANDIDATES", Path.home() / "Developer" / "augur-test" / "candidates")
)
COMMONS_LANG = CANDIDATES / "commons-lang"
COMMONS_LANG_FILES = 629
LANG3 = "org.apache.commons.lang3."

#: A small checkout every case resolves against.
TREE = {
    "src/main/java/org/ex/Base.java": "package org.ex; public class Base {}",
    "src/main/java/org/ex/Shape.java": "package org.ex; public interface Shape {}",
    "src/main/java/org/ex/Outer.java": "package org.ex; public class Outer { public static class Inner {} }",
    "src/main/java/org/ex/util/Helper.java": "package org.ex.util; public class Helper {}",
    "src/main/java/org/ex/Util.java": "package org.ex; public class Util {}",
    "src/main/java/org/ex/other/Base.java": "package org.ex.other; public class Base {}",
    "src/main/java/org/ex/a/Thing.java": "package org.ex.a; public class Thing {}",
    "src/main/java/org/ex/b/Thing.java": "package org.ex.b; public class Thing {}",
    "src/main/java/org/ex/Twin.java": "package org.ex; public class Twin {}",
    "src/test/java/org/ex/Twin.java": "package org.ex; public class Twin {}",
    "src/test/java/org/ex/List.java": "package org.ex; public class List {}",
    "src/main/java/org/ex/Foo.java": "package org.ex; public class Foo {} class Helper {}",
    "src/main/java/org/ex/Inner.java": "package org.ex; public class Inner {}",
    "src/main/java/org/ex/Sup.java": (
        "package org.ex; public class Sup { public static class Inner {} "
        "public static class Error {} public static class Deep {} }"
    ),
    "src/main/java/org/ex/Mid.java": "package org.ex; public class Mid extends Sup {}",
    "src/main/java/org/ex/Hide.java": (
        "package org.ex; public class Hide { private static class Inner {} static class Shape {} }"
    ),
    "src/integrationTest/java/org/ex/It.java": "package org.ex; class It {}",
}
FILE = "src/main/java/org/ex/M.java"


def edges_of(source, kind=None, path=FILE):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        files = {**TREE, path: textwrap.dedent(source)}
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        found, used_ast = JavaResolver(Inventory(files, root)).edges(path, files[path])
    assert not used_ast
    return {
        (e.kind, e.written, e.target, e.target_class, e.detail)
        for e in found if kind is None or e.kind == kind
    }


class TestJavaImports(unittest.TestCase):
    CASES = [
        ("a type in the checkout", "import org.ex.util.Helper;",
         {(IMPORTS, "org.ex.util.Helper", "org.ex.util.helper", INTERNAL, None)}),
        ("a member type, keyed as its symbol node is", "import org.ex.Outer.Inner;",
         {(IMPORTS, "org.ex.Outer.Inner", "org.ex.outer.inner", INTERNAL, None)}),
        ("case-sensitive: package org.ex.util is not class org.ex.Util",
         "import org.ex.util.Missing;",
         {(IMPORTS, "org.ex.util.Missing", None, UNRESOLVED, None)}),
        ("an internal package on demand targets the package, never a class key",
         "import org.ex.util.*;",
         {(IMPORTS, "org.ex.util.*", "org.ex.util.*", INTERNAL, None)}),
        ("an external package on demand", "import java.util.*;",
         {(IMPORTS, "java.util.*", "java.util.*", EXTERNAL, None)}),
        ("a static member: one edge to the type",
         "import static org.ex.Util.a;\nimport static org.ex.Util.b;",
         {(IMPORTS, "org.ex.Util", "org.ex.util", INTERNAL, None)}),
        ("a static star names the type, not its package", "import static org.ex.Util.*;",
         {(IMPORTS, "org.ex.Util", "org.ex.util", INTERNAL, None)}),
        ("outside the checkout", "import java.util.List;",
         {(IMPORTS, "java.util.List", "java.util.list", EXTERNAL, None)}),
        ("a name two files declare stays unresolved: its key names both nodes",
         "import org.ex.Twin;",
         {(IMPORTS, "org.ex.Twin", None, UNRESOLVED, None)}),
    ]

    def test_cases(self):
        for name, imports, expected in self.CASES:
            with self.subTest(name):
                self.assertEqual(edges_of(f"package org.ex;\n{imports}\nclass M {{}}", IMPORTS), expected)

    def test_a_source_set_sees_main(self):
        found = edges_of("package org.ex;\nclass T extends Base {}", INHERITS,
                         path="src/integrationTest/java/org/ex/T.java")
        self.assertEqual(found, {(INHERITS, "Base", "org.ex.base", INTERNAL, "extends")})

    def test_a_package_private_member_is_not_inherited_across_packages(self):
        source = ("package org.ex.q;\nimport org.ex.Hide;\nimport org.ex.Shape;\n"
                  "class M extends Hide { static class N extends Shape {} }")
        found = edges_of(source, INHERITS, path="src/main/java/org/ex/q/M.java")
        self.assertIn((INHERITS, "Shape", "org.ex.shape", INTERNAL, "extends"), found)

    def test_main_does_not_see_a_source_set(self):
        self.assertEqual(edges_of("package org.ex;\nclass M extends It {}", INHERITS),
                         {(INHERITS, "It", None, UNRESOLVED, "extends")})

    def test_a_package_suffix_is_matched_on_a_path_boundary(self):
        found = edges_of("package org.ex;\nclass M extends Base {}", INHERITS,
                         path="lib/borg/ex/M.java")
        self.assertEqual(found, {(INHERITS, "Base", None, UNRESOLVED, "extends")})


class TestJavaBases(unittest.TestCase):
    CASES = [
        ("the same package, extends and implements kept apart",
         "class M extends Base implements Shape {}",
         {(INHERITS, "Base", "org.ex.base", INTERNAL, "extends"),
          (INHERITS, "Shape", "org.ex.shape", INTERNAL, "implements")}),
        ("type arguments are written and do not resolve", "class M extends Base<String> {}",
         {(INHERITS, "Base<String>", "org.ex.base", INTERNAL, "extends")}),
        ("a single-type import shadows the same package",
         "import org.ex.other.Base;\nclass M extends Base {}",
         {(INHERITS, "Base", "org.ex.other.base", INTERNAL, "extends")}),
        ("an enclosing scope shadows a single-type import",
         "import org.ex.other.Base;\nclass M { static class Base {} static class N extends Base {} }",
         {(INHERITS, "Base", "org.ex.m.base", INTERNAL, "extends")}),
        ("a member type through its declaring class", "class M extends Outer.Inner {}",
         {(INHERITS, "Outer.Inner", "org.ex.outer.inner", INTERNAL, "extends")}),
        ("an internal package on demand", "import org.ex.a.*;\nclass M extends Thing {}",
         {(INHERITS, "Thing", "org.ex.a.thing", INTERNAL, "extends")}),
        ("a secondary top-level type in the same package beats on-demand",
         "import org.ex.util.*;\nclass M extends Helper {}",
         {(INHERITS, "Helper", "org.ex.helper", INTERNAL, "extends")}),
        ("type arguments on an outer type do not cut the name",
         "class M extends Outer<String>.Inner {}",
         {(INHERITS, "Outer<String>.Inner", "org.ex.outer.inner", INTERNAL, "extends")}),
        ("a member part the checkout does not declare",
         "class M extends Outer.Missing {}",
         {(INHERITS, "Outer.Missing", None, UNRESOLVED, "extends")}),
        ("a dotted base whose head an on-demand import may supply is not fully qualified",
         "import java.util.*;\nclass M implements Map.Entry<String,String> {}",
         {(INHERITS, "Map.Entry<String,String>", None, UNRESOLVED, "implements")}),
        ("a type only the test tree declares is not visible from main",
         "import java.util.*;\nclass M extends List {}",
         {(INHERITS, "List", None, UNRESOLVED, "extends")}),
        ("a single-static import of a member type", "import static org.ex.Sup.Inner;\nclass M extends Inner {}",
         {(INHERITS, "Inner", "org.ex.sup.inner", INTERNAL, "extends")}),
        ("a static on-demand import of a type's members", "import static org.ex.Sup.*;\nclass M extends Deep {}",
         {(INHERITS, "Deep", "org.ex.sup.deep", INTERNAL, "extends")}),
        ("an on-demand import of a type's members", "import org.ex.Sup.*;\nclass M extends Deep {}",
         {(INHERITS, "Deep", "org.ex.sup.deep", INTERNAL, "extends")}),
        ("an inherited member type shadows the same package",
         "class M extends Sup { static class N extends Inner {} }",
         {(INHERITS, "Sup", "org.ex.sup", INTERNAL, "extends"),
          (INHERITS, "Inner", "org.ex.sup.inner", INTERNAL, "extends")}),
        ("an inherited member type shadows java.lang, two supertypes up",
         "class M extends Mid { static class N extends Error {} }",
         {(INHERITS, "Mid", "org.ex.mid", INTERNAL, "extends"),
          (INHERITS, "Error", "org.ex.sup.error", INTERNAL, "extends")}),
        ("a supertype outside the checkout and the JDK may supply any member type",
         "class M extends javax.servlet.GenericServlet { static class N extends Base {} }",
         {(INHERITS, "javax.servlet.GenericServlet", "javax.servlet.genericservlet", EXTERNAL, "extends"),
          (INHERITS, "Base", None, UNRESOLVED, "extends")}),
        ("a JDK supertype that inherits member types may supply one",
         "class M extends java.util.GregorianCalendar { static class N extends Base {} }",
         {(INHERITS, "java.util.GregorianCalendar", "java.util.gregoriancalendar", EXTERNAL, "extends"),
          (INHERITS, "Base", None, UNRESOLVED, "extends")}),
        ("a member-free JDK supertype outside java.lang hides nothing",
         "class M implements java.io.Serializable { static class N extends Base {} }",
         {(INHERITS, "java.io.Serializable", "java.io.serializable", EXTERNAL, "implements"),
          (INHERITS, "Base", "org.ex.base", INTERNAL, "extends")}),
        ("a private member type is not inherited",
         "class M extends Hide { static class N extends Inner {} }",
         {(INHERITS, "Hide", "org.ex.hide", INTERNAL, "extends"),
          (INHERITS, "Inner", "org.ex.inner", INTERNAL, "extends")}),
        ("a dotted head nothing binds and no package starts with is not fully qualified",
         "class M extends FooGrpc.FooImplBase {}",
         {(INHERITS, "FooGrpc.FooImplBase", None, UNRESOLVED, "extends")}),
        ("an annotation inside type arguments does not count",
         "class M implements Comparable<@Deprecated M> {}",
         {(INHERITS, "Comparable<@DeprecatedM>", "java.lang.comparable", EXTERNAL, "implements")}),
        ("a member-free java.lang supertype hides nothing",
         "class M extends Exception { static class N extends Base {} }",
         {(INHERITS, "Exception", "java.lang.exception", EXTERNAL, "extends"),
          (INHERITS, "Base", "org.ex.base", INTERNAL, "extends")}),
        ("an annotation inside a qualified type",
         "class M implements java.util.@Deprecated List<String> {}",
         {(INHERITS, "java.util.@DeprecatedList<String>", None, UNRESOLVED, "implements")}),
        ("two on-demand imports supplying one name",
         "import org.ex.a.*;\nimport org.ex.b.*;\nclass M extends Thing {}",
         {(INHERITS, "Thing", None, UNRESOLVED, "extends")}),
        ("java.lang", "class M extends Exception implements Runnable {}",
         {(INHERITS, "Exception", "java.lang.exception", EXTERNAL, "extends"),
          (INHERITS, "Runnable", "java.lang.runnable", EXTERNAL, "implements")}),
        ("java.lang is settled even beside an external on-demand import",
         "import java.util.*;\nclass M extends Exception {}",
         {(INHERITS, "Exception", "java.lang.exception", EXTERNAL, "extends")}),
        ("but not a java.lang name added since Java 8",
         "import com.google.inject.*;\nclass M implements Module {}",
         {(INHERITS, "Module", None, UNRESOLVED, "implements")}),
        ("which is settled where nothing on demand could shadow it",
         "class M implements Module {}",
         {(INHERITS, "Module", "java.lang.module", EXTERNAL, "implements")}),
        ("an external on-demand import may supply anything else",
         "import java.util.*;\nclass M extends AbstractList {}",
         {(INHERITS, "AbstractList", None, UNRESOLVED, "extends")}),
        ("fully qualified", "class M implements java.io.Serializable {}",
         {(INHERITS, "java.io.Serializable", "java.io.serializable", EXTERNAL, "implements")}),
        ("an interface's extends, and an enum's implements",
         "interface M extends Shape {}\nenum E implements Shape { X }",
         {(INHERITS, "Shape", "org.ex.shape", INTERNAL, "extends"),
          (INHERITS, "Shape", "org.ex.shape", INTERNAL, "implements")}),
        ("bound nowhere, behind supertypes the checkout enumerates",
         "class M extends Base { static class N extends Hidden {} }",
         {(INHERITS, "Base", "org.ex.base", INTERNAL, "extends"),
          (INHERITS, "Hidden", None, UNRESOLVED, "extends")}),
    ]

    def test_cases(self):
        for name, body, expected in self.CASES:
            with self.subTest(name):
                self.assertEqual(edges_of(f"package org.ex;\n{body}", INHERITS), expected)


@unittest.skipUnless(COMMONS_LANG.is_dir(), f"no commons-lang checkout at {COMMONS_LANG}")
class TestCommonsLang(unittest.TestCase):
    """A regression pin on rel/commons-lang-3.21.0, not a measurement of truth."""

    BY_CLASS = {
        (IMPORTS, INTERNAL): 732, (IMPORTS, EXTERNAL): 2837, (IMPORTS, UNRESOLVED): 0,
        (INHERITS, INTERNAL): 560, (INHERITS, EXTERNAL): 148, (INHERITS, UNRESOLVED): 0,
    }

    #: ``LockingVisitors.java``, checked against the source by hand. The three
    #: ``Builder`` classes extend ``LVBuilder``, which their enclosing class
    #: inherits from ``LockVisitor``: reached by enumerating that supertype.
    LOCKING_VISITORS = {
        *((IMPORTS, f"java.util.{n}", f"java.util.{n.lower()}", EXTERNAL) for n in (
            "Objects", "concurrent.locks.Lock", "concurrent.locks.ReadWriteLock",
            "concurrent.locks.ReentrantLock", "concurrent.locks.ReentrantReadWriteLock",
            "concurrent.locks.StampedLock", "function.Supplier",
        )),
        *((IMPORTS, f"{LANG3}{n}", dotted_key(f"{LANG3}{n}"), INTERNAL) for n in (
            "builder.AbstractSupplier", "function.Failable", "function.FailableConsumer",
            "function.FailableFunction", "function.Suppliers",
        )),
        (INHERITS, "AbstractSupplier<LockVisitor<O,L>,B,RuntimeException>",
         f"{LANG3}builder.abstractsupplier", INTERNAL),
        *((INHERITS, f"LockVisitor<O,{lock}>",
           f"{LANG3}concurrent.locks.lockingvisitors.lockvisitor", INTERNAL)
          for lock in ("ReadWriteLock", "ReentrantLock", "StampedLock")),
        *((INHERITS, f"LVBuilder<O,{lock},Builder<O>>",
           f"{LANG3}concurrent.locks.lockingvisitors.lockvisitor.lvbuilder", INTERNAL)
          for lock in ("ReadWriteLock", "ReentrantLock", "StampedLock")),
    }

    @classmethod
    def setUpClass(cls):
        if len(list(COMMONS_LANG.rglob("*.java"))) != COMMONS_LANG_FILES:
            raise unittest.SkipTest(f"commons-lang is not the {COMMONS_LANG_FILES}-file 3.21.0 release")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            cls.scan = scan_checkout(COMMONS_LANG)

    def test_totals(self):
        self.assertEqual(self.scan.by_class("java"), self.BY_CLASS)

    def test_files_by_evidence(self):
        self.assertEqual(
            {k: v for k, v in self.scan.files.items() if k[0] == "java"},
            {("java", "tree-sitter"): 628, ("java", "tree-sitter-recovered"): 1},
        )

    def test_every_base_says_extends_or_implements(self):
        details = {e.detail for e in self.scan.edges if e.kind == INHERITS}
        self.assertEqual(details, {"extends", "implements"})

    def test_every_source_joins_a_node_key(self):
        modules, classes = set(), set()
        for path in sorted({e.path for e in self.scan.edges}):
            source = (COMMONS_LANG / path).read_text(encoding="utf-8", errors="replace")
            modules.add(dotted_key(package_for(path, source, JAVA)))
            namespace = namespace_for(path, source, JAVA)
            classes |= {
                dotted_key(f"{namespace}.{name}" if namespace else name)
                for kind, name in symbols_in_tree(source, JAVA) if kind == CLASS
            }
        unjoined = [
            e for e in self.scan.edges
            if e.source not in (modules if e.kind == IMPORTS else classes)
        ]
        self.assertEqual(unjoined, [])

    def test_locking_visitors_by_hand(self):
        path = "src/main/java/org/apache/commons/lang3/concurrent/locks/LockingVisitors.java"
        found = {
            (e.kind, e.written, e.target, e.target_class)
            for e in self.scan.edges if e.path == path
        }
        self.assertEqual(found, self.LOCKING_VISITORS)
