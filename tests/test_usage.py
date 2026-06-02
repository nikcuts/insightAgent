from __future__ import annotations

import unittest

from insightagent.messages import Message, ModelResponse
from insightagent.usage import UsageTracker, estimate_tokens


class UsageTests(unittest.TestCase):
    def test_estimates_and_accumulates_usage(self) -> None:
        tracker = UsageTracker()

        tracker.record_model_call([Message(role="user", content="hello world")], ModelResponse(content="ok"))

        self.assertEqual(estimate_tokens(0), 0)
        self.assertEqual(tracker.turns, 1)
        self.assertGreater(tracker.total_tokens_est, 0)
        self.assertIn("total_tokens_est", tracker.summary())


if __name__ == "__main__":
    unittest.main()
