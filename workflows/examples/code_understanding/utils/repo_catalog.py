"""Roll the per-file metadata of every listed repository into one catalog.

Single-multi-repo mode indexes one focal repository with a compact summary of
its siblings. Everything here is read from files data generation already wrote
(``_metadata.txt``, ``provenance.json``, document headers), so the catalog
costs no model calls.
"""

import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

CATALOG_FILENAME = "repo_catalog.json"
SCAN_MODE = "single-multi-repo"

_SCALAR_FIELDS = ("file_path", "language", "package", "purpose")
_LIST_KEY = re.compile(r"^(imports|classes)_(\d+)$")
_HEADER_MARK = "This file is located at"


def _slug(item: dict) -> str:
    from utils.code_utils import generate_slug_from_repo

    return generate_slug_from_repo(item["git_repo"], item.get("git_branch") or "main")


def require_focal_in_list(git_repo: str, git_branch: str, git_repos: list[dict]) -> str:
    """Return the focal repository's slug, or raise if it is not one of ``git_repos``."""
    from utils.code_utils import generate_slug_from_repo

    if not (git_repo or "").strip():
        raise ValueError("GIT_REPO must name the focal repository for single-multi-repo mode")
    focal = generate_slug_from_repo(git_repo, git_branch)
    listed = [_slug(item) for item in git_repos]
    if focal not in listed:
        raise ValueError(f"Focal repository {focal} is not in the repository list: {', '.join(listed)}")
    return focal


def read_flat_metadata(path: str) -> dict | None:
    """Parse one flattened ``_metadata.txt``, regrouping ``imports_N``/``classes_N`` into lists."""
    import yaml

    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        logger.warning("Skipping unreadable metadata %s: %s", path, exc)
        return None
    if not isinstance(data, dict) or not data.get("file_path"):
        logger.warning("Skipping metadata without a file_path: %s", path)
        return None

    meta = {key: data[key] for key in _SCALAR_FIELDS if data.get(key) not in (None, "")}
    lists: dict[str, list[tuple[int, object]]] = {}
    for key, value in data.items():
        match = _LIST_KEY.match(str(key))
        if match and value not in (None, ""):
            lists.setdefault(match.group(1), []).append((int(match.group(2)), value))
    for name in ("imports", "classes"):
        if name in lists:
            meta[name] = [value for _, value in sorted(lists[name], key=lambda pair: pair[0])]
    return meta


def _header_runtime(path: Path) -> list[str]:
    """Runtime entries from a generated document's header line, if it has one."""
    from provenance.corpus import parse_header

    try:
        with open(path, encoding="utf-8") as handle:
            head = [handle.readline() for _ in range(3)]
    except (OSError, UnicodeDecodeError):
        return []
    for line in head:
        if _HEADER_MARK in line:
            parsed = parse_header(line)
            return parsed.runtime.split(" / ") if parsed and parsed.runtime else []
    return []


def _load_facts(repo_dir: Path):
    from provenance.facts import read
    from provenance.pipeline import FACTS_FILENAME

    path = repo_dir / FACTS_FILENAME
    try:
        return read(path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("Skipping provenance facts %s: %s", path, exc)
        return None


def _repo_entry(parent_target_path: str, item: dict) -> dict:
    slug = _slug(item)
    repo_dir = Path(parent_target_path) / slug
    if not repo_dir.is_dir():
        raise RuntimeError(f"Repository {item['git_repo']} has no generated data at {repo_dir}")

    files = sorted(Path(dirpath) / name
                   for dirpath, _, names in os.walk(repo_dir) for name in names)
    metadata = []
    for path in files:
        if path.name.endswith("_metadata.txt"):
            meta = read_flat_metadata(str(path))
            if meta is not None:
                metadata.append(meta)

    facts = _load_facts(repo_dir)
    if facts and facts.runtime:
        runtime = facts.runtime.split(" / ")
    else:
        runtime = [part for path in files
                   if path.suffix == ".txt" and not path.name.endswith("_metadata.txt")
                   for part in _header_runtime(path)]
    runtime_stack = list(dict.fromkeys(part.strip() for part in runtime if part.strip()))

    libraries = sorted({(p.name, p.version) for p in (facts.packages if facts else []) if p.name},
                       key=lambda pair: (pair[0], pair[1] or ""))

    packages: dict[str, list[dict]] = {}
    for meta in metadata:
        if meta.get("package"):
            packages.setdefault(meta["package"], []).append(
                {key: meta[key] for key in ("file_path", "purpose", "classes") if key in meta})

    entry = {"git_repo": item["git_repo"], "git_branch": item.get("git_branch") or "main", "git_slug": slug}
    optional = {
        "languages": sorted({meta["language"] for meta in metadata if "language" in meta}),
        "runtime_stack": runtime_stack,
        "libraries": [{"name": name, "version": version} if version else {"name": name}
                      for name, version in libraries],
        "packages": [{"package": name, "files": packages[name]} for name in sorted(packages)],
    }
    entry.update({key: value for key, value in optional.items() if value})
    return entry


def build_repo_catalog(parent_target_path: str, git_repos: list[dict]) -> dict:
    """Catalog of every listed repository, built from its generated data under ``parent_target_path``."""
    return {"repos": [_repo_entry(parent_target_path, item) for item in git_repos]}


def save_repo_catalog(catalog: dict, parent_target_path: str) -> str:
    """Write ``<parent_target_path>/repo_catalog.json``, log it, and return its path."""
    path = os.path.join(parent_target_path, CATALOG_FILENAME)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(catalog, handle, indent=2, sort_keys=True)
    # The deep pass reads the file on disk; logging it is best effort.
    try:
        from loaders.default_asset_loader import DefaultAssetLoader

        DefaultAssetLoader().log_results(
            path,
            artifact_path=DefaultAssetLoader.get_log_results_artifact_path(
                DefaultAssetLoader.RESULTS_PATH_PREFIX_METADATA, git_slug=None, multi_repo=True),
            tags={"category": "data-generation", "multi_repo": "True", "repo-catalog": "true"})
    except Exception as exc:
        logger.warning("Repository catalog %s not logged: %s", path, exc)
    return path


_SEPARATORS = (".", "/", "::")
_SEGMENT = re.compile(r"\.|/|::")


def _purpose(import_name: str, files: list[dict]) -> str:
    """The named file's purpose, else the first three files' purposes; one line, at most 200 chars."""
    files = sorted(files, key=lambda f: f["file_path"])
    last = _SEGMENT.split(import_name)[-1]
    named = [f for f in files if os.path.splitext(os.path.basename(f["file_path"]))[0] == last]
    chosen = named if len(named) == 1 else files[:3]
    return " ".join("; ".join(f["purpose"] for f in chosen if f.get("purpose")).split())[:200]


def resolve_import(import_name: str, catalog: dict, focal_slug: str) -> list[dict]:
    """Sibling packages providing ``import_name``: the longest matching package, one match per repository."""
    if not import_name or import_name.startswith("."):
        return []
    candidates = [(repo, package) for repo in catalog["repos"] if repo["git_slug"] != focal_slug
                  for package in repo.get("packages", [])
                  if import_name == package["package"]
                  or any(import_name.startswith(package["package"] + sep) for sep in _SEPARATORS)]
    longest = max((len(package["package"]) for _, package in candidates), default=0)
    return sorted(({"git_repo": repo["git_repo"], "git_slug": repo["git_slug"], "package": package["package"],
                    "purpose": _purpose(import_name, package["files"]),
                    "runtime": " / ".join(repo.get("runtime_stack", []))}
                   for repo, package in candidates if len(package["package"]) == longest),
                  key=lambda match: match["git_slug"])


def format_annotation(matches: list[dict]) -> str:
    """``(provided by repository <url>, slug <slug>; purpose: ...; runtime: ...)`` per match, space-separated."""
    return " ".join(
        "(" + "; ".join([f"provided by repository {m['git_repo']}, slug {m['git_slug']}"]
                        + [f"{key}: {m[key]}" for key in ("purpose", "runtime") if m[key]]) + ")"
        for m in matches)


_FLAG = re.compile(r"multi_repo (?:true|false)")
_IMPORT = "- [import] "


def _annotate_code_file(path: Path, annotations: dict[str, str]) -> None:
    """Force the header's flag to false and annotate resolved import lines; every other byte is kept."""
    with open(path, encoding="utf-8", newline="") as handle:
        lines = handle.read().split("\n")
    header = next((i for i, line in enumerate(lines[:3]) if _HEADER_MARK in line), None)
    if header is not None and _FLAG.search(lines[header]):
        lines[header] = _FLAG.sub("multi_repo false", lines[header], count=1)
    else:
        logger.warning("No multi_repo flag in the header of %s; left as is", path)
    start = next((i + 1 for i, line in enumerate(lines) if line.strip() == "Dependencies:"), len(lines))
    for i in range(start, len(lines)):
        if not lines[i].startswith("- ["):
            break
        body = lines[i].removesuffix("\r")
        name = body.removeprefix(_IMPORT)
        if name != body and name in annotations:
            lines[i] = f"{body} {annotations[name]}{lines[i][len(body):]}"
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("\n".join(lines))


def annotate_focal_repo(source_dir: str, dest_dir: str, catalog: dict, focal_slug: str) -> dict:
    """Copy the focal repository's generated data to ``dest_dir``, marked single-repo, with sibling imports annotated."""
    import shutil

    import yaml

    import utils.json_utils  # noqa: F401  registers the literal-block str representer the metadata was written with

    source, dest = Path(source_dir), Path(dest_dir)
    if not any(source.rglob("*.txt")):
        raise RuntimeError(f"Focal repository has no generated data at {source_dir}")
    if source.resolve().is_relative_to(dest.resolve()) or dest.resolve().is_relative_to(source.resolve()):
        raise ValueError(f"dest_dir {dest_dir} overlaps source_dir {source_dir}")
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)

    counts = {"files": 0, "resolved_imports": 0, "unresolved_imports": 0}
    for meta_path in sorted(dest.rglob("*_metadata.txt")):
        meta = read_flat_metadata(str(meta_path))
        if meta is None:
            continue
        imports = set(meta.get("imports", []))
        annotations = {name: text for name in imports
                       if (text := format_annotation(resolve_import(name, catalog, focal_slug)))}
        code_path = meta_path.with_name(meta_path.name.removesuffix("_metadata.txt") + ".txt")
        if code_path.is_file():
            _annotate_code_file(code_path, annotations)

        data = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        data["multi_repo"] = False
        for key, value in data.items():
            if re.fullmatch(r"imports_\d+", str(key)) and value in annotations:
                data[key] = f"{value} {annotations[value]}"
        meta_path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True), encoding="utf-8")

        counts["files"] += 1
        counts["resolved_imports"] += len(annotations)
        counts["unresolved_imports"] += len(imports) - len(annotations)
    return counts
