"""AUGUR's prompts survive GraphRAG's project init.

``run_graphrag`` calls ``initialize_project_at(force=True)``, which writes
GraphRAG's stock prompts into ``root/prompts/``. Until 2026-10-25 that
overwrote the prompts ``prepare_indexing_config`` had staged, so deployed
indexes ran GraphRAG's stock extraction prompt. These tests drive
``run_graphrag`` with stand-in GraphRAG modules whose init does what the real
one does to the prompts directory.
"""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

STOCK = "-Goal-\nGraphRAG's stock prompt\n"
AUGUR = "AUGUR's extraction prompt\n"


def fake_graphrag():
    """Stand-ins for the three graphrag modules ``run_graphrag`` imports."""

    def initialize_project_at(path, force=False):
        prompts = Path(path) / "prompts"
        prompts.mkdir(parents=True, exist_ok=True)
        for name in ("extract_graph.txt", "community_report.txt", "drift_reduce_prompt.txt"):
            (prompts / name).write_text(STOCK)
        (Path(path) / "settings.yaml").write_text("stock: true\n")

    async def build_index(config, verbose=False):
        return []

    initialize = types.ModuleType("graphrag.cli.initialize")
    initialize.initialize_project_at = initialize_project_at
    load_config = types.ModuleType("graphrag.config.load_config")
    load_config.load_config = lambda root: types.SimpleNamespace(workflows=None)
    api = types.ModuleType("graphrag.api")
    api.build_index = build_index
    graphrag = types.ModuleType("graphrag")
    graphrag.api = api
    return {
        "graphrag": graphrag,
        "graphrag.api": api,
        "graphrag.cli": types.ModuleType("graphrag.cli"),
        "graphrag.cli.initialize": initialize,
        "graphrag.config": types.ModuleType("graphrag.config"),
        "graphrag.config.load_config": load_config,
    }


class TestPromptsSurviveInit(unittest.TestCase):
    def setUp(self):
        self.cwd = os.getcwd()
        self.work = Path(tempfile.mkdtemp())
        os.chdir(self.work)
        (self.work / "templates").mkdir()
        (self.work / "templates" / "settings.yaml").write_text("augur: true\n")
        self.root = self.work / "index"

    def tearDown(self):
        os.chdir(self.cwd)

    def run_graphrag(self):
        from pipelines.graphrag import run_graphrag

        with mock.patch.dict(sys.modules, fake_graphrag()):
            run_graphrag(str(self.root))

    def test_staged_prompts_replace_the_stock_ones(self):
        (self.work / "prompts").mkdir()
        (self.work / "prompts" / "extract_graph.txt").write_text(AUGUR)
        (self.work / "prompts" / "community_report.txt").write_text(AUGUR)

        self.run_graphrag()

        prompts = self.root / "prompts"
        self.assertEqual((prompts / "extract_graph.txt").read_text(), AUGUR)
        self.assertEqual((prompts / "community_report.txt").read_text(), AUGUR)
        # A prompt AUGUR does not stage keeps GraphRAG's version.
        self.assertEqual((prompts / "drift_reduce_prompt.txt").read_text(), STOCK)
        self.assertEqual((self.root / "settings.yaml").read_text(), "augur: true\n")

    def test_without_staged_prompts_it_warns_and_keeps_the_stock_ones(self):
        with self.assertLogs("pipelines.graphrag", level="WARNING") as logs:
            self.run_graphrag()
        self.assertEqual((self.root / "prompts" / "extract_graph.txt").read_text(), STOCK)
        self.assertTrue(any("stock prompts" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
