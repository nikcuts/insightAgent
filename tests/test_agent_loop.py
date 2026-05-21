from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from insightagent.agent import CodeAgent
from insightagent.messages import Message, ModelResponse, ToolCall
from insightagent.providers import ModelClient


class FakeModelClient(ModelClient):
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.calls: list[list[Message]] = []

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        self.calls.append(list(messages))
        return self.responses.pop(0)


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
            agent = CodeAgent(client)

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
        client = FakeModelClient(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="call_1",
                            name="read_file",
                            arguments={"path": "/path/does/not/exist"},
                        )
                    ]
                ),
                ModelResponse(content="The read failed."),
            ]
        )
        agent = CodeAgent(client)

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
            agent = CodeAgent(client)
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace("Trace this", trace=events.append)

            self.assertEqual(result.content, "Done.")
            event_types = [event["type"] for event in events]
            self.assertIn("model_response", event_types)
            self.assertIn("tool_call", event_types)
            self.assertIn("tool_result", event_types)
            self.assertEqual(event_types[-1], "final_answer")

    def test_truncates_large_tool_result_before_next_model_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.txt"
            path.write_text("A" * 80 + "Z" * 80, encoding="utf-8")
            client = FakeModelClient(
                [
                    ModelResponse(tool_calls=[ToolCall(id="call_1", name="read_file", arguments={"path": str(path)})]),
                    ModelResponse(content="Done."),
                ]
            )
            agent = CodeAgent(client, max_tool_result_chars=60, compact_completed_turns=False)

            result = agent.run_turn("Read large file")

            self.assertEqual(result.content, "Done.")
            tool_messages = [message for message in client.calls[1] if message.role == "tool"]
            self.assertEqual(len(tool_messages), 1)
            self.assertIn("...[truncated", tool_messages[0].content)
            self.assertLess(len(tool_messages[0].content), 160)

    def test_emits_truncation_and_compaction_trace_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.txt"
            path.write_text("A" * 80 + "Z" * 80, encoding="utf-8")
            client = FakeModelClient(
                [
                    ModelResponse(tool_calls=[ToolCall(id="call_1", name="read_file", arguments={"path": str(path)})]),
                    ModelResponse(content="Done."),
                ]
            )
            agent = CodeAgent(
                client,
                max_tool_result_chars=60,
                compact_completed_turns=True,
                compact_tool_result_chars=40,
            )
            events: list[dict[str, Any]] = []

            agent.run_turn_with_trace("Read large file", trace=events.append)

            event_types = [event["type"] for event in events]
            self.assertIn("tool_result_truncated", event_types)
            self.assertIn("history_compacted", event_types)

    def test_trace_events_include_v2_context_events(self) -> None:
        client = FakeModelClient([ModelResponse(content="Done.")])
        agent = CodeAgent(client)
        events: list[dict[str, Any]] = []

        agent._emit(events.append, {"type": "memory_loaded", "sources": ["MEMORY.md", ".codeagent.md"]})
        agent._emit(
            events.append,
            {
                "type": "tool_result_truncated",
                "id": "call_1",
                "name": "read_file",
                "original_chars": 100,
                "stored_chars": 50,
                "omitted_chars": 50,
            },
        )
        agent._emit(events.append, {"type": "history_compacted", "compacted_count": 1})

        self.assertEqual([event["type"] for event in events], ["memory_loaded", "tool_result_truncated", "history_compacted"])


if __name__ == "__main__":
    unittest.main()
