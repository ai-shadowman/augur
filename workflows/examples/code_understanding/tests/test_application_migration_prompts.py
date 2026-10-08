"""Exercise report assembly with real assets and an isolated, offline LLM double.

The subprocess keeps other tests' global container-dependency stubs from
replacing the real YAML parser and Jinja renderer used by the asset loader.
"""

import os
from pathlib import Path
import subprocess
import sys
import unittest


WORKFLOW_DIR = Path(__file__).resolve().parents[1]

REPORT_PROBE = r'''
import asyncio
from contextlib import ExitStack, redirect_stdout
import json
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import jinja2
import yaml

# These container services are not used by the prompt/report assembly under test.
for name in [
    "graphrag", "graphrag.api", "graphrag.config", "graphrag.config.load_config",
    "pandas", "mlflow", "mlflow.tracking", "mlflow.metrics", "mlflow.metrics.genai",
    "requests", "deepeval", "pyvis", "pyvis.network", "networkx",
    "matplotlib", "matplotlib.pyplot", "litellm",
]:
    stub = MagicMock()
    stub.__path__ = []
    sys.modules[name] = stub
sys.modules["mlflow"].active_run.return_value = None

from loaders.default_asset_loader import DefaultAssetLoader
from utils.duration_tracker import DurationTracker
from utils.graphrag_utils import DependencyAnalyzer
from utils.token_tracker import TokenCostTracker

multi_repo = sys.argv[1] == "multi"
evidence = [
    "GRAPH-EVIDENCE",
    "https://github.com/acme/billing: library-alpha 1.2 declared in build.xml; "
    "library-unknown has unknown version; runtime-java 8. "
    + ("https://github.com/acme/web: library-alpha 3.4 declared in package.json; "
       "runtime-js version unknown." if multi_repo else ""),
    "ORDER-EVIDENCE",
    "SECURITY-EVIDENCE: advisory verification needed",
    "MODERNIZATION-EVIDENCE: library-alpha upgrade candidate needs verification",
    "PLAN-EVIDENCE: migrate src/Billing.java after its library prerequisites",
    "INTEGRATION-EVIDENCE: preserve billing database transaction behavior",
    "RISK-EVIDENCE: validate serialization and authentication contracts",
]
queries = []

async def offline_query(prompt, **options):
    queries.append({"prompt": prompt, **options})
    index = len(queries) - 1
    if index < len(evidence):
        return evidence[index]
    if index == 8:
        return "CHARACTERIZATION-EVIDENCE: preserve billing API behavior"
    if index == 10:
        return '{"modernization_enhanced_code_migration_plan": [{"component": "ENHANCED-COMPONENT-EVIDENCE"}]}'
    return '{"plan": []}'

with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
    stack.enter_context(patch.dict(os.environ, {"ASSET_LOADER": "local"}))
    stack.enter_context(patch.object(DependencyAnalyzer, "_setup_search"))
    stack.enter_context(patch.object(DependencyAnalyzer, "_extract_indexed_git_urls",
                                    return_value={"https://github.com/acme/billing"}))
    stack.enter_context(patch("utils.visualization_utils.log_interactive_dependency_graph"))
    stack.enter_context(patch.object(DurationTracker, "download_from_mlflow"))
    stack.enter_context(patch.object(DurationTracker, "upload_to_mlflow"))
    stack.enter_context(patch.object(TokenCostTracker, "download_from_mlflow"))
    stack.enter_context(patch.object(TokenCostTracker, "upload_to_mlflow"))
    # Fail on missing variables instead of letting Jinja silently drop context.
    stack.enter_context(patch("jinja2.Template",
                              jinja2.Environment(undefined=jinja2.StrictUndefined).from_string))
    DurationTracker.reset_instance()
    analyzer = DependencyAnalyzer(root_dir=root, git_slug="billing-main",
                                  multi_repo=multi_repo,
                                  token_tracker=TokenCostTracker())
    stack.enter_context(patch.object(analyzer, "query_with_llm", side_effect=offline_query))
    with redirect_stdout(sys.stderr):
        report = asyncio.run(analyzer.generate_migration_report())
    print(json.dumps({"queries": queries, "report": report}))
'''


class TestApplicationMigrationPrompts(unittest.TestCase):
    def generate_report(self, mode):
        import json

        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, [str(WORKFLOW_DIR), env.get("PYTHONPATH")])
        )
        result = subprocess.run(
            [sys.executable, "-c", REPORT_PROBE, mode],
            cwd=WORKFLOW_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Could not preload prompt", result.stderr)
        return json.loads(result.stdout)

    def assert_enhanced_evidence(self, result):
        enhanced = [query for query in result["queries"] if query["bypass_index"]]
        self.assertEqual(len(enhanced), 4)
        # Extraction consumes the detailed plan; analytical enhancements need
        # the dependency, modernization, integration, and risk findings too.
        self.assertIn("PLAN-EVIDENCE", enhanced[1]["prompt"])
        for index in (0, 2, 3):
            with self.subTest(enhanced=index):
                prompt = enhanced[index]["prompt"]
                for marker in (
                    "library-alpha 1.2", "library-unknown has unknown version",
                    "SECURITY-EVIDENCE", "MODERNIZATION-EVIDENCE",
                    "PLAN-EVIDENCE", "INTEGRATION-EVIDENCE", "RISK-EVIDENCE",
                ):
                    self.assertIn(marker, prompt)
                self.assertNotIn("RHEL", prompt)
        self.assertNotIn("RHEL", result["report"])
        self.assertIn("CHARACTERIZATION-EVIDENCE", enhanced[3]["prompt"])
        self.assertIn("ENHANCED-COMPONENT-EVIDENCE", enhanced[3]["prompt"])

    def test_single_repository_findings_reach_enhanced_plans(self):
        self.assert_enhanced_evidence(self.generate_report("single"))

    def test_multi_repository_plans_keep_versions_and_findings(self):
        result = self.generate_report("multi")
        self.assert_enhanced_evidence(result)
        indexed = [query for query in result["queries"] if not query["bypass_index"]]
        for query in indexed[1:]:
            self.assertTrue(query["use_global"])
        enhanced = [query for query in result["queries"] if query["bypass_index"]]
        for index in (0, 2, 3):
            self.assertIn("https://github.com/acme/web: library-alpha 3.4",
                          enhanced[index]["prompt"])


if __name__ == "__main__":
    unittest.main()
