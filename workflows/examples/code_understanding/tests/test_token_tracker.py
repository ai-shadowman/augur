import os
import sys
import unittest

# Add code_understanding directory to sys.path
CODE_UNDERSTANDING_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if CODE_UNDERSTANDING_DIR not in sys.path:
    sys.path.insert(0, CODE_UNDERSTANDING_DIR)

from utils.token_tracker import (
    TokenTracker,
    track_tokens,
    get_total_tokens,
    get_total_cost,
    get_token_summary,
    format_token_summary,
    format_markdown_summary,
    insert_token_summary_into_report,
    reset_token_count,
    display_token_summary,
    setup_litellm_token_tracking,
    estimate_cost,
)


class TestTokenTracker(unittest.TestCase):

    def setUp(self):
        reset_token_count()

    def test_initial_state(self):
        self.assertEqual(get_total_tokens(), 0)
        self.assertEqual(get_total_cost(), 0.0)
        summary = get_token_summary()
        self.assertEqual(summary["call_count"], 0)
        self.assertEqual(summary["total_tokens"], 0)

    def test_track_tokens_accumulation(self):
        track_tokens(prompt_tokens=100, completion_tokens=50, model="gpt-4o", stage="data_generation")
        track_tokens(prompt_tokens=200, completion_tokens=80, model="claude-3-5-sonnet", stage="analysis")
        track_tokens(total_tokens=500, cost=0.015, model="gemini-1.5-pro", stage="evaluation")

        total_tokens = get_total_tokens()
        self.assertEqual(total_tokens, 150 + 280 + 500)

        summary = get_token_summary()
        self.assertEqual(summary["call_count"], 3)
        self.assertEqual(summary["prompt_tokens"], 100 + 200 + 500)
        self.assertEqual(summary["completion_tokens"], 50 + 80)
        self.assertGreater(summary["total_cost"], 0.0)

    def test_estimate_cost(self):
        # 1M prompt + 1M completion on gpt-4o: ($2.50 + $10.00) = $12.50
        cost_gpt4o = estimate_cost("gpt-4o", prompt_tokens=1_000_000, completion_tokens=1_000_000)
        self.assertAlmostEqual(cost_gpt4o, 12.50, places=2)

        # 1M prompt + 1M completion on claude-3-5-sonnet: ($3.00 + $15.00) = $18.00
        cost_claude = estimate_cost("claude-3-5-sonnet", prompt_tokens=1_000_000, completion_tokens=1_000_000)
        self.assertAlmostEqual(cost_claude, 18.00, places=2)

    def test_reset(self):
        track_tokens(prompt_tokens=500, completion_tokens=500, model="gpt-4o")
        self.assertGreater(get_total_tokens(), 0)
        reset_token_count()
        self.assertEqual(get_total_tokens(), 0)
        self.assertEqual(get_total_cost(), 0.0)

    def test_litellm_setup_idempotent(self):
        setup_litellm_token_tracking()
        setup_litellm_token_tracking()

    def test_format_and_display_summary(self):
        track_tokens(prompt_tokens=1200, completion_tokens=350, model="gpt-4o", stage="data_generation")
        track_tokens(prompt_tokens=800, completion_tokens=200, model="claude-3-5-sonnet", stage="analysis")
        tracker = TokenTracker()
        tracker.track(prompt_tokens=500, completion_tokens=100, model="gpt-4o", stage="test")
        formatted = tracker.format_summary()
        self.assertIn("LLM TOKEN USAGE & COST SUMMARY", formatted)
        self.assertIn("Estimated Total Cost", formatted)
        self.assertIn("gpt-4o", formatted)

        # Ensure display doesn't raise
        display_token_summary()

    def test_format_markdown_summary(self):
        track_tokens(prompt_tokens=500, completion_tokens=150, model="gpt-4o", stage="analysis")
        md_section = format_markdown_summary()
        self.assertIn("## 12. LLM TOKEN USAGE & COST SUMMARY", md_section)
        self.assertIn("```text", md_section)
        self.assertIn("Estimated Total Cost", md_section)
        self.assertIn("gpt-4o", md_section)

        # Custom header support
        custom_md = format_markdown_summary(header="## Custom Cost Header")
        self.assertIn("## Custom Cost Header", custom_md)

        txt_summary = format_token_summary()
        self.assertIn("LLM TOKEN USAGE & COST SUMMARY", txt_summary)

    def test_insert_token_summary_after_acceptance_criteria(self):
        track_tokens(prompt_tokens=100, completion_tokens=50, model="gpt-4o")
        report = (
            "### Characterization Tests Generation Plan\n\n"
            "## 10. Timeline (Estimated Effort)\nSome timeline details.\n\n"
            "## 11. Acceptance Criteria  \n- All tests pass.\n- Coverage >= 95%.\n\n"
            "### Code Migration Plan (JSON)\n```json\n{}\n```"
        )
        updated = insert_token_summary_into_report(report)
        self.assertIn("## 12. LLM TOKEN USAGE & COST SUMMARY", updated)
        # Verify it appears after Acceptance Criteria but before Code Migration Plan
        pos_ac = updated.index("## 11. Acceptance Criteria")
        pos_summary = updated.index("## 12. LLM TOKEN USAGE & COST SUMMARY")
        pos_json = updated.index("### Code Migration Plan (JSON)")
        self.assertTrue(pos_ac < pos_summary < pos_json)

    def test_insert_token_summary_with_closing_signature(self):
        track_tokens(prompt_tokens=100, completion_tokens=50, model="gpt-4o")
        report = (
            "### Characterization Tests Generation Plan\n\n"
            "## 11. Acceptance Criteria  \n\n"
            "- **All tests pass** on both JDK 1.8 and JDK 11/17.\n\n"
            "---\n\n"
            "*Prepared by:* Senior QA Engineer\n"
            "*Date:* 2026-09-10\n\n"
            "### Code Migration Plan (JSON)\n```json\n{}\n```"
        )
        updated = insert_token_summary_into_report(report)
        pos_ac = updated.index("## 11. Acceptance Criteria")
        pos_prep = updated.index("*Prepared by:*")
        pos_summary = updated.index("## 12. LLM TOKEN USAGE & COST SUMMARY")
        pos_json = updated.index("### Code Migration Plan (JSON)")
        self.assertTrue(pos_ac < pos_prep < pos_summary < pos_json)

    def test_insert_token_summary_deduplication(self):
        track_tokens(prompt_tokens=100, completion_tokens=50, model="gpt-4o")
        report = (
            "### Characterization Tests Generation Plan\n\n"
            "## 11. Acceptance Criteria\nPass all tests.\n\n"
            "### Code Migration Plan (JSON)\n```json\n{}\n```\n\n"
            "---\n\n## LLM Token Usage & Cost Summary\n\n```text\nold table\n```"
        )
        updated = insert_token_summary_into_report(report)
        # Should only have one summary, and it should be ## 12. LLM TOKEN USAGE & COST SUMMARY
        self.assertEqual(updated.count("LLM TOKEN USAGE & COST SUMMARY"), 2)  # once in header, once in ASCII table
        self.assertNotIn("old table", updated)
        pos_summary = updated.index("## 12. LLM TOKEN USAGE & COST SUMMARY")
        pos_json = updated.index("### Code Migration Plan (JSON)")
        self.assertTrue(pos_summary < pos_json)

    def test_insert_token_summary_fallback_end_of_report(self):
        track_tokens(prompt_tokens=100, completion_tokens=50, model="gpt-4o")
        report = "# Simple Report\n\nNo acceptance criteria section here."
        updated = insert_token_summary_into_report(report)
        self.assertIn("## 12. LLM TOKEN USAGE & COST SUMMARY", updated)
        self.assertTrue(updated.endswith("```\n") or updated.endswith("```"))


if __name__ == "__main__":
    unittest.main()
