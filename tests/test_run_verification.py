from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from insightagent.agent.task_state import TaskPhase, TaskState, transition_after_tool
from insightagent.cli.tool_profiles import tool_names_for_profile
from insightagent.runtime.failure_classifier import FailureKind
from insightagent.runtime.tool_context import ToolContext
from insightagent.tools import ToolRegistry
from insightagent.tools.execution_tools import detect_verification_command


class DetectVerificationCommandTests(unittest.TestCase):
    def test_prefers_npm_test_script(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text(
                '{"scripts": {"test": "jest"}}', encoding="utf-8"
            )

            strategy, command = detect_verification_command(root)

        self.assertEqual(strategy, "npm-test")
        self.assertEqual(command, "npm test")

    def test_falls_back_to_py_compile_for_plain_python_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("print('hi')\n", encoding="utf-8")

            strategy, command = detect_verification_command(root)

        self.assertEqual(strategy, "py_compile")
        self.assertIn("py_compile", command or "")
        self.assertIn("app.py", command or "")

    def test_returns_none_when_nothing_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            strategy, command = detect_verification_command(Path(directory))

        self.assertEqual(strategy, "none")
        self.assertIsNone(command)


class RunVerificationToolTests(unittest.TestCase):
    def test_registered_in_default_registry_and_coding_basic_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(context=ToolContext(workspace=Path(directory)))
            names = {schema["name"] for schema in registry.schemas()}

        self.assertIn("run_verification", names)
        self.assertIn("run_verification", tool_names_for_profile("coding-basic"))

    def test_auto_detect_compile_passes_for_valid_python(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=root))

            result = registry.execute("run_verification", {})

        self.assertFalse(result.is_error)
        self.assertTrue(result.content.startswith("exit_code: 0\n"))
        self.assertIn("strategy: py_compile", result.content)

    def test_auto_detect_compile_fails_and_is_classified_as_code_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
            registry = ToolRegistry(context=ToolContext(workspace=root))

            result = registry.execute("run_verification", {})

        self.assertTrue(result.is_error)
        self.assertFalse(result.content.startswith("exit_code: 0\n"))
        self.assertEqual(result.failure_kind, FailureKind.CODE_ERROR)

    def test_explicit_command_overrides_detection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ToolRegistry(context=ToolContext(workspace=root))

            result = registry.execute("run_verification", {"command": "python3 -c \"print('ok')\""})

        self.assertFalse(result.is_error)
        self.assertIn("strategy: explicit", result.content)
        self.assertIn("ok", result.content)

    def test_no_strategy_returns_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(context=ToolContext(workspace=Path(directory)))

            result = registry.execute("run_verification", {})

        self.assertTrue(result.is_error)
        self.assertIn("strategy: none", result.content)
        self.assertIn("explicit", result.content)


class RunVerificationTaskStateTests(unittest.TestCase):
    def test_successful_run_verification_moves_to_summarize(self) -> None:
        state = TaskState(phase=TaskPhase.VERIFY)

        _old, new_phase = transition_after_tool(
            state, "run_verification", "exit_code: 0\nstrategy: pytest\n", is_error=False
        )

        self.assertEqual(new_phase, TaskPhase.SUMMARIZE)
        self.assertEqual(state.verification_attempts, 1)

    def test_failed_run_verification_moves_to_repair(self) -> None:
        state = TaskState(phase=TaskPhase.VERIFY)

        _old, new_phase = transition_after_tool(
            state, "run_verification", "exit_code: 1\nstrategy: pytest\n1 failed", is_error=True
        )

        self.assertEqual(new_phase, TaskPhase.REPAIR)
        self.assertEqual(state.repair_attempts, 1)


if __name__ == "__main__":
    unittest.main()
