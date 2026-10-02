import json
import os
import sys
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Ensure code_understanding package is on sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Provide mock stubs for container dependencies when running in local environments
for pkg_name in [
    "graphrag", "graphrag.api", "graphrag.config", "graphrag.config.load_config",
    "pandas", "yaml", "mlflow", "mlflow.tracking", "requests", "deepeval",
    "pyvis", "pyvis.network", "networkx", "matplotlib", "matplotlib.pyplot", "litellm",
    "pygments", "pygments.lexers", "pygments.util", "github"
]:
    if pkg_name not in sys.modules:
        m = MagicMock()
        m.__path__ = []
        sys.modules[pkg_name] = m

mlflow_mock = sys.modules.get("mlflow")
if isinstance(mlflow_mock, MagicMock):
    mlflow_mock.get_tracking_uri.return_value = "http://localhost:5000"
    mlflow_mock.active_run.return_value = None

from utils.code_metrics_tracker import (
    CodeMetricsTracker,
    extract_data_generation_code_metrics,
)


class TestCodeMetricsTracker(unittest.TestCase):
    """Unit tests for CodeMetricsTracker singleton, line counter, scanning, and persistence."""

    def setUp(self):
        self.tracker = CodeMetricsTracker.reset_instance(git_slug="test-repo_main", git_repo="https://github.com/org/test-repo")
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        CodeMetricsTracker.reset_instance()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_singleton_get_and_reset(self):
        """Verify get_instance() returns the same singleton and reset_instance() creates a new one."""
        inst1 = CodeMetricsTracker.get_instance()
        inst2 = CodeMetricsTracker.get_instance()
        self.assertIs(inst1, inst2)
        self.assertEqual(inst1.git_slug, "test-repo_main")

        inst3 = CodeMetricsTracker.reset_instance(git_slug="another-repo_main")
        self.assertIsNot(inst1, inst3)
        self.assertEqual(inst3.git_slug, "another-repo_main")

    def test_count_file_lines_python(self):
        """Verify line counting for Python files with code, blanks, and comments."""
        py_content = (
            "# Top level comment\n"
            "\n"
            "def hello():\n"
            "    # Inside function comment\n"
            "    print('Hello world')\n"
            "\n"
            "    return True\n"
        )
        fpath = os.path.join(self.temp_dir, "sample.py")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(py_content)

        counts = CodeMetricsTracker.count_file_lines(fpath, language="python")
        self.assertEqual(counts["total"], 7)
        self.assertEqual(counts["blank"], 2)
        self.assertEqual(counts["comment"], 2)
        self.assertEqual(counts["code"], 3)

    def test_count_file_lines_java_block_comments(self):
        """Verify line counting for Java files with single-line and block comments."""
        java_content = (
            "/*\n"
            " * License block\n"
            " */\n"
            "package com.example;\n"
            "\n"
            "// Class comment\n"
            "public class Sample {\n"
            "    public static void main(String[] args) {\n"
            "        System.out.println(\"Hi\");\n"
            "    }\n"
            "}\n"
        )
        fpath = os.path.join(self.temp_dir, "Sample.java")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(java_content)

        counts = CodeMetricsTracker.count_file_lines(fpath, language="java")
        self.assertEqual(counts["total"], 11)
        self.assertEqual(counts["blank"], 1)
        self.assertEqual(counts["comment"], 4)
        self.assertEqual(counts["code"], 6)

    def test_count_file_lines_sql_and_yaml(self):
        """Verify line counting for SQL and YAML files."""
        sql_content = "-- SQL table definition\nSELECT 1;\n\n-- Another query\nSELECT 2;\n"
        fpath_sql = os.path.join(self.temp_dir, "query.sql")
        with open(fpath_sql, "w", encoding="utf-8") as f:
            f.write(sql_content)

        counts_sql = CodeMetricsTracker.count_file_lines(fpath_sql, language="sql")
        self.assertEqual(counts_sql["total"], 5)
        self.assertEqual(counts_sql["comment"], 2)
        self.assertEqual(counts_sql["code"], 2)
        self.assertEqual(counts_sql["blank"], 1)

    def test_count_file_lines_nonexistent_or_invalid(self):
        """Verify graceful fallback for nonexistent files."""
        counts = CodeMetricsTracker.count_file_lines(os.path.join(self.temp_dir, "nonexistent.py"))
        self.assertEqual(counts["total"], 0)
        self.assertEqual(counts["code"], 0)

    def test_measure_repository_basic(self):
        """Verify measuring a repository scans files and groups by language."""
        # Setup Python and Java files
        py_dir = os.path.join(self.temp_dir, "src", "python")
        java_dir = os.path.join(self.temp_dir, "src", "java")
        os.makedirs(py_dir, exist_ok=True)
        os.makedirs(java_dir, exist_ok=True)

        with open(os.path.join(py_dir, "app.py"), "w", encoding="utf-8") as f:
            f.write("import os\n\nprint('hello')\n")

        with open(os.path.join(py_dir, "requirements.txt"), "w", encoding="utf-8") as f:
            f.write("flask>=2.0\nrequests\n")

        with open(os.path.join(java_dir, "Main.java"), "w", encoding="utf-8") as f:
            f.write("public class Main {\n    public static void main(String[] args) {}\n}\n")

        with open(os.path.join(java_dir, "pom.xml"), "w", encoding="utf-8") as f:
            f.write("<project>\n  <modelVersion>4.0.0</modelVersion>\n</project>\n")

        self.tracker.measure_repository(self.temp_dir, languages=["python", "java"])

        self.assertIn("python", self.tracker.languages)
        self.assertIn("java", self.tracker.languages)

        py_stats = self.tracker.languages["python"]
        self.assertGreaterEqual(py_stats["source_files"], 1)
        self.assertGreaterEqual(py_stats["total_files"], 1)

        java_stats = self.tracker.languages["java"]
        self.assertGreaterEqual(java_stats["source_files"], 1)
        self.assertGreaterEqual(java_stats["config_files"], 1)
        self.assertGreaterEqual(java_stats["total_files"], 2)

        self.assertGreaterEqual(self.tracker.total_repo_files, 3)
        self.assertGreaterEqual(self.tracker.total_repo_lines, 5)

    def test_measure_repository_excluded_dirs(self):
        """Verify excluded directories like .git and node_modules are not counted."""
        git_dir = os.path.join(self.temp_dir, ".git")
        os.makedirs(git_dir, exist_ok=True)
        with open(os.path.join(git_dir, "config.py"), "w", encoding="utf-8") as f:
            f.write("secret = True\n")

        src_dir = os.path.join(self.temp_dir, "src")
        os.makedirs(src_dir, exist_ok=True)
        with open(os.path.join(src_dir, "valid.py"), "w", encoding="utf-8") as f:
            f.write("x = 10\n")

        self.tracker.measure_repository(self.temp_dir, languages=["python"])
        py_stats = self.tracker.languages["python"]
        self.assertEqual(py_stats["source_files"], 1)

    def test_measure_repository_large_files(self):
        """Verify files larger than max_file_size are skipped and recorded."""
        large_file = os.path.join(self.temp_dir, "large_data.py")
        with open(large_file, "w", encoding="utf-8") as f:
            f.write("# comment\n" * 500)

        # Use small threshold (100 bytes) so it triggers
        self.tracker.measure_repository(self.temp_dir, max_file_size=100, languages=["python"])
        self.assertEqual(len(self.tracker.skipped_large_files), 1)
        self.assertTrue(self.tracker.skipped_large_files[0].endswith("large_data.py"))

    def test_measure_dataframe(self):
        """Verify measuring directly from a pandas-style DataFrame."""
        mock_df = MagicMock()
        mock_df.__len__.return_value = 2
        mock_df.iterrows.return_value = [
            (0, {"file_path": "a.py", "code": "def a():\n    return 1\n"}),
            (1, {"file_path": "b.py", "code": "# comment\ndef b():\n    return 2\n"}),
        ]

        self.tracker.measure_dataframe(mock_df, language="python", config=False)
        py_stats = self.tracker.languages["python"]
        self.assertEqual(py_stats["source_files"], 2)
        self.assertEqual(py_stats["comment_lines"], 1)
        self.assertEqual(py_stats["code_lines"], 4)

    def test_serialization_to_and_from_dict(self):
        """Verify to_dict and from_dict roundtrip."""
        self.tracker.total_repo_files = 10
        self.tracker.total_repo_lines = 500
        self.tracker.languages = {
            "python": {
                "source_files": 5, "config_files": 1, "total_files": 6,
                "code_lines": 300, "comment_lines": 50, "blank_lines": 50, "total_lines": 400
            }
        }
        self.tracker.skipped_large_files = ["big.py"]

        data = self.tracker.to_dict()
        self.assertEqual(data["total_repo_files"], 10)
        self.assertEqual(data["git_slug"], "test-repo_main")

        new_tracker = CodeMetricsTracker.from_dict(data)
        self.assertEqual(new_tracker.total_repo_files, 10)
        self.assertEqual(new_tracker.total_repo_lines, 500)
        self.assertIn("python", new_tracker.languages)
        self.assertEqual(new_tracker.skipped_large_files, ["big.py"])

    def test_save_and_load_from_file(self):
        """Verify saving to JSON and loading from JSON file."""
        self.tracker.total_repo_files = 3
        self.tracker.total_repo_lines = 120
        self.tracker.languages = {
            "java": {
                "source_files": 3, "config_files": 0, "total_files": 3,
                "code_lines": 100, "comment_lines": 10, "blank_lines": 10, "total_lines": 120
            }
        }
        save_path = os.path.join(self.temp_dir, "project_metrics.json")
        self.tracker.save_to_file(save_path)

        self.assertTrue(os.path.exists(save_path))
        loaded = CodeMetricsTracker.load_from_file(save_path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.total_repo_files, 3)
        self.assertEqual(loaded.total_repo_lines, 120)

    def test_load_and_merge(self):
        """Verify merging metrics from an external file."""
        self.tracker.languages = {
            "python": {
                "source_files": 2, "config_files": 0, "total_files": 2,
                "code_lines": 50, "comment_lines": 5, "blank_lines": 5, "total_lines": 60
            }
        }
        other_path = os.path.join(self.temp_dir, "upstream_metrics.json")
        with open(other_path, "w", encoding="utf-8") as f:
            json.dump({
                "git_slug": "test-repo_main",
                "total_repo_files": 5,
                "total_repo_lines": 150,
                "languages": {
                    "java": {
                        "source_files": 3, "config_files": 0, "total_files": 3,
                        "code_lines": 80, "comment_lines": 5, "blank_lines": 5, "total_lines": 90
                    }
                },
                "skipped_large_files": ["huge.java"]
            }, f)

        self.tracker.load_and_merge(other_path)
        self.assertIn("python", self.tracker.languages)
        self.assertIn("java", self.tracker.languages)
        self.assertEqual(self.tracker.total_repo_files, 5)
        self.assertEqual(self.tracker.total_repo_lines, 150)
        self.assertIn("huge.java", self.tracker.skipped_large_files)

    @patch("utils.code_metrics_tracker.logging")
    def test_log_to_mlflow(self, mock_logging):
        """Verify MLflow metrics logging."""
        mlflow_m = sys.modules.get("mlflow")
        if isinstance(mlflow_m, MagicMock):
            mlflow_m.active_run.return_value = MagicMock()

        self.tracker.total_repo_files = 15
        self.tracker.total_repo_lines = 1000
        self.tracker.languages = {
            "python": {
                "source_files": 10, "config_files": 2, "total_files": 12,
                "code_lines": 800, "comment_lines": 100, "blank_lines": 100, "total_lines": 1000
            }
        }

        self.tracker.log_to_mlflow()
        if isinstance(mlflow_m, MagicMock):
            mlflow_m.log_metrics.assert_called()
            logged_dict = mlflow_m.log_metrics.call_args[0][0]
            self.assertEqual(logged_dict["project_total_files"], 15.0)
            self.assertEqual(logged_dict["project_python_code_lines"], 800.0)

    def test_format_summary(self):
        """Verify ASCII summary formatting contains repo slug, counts, and language columns."""
        self.tracker.total_repo_files = 4
        self.tracker.total_repo_lines = 250
        self.tracker.languages = {
            "python": {
                "source_files": 2, "config_files": 0, "total_files": 2,
                "code_lines": 100, "comment_lines": 10, "blank_lines": 10, "total_lines": 120
            },
            "java": {
                "source_files": 2, "config_files": 0, "total_files": 2,
                "code_lines": 110, "comment_lines": 10, "blank_lines": 10, "total_lines": 130
            }
        }
        summary = self.tracker.format_summary()
        self.assertIn("PROJECT CODEBASE & REPOSITORY METRICS", summary)
        self.assertIn("Total Analyzed Files : 4", summary)
        self.assertIn("python", summary)
        self.assertIn("java", summary)

    def test_format_markdown_section(self):
        """Verify markdown section formatting contains headers and summary note."""
        self.tracker.total_repo_files = 12
        self.tracker.total_repo_lines = 1500
        self.tracker.languages = {
            "python": {
                "source_files": 10, "config_files": 2, "total_files": 12,
                "code_lines": 1200, "comment_lines": 150, "blank_lines": 150, "total_lines": 1500
            }
        }
        self.tracker.skipped_large_files = ["big_model.bin"]
        md = self.tracker.format_markdown_section()
        self.assertIn("### Project Codebase & Scope Summary", md)
        self.assertIn("**12** file(s) analyzed", md)
        self.assertIn("| **python** |", md)
        self.assertIn("oversized file(s)", md)

    def test_extract_data_generation_code_metrics(self):
        """Verify helper extracts metrics from candidate directories."""
        fpath = os.path.join(self.temp_dir, "project_metrics.json")
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump({
                "git_slug": "test-repo_main",
                "total_repo_files": 8,
                "total_repo_lines": 400,
                "languages": {
                    "go": {
                        "source_files": 8, "config_files": 0, "total_files": 8,
                        "code_lines": 350, "comment_lines": 25, "blank_lines": 25, "total_lines": 400
                    }
                }
            }, f)

        res = extract_data_generation_code_metrics([self.temp_dir], self.tracker)
        self.assertIn("go", res.languages)
        self.assertEqual(res.total_repo_files, 8)


if __name__ == "__main__":
    unittest.main()
