import os
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

from utils.repository_ignore import (
    AUGURIGNORE_FILENAME,
    IgnoreStats,
    RepositoryIgnoreError,
    RepositoryIgnorePolicy,
)


def install_windows_symlink_fallback(test_case: unittest.TestCase) -> None:
    """Simulate POSIX symlinks in tests when Windows lacks symlink privileges (WinError 1314)."""
    if os.name != "nt":
        return

    orig_symlink_to = Path.symlink_to
    orig_is_symlink = Path.is_symlink
    orig_resolve = Path.resolve
    orig_islink = os.path.islink
    orig_isdir = os.path.isdir
    orig_isfile = os.path.isfile
    symlinks: dict[str, tuple[Path, bool]] = {}

    def norm_path(path) -> str:
        return os.path.abspath(os.fspath(path))

    def follow_link(path_str: str, strict: bool = False) -> Path:
        seen: set[str] = set()
        curr = path_str
        while curr in symlinks:
            if curr in seen:
                raise RuntimeError(f"Symlink loop: {path_str}")
            seen.add(curr)
            raw_target, _ = symlinks[curr]
            target_path = Path(raw_target)
            if not target_path.is_absolute():
                target_path = Path(curr).parent / target_path
            curr = norm_path(target_path)
        return orig_resolve(Path(curr), strict=strict)

    def symlink_to(self_path: Path, target, target_is_directory: bool = False):
        try:
            return orig_symlink_to(self_path, target, target_is_directory=target_is_directory)
        except OSError as exc:
            if getattr(exc, "winerror", None) != 1314:
                raise
            norm = norm_path(self_path)
            symlinks[norm] = (Path(target), target_is_directory)
            if target_is_directory:
                self_path.mkdir(exist_ok=True)
            elif self_path.name != AUGURIGNORE_FILENAME:
                try:
                    resolved = follow_link(norm, strict=True)
                    content = resolved.read_bytes() if orig_isfile(resolved) else b""
                except (OSError, RuntimeError):
                    content = b""
                self_path.write_bytes(content)

    def is_symlink(self_path: Path) -> bool:
        return norm_path(self_path) in symlinks or orig_is_symlink(self_path)

    def resolve(self_path: Path, strict: bool = False) -> Path:
        norm = norm_path(self_path)
        if norm in symlinks:
            return follow_link(norm, strict=strict)
        return orig_resolve(self_path, strict=strict)

    def islink(path) -> bool:
        return norm_path(path) in symlinks or orig_islink(path)

    def isdir(path) -> bool:
        norm = norm_path(path)
        if norm in symlinks:
            try:
                return orig_isdir(follow_link(norm, strict=True))
            except (OSError, RuntimeError):
                return False
        return orig_isdir(path)

    def isfile(path) -> bool:
        norm = norm_path(path)
        if norm in symlinks:
            try:
                return orig_isfile(follow_link(norm, strict=True))
            except (OSError, RuntimeError):
                return False
        return orig_isfile(path)

    test_case.enterContext(patch.object(Path, "symlink_to", symlink_to))
    test_case.enterContext(patch.object(Path, "is_symlink", is_symlink))
    test_case.enterContext(patch.object(Path, "resolve", resolve))
    test_case.enterContext(patch("os.path.islink", side_effect=islink))
    test_case.enterContext(patch("os.path.isdir", side_effect=isdir))
    test_case.enterContext(patch("os.path.isfile", side_effect=isfile))


class TestRepositoryIgnorePolicyLoading(unittest.TestCase):
    def setUp(self):
        install_windows_symlink_fallback(self)

    def make_repository(self, contents=None):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        if contents is not None:
            (root / AUGURIGNORE_FILENAME).write_text(contents, encoding="utf-8")
        self.addCleanup(directory.cleanup)
        return root

    def test_missing_file_creates_empty_policy(self):
        policy = RepositoryIgnorePolicy.from_repository(self.make_repository())

        self.assertEqual(AUGURIGNORE_FILENAME, ".augurignore")
        self.assertEqual(policy.active_pattern_count, 0)
        self.assertFalse(policy.has_patterns)

    def test_empty_and_comment_only_files_have_zero_active_patterns(self):
        for contents in ("", "# comment\n\n# also a comment\n"):
            with self.subTest(contents=contents):
                policy = RepositoryIgnorePolicy.from_repository(
                    self.make_repository(contents)
                )
                self.assertEqual(policy.active_pattern_count, 0)
                self.assertFalse(policy.has_patterns)

    def test_only_root_augurignore_is_loaded(self):
        root = self.make_repository("*.secret\n")
        (root / "nested").mkdir()
        (root / "nested" / AUGURIGNORE_FILENAME).write_text(
            "*.py\n", encoding="utf-8"
        )

        policy = RepositoryIgnorePolicy.from_repository(root)

        self.assertTrue(policy.is_ignored(root / "token.secret"))
        self.assertFalse(policy.is_ignored(root / "nested" / "app.py"))

    def test_crlf_unicode_and_missing_final_newline_are_accepted(self):
        root = self.make_repository("café.txt\r\nlogs/")
        policy = RepositoryIgnorePolicy.from_repository(root)

        self.assertEqual(policy.active_pattern_count, 2)
        self.assertTrue(policy.is_ignored(root / "café.txt"))
        self.assertTrue(policy.is_ignored(root / "logs", is_directory=True))

    def test_invalid_utf8_raises_repository_ignore_error(self):
        root = self.make_repository()
        (root / AUGURIGNORE_FILENAME).write_bytes(b"\xff\xfe")

        with self.assertRaisesRegex(RepositoryIgnoreError, r"\.augurignore"):
            RepositoryIgnorePolicy.from_repository(root)

    def test_broken_root_augurignore_symlink_raises_repository_ignore_error(self):
        root = self.make_repository()
        ignore_file = root / AUGURIGNORE_FILENAME
        try:
            ignore_file.symlink_to(root / "missing-rules")
        except OSError as exc:
            if getattr(exc, "winerror", None) != 1314:
                raise
            orig_is_symlink = Path.is_symlink
            self.enterContext(
                patch.object(
                    Path,
                    "is_symlink",
                    autospec=True,
                    side_effect=lambda p: True
                    if p == ignore_file
                    else orig_is_symlink(p),
                )
            )

        with self.assertRaisesRegex(RepositoryIgnoreError, r"\.augurignore"):
            RepositoryIgnorePolicy.from_repository(root)

    def test_matcher_compile_error_names_file_without_dumping_contents(self):
        root = self.make_repository("private-pattern\n")

        with patch(
            "utils.repository_ignore.GitIgnoreSpec.from_lines",
            side_effect=ValueError("not valid"),
        ):
            with self.assertRaises(RepositoryIgnoreError) as raised:
                RepositoryIgnorePolicy.from_repository(root)

        self.assertIn(AUGURIGNORE_FILENAME, str(raised.exception))
        self.assertNotIn("private-pattern", str(raised.exception))

    def test_loading_logs_missing_file_or_active_pattern_count(self):
        with self.assertLogs(level="INFO") as missing_logs:
            RepositoryIgnorePolicy.from_repository(self.make_repository())
        self.assertIn("No root .augurignore found", "\n".join(missing_logs.output))

        with self.assertLogs(level="INFO") as loaded_logs:
            RepositoryIgnorePolicy.from_repository(self.make_repository("*.log\n"))
        self.assertIn("1 active patterns", "\n".join(loaded_logs.output))

    def test_loading_logs_active_patterns_in_file_order(self):
        root = self.make_repository(
            "# generated files\n*.log\n\ncache/\n!important.log\n"
        )

        with self.assertLogs(level="INFO") as loaded_logs:
            RepositoryIgnorePolicy.from_repository(root)

        logs = "\n".join(loaded_logs.output)
        self.assertIn(
            "Active .augurignore patterns: *.log, cache/, !important.log",
            logs,
        )
        self.assertNotIn("generated files", logs)


class TestRepositoryIgnorePolicyMatching(unittest.TestCase):
    def setUp(self):
        install_windows_symlink_fallback(self)

    def make_policy(self, patterns):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        (root / AUGURIGNORE_FILENAME).write_text(patterns, encoding="utf-8")
        self.addCleanup(directory.cleanup)
        return root, RepositoryIgnorePolicy.from_repository(root)

    def test_git_pattern_forms_match_files_and_directories(self):
        root, policy = self.make_policy(
            "# comment\n\\#literal\n\\!literal\nname\\ \n*.log\nfile?.py\n[ab].txt\n**/generated/**\n/root.txt\ncache/\n"
        )

        self.assertTrue(policy.is_ignored(root / "#literal"))
        self.assertTrue(policy.is_ignored(root / "!literal"))
        self.assertTrue(policy.is_ignored(root / "name "))
        self.assertTrue(policy.is_ignored(root / "debug.log"))
        self.assertTrue(policy.is_ignored(root / "file1.py"))
        self.assertTrue(policy.is_ignored(root / "a.txt"))
        self.assertTrue(policy.is_ignored(root / "src" / "generated" / "item.py"))
        self.assertTrue(policy.is_ignored(root / "root.txt"))
        self.assertFalse(policy.is_ignored(root / "nested" / "root.txt"))
        self.assertTrue(policy.is_ignored(root / "cache", is_directory=True))

    def test_last_match_negation_and_excluded_parent_constraint(self):
        root, policy = self.make_policy("*.log\n!important.log\nvendor/\n!vendor/keep.py\n")

        self.assertTrue(policy.is_ignored(root / "debug.log"))
        self.assertFalse(policy.is_ignored(root / "important.log"))
        self.assertTrue(policy.is_ignored(root / "vendor", is_directory=True))
        self.assertTrue(policy.is_ignored(root / "vendor" / "keep.py"))

    def test_contents_only_rule_does_not_prune_parent_directory(self):
        root, policy = self.make_policy("foo/**\n!foo/keep.py\n")

        self.assertFalse(policy.is_ignored(root / "foo", is_directory=True))
        self.assertFalse(policy.is_ignored(root / "foo" / "keep.py"))

    def test_lexical_symlink_path_is_matched_and_unsafe_targets_fail_closed(self):
        root, policy = self.make_policy("alias.py\n")
        (root / "real.py").write_text("", encoding="utf-8")
        alias_path = root / "alias.py"
        outside_path = root / "outside.py"
        loop_path = root / "loop.py"
        try:
            alias_path.symlink_to(root / "real.py")
            outside_path.symlink_to(root.parent / "outside.py")
            loop_path.symlink_to("loop.py")
        except OSError as exc:
            if getattr(exc, "winerror", None) != 1314:
                raise
            orig_resolve = Path.resolve

            def _fake_resolve(path_obj, *args, **kwargs):
                norm = os.path.abspath(path_obj)
                if norm == os.path.abspath(alias_path):
                    return orig_resolve(root / "real.py", *args, **kwargs)
                if norm == os.path.abspath(outside_path):
                    return orig_resolve(
                        root.parent / "outside.py", *args, **kwargs
                    )
                if norm == os.path.abspath(loop_path):
                    raise RuntimeError("Symlink loop")
                return orig_resolve(path_obj, *args, **kwargs)

            self.enterContext(
                patch.object(
                    Path, "resolve", autospec=True, side_effect=_fake_resolve
                )
            )

        self.assertTrue(policy.is_ignored(alias_path))
        with self.assertRaises(RepositoryIgnoreError):
            policy.is_ignored(outside_path)
        with self.assertRaises(RepositoryIgnoreError):
            policy.is_ignored(loop_path)

    def test_filtering_combines_builtin_exclusions_and_counts_unique_paths(self):
        root, policy = self.make_policy("cache/\n*.log\n!vendor/keep.py\n")

        self.assertEqual(
            policy.filter_directories(root, ["vendor", "cache", "src"], built_in_names={"vendor"}),
            ["src"],
        )
        self.assertEqual(
            policy.filter_files(root, [".augurignore", "debug.log", "app.py"], built_in_names={".augurignore"}),
            ["app.py"],
        )
        self.assertEqual(policy.stats, IgnoreStats(ignored_directories=2, ignored_files=2))
        policy.filter_directories(root, ["vendor", "cache", "src"], built_in_names={"vendor"})
        policy.filter_files(root, [".augurignore", "debug.log", "app.py"], built_in_names={".augurignore"})
        self.assertEqual(policy.stats, IgnoreStats(ignored_directories=2, ignored_files=2))

    def test_unsafe_links_are_skipped_once_and_are_not_custom_matches(self):
        for patterns in (None, "*.py\nunsafe-dir/\n"):
            with self.subTest(patterns=patterns), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "repo"
                root.mkdir()
                if patterns is not None:
                    (root / AUGURIGNORE_FILENAME).write_text(patterns, encoding="utf-8")
                outside = Path(directory) / "outside.py"
                outside.write_text("secret", encoding="utf-8")
                (root / "unsafe.py").symlink_to(outside)
                (root / "broken.py").symlink_to("missing.py")
                (root / "loop.py").symlink_to("loop.py")
                (root / "unsafe-dir").symlink_to(root.parent, target_is_directory=True)
                (root / "safe.txt").write_text("safe", encoding="utf-8")
                policy = RepositoryIgnorePolicy.from_repository(root)

                with self.assertLogs("utils.repository_ignore", level="WARNING") as logs:
                    for _ in range(2):
                        self.assertEqual(policy.filter_files(root, ["unsafe.py", "broken.py", "loop.py", "safe.txt"]), ["safe.txt"])
                        self.assertEqual(policy.filter_directories(root, ["unsafe-dir"]), [])

                self.assertEqual(len(logs.output), 4)
                self.assertIn("Skipping unsafe.py: symlink target is outside repository root", "\n".join(logs.output))
                self.assertIn("broken.py", "\n".join(logs.output))
                self.assertIn("loop.py", "\n".join(logs.output))
                self.assertEqual(policy.stats, IgnoreStats(ignored_directories=1, ignored_files=3))
                self.assertFalse(policy.has_custom_exclusions)

    def test_internal_links_keep_lexical_ignore_matching(self):
        root, policy = self.make_policy("alias.py\nignored-dir/\n")
        (root / "real.py").write_text("safe", encoding="utf-8")
        (root / "alias.py").symlink_to("real.py")
        (root / "kept.py").symlink_to("real.py")
        (root / "src").mkdir()
        (root / "ignored-dir").symlink_to("src", target_is_directory=True)
        (root / "kept-dir").symlink_to("src", target_is_directory=True)

        self.assertEqual(policy.filter_files(root, ["alias.py", "kept.py"]), ["kept.py"])
        self.assertEqual(policy.filter_directories(root, ["ignored-dir", "kept-dir"]), ["kept-dir"])
        self.assertTrue(policy.has_custom_exclusions)

    @unittest.skipIf(os.name == "nt", "Backslash is a separator on Windows")
    def test_literal_backslash_symlink_names_are_checked_as_filesystem_entries(self):
        root, policy = self.make_policy("")
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "secret.py"
            outside.write_text("secret", encoding="utf-8")
            names = ["unsafe\\alias.py", "broken\\alias.py", "loop\\alias.py"]
            (root / names[0]).symlink_to(outside)
            (root / names[1]).symlink_to("missing.py")
            (root / names[2]).symlink_to(names[2])

            with self.assertLogs("utils.repository_ignore", level="WARNING") as logs:
                self.assertEqual(policy.filter_files(root, names), [])
            self.assertEqual(len(logs.output), 3)
            self.assertEqual(policy.stats.ignored_files, 3)

    def test_filters_still_reject_ordinary_paths_outside_repository(self):
        root, policy = self.make_policy("")
        for method in (policy.filter_files, policy.filter_directories):
            with self.subTest(method=method.__name__), self.assertRaises(RepositoryIgnoreError):
                method(root.parent, ["outside"])

    def test_normalizes_platform_separators_and_rejects_outside_paths(self):
        root, policy = self.make_policy("cache/**\n")
        windows_like = str(root / "cache" / "entry.txt").replace("/", "\\")

        self.assertTrue(policy.is_ignored(windows_like))
        with self.assertRaisesRegex(RepositoryIgnoreError, "outside repository root"):
            policy.is_ignored(root.parent / "elsewhere.txt")

    def test_individual_ignored_paths_are_debug_only(self):
        root, policy = self.make_policy("secret.txt\n")

        with self.assertLogs(level="INFO") as normal_logs:
            policy.filter_files(root, ["secret.txt"])
            policy.log_summary()
        self.assertNotIn("secret.txt", "\n".join(normal_logs.output))
        with self.assertLogs(level="DEBUG") as debug_logs:
            policy.filter_files(root, ["secret.txt"])
        self.assertIn("secret.txt", "\n".join(debug_logs.output))


class TestRepositoryIgnoreGitConformance(unittest.TestCase):
    def test_representative_patterns_match_git_check_ignore(self):
        patterns = "/root.txt\n**/generated/**\ncache/\n*.log\n!important.log\nvendor/\n!vendor/keep.py\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            paths = [
                "root.txt",
                "nested/root.txt",
                "src/generated/item.py",
                "cache/item.txt",
                "debug.log",
                "important.log",
                "vendor/keep.py",
            ]
            for relative_path in paths:
                absolute_path = root / relative_path
                absolute_path.parent.mkdir(parents=True, exist_ok=True)
                absolute_path.write_text("fixture", encoding="utf-8")
            (root / AUGURIGNORE_FILENAME).write_text(patterns, encoding="utf-8")
            (root / ".gitignore").write_text(patterns, encoding="utf-8")

            check_ignore = subprocess.run(
                ["git", "check-ignore", "--no-index", "-z", "--stdin"],
                cwd=root,
                input="\0".join(paths) + "\0",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertIn(check_ignore.returncode, (0, 1), check_ignore.stderr)
            git_ignored = set(filter(None, check_ignore.stdout.split("\0")))
            policy = RepositoryIgnorePolicy.from_repository(root)

            for relative_path in paths:
                with self.subTest(path=relative_path):
                    self.assertEqual(
                        policy.is_ignored(root / relative_path),
                        relative_path in git_ignored,
                    )
