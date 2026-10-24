"""A model answer's relevant_files reach the report with a verdict beside each.

The hook runs after `query_with_llm` returns, so it checks rather than
replaces: the model's list stays as written and `provenance.relevant_files`
adds `relevant_files_verification` next to it.
"""

import asyncio
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# The container-only dependencies graphrag_utils imports, stubbed as the other
# analyzer tests do.
for pkg_name in [
    "graphrag", "graphrag.api", "graphrag.config", "graphrag.config.load_config",
    "mlflow", "mlflow.tracking", "mlflow.metrics", "mlflow.metrics.genai",
    "requests", "deepeval", "pyvis", "pyvis.network", "networkx", "matplotlib",
    "matplotlib.pyplot", "litellm", "lancedb", "nest_asyncio",
]:
    if pkg_name not in sys.modules:
        stub = MagicMock()
        stub.__path__ = []
        sys.modules[pkg_name] = stub

# provenance.corpus reads a real frame; see tests/conftest.py.
REAL_DATAFRAMES = True

INDEXED = ("celery/app/base.py", "celery/worker/__init__.py")
HEADER = ('"""This file is located at {path} from repository url https://github.com/celery/celery, '
          'repository slug celery-v2-5-5, multi_repo false"""\nimport os\n')
META = {"title": "### Code Migration Plan (JSON)", "key": "migration-plan-json"}
ANSWER = ('```json\n{"code_migration_plan": [{"component": "Pool", "relevant_files": '
          '["celery/worker/__init__.py", "setup.py", "celery/worker/task.py"]}]}\n```')


class TestReportRelevantFiles(unittest.TestCase):

    def setUp(self):
        import pandas as pd

        from utils import graphrag_utils
        from utils.graphrag_utils import DependencyAnalyzer

        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.work = Path(self.root.name)
        (self.work / "output").mkdir()
        (self.work / "output" / "documents.parquet").touch()
        self.checkout = self.work / "repo"
        for path in (*INDEXED, "setup.py"):
            (self.checkout / path).parent.mkdir(parents=True, exist_ok=True)
            (self.checkout / path).write_text("")

        documents = pd.DataFrame({"title": list(INDEXED),
                                  "text": [HEADER.format(path=p) for p in INDEXED]})
        self.read_parquet = MagicMock(return_value=documents)
        patcher = patch.object(graphrag_utils, "pd", types.SimpleNamespace(read_parquet=self.read_parquet))
        patcher.start()
        self.addCleanup(patcher.stop)

        with patch.object(DependencyAnalyzer, "_setup_search"):
            self.analyzer = DependencyAnalyzer(str(self.work), git_slug="celery-v2-5-5")
        self.analyzer.multi_repo = False

        async def query(prompt, **kwargs):
            return ANSWER

        self.analyzer.query_with_llm = query
        self.analyzer._extract_indexed_git_urls = MagicMock(return_value=["https://github.com/celery/celery"])

    def report(self):
        from loaders.default_asset_loader import DefaultAssetLoader

        with patch.object(DefaultAssetLoader, "num_prompts",
                          side_effect=lambda path: 0 if "enhanced" in path else 1), \
             patch.object(DefaultAssetLoader, "download_prompt", return_value=("plan?", META)):
            return asyncio.run(self.analyzer.generate_migration_report())

    def test_verdicts_against_the_checkout(self):
        self.analyzer.checkout_dir = str(self.checkout)
        report = self.report()
        self.assertIn('"relevant_files": [\n                "celery/worker/__init__.py",', report)
        for expected in ('"path": "celery/worker/__init__.py",\n                    "verdict": "confirmed"',
                         '"path": "setup.py",\n                    "verdict": "not-indexed"',
                         '"verdict": "matches-nothing",\n                    "shape": "repo-path"'):
            self.assertIn(expected, report)
        self.assertIn("checked against the index and the checkout: 3 entries, "
                      "1 confirmed, 1 not-indexed, 1 matches-nothing", report)

    def test_without_a_checkout_nothing_is_called_invented(self):
        with patch.dict(os.environ, {"PARENT_SOURCE_PATH": str(self.work / "absent")}):
            report = self.report()
        self.assertNotIn("matches-nothing", report)
        self.assertIn("checked against the index only; no checkout: 3 entries, "
                      "1 confirmed, 2 not-confirmed", report)

    def test_the_checkout_is_found_under_parent_source_path(self):
        (self.work / "src").mkdir()
        self.checkout.rename(self.work / "src" / "celery-v2-5-5")
        with patch.dict(os.environ, {"PARENT_SOURCE_PATH": str(self.work / "src")}):
            self.assertIn("1 not-indexed", self.report())

    def test_evidence_is_built_once(self):
        self.analyzer.checkout_dir = str(self.checkout)
        self.analyzer._check_answer(ANSWER)
        self.analyzer._check_answer(ANSWER)
        self.read_parquet.assert_called_once_with(f"{self.work}/output/documents.parquet")

    def test_answers_without_the_field_pass_through_untouched(self):
        self.assertEqual("prose answer", self.analyzer._check_answer("prose answer"))
        self.read_parquet.assert_not_called()


if __name__ == "__main__":
    unittest.main()
