"""A prompt with `render:` in its front matter is built from the index, not asked.

`migration-report/0.txt` is the first: its slot, title and key stay so the
report keeps its shape, and the Mermaid block comes from
`provenance.dependency_graph` with no model call.
"""

import asyncio
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# The container-only dependencies graphrag_utils imports, stubbed as the other
# analyzer tests do. pandas is left alone: the renderer reads real frames.
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

REAL_DATAFRAMES = True

GRAPH_META = {"title": "### Dependency Graph", "key": "dependency-graph",
              "render": "dependency-graph"}
HEADER = ('"""This file is located at {path} from repository url {repo}, '
          'repository slug s, multi_repo true"""\nimport os\n')


class TestRenderedSections(unittest.TestCase):

    def setUp(self):
        import pandas as pd

        from utils.graphrag_utils import DependencyAnalyzer

        self.pd = pd
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        os.makedirs(os.path.join(self.root.name, "output"))
        # _setup_search is the hook that reads the index; the frames are set below.
        with patch.object(DependencyAnalyzer, "_setup_search"):
            self.analyzer = DependencyAnalyzer(self.root.name, git_slug="test-repo_main")
        self.analyzer.entity_df = pd.DataFrame(
            [("CELERY.APP", "PACKAGE"), ("CELERY/BEAT.PY", "MODULE")], columns=["title", "type"])
        self.analyzer.relationship_df = pd.DataFrame(
            [("CELERY/BEAT.PY", "CELERY.APP")], columns=["source", "target"])
        self.analyzer.multi_repo = False
        self.model_prompts = []

        async def query(prompt, **kwargs):
            self.model_prompts.append(prompt)
            return "model answer"

        self.analyzer.query_with_llm = query
        self.analyzer._extract_indexed_git_urls = MagicMock(return_value=["https://github.com/x/y"])

    def test_the_graph_section_is_rendered_and_the_model_is_not_called(self):
        from loaders.default_asset_loader import DefaultAssetLoader

        with patch.object(DefaultAssetLoader, "num_prompts",
                          side_effect=lambda path: 0 if "enhanced" in path else 1), \
             patch.object(DefaultAssetLoader, "download_prompt", return_value=("", GRAPH_META)):
            report = asyncio.run(self.analyzer.generate_migration_report())

        self.assertEqual(self.model_prompts, [])
        self.assertIn('### Dependency Graph\n\n```mermaid\ngraph TD\n    n0["CELERY.APP"]\n'
                      '    n1["CELERY/BEAT.PY"]\n    n1 --> n0\n```', report)

    def test_multi_repo_draws_one_node_per_header_repository(self):
        from utils import graphrag_utils

        documents = self.pd.DataFrame({"title": ["a", "b", "c"], "text": [
            HEADER.format(path="a.py", repo="https://github.com/o/two"),
            HEADER.format(path="b.py", repo="https://github.com/o/one"),
            HEADER.format(path="c.py", repo="https://github.com/o/two"),
        ]})
        self.analyzer.multi_repo = True
        real = types.SimpleNamespace(read_parquet=MagicMock(return_value=documents))
        with patch.object(graphrag_utils, "pd", real):
            out = self.analyzer._render_section("dependency-graph", "analysis/migration-report/0")
        real.read_parquet.assert_called_once_with(f"{self.root.name}/output/documents.parquet")
        self.assertEqual(out, '```mermaid\ngraph TD\n    n0["https://github.com/o/one"]\n'
                              '    n1["https://github.com/o/two"]\n```')

    def test_an_unknown_renderer_raises_naming_the_prompt(self):
        with self.assertRaisesRegex(ValueError, "migration-report/9.*'dependency-grpah'"):
            self.analyzer._render_section("dependency-grpah", "analysis/migration-report/9")


if __name__ == "__main__":
    unittest.main()
