"""Long-horizon task tests: full lifecycle, repair escalation, iteration limits, usage."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from insightagent.agent import CodeAgent
from insightagent.messages import Message, ModelResponse, ToolCall
from insightagent.providers import ModelClient
from insightagent.task_state import TaskPhase
from insightagent.tool_context import ToolContext
from insightagent.tools import ToolRegistry


class ScriptedModelClient(ModelClient):
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[list[Message]] = []

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        self.calls.append(list(messages))
        return self.responses.pop(0)


def tool_response(call_id: str, name: str, arguments: dict[str, Any], content: str = "") -> ModelResponse:
    return ModelResponse(content=content, tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)])


def phase_sequence(events: list[dict[str, Any]]) -> list[str]:
    return [event["phase"] for event in events if event["type"] == "task_phase_changed"]


class LongHorizonLifecycleTests(unittest.TestCase):
    """A scripted multi-file build -> failing test -> repair -> verify -> summarize cycle."""

    BUGGY_CALC = "def add(a, b):\n    return a - b\n\n\ndef sub(a, b):\n    return a - b\n"
    TEST_CALC = (
        "from calc import add, sub\n\n"
        "assert add(2, 3) == 5\n"
        "assert sub(5, 2) == 3\n"
        "print('all tests passed')\n"
    )

    def _build_script(self, directory: str) -> list[ModelResponse]:
        return [
            tool_response(
                "call_todo",
                "todo_write",
                {
                    "todos": [
                        {"content": "write calc module", "status": "in_progress"},
                        {"content": "write tests", "status": "pending"},
                        {"content": "run tests and fix failures", "status": "pending"},
                    ]
                },
                content="Plan: build a calculator module, write tests, run them, repair failures.",
            ),
            tool_response("call_write_calc", "write_file", {"path": "calc.py", "content": self.BUGGY_CALC}),
            tool_response("call_write_test", "write_file", {"path": "test_calc.py", "content": self.TEST_CALC}),
            tool_response(
                "call_run_fail",
                "execute_command",
                {"command": "python3 test_calc.py", "cwd": directory},
            ),
            tool_response(
                "call_fix",
                "edit_file",
                {"path": "calc.py", "old": "def add(a, b):\n    return a - b", "new": "def add(a, b):\n    return a + b"},
            ),
            tool_response(
                "call_run_pass",
                "execute_command",
                {"command": "python3 test_calc.py", "cwd": directory},
            ),
            ModelResponse(content="Built calc.py with passing tests after one repair."),
        ]

    def test_full_build_test_repair_verify_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = ScriptedModelClient(self._build_script(directory))
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=Path(directory))),
                max_tool_iterations=20,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace("Build a tested calculator module", trace=events.append)

            self.assertEqual(result.content, "Built calc.py with passing tests after one repair.")
            self.assertEqual(result.iterations, 7)

            # Artifacts on disk reflect the whole task, including the repair.
            calc = (Path(directory) / "calc.py").read_text(encoding="utf-8")
            self.assertIn("return a + b", calc)
            self.assertNotIn("def add(a, b):\n    return a - b", calc)
            self.assertTrue((Path(directory) / "test_calc.py").is_file())
            todos = json.loads((Path(directory) / ".insightagent" / "todos.json").read_text(encoding="utf-8"))
            self.assertEqual(len(todos["todos"]), 3)

            # The task walked the entire phase state machine.
            self.assertEqual(phase_sequence(events), ["implement", "repair", "verify", "summarize", "done"])
            self.assertEqual(agent.task_state.phase, TaskPhase.DONE)
            self.assertEqual(agent.task_state.repair_attempts, 1)
            self.assertEqual(agent.task_state.verification_attempts, 2)

            # The failing test run triggered self-healing before the fix.
            self.assertTrue(any(event["type"] == "self_healing_repair" for event in events))
            repair_call_messages = client.calls[4]
            self.assertTrue(
                any(message.role == "user" and "Enter repair mode" in message.content for message in repair_call_messages)
            )

    def test_phase_hints_follow_task_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = ScriptedModelClient(self._build_script(directory))
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=Path(directory))),
                max_tool_iterations=20,
            )

            agent.run_turn("Build a tested calculator module")

            def hint_of(call_index: int) -> str:
                hints = [
                    message.content
                    for message in client.calls[call_index]
                    if message.role == "user" and message.content.startswith("Current task phase:")
                ]
                self.assertEqual(len(hints), 1)
                return hints[0]

            self.assertIn("plan", hint_of(0))
            self.assertIn("implement", hint_of(2))
            self.assertIn("repair", hint_of(4))
            self.assertIn("verify", hint_of(5))
            self.assertIn("summarize", hint_of(6))


class RepairEscalationTests(unittest.TestCase):
    def test_repeated_failures_escalate_to_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            failing_call = {"command": "python3 missing_script.py", "cwd": directory}
            client = ScriptedModelClient(
                [
                    tool_response("call_1", "execute_command", failing_call),
                    tool_response("call_2", "execute_command", failing_call),
                    tool_response("call_3", "execute_command", failing_call),
                    ModelResponse(content="Could not repair the task within the limit."),
                    ModelResponse(content="fresh start"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=Path(directory))),
                max_tool_iterations=20,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace("Run the missing script", trace=events.append)

            self.assertEqual(result.content, "Could not repair the task within the limit.")
            self.assertEqual(phase_sequence(events), ["repair", "failed"])
            self.assertEqual(agent.task_state.phase, TaskPhase.FAILED)
            self.assertEqual(agent.task_state.repair_attempts, 3)
            self.assertIsNotNone(agent.task_state.last_error)

            # A new user turn must reset the task state back to planning.
            second = agent.run_turn("Try something else")
            self.assertEqual(second.content, "fresh start")
            self.assertEqual(agent.task_state.phase, TaskPhase.DONE)
            self.assertEqual(agent.task_state.repair_attempts, 0)
            self.assertTrue(
                any(
                    message.role == "user" and "Current task phase: plan" in message.content
                    for message in client.calls[4]
                )
            )

    def test_iteration_limit_marks_task_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            responses = [
                tool_response(f"call_{index}", "write_file", {"path": f"step_{index}.txt", "content": f"step {index}\n"})
                for index in range(4)
            ]
            client = ScriptedModelClient(responses)
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=Path(directory))),
                max_tool_iterations=4,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace("Never finish", trace=events.append)

            self.assertIn("max tool-iteration limit", result.content)
            self.assertEqual(result.iterations, 4)
            self.assertEqual(agent.task_state.phase, TaskPhase.FAILED)
            for index in range(4):
                self.assertTrue((Path(directory) / f"step_{index}.txt").is_file())
            final_events = [event for event in events if event["type"] == "final_answer"]
            self.assertEqual(len(final_events), 1)
            self.assertEqual(final_events[0]["iterations"], 4)


class LongTaskUsageTests(unittest.TestCase):
    def test_usage_accumulates_across_long_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            responses = [
                tool_response(f"call_{index}", "write_file", {"path": f"part_{index}.py", "content": f"VALUE = {index}\n"})
                for index in range(5)
            ] + [ModelResponse(content="All five parts written.")]
            client = ScriptedModelClient(responses)
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=Path(directory))),
                max_tool_iterations=20,
            )

            result = agent.run_turn("Write five module parts")

            self.assertEqual(result.content, "All five parts written.")
            self.assertEqual(agent.usage_tracker.turns, 6)
            self.assertGreater(agent.usage_tracker.total_input_tokens_est, 0)
            self.assertGreater(agent.usage_tracker.total_output_tokens_est, 0)
            self.assertEqual(
                agent.usage_tracker.total_tokens_est,
                agent.usage_tracker.total_input_tokens_est + agent.usage_tracker.total_output_tokens_est,
            )
            # Input context grows monotonically across a long task (history accumulates).
            input_sizes = [sample.input_chars for sample in agent.usage_tracker.samples]
            self.assertEqual(input_sizes, sorted(input_sizes))


if __name__ == "__main__":
    unittest.main()
