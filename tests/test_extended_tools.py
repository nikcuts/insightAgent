from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from insightagent.runtime.tool_context import PermissionDenied, ToolContext
from insightagent.tools import ToolRegistry, default_tools


class ExternalTool:
    name = "external_tool"
    description = "External test tool"
    input_schema = {"type": "object", "properties": {}}

    def run(self, arguments: dict) -> str:
        return "external ok"


class ExtendedToolTests(unittest.TestCase):
    def test_default_registry_includes_extended_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(context=ToolContext(workspace=Path(directory)))

            names = {schema["name"] for schema in registry.schemas()}

        self.assertIn("glob_search", names)
        self.assertIn("git_status", names)
        self.assertIn("git_diff", names)
        self.assertIn("todo_write", names)
        self.assertIn("lsp_diagnostics", names)
        self.assertIn("parse_ast", names)
        self.assertIn("get_function_signature", names)
        self.assertIn("find_dependencies", names)
        self.assertIn("get_code_metrics", names)

    def test_registry_accepts_external_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = ToolContext(workspace=Path(directory))
            registry = ToolRegistry(tools=default_tools(context) + [ExternalTool()], context=context)

            names = {schema["name"] for schema in registry.schemas()}
            result = registry.run("external_tool", {})

        self.assertIn("external_tool", names)
        self.assertEqual(result, "external ok")

    def test_registry_rejects_duplicate_tool_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = ToolContext(workspace=Path(directory))

            with self.assertRaises(ValueError):
                ToolRegistry(tools=[ExternalTool(), ExternalTool()], context=context)

    def test_glob_search_returns_matching_workspace_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
            (root / "notes.txt").write_text("hello\n", encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=root))

            result = registry.run("glob_search", {"pattern": "*.py"})

        self.assertIn("app.py", result)
        self.assertNotIn("notes.txt", result)

    def test_glob_search_matches_src_package_suffix_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src" / "flask"
            source.mkdir(parents=True)
            (source / "blueprints.py").write_text("class Blueprint:\n    pass\n", encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=root))

            result = registry.run("glob_search", {"pattern": "flask/*.py"})

        self.assertIn("src/flask/blueprints.py", result)

    def test_git_status_and_diff_report_repository_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            (root / "app.py").write_text("print('v1')\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(
                ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "init"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            (root / "app.py").write_text("print('v2')\n", encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=root))

            status = registry.run("git_status", {})
            diff = registry.run("git_diff", {"path": "app.py"})

        self.assertIn("M app.py", status)
        self.assertIn("-print('v1')", diff)
        self.assertIn("+print('v2')", diff)

    def test_todo_write_persists_todos_inside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ToolRegistry(context=ToolContext(workspace=root))

            result = registry.run(
                "todo_write",
                {
                    "todos": [
                        {"content": "inspect project", "status": "completed"},
                        {"content": "run tests", "status": "pending"},
                    ]
                },
            )

            saved = json.loads((root / ".insightagent" / "todos.json").read_text(encoding="utf-8"))

        self.assertIn("todos=2", result)
        self.assertEqual(saved["todos"][0]["content"], "inspect project")
        self.assertEqual(saved["todos"][1]["status"], "pending")

    def test_todo_write_denied_in_read_only_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(context=ToolContext(workspace=Path(directory), permission_mode="read-only"))

            with self.assertRaises(PermissionDenied):
                registry.run("todo_write", {"todos": [{"content": "no", "status": "pending"}]})

    def test_lsp_diagnostics_reports_python_syntax_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=root))

            result = registry.run("lsp_diagnostics", {"path": "bad.py"})

        self.assertIn("bad.py", result)
        self.assertIn("SyntaxError", result)


if __name__ == "__main__":
    unittest.main()
