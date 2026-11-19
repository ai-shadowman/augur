import ast
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from provenance import facts as facts_module
from provenance.models import Layer, Package, RepoFacts
from provenance.pipeline import FACTS_FILENAME
from utils.repo_catalog import (
    annotate_focal_repo,
    build_repo_catalog,
    format_annotation,
    read_flat_metadata,
    require_focal_in_list,
    resolve_import,
    save_repo_catalog,
)

TRAIN = "https://github.com/org/train"
TIMETABLE = "https://github.com/org/timetable"
RAIL = "https://github.com/org/rail"


def generate_slug_from_repo(git_repo, git_branch):
    # Imported per call: utils.code_utils loads real pandas/mlflow, and doing that at
    # collection stops other test modules from installing their sys.modules stubs.
    from utils.code_utils import generate_slug_from_repo as slug

    return slug(git_repo, git_branch)


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

    def test_non_string_scalars_are_coerced_to_str(self):
        path = write_meta(self.tmp.name, "A_metadata.txt", file_path="A.py", package=12, purpose=True, imports_0=5)
        self.assertEqual(read_flat_metadata(path),
                         {"file_path": "A.py", "package": "12", "purpose": "True", "imports": ["5"]})

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


def one_repo_catalog(packages, runtime_stack=None):
    repo = {"git_repo": "https://github.com/org/lib", "git_branch": "main", "git_slug": "org-lib-main",
            "packages": packages}
    if runtime_stack:
        repo["runtime_stack"] = runtime_stack
    return {"repos": [repo]}


class TestResolveImport(unittest.TestCase):
    FOCAL = "org-timetable-main"
    CATALOG = {"repos": [
        {"git_repo": TIMETABLE, "git_branch": "main", "git_slug": "org-timetable-main",
         "packages": [{"package": "com.org.timetable",
                       "files": [{"file_path": "src/Model.java", "purpose": "Timetable model."}]}]},
        {"git_repo": TRAIN, "git_branch": "main", "git_slug": "org-train-main", "runtime_stack": ["Java 17"],
         "packages": [
             {"package": "com.org.train", "files": [
                 {"file_path": "src/Car.java", "purpose": "Models a car."},
                 {"file_path": "src/Train.java", "purpose": "Models a train."}]},
             {"package": "com.org.train.api", "files": [
                 {"file_path": "src/api/Routes.java", "purpose": "HTTP routes."}]}]},
        # Listed after org-train-main so the slug sort is observable.
        {"git_repo": RAIL, "git_branch": "main", "git_slug": "org-rail-main",
         "packages": [{"package": "com.org.train",
                       "files": [{"file_path": "src/Rail.java", "purpose": "Rail things."}]}]},
    ]}

    def resolve(self, import_name, catalog=None):
        return resolve_import(import_name, catalog or self.CATALOG, self.FOCAL)

    def by_slug(self, import_name):
        return {match["git_slug"]: match for match in self.resolve(import_name)}

    def test_exact_match(self):
        self.assertEqual([(m["git_slug"], m["package"]) for m in self.resolve("com.org.train.api")],
                         [("org-train-main", "com.org.train.api")])

    def test_prefix_match_with_dot(self):
        self.assertEqual(self.resolve("com.org.train.api.Routes"), [
            {"git_repo": TRAIN, "git_slug": "org-train-main", "package": "com.org.train.api",
             "purpose": "HTTP routes.", "runtime": "Java 17"}])

    def test_wildcard_import(self):
        self.assertEqual([(m["git_slug"], m["package"]) for m in self.resolve("com.org.train.api.*")],
                         [("org-train-main", "com.org.train.api")])

    def test_longest_package_wins(self):
        self.assertEqual([m["package"] for m in self.resolve("com.org.train.api.Routes")],
                         ["com.org.train.api"])

    def test_tie_returns_both_sorted_by_slug(self):
        matches = self.resolve("com.org.train.Train")
        self.assertEqual([m["git_slug"] for m in matches], ["org-rail-main", "org-train-main"])
        self.assertEqual({m["package"] for m in matches}, {"com.org.train"})

    def test_string_prefix_that_is_not_a_path_prefix_does_not_match(self):
        # Review Focus 1.
        self.assertEqual(self.resolve("com.org.trainer.Core"), [])
        self.assertEqual(self.resolve("trainer.core", one_repo_catalog(
            [{"package": "train", "files": [{"file_path": "train/core.py"}]}])), [])

    def test_slash_and_double_colon_separators(self):
        catalog = one_repo_catalog([
            {"package": "lib/train", "files": [{"file_path": "lib/train/car.go"}]},
            {"package": "train", "files": [{"file_path": "src/car.rs"}]}])
        self.assertEqual([m["package"] for m in self.resolve("lib/train/car", catalog)], ["lib/train"])
        self.assertEqual([m["package"] for m in self.resolve("train::car", catalog)], ["train"])

    def test_own_package_never_matches(self):
        self.assertEqual(self.resolve("com.org.timetable.Model"), [])

    def focal_and_sibling(self, focal_package, sibling_package):
        repos = [(TIMETABLE, self.FOCAL, focal_package), (TRAIN, "org-train-main", sibling_package)]
        return {"repos": [{"git_repo": url, "git_branch": "main", "git_slug": slug,
                           "packages": [{"package": package, "files": [{"file_path": "src/X.java"}]}]}
                          for url, slug, package in repos]}

    def test_own_package_vetoes_a_shorter_sibling_package(self):
        self.assertEqual(self.resolve("com.org.timetable.Model",
                                      self.focal_and_sibling("com.org.timetable", "com.org")), [])

    def test_own_package_vetoes_an_equal_sibling_package(self):
        self.assertEqual(self.resolve("app.models", self.focal_and_sibling("app", "app")), [])

    def test_longer_sibling_package_still_beats_own_package(self):
        matches = self.resolve("com.org.train.Train", self.focal_and_sibling("com.org", "com.org.train"))
        self.assertEqual([(m["git_slug"], m["package"]) for m in matches], [("org-train-main", "com.org.train")])

    def test_relative_import_never_resolves(self):
        self.assertEqual(self.resolve("..train"), [])

    def test_empty_import_never_resolves(self):
        self.assertEqual(self.resolve(""), [])

    def test_no_match(self):
        self.assertEqual(self.resolve("org.junit.Test"), [])

    def test_matching_is_case_sensitive(self):
        self.assertEqual(self.resolve("com.org.Train.api"), [])

    def test_file_purpose_used_when_last_segment_names_a_file(self):
        matches = self.by_slug("com.org.train.Car")
        self.assertEqual(matches["org-train-main"]["purpose"], "Models a car.")
        self.assertEqual(matches["org-rail-main"]["purpose"], "Rail things.")

    def test_package_purpose_joins_first_three_files(self):
        self.assertEqual(self.by_slug("com.org.train.*")["org-train-main"]["purpose"],
                         "Models a car.; Models a train.")
        catalog = one_repo_catalog([{"package": "pkg", "files": [
            {"file_path": "pkg/d.py", "purpose": "D."}, {"file_path": "pkg/a.py", "purpose": "A."},
            {"file_path": "pkg/b.py"}, {"file_path": "pkg/c.py", "purpose": "C."}]}])
        self.assertEqual(self.resolve("pkg", catalog)[0]["purpose"], "A.; C.")

    def test_purpose_is_one_line_and_at_most_200_chars(self):
        text = "Line one.\n  Line\ttwo.  " + "x" * 300
        catalog = one_repo_catalog([{"package": "pkg", "files": [{"file_path": "pkg/a.py", "purpose": text}]}])
        purpose = self.resolve("pkg.a", catalog)[0]["purpose"]
        self.assertEqual(purpose, ("Line one. Line two. " + "x" * 300)[:200])
        self.assertEqual(len(purpose), 200)

    def test_runtime_joined(self):
        self.assertEqual(self.by_slug("com.org.train.Train")["org-train-main"]["runtime"], "Java 17")
        catalog = one_repo_catalog([{"package": "pkg", "files": [{"file_path": "pkg/a.py"}]}],
                                   ["Python 3.12", "pip 24"])
        self.assertEqual(self.resolve("pkg", catalog)[0]["runtime"], "Python 3.12 / pip 24")

    def test_unknown_purpose_and_runtime_are_empty(self):
        match = self.resolve("lib.x", one_repo_catalog([{"package": "lib", "files": [{"file_path": "lib/x.py"}]}]))[0]
        self.assertEqual((match["purpose"], match["runtime"]), ("", ""))


class TestFormatAnnotation(unittest.TestCase):
    TRAIN_MATCH = {"git_repo": TRAIN, "git_slug": "org-train-main",
                   "package": "com.org.train", "purpose": "Models a train.", "runtime": "Java 17"}

    def test_full(self):
        self.assertEqual(
            format_annotation([self.TRAIN_MATCH]),
            "(provided by repository https://github.com/org/train, slug org-train-main; "
            "purpose: Models a train.; runtime: Java 17)")

    def test_omits_empty_purpose_and_runtime(self):
        self.assertEqual(format_annotation([dict(self.TRAIN_MATCH, purpose="", runtime="")]),
                         "(provided by repository https://github.com/org/train, slug org-train-main)")
        self.assertEqual(format_annotation([dict(self.TRAIN_MATCH, purpose="")]),
                         "(provided by repository https://github.com/org/train, slug org-train-main; "
                         "runtime: Java 17)")

    def test_two_matches_are_two_groups_separated_by_a_space(self):
        rail = {"git_repo": RAIL, "git_slug": "org-rail-main", "package": "com.org.train",
                "purpose": "Rail things.", "runtime": ""}
        self.assertEqual(
            format_annotation([rail, self.TRAIN_MATCH]),
            "(provided by repository https://github.com/org/rail, slug org-rail-main; purpose: Rail things.) "
            "(provided by repository https://github.com/org/train, slug org-train-main; "
            "purpose: Models a train.; runtime: Java 17)")

    def test_no_matches_is_empty_string(self):
        self.assertEqual(format_annotation([]), "")


class TestAnnotateFocalRepo(RealYaml):
    FOCAL = TestResolveImport.FOCAL
    CATALOG = TestResolveImport.CATALOG
    HEADER = "/*This file is located at src/Model.java from repository url U, repository slug S, multi_repo true\n"
    REST = ("\n Package: com.org.timetable\n\nDependencies:\n"
            "- [import] com.org.train.Train\n- [import] java.util.List\n- [library] junit 5\n*/\n"
            "import com.org.train.Train;\n// - [import] com.org.train.Train\n")
    ANNOTATION = format_annotation(resolve_import("com.org.train.Train", CATALOG, FOCAL))
    ANNOTATED = "- [import] com.org.train.Train " + ANNOTATION

    def setUp(self):
        super().setUp()
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source, self.dest = tmp / "source", tmp / "dest"
        self.write_code(self.HEADER + self.REST)
        write_meta(self.source, "src/Model_metadata.txt", file_path="src/Model.java", package="com.org.timetable",
                   multi_repo="true", imports_0="com.org.train.Train", imports_1="java.util.List")
        (self.source / FACTS_FILENAME).write_text('{"runtime": "Java 17"}\n', encoding="utf-8")

    def write_code(self, text):
        path = self.source / "src" / "Model.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())

    def annotate(self):
        return annotate_focal_repo(str(self.source), str(self.dest), self.CATALOG, self.FOCAL)

    def code(self, root=None):
        return ((root or self.dest) / "src" / "Model.txt").read_bytes().decode()

    def lines(self):
        return self.code().split("\n")

    def snapshot(self, root):
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    def test_first_line_flag_is_forced_false(self):
        self.annotate()
        self.assertEqual(self.lines()[0], self.HEADER.replace("multi_repo true", "multi_repo false").rstrip("\n"))

    def test_flag_already_false_is_left_false(self):
        header = self.HEADER.replace("multi_repo true", "multi_repo false")
        self.write_code(header + self.REST)
        self.annotate()
        self.assertEqual(self.lines()[0], header.rstrip("\n"))

    def test_resolved_import_line_gets_the_annotation(self):
        self.assertIn("provided by repository", self.ANNOTATION)
        self.annotate()
        self.assertEqual(self.lines()[5], self.ANNOTATED)

    def test_unresolved_import_and_library_lines_are_byte_identical(self):
        self.annotate()
        self.assertEqual(self.lines()[6:8], ["- [import] java.util.List", "- [library] junit 5"])

    def test_only_the_flag_and_resolved_lines_differ(self):
        self.annotate()
        before, after = self.code(self.source).split("\n"), self.lines()
        self.assertEqual(len(before), len(after))
        self.assertEqual([i for i, (a, b) in enumerate(zip(before, after)) if a != b], [0, 5])
        self.assertTrue(self.code().endswith("// - [import] com.org.train.Train\n"))

    def test_line_endings_and_missing_final_newline_are_kept(self):
        self.write_code((self.HEADER + self.REST).replace("\n", "\r\n").removesuffix("\r\n"))
        self.annotate()
        self.assertEqual(self.code(), (self.HEADER + self.REST).replace("multi_repo true", "multi_repo false")
                         .replace("- [import] com.org.train.Train\n", self.ANNOTATED + "\n", 1)
                         .replace("\n", "\r\n").removesuffix("\r\n"))

    def test_header_line_on_second_line_is_still_found(self):
        self.write_code("/*\n" + self.HEADER.removeprefix("/*") + self.REST)
        self.annotate()
        lines = self.lines()
        self.assertEqual(lines[0], "/*")
        self.assertIn("repository slug S, multi_repo false", lines[1])
        self.assertEqual(lines[6], self.ANNOTATED)

    def test_header_lookalike_in_code_body_is_not_rewritten(self):
        # Review Focus 3.
        body = "This file is located at X, multi_repo true\nDependencies:\n- [import] com.org.train.Train\n"
        self.write_code(self.HEADER + self.REST + body)
        self.annotate()
        self.assertEqual(self.lines()[9:], ["import com.org.train.Train;", "// - [import] com.org.train.Train",
                                            *body.split("\n")])

    def test_header_below_line_three_keeps_its_flag_with_a_warning(self):
        self.write_code("/*\n\n\n" + self.HEADER.removeprefix("/*") + self.REST)
        with self.assertLogs("utils.repo_catalog", "WARNING"):
            self.annotate()
        self.assertIn("multi_repo true", self.lines()[3])
        self.assertEqual(self.lines()[8], self.ANNOTATED)

    def test_file_without_dependencies_block_only_changes_the_flag(self):
        text = self.HEADER + "\n Package: com.org.timetable*/\nimport com.org.train.Train;\n- [import] com.org.train.Train\n"
        self.write_code(text)
        self.annotate()
        self.assertEqual(self.code(), text.replace("multi_repo true", "multi_repo false"))

    def test_metadata_file_gets_flag_false_and_annotated_import(self):
        import yaml

        self.annotate()
        data = yaml.safe_load((self.dest / "src" / "Model_metadata.txt").read_text(encoding="utf-8"))
        self.assertIs(data["multi_repo"], False)
        self.assertEqual(data["imports_0"], "com.org.train.Train " + self.ANNOTATION)
        self.assertEqual(data["imports_1"], "java.util.List")
        self.assertEqual((data["file_path"], data["package"]), ("src/Model.java", "com.org.timetable"))

    def test_annotated_copy_parses_as_single_repo(self):
        # The provenance _belongs_to skip relies on the annotated copy reading as multi_repo false.
        from provenance.corpus import parse_header

        write_meta(self.source, "src/Model_metadata.txt", file_path="src/Model.java", git_repo="U", git_slug="S",
                   multi_repo="true", imports_0="com.org.train.Train")
        self.annotate()
        for name in ("Model.txt", "Model_metadata.txt"):
            with self.subTest(name=name):
                self.assertIs(parse_header((self.dest / "src" / name).read_text(encoding="utf-8")).multi_repo, False)

    def test_metadata_without_code_file_is_still_rewritten(self):
        import yaml

        (self.source / "src" / "Model.txt").unlink()
        self.assertEqual(self.annotate()["files"], 1)
        data = yaml.safe_load((self.dest / "src" / "Model_metadata.txt").read_text(encoding="utf-8"))
        self.assertIs(data["multi_repo"], False)
        self.assertEqual(data["imports_0"], "com.org.train.Train " + self.ANNOTATION)

    def test_source_dir_is_not_modified(self):
        before = self.snapshot(self.source)
        self.annotate()
        self.assertEqual(self.snapshot(self.source), before)

    def test_other_files_are_copied_untouched(self):
        # tokens_metadata.txt is a telemetry sidecar, not source metadata: the pair is skipped.
        (self.source / "tokens.txt").write_text("{}", encoding="utf-8")
        (self.source / "tokens_metadata.txt").write_text("{}", encoding="utf-8")
        with self.assertLogs("utils.repo_catalog", "WARNING"):
            self.assertEqual(self.annotate()["files"], 1)
        for name in (FACTS_FILENAME, "tokens.txt", "tokens_metadata.txt"):
            self.assertEqual((self.dest / name).read_bytes(), (self.source / name).read_bytes())

    def test_existing_dest_dir_is_replaced(self):
        self.dest.mkdir()
        (self.dest / "stale.txt").write_text("old", encoding="utf-8")
        self.annotate()
        self.assertFalse((self.dest / "stale.txt").exists())
        self.assertTrue((self.dest / "src" / "Model.txt").is_file())

    def test_dest_overlapping_source_is_rejected(self):
        before = self.snapshot(self.source)
        for dest in (self.source, self.source / "out", self.source.parent):
            with self.subTest(dest=dest), self.assertRaises(ValueError):
                annotate_focal_repo(str(self.source), str(dest), self.CATALOG, self.FOCAL)
        self.assertEqual(self.snapshot(self.source), before)

    def test_counts(self):
        self.assertEqual(self.annotate(), {"files": 1, "resolved_imports": 1, "unresolved_imports": 1})

    def test_missing_source_dir_raises(self):
        with self.assertRaises(RuntimeError):
            annotate_focal_repo(str(self.source.parent / "absent"), str(self.dest), self.CATALOG, self.FOCAL)

    def test_source_dir_without_txt_files_raises(self):
        empty = self.source.parent / "empty"
        empty.mkdir()
        (empty / FACTS_FILENAME).write_text("{}", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            annotate_focal_repo(str(empty), str(self.dest), self.CATALOG, self.FOCAL)
        self.assertFalse(self.dest.exists())


class TestSaveRepoCatalog(unittest.TestCase):
    CATALOG = {"repos": [{"git_slug": "org-train-main", "git_repo": TRAIN, "git_branch": "main"}]}
    TAGS = {"category": "data-generation", "multi_repo": "True", "repo-catalog": "true"}

    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.loader = self.enterContext(patch("loaders.default_asset_loader.DefaultAssetLoader"))

    def test_writes_json_and_returns_path(self):
        path = save_repo_catalog(self.CATALOG, self.tmp)
        self.assertEqual(path, os.path.join(self.tmp, "repo_catalog.json"))
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), self.CATALOG)
        self.assertEqual(Path(path).read_text(encoding="utf-8"),
                         json.dumps(self.CATALOG, indent=2, sort_keys=True))

    def test_logs_under_multi_repo_metadata_path_with_tags(self):
        path = save_repo_catalog(self.CATALOG, self.tmp)
        self.loader.get_log_results_artifact_path.assert_called_once_with(
            self.loader.RESULTS_PATH_PREFIX_METADATA, git_slug=None, multi_repo=True)
        self.loader.return_value.log_results.assert_called_once_with(
            path, artifact_path=self.loader.get_log_results_artifact_path.return_value, tags=self.TAGS)

    def test_logging_failure_does_not_raise(self):
        self.loader.return_value.log_results.side_effect = RuntimeError("tracking server down")
        with self.assertLogs("utils.repo_catalog", "WARNING"):
            path = save_repo_catalog(self.CATALOG, self.tmp)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), self.CATALOG)


class TestExtractGraphPromptRule(unittest.TestCase):
    PROMPT = (Path(__file__).resolve().parents[1] / "assets/graphrag/extract_graph.txt").read_text()

    def test_prompt_recognises_the_annotation_format_annotation_writes(self):
        annotation = format_annotation([{"git_repo": "U", "git_slug": "S", "package": "p",
                                         "purpose": "", "runtime": ""}])
        self.assertTrue(annotation.startswith("(provided by repository "))
        self.assertIn("(provided by repository <url>, slug <slug>", self.PROMPT)

    def test_rule_names_the_relationship_and_the_no_annotation_guard(self):
        self.assertIn("An import line with no annotation is\n  unaffected by this rule.", self.PROMPT)
        rule = self.PROMPT[self.PROMPT.index("(provided by repository <url>"):]
        self.assertIn("CROSS_REPO_IMPORTS", rule[:700])


class TestPrepareFocalCodebase(RealYaml):
    LIST = [{"git_repo": TRAIN, "git_branch": "main"}, {"git_repo": TIMETABLE}]

    def setUp(self):
        # Slugs first: utils.code_utils loads numpy, which cannot be reloaded once RealYaml's scope drops it.
        train, focal = (generate_slug_from_repo(repo, "main") for repo in (TRAIN, TIMETABLE))
        super().setUp()
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.parent, self.dest = tmp / "target", tmp / "target-single-repo-with-context" / "focal"
        focal = self.parent / focal
        write_meta(self.parent / train, "src/Train_metadata.txt",
                   file_path="src/Train.java", language="java", package="com.org.train", purpose="Models a train.")
        write_meta(focal, "src/Model_metadata.txt", file_path="src/Model.java", language="java",
                   package="com.org.timetable", imports_0="com.org.train.Train", imports_1="java.util.List")
        (focal / "src" / "Model.txt").write_text(TestAnnotateFocalRepo.HEADER + TestAnnotateFocalRepo.REST,
                                                 encoding="utf-8")
        self.enterContext(patch("loaders.default_asset_loader.DefaultAssetLoader"))

    def prepare(self, git_repo):
        from utils.repo_catalog import prepare_focal_codebase

        return prepare_focal_codebase(git_repo, "main", self.LIST, str(self.parent), str(self.dest))

    def test_writes_catalog_and_annotated_copy_and_returns_counts(self):
        self.assertEqual(self.prepare(TIMETABLE), {"files": 1, "resolved_imports": 1, "unresolved_imports": 1})
        self.assertTrue((self.parent / "repo_catalog.json").is_file())
        self.assertIn(f"- [import] com.org.train.Train (provided by repository {TRAIN}, slug ",
                      (self.dest / "src" / "Model.txt").read_text(encoding="utf-8"))

    def test_unlisted_focal_fails_before_anything_is_written(self):
        with self.assertRaises(ValueError):
            self.prepare(RAIL)
        self.assertFalse((self.parent / "repo_catalog.json").exists())
        self.assertFalse(self.dest.exists())


class TestOrchestratorWiring(unittest.TestCase):
    SRC = (Path(__file__).resolve().parents[1] / "pipelines/orchestrator.py").read_text()

    def test_pipeline_is_defined_and_registered(self):
        tree = ast.parse(self.SRC)
        self.assertIn("single_repo_with_context_pipeline",
                      [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)])
        self.assertIn('"single_repo_with_context"', self.SRC)
        self.assertIn('name="single-repo-with-context-pipeline"', self.SRC)

    def test_existing_pipelines_are_still_registered(self):
        for key in ('"single_repo"', '"multi_repo"', '"data_generation"', '"indexing"', '"analysis"'):
            self.assertIn(key, self.SRC)


class TestRunScriptFlag(unittest.TestCase):
    SH = (Path(__file__).resolve().parents[1] / "scripts/run_pipelines.sh").read_text()

    def test_flag_and_pipeline_name(self):
        self.assertIn('--single-repo-with-context) MODE="single_repo_with_context"', self.SH)
        self.assertIn('trigger_pipeline "single_repo_with_context"', self.SH)
        self.assertIn('MODE="multi"\n', self.SH)  # default unchanged


if __name__ == "__main__":
    unittest.main()
