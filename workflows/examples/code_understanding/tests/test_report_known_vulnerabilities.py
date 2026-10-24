"""The Known Vulnerabilities section: the model's answer, then the joins.

`append:` in a prompt's front matter adds deterministic content after the
model answer rather than replacing it, the way `render:` does.
"""

import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from provenance import facts as facts_module
from provenance import pipeline, vulns
from provenance.models import Layer, Package, RepoFacts

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

HEADER = ('"""This file is located at celery/app/base.py from repository url '
          'https://github.com/celery/celery, repository slug celery-v2-5-5, multi_repo false, '
          'runtime Python 2.7"""\nimport os\n')
META = {"title": "### Known Vulnerabilities", "key": "vulnerabilities",
        "append": "known-vulnerabilities"}
ANSWER = "pickle.loads on broker input in celery/app/base.py. Also CVE-2011-4356."
SNAPSHOT = {"format": vulns.SNAPSHOT_FORMAT, "built": "2026-10-23T06:30:15Z", "schema": "v6.1.10", "ranges": [
    {"ecosystem": "python", "name": "kombu", "ranges": [
        {"advisory": "GHSA-aaaa-bbbb-cccc", "constraint": "<2.2.0", "fix_state": "fixed",
         "fixed": "2.2.0", "aliases": ["CVE-2020-1234"], "severity": None}]}]}


class TestReportKnownVulnerabilities(unittest.TestCase):

    def setUp(self):
        import pandas as pd

        from utils import graphrag_utils
        from utils.graphrag_utils import DependencyAnalyzer

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name)
        (self.work / "output").mkdir()
        (self.work / "output" / "documents.parquet").touch()
        self.snapshot = self.work / vulns.SNAPSHOT_FILENAME
        self.snapshot.write_text(json.dumps(SNAPSHOT))

        documents = pd.DataFrame({"title": ["celery/app/base.py"], "text": [HEADER]})
        patcher = patch.object(graphrag_utils, "pd",
                               types.SimpleNamespace(read_parquet=MagicMock(return_value=documents)))
        patcher.start()
        self.addCleanup(patcher.stop)

        with patch.object(DependencyAnalyzer, "_setup_search"):
            self.analyzer = DependencyAnalyzer(str(self.work), git_slug="celery-v2-5-5")
        self.analyzer.multi_repo = False

        async def query(prompt, **kwargs):
            return ANSWER

        self.analyzer.query_with_llm = query
        self.analyzer._extract_indexed_git_urls = MagicMock(return_value=[])

    def write_facts(self):
        kombu = Package(name="kombu", ecosystem="pypi", layer=Layer.PACKAGE,
                        source_file="requirements/default.txt", constraint=">=2.1.8,<2.2.0",
                        declared_line="kombu>=2.1.8,<2.2.0")
        facts_module.write(RepoFacts(root="/repo", packages=[kombu]),
                           self.work / "output" / pipeline.FACTS_FILENAME)

    def report(self, snapshot=True):
        from loaders.default_asset_loader import DefaultAssetLoader

        env = {vulns.SNAPSHOT_ENV: str(self.snapshot)} if snapshot else {}
        with patch.dict(os.environ, env), \
             patch.object(DefaultAssetLoader, "num_prompts",
                          side_effect=lambda path: 0 if "enhanced" in path else 1), \
             patch.object(DefaultAssetLoader, "download_prompt", return_value=("q?", META)), \
             patch.object(DefaultAssetLoader, "download", return_value=None):
            return asyncio.run(self.analyzer.generate_migration_report())

    def test_the_joins_follow_the_model_answer(self):
        self.write_facts()
        report = self.report()
        self.assertLess(report.index(ANSWER), report.index("#### Declared dependencies"))
        self.assertIn("| `kombu>=2.1.8,<2.2.0` | `requirements/default.txt` | all-affected "
                      "| GHSA-aaaa-bbbb-cccc (CVE-2020-1234)", report)
        self.assertIn("| Python 2.7 | Python (upstream) | 2020-01-01 | ended |", report)
        self.assertIn("names CVE-2011-4356, which the advisory join did not produce", report)

    def test_missing_inputs_are_reported_not_dropped(self):
        report = self.report(snapshot=False)
        self.assertIn("Not assessed: no dependency facts", report)
        self.write_facts()
        self.assertIn("Not assessed: no advisory snapshot", self.report(snapshot=False))

    def test_missing_documents_and_table_degrade_rather_than_crash(self):
        from provenance import eol
        from utils import graphrag_utils

        self.write_facts()
        with patch.dict(os.environ, {vulns.SNAPSHOT_ENV: str(self.snapshot)}), \
             patch.object(graphrag_utils.os.path, "exists", return_value=False), \
             patch.object(eol, "load", side_effect=OSError("gone")):
            block = self.analyzer._append_section("known-vulnerabilities", ANSWER, "3")
        self.assertIn("all-affected", block)
        self.assertIn("Not assessed: the lifecycle table could not be read", block)

    def test_an_unknown_append_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "only one is 'known-vulnerabilities'"):
            self.analyzer._append_section("cves", ANSWER, "analysis/migration-report/3")


if __name__ == "__main__":
    unittest.main()
