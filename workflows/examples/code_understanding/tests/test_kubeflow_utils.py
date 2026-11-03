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

    def test_inject_secret_as_env_sets_image_pull_policy_default_if_not_present(self):
        """Verify inject_secret_as_env defaults imagePullPolicy to IfNotPresent when IMAGE_PULL_POLICY is unset."""
        mock_task = MagicMock()
        mock_kfp_kubernetes = MagicMock()
        mock_kfp_module = MagicMock()
        mock_kfp_module.kubernetes = mock_kfp_kubernetes

        @inject_secret_as_env(secret_name="code-understanding-env")
        def sample_component():
            return mock_task

        env_without_policy = {k: v for k, v in os.environ.items() if k != "IMAGE_PULL_POLICY"}
        with patch.dict(os.environ, env_without_policy, clear=True):
            with patch.dict(sys.modules, {"kfp": mock_kfp_module, "kfp.kubernetes": mock_kfp_kubernetes}):
                result = sample_component()

        self.assertIs(result, mock_task)
        mock_kfp_kubernetes.set_image_pull_policy.assert_called_once_with(mock_task, "IfNotPresent")
        mock_kfp_kubernetes.use_config_map_as_volume.assert_called_once_with(
            mock_task,
            config_map_name="odh-trusted-ca-bundle",
            mount_path="/etc/pki/ca-trust/extracted/pem/",
        )
        mock_task.set_env_variable.assert_any_call("KFP_RUN_ID", "{{$.pipeline_job_uuid}}")
        mock_task.set_env_variable.assert_any_call("KFP_RUN_NAME", "{{$.pipeline_job_name}}")
        mock_task.set_env_variable.assert_any_call("KFP_TASK_NAME", "{{$.pipeline_task_name}}")
        mock_task.set_env_variable.assert_any_call(
            "OTEL_RESOURCE_ATTRIBUTES",
            "kfp.run_id={{$.pipeline_job_uuid}},kfp.run_name={{$.pipeline_job_name}},kfp.task_name={{$.pipeline_task_name}}",
        )

    def test_inject_secret_as_env_sets_image_pull_policy_from_env(self):
        """Verify inject_secret_as_env reads IMAGE_PULL_POLICY from environment variables."""
        for policy in ("Always", "IfNotPresent", "Never"):
            with self.subTest(policy=policy):
                mock_task = MagicMock()
                mock_kfp_kubernetes = MagicMock()
                mock_kfp_module = MagicMock()
                mock_kfp_module.kubernetes = mock_kfp_kubernetes

                @inject_secret_as_env(secret_name="code-understanding-env")
                def sample_component():
                    return mock_task

                with patch.dict(os.environ, {"IMAGE_PULL_POLICY": policy}):
                    with patch.dict(sys.modules, {"kfp": mock_kfp_module, "kfp.kubernetes": mock_kfp_kubernetes}):
                        result = sample_component()

                self.assertIs(result, mock_task)
                mock_kfp_kubernetes.set_image_pull_policy.assert_called_once_with(mock_task, policy)


class TestMlFlowCustomTelemetryKfpRunTagging(unittest.TestCase):
    """Verify MlFlowCustomTelemetry tags OTEL_RESOURCE_ATTRIBUTES with KFP run/task metadata."""

    def setUp(self):
        for pkg in ("mlflow", "litellm", "requests"):
            if pkg not in sys.modules:
                sys.modules[pkg] = MagicMock()
        from telemetry.mlflow_custom_telemetry import MlFlowCustomTelemetry
        self.telemetry_cls = MlFlowCustomTelemetry
        self.telemetry_cls.reset()

    def tearDown(self):
        self.telemetry_cls.reset()

    def test_track_populates_otel_resource_attributes_from_kfp_env(self):
        """Verify KFP_RUN_ID, KFP_RUN_NAME, KFP_TASK_NAME, and HOSTNAME are added to OTEL_RESOURCE_ATTRIBUTES."""
        env = {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://code-understanding-collector.otel.svc.cluster.local:4318",
            "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment=dev",
            "KFP_RUN_ID": "a1b2c3d4-5678-90ab-cdef-1234567890ab",
            "KFP_RUN_NAME": "code-understanding-run-42",
            "KFP_TASK_NAME": "graphrag-indexing-op",
            "HOSTNAME": "code-understanding-pipeline-xyz-pod",
        }
        with patch.dict(os.environ, env, clear=True):
            t = self.telemetry_cls()
            t.track()
            resource_attrs = os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
            self.assertIn("deployment.environment=dev", resource_attrs)
            self.assertIn("kfp.run_id=a1b2c3d4-5678-90ab-cdef-1234567890ab", resource_attrs)
            self.assertIn("kfp.run_name=code-understanding-run-42", resource_attrs)
            self.assertIn("kfp.task_name=graphrag-indexing-op", resource_attrs)
            self.assertIn("k8s.pod.name=code-understanding-pipeline-xyz-pod", resource_attrs)
            self.assertEqual(
                os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"),
                "http://code-understanding-collector.otel.svc.cluster.local:4318/v1/traces",
            )

    def test_unexpanded_kfp_placeholders_are_ignored(self):
        """Verify unexpanded '{{$.pipeline_job_uuid}}' placeholders are not written to OTEL_RESOURCE_ATTRIBUTES."""
        env = {
            "KFP_RUN_ID": "{{$.pipeline_job_uuid}}",
            "KFP_RUN_NAME": "{{$.pipeline_job_name}}",
            "KFP_TASK_NAME": "{{$.pipeline_task_name}}",
            "HOSTNAME": "pod-abc",
        }
        with patch.dict(os.environ, env, clear=True):
            meta = self.telemetry_cls._configure_otel_resource_attributes()
            self.assertNotIn("kfp.run_id", meta)
            self.assertNotIn("kfp.run_name", meta)
            self.assertNotIn("kfp.task_name", meta)
            self.assertEqual(meta.get("k8s.pod.name"), "pod-abc")

    def test_enrich_otel_span_extracts_tokens_and_updates_readonly_mappingproxy(self):
        """Verify _enrich_otel_span populates gen_ai.usage.* and human-readable span name on ReadableSpan."""
        import types
        from telemetry.mlflow_custom_telemetry import _enrich_otel_span

        class DummyReadableSpan:
            def __init__(self):
                self._name = "AsyncCompletions"
                self._end_time = 123456789
                self._attributes = types.MappingProxyType(
                    {"mlflow.chat.tokenUsage": '{"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500}'}
                )

            @property
            def attributes(self):
                return self._attributes

        span = DummyReadableSpan()
        _enrich_otel_span(span, {"kfp.run_id": "1ab1269b-8530-4fc9-88a5-a73705208c0a"})
        self.assertEqual(span.attributes["kfp.run_id"], "1ab1269b-8530-4fc9-88a5-a73705208c0a")
        self.assertEqual(span.attributes["gen_ai.usage.input_tokens"], 1200)
        self.assertEqual(span.attributes["gen_ai.usage.output_tokens"], 300)
        self.assertEqual(span.attributes["gen_ai.usage.total_tokens"], 1500)
        self.assertEqual(span._name, "AsyncCompletions (tokens: 1500 | in: 1200, out: 300)")


if __name__ == "__main__":
    unittest.main()
