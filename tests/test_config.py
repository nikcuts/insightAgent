from __future__ import annotations

import unittest

from insightagent.config import AgentConfig


class AgentConfigTests(unittest.TestCase):
    def test_defaults_match_v2_design(self) -> None:
        config = AgentConfig()

        self.assertEqual(config.max_messages, 20)
        self.assertEqual(config.max_tool_iterations, 8)
        self.assertEqual(config.max_tool_result_chars, 6000)
        self.assertTrue(config.compact_completed_turns)
        self.assertEqual(config.compact_tool_result_chars, 1200)
        self.assertEqual(config.memory_filenames, ("MEMORY.md", ".codeagent.md"))

    def test_can_override_runtime_limits(self) -> None:
        config = AgentConfig(max_messages=5, max_tool_iterations=3, max_tool_result_chars=200)

        self.assertEqual(config.max_messages, 5)
        self.assertEqual(config.max_tool_iterations, 3)
        self.assertEqual(config.max_tool_result_chars, 200)


if __name__ == "__main__":
    unittest.main()
