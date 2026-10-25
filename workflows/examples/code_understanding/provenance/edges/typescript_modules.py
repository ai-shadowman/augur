"""Where a TypeScript specifier lands, by the compiler's resolution order.

Split from :mod:`provenance.edges.typescript`, which decides what a name
inside the module is; this decides which file a specifier names.

A relative specifier is a file beside the importer, tried as written, with the
ESM convention that ``./x.js`` names ``x.ts``, with each TypeScript suffix,
through a directory's ``package.json`` ``types``/``main``, and as its
``index``. A bare specifier goes through the nearest ``tsconfig.json``'s
``paths`` (its whole relative ``extends`` chain applied, an exact pattern
before any wildcard), then its ``baseUrl``, then ``node_modules``. There the
one thing the checkout settles is a workspace: a file inside one importing a
member by name gets the member, because npm, yarn and pnpm link workspace
packages, unless the declaring ``package.json`` asks for a range the member's
version does not satisfy. A file outside, nest's ``sample/`` directories, gets
whatever its own ``package.json`` declares, which is the published package
and external.

**Evidence not read is not evidence of absence.** A ``tsconfig`` that fails to
parse, or ``extends`` a package this does not read, may hold ``paths`` that
would have claimed a bare specifier, so every bare specifier under it is
unresolved rather than external. Workspaces are read from every
``package.json`` that declares them, ``pnpm-workspace.yaml`` and
``lerna.json``; a ``file:`` or ``link:`` dependency names a directory here and
is followed; a ``#`` specifier resolves only through its own package's
``imports``, never outward. ``package.json`` ``exports`` maps are not read: a
subpath resolves as a file under the package.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import PurePosixPath

from ..corpus import dotted_key
from ..declarations import module_key
from ..dialects.typescript import package_name
from .model import EXTERNAL, INTERNAL, Inventory, Resolution, unresolved

#: What a ``.js``-family specifier names in TypeScript source.
_JS_TO_TS = {".js": (".ts", ".tsx", ".d.ts"), ".mjs": (".mts", ".d.mts"), ".cjs": (".cts", ".d.cts"), ".jsx": (".tsx",)}
_TS_FILE = (".ts", ".tsx", ".mts", ".cts")
#: A dependency a manifest does not declare, apart from one it declares as junk.
_UNDECLARED = object()


def jsonc(text: str) -> dict | None:
    """``tsconfig.json`` is JSON with comments, trailing commas, and sometimes a BOM."""
    text = text.removeprefix("\ufeff")
    out, i, quote = [], 0, None
    while i < len(text):
        c = text[i]
        if quote:
            out.append(c)
            if c == "\\":
                out.append(text[i + 1 : i + 2])
                i += 1
            elif c == quote:
                quote = None
        elif c == '"':
            quote = c
            out.append(c)
        elif text.startswith("//", i):
            i = text.find("\n", i)
            i = len(text) if i < 0 else i
            continue
        elif text.startswith("/*", i):
            i = text.find("*/", i)
            i = len(text) if i < 0 else i + 2
            continue
        else:
            out.append(c)
        i += 1
    try:
        loaded = json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))
    except ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


@dataclass
class _Config:
    """The compiler options that decide a bare specifier, for one directory."""

    paths: dict[str, list[str]] = field(default_factory=dict)
    paths_base: str = ""
    base_url: str | None = None
    #: Why some of the chain could not be read; ``None`` when all of it was.
    unread: str | None = None

def _norm(path: str) -> str | None:
    """A repository-relative path, normalised; ``None`` if it climbs out."""
    normal = posixpath.normpath(path)
    if normal == ".." or normal.startswith(("../", "/")):
        return None
    return "" if normal == "." else normal


def _glob(relative: str, pattern: str) -> bool:
    """A workspace glob: ``*`` stays within a segment, ``**`` spans any number."""
    parts, patterns = relative.split("/"), pattern.strip().removeprefix("./").rstrip("/").split("/")
    if "**" in patterns:
        head = patterns[: patterns.index("**")]
        return len(parts) >= len(head) and all(fnmatch(p, q) for p, q in zip(parts, head, strict=False))
    return len(parts) == len(patterns) and all(fnmatch(p, q) for p, q in zip(parts, patterns, strict=True))


def _satisfies(wanted, version: str) -> bool | None:
    """Whether a declared range admits a version; ``None`` where the range is not read."""
    if not isinstance(wanted, str):
        return None
    wanted = wanted.strip()
    if wanted in ("", "*", "latest") or wanted.startswith("workspace:"):
        return True
    match = re.fullmatch(r"([\^~]|>=)?v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", wanted)
    have = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match or not have:
        return None  # a prerelease, or a range this does not read
    op = match.group(1) or ""
    low = tuple(int(g or 0) for g in match.group(2, 3, 4))
    got = tuple(int(g) for g in have.groups())
    if op == ">=":
        return got >= low
    if op == "^":
        width = 1 if low[0] else 2 if low[1] else 3
        return got[:width] == low[:width] and got >= low
    if op == "~":
        return got[:2] == low[:2] and got >= low
    return got == low

class ModuleResolver:
    """Which file a specifier names from one file, against one checkout."""

    def __init__(self, inventory: Inventory):
        if inventory.root is None:
            raise ValueError("TypeScript resolution reads tsconfig and package.json; the inventory needs a root")
        self.inventory = inventory
        self.paths = inventory.paths
        self._configs: dict[str, _Config] = {}
        self._json: dict[str, dict | None] = {}
        self.workspaces = self._find_workspaces()

    # ------------------------------------------------------------ manifests

    def _read_json(self, path: str) -> dict | None:
        if path not in self._json:
            text = self.inventory.read(path)
            self._json[path] = jsonc(text) if text is not None else None
        return self._json[path]

    def _find_workspaces(self) -> list[tuple[str, dict[str, tuple[str, str]]]]:
        """Every workspace: its root, and member name to ``(directory, version)``.

        Declared by a ``package.json`` ``workspaces`` field (a list, or an
        object with ``packages``), by ``pnpm-workspace.yaml``, or by
        ``lerna.json``. A workspace this misses would make its members read as
        external, so all three are read and none is preferred.
        """
        manifests = sorted(p for p in self.paths if PurePosixPath(p).name == "package.json")
        declarations: list[tuple[str, list[str]]] = []
        for path in sorted(self.paths):
            name = PurePosixPath(path).name
            root = posixpath.dirname(path)
            if name == "package.json":
                declared = (self._read_json(path) or {}).get("workspaces")
                if isinstance(declared, dict):
                    declared = declared.get("packages")
            elif name == "lerna.json":
                declared = (self._read_json(path) or {}).get("packages")
            elif name == "pnpm-workspace.yaml":
                text = self.inventory.read(path) or ""
                block = re.search(r"^packages:\s*\n((?:[ \t]+-.*\n?)+)", text, re.M)
                declared = re.findall(r"-\s*['\"]?([^'\"#\n]+?)['\"]?\s*$", block.group(1), re.M) if block else None
            else:
                continue
            if isinstance(declared, list):
                declarations.append((root, [p for p in declared if isinstance(p, str) and not p.startswith("!")]))
        workspaces = []
        for root, patterns in declarations:
            members = {}
            for other in manifests:
                directory = posixpath.dirname(other)
                if directory == root or (root and not directory.startswith(f"{root}/")):
                    continue
                relative = directory[len(root) + 1 :] if root else directory
                if any(_glob(relative, pattern) for pattern in patterns):
                    data = self._read_json(other) or {}
                    if isinstance(data.get("name"), str):
                        members[data["name"]] = (directory, str(data.get("version", "")))
            workspaces.append((root, members))
        return workspaces

    def _nearest(self, directory: str, name: str) -> str | None:
        while True:
            candidate = f"{directory}/{name}" if directory else name
            if candidate in self.paths:
                return candidate
            if not directory:
                return None
            directory = PurePosixPath(directory).parent.as_posix()
            directory = "" if directory == "." else directory

    def _chain(self, path: str, seen: frozenset) -> tuple[list[tuple[str, dict]], str | None]:
        """A tsconfig and everything it extends, earliest applied first."""
        data = self._read_json(path)
        if data is None:
            return [], f"{path} does not parse"
        options = data.get("compilerOptions") or {}
        if not isinstance(options, dict):
            return [], f"{path} has compilerOptions that are not an object"
        parents = data.get("extends")
        parents = parents if isinstance(parents, list) else [parents] if parents is not None else []
        chain: list[tuple[str, dict]] = []
        for parent in parents:
            if not isinstance(parent, str) or not parent.startswith("."):
                return [], f"{path} extends {parent!r}, which is not read"
            base = _norm(posixpath.join(posixpath.dirname(path), parent))
            found = base if base and base.endswith(".json") else f"{base}.json" if base else None
            if found is None or found not in self.paths or found in seen:
                return [], f"{path} extends {parent!r}, which is not a file in the checkout"
            earlier, unread = self._chain(found, seen | {found})
            if unread:
                return [], unread
            chain += earlier
        return [*chain, (path, options)], None

    def _config(self, directory: str) -> _Config:
        """The nearest ``tsconfig.json``'s options, its whole ``extends`` chain applied."""
        if directory in self._configs:
            return self._configs[directory]
        config = _Config()
        nearest = self._nearest(directory, "tsconfig.json")
        if nearest is not None:
            chain, config.unread = self._chain(nearest, frozenset({nearest}))
            for path, options in chain:
                here = posixpath.dirname(path)
                if isinstance(options.get("baseUrl"), str):
                    config.base_url = _norm(posixpath.join(here, options["baseUrl"]))
                if isinstance(options.get("paths"), dict):
                    config.paths = options["paths"]
                    config.paths_base = here
            if config.paths and config.base_url is not None:
                config.paths_base = config.base_url
        self._configs[directory] = config
        return config

    # -------------------------------------------------------------- modules

    def _file(self, base: str | None, manifest: bool = True) -> str | None:
        """The file a specifier names once its directory is known, in TypeScript's order.

        A directory with a ``package.json`` names the file its ``types``,
        ``typings`` or ``main`` points at before its ``index``.
        """
        if base is None:
            return None
        if manifest and base and f"{base}/package.json" in self.paths:
            data = self._read_json(f"{base}/package.json") or {}
            for key in ("types", "typings", "main"):
                target = data.get(key)
                if isinstance(target, str) and (found := self._file(_norm(posixpath.join(base, target)), False)):
                    return found
        suffix = PurePosixPath(base).suffix
        candidates = []
        if base.endswith(".d.ts"):
            candidates.append(base[:-5] + ".ts")
        if suffix in _JS_TO_TS:
            candidates += [base[: -len(suffix)] + ts for ts in _JS_TO_TS[suffix]]
        if suffix in _TS_FILE:
            candidates.append(base)
        candidates += [base + ext for ext in (".ts", ".tsx", ".d.ts")]
        candidates += [f"{base}/index{ext}" if base else f"index{ext}" for ext in (".ts", ".tsx", ".d.ts")]
        candidates.append(base)
        return next((c for c in candidates if c in self.paths), None)

    def _landed(self, path: str | None, spec: str) -> tuple[Resolution, str | None]:
        if path is None:
            return unresolved(f"{spec} names no file in the checkout"), None
        return Resolution(INTERNAL, dotted_key(module_key(path))), path

    def resolve(self, spec: str, importer: str) -> tuple[Resolution, str | None]:
        """The module a specifier names from one file, and its path if internal."""
        here = posixpath.dirname(importer)
        if spec.startswith("."):
            return self._landed(self._file(_norm(posixpath.join(here, spec))), spec)
        if spec.startswith("/"):
            return unresolved(f"{spec} is an absolute path"), None
        manifest = self._nearest(here, "package.json")
        if spec.startswith("#"):
            return self._subpath_import(spec, manifest)

        config = self._config(here)
        if config.unread:
            return unresolved(f"{spec} is bare and {config.unread}, so its paths are unknown"), None
        best = None
        for pattern, targets in config.paths.items():
            prefix, star, suffix = pattern.partition("*")
            if star and spec.startswith(prefix) and spec.endswith(suffix) and len(spec) >= len(prefix + suffix):
                rank, matched = (0, len(prefix)), spec[len(prefix) : len(spec) - len(suffix)]
            elif not star and spec == pattern:
                rank, matched = (1, len(pattern)), ""
            else:
                continue
            # An exact pattern beats any wildcard; among wildcards the longest prefix wins.
            if best is None or rank > best[0]:
                best = (rank, matched, targets)
        if best is not None:
            for target in best[2] if isinstance(best[2], list) else []:
                if not isinstance(target, str):
                    continue
                substituted = target.replace("*", best[1])
                if found := self._file(_norm(posixpath.join(config.paths_base, substituted))):
                    return self._landed(found, spec)
            return unresolved(f"{spec} matches a tsconfig path whose targets name no file"), None
        if config.base_url is not None and (found := self._file(_norm(posixpath.join(config.base_url, spec)))):
            return self._landed(found, spec)

        name = package_name(spec) or spec
        sub = spec[len(name) :].lstrip("/")
        data = (self._read_json(manifest) or {}) if manifest else {}
        wanted = next(
            (data[k][name] for k in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")
             if isinstance(data.get(k), dict) and name in data[k]),
            _UNDECLARED,
        )
        if isinstance(wanted, str) and wanted.startswith(("file:", "link:")):
            directory = _norm(posixpath.join(posixpath.dirname(manifest), wanted.split(":", 1)[1]))
            found = self._file(_norm(posixpath.join(directory, sub))) if directory is not None and sub else self._file(directory)
            return self._landed(found, spec)

        owner = posixpath.dirname(manifest) if manifest else None
        mine = next(
            (members for root, members in self.workspaces
             if owner is not None and (owner == root or owner in {d for d, _ in members.values()})),
            None,
        )
        if mine is not None and name in mine:
            directory, version = mine[name]
            if wanted is not _UNDECLARED and _satisfies(wanted, version) is not True:
                return unresolved(f"{name} is wanted at {wanted!r} and the workspace has {version}"), None
            found = self._file(_norm(posixpath.join(directory, sub))) if sub else self._file(directory)
            return self._landed(found, spec)
        if any(name in members for _root, members in self.workspaces):
            if wanted is not _UNDECLARED:
                return Resolution(EXTERNAL, dotted_key(spec)), None
            return unresolved(f"{name} is a workspace package and {importer} is outside its workspace"), None
        return Resolution(EXTERNAL, dotted_key(spec)), None

    def _subpath_import(self, spec: str, manifest: str | None) -> tuple[Resolution, str | None]:
        """A ``#`` specifier, which only its own package's ``imports`` can resolve."""
        imports = ((self._read_json(manifest) or {}) if manifest else {}).get("imports")
        if not isinstance(imports, dict):
            return unresolved(f"{spec} is a subpath import and its package declares no imports"), None
        for pattern, target in imports.items():
            prefix, star, suffix = pattern.partition("*")
            if star and spec.startswith(prefix) and spec.endswith(suffix):
                matched = spec[len(prefix) : len(spec) - len(suffix)]
            elif spec == pattern:
                matched = ""
            else:
                continue
            if isinstance(target, dict):
                target = next((target[k] for k in ("types", "import", "node", "default") if isinstance(target.get(k), str)), None)
            if isinstance(target, str):
                found = self._file(_norm(posixpath.join(posixpath.dirname(manifest), target.replace("*", matched))))
                return self._landed(found, spec)
            return unresolved(f"{spec} maps to a target this does not read"), None
        return unresolved(f"{spec} matches nothing in its package's imports"), None
