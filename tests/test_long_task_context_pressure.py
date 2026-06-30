"""Long-task context pressure tests: truncation, compaction, and small memory windows."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from insightagent.agent.core import CodeAgent
from insightagent.agent.context import ContextManager
from insightagent.agent.memory import SlidingWindowMemory
from insightagent.api.messages import Message, ModelResponse, ToolCall
from insightagent.api.providers import ModelClient
from insightagent.runtime.tool_context import ToolContext
from insightagent.tools import ToolRegistry


class ScriptedModelClient(ModelClient):
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[list[Message]] = []

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        self.calls.append(list(messages))
        return self.responses.pop(0)


def tool_response(call_id: str, name: str, arguments: dict[str, Any]) -> ModelResponse:
    return ModelResponse(tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)])


class OversizedToolOutputTests(unittest.TestCase):
    def test_oversized_tool_output_is_truncated_then_compacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = ScriptedModelClient(
                [
                    tool_response(
                        "call_big",
                        "execute_command",
                        {"command": "python3 -c \"print('A' * 3000)\"", "cwd": directory},
                    ),
                    ModelResponse(content="Inspected the large output."),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=Path(directory))),
                context_manager=ContextManager(max_tool_output_chars=400, compact_tool_output_chars=150),
                max_tool_iterations=10,
            )
            events: list[dict[str, Any]] = []

            result = agent.run_turn_with_trace("Generate a large output", trace=events.append)

            self.assertEqual(result.content, "Inspected the large output.")

            # Mid-task: the oversized output was truncated before reaching the model.
            truncation_events = [event for event in events if event["type"] == "tool_output_truncated"]
            self.assertEqual(len(truncation_events), 1)
            self.assertGreater(truncation_events[0]["original_chars"], 3000)
            model_seen_tool = [message for message in client.calls[1] if message.role == "tool"]
            self.assertEqual(len(model_seen_tool), 1)
            self.assertIn("truncated tool output", model_seen_tool[0].content)
            self.assertLess(len(model_seen_tool[0].content), 700)
            # Exit-code detection ran on the untruncated output, so this still counts as success.
            self.assertFalse(model_seen_tool[0].is_error)

            # After completion: tool output is compacted down to a small preview.
            self.assertTrue(any(event["type"] == "context_compaction" for event in events))
            final_tool_messages = [message for message in result.messages if message.role == "tool"]
            self.assertEqual(len(final_tool_messages), 1)
            self.assertIn("compacted completed tool output", final_tool_messages[0].content)
            self.assertLess(len(final_tool_messages[0].content), len(model_seen_tool[0].content))


class SmallMemoryWindowTests(unittest.TestCase):
    def test_long_task_completes_with_small_memory_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            step_count = 6
            responses = [
                tool_response(f"call_{index}", "write_file", {"path": f"chunk_{index}.txt", "content": f"chunk {index}\n"})
                for index in range(step_count)
            ] + [ModelResponse(content="All chunks written despite the tiny window.")]
            client = ScriptedModelClient(responses)
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=Path(directory))),
                memory=SlidingWindowMemory(max_messages=6),
                max_tool_iterations=20,
            )

            result = agent.run_turn("Write all chunks")

            self.assertEqual(result.content, "All chunks written despite the tiny window.")
            for index in range(step_count):
                self.assertTrue((Path(directory) / f"chunk_{index}.txt").is_file())

            # The window held: system prompt survives, history stays bounded.
            self.assertEqual(result.messages[0].role, "system")
            non_system = [message for message in result.messages if message.role != "system"]
            self.assertLessEqual(len(non_system), 6)

            # Every model request stayed within the window budget too.
            for request_messages in client.calls:
                request_non_system = [message for message in request_messages if message.role != "system"]
                # +1 for the per-request phase-instruction message appended after trimming.
                self.assertLessEqual(len(request_non_system), 6 + 1)


class ManualCompactionTests(unittest.TestCase):
    def test_compact_history_summarizes_long_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            responses = [
                tool_response(f"call_{index}", "write_file", {"path": f"file_{index}.txt", "content": f"content {index}\n"})
                for index in range(4)
            ] + [ModelResponse(content="done")]
            client = ScriptedModelClient(responses)
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=Path(directory))),
                max_tool_iterations=20,
            )
            agent.run_turn("Write four files")
            message_count_before = len(agent.messages)

            stats = agent.compact_history(preserve_recent_messages=2)

            self.assertGreater(stats["removed_message_count"], 0)
            self.assertLess(len(agent.messages), message_count_before)
            self.assertEqual(agent.messages[0].role, "system")
            summary = agent.messages[1]
            self.assertEqual(summary.role, "system")
            self.assertIn("compacted earlier conversation", summary.content)
            self.assertIn("write_file", summary.content)
            # The most recent exchanges survive verbatim.
            self.assertEqual([message.content for message in agent.messages[-1:]], ["done"])


if __name__ == "__main__":
    unittest.main()
