"""Ruby edges: constructed cases per rule, and a regression pin.

There is no second Ruby reader, so nothing here is an oracle. The cases are
Ruby's load and constant-lookup rules written as inputs with known answers;
the jekyll totals are what the resolver produced on v4.4.1, guarded by a file
count. Two structural assertions keep the pin from meaning "it matched
itself": every source joins a node key, and every internal superclass is a
class node. Two files' edges were checked against the source by hand.
"""

import os
import tempfile
import textwrap
import unittest
import warnings
from pathlib import Path

from provenance.corpus import dotted_key
from provenance.dialects import RUBY
from provenance.edges import (
    EXTERNAL,
    IMPORTS,
    INHERITS,
    INTERNAL,
    UNRESOLVED,
    Inventory,
    scan_checkout,
)
from provenance.edges.ruby import RubyResolver
from provenance.symbols import package_for
from provenance.treesitter import CLASS, symbols_in_tree

CANDIDATES = Path(
    os.environ.get("AUGUR_CANDIDATES", Path.home() / "Developer" / "augur-test" / "candidates")
)
JEKYLL = CANDIDATES / "jekyll"
JEKYLL_FILES = 160

TREE = {
    "lib/gem.rb": "module Gem2; end\n",
    "lib/gem/site.rb": "module Gem2\n  class Site; end\n  module Errors\n    Fatal = Class.new(StandardError)\n  end\nend\n",
    "lib/gem/tags.rb": "module Gem2\n  module Tags\n    class Base; end\n  end\nend\n",
    "lib/gem/hash.rb": "class Hash\n  def deep; end\nend\n",
    "other/lib/gem/site.rb": "",
    "other/other.gemspec": "",
    "spec/fixtures/lib/only.rb": "",
    "ext/nat/nat.c": "",
    "docs/liquid.md": "",
    "bench/doc.rb": "class Document; end\n",
    "test/helper.rb": "",
    "lib/top.rb": "class Base; end\nmodule Errors; class Top; end; end\n",
}
FILE = "lib/gem/m.rb"


def edges_of(source, kind=None, path=FILE, tree=TREE):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        files = {**tree, path: textwrap.dedent(source)}
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        found, used_ast = RubyResolver(Inventory(files, root)).edges(path, files[path])
    assert not used_ast
    return {(e.kind, e.written, e.target, e.target_class) for e in found if kind is None or e.kind == kind}


class TestRubyLoads(unittest.TestCase):
    CASES = [
        ("require under one lib/", 'require "gem/tags"',
         {(IMPORTS, "gem/tags", "lib.gem.tags", INTERNAL)}),
        ("autoload names its path second", 'autoload :Tags, "gem/tags"',
         {(IMPORTS, "gem/tags", "lib.gem.tags", INTERNAL)}),
        ("a path two lib/ directories hold", 'require "gem/site"',
         {(IMPORTS, "gem/site", None, UNRESOLVED)}),
        ("in the checkout but under no lib/: the load path is not in the source", 'require "helper"',
         {(IMPORTS, "helper", None, UNRESOLVED)}),
        ("a gem or the standard library", 'require "liquid"\nrequire "set"',
         {(IMPORTS, "liquid", "liquid", EXTERNAL), (IMPORTS, "set", "set", EXTERNAL)}),
        ("require_relative names a file beside this one", 'require_relative "tags"',
         {(IMPORTS, "tags", "lib.gem.tags", INTERNAL)}),
        ("require_relative naming nothing", 'require_relative "../gone"',
         {(IMPORTS, "../gone", None, UNRESOLVED)}),
        ("a computed path", 'require File.expand_path("../tags", __FILE__)',
         {(IMPORTS, 'File.expand_path("../tags",__FILE__)', None, UNRESOLVED)}),
        ("an interpolated path", 'require "gem/#{name}"',
         {(IMPORTS, '"gem/#{name}"', None, UNRESOLVED)}),
        ("a call with a receiver is not a loader", 'Bundler.require "gem/tags"', set()),
        ("a lib/ beside no gemspec is not a load path", 'require "only"',
         {(IMPORTS, "only", None, UNRESOLVED)}),
        ("a C extension the checkout builds", 'require "nat/nat"',
         {(IMPORTS, "nat/nat", None, UNRESOLVED)}),
        ("a document of the same name is not loadable", 'require "liquid"',
         {(IMPORTS, "liquid", "liquid", EXTERNAL)}),
        ("./ resolves against the current directory", 'require "./lib/gem/tags"',
         {(IMPORTS, "./lib/gem/tags", None, UNRESOLVED)}),
        ("load adds no .rb", 'load "gem/tags"', {(IMPORTS, "gem/tags", None, UNRESOLVED)}),
        ("autoload with a string constant name reads the path second", 'autoload "Tags", "gem/tags"',
         {(IMPORTS, "gem/tags", "lib.gem.tags", INTERNAL)}),
    ]

    def test_cases(self):
        for name, source, expected in self.CASES:
            with self.subTest(name):
                self.assertEqual(edges_of(source, IMPORTS), expected)


class TestRubySuperclasses(unittest.TestCase):
    CASES = [
        ("the innermost enclosing module first",
         "module Gem2\n  module Tags\n    class Link < Base; end\n  end\nend",
         {(INHERITS, "Base", "gem2.tags.base", INTERNAL)}),
        ("a bare name falling through from a nested scope is unresolved",
         "module Gem2\n  class Link < Base; end\nend",
         {(INHERITS, "Base", None, UNRESOLVED)}),
        ("a top-level class looks only at the top level",
         "class Link < Base; end",
         {(INHERITS, "Base", "base", INTERNAL)}),
        ("a class declared only outside lib/ does not count for lib code",
         "class J < Document; end",
         {(INHERITS, "Document", None, UNRESOLVED)}),
        ("Object:: means the top level", "class J < Object::Base; end",
         {(INHERITS, "Object::Base", "base", INTERNAL)}),
        ("a compact definition declares its prefixes as namespaces",
         "module Gem2\n  class Q < Tags::Base; end\nend",
         {(INHERITS, "Tags::Base", "gem2.tags.base", INTERNAL)}),
        ("an absolute path skips the nesting",
         "module Gem2\n  module Tags\n    class Link < ::Base; end\n  end\nend",
         {(INHERITS, "::Base", "base", INTERNAL)}),
        ("a nested constant path, found lexically",
         "module Gem2\n  class Link < Tags::Base; end\nend",
         {(INHERITS, "Tags::Base", "gem2.tags.base", INTERNAL)}),
        ("a nearer namespace shadows the top level, and lacks the member",
         "module Gem2\n  class E < Errors::Top; end\nend",
         {(INHERITS, "Errors::Top", None, UNRESOLVED)}),
        ("an assigned constant's value is not syntax",
         "module Gem2\n  class E < Errors::Fatal; end\nend",
         {(INHERITS, "Errors::Fatal", None, UNRESOLVED)}),
        ("a core class the checkout reopens is still core",
         "module Gem2\n  class MyHash < Hash; end\nend",
         {(INHERITS, "Hash", "hash", EXTERNAL)}),
        ("a constant the checkout never declares", "class PostUrl < Liquid::Tag; end",
         {(INHERITS, "Liquid::Tag", "liquid.tag", EXTERNAL)}),
        ("an expression", "class S < Struct.new(:a); end",
         {(INHERITS, "Struct.new(:a)", None, UNRESOLVED)}),
        ("include is not inheritance", "class C\n  include Comparable\nend", set()),
        ("Class.new's argument is looked up where a class statement's would be",
         "module Gem2\n  module Tags\n    Link = Class.new(Base) do\n      def x; end\n    end\n  end\nend",
         {(INHERITS, "Base", "gem2.tags.base", INTERNAL)}),
        ("Class.new of a core class", "module Gem2\n  Oops = Class.new(::StandardError)\nend",
         {(INHERITS, "::StandardError", "standarderror", EXTERNAL)}),
        ("Class.new of an assigned constant", "module Gem2\n  E = Class.new(Errors::Fatal)\nend",
         {(INHERITS, "Errors::Fatal", None, UNRESOLVED)}),
        ("Class.new with no argument is Object's subclass", "module Gem2\n  Drop = Class.new\nend", set()),
    ]

    #: ``Errors`` is declared at the top level and as ``Gem2::Errors``.
    ANCESTRY = [
        ("a superclass's namespaces come before the top level",
         "class C < Gem2::Site\n  class E < Errors::Top; end\nend", None, UNRESOLVED),
        ("so do an included module's",
         "class C\n  include Gem2\n  class E < Errors::Top; end\nend", None, UNRESOLVED),
        ("extend changes the singleton class, which lookup does not consult",
         "class C\n  extend Gem2\n  class E < Errors::Top; end\nend", "errors.top", INTERNAL),
        ("a core superclass adds nothing the checkout declares",
         "class C < StandardError\n  class E < Errors::Top; end\nend", "errors.top", INTERNAL),
        ("only the innermost scope's ancestors are searched",
         "class C < Gem2::Site\n  module M\n    class E < Errors::Top; end\n  end\nend", "errors.top", INTERNAL),
        ("a head nothing else declares still names the top level",
         "class C < Gem2::Site\n  class E < Liquid::Tag; end\nend", "liquid.tag", EXTERNAL),
    ]

    def test_ancestry(self):
        for name, source, target, target_class in self.ANCESTRY:
            with self.subTest(name):
                written = source.rpartition("< ")[2].partition(";")[0]
                inner = {e for e in edges_of(source, INHERITS) if e[1] == written}
                self.assertEqual(inner, {(INHERITS, written, target, target_class)})

    def test_cases(self):
        for name, source, expected in self.CASES:
            with self.subTest(name):
                self.assertEqual(edges_of(source, INHERITS), expected)


class TestTopLevelInclude(unittest.TestCase):
    """``include Jekyll`` at the top of ``test/helper.rb`` and what it reaches."""

    TREE = {
        "test/helper.rb": "include Gem2\nclass UnitTest; end\n",
        "lib/gem2.rb": "module Gem2\n  Generator = Class.new\nend\n",
    }

    def test_a_name_the_top_level_declares_wins_over_the_include(self):
        self.assertEqual(edges_of("class T < UnitTest; end", INHERITS, "test/t.rb", self.TREE),
                         {(INHERITS, "UnitTest", "unittest", INTERNAL)})

    def test_any_other_name_may_come_from_the_included_module(self):
        self.assertEqual(edges_of("class T < Liquid::Tag; end", INHERITS, "test/t.rb", self.TREE),
                         {(INHERITS, "Liquid::Tag", None, UNRESOLVED)})

    def test_the_include_does_not_reach_another_directory(self):
        self.assertEqual(edges_of("class T < Liquid::Tag; end", INHERITS, "bench/t.rb", self.TREE),
                         {(INHERITS, "Liquid::Tag", "liquid.tag", EXTERNAL)})

    def test_a_constant_bound_by_or_assign_is_never_settled(self):
        tree = {**self.TREE, "lib/x.rb": "Foo ||= Class.new\n"}
        self.assertEqual(edges_of("class T < Foo; end", INHERITS, "bench/t.rb", tree),
                         {(INHERITS, "Foo", None, UNRESOLVED)})


@unittest.skipUnless(JEKYLL.is_dir(), f"no jekyll checkout at {JEKYLL}")
class TestJekyll(unittest.TestCase):
    """A regression pin on jekyll v4.4.1, not a measurement of truth."""

    BY_CLASS = {
        (IMPORTS, INTERNAL): 71, (IMPORTS, EXTERNAL): 77, (IMPORTS, UNRESOLVED): 56,
        (INHERITS, INTERNAL): 78, (INHERITS, EXTERNAL): 14, (INHERITS, UNRESOLVED): 25,
    }

    #: ``lib/jekyll.rb``'s edges that are not internal, checked by hand: the
    #: standard library and gems, and one computed path. Its other 51 are the
    #: ``autoload`` and ``require`` lines under ``lib/jekyll/``.
    JEKYLL_RB_EXTERNAL = {
        "English", "addressable/uri", "colorator", "csv", "fileutils", "forwardable", "i18n",
        "jekyll-sass-converter", "json", "kramdown", "liquid", "logger", "pathname", "pathutil",
        "rubygems", "safe_yaml/load", "set", "time",
    }

    @classmethod
    def setUpClass(cls):
        if len(list(JEKYLL.rglob("*.rb"))) != JEKYLL_FILES:
            raise unittest.SkipTest(f"jekyll is not the {JEKYLL_FILES}-file v4.4.1 release")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            cls.scan = scan_checkout(JEKYLL)

    def test_totals(self):
        self.assertEqual(self.scan.by_class("ruby"), self.BY_CLASS)

    def test_sources_join_and_internal_superclasses_are_class_nodes(self):
        modules, classes, bindings = set(), set(), set()
        for path in sorted(str(p.relative_to(JEKYLL)) for p in JEKYLL.rglob("*.rb")):
            source = (JEKYLL / path).read_text(encoding="utf-8", errors="replace")
            modules.add(dotted_key(package_for(path, source, RUBY)))
            for kind, name in symbols_in_tree(source, RUBY):
                (classes if kind == CLASS else bindings).add(dotted_key(name))
        # ``Generator = Class.new(Plugin)`` makes a class the inventory holds as a binding.
        unjoined = [e for e in self.scan.edges
                    if e.source not in (modules if e.kind == IMPORTS else classes | bindings)]
        self.assertEqual(unjoined, [])
        not_nodes = [
            e for e in self.scan.edges
            if e.kind == INHERITS and e.target_class == INTERNAL and e.target not in classes
        ]
        self.assertEqual(not_nodes, [])

    def test_jekyll_rb_by_hand(self):
        edges = [e for e in self.scan.edges if e.path == "lib/jekyll.rb"]
        self.assertEqual({e.written for e in edges if e.target_class == EXTERNAL}, self.JEKYLL_RB_EXTERNAL)
        self.assertEqual([e.written for e in edges if e.target_class == UNRESOLVED], ["f"])
        self.assertEqual(sum(e.target_class == INTERNAL for e in edges), 51)

    def test_post_url_by_hand(self):
        found = {
            (e.kind, e.written, e.target, e.target_class)
            for e in self.scan.edges if e.path == "lib/jekyll/tags/post_url.rb"
        }
        self.assertEqual(found, {(INHERITS, "Liquid::Tag", "liquid.tag", EXTERNAL)})
