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


if __name__ == "__main__":
    unittest.main()
