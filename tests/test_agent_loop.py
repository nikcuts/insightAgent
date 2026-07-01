from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from insightagent.agent.core import CodeAgent
from insightagent.agent.task_state import TaskPhase, TaskState
from insightagent.api.messages import Message, ModelResponse, ToolCall
from insightagent.api.providers import ModelClient, ToolArgumentsParseError
from insightagent.runtime.tool_context import ToolContext
from insightagent.tools import ToolRegistry


class FakeModelClient(ModelClient):
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.calls: list[list[Message]] = []

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        self.calls.append(list(messages))
        return self.responses.pop(0)


class BadArgumentsThenSuccessClient(ModelClient):
    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            raise ToolArgumentsParseError("write_file", '{"path": "x.py", "content": "bad"', "mock parse error")
        return ModelResponse(content="recovered")


class BadArgumentsThenNoToolThenSuccessClient(ModelClient):
    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            raise ToolArgumentsParseError("write_file", '{"path": "x.py", "content": "bad"', "mock parse error")
        if len(self.calls) == 2:
            return ModelResponse(content="I will retry now.")
        if len(self.calls) == 3:
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="write_file",
                        arguments={"path": "recovered.txt", "content": "ok"},
                    )
                ]
            )
        return ModelResponse(content="recovered")


class AgentLoopTests(unittest.TestCase):
    def test_executes_tool_and_returns_final_answer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.txt"
            path.write_text("hello from file", encoding="utf-8")
            client = FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_1",
                                name="read_file",
                                arguments={"path": str(path)},
                            )
                        ]
                    ),
                    ModelResponse(content="The file says: hello from file"),
                ]
            )
            agent = CodeAgent(client, tools=ToolRegistry(context=ToolContext(workspace=Path(directory))))

            result = agent.run_turn("Read the note")

            self.assertEqual(result.content, "The file says: hello from file")
            self.assertEqual(result.iterations, 2)
            self.assertEqual(len(client.calls), 2)
            second_call_messages = client.calls[1]
            self.assertTrue(any(message.role == "tool" and "hello from file" in message.content for message in second_call_messages))

    def test_sliding_window_keeps_recent_non_system_messages(self) -> None:
        client = FakeModelClient([ModelResponse(content="done")])
        agent = CodeAgent(client)
        for index in range(30):
            agent.messages.append(Message(role="user", content=f"old-{index}"))

        result = agent.run_turn("latest")

        self.assertEqual(result.content, "done")
        self.assertEqual(result.messages[0].role, "system")
        non_system = [message for message in result.messages if message.role != "system"]
        self.assertLessEqual(len(non_system), 20)
        self.assertTrue(any(message.content == "latest" for message in non_system))

    def test_tool_error_is_returned_to_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_1",
                                name="read_file",
                                arguments={"path": "missing.txt"},
                            )
                        ]
                    ),
                    ModelResponse(content="The read failed."),
                ]
            )
            agent = CodeAgent(client, tools=ToolRegistry(context=ToolContext(workspace=Path(directory))))

            result = agent.run_turn("Read missing file")

            self.assertEqual(result.content, "The read failed.")
            tool_messages = [message for message in client.calls[1] if message.role == "tool"]
            self.assertEqual(len(tool_messages), 1)
            self.assertTrue(tool_messages[0].is_error)
            self.assertIn("FileNotFoundError", tool_messages[0].content)

    def test_trace_events_include_tool_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.txt"
            path.write_text("trace data", encoding="utf-8")
            client = FakeModelClient(
                [
                    ModelResponse(
                        content="Plan: read the file.",
                        tool_calls=[
                            ToolCall(
                                id="call_1",
                                name="read_file",
                                arguments={"path": str(path)},
                            )
                        ],
                    ),
                    ModelResponse(content="Done."),
                ]
            )
            agent = CodeAgent(client, tools=ToolRegistry(context=ToolContext(workspace=Path(directory))))
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace("Trace this", trace=events.append)

            self.assertEqual(result.content, "Done.")
            event_types = [event["type"] for event in events]
            self.assertIn("model_response", event_types)
            self.assertIn("tool_call", event_types)
            self.assertIn("tool_result", event_types)
            self.assertEqual(event_types[-1], "final_answer")

    def test_repository_repair_injects_structure_snapshot_before_first_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
            (workspace / "test_calc.py").write_text("from calc import add\n", encoding="utf-8")
            (workspace / ".env").write_text("SECRET=value\n", encoding="utf-8")
            (workspace / ".git").mkdir()
            (workspace / ".git" / "config").write_text("[core]\n", encoding="utf-8")
            client = FakeModelClient([ModelResponse(content="done")])
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=workspace)),
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace(
                "This is a SWE-bench-style repository repair task. Fix the bug.",
                trace=events.append,
            )

            self.assertEqual(result.content, "done")
            first_call_content = "\n".join(message.content for message in client.calls[0])
            self.assertIn("Repository snapshot for repair", first_call_content)
            self.assertIn("calc.py", first_call_content)
            self.assertIn("test_calc.py", first_call_content)
            self.assertNotIn("return a - b", first_call_content)
            self.assertNotIn(".env", first_call_content)
            self.assertNotIn(".git/config", first_call_content)
            self.assertTrue(any(event["type"] == "repository_snapshot_injected" for event in events))

    def test_non_repository_repair_task_does_not_inject_structure_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
            client = FakeModelClient([ModelResponse(content="done")])
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=workspace)),
            )

            result = agent.run_turn("Create a small helper file")

            self.assertEqual(result.content, "done")
            first_call_content = "\n".join(message.content for message in client.calls[0])
            self.assertNotIn("Repository snapshot for repair", first_call_content)

    def test_failed_command_triggers_self_healing_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_1",
                                name="execute_command",
                                arguments={"command": "python3 missing.py", "cwd": str(directory)},
                            )
                        ]
                    ),
                    ModelResponse(content="I repaired the issue."),
                ]
            )
            agent = CodeAgent(client, tools=ToolRegistry(context=ToolContext(workspace=Path(directory))))
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace("Run missing file", trace=events.append)

            self.assertEqual(result.content, "I repaired the issue.")
            self.assertTrue(any(event["type"] == "self_healing_repair" for event in events))
            second_call_messages = client.calls[1]
            self.assertTrue(
                any(
                    message.role == "user" and "Enter repair mode" in message.content
                    for message in second_call_messages
                )
            )

    def test_failed_phase_stops_without_accepting_next_model_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_1",
                                name="execute_command",
                                arguments={
                                    "command": "python -c \"raise SystemExit(1)\"",
                                    "cwd": str(directory),
                                },
                            )
                        ]
                    ),
                    ModelResponse(content="Next I will keep trying."),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=Path(directory))),
                task_state=TaskState(max_repairs=1),
                max_tool_iterations=5,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace("Run the failing command", trace=events.append)

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(agent.task_state.phase, TaskPhase.FAILED)
        self.assertIn("repair limit", result.content)
        self.assertNotIn("Next I will keep trying", result.content)
        self.assertTrue(any(event["type"] == "task_failed" for event in events))

    def test_require_tool_use_reprompts_when_model_only_describes_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "created.txt"
            client = FakeModelClient(
                [
                    ModelResponse(content="I will call write_file now, but only in prose."),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_1",
                                name="write_file",
                                arguments={"path": "created.txt", "content": "ok"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_2",
                                name="run_verification",
                                arguments={"command": "python3 -c \"print('ok')\""},
                            )
                        ]
                    ),
                    ModelResponse(content="done"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=Path(directory))),
                require_tool_use=True,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace("Create a file", trace=events.append)

            self.assertEqual(result.content, "done")
            self.assertEqual(path.read_text(encoding="utf-8"), "ok")
            self.assertTrue(any(event["type"] == "tool_use_required" for event in events))

    def test_implement_without_verification_is_nudged_not_finalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_1",
                                name="write_file",
                                arguments={"path": "main.py", "content": "print('hi')\n"},
                            )
                        ]
                    ),
                    # The model tries to stop after writing but before verifying. The
                    # runtime must keep steering it instead of accepting this as final.
                    ModelResponse(content="I have created the file. Next I will verify it."),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(id="call_2", name="run_verification", arguments={}),
                        ]
                    ),
                    ModelResponse(content="done"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=Path(directory))),
                require_tool_use=True,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace("Create and verify a script", trace=events.append)

            self.assertEqual(result.content, "done")
            self.assertGreaterEqual(len(client.calls), 4)
            self.assertTrue(any(event["type"] == "tool_use_required" for event in events))
            self.assertTrue(
                any(
                    event["type"] == "tool_result" and event["name"] == "run_verification"
                    for event in events
                )
            )

    def test_verify_phase_without_verification_is_nudged_not_finalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "main.py"
            path.write_text("value = 1\n", encoding="utf-8")
            client = FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_1",
                                name="edit_file",
                                arguments={"path": "main.py", "old": "missing", "new": "value = 2"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_2",
                                name="edit_file",
                                arguments={"path": "main.py", "old": "value = 1", "new": "value = 2"},
                            )
                        ]
                    ),
                    ModelResponse(content="I changed the code. Next I will verify it."),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_3",
                                name="run_verification",
                                arguments={"command": "python -c \"print('ok')\""},
                            ),
                        ]
                    ),
                    ModelResponse(content="done"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=Path(directory))),
                require_tool_use=True,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace("Repair and verify a script", trace=events.append)

            self.assertEqual(result.content, "done")
            self.assertEqual(path.read_text(encoding="utf-8"), "value = 2\n")
            self.assertGreaterEqual(len(client.calls), 5)
            self.assertTrue(any(event["type"] == "tool_use_required" for event in events))
            self.assertTrue(
                any(
                    event["type"] == "tool_result" and event["name"] == "run_verification"
                    for event in events
                )
            )

    def test_repair_phase_forces_targeted_edit_before_write_file(self) -> None:
        agent = CodeAgent(FakeModelClient([]), require_tool_use=True)
        agent.task_state.phase = TaskPhase.REPAIR

        self.assertEqual(agent._forced_tool_for_phase(), "edit_file")

    def test_repository_repair_raises_default_repair_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            client = FakeModelClient([ModelResponse(content="done")])
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=workspace)),
                require_tool_use=False,
            )

            agent.run_turn(
                "This is a SWE-bench-style repository repair task.\n"
                "Run this exact verification command before finalizing: python -c \"print('ok')\""
            )

            self.assertGreaterEqual(agent.task_state.max_repairs, 8)

    def test_malformed_tool_arguments_enter_repair_loop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = BadArgumentsThenSuccessClient()
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=Path(directory))),
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace("Create a file", trace=events.append)

            self.assertEqual(result.content, "recovered")
            self.assertEqual(len(client.calls), 2)
            self.assertTrue(any(event["type"] == "tool_arguments_invalid" for event in events))
            self.assertTrue(
                any(
                    message.role == "user" and "malformed JSON arguments" in message.content
                    for message in client.calls[1]
                )
            )

    def test_pending_repair_reprompts_when_model_returns_no_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = BadArgumentsThenNoToolThenSuccessClient()
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=Path(directory))),
                require_tool_use=True,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace("Create a file", trace=events.append)

            self.assertEqual(result.content, "recovered")
            self.assertEqual((Path(directory) / "recovered.txt").read_text(encoding="utf-8"), "ok")
            self.assertEqual(len(client.calls), 4)
            self.assertTrue(any(event["type"] == "tool_use_required" for event in events))

    def test_task_state_emits_phase_events_and_phase_hints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeModelClient(
                [
                    ModelResponse(
                        content="Plan: create then verify.",
                        tool_calls=[
                            ToolCall(
                                id="call_1",
                                name="write_file",
                                arguments={"path": "ok.py", "content": "print('ok')\n"},
                            )
                        ],
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_2",
                                name="execute_command",
                                arguments={"command": "python3 ok.py", "cwd": str(directory)},
                            )
                        ]
                    ),
                    ModelResponse(content="Created ok.py and verified it."),
                ]
            )
            agent = CodeAgent(client, tools=ToolRegistry(context=ToolContext(workspace=Path(directory))))
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace("Create and verify", trace=events.append)

        self.assertEqual(result.content, "Created ok.py and verified it.")
        phase_events = [event for event in events if event["type"] == "task_phase_changed"]
        self.assertEqual([event["phase"] for event in phase_events], ["implement", "summarize", "done"])
        first_call_messages = client.calls[0]
        self.assertTrue(
            any(
                message.role == "user" and "Current task phase: plan" in message.content
                for message in first_call_messages
            )
        )
        final_call_messages = client.calls[-1]
        self.assertTrue(
            any(
                message.role == "user" and "Current task phase: summarize" in message.content
                for message in final_call_messages
            )
        )

    def test_summary_code_block_is_not_recovered_as_write_file_after_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_1",
                                name="write_file",
                                arguments={"path": "ok.py", "content": "print('ok')\n"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_2",
                                name="run_verification",
                                arguments={"command": "python ok.py", "cwd": str(directory)},
                            )
                        ]
                    ),
                    ModelResponse(
                        content=(
                            "Summary: verified successfully.\n"
                            "```python\n"
                            "print('this is documentation, not a new tool call')\n"
                            "```"
                        )
                    ),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=Path(directory))),
                require_tool_use=True,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace("Create and verify ok.py", trace=events.append)

            self.assertIn("verified successfully", result.content)
            self.assertEqual((Path(directory) / "ok.py").read_text(encoding="utf-8"), "print('ok')\n")
            self.assertFalse(any(event["type"] == "tool_call_recovered" for event in events))
            self.assertEqual(client.responses, [])

    def test_wrong_explicit_verification_command_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exact_command = "python -c \"print('exact')\""
            wrong_command = "python -c \"print('wrong')\""
            client = FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_1",
                                name="write_file",
                                arguments={"path": "ok.py", "content": "print('ok')\n"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_2",
                                name="run_verification",
                                arguments={"command": wrong_command, "cwd": str(directory)},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_3",
                                name="run_verification",
                                arguments={"command": exact_command, "cwd": str(directory)},
                            )
                        ]
                    ),
                    ModelResponse(content="done"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=Path(directory))),
                require_tool_use=True,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace(
                "Repair the existing repository.\n"
                f"Run this exact verification command before finalizing: {exact_command}",
                trace=events.append,
            )

            self.assertEqual(result.content, "done")
            rejected = [
                event
                for event in events
                if event["type"] == "tool_result" and event["id"] == "call_2"
            ]
            self.assertEqual(len(rejected), 1)
            self.assertTrue(rejected[0]["is_error"])
            self.assertIn("does not match the required verification command", rejected[0]["content"])
            self.assertNotIn("wrong", rejected[0]["content"])

    def test_execute_command_cannot_bypass_explicit_verification_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exact_command = "python -m unittest discover -s . -v"
            wrong_command = "python -m pytest tests/test_wrong.py"
            client = FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_1",
                                name="write_file",
                                arguments={"path": "ok.py", "content": "print('ok')\n"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_2",
                                name="execute_command",
                                arguments={"command": wrong_command, "cwd": str(directory)},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_3",
                                name="run_verification",
                                arguments={"command": exact_command, "cwd": str(directory)},
                            )
                        ]
                    ),
                    ModelResponse(content="done"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=Path(directory))),
                require_tool_use=True,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace(
                "Repair the existing repository.\n"
                f"Run this exact verification command before finalizing: {exact_command}",
                trace=events.append,
            )

            self.assertEqual(result.content, "done")
            rejected = [
                event
                for event in events
                if event["type"] == "tool_result" and event["id"] == "call_2"
            ]
            self.assertEqual(len(rejected), 1)
            self.assertTrue(rejected[0]["is_error"])
            self.assertIn("does not match the required verification command", rejected[0]["content"])

    def test_repository_repair_allows_execute_command_for_read_only_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
            exact_command = "python -c \"print('ok')\""
            inspect_command = "python -c \"print('calc.py')\""
            client = FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_1",
                                name="read_file",
                                arguments={"path": "calc.py"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_2",
                                name="execute_command",
                                arguments={"command": inspect_command, "cwd": str(workspace)},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_3",
                                name="write_file",
                                arguments={"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_4",
                                name="run_verification",
                                arguments={"command": exact_command, "cwd": str(workspace)},
                            )
                        ]
                    ),
                    ModelResponse(content="done"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=workspace)),
                require_tool_use=True,
                max_tool_iterations=10,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace(
                "This is a SWE-bench-style repository repair task. Fix the existing bug.\n"
                f"Run this exact verification command before finalizing: {exact_command}",
                trace=events.append,
            )

            self.assertEqual(result.content, "done")
            inspection = [
                event
                for event in events
                if event["type"] == "tool_result" and event["id"] == "call_2"
            ]
            self.assertEqual(len(inspection), 1)
            self.assertFalse(inspection[0]["is_error"])
            self.assertIn("calc.py", inspection[0]["content"])
            self.assertNotIn("does not match the required verification command", inspection[0]["content"])

    def test_execute_command_requires_existing_patch_before_repository_repair_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
            exact_command = "python -c \"print('ok')\""
            client = FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(id="call_1", name="read_file", arguments={"path": "calc.py"})
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_2",
                                name="write_file",
                                arguments={"path": "addition.py", "content": "print(1 + 1)\n"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_3",
                                name="execute_command",
                                arguments={"command": exact_command, "cwd": str(workspace)},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_4",
                                name="read_file",
                                arguments={"path": "calc.py"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_5",
                                name="write_file",
                                arguments={"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_6",
                                name="execute_command",
                                arguments={"command": exact_command, "cwd": str(workspace)},
                            )
                        ]
                    ),
                    ModelResponse(content="done"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=workspace)),
                require_tool_use=True,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace(
                "This is a SWE-bench-style repository repair task. Fix the existing bug.\n"
                f"Run this exact verification command before finalizing: {exact_command}",
                trace=events.append,
            )

            self.assertEqual(result.content, "done")
            rejected = [
                event
                for event in events
                if event["type"] == "tool_result" and event["id"] == "call_3"
            ]
            self.assertEqual(len(rejected), 1)
            self.assertTrue(rejected[0]["is_error"])
            self.assertIn("Modify at least one existing non-test repository file", rejected[0]["content"])

    def test_repository_repair_requires_inspection_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
            exact_command = "python -c \"print('ok')\""
            client = FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_1",
                                name="write_file",
                                arguments={"path": "addition.py", "content": "print(1 + 1)\n"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_2",
                                name="read_file",
                                arguments={"path": "calc.py"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_3",
                                name="write_file",
                                arguments={"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_4",
                                name="run_verification",
                                arguments={"command": exact_command, "cwd": str(workspace)},
                            )
                        ]
                    ),
                    ModelResponse(content="done"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=workspace)),
                require_tool_use=True,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace(
                "This is a SWE-bench-style repository repair task. Fix the existing bug.\n"
                f"Run this exact verification command before finalizing: {exact_command}",
                trace=events.append,
            )

            self.assertEqual(result.content, "done")
            self.assertFalse((workspace / "addition.py").exists())
            self.assertEqual((workspace / "calc.py").read_text(encoding="utf-8"), "def add(a, b):\n    return a + b\n")
            rejected = [
                event
                for event in events
                if event["type"] == "tool_result" and event["id"] == "call_1"
            ]
            self.assertEqual(len(rejected), 1)
            self.assertTrue(rejected[0]["is_error"])
            self.assertIn("Inspect the existing repository before modifying files", rejected[0]["content"])

    def test_repository_repair_requires_failing_test_read_before_source_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            tests_dir = workspace / "tests"
            tests_dir.mkdir()
            (workspace / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
            (tests_dir / "test_calc.py").write_text(
                "from calc import add\n\n"
                "def test_add():\n"
                "    assert add(2, 3) == 5\n",
                encoding="utf-8",
            )
            exact_command = "python -c \"print('ok')\""
            client = FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_1",
                                name="grep_search",
                                arguments={"pattern": "test_add", "glob": "tests/test_calc.py", "max_results": 50},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_2",
                                name="edit_file",
                                arguments={"path": "calc.py", "old": "return a - b", "new": "return a + b"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_3",
                                name="read_file",
                                arguments={"path": "tests/test_calc.py", "start_line": 1, "max_lines": 20},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_4",
                                name="edit_file",
                                arguments={"path": "calc.py", "old": "return a - b", "new": "return a + b"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_5",
                                name="run_verification",
                                arguments={"command": exact_command, "cwd": str(workspace)},
                            )
                        ]
                    ),
                    ModelResponse(content="done"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=workspace)),
                require_tool_use=True,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace(
                "This is a SWE-bench-style repository repair task. Fix the existing bug.\n"
                "Fail-to-pass tests: ['tests/test_calc.py::test_add']\n"
                f"Run this exact verification command before finalizing: {exact_command}",
                trace=events.append,
            )

            self.assertEqual(result.content, "done")
            self.assertIn("return a + b", (workspace / "calc.py").read_text(encoding="utf-8"))
            rejected = [
                event
                for event in events
                if event["type"] == "tool_result" and event["id"] == "call_2"
            ]
            self.assertEqual(len(rejected), 1)
            self.assertTrue(rejected[0]["is_error"])
            self.assertIn("Read the fail-to-pass test body", rejected[0]["content"])

    def test_repository_repair_rejects_duplicate_python_method_insertion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "blueprints.py").write_text(
                "class Blueprint:\n"
                "    \"\"\"Blueprint docs.\"\"\"\n"
                "\n"
                "    def __init__(self, name):\n"
                "        self.name = name\n",
                encoding="utf-8",
            )
            exact_command = "python -c \"print('ok')\""
            client = FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_1",
                                name="read_file",
                                arguments={"path": "blueprints.py"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_2",
                                name="edit_file",
                                arguments={
                                    "path": "blueprints.py",
                                    "old": "class Blueprint:",
                                    "new": (
                                        "class Blueprint:\n\n"
                                        "    def __init__(self, name):\n"
                                        "        if '.' in name:\n"
                                        "            raise ValueError('bad name')"
                                    ),
                                },
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_3",
                                name="edit_file",
                                arguments={
                                    "path": "blueprints.py",
                                    "old": (
                                        "    def __init__(self, name):\n"
                                        "        self.name = name\n"
                                    ),
                                    "new": (
                                        "    def __init__(self, name):\n"
                                        "        if '.' in name:\n"
                                        "            raise ValueError('bad name')\n"
                                        "        self.name = name\n"
                                    ),
                                },
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_4",
                                name="run_verification",
                                arguments={"command": exact_command, "cwd": str(workspace)},
                            )
                        ]
                    ),
                    ModelResponse(content="done"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=workspace)),
                require_tool_use=True,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace(
                "This is a SWE-bench-style repository repair task. Fix the existing bug.\n"
                f"Run this exact verification command before finalizing: {exact_command}",
                trace=events.append,
            )

            self.assertEqual(result.content, "done")
            rejected = [
                event
                for event in events
                if event["type"] == "tool_result" and event["id"] == "call_2"
            ]
            self.assertEqual(len(rejected), 1)
            self.assertTrue(rejected[0]["is_error"])
            self.assertIn("already defines method", rejected[0]["content"])
            self.assertIn("parse_ast", rejected[0]["content"])
            self.assertEqual((workspace / "blueprints.py").read_text(encoding="utf-8").count("def __init__"), 1)

    def test_repository_repair_requires_inspection_after_failed_verification_before_editing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
            exact_command = 'python -B -c "from calc import add; assert add(2, 3) == 5"'
            client = FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(id="call_1", name="read_file", arguments={"path": "calc.py"})
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_2",
                                name="edit_file",
                                arguments={"path": "calc.py", "old": "return a - b", "new": "return a * b"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_3",
                                name="run_verification",
                                arguments={"command": exact_command, "cwd": str(workspace)},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_4",
                                name="edit_file",
                                arguments={"path": "calc.py", "old": "return a * b", "new": "return a + b"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(id="call_5", name="read_file", arguments={"path": "calc.py"})
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_6",
                                name="edit_file",
                                arguments={"path": "calc.py", "old": "return a * b", "new": "return a + b"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_7",
                                name="run_verification",
                                arguments={"command": exact_command, "cwd": str(workspace)},
                            )
                        ]
                    ),
                    ModelResponse(content="done"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=workspace)),
                require_tool_use=True,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace(
                "This is a SWE-bench-style repository repair task. Fix the existing bug.\n"
                f"Run this exact verification command before finalizing: {exact_command}",
                trace=events.append,
            )

            self.assertEqual(result.content, "done")
            rejected = [
                event
                for event in events
                if event["type"] == "tool_result" and event["id"] == "call_4"
            ]
            self.assertEqual(len(rejected), 1)
            self.assertTrue(rejected[0]["is_error"])
            self.assertIn("Inspect the latest failing verification", rejected[0]["content"])
            self.assertIn("return a + b", (workspace / "calc.py").read_text(encoding="utf-8"))

    def test_repository_repair_rejects_test_file_modification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
            (workspace / "test_calc.py").write_text(
                "from calc import add\n\n"
                "def test_add():\n"
                "    assert add(2, 3) == 5\n",
                encoding="utf-8",
            )
            exact_command = "python -c \"print('ok')\""
            client = FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_1",
                                name="read_file",
                                arguments={"path": "calc.py"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_2",
                                name="write_file",
                                arguments={
                                    "path": "test_calc.py",
                                    "content": "def test_add():\n    assert True\n",
                                },
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_3",
                                name="write_file",
                                arguments={"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_4",
                                name="run_verification",
                                arguments={"command": exact_command, "cwd": str(workspace)},
                            )
                        ]
                    ),
                    ModelResponse(content="done"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=workspace)),
                require_tool_use=True,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace(
                "This is a SWE-bench-style repository repair task. Fix the existing bug without editing tests.\n"
                f"Run this exact verification command before finalizing: {exact_command}",
                trace=events.append,
            )

            self.assertEqual(result.content, "done")
            self.assertIn("assert add(2, 3) == 5", (workspace / "test_calc.py").read_text(encoding="utf-8"))
            rejected = [
                event
                for event in events
                if event["type"] == "tool_result" and event["id"] == "call_2"
            ]
            self.assertEqual(len(rejected), 1)
            self.assertTrue(rejected[0]["is_error"])
            self.assertIn("Do not modify test files for this repository repair task", rejected[0]["content"])

    def test_repository_repair_rejects_replace_all_on_repeated_source_snippet(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "calc.py").write_text(
                "def calculate(left, operator, right):\n"
                "    if operator == '+':\n"
                "        return left - right\n"
                "    if operator == '-':\n"
                "        return left - right\n",
                encoding="utf-8",
            )
            exact_command = "python -c \"print('ok')\""
            client = FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_1",
                                name="read_file",
                                arguments={"path": "calc.py"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_2",
                                name="edit_file",
                                arguments={
                                    "path": "calc.py",
                                    "old": "return left - right",
                                    "new": "return left + right",
                                    "replace_all": True,
                                },
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_3",
                                name="write_file",
                                arguments={
                                    "path": "calc.py",
                                    "content": (
                                        "def calculate(left, operator, right):\n"
                                        "    if operator == '+':\n"
                                        "        return left + right\n"
                                        "    if operator == '-':\n"
                                        "        return left - right\n"
                                    ),
                                },
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_4",
                                name="run_verification",
                                arguments={"command": exact_command, "cwd": str(workspace)},
                            )
                        ]
                    ),
                    ModelResponse(content="done"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=workspace)),
                require_tool_use=True,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace(
                "This is a SWE-bench-style repository repair task. Fix the existing bug.\n"
                f"Run this exact verification command before finalizing: {exact_command}",
                trace=events.append,
            )

            self.assertEqual(result.content, "done")
            self.assertIn("return left + right", (workspace / "calc.py").read_text(encoding="utf-8"))
            rejected = [
                event
                for event in events
                if event["type"] == "tool_result" and event["id"] == "call_2"
            ]
            self.assertEqual(len(rejected), 1)
            self.assertTrue(rejected[0]["is_error"])
            self.assertIn("Avoid replace_all=True", rejected[0]["content"])

    def test_repository_repair_rejects_destructive_python_write_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            source = (
                "class Blueprint:\n"
                "    def __init__(self, name):\n"
                "        self.name = name\n"
                "\n"
                "    def record(self, func):\n"
                "        self.deferred.append(func)\n"
                "\n"
                "    def record_once(self, func):\n"
                "        return self.record(func)\n"
                "\n"
                "    def make_setup_state(self):\n"
                "        return object()\n"
                "\n"
                "    def register_blueprint(self, blueprint):\n"
                "        self.children.append(blueprint)\n"
                "\n"
                "    def register(self, app):\n"
                "        app.blueprints[self.name] = self\n"
                "\n"
                "    def add_url_rule(self, rule, endpoint):\n"
                "        self.record((rule, endpoint))\n"
            )
            (workspace / "blueprints.py").write_text(source, encoding="utf-8")
            destructive = (
                "class Blueprint:\n"
                "    def __init__(self, name):\n"
                "        if '.' in name:\n"
                "            raise ValueError('bad name')\n"
                "        self.name = name\n"
            )
            exact_command = "python -c \"print('ok')\""
            client = FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(id="call_1", name="read_file", arguments={"path": "blueprints.py"})
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_2",
                                name="write_file",
                                arguments={"path": "blueprints.py", "content": destructive},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_3",
                                name="edit_file",
                                arguments={
                                    "path": "blueprints.py",
                                    "old": "        self.name = name",
                                    "new": (
                                        "        if '.' in name:\n"
                                        "            raise ValueError('bad name')\n"
                                        "        self.name = name"
                                    ),
                                },
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_4",
                                name="run_verification",
                                arguments={"command": exact_command, "cwd": str(workspace)},
                            )
                        ]
                    ),
                    ModelResponse(content="done"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=workspace)),
                require_tool_use=True,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace(
                "This is a SWE-bench-style repository repair task. Fix the existing bug.\n"
                f"Run this exact verification command before finalizing: {exact_command}",
                trace=events.append,
            )

            self.assertEqual(result.content, "done")
            edited = (workspace / "blueprints.py").read_text(encoding="utf-8")
            self.assertIn("def record_once", edited)
            self.assertIn("raise ValueError('bad name')", edited)
            rejected = [
                event
                for event in events
                if event["type"] == "tool_result" and event["id"] == "call_2"
            ]
            self.assertEqual(len(rejected), 1)
            self.assertTrue(rejected[0]["is_error"])
            self.assertIn("destructive Python source rewrite", rejected[0]["content"])

    def test_repository_repair_requires_existing_non_test_patch_before_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
            exact_command = "python -c \"print('ok')\""
            client = FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_1",
                                name="read_file",
                                arguments={"path": "calc.py"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_2",
                                name="write_file",
                                arguments={"path": "addition.py", "content": "print(1 + 1)\n"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_3",
                                name="run_verification",
                                arguments={"command": exact_command, "cwd": str(workspace)},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_4",
                                name="read_file",
                                arguments={"path": "calc.py"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_5",
                                name="write_file",
                                arguments={"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_6",
                                name="run_verification",
                                arguments={"command": exact_command, "cwd": str(workspace)},
                            )
                        ]
                    ),
                    ModelResponse(content="done"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=workspace)),
                require_tool_use=True,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace(
                "This is a SWE-bench-style repository repair task. Fix the existing bug.\n"
                f"Run this exact verification command before finalizing: {exact_command}",
                trace=events.append,
            )

            self.assertEqual(result.content, "done")
            rejected = [
                event
                for event in events
                if event["type"] == "tool_result" and event["id"] == "call_3"
            ]
            self.assertEqual(len(rejected), 1)
            self.assertTrue(rejected[0]["is_error"])
            self.assertIn("Modify at least one existing non-test repository file", rejected[0]["content"])

    def test_repository_repair_rejects_unrequested_cli_option_workaround(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            source = (
                "@click.command('routes')\n"
                "def routes_command():\n"
                "    return 'old'\n"
            )
            (workspace / "cli.py").write_text(source, encoding="utf-8")
            exact_command = "python -c \"print('ok')\""
            client = FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_1",
                                name="read_file",
                                arguments={"path": "cli.py"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_2",
                                name="edit_file",
                                arguments={
                                    "path": "cli.py",
                                    "old": "@click.command('routes')\n",
                                    "new": "@click.command('routes')\n@click.option('--show-subdomain', is_flag=True)\n",
                                },
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_3",
                                name="edit_file",
                                arguments={"path": "cli.py", "old": "return 'old'", "new": "return 'new'"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_4",
                                name="run_verification",
                                arguments={"command": exact_command, "cwd": str(workspace)},
                            )
                        ]
                    ),
                    ModelResponse(content="done"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=workspace)),
                require_tool_use=True,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace(
                "This is a SWE-bench-style repository repair task. Fix the existing routes output.\n"
                f"Run this exact verification command before finalizing: {exact_command}",
                trace=events.append,
            )

            self.assertEqual(result.content, "done")
            self.assertNotIn("@click.option", (workspace / "cli.py").read_text(encoding="utf-8"))
            rejected = [
                event
                for event in events
                if event["type"] == "tool_result" and event["id"] == "call_2"
            ]
            self.assertEqual(len(rejected), 1)
            self.assertTrue(rejected[0]["is_error"])
            self.assertIn("Do not hide required fixes behind new optional flags", rejected[0]["content"])

    def test_repository_repair_rejects_unrelated_new_file_extension(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "cli.py").write_text("def routes_command():\n    return 'old'\n", encoding="utf-8")
            exact_command = "python -c \"print('ok')\""
            client = FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=[ToolCall(id="call_1", name="read_file", arguments={"path": "cli.py"})]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_2",
                                name="write_file",
                                arguments={"path": "admin_blueprint.h", "content": "def routes_command():\n    pass\n"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_3",
                                name="edit_file",
                                arguments={"path": "cli.py", "old": "return 'old'", "new": "return 'new'"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_4",
                                name="run_verification",
                                arguments={"command": exact_command, "cwd": str(workspace)},
                            )
                        ]
                    ),
                    ModelResponse(content="done"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=workspace)),
                require_tool_use=True,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace(
                "This is a SWE-bench-style repository repair task. Fix the existing routes output.\n"
                f"Run this exact verification command before finalizing: {exact_command}",
                trace=events.append,
            )

            self.assertEqual(result.content, "done")
            self.assertFalse((workspace / "admin_blueprint.h").exists())
            rejected = [
                event
                for event in events
                if event["type"] == "tool_result" and event["id"] == "call_2"
            ]
            self.assertEqual(len(rejected), 1)
            self.assertTrue(rejected[0]["is_error"])
            self.assertIn("Do not create unrelated new files", rejected[0]["content"])

    def test_repository_repair_lsp_diagnostics_does_not_replace_exact_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "calc.py").write_text("VALUE = 0\n", encoding="utf-8")
            exact_command = (
                "python -c \"import pathlib; "
                "raise SystemExit(0 if pathlib.Path('calc.py').read_text().strip() == 'VALUE = 1' else 1)\""
            )
            client = FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(id="call_1", name="read_file", arguments={"path": "calc.py"})
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_2",
                                name="write_file",
                                arguments={"path": "calc.py", "content": "VALUE = 0\n# touched\n"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_3",
                                name="run_verification",
                                arguments={"command": exact_command, "cwd": str(workspace)},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(id="call_4", name="lsp_diagnostics", arguments={"path": "calc.py"})
                        ]
                    ),
                    ModelResponse(content="done too early"),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_5",
                                name="write_file",
                                arguments={"path": "calc.py", "content": "VALUE = 1\n"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_6",
                                name="run_verification",
                                arguments={"command": exact_command, "cwd": str(workspace)},
                            )
                        ]
                    ),
                    ModelResponse(content="done"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=workspace)),
                require_tool_use=True,
                max_tool_iterations=8,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace(
                "This is a SWE-bench-style repository repair task. Fix the existing bug.\n"
                f"Run this exact verification command before finalizing: {exact_command}",
                trace=events.append,
            )

            self.assertEqual(result.content, "done")
            self.assertEqual((workspace / "calc.py").read_text(encoding="utf-8"), "VALUE = 1\n")
            self.assertTrue(
                any(
                    event["type"] == "tool_use_required"
                    for event in events
                )
            )

    def test_repository_repair_nudges_no_tool_response_before_patch_and_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "src.py").write_text("def f():\n    return 1\n", encoding="utf-8")
            exact_command = "python -c \"print('ok')\""
            client = FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_1",
                                name="read_file",
                                arguments={"path": "src.py"},
                            )
                        ]
                    ),
                    ModelResponse(content="Next I will search for the relevant implementation."),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_2",
                                name="write_file",
                                arguments={"path": "src.py", "content": "def f():\n    return 2\n"},
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call_3",
                                name="run_verification",
                                arguments={"command": exact_command, "cwd": str(workspace)},
                            )
                        ]
                    ),
                    ModelResponse(content="done"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=workspace)),
                require_tool_use=True,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace(
                "This is a SWE-bench-style repository repair task. Fix the existing bug.\n"
                f"Run this exact verification command before finalizing: {exact_command}",
                trace=events.append,
            )

            self.assertEqual(result.content, "done")
            self.assertEqual((workspace / "src.py").read_text(encoding="utf-8"), "def f():\n    return 2\n")
            self.assertTrue(any(event["type"] == "tool_use_required" for event in events))

    def test_task_state_resets_for_new_user_turn_after_done(self) -> None:
        client = FakeModelClient([ModelResponse(content="first done"), ModelResponse(content="second done")])
        agent = CodeAgent(client)

        first = agent.run_turn("First task")
        second = agent.run_turn("Second task")

        self.assertEqual(first.content, "first done")
        self.assertEqual(second.content, "second done")
        first_call_messages = client.calls[0]
        second_call_messages = client.calls[1]
        self.assertTrue(
            any(
                message.role == "user" and "Current task phase: plan" in message.content
                for message in first_call_messages
            )
        )
        self.assertTrue(
            any(
                message.role == "user" and "Current task phase: plan" in message.content
                for message in second_call_messages
            )
        )


if __name__ == "__main__":
    unittest.main()
