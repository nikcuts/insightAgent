from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from insightagent.tool_context import PermissionDenied, ToolContext, WorkspaceViolation
from insightagent.tools import ToolRegistry


class ToolContextTests(unittest.TestCase):
    def test_write_file_stays_inside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(context=ToolContext(workspace=Path(directory)))

            result = registry.run("write_file", {"path": "notes/a.txt", "content": "ok"})

            self.assertIn("wrote", result)
            self.assertEqual((Path(directory) / "notes" / "a.txt").read_text(encoding="utf-8"), "ok")

    def test_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(context=ToolContext(workspace=Path(directory)))

            with self.assertRaises(WorkspaceViolation):
                registry.run("write_file", {"path": "../escape.txt", "content": "no"})

    def test_read_only_denies_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(context=ToolContext(workspace=Path(directory), permission_mode="read-only"))

            with self.assertRaises(PermissionDenied):
                registry.run("write_file", {"path": "x.txt", "content": "no"})

    def test_destructive_command_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(context=ToolContext(workspace=Path(directory)))

            with self.assertRaises(PermissionDenied):
                registry.run("execute_command", {"command": "rm -rf .", "cwd": str(directory)})

    def test_edit_file_replaces_unique_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.py"
            path.write_text("print('old')\n", encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=Path(directory)))

            result = registry.run("edit_file", {"path": "app.py", "old": "old", "new": "new"})

            self.assertIn("replacements=1", result)
            self.assertEqual(path.read_text(encoding="utf-8"), "print('new')\n")

    def test_grep_search_finds_workspace_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.py"
            path.write_text("def target_function():\n    return 1\n", encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=Path(directory)))

            result = registry.run("grep_search", {"pattern": "target_function", "glob": "*.py"})

            self.assertIn("app.py:1:def target_function():", result)


if __name__ == "__main__":
    unittest.main()
