from __future__ import annotations

import unittest

from insightagent.api.messages import Message, ModelResponse, TokenUsage
from insightagent.telemetry.usage import UsageTracker, estimate_tokens


class UsageTests(unittest.TestCase):
    def test_estimates_and_accumulates_usage(self) -> None:
        tracker = UsageTracker()

        tracker.record_model_call([Message(role="user", content="hello world")], ModelResponse(content="ok"))

        self.assertEqual(estimate_tokens(0), 0)
        self.assertEqual(tracker.turns, 1)
        self.assertGreater(tracker.total_tokens_est, 0)
        self.assertIn("total_tokens_est", tracker.summary())

    def test_uses_real_provider_usage_when_present(self) -> None:
        tracker = UsageTracker()
        response = ModelResponse(content="x" * 1000, usage=TokenUsage(input_tokens=123, output_tokens=45))

        sample = tracker.record_model_call([Message(role="user", content="short")], response)

        self.assertFalse(sample.is_estimated)
        self.assertEqual(sample.input_tokens_est, 123)
        self.assertEqual(sample.output_tokens_est, 45)
        self.assertEqual(tracker.source, "actual")

    def test_falls_back_to_estimate_without_usage(self) -> None:
        tracker = UsageTracker()

        sample = tracker.record_model_call([Message(role="user", content="hello world")], ModelResponse(content="ok"))

        self.assertTrue(sample.is_estimated)
        self.assertEqual(tracker.source, "estimated")

    def test_cost_estimated_from_price_table(self) -> None:
        tracker = UsageTracker()
        response = ModelResponse(usage=TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000))

        tracker.record_model_call([], response, model="gpt-4o-mini")

        # gpt-4o-mini: (0.15 input + 0.60 output) per 1M tokens.
        self.assertAlmostEqual(tracker.total_cost_usd, 0.75, places=6)
        self.assertIn("cost_usd=", tracker.summary())

    def test_unknown_model_has_zero_cost(self) -> None:
        tracker = UsageTracker()
        response = ModelResponse(usage=TokenUsage(input_tokens=1000, output_tokens=1000))

        tracker.record_model_call([], response, model="some-unlisted-model")

        self.assertEqual(tracker.total_cost_usd, 0.0)

    def test_mixed_source_when_some_calls_estimated(self) -> None:
        tracker = UsageTracker()
        tracker.record_model_call([], ModelResponse(content="a", usage=TokenUsage(1, 1)))
        tracker.record_model_call([], ModelResponse(content="b"))

        self.assertEqual(tracker.source, "mixed")


if __name__ == "__main__":
    unittest.main()
