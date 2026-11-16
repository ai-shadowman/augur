import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from provenance import facts as facts_module
from provenance.models import Layer, Package, RepoFacts
from provenance.pipeline import FACTS_FILENAME
from utils.code_utils import generate_slug_from_repo
from utils.repo_catalog import (
    build_repo_catalog,
    read_flat_metadata,
    require_focal_in_list,
)

TRAIN = "https://github.com/org/train"
TIMETABLE = "https://github.com/org/timetable"


def write_meta(repo_dir, rel, **fields):
    import yaml

    path = Path(repo_dir) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(fields), encoding="utf-8")
    return str(path)


def header(path, slug, runtime=None):
    text = (f"This file is located at {path} from repository url https://github.com/{slug}, "
            f"repository slug {slug}, multi_repo true")
    return text + (f", runtime {runtime}" if runtime else "")


class RealYaml(unittest.TestCase):
    """Other test modules stub yaml in sys.modules at collection; use the real one, scoped per test."""

    def setUp(self):
        modules = self.enterContext(patch.dict(sys.modules))
        for name in [n for n in modules if n == "yaml" or n.startswith("yaml.")]:
            del modules[name]
        importlib.import_module("yaml")


class TestRequireFocalInList(unittest.TestCase):
    LIST = [{"git_repo": TRAIN, "git_branch": "main"},
            {"git_repo": TIMETABLE}]

    def test_returns_slug_of_listed_repo(self):
        self.assertEqual(require_focal_in_list(TRAIN, "main", self.LIST),
                         generate_slug_from_repo(TRAIN, "main"))

    def test_list_item_without_branch_means_main(self):
        self.assertEqual(require_focal_in_list(TIMETABLE, "main", self.LIST),
                         generate_slug_from_repo(TIMETABLE, "main"))

    def test_empty_git_repo_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "GIT_REPO"):
            require_focal_in_list("", "main", self.LIST)

    def test_unlisted_repo_is_rejected_naming_both_sides(self):
        with self.assertRaises(ValueError) as ctx:
            require_focal_in_list("https://github.com/org/other", "main", self.LIST)
        self.assertIn(generate_slug_from_repo("https://github.com/org/other", "main"), str(ctx.exception))
        self.assertIn(generate_slug_from_repo(TRAIN, "main"), str(ctx.exception))
        self.assertIn(generate_slug_from_repo(TIMETABLE, "main"), str(ctx.exception))

    def test_other_branch_of_listed_repo_is_rejected(self):
        with self.assertRaises(ValueError):
            require_focal_in_list(TRAIN, "develop", self.LIST)

    def test_spelling_variants_follow_the_slug(self):
        # Review Focus 5: accepted exactly when generate_slug_from_repo agrees.
        for variant in (TRAIN + ".git", TRAIN + "/"):
            with self.subTest(variant=variant):
                same = generate_slug_from_repo(variant, "main") == generate_slug_from_repo(TRAIN, "main")
                if same:
                    require_focal_in_list(variant, "main", self.LIST)
                else:
                    with self.assertRaises(ValueError):
                        require_focal_in_list(variant, "main", self.LIST)


class TestReadFlatMetadata(RealYaml):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_regroups_lists_and_drops_empty_values(self):
        path = write_meta(self.tmp.name, "A_metadata.txt",
                          file_path="src/A.java", language="java", package="com.a", purpose="Does A.",
                          imports_0="a.b", imports_1="", classes_0="C", runtime_stack_0_name="")
        self.assertEqual(read_flat_metadata(path), {
            "file_path": "src/A.java", "language": "java", "package": "com.a",
            "purpose": "Does A.", "imports": ["a.b"], "classes": ["C"]})

    def test_list_items_are_ordered_by_numeric_index(self):
        fields = {f"imports_{i}": f"m{i}" for i in range(12)}
        path = write_meta(self.tmp.name, "A_metadata.txt", file_path="A.py", **fields)
        self.assertEqual(read_flat_metadata(path)["imports"], [f"m{i}" for i in range(12)])

    def test_empty_and_null_values_are_dropped(self):
        path = write_meta(self.tmp.name, "A_metadata.txt",
                          file_path="A.py", language=None, package="", purpose="", imports_0=None)
        self.assertEqual(read_flat_metadata(path), {"file_path": "A.py"})

    def test_file_without_file_path_is_none(self):
        path = write_meta(self.tmp.name, "A_metadata.txt", language="java", package="com.a")
        self.assertIsNone(read_flat_metadata(path))
        empty = write_meta(self.tmp.name, "B_metadata.txt", file_path="", package="com.a")
        self.assertIsNone(read_flat_metadata(empty))

    def test_non_mapping_yaml_is_none(self):
        path = Path(self.tmp.name) / "A_metadata.txt"
        path.write_text("- just\n- a list\n", encoding="utf-8")
        self.assertIsNone(read_flat_metadata(str(path)))
        path.write_text("plain text", encoding="utf-8")
        self.assertIsNone(read_flat_metadata(str(path)))

    def test_unreadable_file_is_none(self):
        self.assertIsNone(read_flat_metadata(os.path.join(self.tmp.name, "missing_metadata.txt")))
        bad = Path(self.tmp.name) / "bad_metadata.txt"
        bad.write_text("key: [unclosed", encoding="utf-8")
        self.assertIsNone(read_flat_metadata(str(bad)))


class TestBuildRepoCatalog(RealYaml):
    LIST = [{"git_repo": TRAIN, "git_branch": "main"},
            {"git_repo": TIMETABLE}]

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = self.tmp.name
        self.train = Path(self.parent) / generate_slug_from_repo(TRAIN, "main")
        self.timetable = Path(self.parent) / generate_slug_from_repo(TIMETABLE, "main")
        self.train.mkdir()
        self.timetable.mkdir()

    def build(self, git_repos=None):
        return build_repo_catalog(self.parent, git_repos or self.LIST)

    def test_rolls_up_packages_files_and_languages(self):
        write_meta(self.train, "src/Train_metadata.txt", file_path="src/Train.java", language="java",
                   package="com.org.train", purpose="Models a train.", classes_0="Train")
        write_meta(self.train, "src/Car_metadata.txt", file_path="src/Car.java", language="java",
                   package="com.org.train", purpose="Models a car.", classes_0="Car", classes_1="Wheel")
        write_meta(self.timetable, "app/main_metadata.txt", file_path="app/main.py", language="python",
                   package="app", purpose="Entry point.")
        write_meta(self.timetable, "web/index_metadata.txt", file_path="web/index.js",
                   language="javascript", package="web")

        self.assertEqual(self.build(), {"repos": [
            {"git_repo": TRAIN, "git_branch": "main", "git_slug": self.train.name,
             "languages": ["java"],
             "packages": [{"package": "com.org.train", "files": [
                 {"file_path": "src/Car.java", "purpose": "Models a car.", "classes": ["Car", "Wheel"]},
                 {"file_path": "src/Train.java", "purpose": "Models a train.", "classes": ["Train"]}]}]},
            {"git_repo": TIMETABLE, "git_branch": "main", "git_slug": self.timetable.name,
             "languages": ["javascript", "python"],
             "packages": [
                 {"package": "app", "files": [{"file_path": "app/main.py", "purpose": "Entry point."}]},
                 {"package": "web", "files": [{"file_path": "web/index.js"}]}]},
        ]})

    def test_files_without_package_are_left_out_of_packages(self):
        write_meta(self.train, "Train_metadata.txt", file_path="Train.java", language="java",
                   package="com.org.train")
        write_meta(self.train, "build_metadata.txt", file_path="build.gradle", language="groovy")
        repo = self.build()["repos"][0]
        self.assertEqual(repo["languages"], ["groovy", "java"])
        self.assertEqual(repo["packages"], [{"package": "com.org.train",
                                             "files": [{"file_path": "Train.java"}]}])

    def test_empty_package_value_is_never_a_package(self):
        # Review Focus 2: '' is absent, not a package named ''.
        write_meta(self.train, "A_metadata.txt", file_path="A.java", language="java", package="")
        write_meta(self.train, "B_metadata.txt", file_path="B.java", language="java", package=None)
        repo = self.build()["repos"][0]
        self.assertNotIn("packages", repo)
        self.assertEqual(repo["languages"], ["java"])

    def test_unusable_metadata_is_skipped_with_a_warning(self):
        (self.train / "bad_metadata.txt").write_text("key: [unclosed", encoding="utf-8")
        (self.train / "tokens_metadata.txt").write_text("{}", encoding="utf-8")
        write_meta(self.train, "A_metadata.txt", file_path="A.java", language="java", package="a")
        with self.assertLogs("utils.repo_catalog", "WARNING"):
            repo = self.build()["repos"][0]
        self.assertEqual(repo["packages"], [{"package": "a", "files": [{"file_path": "A.java"}]}])

    def test_runtime_and_libraries_come_from_provenance_json(self):
        def pkg(name, version):
            return Package(name=name, ecosystem="maven", layer=Layer.PACKAGE,
                           source_file="pom.xml", version=version)

        facts_module.write(RepoFacts(root="/train", runtime="Java 17", packages=[
            pkg("junit", "5.10.0"), pkg("guava", "33.0"), pkg("junit", "5.10.0"), pkg("slf4j", None)]),
            self.train / FACTS_FILENAME)
        (self.train / "Train.txt").write_text(header("Train.java", self.train.name, "Java 8"), encoding="utf-8")

        repo = self.build()["repos"][0]
        self.assertEqual(repo["runtime_stack"], ["Java 17"])
        self.assertEqual(repo["libraries"], [{"name": "guava", "version": "33.0"},
                                             {"name": "junit", "version": "5.10.0"},
                                             {"name": "slf4j"}])

    def test_runtime_falls_back_to_header_suffix(self):
        (self.train / "a.txt").write_text(
            header("A.java", self.train.name, "Java 17 / Maven 3.9") + "\n Package: com.a\n",
            encoding="utf-8")
        # A file with no metadata lines closes its comment on the header line.
        (self.train / "b.txt").write_text("/*" + header("B.java", self.train.name, "Java 17") + "*/\nclass B {}\n",
                                          encoding="utf-8")
        repo = self.build()["repos"][0]
        self.assertEqual(repo["runtime_stack"], ["Java 17", "Maven 3.9"])
        self.assertNotIn("libraries", repo)

    def test_header_below_line_three_is_ignored(self):
        (self.train / "a.txt").write_text(
            "/*\n\n\n" + header("A.java", self.train.name, "Java 17") + "*/\n", encoding="utf-8")
        self.assertNotIn("runtime_stack", self.build()["repos"][0])

    def test_unreadable_provenance_json_is_skipped_with_a_warning(self):
        (self.train / FACTS_FILENAME).write_text("not json", encoding="utf-8")
        with self.assertLogs("utils.repo_catalog", "WARNING"):
            repo = self.build()["repos"][0]
        self.assertNotIn("libraries", repo)

    def test_missing_optional_data_omits_the_keys(self):
        with self.assertLogs("utils.repo_catalog", "WARNING"):
            catalog = self.build()
        for repo in catalog["repos"]:
            self.assertEqual(set(repo), {"git_repo", "git_branch", "git_slug"})

    def test_missing_repo_directory_raises_naming_the_repo(self):
        missing = "https://github.com/org/absent"
        with self.assertRaisesRegex(RuntimeError, "org/absent"):
            self.build([{"git_repo": missing, "git_branch": "main"}])


if __name__ == "__main__":
    unittest.main()
