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


class TestRepositoryIgnorePolicyLoading(unittest.TestCase):
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
        (root / AUGURIGNORE_FILENAME).symlink_to(root / "missing-rules")

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
        (root / "alias.py").symlink_to(root / "real.py")
        (root / "outside.py").symlink_to(root.parent / "outside.py")
        (root / "loop.py").symlink_to("loop.py")

        self.assertTrue(policy.is_ignored(root / "alias.py"))
        with self.assertRaises(RepositoryIgnoreError):
            policy.is_ignored(root / "outside.py")
        with self.assertRaises(RepositoryIgnoreError):
            policy.is_ignored(root / "loop.py")

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
