from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from insightagent.project_memory import load_project_memory


class ProjectMemoryTests(unittest.TestCase):
    def test_missing_memory_files_returns_empty_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            memory = load_project_memory(Path(directory), ("MEMORY.md", ".codeagent.md"))

        self.assertEqual(memory.entries, [])
        self.assertFalse(memory.has_entries)
        self.assertEqual(memory.source_names(), [])

    def test_loads_memory_file_from_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "MEMORY.md").write_text("Use concise answers.", encoding="utf-8")

            memory = load_project_memory(workspace, ("MEMORY.md", ".codeagent.md"))

        self.assertTrue(memory.has_entries)
        self.assertEqual(memory.source_names(), ["MEMORY.md"])
        self.assertEqual(memory.entries[0].content, "Use concise answers.")

    def test_loads_multiple_files_in_configured_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".codeagent.md").write_text("Agent rule.", encoding="utf-8")
            (workspace / "MEMORY.md").write_text("General memory.", encoding="utf-8")

            memory = load_project_memory(workspace, ("MEMORY.md", ".codeagent.md"))

        self.assertEqual(memory.source_names(), ["MEMORY.md", ".codeagent.md"])
        self.assertEqual([entry.content for entry in memory.entries], ["General memory.", "Agent rule."])


if __name__ == "__main__":
    unittest.main()
