import json
import tempfile
import unittest
from pathlib import Path

from utils.code_metrics_tracker import CodeMetricsTracker
from utils.repository_ignore import RepositoryIgnorePolicy


class TestIgnoredSymlinkReporting(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def scan(self):
        policy = RepositoryIgnorePolicy.from_repository(self.repo)
        for _ in range(2):
            policy.filter_files(self.repo, [p.name for p in self.repo.iterdir() if not p.is_dir()])
            policy.filter_directories(self.repo, [p.name for p in self.repo.iterdir() if p.is_dir()])
        return policy

    def test_inventory_records_skipped_links_and_preserves_safe_file_ingestion(self):
        (self.repo / "real.py").write_text("print('safe')\n")
        (self.repo / "safe.py").symlink_to("real.py")
        (self.repo / "external.py").symlink_to(self.root)
        (self.repo / "broken.py").symlink_to("missing.py")
        (self.repo / "loop.py").symlink_to("loop.py")
        (self.repo / "directory").symlink_to(".", target_is_directory=True)
        policy = self.scan()

        self.assertEqual(policy.ignored_symlinks, [
            {"path": "broken.py", "reason": "symlink target does not exist"},
            {"path": "directory", "reason": "directory symlink was not traversed"},
            {"path": "external.py", "reason": "symlink target is outside repository root"},
            {"path": "loop.py", "reason": "symlink target cannot be resolved"},
        ])
        self.assertEqual(policy.filter_files(self.repo, ["safe.py"]), ["safe.py"])

    def test_inventory_includes_links_excluded_by_ignore_rules(self):
        (self.repo / ".augurignore").write_text("alias.py\n")
        (self.repo / "real.py").write_text("pass\n")
        (self.repo / "alias.py").symlink_to("real.py")
        self.assertEqual(self.scan().ignored_symlinks, [
            {"path": "alias.py", "reason": "excluded by .augurignore"},
        ])

    def test_inventory_includes_links_excluded_by_builtin_rules(self):
        (self.repo / "real.py").write_text("pass\n")
        (self.repo / "alias.py").symlink_to("real.py")
        policy = RepositoryIgnorePolicy.from_repository(self.repo)
        self.assertEqual(policy.filter_files(self.repo, ["alias.py"], built_in_names={"alias.py"}), [])
        self.assertEqual(policy.ignored_symlinks, [
            {"path": "alias.py", "reason": "excluded by built-in rules"},
        ])

    def test_directory_reason_is_stable_across_language_scan_order(self):
        (self.repo / "build").symlink_to(".", target_is_directory=True)
        for exclusions in (({"build"}, set()), (set(), {"build"})):
            policy = RepositoryIgnorePolicy.from_repository(self.repo)
            for built_in_names in exclusions:
                policy.filter_directories(self.repo, ["build"], built_in_names=built_in_names)
            self.assertEqual(policy.ignored_symlinks, [
                {"path": "build", "reason": "excluded by built-in rules"},
            ])

    def test_inventory_survives_artifact_roundtrip_and_multi_repository_merge(self):
        (self.repo / "broken.py").symlink_to("missing.py")
        first = CodeMetricsTracker(git_slug="first_main", git_repo="https://example.com/first")
        first.record_ignored_symlinks(self.scan())
        second = CodeMetricsTracker(git_slug="second_main", git_repo="https://example.com/second")
        second.record_ignored_symlinks(self.scan())
        combined = CodeMetricsTracker()
        for index, tracker in enumerate((first, second, first)):
            artifact = self.root / f"metrics-{index}.json"
            tracker.save_to_file(str(artifact))
            combined.load_and_merge(str(artifact))
        report = combined.format_markdown_section()
        self.assertIn("### Ignored symlink locations", report)
        self.assertIn("https://example.com/first", report)
        self.assertIn("https://example.com/second", report)
        self.assertEqual(report.count("broken.py"), 2)
        self.assertIn("symlink target does not exist", report)

    def test_merging_scan_snapshots_lists_each_location_once_with_stable_reason(self):
        snapshots = [
            {"path": "build", "reason": "directory symlink was not traversed"},
            {"path": "build", "reason": "excluded by built-in rules"},
        ]
        for order in (snapshots, list(reversed(snapshots))):
            combined = CodeMetricsTracker()
            for index, entry in enumerate(order):
                artifact = self.root / f"snapshot-{index}.json"
                artifact.write_text(json.dumps({"symlink_inventories": {
                    "repo_main": {"git_repo": "org/repo", "locations": [entry]},
                }}))
                combined.load_and_merge(str(artifact))
            report = combined.format_markdown_section()
            self.assertEqual(report.count("| build |"), 1)
            self.assertIn("excluded by built-in rules", report)

    def test_empty_inventory_is_distinct_from_legacy_artifact(self):
        fresh = CodeMetricsTracker(git_slug="fresh_main")
        fresh.record_ignored_symlinks(self.scan())
        fresh = CodeMetricsTracker.from_dict(fresh.to_dict())
        legacy = CodeMetricsTracker.from_dict({"git_slug": "old_main"})
        self.assertIn("None encountered", fresh.format_markdown_section())
        self.assertIn("not recorded", legacy.format_markdown_section())
        self.assertNotIn("None encountered", legacy.format_markdown_section())

    def test_merging_legacy_artifact_does_not_erase_recorded_inventory(self):
        tracker = CodeMetricsTracker(git_slug="repo_main")
        tracker.record_ignored_symlinks(self.scan())
        artifact = self.root / "old.json"
        artifact.write_text(json.dumps({"git_slug": "repo_main"}))
        tracker.load_and_merge(str(artifact))
        self.assertIn("None encountered", tracker.format_markdown_section())
        self.assertNotIn("not recorded", tracker.format_markdown_section())

    def test_mixed_repository_availability_survives_aggregate_roundtrip(self):
        tracker = CodeMetricsTracker(git_slug="fresh_main")
        tracker.record_ignored_symlinks(self.scan())
        artifact = self.root / "old.json"
        artifact.write_text(json.dumps({"git_slug": "old_main"}))
        tracker.load_and_merge(str(artifact))
        report = CodeMetricsTracker.from_dict(tracker.to_dict()).format_markdown_section()
        self.assertIn("fresh_main", report)
        self.assertIn("old_main", report)
        self.assertIn("None encountered", report)
        self.assertIn("not recorded", report)

    def test_inventory_updates_preserve_existing_positive_metrics_when_measurement_is_empty(self):
        artifact = self.root / "project_metrics.json"
        artifact.write_text(json.dumps({
            "git_slug": "repo_main", "total_repo_files": 12, "total_repo_lines": 300,
        }))
        (self.repo / "broken.py").symlink_to("missing.py")
        tracker = CodeMetricsTracker(git_slug="repo_main")
        tracker.record_ignored_symlinks(self.scan())
        tracker.save_to_file(str(artifact))
        restored = CodeMetricsTracker.load_from_file(str(artifact))
        self.assertEqual(restored.total_repo_files, 12)
        self.assertEqual(restored.total_repo_lines, 300)
        self.assertIn("broken.py", restored.format_markdown_section())

    def test_report_escapes_repository_controlled_paths(self):
        (self.repo / "bad|<tag>`\n.py").symlink_to("missing")
        tracker = CodeMetricsTracker(git_slug="repo_main")
        tracker.record_ignored_symlinks(self.scan())
        report = tracker.format_markdown_section()
        self.assertNotIn("bad|<tag>", report)
        self.assertIn("bad&#124;&lt;tag&gt;", report)


if __name__ == "__main__":
    unittest.main()
