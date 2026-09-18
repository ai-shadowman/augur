import io
import json
import logging
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Ensure code_understanding package is on sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Provide mock stubs for container dependencies when running in local environments
for pkg_name in [
    "graphrag", "graphrag.api", "graphrag.config", "graphrag.config.load_config",
    "pandas", "yaml", "mlflow", "mlflow.tracking", "mlflow.metrics", "mlflow.metrics.genai",
    "requests", "deepeval",
    "pyvis", "pyvis.network", "networkx", "matplotlib", "matplotlib.pyplot", "litellm",
    "sdg_hub", "sdg_hub.core", "sdg_hub.core.flow", "flows.flow_extensions", "datasets"
]:
    if pkg_name not in sys.modules:
        m = MagicMock()
        m.__path__ = []
        sys.modules[pkg_name] = m

from utils.duration_tracker import DurationTracker
from utils.token_tracker import TokenCostTracker


class TestDataGenerationDurationTelemetry(unittest.TestCase):
    """Tests for Data Generation stage duration tracking."""

    def setUp(self):
        DurationTracker.reset_instance()
        self.tracker = DurationTracker.get_instance()

    def tearDown(self):
        DurationTracker.reset_instance()

    def test_data_generation_substeps_recorded(self):
        """Verify that all Data Generation sub-steps are recorded under Data Generation stage."""
        steps = [
            ("Reset Environment", 0.5),
            ("GitHub Checkout", 1.2),
            ("Detect Languages", 0.3),
            ("Load External Data", 0.4),
            ("Parse Raw Code (python)", 2.5),
            ("LLM Metadata Extraction (python)", 5.0),
            ("Save Metadata Files (python)", 0.2),
            ("Log Metadata Results (python)", 0.8),
            ("Log Pipeline Result (python)", 0.6),
        ]
        for step, dur in steps:
            self.tracker.record_step(stage="Data Generation", step=step, duration=dur)

        # Aggregate total step
        total_time = sum(d for _, d in steps)
        self.tracker.record_step(
            stage="Data Generation",
            step="Data Generation Total",
            duration=total_time,
            metadata={"is_aggregate": True},
        )

        all_records = self.tracker.get_all_records()
        self.assertEqual(len(all_records), 10)

        # Check stage duration does NOT double count Data Generation Total
        stage_durations = self.tracker.get_stage_durations()
        self.assertIn("Data Generation", stage_durations)
        self.assertAlmostEqual(stage_durations["Data Generation"], total_time, places=2)

    def test_canonical_stage_ordering_in_duration_summary(self):
        """Verify Data Generation comes before Indexing and Analysis in duration summary."""
        self.tracker.record_step(stage="Analysis", step="Adhoc Query", duration=3.0)
        self.tracker.record_step(stage="Indexing", step="GraphRAG Indexing", duration=15.0)
        self.tracker.record_step(stage="Data Generation", step="Detect Languages", duration=1.0)

        summary = self.tracker.format_summary()

        pos_datagen = summary.find("Data Generation")
        pos_indexing = summary.find("Indexing")
        pos_analysis = summary.find("Analysis")

        self.assertTrue(pos_datagen != -1 and pos_indexing != -1 and pos_analysis != -1)
        self.assertLess(pos_datagen, pos_indexing)
        self.assertLess(pos_indexing, pos_analysis)

    def test_stage_breakdown_includes_data_generation(self):
        """Verify stage breakdown section includes Data Generation with percentage."""
        self.tracker.record_step(stage="Data Generation", step="Detect Languages", duration=10.0)
        self.tracker.record_step(stage="Indexing", step="Prepare Config", duration=30.0)
        self.tracker.record_step(stage="Analysis", step="Generate Report", duration=60.0)

        summary = self.tracker.format_summary()
        self.assertIn("Stage Breakdown:", summary)
        self.assertIn("Data Generation: 10.00s (10.0%)", summary)
        self.assertIn("Indexing: 30.00s (30.0%)", summary)
        self.assertIn("Analysis: 1m 0.0s (60.0%)", summary)


class TestDataGenerationTokenTelemetry(unittest.TestCase):
    """Tests for Data Generation stage LLM token and cost tracking."""

    def setUp(self):
        TokenCostTracker.reset_instance()
        self.tracker = TokenCostTracker.get_instance()

    def tearDown(self):
        TokenCostTracker.reset_instance()

    def test_set_category_updates_active_category(self):
        """Verify set_category updates the active category on singleton instance."""
        self.tracker.set_category("Data Generation (python)")
        self.assertEqual(self.tracker._active_category, "Data Generation (python)")

        self.tracker.set_category("Data Generation (yaml)")
        self.assertEqual(self.tracker._active_category, "Data Generation (yaml)")

    def test_canonical_stage_ordering_in_token_summary(self):
        """Verify Data Generation appears first in LLM Token Usage summary table."""
        self.tracker.track("GraphRAG Chat (gpt-oss-120b)", calls=4, prompt_tokens=10008, output_tokens=7200, model="gpt-oss-120b")
        self.tracker.track("GraphRAG Indexing (gpt-oss-120b)", calls=9, prompt_tokens=18000, output_tokens=3600, model="gpt-oss-120b")
        self.tracker.track("Data Generation (python)", calls=12, prompt_tokens=12000, output_tokens=2400, model="gpt-oss-120b")

        summary = self.tracker.format_summary()

        pos_datagen = summary.find("Data Generation (python)")
        pos_indexing = summary.find("GraphRAG Indexing (gpt-oss-120b)")
        pos_chat = summary.find("GraphRAG Chat (gpt-oss-120b)")

        self.assertTrue(pos_datagen != -1 and pos_indexing != -1 and pos_chat != -1)
        self.assertLess(pos_datagen, pos_indexing)
        self.assertLess(pos_indexing, pos_chat)

    def test_token_tracker_deduplication_on_merge(self):
        """Verify merging identical tokens.json files does not double-count calls or tokens."""
        self.tracker.track("Data Generation (python)", calls=5, prompt_tokens=5000, output_tokens=1000, model="gpt-oss-120b")

        other = TokenCostTracker()
        other.track("Data Generation (python)", calls=5, prompt_tokens=5000, output_tokens=1000, model="gpt-oss-120b")

        self.tracker.merge(other)

        # Should remain 5 calls, not 10
        self.assertEqual(self.tracker.records["Data Generation (python)"]["calls"], 5)
        self.assertEqual(self.tracker.records["Data Generation (python)"]["prompt_tokens"], 5000)

    def test_fallback_token_estimation_in_data_generation(self):
        """Verify fallback token estimation calculates reasonable token counts when callbacks intercept 0 calls."""
        from pipelines.base.data_generation import get_parsed_code_metadata

        mock_rows = [
            {"code": "def hello():\n    return 'world'\n" * 10, "file_name": "test1.py", "repo": "https://github.com/test/repo"},
            {"code": "def foo():\n    return 'bar'\n" * 10, "file_name": "test2.py", "repo": "https://github.com/test/repo"},
            {"code": "def baz():\n    return 'qux'\n" * 10, "file_name": "test3.py", "repo": "https://github.com/test/repo"},
        ]

        class DummyDF:
            def __len__(self):
                return len(mock_rows)
            def iterrows(self):
                return [(i, row) for i, row in enumerate(mock_rows)]
            def copy(self):
                return self
            def __getitem__(self, key):
                return [r.get(key) for r in mock_rows]

        mock_df = DummyDF()
        mock_flow = MagicMock()
        mock_flow.run.return_value = mock_df

        orig_flow = getattr(sys.modules["sdg_hub.core.flow"], "Flow", None)
        orig_ds = getattr(sys.modules["datasets"], "Dataset", None)
        try:
            sys.modules["sdg_hub.core.flow"].Flow = MagicMock(return_value=mock_flow)
            sys.modules["datasets"].Dataset = MagicMock()

            calls_before = self.tracker.get_totals()["total_calls"]

            get_parsed_code_metadata(
                mock_df,
                language="python",
            )
        finally:
            if orig_flow is not None:
                sys.modules["sdg_hub.core.flow"].Flow = orig_flow
            if orig_ds is not None:
                sys.modules["datasets"].Dataset = orig_ds

        calls_after = self.tracker.get_totals()["total_calls"]
        # Fallback should have recorded 3 calls (one per row)
        self.assertEqual(calls_after - calls_before, 3)
        matching_keys = [k for k in self.tracker.records if "Data Generation (python)" in k]
        self.assertTrue(len(matching_keys) > 0)
        record = self.tracker.records[matching_keys[0]]
        self.assertEqual(record["calls"], 3)
        self.assertGreater(record["prompt_tokens"], 0)
        self.assertGreater(record["output_tokens"], 0)


class TestMultiStageReportAssembly(unittest.TestCase):
    """Tests verifying full multi-stage telemetry integration across Data Generation, Indexing, and Analysis."""

    def test_multi_stage_report_contains_all_three_stages(self):
        """Verify that duration and token tables across all 3 stages format cleanly together."""
        dur_tracker = DurationTracker()
        dur_tracker.record_step(stage="Data Generation", step="Detect Languages", duration=2.1)
        dur_tracker.record_step(stage="Data Generation", step="LLM Metadata Extraction (python)", duration=14.5)
        dur_tracker.record_step(stage="Indexing", step="Prepare Settings & Config", duration=1.8)
        dur_tracker.record_step(stage="Indexing", step="GraphRAG Indexing", duration=120.0)
        dur_tracker.record_step(stage="Analysis", step="Migration Plan Generation", duration=45.2)

        tok_tracker = TokenCostTracker()
        tok_tracker.track("Data Generation (python)", calls=8, prompt_tokens=8500, output_tokens=1700, model="gpt-oss-120b")
        tok_tracker.track("GraphRAG Indexing (gpt-oss-120b)", calls=9, prompt_tokens=18000, output_tokens=3600, model="gpt-oss-120b")
        tok_tracker.track("GraphRAG Chat (gpt-oss-120b)", calls=4, prompt_tokens=10008, output_tokens=7200, model="gpt-oss-120b")

        dur_summary = dur_tracker.format_summary()
        tok_summary = tok_tracker.format_summary()

        # Check duration summary order
        idx_dg_dur = dur_summary.find("Data Generation")
        idx_idx_dur = dur_summary.find("Indexing")
        idx_an_dur = dur_summary.find("Analysis")
        self.assertTrue(idx_dg_dur < idx_idx_dur < idx_an_dur)

        # Check token summary order
        idx_dg_tok = tok_summary.find("Data Generation (python)")
        idx_idx_tok = tok_summary.find("GraphRAG Indexing (gpt-oss-120b)")
        idx_an_tok = tok_summary.find("GraphRAG Chat (gpt-oss-120b)")
        self.assertTrue(idx_dg_tok < idx_idx_tok < idx_an_tok)

        # Check Markdown sections formatting
        dur_section = dur_tracker.format_markdown_section()
        tok_section = tok_tracker.format_markdown_section()

        self.assertIn("### Pipeline Execution Duration Summary", dur_section)
        self.assertIn("### LLM Token Usage & Cost Summary", tok_section)
        self.assertIn("Data Generation", dur_section)
        self.assertIn("Indexing", dur_section)
        self.assertIn("Analysis", dur_section)


if __name__ == "__main__":
    unittest.main()
