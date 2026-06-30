from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from insightagent.cli.tool_profiles import tool_names_for_profile
from insightagent.runtime.tool_context import ToolContext
from insightagent.tools import ToolRegistry


class ApplyEditsToolTests(unittest.TestCase):
    def test_registered_in_default_registry_and_coding_basic_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(context=ToolContext(workspace=Path(directory)))
            names = {schema["name"] for schema in registry.schemas()}

            self.assertIn("apply_edits", names)
            self.assertIn("apply_edits", tool_names_for_profile("coding-basic"))

    def test_creates_and_edits_multiple_files_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=root))

            result = registry.execute(
                "apply_edits",
                {
                    "edits": [
                        {"path": "mod.py", "old": "VALUE = 1", "new": "VALUE = 2"},
                        {"path": "main.py", "content": "from mod import VALUE\nprint(VALUE)\n"},
                    ]
                },
            )

            self.assertFalse(result.is_error)
            self.assertIn("2 file(s)", result.content)
            self.assertIn("no diagnostics", result.content)
            self.assertEqual((root / "mod.py").read_text(encoding="utf-8"), "VALUE = 2\n")
            self.assertTrue((root / "main.py").is_file())

    def test_reports_diagnostics_for_broken_python(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ToolRegistry(context=ToolContext(workspace=root))

            result = registry.execute(
                "apply_edits",
                {"edits": [{"path": "broken.py", "content": "def broken(:\n    pass\n"}]},
            )

            self.assertFalse(result.is_error)
            self.assertIn("broken.py", result.content)
            self.assertIn("SyntaxError", result.content)

    def test_failed_edit_aborts_all_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.py").write_text("a = 1\n", encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=root))

            result = registry.execute(
                "apply_edits",
                {
                    "edits": [
                        {"path": "a.py", "old": "a = 1", "new": "a = 2"},
                        {"path": "missing.py", "old": "nope", "new": "x"},
                    ]
                },
            )

            self.assertTrue(result.is_error)
            # The first edit must NOT have been written because the batch failed.
            self.assertEqual((root / "a.py").read_text(encoding="utf-8"), "a = 1\n")
            self.assertFalse((root / "missing.py").exists())

    def test_ambiguous_old_without_replace_all_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "dup.py").write_text("x\nx\n", encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=root))

            result = registry.execute(
                "apply_edits", {"edits": [{"path": "dup.py", "old": "x", "new": "y"}]}
            )

            self.assertTrue(result.is_error)
            self.assertEqual((root / "dup.py").read_text(encoding="utf-8"), "x\nx\n")

    def test_replace_all_handles_multiple_occurrences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "dup.py").write_text("x = 0\nx = 0\n", encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=root))

            result = registry.execute(
                "apply_edits",
                {"edits": [{"path": "dup.py", "old": "x = 0", "new": "x = 1", "replace_all": True}]},
            )

            self.assertFalse(result.is_error)
            self.assertEqual((root / "dup.py").read_text(encoding="utf-8"), "x = 1\nx = 1\n")

    def test_content_and_replacement_conflict_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(context=ToolContext(workspace=Path(directory)))

            result = registry.execute(
                "apply_edits",
                {"edits": [{"path": "x.py", "content": "a", "old": "a", "new": "b"}]},
            )

            self.assertTrue(result.is_error)

    def test_denied_in_read_only_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(
                context=ToolContext(workspace=Path(directory), permission_mode="read-only")
            )

            result = registry.execute(
                "apply_edits", {"edits": [{"path": "x.py", "content": "a"}]}
            )

            self.assertTrue(result.is_error)


if __name__ == "__main__":
    unittest.main()
