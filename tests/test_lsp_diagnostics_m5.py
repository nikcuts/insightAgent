from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from insightagent.runtime.tool_context import ToolContext
from insightagent.tools import ToolRegistry
from insightagent.tools.state_tools import (
    _parse_pyright_output,
    pyright_available,
)


class ParsePyrightOutputTests(unittest.TestCase):
    def test_parses_diagnostics_with_one_based_position_and_rule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = ToolContext(workspace=Path(directory))
            sample = json.dumps(
                {
                    "generalDiagnostics": [
                        {
                            "file": str(context.workspace / "foo.py"),
                            "severity": "error",
                            "message": "X is not defined",
                            "rule": "reportUndefinedVariable",
                            "range": {"start": {"line": 4, "character": 2}},
                        }
                    ]
                }
            )

            parsed = _parse_pyright_output(sample, context, 100)

        self.assertEqual(
            parsed,
            ["foo.py:5:3: error: X is not defined (reportUndefinedVariable)"],
        )

    def test_invalid_json_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = ToolContext(workspace=Path(directory))

            self.assertEqual(_parse_pyright_output("not json", context, 100), [])


class LspDiagnosticsBackendTests(unittest.TestCase):
    def test_basic_checker_reports_syntax_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=root))

            result = registry.run("lsp_diagnostics", {"path": "bad.py", "checker": "basic"})

            self.assertIn("bad.py", result)
            self.assertIn("SyntaxError", result)

    def test_basic_checker_clean_file_returns_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.py").write_text("print('hi')\n", encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=root))

            result = registry.run("lsp_diagnostics", {"path": "ok.py", "checker": "basic"})

            self.assertEqual(result, "no diagnostics")

    def test_invalid_checker_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(context=ToolContext(workspace=Path(directory)))

            outcome = registry.execute("lsp_diagnostics", {"checker": "nope"})

            self.assertTrue(outcome.is_error)

    @unittest.skipIf(pyright_available(), "pyright is installed in this environment")
    def test_forced_pyright_reports_unavailable_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.py").write_text("print('hi')\n", encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=root))

            result = registry.run("lsp_diagnostics", {"checker": "pyright"})

            self.assertIn("not available", result)


if __name__ == "__main__":
    unittest.main()
