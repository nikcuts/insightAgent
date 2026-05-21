from __future__ import annotations

import unittest

from insightagent.compaction import compact_completed_turn, compact_tool_result, truncate_text
from insightagent.messages import Message


class CompactionTests(unittest.TestCase):
    def test_short_text_is_unchanged(self) -> None:
        result = truncate_text("short", 20)

        self.assertEqual(result.text, "short")
        self.assertFalse(result.truncated)
        self.assertEqual(result.original_chars, 5)
        self.assertEqual(result.stored_chars, 5)

    def test_long_text_keeps_head_tail_and_count(self) -> None:
        text = "A" * 20 + "B" * 20 + "C" * 20

        result = truncate_text(text, 30)

        self.assertTrue(result.truncated)
        self.assertEqual(result.original_chars, 60)
        self.assertLessEqual(result.stored_chars, 60)
        self.assertIn("...[truncated", result.text)
        self.assertIn("chars]...", result.text)
        self.assertTrue(result.text.startswith("A"))
        self.assertTrue(result.text.endswith("C" * 9))

    def test_compact_tool_result_preserves_metadata(self) -> None:
        message = Message(role="tool", content="x" * 80, tool_call_id="call_1", is_error=True)

        compacted, result = compact_tool_result(message, 40)

        self.assertTrue(result.truncated)
        self.assertEqual(compacted.role, "tool")
        self.assertEqual(compacted.tool_call_id, "call_1")
        self.assertTrue(compacted.is_error)
        self.assertIn("...[truncated", compacted.content)

    def test_compact_completed_turn_compacts_only_large_tool_messages(self) -> None:
        messages = [
            Message(role="system", content="system"),
            Message(role="user", content="question"),
            Message(role="tool", content="x" * 80, tool_call_id="call_1"),
            Message(role="assistant", content="final"),
        ]

        compacted, count = compact_completed_turn(messages, 40)

        self.assertEqual(count, 1)
        self.assertEqual(compacted[0].content, "system")
        self.assertIn("...[truncated", compacted[2].content)
        self.assertEqual(compacted[3].content, "final")


if __name__ == "__main__":
    unittest.main()
