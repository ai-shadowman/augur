"""The edge verdict: one constructed case per reason, and a pin on the celery arms.

The constructed checkout is small enough to read in full, so every expected
verdict below can be checked by eye against it. The pin needs the measured
runs under ``augur-test/runs`` and skips without them.
"""

from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from provenance import edge_verdict
from provenance.edge_evidence import EdgeEvidence

SANDBOX = Path(os.environ.get("AUGUR_SANDBOX", Path.home() / "Developer" / "augur-test"))
RUNS, CELERY = SANDBOX / "runs", SANDBOX / "candidates" / "celery"

#: ``pkg/a.py`` imports ``pkg.b`` and ``os``; ``pkg.e.Sub`` inherits
#: ``pkg.b.B``, which inherits ``pkg.c.C``;
#: ``pkg/c.py`` imports nothing internal; ``pkg/d.py`` has an import the
#: oracle cannot resolve (three dots from a two-deep package).
FILES = {
    "pkg/__init__.py": """
        from .c import C as Base

        VERSION = "1"
    """,
    # Imports the class from where it is declared; the model names the
    # package's re-export of it.
    "pkg/g.py": """
        from pkg.c import C
        from pkg import VERSION
    """,
    "pkg/h.py": """
        from os.path import join

        resource = None

        def f():
            from os import sep
            return sep
    """,
    "pkg/a.py": """
        import os
        from pkg import b

        class A:
            def run(self):
                pass

        class Helper:
            pass
    """,
    "pkg/b.py": """
        from pkg.c import C

        class B(C):
            pass
    """,
    "pkg/c.py": """
        class C:
            pass

        class E(C):
            pass
    """,
    "pkg/e.py": """
        from pkg.b import B

        class Sub(B):
            pass
    """,
    "pkg/r.rb": """
        require_relative "rmod/inner"
    """,
    "pkg/rmod.rb": """
        module Rmod
        end
    """,
    "pkg/rmod/inner.rb": """
        module Rmod
          class Inner
          end
        end
    """,
    # Ruby: StaticDrop's bare ``Drop`` is Jk::Drops::Drop by lexical
    # nesting, not the top-level Drop a benchmark script declares.
    "lib/jk/drops/drop.rb": """
        module Jk
          module Drops
            class Drop
            end
          end
        end
    """,
    "lib/jk/drops/static_drop.rb": """
        module Jk
          module Drops
            class StaticDrop < Drop
            end
          end
        end
    """,
    "bench/top.rb": """
        class Drop
        end
    """,
    "pkg/d.py": """
        from ...nowhere import thing

        class D:
            pass
    """,
}

#: ``(source, target, description, verdict, reason)``. The description names
#: the type and, through its subject, the direction.
CASES = [
    ("PKG/A.PY", "PKG/B.PY", "pkg/a.py IMPORTS pkg.b", "confirmed", "exact"),
    ("PKG.A.A.RUN", "PKG/B.PY", "pkg.a.A.run IMPORTS pkg.b", "confirmed", "lifted"),
    ("PKG/A.PY", "OS", "pkg/a.py IMPORTS os", "confirmed", "external"),
    ("PKG.B.B", "PKG.C.C", "pkg.b.B INHERITS pkg.c.C", "confirmed", "exact"),
    ("PKG.B.B", "PKG.C.C", "related through inheritance INHERITS", "confirmed", "exact, unoriented"),
    ("PKG.E.SUB", "PKG.C.C", "pkg.e.Sub INHERITS pkg.c.C", "confirmed", "transitive"),
    # Importing pkg.b runs pkg/__init__.py, so the package is imported too.
    ("PKG/B.PY", "PKG/__INIT__.PY", "pkg/b.py IMPORTS pkg", "confirmed", "ancestor"),
    ("PKG.E.SUB", "PKG/C.PY", "pkg.e.Sub INHERITS pkg.c", "confirmed", "container"),
    # Ruby's require loads one file, never the directory's namesake.
    ("PKG/R.RB", "PKG/RMOD.RB", "pkg/r.rb IMPORTS pkg/rmod.rb", "contradicted", "no-edge"),
    # A module-level binding, and a re-export chased to its declaring file.
    ("PKG/G.PY", "PKG.VERSION", "pkg/g.py IMPORTS pkg.VERSION", "confirmed", "lifted"),
    ("PKG/G.PY", "PKG.BASE", "pkg/g.py IMPORTS pkg.Base", "confirmed", "lifted"),
    # A bare name is looked up where the claimant uses it, then globally.
    ("JK::DROPS::STATICDROP", "DROP", "Jk::Drops::StaticDrop INHERITS Drop", "confirmed", "exact"),
    ("PKG/G.PY", "C", "pkg/g.py IMPORTS C", "confirmed", "lifted"),
    # An assignment's value is unknown, so a bare name never resolves to one.
    ("PKG/H.PY", "RESOURCE", "pkg/h.py IMPORTS resource", "out-of-reach", "ungrounded"),
    ("PKG/B.PY", "PKG/A.PY", "pkg/b.py IMPORTS pkg.a", "contradicted", "reversed"),
    ("PKG.A.A", "PKG/B.PY", "pkg.a.A INHERITS pkg.b", "contradicted", "wrong-kind"),
    ("PKG.A.A", "PKG.A.HELPER", "pkg.a.A IMPORTS pkg.a.Helper", "contradicted", "same-file"),
    ("PKG/A.PY", "PKG/C.PY", "pkg/a.py IMPORTS pkg.c", "contradicted", "no-edge"),
    ("PKG/D.PY", "PKG/C.PY", "pkg/d.py IMPORTS pkg.c", "unchecked", "no-edge"),
    # This evidence carries no call graph, as facts from before 2026-10-28 do.
    ("PKG/A.PY", "PKG/B.PY", "pkg/a.py CALLS pkg.b", "out-of-reach", "uncovered"),
    ("PKG/A.PY", "PKG/B.PY", "pkg/a.py talks to pkg.b", "out-of-reach", "type"),
    # Partial evidence: a plausible name inside a real package that nothing
    # declares and the claimant does not import is out of reach, never a
    # contradiction, since the oracle cannot tell invented from unseen.
    ("PKG/A.PY", "PKG.MISSING", "pkg/a.py IMPORTS pkg.missing", "out-of-reach", "ungrounded"),
    ("PKG/A.PY", "REQUESTS", "pkg/a.py IMPORTS requests", "out-of-reach", "ungrounded"),
]


def _checkout(root: Path) -> None:
    for path, body in FILES.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


#: Shapes an adversarial review constructed, each once confidently wrong.
REVIEW_FILES = {
    # Java: an on-demand import, and a single-type import shadowing a
    # same-package type of the same name (JLS 6.4.1).
    "src/com/x/util/Foo.java": "package com.x.util;\npublic class Foo {}\n",
    "src/com/x/Util.java": "package com.x;\npublic class Util {}\n",
    "src/com/y/B.java": "package com.y;\nimport com.x.util.*;\npublic class B { Foo f; }\n",
    "src/com/x/Base.java": "package com.x;\npublic class Base {}\n",
    "src/com/y/Base.java": "package com.y;\npublic class Base {}\n",
    "src/com/x/A.java": "package com.x;\nimport com.y.Base;\npublic class A extends Base {}\n",
    # TypeScript: a bare imported name, and a root file it folds onto.
    "web/lib/index.ts": "export class Widget {}\n",
    "web/a.ts": "import { Widget } from './lib';\nexport class A extends Widget {}\n",
    "widget.ts": "export const z = 1;\n",
    # Python: ``from . import sub`` makes the oracle state pkg -> pkg; and a
    # module importing ``beat`` while declaring ``class Beat``.
    "pkg/__init__.py": "from . import sub\n\nclass X:\n    pass\n\nclass Y:\n    pass\n",
    "pkg/sub.py": "",
    "pkg/beat.py": "class Service:\n    pass\n",
    "pkg/worker.py": "class Beat:\n    def start(self):\n        from pkg.beat import Service\n",
    # Ruby: in ``class Jk::Commands::Build`` the lexical scope is the class
    # alone, so ``Command`` is ``::Command``; and a superclass is evaluated
    # before its class opens, so ``Box < Item`` never means ``Jk::Box::Item``.
    "lib/jk.rb": "module Jk\n  module Commands\n    class Command\n    end\n  end\n  class Item\n  end\n"
                 "  class Box\n    class Item\n    end\n  end\nend\n",
    "lib/command.rb": "class Command\nend\n",
    "lib/jk/commands/build.rb": "class Jk::Commands::Build < Command\nend\n",
    "lib/jk/box.rb": "module Jk\n  class Box < Item\n  end\nend\n",
    # Python: a re-exported claimant's base list is read in the declaring
    # file, not in the ``__init__`` that binds an unrelated ``Base``.
    "rx/__init__.py": "from .impl import Worker\nfrom .other import Base\n",
    "rx/bases.py": "class Base(object):\n    pass\n",
    "rx/other.py": "class Base(object):\n    pass\n",
    "rx/impl.py": "from .bases import Base\n\nclass Worker(Base):\n    pass\n",
    # ...but a file that declares the name and binds its fold is the declarer.
    "rx/apps/beat.py": "from .. import beat\nfrom ..bases import Base\n\nclass Beat(Base):\n    pass\n",
    "rx/beat.py": "",
    # Python: ``cf.Config`` (bound by import) and ``cf/config.py`` fold to one key.
    "cf/__init__.py": "from .settings import Config\n",
    "cf/settings.py": "class Config(object):\n    pass\n",
    "cf/config.py": "from .helpers import DEFAULTS\n",
    "cf/helpers.py": "DEFAULTS = {}\n",
    # A second review. Python: a package ``__init__`` with no edge of its own
    # is still what ``import pa`` names.
    "pa/__init__.py": "class Base:\n    pass\n",
    "pa/x.py": "from pa import Base\n",
    "pa/y.py": "import pa\n",
    # Python: a base reached through ``cg.Config``, which folds onto the module
    # ``cg/config.py``, could be either file.
    "cg/__init__.py": "from .settings import Config\n",
    "cg/settings.py": "class Config(object):\n    pass\n",
    "cg/config.py": "DEFAULTS = {}\n",
    "cg/app.py": "from cg import Config\n\nclass AppConfig(Config):\n    pass\n",
    # Python: a nested class's base is looked up in the enclosing class body.
    "pn/a.py": "class Base:\n    pass\n\nclass Outer:\n    class Base:\n        pass\n\n"
               "    class Inner(Base):\n        pass\n",
    # Python: ``from . import sub`` must not lend ``ps/sub.py`` the __init__'s edges.
    "ps/__init__.py": "from . import sub\nfrom . import other\n",
    "ps/sub.py": "X = 1\n",
    "ps/other.py": "Y = 2\n",
    # Java: an ``extends`` clause is outside the class body (JLS 6.3), and an
    # outer type's inherited member type outranks a same-package one.
    "src/com/z/Base.java": "package com.z;\npublic class Base {}\n",
    "src/com/z/Outer.java": "package com.z;\npublic class Outer extends Base {\n  static class Base {}\n}\n",
    "src/com/z/Parent.java": "package com.z;\npublic class Parent {\n  public static class Base {}\n}\n",
    "src/com/z/Holder.java": "package com.z;\npublic class Holder extends Parent {\n"
                             "  static class Inner extends Base {}\n}\n",
    # Ruby: ``module A::B`` records no scopes for a class nested the long way;
    # and ``include`` is a gap the oracle names, not evidence of absence.
    "lib/a.rb": "module A\n  module B\n    class C < D\n    end\n    class E < Other::D\n    end\n"
                "  end\nend\nmodule A::B\nend\n",
    "lib/a/d.rb": "module A\n  class D\n  end\nend\n",
    "lib/other.rb": "module Other\n  class D\n  end\nend\n",
    "lib/d.rb": "class D\nend\n",
    "lib/mixin.rb": "module Mixin\nend\n",
    "lib/foo.rb": "class Foo\n  include Mixin\nend\n",
    # Ruby: a qualified head searches the innermost scope's ancestors before
    # the top level, so ``Part::Thing`` inside ``Junior < Elder`` is
    # ``Elder::Part::Thing``, which the oracle does not walk.
    # Three files, so no file-level lift settles either claim.
    "lib/elder.rb": "class Elder\nend\n",
    "lib/elder/part.rb": "module Elder::Part\n  class Thing\n  end\nend\n",
    "lib/part.rb": "module Part\n  class Thing\n  end\nend\n",
    "lib/junior.rb": "class Junior < Elder\n  class Inner < Part::Thing\n  end\nend\n",
    # A third review. Python: one class's base joins two files, and lifts
    # nothing onto a sibling class that inherits from object.
    "pz/a.py": "from pz.b import Other\n\nclass Helper:\n    pass\n\nclass Sub(Other):\n    def run(self):\n        pass\n",
    "pz/b.py": "class Other:\n    pass\n",
    # Ruby and TypeScript: a class made by assignment has a superclass too.
    "lib/rv/errors.rb": "module Rv\n  module Errors\n    Fatal = Class.new(::RuntimeError)\n"
                        "    Late = Class.new(Fatal)\n  end\nend\n",
    "lib/rv/other.rb": "module Rv\n  class Base\n  end\n  Sub = Class.new(Base) do\n    def x; end\n  end\nend\n",
    "rq/base.ts": "export class Base {}\n",
    "rq/q.ts": "export class Model {}\nexport const Stamped = class extends Model {};\n",
    "rq/r.ts": "import { Base } from './base';\nexport const Mixed = class extends Base {};\n",
    # A bare subject has no scope, so only the top-level fallback's guards
    # stand between it and a root module: a base is never a module,
    # TypeScript has no top level, and a name another key ends in is not
    # settled by the one exact match.
    "qconf.py": "X = 1\n",
    "tswid.ts": "export const v = 1;\n",
    "rcfg.py": "import os\n",
    "rapp/__init__.py": "",
    "rapp/base.py": "class B:\n    pass\n",
    "rapp/settings.py": "from rapp import base\n\nclass Rcfg(base.B):\n    pass\n",
    # Python: a name imported through a package's re-export is still the
    # declaring module's name; the declaring file itself is a transitive load.
    "pr/__init__.py": "from pr.impl import Gadget\n",
    "pr/impl.py": "class Gadget:\n    pass\n",
    "pr/a.py": "from pr import Gadget\n\ndef go():\n    return Gadget()\n",
    # A module re-exported by name is the bound object itself, so the path
    # claim holds too; a same-named function the ``__init__`` defines does not.
    "pm/__init__.py": "from pm.impl import helpers\n",
    "pm/impl/__init__.py": "",
    "pm/impl/helpers.py": "def h():\n    return 1\n",
    "pm/a.py": "from pm import helpers\n\ndef go():\n    return helpers.h()\n",
    "pq/__init__.py": "def helpers():\n    return 1\n",
    "pq/impl/__init__.py": "",
    "pq/impl/helpers.py": "def h():\n    return 1\n",
    "pq/a.py": "from pq import helpers\n",
    # Python: a class declared in an ``__init__`` and a sibling module of the
    # same name fold to one key, as an import-bound collision does.
    "pw/__init__.py": "class Settingz:\n    pass\n",
    "pw/settingz.py": "DEFAULTS = {}\n",
    "pw/x.py": "from pw import Settingz\n",
    # A fourth review. Python: an ``__init__`` importing its own submodule by
    # absolute name binds the submodule, which hides nothing.
    "pi/__init__.py": "from pi import sub2\n",
    "pi/sub2.py": "def f():\n    return 1\n",
    "pi/a.py": "from pi import sub2\n",
    # Ruby: a plugin requires the host gem and reopens its namespace.
    "lib/jekyll-zz.rb": "require \"jekyll\"\nmodule Jekyll\n  module ZzFilter\n  end\nend\n",
    # Java: a single-type import of an outside type shadows a same-package one.
    "src/com/w/Timer.java": "package com.w;\npublic class Timer {}\n",
    "src/com/w/Job.java": "package com.w;\nimport java.util.Timer;\npublic class Job extends Timer {}\n",
    # Python: pytest prepends a conftest.py's directory to the path.
    "tq/conftest.py": "",
    "tq/helpers.py": "def make():\n    return 1\n",
    "tq/unit/test_more.py": "import helpers\n",
    # Python: inheriting one class of a file is not inheriting its sibling;
    # and a re-exported claimant does not borrow the ``__init__``'s imports.
    "pe/errors.py": "class ClientErr(Exception):\n    pass\n\nclass ServerErr(Exception):\n    pass\n",
    "pe/views.py": "from pe.errors import ClientErr\n\nclass Missing(ClientErr):\n    pass\n",
    "pt/__init__.py": "from pt.impl import Thingy\nfrom pt.other import helper\n",
    "pt/impl.py": "class Thingy:\n    pass\n",
    "pt/other.py": "def helper():\n    return 1\n",
}

REVIEW_CASES = [
    ("COM.Y.B", "COM.X.UTIL.FOO", "com.y.B IMPORTS com.x.util.Foo", "confirmed", "lifted"),
    ("COM.Y.B", "COM.X.UTIL", "com.y.B IMPORTS com.x.Util", "contradicted", "no-edge"),
    ("COM.X.A", "BASE", "com.x.A INHERITS Base", "confirmed", "exact"),
    ("WEB.A.A", "WIDGET", "web.a.A INHERITS Widget", "confirmed", "exact"),
    ("WEB/A.TS", "WIDGET", "web/a.ts IMPORTS Widget", "confirmed", "lifted"),
    ("PKG.X", "PKG.Y", "pkg.X IMPORTS pkg.Y", "contradicted", "same-file"),
    ("PKG/WORKER.PY", "BEAT", "pkg/worker.py IMPORTS beat", "confirmed", "exact"),
    ("JK::COMMANDS::BUILD", "COMMAND", "Jk::Commands::Build INHERITS Command", "confirmed", "exact"),
    ("JK::BOX", "ITEM", "Jk::Box INHERITS Item", "confirmed", "exact"),
    ("RX.WORKER", "BASE", "rx.Worker INHERITS Base", "confirmed", "lifted"),
    ("RX.APPS.BEAT.BEAT", "BASE", "rx.apps.beat.Beat INHERITS Base", "confirmed", "exact"),
    ("RX.APPS.BEAT.BEAT", "BASE", "rx.apps.beat.Beat IMPORTS Base", "confirmed", "lifted"),
    # A dotted title on the folded key may mean either; a path names the module.
    ("CF.CONFIG", "CF/SETTINGS.PY", "cf.Config IMPORTS cf.settings", "out-of-reach", "ungrounded"),
    ("CF/CONFIG.PY", "CF/SETTINGS.PY", "cf/config.py IMPORTS cf.settings", "contradicted", "no-edge"),
    ("CF/CONFIG.PY", "CF/HELPERS.PY", "cf/config.py IMPORTS cf.helpers", "confirmed", "exact"),
    ("PA/X.PY", "PA.BASE", "pa/x.py IMPORTS pa.Base", "confirmed", "lifted"),
    ("PA/Y.PY", "PA/__INIT__.PY", "pa/y.py IMPORTS pa", "confirmed", "lifted"),
    ("CG.APP.APPCONFIG", "CG.SETTINGS.CONFIG", "cg.app.AppConfig INHERITS cg.settings.Config", "unchecked", "no-edge"),
    ("CG.APP.APPCONFIG", "CG/CONFIG.PY", "cg.app.AppConfig INHERITS cg/config.py", "unchecked", "no-edge"),
    ("PN.A.OUTER.INNER", "BASE", "pn.a.Outer.Inner INHERITS Base", "confirmed", "exact"),
    ("PS/SUB.PY", "PS/OTHER.PY", "ps/sub.py IMPORTS ps/other.py", "contradicted", "no-edge"),
    ("COM.Z.OUTER", "BASE", "com.z.Outer INHERITS Base", "confirmed", "exact"),
    ("COM.Z.HOLDER.INNER", "BASE", "com.z.Holder.Inner INHERITS Base", "confirmed", "exact"),
    ("A::B::C", "D", "A::B::C INHERITS D", "confirmed", "exact"),
    ("FOO", "MIXIN", "Foo INHERITS Mixin", "unchecked", "no-edge"),
    ("JUNIOR::INNER", "ELDER::PART::THING", "Junior::Inner INHERITS Elder::Part::Thing", "unchecked", "no-edge"),
    ("JUNIOR::INNER", "PART::THING", "Junior::Inner INHERITS Part::Thing", "unchecked", "no-edge"),
    ("PZ.A.HELPER", "PZ.B.OTHER", "pz.a.Helper INHERITS pz.b.Other", "contradicted", "other-type"),
    # A member named as the claimant still lifts through its class.
    ("PZ.A.SUB.RUN", "PZ.B.OTHER", "pz.a.Sub.run INHERITS pz.b.Other", "confirmed", "lifted"),
    ("RV::SUB", "RV::BASE", "Rv::Sub INHERITS Rv::Base", "confirmed", "exact"),
    # ``Fatal`` is itself assigned, so the base stays unresolved.
    ("RV::ERRORS::LATE", "RV::ERRORS::FATAL", "Rv::Errors::Late INHERITS Rv::Errors::Fatal", "unchecked", "same-file"),
    ("RQ.Q.STAMPED", "RQ.Q.MODEL", "rq.q.Stamped INHERITS rq.q.Model", "confirmed", "exact"),
    ("RQ.R.MIXED", "RQ.BASE.BASE", "rq.r.Mixed INHERITS rq.base.Base", "confirmed", "exact"),
    ("QCONF", "PZ.B.OTHER", "Qconf INHERITS pz.b.Other", "out-of-reach", "ungrounded"),
    ("TSWID", "RQ/BASE.TS", "Tswid IMPORTS rq/base.ts", "out-of-reach", "ungrounded"),
    ("RCFG", "RAPP.BASE", "Rcfg IMPORTS rapp.base", "out-of-reach", "ungrounded"),
    ("PR.A", "PR.IMPL.GADGET", "pr.a IMPORTS pr.impl.Gadget", "confirmed", "re-export"),
    ("PR.A.GO", "PR.IMPL.GADGET", "pr.a.go IMPORTS pr.impl.Gadget", "confirmed", "re-export"),
    ("PR/A.PY", "PR/IMPL.PY", "pr/a.py IMPORTS pr/impl.py", "contradicted", "no-edge"),
    ("PM.A", "PM.IMPL.HELPERS", "pm.a IMPORTS pm.impl.helpers", "confirmed", "re-export"),
    ("PM/A.PY", "PM/IMPL/HELPERS.PY", "pm/a.py IMPORTS pm/impl/helpers.py", "confirmed", "re-export"),
    ("PQ.A", "PQ.IMPL.HELPERS", "pq.a IMPORTS pq.impl.helpers", "contradicted", "no-edge"),
    ("PW.X", "PW.SETTINGZ", "pw.x IMPORTS pw.settingz", "out-of-reach", "ungrounded"),
    ("PW/X.PY", "PW/SETTINGZ.PY", "pw/x.py IMPORTS pw/settingz.py", "contradicted", "no-edge"),
    ("PI/A.PY", "PI/SUB2.PY", "pi/a.py IMPORTS pi/sub2.py", "confirmed", "exact"),
    ("LIB/JEKYLL-ZZ.RB", "JEKYLL", "lib/jekyll-zz.rb IMPORTS jekyll", "confirmed", "external"),
    ("COM.W.JOB", "TIMER", "com.w.Job INHERITS Timer", "out-of-reach", "ungrounded"),
    ("TQ/UNIT/TEST_MORE.PY", "TQ/HELPERS.PY", "tq/unit/test_more.py IMPORTS tq/helpers.py", "unchecked", "no-edge"),
    ("PE.VIEWS.MISSING", "PE.ERRORS.SERVERERR", "pe.views.Missing INHERITS pe.errors.ServerErr",
     "contradicted", "other-type"),
    ("PE.VIEWS.MISSING", "PE.ERRORS.CLIENTERR", "pe.views.Missing INHERITS pe.errors.ClientErr", "confirmed", "exact"),
    ("PT.THINGY", "PT.OTHER", "pt.Thingy IMPORTS pt.other", "contradicted", "other-type"),
]


class TestReviewCases(unittest.TestCase):
    def test_each(self):
        import pandas as pd

        with tempfile.TemporaryDirectory() as tmp:
            for path, body in REVIEW_FILES.items():
                (Path(tmp) / path).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / path).write_text(body, encoding="utf-8")
            ev = EdgeEvidence.from_checkout(Path(tmp))
        frame = pd.DataFrame([c[:3] for c in REVIEW_CASES], columns=["source", "target", "description"])
        scored = edge_verdict.score(ev, frame)
        for case, got, why in zip(REVIEW_CASES, scored["verdict"], scored["reason"], strict=True):
            with self.subTest(description=case[2]):
                self.assertEqual((got, why), case[3:])


class TestAncestorRule(unittest.TestCase):
    def test_every_edge_dialect_records_a_rule(self):
        from provenance.edges import RESOLVERS

        self.assertEqual(set(edge_verdict.ANCESTOR_IMPORTS), set(RESOLVERS))

    def test_an_unrecorded_dialect_raises(self):
        from types import SimpleNamespace
        from unittest import mock

        cobol = SimpleNamespace(name="cobol")
        with mock.patch.object(edge_verdict, "dialect_for", return_value=cobol), \
                self.assertRaisesRegex(ValueError, "cobol"):
            edge_verdict.imports_ancestors("x.cbl")


class TestImportBindings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        _checkout(Path(cls._tmp.name))
        cls.ev = EdgeEvidence.from_checkout(Path(cls._tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_where_each_bound_name_grounds(self):
        cases = [
            ("PKG.H.JOIN", {"pkg/h.py"}),  # bound from outside the checkout
            ("PKG.VERSION", {"pkg/__init__.py"}),  # a module-level assignment
            ("PKG.BASE", {"pkg/__init__.py", "pkg/c.py"}),  # binder and declarer
            ("PKG.H.SEP", set()),  # a function's import binds nothing in the module
        ]
        for title, files in cases:
            with self.subTest(title=title):
                self.assertEqual(self.ev.files(self.ev.ground(title)), files)

    def test_every_edge_dialect_records_a_rule(self):
        from provenance.edge_evidence import IMPORT_BINDS, SCOPES
        from provenance.edges import RESOLVERS

        self.assertEqual(set(IMPORT_BINDS), set(RESOLVERS))
        self.assertEqual(set(SCOPES), set(RESOLVERS))


class TestFactsCarryEvidence(unittest.TestCase):
    """The verdict runs where the checkout is gone, so facts must carry it intact."""

    def test_collect_write_read_merge(self):
        from provenance import facts
        from provenance.pipeline import merge_facts

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            _checkout(root)
            collected = facts.collect(root, run_syft=False)
            self.assertEqual(collected.edge_evidence, EdgeEvidence.from_checkout(root).to_dict())
            again = facts.read(facts.write(collected, Path(tmp) / "facts.json"))
            self.assertEqual(again.edge_evidence, collected.edge_evidence)
            merged = merge_facts([again, facts.read(Path(tmp) / "facts.json")], root=tmp)
            self.assertEqual(merged.edge_evidence, collected.edge_evidence)


class TestVerdictByReason(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        _checkout(Path(cls._tmp.name))
        cls.ev = EdgeEvidence.from_checkout(Path(cls._tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _frame(self, rows):
        import pandas as pd

        return pd.DataFrame(rows, columns=["source", "target", "description"])

    def test_each_reason(self):
        scored = edge_verdict.score(self.ev, self._frame([c[:3] for c in CASES]))
        for (source, target, description, want, reason), got, why in zip(
            CASES, scored["verdict"], scored["reason"], strict=True
        ):
            with self.subTest(description=description):
                self.assertEqual((got, why), (want, reason), f"{source} -> {target}")

    def test_split_accounts_for_every_row(self):
        scored = edge_verdict.score(self.ev, self._frame([c[:3] for c in CASES]))
        counts = edge_verdict.split(scored)
        self.assertEqual(sum(counts.values()), len(CASES))
        self.assertEqual(set(counts), set(edge_verdict.VERDICTS))

    def test_round_trip_through_facts_form(self):
        again = EdgeEvidence.from_dict(self.ev.to_dict())
        self.assertEqual(again.to_dict(), self.ev.to_dict())
        scored = edge_verdict.score(again, self._frame([c[:3] for c in CASES]))
        self.assertEqual(list(scored["verdict"]), [c[3] for c in CASES])

    def test_merge_is_a_union(self):
        part = self.ev.to_dict()
        merged = EdgeEvidence.merge([part, part, {}])
        self.assertEqual(merged, part)

    def test_report_accounts_for_every_row(self):
        from types import SimpleNamespace

        from provenance.grounding_report import GroundingReport, render

        rows = [c[:3] for c in CASES] + [("PKG/A.PY", "REPO", "pkg/a.py BELONGS_TO repo")]
        report = GroundingReport()
        edge_verdict.record(self._frame(rows), SimpleNamespace(edge_evidence=self.ev.to_dict()), report)
        self.assertEqual(sum(report.edge_verdicts.values()), len(CASES))
        self.assertEqual(report.edges_written, 1)
        self.assertEqual(sum(report.edge_contradictions.values()), report.edge_verdicts["contradicted"])
        text = render(report)
        self.assertIn(f"edge verdict:  {len(CASES)} model edge(s)", text)
        self.assertIn("1 written row(s) not judged", text)

    def test_without_evidence_the_report_says_it_did_not_run(self):
        from provenance.grounding_report import GroundingReport, render

        report = GroundingReport()
        edge_verdict.record(self._frame([c[:3] for c in CASES]), None, report)
        self.assertEqual(report.edge_verdicts, {})
        self.assertIn("edge verdict:  not run", render(report))

    def test_empty_evidence_checks_nothing(self):
        scored = edge_verdict.score(EdgeEvidence.from_dict(None), self._frame([c[:3] for c in CASES]))
        self.assertEqual(edge_verdict.precision(edge_verdict.split(scored)), None)


class TestDrop(unittest.TestCase):
    """Grounding removes only the three reasons, and records what it removed."""

    @classmethod
    def setUpClass(cls):
        from types import SimpleNamespace

        import pandas as pd

        from provenance.grounding_report import GroundingReport

        with tempfile.TemporaryDirectory() as tmp:
            _checkout(Path(tmp))
            cls.facts = SimpleNamespace(edge_evidence=EdgeEvidence.from_checkout(Path(tmp)).to_dict())
        cls.frame = pd.DataFrame([c[:3] for c in CASES], columns=["source", "target", "description"])
        cls.report = GroundingReport()
        cls.kept = edge_verdict.judge(cls.frame, cls.facts, cls.report)

    def test_only_the_three_reasons_go(self):
        want = [c[2] for c in CASES if c[3] == "contradicted" and c[4] in edge_verdict.DROPPED_REASONS]
        self.assertEqual([r["description"] for r in self.report.dropped_edges], want)
        self.assertEqual(self.report.edges_dropped, {"no-edge": 2, "reversed": 1, "same-file": 1})
        self.assertEqual(len(self.kept), len(CASES) - len(want))
        self.assertEqual(list(self.kept.index), list(range(len(self.kept))))

    def test_what_survives(self):
        kept = set(self.kept["description"])
        for description in (
            # Partial evidence: nothing declares it, so nothing contradicts it.
            "pkg/a.py IMPORTS pkg.missing",
            "pkg/a.py IMPORTS requests",
            # A bare name that would ground uniquely to the wrong file: the
            # top-level ``Drop`` in ``bench/top.rb``.
            "Jk::Drops::StaticDrop INHERITS Drop",
            # The oracle relates the pair; only the type is wrong.
            "pkg.a.A INHERITS pkg.b",
            # Beside an unresolved oracle edge, so the oracle did not say no.
            "pkg/d.py IMPORTS pkg.c",
        ):
            with self.subTest(description=description):
                self.assertIn(description, kept)

    def test_the_table_keeps_every_row(self):
        table = edge_verdict.table(self.kept, self.facts, self.report.dropped_edges)
        self.assertEqual(len(table), len(CASES))
        flagged = table[table[edge_verdict.DROPPED_COLUMN]]
        self.assertEqual(set(flagged["reason"]), set(edge_verdict.DROPPED_REASONS))
        self.assertEqual(list(flagged["description"]), [r["description"] for r in self.report.dropped_edges])

    def test_without_evidence_nothing_is_dropped(self):
        from provenance.grounding_report import GroundingReport

        report = GroundingReport()
        self.assertIs(edge_verdict.judge(self.frame, None, report), self.frame)
        self.assertEqual((report.edges_dropped, report.dropped_edges), ({}, []))


#: ``run -> (confirmed, contradicted, out-of-reach, unchecked)`` over the
#: model's rows of the grounded arm, as the harness reported them. Grounding
#: drops the IMPORTS and INHERITS contradictions it can (4 and 9), so what is
#: left is the one kept INHERITS row and the CALLS contradictions, which are
#: reported and never dropped (12 and 5). MANIFEST, Relationship layer Phase 8.
PINNED = {
    "parser-full-120b": (1359, 5, 2441, 141),
    "parser-full-120b-typed": (1469, 13, 1324, 119),
}


@unittest.skipUnless(CELERY.is_dir() and all((RUNS / f"{r}-dedup").is_dir() for r in PINNED),
                     "needs the celery checkout and the grounded celery arms under augur-test")
class TestCeleryArmsPin(unittest.TestCase):
    """Reproduces ``harness/edge_verdict.py`` on the two celery arms.

    The ``-dedup`` arms are rewritten by every grounding change, so a moved
    count here means rebuilding them with ``apply_provenance.py --refinalise``
    before reading it as a change in the verdict.
    """

    def test_counts(self):
        import pandas as pd

        ev = EdgeEvidence.from_checkout(CELERY)
        for run, want in PINNED.items():
            raw = pd.read_parquet(RUNS / run / "output" / "relationships.parquet")
            rel = pd.read_parquet(RUNS / f"{run}-dedup" / "output" / "relationships.parquet")
            rel = rel[rel["id"].isin(set(raw["id"]))].reset_index(drop=True)
            counts = edge_verdict.split(edge_verdict.score(ev, rel))
            with self.subTest(run=run):
                self.assertEqual(tuple(counts[v] for v in edge_verdict.VERDICTS), want, counts)

    def test_the_product_selects_the_same_model_rows_by_type(self):
        """The harness picks model rows by raw id; the product, before ids exist, by type."""
        from types import SimpleNamespace

        import pandas as pd

        from provenance.grounding_report import GroundingReport

        facts = SimpleNamespace(edge_evidence=EdgeEvidence.from_checkout(CELERY).to_dict())
        for run, want in PINNED.items():
            report = GroundingReport()
            rel = pd.read_parquet(RUNS / f"{run}-dedup" / "output" / "relationships.parquet")
            edge_verdict.record(rel, facts, report)
            with self.subTest(run=run):
                self.assertEqual(tuple(report.edge_verdicts[v] for v in edge_verdict.VERDICTS), want)


if __name__ == "__main__":
    unittest.main()
