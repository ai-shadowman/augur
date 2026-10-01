import builtins
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class _Column(list):
    def tolist(self):
        return list(self)


class _DataFrame:
    def __init__(self, records):
        self.records = records

    def __getitem__(self, key):
        return _Column(record[key] for record in self.records)


if "pygments" not in sys.modules:
    pygments = types.ModuleType("pygments")
    lexers = types.ModuleType("pygments.lexers")
    lexers.guess_lexer_for_filename = lambda *_: None
    util = types.ModuleType("pygments.util")
    util.ClassNotFound = type("ClassNotFound", (Exception,), {})
    sys.modules.update({"pygments": pygments, "pygments.lexers": lexers, "pygments.util": util})

if "pandas" not in sys.modules:
    pandas = MagicMock()
    pandas.__path__ = []
    sys.modules["pandas"] = pandas

if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None
    sys.modules["dotenv"] = dotenv

if "loaders.default_asset_loader" not in sys.modules:
    loader_module = types.ModuleType("loaders.default_asset_loader")

    class _DefaultAssetLoader:
        RESULTS_PATH_PREFIX_PIPELINES = "results/pipelines"
        RESULTS_PATH_PREFIX_VISUALIZATIONS = "results/visualizations"

        def download(self, asset_file_path):
            asset = Path(__file__).parents[1] / "assets" / asset_file_path
            return json.loads(asset.read_text(encoding="utf-8"))

        def download_dir(self, *args, **kwargs):
            return None

        def log_results(self, *args, **kwargs):
            return None

        def num_prompts(self, *args, **kwargs):
            return 0

        def download_prompt(self, *args, **kwargs):
            return "", {}

        @staticmethod
        def get_log_results_artifact_path(*args, **kwargs):
            return "test"

    loader_module.DefaultAssetLoader = _DefaultAssetLoader
    sys.modules["loaders.default_asset_loader"] = loader_module
    import loaders
    loaders.default_asset_loader = loader_module

from pipelines.base import data_generation
from utils import code_utils
from utils.repository_ignore import RepositoryIgnoreError, RepositoryIgnorePolicy


class TestAugurIgnoreKubeflowFailure(unittest.TestCase):
    def test_repository_ignore_error_is_fatal_in_multi_repo_mode(self):
        self.assertTrue(
            data_generation.should_reraise_processing_error(
                RepositoryIgnoreError("invalid .augurignore"), multi_repo=True
            )
        )
        self.assertTrue(
            data_generation.should_reraise_processing_error(RuntimeError("failure"), multi_repo=False)
        )

    def test_unrelated_error_remains_skippable_in_multi_repo_mode(self):
        self.assertFalse(
            data_generation.should_reraise_processing_error(RuntimeError("failure"), multi_repo=True)
        )


class TestAugurIgnoreDataGeneration(unittest.TestCase):
    def repository(self, rules=""):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        if rules:
            (root / ".augurignore").write_text(rules, encoding="utf-8")
        self.addCleanup(directory.cleanup)
        return root

    def test_language_detection_prunes_ignored_directories_and_files(self):
        root = self.repository("private/\n*.secret\n")
        (root / "private").mkdir()
        (root / "private" / "hidden.py").write_text("", encoding="utf-8")
        (root / "visible.py").write_text("", encoding="utf-8")
        (root / "token.secret").write_text("", encoding="utf-8")
        policy = RepositoryIgnorePolicy.from_repository(root)

        with patch("utils.code_utils.guess_lexer_for_filename") as lexer:
            lexer.return_value.name = "Python"
            self.assertEqual(
                code_utils.get_detected_languages_for_repo(str(root), policy), ["python"]
            )
            self.assertEqual(lexer.call_args_list[0].args[0], "visible.py")
            self.assertEqual(lexer.call_count, 1)

    def test_raw_dataset_never_stats_or_opens_ignored_files(self):
        root = self.repository("hidden.py\n")
        (root / "hidden.py").write_text("secret", encoding="utf-8")
        (root / "visible.py").write_text("visible", encoding="utf-8")
        policy = RepositoryIgnorePolicy.from_repository(root)
        real_open = builtins.open
        seen = []

        def recording_open(path, *args, **kwargs):
            seen.append(os.fspath(path))
            return real_open(path, *args, **kwargs)

        with patch("utils.code_utils.is_large_code_file", return_value=False), patch(
            "builtins.open", side_effect=recording_open
        ), patch("os.path.getsize", wraps=os.path.getsize) as getsize, patch(
            "pandas.DataFrame", _DataFrame
        ):
            dataframe = data_generation.generate_raw_dataset(
                str(root), str(root / "target"), "org/repo", "main", ignore_policy=policy
            )

        self.assertEqual(dataframe["file_path"].tolist(), ["visible.py"])
        self.assertNotIn(str(root / "hidden.py"), seen)
        self.assertNotIn(str(root / "hidden.py"), [call.args[0] for call in getsize.call_args_list])

    def test_external_metadata_skips_ignored_json_and_prunes_ignored_directories(self):
        root = self.repository(".code_metadata/private/\n.code_metadata/hidden.json\n")
        metadata = root / ".code_metadata"
        (metadata / "private").mkdir(parents=True)
        (metadata / "kept").mkdir()
        (metadata / "private" / "hidden.json").write_text('{"private": true}', encoding="utf-8")
        (metadata / "hidden.json").write_text('{"hidden": true}', encoding="utf-8")
        (metadata / "kept" / "kept.json").write_text('{"kept": true}', encoding="utf-8")

        self.assertEqual(
            data_generation.load_external_data(str(root), RepositoryIgnorePolicy.from_repository(root)),
            {"kept": True},
        )

    def test_absent_augurignore_preserves_existing_builtin_exclusions(self):
        root = self.repository()
        (root / ".code_metadata").mkdir()
        (root / ".code_metadata" / "metadata.py").write_text("", encoding="utf-8")
        (root / "visible.py").write_text("", encoding="utf-8")
        policy = RepositoryIgnorePolicy.from_repository(root)

        with patch("utils.code_utils.guess_lexer_for_filename") as lexer:
            lexer.return_value.name = "Python"
            self.assertEqual(code_utils.get_detected_languages_for_repo(str(root), policy), ["python"])
            self.assertEqual(lexer.call_count, 1)

    def test_one_policy_is_reused_across_language_and_config_passes(self):
        root = self.repository("ignored/\n")
        policy = RepositoryIgnorePolicy.from_repository(root)
        with patch("pipelines.base.data_generation.prepare_environment"), patch(
            "pipelines.base.data_generation.detect_languages", return_value=["python"]
        ) as detect, patch("pipelines.base.data_generation.load_external_data", return_value={}) as metadata, patch(
            "pipelines.base.data_generation.generate_code_and_meta"
        ) as generate, patch("pipelines.base.data_generation.RepositoryIgnorePolicy.from_repository", return_value=policy) as load:
            result = data_generation.DataGenerationPipeline().run("org/repo", "main", str(root), str(root / "out"))

        self.assertEqual(result["status"], "complete")
        self.assertEqual(load.call_count, 1)
        self.assertIs(detect.call_args.args[1], policy)
        self.assertIs(metadata.call_args.args[1], policy)
        self.assertTrue(all(call.kwargs["ignore_policy"] is policy for call in generate.call_args_list))

    def test_separate_repositories_do_not_share_rules_or_stats(self):
        first = self.repository("private/\n")
        second = self.repository("*.secret\n")
        first_policy = RepositoryIgnorePolicy.from_repository(first)
        second_policy = RepositoryIgnorePolicy.from_repository(second)

        first_policy.filter_directories(first, ["private"])
        second_policy.filter_files(second, ["token.secret"])
        self.assertEqual(first_policy.stats.ignored_directories, 1)
        self.assertEqual(first_policy.stats.ignored_files, 0)
        self.assertEqual(second_policy.stats.ignored_directories, 0)
        self.assertEqual(second_policy.stats.ignored_files, 1)

    def test_all_analyzable_content_excluded_raises_clear_error(self):
        root = self.repository("*.py\n")
        (root / "only.py").write_text("", encoding="utf-8")
        with self.assertRaisesRegex(RepositoryIgnoreError, "No supported source or configuration files remain"):
            data_generation.detect_languages(str(root), RepositoryIgnorePolicy.from_repository(root))
