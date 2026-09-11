import unittest
from unittest.mock import MagicMock, patch
import os
import sys

# Ensure code_understanding package is on sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from utils.token_tracker import TokenCostTracker


class TestTokenCostTracker(unittest.TestCase):
    """Unit tests for TokenCostTracker with LiteLLM integration and ASCII table formatting."""

    def setUp(self):
        self.tracker = TokenCostTracker(
            chat_model="openai/gpt-oss-120b",
            embed_model="e5-mistral-7b-instruct",
            chat_prompt_price=0.000002,   # $2.00 / 1M
            chat_output_price=0.000008,   # $8.00 / 1M
            embed_prompt_price=0.0000002, # $0.20 / 1M
        )

    def test_token_counting(self):
        """Test token counting functionality and empty text handling."""
        self.assertEqual(self.tracker.count_tokens(""), 0)
        tokens = self.tracker.count_tokens("Hello world, this is a test prompt.")
        self.assertGreater(tokens, 0)

    def test_cost_calculation(self):
        """Test pricing math for chat and embedding models."""
        # Chat model: 10,000 prompt @ $0.000002 = $0.02, 5,000 output @ $0.000008 = $0.04 -> $0.06
        chat_cost = self.tracker.calculate_cost(
            prompt_tokens=10000,
            output_tokens=5000,
            model="openai/gpt-oss-120b"
        )
        self.assertAlmostEqual(chat_cost, 0.06, places=5)

        # Embedding model: 10,000 prompt @ $0.0000002 = $0.002, 0 output -> $0.002
        embed_cost = self.tracker.calculate_cost(
            prompt_tokens=10000,
            output_tokens=0,
            model="e5-mistral-7b-instruct"
        )
        self.assertAlmostEqual(embed_cost, 0.002, places=6)

    def test_exact_sample_metrics_and_total_cost(self):
        """Verify the exact sample table metrics from the prompt:
        - 15 invocations
        - 20,724 prompt tokens
        - 14,141 output tokens
        - 34,865 total tokens
        - $0.1536 total cost
        """
        # Row 1: e5-mistral-7b-instruct (3 calls, 561 prompt, 0 output)
        self.tracker.track_embedding(prompt_tokens=561, calls=3)

        # Row 2: Local search (3 calls, 10,881 prompt, 2,093 output)
        self.tracker.track_local_search(prompt_tokens=10881, output_tokens=2093, calls=3)

        # Row 3: Global search (5 calls, 461 prompt, 4,170 output)
        self.tracker.track_global_search(prompt_tokens=461, output_tokens=4170, calls=5)

        # Row 4: Chat (4 calls, 8,821 prompt, 7,878 output)
        self.tracker.track_chat(prompt_tokens=8821, output_tokens=7878, calls=4)

        totals = self.tracker.get_totals()

        self.assertEqual(totals["total_calls"], 15)
        self.assertEqual(totals["total_prompt_tokens"], 20724)
        self.assertEqual(totals["total_output_tokens"], 14141)
        self.assertEqual(totals["total_tokens"], 34865)

        # Cost breakdown check:
        # e5-mistral: 561 * 0.0000002 = $0.0001122 -> $0.0001
        # local search: 10881 * 0.000002 + 2093 * 0.000008 = 0.021762 + 0.016744 = $0.038506 -> $0.0385
        # global search: 461 * 0.000002 + 4170 * 0.000008 = 0.000922 + 0.033360 = $0.034282 -> $0.0343
        # chat: 8821 * 0.000002 + 7878 * 0.000008 = 0.017642 + 0.063024 = $0.080666 -> $0.0807
        # Total cost = 0.0001122 + 0.038506 + 0.034282 + 0.080666 = 0.1535662 -> $0.1536
        self.assertAlmostEqual(totals["total_cost"], 0.1535662, places=6)
        self.assertEqual(f"${totals['total_cost']:.4f}", "$0.1536")

    def test_format_summary_structure(self):
        """Verify the generated ASCII summary matches the required layout, dividers, and columns."""
        self.tracker.track_embedding(prompt_tokens=561, calls=3)
        self.tracker.track_local_search(prompt_tokens=10881, output_tokens=2093, calls=3)
        self.tracker.track_global_search(prompt_tokens=461, output_tokens=4170, calls=5)
        self.tracker.track_chat(prompt_tokens=8821, output_tokens=7878, calls=4)

        summary = self.tracker.format_summary()
        lines = summary.split("\n")

        # Title check
        self.assertIn("LLM TOKEN USAGE & COST SUMMARY", lines[0])

        # Border widths check (78 characters)
        self.assertEqual(lines[1], "=" * 78)
        self.assertEqual(lines[7], "-" * 78)
        self.assertEqual(lines[9], "-" * 78)

        # Totals section check
        self.assertIn("Total LLM Invocations : 15", lines[2])
        self.assertIn("Total Prompt Tokens   : 20,724", lines[3])
        self.assertIn("Total Output Tokens   : 14,141", lines[4])
        self.assertIn("Total Tokens Used     : 34,865", lines[5])
        self.assertIn("Estimated Total Cost  : $0.1536", lines[6])

        # Column header check
        self.assertEqual(
            lines[8],
            " Source / Model                   Calls   Prompt     Output     Total      Est. Cost "
        )

        # Row count check (4 rows after header divider)
        self.assertEqual(len(lines), 14)

        # Check name truncation for source names exceeding 32 characters
        long_tracker = TokenCostTracker()
        long_tracker.track("A" * 40, calls=1, prompt_tokens=100, output_tokens=50)
        long_summary = long_tracker.format_summary()
        self.assertIn("A" * 29 + "...", long_summary)

    def test_format_markdown_section(self):
        """Verify markdown section wrapping."""
        self.tracker.track_chat(prompt_tokens=100, output_tokens=50, calls=1)
        md_section = self.tracker.format_markdown_section()

        self.assertTrue(md_section.startswith("\n\n### LLM Token Usage & Cost Summary\n\n```\n"))
        self.assertTrue(md_section.endswith("\n```\n"))
        self.assertIn("Total LLM Invocations : 1", md_section)

    def test_reset(self):
        """Verify tracker reset clears all metrics."""
        self.tracker.track_chat(prompt_tokens=500, output_tokens=200, calls=2)
        self.assertGreater(self.tracker.get_totals()["total_calls"], 0)

        self.tracker.reset()
        totals = self.tracker.get_totals()
        self.assertEqual(totals["total_calls"], 0)
        self.assertEqual(totals["total_prompt_tokens"], 0)
        self.assertEqual(totals["total_output_tokens"], 0)
        self.assertEqual(totals["total_cost"], 0.0)


class TestDependencyAnalyzerIntegration(unittest.TestCase):
    """Test DependencyAnalyzer integration with TokenCostTracker."""

    def test_analyzer_token_tracker_initialization(self):
        """Verify DependencyAnalyzer correctly initializes or receives a token tracker."""
        from utils.graphrag_utils import DependencyAnalyzer
        custom_tracker = TokenCostTracker()
        custom_tracker.track_chat(prompt_tokens=123, output_tokens=456)

        # Mock out setup methods to avoid requiring disk parquet files in unit test
        with patch.object(DependencyAnalyzer, '_setup_configuration'), \
             patch.object(DependencyAnalyzer, '_setup_search'), \
             patch.object(DependencyAnalyzer, '_setup_prompts'):
            analyzer = DependencyAnalyzer(token_tracker=custom_tracker)
            self.assertEqual(analyzer.token_tracker, custom_tracker)
            summary = analyzer.get_token_usage_summary()
            self.assertIn("Total LLM Invocations : 1", summary)
            self.assertIn("123", summary)

    def test_report_placement_above_code_migration_plan_json(self):
        """Verify that the token usage table is placed immediately above the Code Migration Plan (JSON) section."""
        import asyncio
        from utils.graphrag_utils import DependencyAnalyzer

        custom_tracker = TokenCostTracker()
        custom_tracker.track_chat(prompt_tokens=500, output_tokens=250)

        with patch.object(DependencyAnalyzer, '_setup_configuration'), \
             patch.object(DependencyAnalyzer, '_setup_search'), \
             patch.object(DependencyAnalyzer, '_setup_prompts'), \
             patch.object(DependencyAnalyzer, '_extract_indexed_git_urls', return_value={"https://github.com/org/repo"}), \
             patch('utils.visualization_utils.log_interactive_dependency_graph'):

            analyzer = DependencyAnalyzer(token_tracker=custom_tracker)

            mock_loader = MagicMock()
            mock_loader.num_prompts.side_effect = lambda path: 1 if "enhanced" in path else 1
            mock_loader.download_prompt.side_effect = [
                ("prompt 1", {"title": "### Overview", "skip_prompt": None}),
                ("prompt 2", {"title": "### Code Migration Plan (JSON)", "skip_prompt": None}),
            ]

            with patch('loaders.default_asset_loader.DefaultAssetLoader', return_value=mock_loader), \
                 patch.object(analyzer, 'query_with_llm', side_effect=["Overview content", '{"plan": []}']):

                report = asyncio.run(analyzer.generate_migration_report())

                self.assertIn("### LLM Token Usage & Cost Summary", report)
                self.assertIn("### Code Migration Plan (JSON)", report)

                summary_pos = report.index("### LLM Token Usage & Cost Summary")
                plan_pos = report.index("### Code Migration Plan (JSON)")
                self.assertLess(summary_pos, plan_pos)

    def test_prepare_settings_empty_tokens_fallback(self):
        """Verify that prepare_settings replaces empty or missing tokens with 'EMPTY' to prevent Pydantic validation failures."""
        import tempfile
        from utils.graphrag_utils import DependencyAnalyzer

        with tempfile.TemporaryDirectory() as tmp_dir:
            template_path = os.path.join(tmp_dir, "settings.yaml.in")
            with open(template_path, "w") as f:
                f.write("chat_key: ${GRAPHRAG_LLM_TOKEN}\nembed_key: ${EMBED_LLM_TOKEN}\n")

            mock_loader = MagicMock()
            with patch('loaders.default_asset_loader.DefaultAssetLoader', return_value=mock_loader), \
                 patch.dict(os.environ, {"GRAPHRAG_LLM_TOKEN": "", "EMBED_LLM_TOKEN": ""}):

                DependencyAnalyzer.prepare_settings(template_dir=tmp_dir, output_dir=tmp_dir)

                output_path = os.path.join(tmp_dir, "settings.yaml")
                with open(output_path) as f:
                    content = f.read()

                self.assertIn("chat_key: EMPTY", content)
                self.assertIn("embed_key: EMPTY", content)


if __name__ == "__main__":
    unittest.main()


