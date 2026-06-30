from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from insightagent.cli.tool_profiles import tool_names_for_profile
from insightagent.runtime.tool_context import ToolContext
from insightagent.tools import ToolRegistry


SAMPLE = '''\
class Calculator:
    def add(self, a, b):
        return a + b

    def sub(self, a, b):
        return a - b


def main():
    print(Calculator().add(1, 2))
'''


class RepoMapToolTests(unittest.TestCase):
    def test_registered_and_in_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(context=ToolContext(workspace=Path(directory)))
            names = {schema["name"] for schema in registry.schemas()}

            self.assertIn("repo_map", names)
            self.assertIn("find_symbol", names)
            self.assertIn("repo_map", tool_names_for_profile("coding-basic"))
            self.assertIn("find_symbol", tool_names_for_profile("analysis"))

    def test_repo_map_lists_python_symbols_and_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "calc.py").write_text(SAMPLE, encoding="utf-8")
            (root / "README.md").write_text("# hi\n", encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=root))

            result = registry.run("repo_map", {})
            payload = json.loads(result)

            self.assertEqual(payload["python_files"], 1)
            entry = payload["files"][0]
            self.assertEqual(entry["path"], "calc.py")
            class_names = {item["name"] for item in entry["classes"]}
            self.assertIn("Calculator", class_names)
            self.assertIn("add", entry["classes"][0]["methods"])
            self.assertIn("main", {fn["name"] for fn in entry["functions"]})
            self.assertIn("README.md", payload["other_files"])

    def test_repo_map_skips_ignored_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "real.py").write_text("def f():\n    pass\n", encoding="utf-8")
            cache = root / "__pycache__"
            cache.mkdir()
            (cache / "junk.py").write_text("def g():\n    pass\n", encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=root))

            payload = json.loads(registry.run("repo_map", {}))
            paths = {entry["path"] for entry in payload["files"]}

            self.assertIn("real.py", paths)
            self.assertNotIn("__pycache__/junk.py", paths)


class FindSymbolToolTests(unittest.TestCase):
    def test_finds_class_function_and_method(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "calc.py").write_text(SAMPLE, encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=root))

            cls = json.loads(registry.run("find_symbol", {"name": "Calculator"}))
            fn = json.loads(registry.run("find_symbol", {"name": "main"}))
            method = json.loads(registry.run("find_symbol", {"name": "add", "kind": "method"}))

            self.assertEqual(cls["matches"][0]["kind"], "class")
            self.assertEqual(cls["matches"][0]["path"], "calc.py")
            self.assertEqual(fn["matches"][0]["kind"], "function")
            self.assertEqual(method["matches"][0]["kind"], "method")
            self.assertEqual(method["matches"][0]["class"], "Calculator")

    def test_kind_filter_excludes_other_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "calc.py").write_text(SAMPLE, encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=root))

            result = json.loads(registry.run("find_symbol", {"name": "add", "kind": "function"}))

            self.assertEqual(result["matches"], [])

    def test_no_match_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "calc.py").write_text(SAMPLE, encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=root))

            result = json.loads(registry.run("find_symbol", {"name": "does_not_exist"}))

            self.assertEqual(result["matches"], [])


if __name__ == "__main__":
    unittest.main()
