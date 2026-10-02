"""Repository-local Git-compatible exclusions for Augur analysis."""

from __future__ import annotations

import logging
import os
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path

from pathspec import GitIgnoreSpec


AUGURIGNORE_FILENAME = ".augurignore"
_LOG = logging.getLogger(__name__)


class RepositoryIgnoreError(ValueError):
    """A repository's root .augurignore could not be safely applied."""


@dataclass(frozen=True)
class IgnoreStats:
    ignored_directories: int
    ignored_files: int


class RepositoryIgnorePolicy:
    """A compiled root .augurignore policy for one repository."""

    def __init__(self, repository_root: Path, spec: GitIgnoreSpec, pattern_count: int):
        self._repository_root = repository_root
        self._resolved_repository_root = repository_root.resolve()
        self._spec = spec
        self._pattern_count = pattern_count
        self._ignored_directories: set[str] = set()
        self._ignored_files: set[str] = set()
        self._custom_ignored_directories: set[str] = set()
        self._custom_ignored_files: set[str] = set()

    @classmethod
    def from_repository(
        cls, repository_root: str | os.PathLike[str]
    ) -> "RepositoryIgnorePolicy":
        root = Path(os.path.abspath(repository_root))
        ignore_file = root / AUGURIGNORE_FILENAME
        if not ignore_file.exists() and not ignore_file.is_symlink():
            _LOG.info("No root %s found", AUGURIGNORE_FILENAME)
            return cls(root, GitIgnoreSpec.from_lines(()), 0)

        try:
            lines = ignore_file.read_text(encoding="utf-8").splitlines()
            spec = GitIgnoreSpec.from_lines(lines)
        except (OSError, RuntimeError, UnicodeError, ValueError) as error:
            raise RepositoryIgnoreError(
                f"Could not load {AUGURIGNORE_FILENAME} for repository {root}"
            ) from error

        pattern_count = sum(
            bool(line.strip()) and not line.startswith("#") for line in lines
        )
        _LOG.info("Loaded %s: %s active patterns", AUGURIGNORE_FILENAME, pattern_count)
        return cls(root, spec, pattern_count)

    @property
    def active_pattern_count(self) -> int:
        return self._pattern_count

    @property
    def has_patterns(self) -> bool:
        return self._pattern_count > 0

    @property
    def stats(self) -> IgnoreStats:
        return IgnoreStats(len(self._ignored_directories), len(self._ignored_files))

    @property
    def has_custom_exclusions(self) -> bool:
        return bool(self._custom_ignored_directories or self._custom_ignored_files)

    def _relative_path(
        self, path: str | os.PathLike[str], *, is_directory: bool
    ) -> str:
        candidate = Path(str(path).replace("\\", "/"))
        if not candidate.is_absolute():
            candidate = self._repository_root / candidate
        candidate = Path(os.path.abspath(candidate))
        try:
            relative = candidate.relative_to(self._repository_root)
        except ValueError as error:
            raise RepositoryIgnoreError(
                f"Path is outside repository root: {candidate}"
            ) from error
        normalized = relative.as_posix()
        return f"{normalized}/" if is_directory and normalized else normalized

    def _validate_resolved_containment(self, path: str | os.PathLike[str]) -> None:
        try:
            resolved = Path(str(path).replace("\\", "/")).resolve()
            resolved.relative_to(self._resolved_repository_root)
        except (OSError, RuntimeError, ValueError) as error:
            raise RepositoryIgnoreError(
                f"Path is outside repository root: {path}"
            ) from error

    def _matches_directory_rule(self, relative_path: str) -> bool:
        decision = None
        candidate = f"{relative_path.rstrip('/')}/"
        for pattern in self._spec.patterns:
            source = pattern.pattern.lstrip("!")
            if source.endswith("/") and pattern.regex.match(candidate):
                decision = pattern.include
        return bool(decision)

    def _directory_is_ignored(self, relative_path: str) -> bool:
        candidate = relative_path.rstrip("/")
        return self._spec.match_file(candidate) or self._matches_directory_rule(candidate)

    def _matches_custom_rule(self, relative_path: str, *, is_directory: bool) -> bool:
        candidate = relative_path.rstrip("/")
        if (self._directory_is_ignored(candidate) if is_directory
                else self._spec.match_file(candidate)):
            return True

        parts = candidate.split("/") if candidate else []
        for index in range(1, len(parts)):
            if self._directory_is_ignored("/".join(parts[:index])):
                return True
        return False

    def is_ignored(
        self, path: str | os.PathLike[str], *, is_directory: bool = False
    ) -> bool:
        relative_path = self._relative_path(path, is_directory=is_directory)
        ignored = self._matches_custom_rule(
            relative_path, is_directory=is_directory
        )
        self._validate_resolved_containment(path)
        return ignored

    def _filter_names(
        self,
        parent_dir: str | os.PathLike[str],
        names: Iterable[str],
        *,
        is_directory: bool,
        built_in_names: Collection[str],
    ) -> list[str]:
        kept = []
        parent = Path(str(parent_dir).replace("\\", "/"))
        for name in names:
            candidate = parent / name
            custom_excluded = self.is_ignored(candidate, is_directory=is_directory)
            excluded = name in built_in_names or custom_excluded
            if not excluded:
                kept.append(name)
                continue

            relative_path = self._relative_path(candidate, is_directory=is_directory)
            relative_path = relative_path.rstrip("/")
            if is_directory:
                self._ignored_directories.add(relative_path)
                if custom_excluded:
                    self._custom_ignored_directories.add(relative_path)
                _LOG.debug("Ignoring directory: %s", relative_path)
            else:
                self._ignored_files.add(relative_path)
                if custom_excluded:
                    self._custom_ignored_files.add(relative_path)
                _LOG.debug("Ignoring file: %s", relative_path)
        return kept

    def filter_directories(
        self,
        parent_dir: str | os.PathLike[str],
        names: list[str],
        *,
        built_in_names: Collection[str] = (),
    ) -> list[str]:
        return self._filter_names(
            parent_dir,
            names,
            is_directory=True,
            built_in_names=built_in_names,
        )

    def filter_files(
        self,
        parent_dir: str | os.PathLike[str],
        names: Iterable[str],
        *,
        built_in_names: Collection[str] = (),
    ) -> list[str]:
        return self._filter_names(
            parent_dir,
            names,
            is_directory=False,
            built_in_names=built_in_names,
        )

    def log_summary(self) -> None:
        stats = self.stats
        _LOG.info(
            "Repository filtering: %s directories and %s files ignored",
            stats.ignored_directories,
            stats.ignored_files,
        )
