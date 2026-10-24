import json
import os
import tempfile
import unittest
from pathlib import Path

from utils.code_metrics_tracker import CodeMetricsTracker
from utils.repository_ignore import RepositoryIgnorePolicy


class TestIgnoredLocations(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def scan(self):
        policy = RepositoryIgnorePolicy.from_repository(self.repo)
        for _ in range(2):
            for root, dirs, files in os.walk(self.repo):
                dirs[:] = policy.filter_directories(root, dirs)
                policy.filter_files(root, files)
        return policy

    def test_scan_records_skipped_links_and_augurignore_exclusions_once(self):
        (self.repo / ".augurignore").write_text("private/\nhidden.py\nalias.py\n")
        (self.repo / "private").mkdir()
        (self.repo / "private" / "unseen.py").write_text("secret")
        (self.repo / "hidden.py").write_text("secret")
        (self.repo / "real.py").write_text("pass\n")
        (self.repo / "safe.py").symlink_to("real.py")
        (self.repo / "alias.py").symlink_to("real.py")
        (self.repo / "external.py").symlink_to(self.root)
        (self.repo / "broken.py").symlink_to("missing.py")
        (self.repo / "loop.py").symlink_to("loop.py")
        (self.repo / "directory").symlink_to(".", target_is_directory=True)
        policy = self.scan()
        self.assertEqual(policy.ignored_locations, {
            "alias.py": "excluded by .augurignore",
            "broken.py": "symlink target does not exist",
            "directory": "directory symlink was not traversed",
            "external.py": "symlink target is outside repository root",
            "hidden.py": "excluded by .augurignore",
            "loop.py": "symlink target cannot be resolved",
            "private": "excluded by .augurignore",
        })
        self.assertEqual(policy.filter_files(self.repo, ["safe.py"]), ["safe.py"])

    def test_directory_reason_does_not_depend_on_language_exclusions(self):
        (self.repo / "build").symlink_to(".", target_is_directory=True)
        for exclusions in (({"build"}, set()), (set(), {"build"})):
            policy = RepositoryIgnorePolicy.from_repository(self.repo)
            for built_in_names in exclusions:
                policy.filter_directories(self.repo, ["build"], built_in_names=built_in_names)
            self.assertEqual(policy.ignored_locations, {"build": "directory symlink was not traversed"})

    def test_artifact_merge_preserves_repositories_and_deduplicates_paths(self):
        (self.repo / ".augurignore").write_text("hidden.py\n")
        (self.repo / "hidden.py").write_text("secret")
        tracker = CodeMetricsTracker(git_slug="first_main", git_repo="https://example.com/first")
        tracker.record_ignored_locations(self.scan())
        combined = CodeMetricsTracker()
        for index in range(2):
            artifact = self.root / f"metrics-{index}.json"
            tracker.save_to_file(str(artifact))
            combined.load_and_merge(str(artifact))
        (self.repo / "hidden.py").unlink()
        tracker = CodeMetricsTracker(git_slug="second_main", git_repo="https://example.com/second")
        tracker.record_ignored_locations(self.scan())
        artifact = self.root / "second.json"
        tracker.save_to_file(str(artifact))
        combined.load_and_merge(str(artifact))
        artifact = self.root / "legacy.json"
        artifact.write_text(json.dumps({"git_slug": "old_main", "symlink_inventories": {}}))
        combined.load_and_merge(str(artifact))
        report = CodeMetricsTracker.from_dict(combined.to_dict()).format_markdown_section()
        self.assertIn("### Ignored / unexplored locations", report)
        self.assertIn("https://example.com/first", report)
        self.assertIn("https://example.com/second", report)
        self.assertEqual(report.count("hidden.py"), 1)
        self.assertIn("excluded by .augurignore", report)
        self.assertIn("None encountered", report)
        self.assertIn("old_main", report)
        self.assertIn("not recorded", report)

    def test_legacy_artifacts_cannot_erase_recorded_inventory(self):
        tracker = CodeMetricsTracker(git_slug="repo_main")
        tracker.record_ignored_locations(self.scan())
        artifact = self.root / "old.json"
        artifact.write_text(json.dumps({"git_slug": "repo_main"}))
        tracker.load_and_merge(str(artifact))
        self.assertIn("None encountered", tracker.format_markdown_section())
        self.assertNotIn("not recorded", tracker.format_markdown_section())

    def test_inventory_save_preserves_existing_counts_when_measurement_is_empty(self):
        artifact = self.root / "project_metrics.json"
        artifact.write_text(json.dumps({"git_slug": "repo_main", "total_repo_files": 12, "total_repo_lines": 300}))
        (self.repo / "broken.py").symlink_to("missing.py")
        tracker = CodeMetricsTracker(git_slug="repo_main")
        tracker.record_ignored_locations(self.scan())
        tracker.save_to_file(str(artifact))
        restored = CodeMetricsTracker.load_from_file(str(artifact))
        self.assertEqual(restored.total_repo_files, 12)
        self.assertEqual(restored.total_repo_lines, 300)
        self.assertIn("broken.py", restored.format_markdown_section())

    def test_report_escapes_repository_controlled_paths(self):
        (self.repo / "bad|<tag>`\n.py").symlink_to("missing")
        tracker = CodeMetricsTracker(git_slug="repo_main")
        tracker.record_ignored_locations(self.scan())
        report = tracker.format_markdown_section()
        self.assertNotIn("bad|<tag>", report)
        self.assertIn("bad&#124;&lt;tag&gt;", report)


if __name__ == "__main__":
    unittest.main()
