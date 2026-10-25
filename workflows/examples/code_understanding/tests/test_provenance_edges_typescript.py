"""TypeScript edges: constructed cases per resolution rule, and a regression pin.

There is no second TypeScript reader, so nothing here is an oracle. The cases
are the compiler's module resolution and the npm workspace rule written as
inputs with known answers; the nest totals are what the resolver produced on
v12.2.0, guarded by a file count. Three structural assertions keep the pin
from meaning "it matched itself": every source joins a node key, every base
says extends or implements, and every internal base target is a class node,
which is what following re-exports through barrel files is for. One file's
edges were checked against the source by hand.
"""

import json
import os
import tempfile
import textwrap
import unittest
import warnings
from pathlib import Path

from provenance.corpus import dotted_key
from provenance.declarations import namespace_for
from provenance.dialects import TYPESCRIPT, dialect_for
from provenance.edges import (
    EXTERNAL,
    IMPORTS,
    INHERITS,
    INTERNAL,
    UNRESOLVED,
    Inventory,
    scan_checkout,
)
from provenance.edges.typescript import TypeScriptResolver, jsonc
from provenance.symbols import package_for
from provenance.treesitter import CLASS, symbols_in_tree

CANDIDATES = Path(
    os.environ.get("AUGUR_CANDIDATES", Path.home() / "Developer" / "augur-test" / "candidates")
)
NEST = CANDIDATES / "nest"
NEST_TS_FILES = 2062


def manifest(**fields):
    return json.dumps(fields)


#: A workspace with two members, a root tsconfig mapping one of them, a
#: member whose own tsconfig hides that mapping, and a sample outside.
TREE = {
    "package.json": manifest(name="root", workspaces=["packages/*"]),
    "tsconfig.json": """{
        // comments and trailing commas, as tsconfig allows
        "compilerOptions": {"paths": {"@ex/mapped": ["./packages/common"], "@ex/mapped/*": ["./packages/common/*"],},},
    }""",
    "packages/common/package.json": manifest(name="@ex/common", version="2.1.0", main="./index.js"),
    "packages/common/index.ts": "export * from './services';\nexport { Inner as Renamed } from './inner.js';\n",
    "packages/common/services/index.ts": "export * from './logger.service.js';\n",
    "packages/common/services/logger.service.ts": "export class Logger {}\nexport const value = 1;\n",
    "packages/common/inner.ts": "export class Inner {}\nexport default class Def {}\n",
    "packages/common/internal.ts": "export class Hidden {}\n",
    "packages/common/twice.ts": "export * from './a';\nexport * from './b';\n",
    "packages/common/a.ts": "export class Dup {}\n",
    "packages/common/b.ts": "export class Dup {}\n",
    "packages/common/leaky.ts": "export * from 'rxjs';\n",
    "packages/common/aliased.ts": "class Local {}\nexport { Local as Public };\n",
    "packages/core/package.json": manifest(name="@ex/core", version="2.1.0",
                                           peerDependencies={"@ex/common": "^2.0.0"}),
    "packages/core/tsconfig.json": '{"extends": "../tsconfig.build.json"}',
    "packages/tsconfig.build.json": '{"compilerOptions": {"strict": true}}',
    "packages/legacy/package.json": manifest(name="@ex/legacy", dependencies={"@ex/common": "^1.0.0"}),
    "packages/core/nested/package.json": manifest(name="nested", dependencies={"@ex/common": "^1.0.0"}),
    "sample/app/package.json": manifest(name="app", dependencies={"@ex/common": "2.0.0"}),
    "sample/app/tsconfig.json": '{"compilerOptions": {"baseUrl": "./src"}}',
    "sample/app/src/shared/util.ts": "export class Util {}\n",
    "sample/bare/package.json": manifest(name="bare"),
}


def edges_of(path, source, kind=None):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        files = {**TREE, path: textwrap.dedent(source)}
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        found, used_ast = TypeScriptResolver(Inventory(files, root)).edges(path, files[path])
    assert not used_ast
    return {
        (e.kind, e.written, e.target, e.target_class)
        for e in found if kind is None or e.kind == kind
    }


CORE = "packages/core/src/m.ts"


class TestJsonc(unittest.TestCase):
    def test_comments_and_trailing_commas_inside_and_outside_strings(self):
        self.assertEqual(jsonc('{"a": "x//y", /* c */ "b": [1,], // d\n}'), {"a": "x//y", "b": [1]})

    def test_garbage_is_none(self):
        self.assertIsNone(jsonc("{nope"))


class TestTypeScriptImports(unittest.TestCase):
    CASES = [
        ("the ESM convention: ./x.js names x.ts",
         "packages/common/m.ts", "import { Inner } from './inner.js';",
         {(IMPORTS, "./inner.js", "packages.common.inner", INTERNAL)}),
        ("a directory names its index",
         "packages/common/m.ts", "import { Logger } from './services';",
         {(IMPORTS, "./services", "packages.common.services.index", INTERNAL)}),
        ("a relative specifier naming nothing",
         "packages/common/m.ts", "import { X } from './gone';",
         {(IMPORTS, "./gone", None, UNRESOLVED)}),
        ("the nearest tsconfig's paths, exact and wildcard",
         "tools/m.ts", "import { A } from '@ex/mapped';\nimport { B } from '@ex/mapped/internal';",
         {(IMPORTS, "@ex/mapped", "packages.common.index", INTERNAL),
          (IMPORTS, "@ex/mapped/internal", "packages.common.internal", INTERNAL)}),
        ("a path that matches and names no file",
         "tools/m.ts", "import { A } from '@ex/mapped/gone';",
         {(IMPORTS, "@ex/mapped/gone", None, UNRESOLVED)}),
        ("a nearer tsconfig hides the root's paths, so a mapping does not apply",
         CORE, "import { A } from '@ex/mapped';",
         {(IMPORTS, "@ex/mapped", "@ex.mapped", EXTERNAL)}),
        ("a workspace member by name, through its manifest's main",
         CORE, "import { Logger } from '@ex/common';",
         {(IMPORTS, "@ex/common", "packages.common.index", INTERNAL)}),
        ("a workspace member's subpath",
         CORE, "import { Hidden } from '@ex/common/internal';",
         {(IMPORTS, "@ex/common/internal", "packages.common.internal", INTERNAL)}),
        ("a member asking for a range the workspace version does not satisfy",
         "packages/legacy/m.ts", "import { Logger } from '@ex/common';",
         {(IMPORTS, "@ex/common", None, UNRESOLVED)}),
        ("a workspace glob's * stays within one directory, so nested is outside",
         "packages/core/nested/m.ts", "import { Logger } from '@ex/common';",
         {(IMPORTS, "@ex/common", "@ex.common", EXTERNAL)}),
        ("outside the workspace, a declared dependency is the published package",
         "sample/app/src/m.ts", "import { Logger } from '@ex/common';",
         {(IMPORTS, "@ex/common", "@ex.common", EXTERNAL)}),
        ("outside the workspace and undeclared",
         "sample/bare/m.ts", "import { Logger } from '@ex/common';",
         {(IMPORTS, "@ex/common", None, UNRESOLVED)}),
        ("baseUrl resolves a bare specifier before node_modules",
         "sample/app/src/m.ts", "import { Util } from 'shared/util';",
         {(IMPORTS, "shared/util", "sample.app.src.shared.util", INTERNAL)}),
        ("a package outside the checkout",
         CORE, "import { map } from 'rxjs/operators';",
         {(IMPORTS, "rxjs/operators", "rxjs.operators", EXTERNAL)}),
        ("re-exports, side effects and dynamic imports are edges too",
         "packages/common/m.ts",
         "export * from './a';\nimport './b';\nasync function f() { await import('./inner.js'); }",
         {(IMPORTS, "./a", "packages.common.a", INTERNAL),
          (IMPORTS, "./b", "packages.common.b", INTERNAL),
          (IMPORTS, "./inner.js", "packages.common.inner", INTERNAL)}),
    ]

    def test_cases(self):
        for name, path, source, expected in self.CASES:
            with self.subTest(name):
                self.assertEqual(edges_of(path, source, IMPORTS), expected)


class TestTypeScriptBases(unittest.TestCase):
    CASES = [
        ("a class declared in the file", "class Base {}\nclass M extends Base {}",
         {(INHERITS, "Base", "packages.core.src.m.base", INTERNAL)}),
        ("through two barrels and export *",
         "import { Logger } from '@ex/common';\nclass M extends Logger {}",
         {(INHERITS, "Logger", "packages.common.services.logger.service.logger", INTERNAL)}),
        ("export { a as b } from",
         "import { Renamed } from '@ex/common';\nclass M extends Renamed {}",
         {(INHERITS, "Renamed", "packages.common.inner.inner", INTERNAL)}),
        ("a local export clause's alias",
         "import { Public } from '@ex/common/aliased';\nclass M extends Public {}",
         {(INHERITS, "Public", "packages.common.aliased.local", INTERNAL)}),
        ("a default import of a default-exported class",
         "import Base from '@ex/common/inner';\nclass M extends Base {}",
         {(INHERITS, "Base", "packages.common.inner.def", INTERNAL)}),
        ("a namespace import",
         "import * as c from '@ex/common/inner';\nclass M extends c.Inner {}",
         {(INHERITS, "c.Inner", "packages.common.inner.inner", INTERNAL)}),
        ("two export * sources supplying one name",
         "import { Dup } from '@ex/common/twice';\nclass M extends Dup {}",
         {(INHERITS, "Dup", None, UNRESOLVED)}),
        ("a name an external export * may supply",
         "import { Subject } from '@ex/common/leaky';\nclass M extends Subject {}",
         {(INHERITS, "Subject", None, UNRESOLVED)}),
        ("an exported value is not a class",
         "import { value } from '@ex/common';\nclass M extends value {}",
         {(INHERITS, "value", None, UNRESOLVED)}),
        ("a name imported from outside, with its type arguments",
         "import { Subject } from 'rxjs';\nclass M extends Subject<number> implements Iterable<number> {}",
         {(INHERITS, "Subject", "rxjs.subject", EXTERNAL),
          (INHERITS, "Iterable<number>", None, UNRESOLVED)}),
        ("a JavaScript global constructor", "class M extends Error {}",
         {(INHERITS, "Error", "error", EXTERNAL)}),
        ("a mixin call is an expression", "class M extends mixin(Base) {}",
         {(INHERITS, "mixin(Base)", None, UNRESOLVED)}),
        ("a binding's value is not syntax",
         "const Base = mixin(X);\nclass M extends Base {}",
         {(INHERITS, "Base", None, UNRESOLVED)}),
        ("an interface extending two types",
         "import { Logger } from '@ex/common';\ninterface I {}\ninterface M extends I, Logger {}",
         {(INHERITS, "I", "packages.core.src.m.i", INTERNAL),
          (INHERITS, "Logger", "packages.common.services.logger.service.logger", INTERNAL)}),
    ]

    def test_cases(self):
        for name, source, expected in self.CASES:
            with self.subTest(name):
                self.assertEqual(edges_of(CORE, source, INHERITS), expected)


def edges_in(files, path, source, kind=None):
    """Like :func:`edges_of`, over a tree of the case's own."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        files = {**files, path: textwrap.dedent(source)}
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_bytes(text.encode("utf-8"))
        found, _ = TypeScriptResolver(Inventory(files, root)).edges(path, files[path])
    return {(e.kind, e.written, e.target, e.target_class) for e in found if kind is None or e.kind == kind}


APP = {"src/x.ts": "export class X {}\n"}
BOM = "\ufeff"


class TestEvidenceNotReadIsNotAbsence(unittest.TestCase):
    """A bare specifier is external only when every config that could claim it was read."""

    CASES = [
        ("every element of an extends array applies, the last winning",
         {**APP, "tsconfig.json": '{"extends": ["./a.json", "./b.json"]}', "a.json": "{}",
          "b.json": '{"compilerOptions": {"paths": {"@app/*": ["src/*"]}}}'},
         "import { X } from '@app/x';", {(IMPORTS, "@app/x", "src.x", INTERNAL)}),
        ("a BOM does not hide a tsconfig",
         {**APP, "tsconfig.json": BOM + '{"compilerOptions": {"paths": {"@app/*": ["src/*"]}}}'},
         "import { X } from '@app/x';", {(IMPORTS, "@app/x", "src.x", INTERNAL)}),
        ("a tsconfig that does not parse",
         {**APP, "tsconfig.json": "{nope"},
         "import { X } from 'lib';", {(IMPORTS, "lib", None, UNRESOLVED)}),
        ("a tsconfig extending a package this does not read",
         {**APP, "tsconfig.json": '{"extends": "@tsconfig/node18/tsconfig.json"}'},
         "import { X } from 'lib';", {(IMPORTS, "lib", None, UNRESOLVED)}),
        ("compilerOptions that are not an object, without crashing",
         {**APP, "tsconfig.json": '{"compilerOptions": [1]}'},
         "import { X } from 'lib';", {(IMPORTS, "lib", None, UNRESOLVED)}),
        ("an exact paths pattern beats a wildcard matching with an empty star",
         {"tsconfig.json": '{"compilerOptions": {"paths": {"foo*": ["./w*"], "foo": ["./exact"]}}}',
          "w.ts": "", "exact.ts": ""},
         "import 'foo';", {(IMPORTS, "foo", "exact", INTERNAL)}),
        ("a paths target that is not a string is skipped, not a crash",
         {"tsconfig.json": '{"compilerOptions": {"paths": {"x": [1, "./y"]}}}', "y.ts": ""},
         "import 'x';", {(IMPORTS, "x", "y", INTERNAL)}),
        ("a directory names its manifest's types before its index",
         {"lib/package.json": manifest(types="src/main.d.ts"), "lib/src/main.ts": "", "lib/index.ts": ""},
         "import './lib';", {(IMPORTS, "./lib", "lib.src.main", INTERNAL)}),
    ]

    def test_cases(self):
        for name, files, source, expected in self.CASES:
            with self.subTest(name):
                self.assertEqual(edges_in(files, "m.ts", source, IMPORTS), expected)


def member(name, version="1.0.0", **fields):
    return manifest(name=name, version=version, main="./index.js", **fields)


class TestWorkspacesAndManifests(unittest.TestCase):
    CASES = [
        ("two sibling workspaces, each linking its own",
         {"be/package.json": manifest(workspaces=["pk/*"]), "be/pk/a/package.json": member("@be/a"),
          "be/pk/a/index.ts": "", "fe/package.json": manifest(workspaces=["pk/*"]),
          "fe/pk/a/package.json": member("@fe/a"), "fe/pk/a/index.ts": "",
          "fe/pk/c/package.json": member("@fe/c")},
         "fe/pk/c/m.ts", "import '@fe/a';", {(IMPORTS, "@fe/a", "fe.pk.a.index", INTERNAL)}),
        ("pnpm-workspace.yaml",
         {"pnpm-workspace.yaml": "packages:\n  - 'packages/*'\n", "package.json": manifest(name="r"),
          "packages/a/package.json": member("@s/a"), "packages/a/index.ts": "",
          "packages/c/package.json": member("@s/c")},
         "packages/c/m.ts", "import '@s/a';", {(IMPORTS, "@s/a", "packages.a.index", INTERNAL)}),
        ("a workspace pattern written with ./ and a BOM on the root manifest",
         {"package.json": BOM + manifest(workspaces=["./packages/*"]),
          "packages/a/package.json": member("@s/a"), "packages/a/index.ts": "",
          "packages/c/package.json": member("@s/c")},
         "packages/c/m.ts", "import '@s/a';", {(IMPORTS, "@s/a", "packages.a.index", INTERNAL)}),
        ("a file: dependency names a directory in the checkout",
         {"packages/a/package.json": member("@s/a"), "packages/a/index.ts": "",
          "sample/package.json": manifest(dependencies={"@s/a": "file:../packages/a"})},
         "sample/m.ts", "import '@s/a';", {(IMPORTS, "@s/a", "packages.a.index", INTERNAL)}),
        ("a # subpath import resolves through its package's imports",
         {"package.json": manifest(imports={"#util": "./src/util.js"}), "src/util.ts": ""},
         "m.ts", "import '#util';", {(IMPORTS, "#util", "src.util", INTERNAL)}),
        ("a # subpath import its package does not map is never external",
         {"package.json": manifest(name="p")},
         "m.ts", "import '#util';", {(IMPORTS, "#util", None, UNRESOLVED)}),
        ("^0.0.3 admits only 0.0.3",
         {"package.json": manifest(workspaces=["packages/*"]),
          "packages/a/package.json": member("@s/a", "0.0.5"), "packages/a/index.ts": "",
          "packages/c/package.json": member("@s/c", dependencies={"@s/a": "^0.0.3"})},
         "packages/c/m.ts", "import '@s/a';", {(IMPORTS, "@s/a", None, UNRESOLVED)}),
        ("a prerelease version is not read as satisfying a range",
         {"package.json": manifest(workspaces=["packages/*"]),
          "packages/a/package.json": member("@s/a", "2.0.0-beta.1"), "packages/a/index.ts": "",
          "packages/c/package.json": member("@s/c", dependencies={"@s/a": "^2.0.0"})},
         "packages/c/m.ts", "import '@s/a';", {(IMPORTS, "@s/a", None, UNRESOLVED)}),
        ("a dependency range that is not a string, without crashing",
         {"package.json": manifest(workspaces=["packages/*"]),
          "packages/a/package.json": member("@s/a"), "packages/a/index.ts": "",
          "packages/c/package.json": member("@s/c", dependencies={"@s/a": None})},
         "packages/c/m.ts", "import '@s/a';", {(IMPORTS, "@s/a", None, UNRESOLVED)}),
    ]

    def test_cases(self):
        for name, files, path, source, expected in self.CASES:
            with self.subTest(name):
                self.assertEqual(edges_in(files, path, source, IMPORTS), expected)


class TestTopLevelScope(unittest.TestCase):
    CASES = [
        ("a local name blocks export * only when it is exported",
         {"a.ts": "class Foo {}\nexport * from './b';\n", "b.ts": "export class Foo {}\n"},
         "import { Foo } from './a';\nclass X extends Foo {}", {(INHERITS, "Foo", "b.foo", INTERNAL)}),
        ("a namespace member is not a top-level export",
         {"a.ts": "namespace N { export class Foo {} }\nexport * from './b';\n", "b.ts": "export class Foo {}\n"},
         "import { Foo } from './a';\nclass X extends Foo {}", {(INHERITS, "Foo", "b.foo", INTERNAL)}),
        ("declare global does not make a global a local class",
         {}, "declare global { interface Array<T> {} }\nclass X<T> extends Array<T> {}",
         {(INHERITS, "Array", "array", EXTERNAL)}),
        ("a class in a block does not shadow the global",
         {}, "if (c) { class Error {} }\nclass X extends Error {}", {(INHERITS, "Error", "error", EXTERNAL)}),
        ("an import inside a namespace does not bind at the top level",
         {"b.ts": "export class Foo {}\n", "c.ts": "export class Foo {}\n"},
         "import { Foo } from './b';\nnamespace N { import { Foo } from './c'; }\nclass X extends Foo {}",
         {(INHERITS, "Foo", "b.foo", INTERNAL)}),
        ("an import alias is not followed, and is not a global",
         {}, "import Event = NS.Event;\nclass X extends Event {}", {(INHERITS, "Event", None, UNRESOLVED)}),
        ("an ambient class leaves no node to point at",
         {"d.ts": "export declare class D {}\n"}, "import { D } from './d';\nclass X extends D {}",
         {(INHERITS, "D", None, UNRESOLVED)}),
    ]

    def test_cases(self):
        for name, files, source, expected in self.CASES:
            with self.subTest(name):
                self.assertEqual(edges_in(files, "m.ts", source, INHERITS), expected)


@unittest.skipUnless(NEST.is_dir(), f"no nest checkout at {NEST}")
class TestNest(unittest.TestCase):
    """A regression pin on nest v12.2.0, not a measurement of truth."""

    BY_CLASS = {
        (IMPORTS, INTERNAL): 5603, (IMPORTS, EXTERNAL): 1721, (IMPORTS, UNRESOLVED): 5,
        (INHERITS, INTERNAL): 353, (INHERITS, EXTERNAL): 80, (INHERITS, UNRESOLVED): 16,
    }

    #: ``packages/microservices/server/server-grpc.ts``, checked by hand.
    SERVER_GRPC = {
        *((IMPORTS, f"../{name}.js", f"packages.microservices.{name.replace('/', '.')}", INTERNAL) for name in (
            "constants", "decorators/index", "enums/index", "errors/invalid-grpc-package.exception",
            "errors/invalid-proto-definition.exception", "external/grpc-options.interface",
            "helpers/index", "interfaces/index", "interfaces/microservice-configuration.interface",
        )),
        (IMPORTS, "./server.js", "packages.microservices.server.server", INTERNAL),
        (IMPORTS, "@nestjs/common/internal", "packages.common.internal", INTERNAL),
        (IMPORTS, "@nestjs/core/internal", "packages.core.internal", INTERNAL),
        (IMPORTS, "module", "module", EXTERNAL),
        (IMPORTS, "rxjs", "rxjs", EXTERNAL),
        (IMPORTS, "rxjs/operators", "rxjs.operators", EXTERNAL),
        (INHERITS, "Server", "packages.microservices.server.server.server", INTERNAL),
    }

    @classmethod
    def setUpClass(cls):
        if len(list(NEST.rglob("*.ts"))) != NEST_TS_FILES:
            raise unittest.SkipTest(f"nest is not the {NEST_TS_FILES}-file v12.2.0 release")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            cls.scan = scan_checkout(NEST)

    def test_totals(self):
        self.assertEqual(self.scan.by_class("typescript"), self.BY_CLASS)

    def test_files_by_evidence(self):
        self.assertEqual(
            {k: v for k, v in self.scan.files.items() if k[0] == "typescript"},
            {("typescript", "tree-sitter"): 2139, ("typescript", "tree-sitter-recovered"): 2},
        )

    def test_every_base_says_extends_or_implements(self):
        self.assertEqual({e.detail for e in self.scan.edges if e.kind == INHERITS}, {"extends", "implements"})

    def test_sources_join_and_internal_bases_are_class_nodes(self):
        modules, classes = set(), set()
        paths = {e.path for e in self.scan.edges} | {
            str(p.relative_to(NEST)) for p in NEST.rglob("*") if p.suffix in TYPESCRIPT.suffixes
        }
        for path in sorted(paths):
            dialect = dialect_for(path)
            source = (NEST / path).read_text(encoding="utf-8", errors="replace")
            modules.add(dotted_key(package_for(path, source, dialect)))
            namespace = namespace_for(path, source, dialect)
            classes |= {
                dotted_key(f"{namespace}.{name}" if namespace else name)
                for kind, name in symbols_in_tree(source, dialect) if kind == CLASS
            }
        unjoined = [e for e in self.scan.edges if e.source not in (modules if e.kind == IMPORTS else classes)]
        self.assertEqual(unjoined, [])
        not_nodes = [
            e for e in self.scan.edges
            if e.kind == INHERITS and e.target_class == INTERNAL and e.target not in classes
        ]
        self.assertEqual(not_nodes, [], "a barrel was not followed to the class it re-exports")

    def test_server_grpc_by_hand(self):
        path = "packages/microservices/server/server-grpc.ts"
        found = {(e.kind, e.written, e.target, e.target_class) for e in self.scan.edges if e.path == path}
        self.assertEqual(found, self.SERVER_GRPC)
