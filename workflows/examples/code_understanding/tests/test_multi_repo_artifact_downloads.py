import importlib.util
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from utils.loader_utils import download_code_metadata_directories


class TestMultiRepoArtifactDownloads(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.target = self.root / "target"
        self.repos = [
            {"git_repo": "https://github.com/example/first", "git_branch": "main"},
            {"git_repo": "https://github.com/example/second", "git_branch": "main"},
        ]

        # Existing tests install global loader stubs. Load the real classes in a
        # temporary namespace and replace only the remote MLflow boundary here.
        package = types.ModuleType("_multi_repo_test_loaders")
        package.__path__ = [str(BASE_DIR / "loaders")]
        modules = self.enterContext(patch.dict(sys.modules, {
            "_multi_repo_test_loaders": package,
            "mlflow": MagicMock(),
            "mlflow.tracking": MagicMock(),
            "requests": MagicMock(),
            "yaml": sys.modules.get("yaml") or MagicMock(),
            "pygments": sys.modules.get("pygments") or MagicMock(),
            "pygments.lexers": sys.modules.get("pygments.lexers") or MagicMock(),
            "pygments.util": sys.modules.get("pygments.util") or MagicMock(),
        }))
        from _multi_repo_test_loaders.default_asset_loader import DefaultAssetLoader
        from _multi_repo_test_loaders import mlflow_asset_loader

        modules["loaders.default_asset_loader"] = sys.modules[
            "_multi_repo_test_loaders.default_asset_loader"
        ]
        modules["loaders.mlflow_asset_loader"] = mlflow_asset_loader
        self.enterContext(patch.dict(os.environ, {"ASSET_LOADER": "local"}))
        spec = importlib.util.spec_from_file_location(
            "_multi_repo_test_code_utils", BASE_DIR / "utils" / "code_utils.py"
        )
        code_utils = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(code_utils)
        modules["utils.code_utils"] = code_utils
        self.enterContext(patch.dict(os.environ, {
            "ASSET_LOADER": "mlflow", "MLFLOW_TRACKING_TOKEN": "",
        }))

        self.client = MagicMock()
        self.client.get_experiment_by_name.return_value = types.SimpleNamespace(
            experiment_id="9", lifecycle_stage="active",
            artifact_location="mlflow-artifacts:/workspaces/test/9",
        )
        self.runs = {}
        self.artifacts = {}
        self.client.search_runs.side_effect = self.search_runs
        self.enterContext(patch.object(
            mlflow_asset_loader, "MlflowClient", return_value=self.client,
        ))
        self.enterContext(patch.object(
            mlflow_asset_loader.mlflow.artifacts, "download_artifacts",
            side_effect=self.download_artifacts,
        ))

    def add_generated_repo(self, slug):
        source = self.root / slug
        source.mkdir()
        (source / "code.txt").write_text(f"code for {slug}", encoding="utf-8")
        # Literal tags match the artifacts published by data generation.
        self.runs[slug] = {
            "git_slug": slug, "category": "data-generation",
            "code-metadata": "true", "multi_repo": "True",
        }
        self.artifacts[
            f"mlflow-artifacts:/workspaces/test/9/{slug}/artifacts/"
            f"results/metadata/multi-repo/{slug}"
        ] = source

    def search_runs(self, *, filter_string, **kwargs):
        filters = dict(re.findall(r'tags\."([^"]+)" = \'([^\']*)\'', filter_string))
        return [
            types.SimpleNamespace(info=types.SimpleNamespace(
                artifact_uri=f"mlflow-artifacts:/workspaces/test/9/{slug}/artifacts"
            ))
            for slug, tags in self.runs.items()
            if all(tags.get(key) == value for key, value in filters.items())
        ][:1]

    def download_artifacts(self, *, artifact_uri):
        normalized_uri = artifact_uri.replace("\\", "/")
        if normalized_uri not in self.artifacts:
            raise FileNotFoundError(f"No such artifact: {artifact_uri}")
        return str(self.artifacts[normalized_uri])

    def test_downloads_generated_artifacts_for_every_requested_repo(self):
        for slug in ("example-first-main", "example-second-main"):
            self.add_generated_repo(slug)

        download_code_metadata_directories(self.repos, str(self.target))

        for slug in ("example-first-main", "example-second-main"):
            self.assertEqual(
                (self.target / slug / "code.txt").read_text(encoding="utf-8"),
                f"code for {slug}",
            )

    def test_missing_repo_stops_download_with_repo_context_and_original_cause(self):
        with self.assertRaises(RuntimeError) as raised:
            download_code_metadata_directories(self.repos, str(self.target))

        self.assertIn("https://github.com/example/first", str(raised.exception))
        self.assertIn("example-first-main", str(raised.exception))
        self.assertIsInstance(raised.exception.__cause__, FileNotFoundError)
        self.assertFalse(self.target.exists())

    def test_missing_second_repo_does_not_silently_accept_a_partial_index(self):
        self.add_generated_repo("example-first-main")

        with self.assertRaisesRegex(RuntimeError, "example-second-main"):
            download_code_metadata_directories(self.repos, str(self.target))

        self.assertTrue((self.target / "example-first-main" / "code.txt").is_file())

    def test_empty_repo_list_is_rejected_before_indexing(self):
        with self.assertRaisesRegex(ValueError, "repositories"):
            download_code_metadata_directories([], str(self.target))


if __name__ == "__main__":
    unittest.main()
