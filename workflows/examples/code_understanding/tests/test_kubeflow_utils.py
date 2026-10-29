import os
import sys
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Ensure code_understanding package is on sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from utils.kubeflow_utils import (
    inject_secret_as_env,
    read_from_input_artifact,
    write_to_output_artifact,
    use_ephemeral_space,
)


class TestKubeflowUtilsArtifactIO(unittest.TestCase):
    """Unit tests for Kubeflow artifact I/O context managers, specifically read_from_input_artifact."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_sample_tar_gz(self, files_dict: dict) -> str:
        """Helper to create a .tar.gz archive with specified file paths and string contents."""
        archive_path = os.path.join(self.test_dir, "test_input.tar.gz")
        source_dir = os.path.join(self.test_dir, "source_to_pack")
        os.makedirs(source_dir, exist_ok=True)

        for rel_path, content in files_dict.items():
            full_path = os.path.join(source_dir, rel_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)

        with tarfile.open(archive_path, "w:gz") as tar:
            for item in os.listdir(source_dir):
                tar.add(os.path.join(source_dir, item), arcname=item)

        return archive_path

    def test_read_from_input_artifact_extracts_files(self):
        """Verify that read_from_input_artifact extracts all files and cleans up on exit."""
        archive_path = self._create_sample_tar_gz({
            "hello.txt": "Hello from input artifact!",
            "nested/config.json": '{"status": "ok"}',
        })

        artifact = MagicMock()
        artifact.path = archive_path

        extracted_dir = None
        with read_from_input_artifact(artifact) as tmp:
            extracted_dir = tmp
            self.assertTrue(os.path.isdir(tmp))
            self.assertTrue(os.path.exists(os.path.join(tmp, "hello.txt")))
            self.assertTrue(os.path.exists(os.path.join(tmp, "nested", "config.json")))

            with open(os.path.join(tmp, "hello.txt"), "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "Hello from input artifact!")

            with open(os.path.join(tmp, "nested", "config.json"), "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), '{"status": "ok"}')

        # Verify that temporary directory was deleted after context manager exited
        self.assertFalse(os.path.exists(extracted_dir))

    def test_read_from_input_artifact_cleans_up_on_exception(self):
        """Verify that temporary directory is removed even when an exception is raised inside the context."""
        archive_path = self._create_sample_tar_gz({"dummy.txt": "data"})
        artifact = MagicMock(path=archive_path)

        extracted_dir = None
        with self.assertRaises(RuntimeError):
            with read_from_input_artifact(artifact) as tmp:
                extracted_dir = tmp
                self.assertTrue(os.path.exists(tmp))
                raise RuntimeError("Simulated processing failure")

        self.assertIsNotNone(extracted_dir)
        self.assertFalse(os.path.exists(extracted_dir))

    def test_read_from_input_artifact_tar_filter_branch(self):
        """Verify that filter='tar' is used when tarfile.tar_filter is present."""
        archive_path = self._create_sample_tar_gz({"file.txt": "test"})
        artifact = MagicMock(path=archive_path)

        orig_tar_filter = getattr(tarfile, "tar_filter", None)
        try:
            tarfile.tar_filter = lambda *args: None

            with patch("tarfile.open") as mock_open:
                mock_tar = MagicMock()
                mock_open.return_value.__enter__.return_value = mock_tar

                with read_from_input_artifact(artifact) as tmp:
                    mock_tar.extractall.assert_called_once_with(tmp, filter="tar")
        finally:
            if orig_tar_filter is not None:
                tarfile.tar_filter = orig_tar_filter
            elif hasattr(tarfile, "tar_filter"):
                delattr(tarfile, "tar_filter")

    def test_read_from_input_artifact_fully_trusted_filter_branch(self):
        """Verify that filter='fully_trusted' is used when tar_filter is absent but fully_trusted_filter is present."""
        archive_path = self._create_sample_tar_gz({"file.txt": "test"})
        artifact = MagicMock(path=archive_path)

        orig_tar_filter = getattr(tarfile, "tar_filter", None)
        orig_trusted = getattr(tarfile, "fully_trusted_filter", None)
        try:
            if hasattr(tarfile, "tar_filter"):
                delattr(tarfile, "tar_filter")
            tarfile.fully_trusted_filter = lambda *args: None

            with patch("tarfile.open") as mock_open:
                mock_tar = MagicMock()
                mock_open.return_value.__enter__.return_value = mock_tar

                with read_from_input_artifact(artifact) as tmp:
                    mock_tar.extractall.assert_called_once_with(tmp, filter="fully_trusted")
        finally:
            if orig_tar_filter is not None:
                tarfile.tar_filter = orig_tar_filter
            if orig_trusted is not None:
                tarfile.fully_trusted_filter = orig_trusted
            elif hasattr(tarfile, "fully_trusted_filter"):
                delattr(tarfile, "fully_trusted_filter")

    def test_read_from_input_artifact_legacy_fallback_branch(self):
        """Verify that tar.extractall(tmp) is called with no filter when neither filter attribute exists."""
        archive_path = self._create_sample_tar_gz({"file.txt": "test"})
        artifact = MagicMock(path=archive_path)

        orig_tar_filter = getattr(tarfile, "tar_filter", None)
        orig_trusted = getattr(tarfile, "fully_trusted_filter", None)
        try:
            if hasattr(tarfile, "tar_filter"):
                delattr(tarfile, "tar_filter")
            if hasattr(tarfile, "fully_trusted_filter"):
                delattr(tarfile, "fully_trusted_filter")

            with patch("tarfile.open") as mock_open:
                mock_tar = MagicMock()
                mock_open.return_value.__enter__.return_value = mock_tar

                with read_from_input_artifact(artifact) as tmp:
                    mock_tar.extractall.assert_called_once_with(tmp)
        finally:
            if orig_tar_filter is not None:
                tarfile.tar_filter = orig_tar_filter
            if orig_trusted is not None:
                tarfile.fully_trusted_filter = orig_trusted

    def test_roundtrip_write_and_read_output_artifact(self):
        """Verify that write_to_output_artifact followed by read_from_input_artifact preserves data."""
        archive_path = os.path.join(self.test_dir, "roundtrip.tar.gz")
        out_artifact = MagicMock(path=archive_path)

        with write_to_output_artifact(out_artifact) as out_tmp:
            with open(os.path.join(out_tmp, "result.txt"), "w", encoding="utf-8") as f:
                f.write("Output pipeline result")
            sub = os.path.join(out_tmp, "models")
            os.makedirs(sub, exist_ok=True)
            with open(os.path.join(sub, "weights.meta"), "w", encoding="utf-8") as f:
                f.write("meta: v1.0")

        self.assertTrue(os.path.exists(archive_path))

        in_artifact = MagicMock(path=archive_path)
        with read_from_input_artifact(in_artifact) as in_tmp:
            self.assertTrue(os.path.exists(os.path.join(in_tmp, "result.txt")))
            with open(os.path.join(in_tmp, "result.txt"), "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "Output pipeline result")

            self.assertTrue(os.path.exists(os.path.join(in_tmp, "models", "weights.meta")))
            with open(os.path.join(in_tmp, "models", "weights.meta"), "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "meta: v1.0")

    def test_use_ephemeral_space(self):
        """Verify that use_ephemeral_space yields a temporary directory that is deleted on exit."""
        ephemeral_dir = None
        with use_ephemeral_space() as tmp:
            ephemeral_dir = tmp
            self.assertTrue(os.path.isdir(tmp))
            test_file = os.path.join(tmp, "scratch.txt")
            with open(test_file, "w", encoding="utf-8") as f:
                f.write("temporary data")
            self.assertTrue(os.path.exists(test_file))

        self.assertFalse(os.path.exists(ephemeral_dir))


class TestOfflineImageBakedDependencies(unittest.TestCase):
    """Verify all Python dependencies and local packages are baked into images with no runtime pip install."""

    REPO_ROOT = os.path.abspath(os.path.join(BASE_DIR, "../../.."))
    IMAGE_NAMES = ("data-generation", "data-indexing", "data-analysis", "pipeline-tools")
    REQUIRED_PYPROJECT_DEPS = (
        "setuptools>=64",
        "wheel",
        "packaging",
        "pip-requirements-parser>=32,<33",
        "pathspec>=1.1.1,<2",
        "litellm",
    )

    def test_all_kfp_components_disable_runtime_pip_installs(self):
        """Verify every @dsl.component sets install_kfp_package=False and omits packages_to_install."""
        import ast

        kfp_files = (
            os.path.join(BASE_DIR, "pipelines", "kubeflow", "data_generation.py"),
            os.path.join(BASE_DIR, "pipelines", "kubeflow", "indexing.py"),
            os.path.join(BASE_DIR, "pipelines", "kubeflow", "analysis.py"),
        )
        total_components = 0
        for file_path in kfp_files:
            with open(file_path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=file_path)

            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                for dec in node.decorator_list:
                    if (
                        isinstance(dec, ast.Call)
                        and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr == "component"
                    ):
                        total_components += 1
                        kw_map = {kw.arg: kw.value for kw in dec.keywords}
                        self.assertNotIn(
                            "packages_to_install",
                            kw_map,
                            f"{node.name} in {os.path.basename(file_path)} must not use packages_to_install at runtime",
                        )
                        self.assertIn(
                            "install_kfp_package",
                            kw_map,
                            f"{node.name} in {os.path.basename(file_path)} must specify install_kfp_package=False",
                        )
                        self.assertIsInstance(kw_map["install_kfp_package"], ast.Constant)
                        self.assertFalse(
                            kw_map["install_kfp_package"].value,
                            f"{node.name} in {os.path.basename(file_path)} must set install_kfp_package=False",
                        )
        self.assertEqual(total_components, 9)

    def test_all_image_requirements_include_pyproject_dependencies(self):
        """Verify each image's requirements.txt includes all pyproject.toml build and runtime dependencies."""
        for image in self.IMAGE_NAMES:
            req_path = os.path.join(self.REPO_ROOT, "resources", "images", image, "requirements.txt")
            with open(req_path, "r", encoding="utf-8") as f:
                lines = {line.strip() for line in f if line.strip() and not line.strip().startswith("#")}
            for dep in self.REQUIRED_PYPROJECT_DEPS:
                self.assertIn(
                    dep,
                    lines,
                    f"Missing '{dep}' in resources/images/{image}/requirements.txt",
                )
            self.assertIn(
                "kfp==2.17.0",
                lines,
                f"Missing 'kfp==2.17.0' in resources/images/{image}/requirements.txt",
            )

    def test_all_containerfiles_bake_code_understanding_offline(self):
        """Verify each Containerfile copies workflows/examples/code_understanding and installs with --no-build-isolation --no-deps."""
        for image in self.IMAGE_NAMES:
            cf_path = os.path.join(self.REPO_ROOT, "resources", "images", image, "Containerfile")
            with open(cf_path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn(
                f"resources/images/{image}/requirements.txt",
                content,
                f"{image}/Containerfile must copy requirements.txt from repo-root build context",
            )
            self.assertIn(
                "COPY --chown=1001:0 workflows/examples/code_understanding ./workflows/examples/code_understanding",
                content,
                f"{image}/Containerfile must copy workflows/examples/code_understanding into the image",
            )
            self.assertIn(
                "pip install --no-build-isolation --no-deps ./workflows/examples/code_understanding",
                content,
                f"{image}/Containerfile must install code_understanding with --no-build-isolation --no-deps",
            )

    def test_inject_secret_as_env_sets_image_pull_policy_always(self):
        """Verify inject_secret_as_env sets imagePullPolicy to Always on the component task."""
        mock_task = MagicMock()
        mock_kfp_kubernetes = MagicMock()
        mock_kfp_module = MagicMock()
        mock_kfp_module.kubernetes = mock_kfp_kubernetes

        @inject_secret_as_env(secret_name="code-understanding-env")
        def sample_component():
            return mock_task

        with patch.dict(sys.modules, {"kfp": mock_kfp_module, "kfp.kubernetes": mock_kfp_kubernetes}):
            result = sample_component()

        self.assertIs(result, mock_task)
        mock_kfp_kubernetes.set_image_pull_policy.assert_called_once_with(mock_task, "Always")
        mock_kfp_kubernetes.use_config_map_as_volume.assert_called_once_with(
            mock_task,
            config_map_name="odh-trusted-ca-bundle",
            mount_path="/etc/pki/ca-trust/extracted/pem/",
        )


if __name__ == "__main__":
    unittest.main()
