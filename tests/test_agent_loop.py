from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from insightagent.agent.core import CodeAgent
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

    def test_converges_after_repeated_verification_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=[ToolCall(id="w1", name="write_file", arguments={"path": "main.py", "content": "print(1)\n"})]
                    ),
                    ModelResponse(tool_calls=[ToolCall(id="v1", name="run_verification", arguments={})]),
                    # Needless churn: rewrite the same file after it already passed.
                    ModelResponse(
                        tool_calls=[ToolCall(id="w2", name="write_file", arguments={"path": "main.py", "content": "print(1)\n"})]
                    ),
                    ModelResponse(tool_calls=[ToolCall(id="v2", name="run_verification", arguments={})]),
                    # If convergence works, this tool-free response finalizes the turn.
                    ModelResponse(content="done"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=Path(directory))),
                require_tool_use=True,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace("Create and verify", trace=events.append)

            self.assertEqual(result.content, "done")
            self.assertTrue(any(event["type"] == "verification_converged" for event in events))

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
