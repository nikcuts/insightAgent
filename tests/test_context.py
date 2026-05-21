from __future__ import annotations

import unittest
from pathlib import Path

from insightagent.context import build_system_prompt
from insightagent.project_memory import MemoryEntry, ProjectMemory


class ContextTests(unittest.TestCase):
    def test_returns_base_prompt_when_memory_is_empty(self) -> None:
        prompt = build_system_prompt("Base prompt.", ProjectMemory(entries=[]))

        self.assertEqual(prompt, "Base prompt.")

    def test_appends_memory_sections_in_order(self) -> None:
        memory = ProjectMemory(
            entries=[
                MemoryEntry(path=Path("/repo/MEMORY.md"), name="MEMORY.md", content="General memory."),
                MemoryEntry(path=Path("/repo/.codeagent.md"), name=".codeagent.md", content="Agent rules."),
            ]
        )

        prompt = build_system_prompt("Base prompt.", memory)

        self.assertIn("Base prompt.", prompt)
        self.assertLess(prompt.index("## Project Memory: MEMORY.md"), prompt.index("## Project Memory: .codeagent.md"))
        self.assertIn("General memory.", prompt)
        self.assertIn("Agent rules.", prompt)


if __name__ == "__main__":
    unittest.main()
