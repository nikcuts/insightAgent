from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from insightagent.runtime.failure_classifier import FailureKind
from insightagent.runtime.tool_context import PermissionDenied, ToolContext
from insightagent.tools import ToolRegistry


def _ctx(directory: str) -> ToolContext:
    return ToolContext(workspace=Path(directory), protected_globs=("tests/*", "test_*.py"))


class ProtectedPathTests(unittest.TestCase):
    def test_write_file_to_protected_basename_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(context=_ctx(directory))

            result = registry.execute("write_file", {"path": "test_thing.py", "content": "x = 1\n"})

            self.assertTrue(result.is_error)
            self.assertEqual(result.failure_kind, FailureKind.PERMISSION_DENIED)
            self.assertFalse((Path(directory) / "test_thing.py").exists())

    def test_write_file_to_protected_dir_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(context=_ctx(directory))

            result = registry.execute("write_file", {"path": "tests/test_store.py", "content": "x = 1\n"})

            self.assertTrue(result.is_error)

    def test_write_file_to_normal_path_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(context=_ctx(directory))

            result = registry.execute("write_file", {"path": "store.py", "content": "x = 1\n"})

            self.assertFalse(result.is_error)
            self.assertTrue((Path(directory) / "store.py").is_file())

    def test_edit_file_on_protected_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_x.py").write_text("a = 1\n", encoding="utf-8")
            registry = ToolRegistry(context=_ctx(directory))

            result = registry.execute("edit_file", {"path": "test_x.py", "old": "a = 1", "new": "a = 2"})

            self.assertTrue(result.is_error)
            self.assertEqual((root / "test_x.py").read_text(encoding="utf-8"), "a = 1\n")

    def test_apply_edits_aborts_when_any_target_is_protected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "code.py").write_text("a = 1\n", encoding="utf-8")
            registry = ToolRegistry(context=_ctx(directory))

            result = registry.execute(
                "apply_edits",
                {
                    "edits": [
                        {"path": "code.py", "old": "a = 1", "new": "a = 2"},
                        {"path": "test_x.py", "content": "broken\n"},
                    ]
                },
            )

            self.assertTrue(result.is_error)
            # Atomic: the allowed edit must not have been written either.
            self.assertEqual((root / "code.py").read_text(encoding="utf-8"), "a = 1\n")
            self.assertFalse((root / "test_x.py").exists())

    def test_context_check_path_writable_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ctx = _ctx(directory)
            with self.assertRaises(PermissionDenied):
                ctx.check_path_writable(Path(directory) / "tests" / "test_a.py")
            ctx.check_path_writable(Path(directory) / "main.py")


if __name__ == "__main__":
    unittest.main()
