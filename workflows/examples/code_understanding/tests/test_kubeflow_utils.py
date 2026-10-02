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


if __name__ == "__main__":
    unittest.main()
