from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from insightagent.runtime.tool_context import ToolContext
from insightagent.tools import ToolRegistry


class CodeAnalysisToolTests(unittest.TestCase):
    def test_parse_ast_returns_python_structure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package").mkdir()
            source = root / "package" / "sample.py"
            source.write_text(
                textwrap.dedent(
                    '''
                    import os
                    from pathlib import Path

                    VALUE = 42

                    class Worker(BaseWorker):
                        """Do useful work."""

                        def run(self, item: str) -> int:
                            return len(item)

                    async def build(name: str) -> Worker:
                        return Worker()
                    '''
                ).lstrip(),
                encoding="utf-8",
            )
            registry = ToolRegistry(context=ToolContext(workspace=root))

            result = json.loads(registry.run("parse_ast", {"path": "package/sample.py"}))

        self.assertEqual(result["file"], "package/sample.py")
        self.assertEqual(result["imports"][0]["module"], "os")
        self.assertEqual(result["imports"][1]["module"], "pathlib")
        self.assertEqual(result["classes"][0]["name"], "Worker")
        self.assertEqual(result["classes"][0]["methods"][0]["name"], "run")
        self.assertEqual(result["functions"][0]["name"], "build")
        self.assertTrue(result["functions"][0]["is_async"])
        self.assertEqual(result["global_variables"][0]["name"], "VALUE")

    def test_get_function_signature_finds_functions_and_methods(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample.py"
            source.write_text(
                textwrap.dedent(
                    '''
                    class Worker:
                        async def run(self, item: str, count: int = 1) -> str:
                            """Run work."""
                            return item * count

                    def build(name: str) -> Worker:
                        return Worker()
                    '''
                ).lstrip(),
                encoding="utf-8",
            )
            registry = ToolRegistry(context=ToolContext(workspace=root))

            method = json.loads(
                registry.run("get_function_signature", {"path": "sample.py", "function_name": "run"})
            )
            function = json.loads(
                registry.run("get_function_signature", {"path": "sample.py", "function_name": "build"})
            )
            missing = registry.run("get_function_signature", {"path": "sample.py", "function_name": "missing"})

        self.assertEqual(method["name"], "run")
        self.assertEqual(method["kind"], "method")
        self.assertTrue(method["is_async"])
        self.assertEqual(method["signature"], "async def run(self, item: str, count: int = 1) -> str")
        self.assertEqual(method["docstring"], "Run work.")
        self.assertEqual(function["kind"], "function")
        self.assertEqual(function["signature"], "def build(name: str) -> Worker")
        self.assertIn("function not found: missing", missing)

    def test_find_dependencies_classifies_imports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "local_module.py").write_text("VALUE = 1\n", encoding="utf-8")
            source = root / "sample.py"
            source.write_text(
                textwrap.dedent(
                    """
                    import json
                    import requests
                    import local_module
                    from . import sibling
                    from pathlib import Path
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            registry = ToolRegistry(context=ToolContext(workspace=root))

            result = json.loads(registry.run("find_dependencies", {"path": "sample.py"}))

        self.assertIn("json", result["stdlib"])
        self.assertIn("pathlib", result["stdlib"])
        self.assertIn("requests", result["third_party"])
        self.assertIn("local_module", result["local"])
        self.assertIn("sibling", result["relative"])

    def test_get_code_metrics_counts_basic_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample.py"
            source.write_text(
                textwrap.dedent(
                    """
                    import os
                    # comment

                    class Worker:
                        pass

                    def build():
                        return Worker()
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            registry = ToolRegistry(context=ToolContext(workspace=root))

            result = json.loads(registry.run("get_code_metrics", {"path": "sample.py"}))

        self.assertEqual(result["file"], "sample.py")
        self.assertEqual(result["imports"], 1)
        self.assertEqual(result["classes"], 1)
        self.assertEqual(result["functions"], 1)
        self.assertEqual(result["comment_lines"], 1)
        self.assertGreaterEqual(result["total_lines"], 7)

    def test_code_analysis_reports_syntax_and_file_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad.py").write_text("def broken(:\n", encoding="utf-8")
            (root / "notes.txt").write_text("hello\n", encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=root))

            syntax = registry.run("parse_ast", {"path": "bad.py"})
            non_python = registry.run("parse_ast", {"path": "notes.txt"})
            missing = registry.run("parse_ast", {"path": "missing.py"})

        self.assertIn("SyntaxError", syntax)
        self.assertIn("not a Python file", non_python)
        self.assertIn("file does not exist", missing)


if __name__ == "__main__":
    unittest.main()
