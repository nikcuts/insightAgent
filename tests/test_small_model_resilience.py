"""End-to-end tests proving a weak model can complete tasks via the framework.

Each test drives the real CodeAgent + real tools with a scripted fake model that
imitates a specific weak-model failure mode, and asserts the resilience layer
recovers it deterministically (no network, no real LLM).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from insightagent.agent.core import CodeAgent
from insightagent.api.messages import Message, ModelResponse, ToolCall
from insightagent.api.resilience import RetryPolicy
from insightagent.runtime.tool_context import ToolContext
from insightagent.tools import ToolRegistry, default_tools


def _no_delay_policy(max_attempts: int = 2) -> RetryPolicy:
    return RetryPolicy(max_attempts=max_attempts, sleep=lambda _s: None)


class ScriptedModel:
    """A fake model whose responses are scripted; records tool_choice it sees."""

    model = "scripted"

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = responses
        self.calls = 0
        self.tool_choices: list[Any] = []

    def complete(self, messages, tools, tool_choice=None):  # noqa: ANN001
        self.calls += 1
        self.tool_choices.append(tool_choice)
        if self._responses:
            return self._responses.pop(0)
        return ModelResponse(content="(no more scripted responses)", tool_calls=[])


def _agent(workspace: Path, model, **kwargs) -> CodeAgent:
    context = ToolContext(workspace=workspace, permission_mode="workspace-write")
    tools = ToolRegistry(tools=default_tools(context), context=context, retry_policy=_no_delay_policy())
    return CodeAgent(model, tools=tools, system_prompt="test", require_tool_use=True, **kwargs)


CALC = "def calculate(a, op, b):\n    return {'+': a + b, '-': a - b, '*': a * b, '/': a / b}[op]\n"


class SmallModelResilienceTests(unittest.TestCase):
    def test_codeblock_only_model_still_writes_and_runs_file(self) -> None:
        model = ScriptedModel(
            [
                ModelResponse(content=f"Here is the code, save as calculator.py:\n```python\n{CALC}```"),
                ModelResponse(
                    content='<tool_call>{"name": "execute_command", "arguments": {"command": "python3 calculator.py"}}</tool_call>'
                ),
                ModelResponse(content="Done: calculator.py written and verified."),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            events: list[dict[str, Any]] = []
            agent = _agent(workspace, model, max_tool_iterations=8)
            result = agent.run_turn_with_trace("Write calculator.py and run it", trace=events.append)

            self.assertTrue((workspace / "calculator.py").exists())
        recovered = [e for e in events if e["type"] == "tool_call_recovered"]
        self.assertEqual({e["source"] for e in recovered}, {"codeblock", "protocol"})
        self.assertIn("Done", result.content)

    def test_repeated_readonly_call_is_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "notes.txt").write_text("hello", encoding="utf-8")
            read = ToolCall(id="r1", name="read_file", arguments={"path": "notes.txt"})
            model = ScriptedModel(
                [
                    ModelResponse(tool_calls=[read]),
                    ModelResponse(tool_calls=[ToolCall(id="r2", name="read_file", arguments={"path": "notes.txt"})]),
                    ModelResponse(content="Summarized from cached read."),
                ]
            )
            events: list[dict[str, Any]] = []
            agent = _agent(workspace, model, max_tool_iterations=6)
            agent.run_turn_with_trace("read notes.txt", trace=events.append)

        tool_results = [e for e in events if e["type"] == "tool_result"]
        # The second identical read must be a suppressed duplicate (no re-read).
        self.assertTrue(any(e.get("suppressed") for e in tool_results))
        suppressed = [e for e in tool_results if e.get("suppressed")]
        self.assertIn("刚刚执行过", suppressed[0]["content"])
        self.assertFalse(suppressed[0]["is_error"])

    def test_failed_task_finalizes_with_text_without_deadlock(self) -> None:
        model = ScriptedModel(
            [
                ModelResponse(content="plan", tool_calls=[ToolCall(id="w", name="write_file", arguments={"path": "bug.py", "content": "print(1/0)\n"})]),
                ModelResponse(content="run", tool_calls=[ToolCall(id="r2", name="execute_command", arguments={"command": "python3 bug.py # a"})]),
                ModelResponse(content="run", tool_calls=[ToolCall(id="r3", name="execute_command", arguments={"command": "python3 bug.py # b"})]),
                ModelResponse(content="run", tool_calls=[ToolCall(id="r4", name="execute_command", arguments={"command": "python3 bug.py # c"})]),
                ModelResponse(content="I cannot fix this within the limit: persistent ZeroDivisionError."),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            agent = _agent(Path(directory), model, max_tool_iterations=12)
            result = agent.run_turn_with_trace("write and run bug.py", trace=lambda _e: None)

        # Must NOT burn all iterations; should finalize with the model's text.
        self.assertLess(result.iterations, 12)
        self.assertIn("cannot fix", result.content)
        self.assertEqual(agent.task_state.phase.value, "failed")

    def test_nudge_escalates_to_forced_tool_choice(self) -> None:
        # Model emits prose with no tool call and no recoverable code block twice,
        # then finally calls a tool. The 2nd nudge must force a tool_choice.
        model = ScriptedModel(
            [
                ModelResponse(content="I think we should write a calculator."),
                ModelResponse(content="Let me consider the design more."),
                ModelResponse(tool_calls=[ToolCall(id="w", name="write_file", arguments={"path": "calc.py", "content": CALC})]),
                ModelResponse(content="Done."),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            agent = _agent(Path(directory), model, max_tool_iterations=8)
            agent.run_turn_with_trace("write calc.py", trace=lambda _e: None)

        forced = [tc for tc in model.tool_choices if isinstance(tc, dict) and tc.get("force_tool")]
        self.assertTrue(forced, "expected at least one forced tool_choice during escalation")

    def test_network_failure_applies_backoff_then_suppresses(self) -> None:
        slept: list[float] = []
        net_cmd = "python3 -c \"import sys; sys.stderr.write('Could not resolve host: x\\n'); sys.exit(6)\""
        model = ScriptedModel(
            [
                ModelResponse(tool_calls=[ToolCall(id="n1", name="execute_command", arguments={"command": net_cmd})]),
                ModelResponse(tool_calls=[ToolCall(id="n2", name="execute_command", arguments={"command": net_cmd})]),
                ModelResponse(content="Stopping: network is unavailable."),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            context = ToolContext(workspace=Path(directory), permission_mode="workspace-write")
            tools = ToolRegistry(
                tools=default_tools(context),
                context=context,
                retry_policy=RetryPolicy(base_delay=0.5, max_attempts=2, sleep=slept.append),
            )
            agent = CodeAgent(model, tools=tools, system_prompt="t", require_tool_use=True, max_tool_iterations=6)
            agent.run_turn_with_trace("run the network command", trace=lambda _e: None)

        # The identical retry must have triggered a backoff wait.
        self.assertTrue(slept, "expected exponential backoff sleep before retrying a network failure")


if __name__ == "__main__":
    unittest.main()
