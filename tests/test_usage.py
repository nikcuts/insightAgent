from __future__ import annotations

import unittest

from langchain_core.messages import AIMessage

from insightagent.graph.usage import UsageAccumulator


class UsageTests(unittest.TestCase):
    def test_accumulates_provider_reported_tokens_without_character_estimates(self) -> None:
        accumulator = UsageAccumulator()

        accumulator.record(
            AIMessage(
                content="hello world",
                usage_metadata={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            )
        )
        accumulator.record(AIMessage(content="no metadata"))

        self.assertEqual(
            accumulator.snapshot(),
            {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
        )


if __name__ == "__main__":
    unittest.main()
