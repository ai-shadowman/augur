import logging
import os
import sys
import tempfile
import unittest

# Ensure code_understanding package is on sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from utils.kubeflow_utils import get_logger, setup_logging


class TestUnifiedLogging(unittest.TestCase):
    """Tests for unified console logging and file handler prevention."""

    def setUp(self):
        self._orig_level = os.environ.get("LOGLEVEL")

    def tearDown(self):
        if self._orig_level is not None:
            os.environ["LOGLEVEL"] = self._orig_level
        else:
            os.environ.pop("LOGLEVEL", None)
        setup_logging(level="INFO")

    def test_setup_logging_configures_stdout_stream_handler(self):
        """Verify root logger has a StreamHandler pointing to sys.stdout."""
        setup_logging(level="INFO")
        root = logging.getLogger()
        self.assertEqual(len(root.handlers), 1)
        handler = root.handlers[0]
        self.assertIsInstance(handler, logging.StreamHandler)
        self.assertIs(handler.stream, sys.stdout)

    def test_setup_logging_removes_file_handlers(self):
        """Verify existing FileHandlers on root and child loggers are removed."""
        root = logging.getLogger()
        child = logging.getLogger("test_child_logger")

        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name

        try:
            fh_root = logging.FileHandler(tmp_path)
            fh_child = logging.FileHandler(tmp_path)
            root.addHandler(fh_root)
            child.addHandler(fh_child)

            self.assertTrue(any(isinstance(h, logging.FileHandler) for h in root.handlers))
            self.assertTrue(any(isinstance(h, logging.FileHandler) for h in child.handlers))

            setup_logging()

            self.assertFalse(any(isinstance(h, logging.FileHandler) for h in root.handlers))
            self.assertFalse(any(isinstance(h, logging.FileHandler) for h in child.handlers))
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_setup_logging_respects_env_loglevel(self):
        """Verify LOGLEVEL env var sets the logger level."""
        os.environ["LOGLEVEL"] = "DEBUG"
        setup_logging()
        self.assertEqual(logging.getLogger().level, logging.DEBUG)

        os.environ["LOGLEVEL"] = "WARNING"
        setup_logging()
        self.assertEqual(logging.getLogger().level, logging.WARNING)

    def test_setup_logging_explicit_level_override(self):
        """Verify passing an explicit level parameter overrides LOGLEVEL env var."""
        os.environ["LOGLEVEL"] = "INFO"
        setup_logging(level="ERROR")
        self.assertEqual(logging.getLogger().level, logging.ERROR)

    def test_get_logger_returns_configured_logger(self):
        """Verify get_logger returns a child logger that outputs formatted logs."""
        logger = get_logger("test_module")
        self.assertEqual(logger.name, "test_module")

        with self.assertLogs("test_module", level="INFO") as cm:
            logger.info("Test unified console output")
        self.assertTrue(any("Test unified console output" in msg for msg in cm.output))


if __name__ == "__main__":
    unittest.main()
