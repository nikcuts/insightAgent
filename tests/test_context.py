from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from insightagent.agent.core import CodeAgent
from insightagent.agent.context import ContextManager, build_system_prompt, load_project_memory
from insightagent.api.messages import Message, ModelResponse, ToolCall
from insightagent.api.providers import ModelClient
from insightagent.runtime.tool_context import ToolContext
from insightagent.tools import ToolRegistry


class FakeModelClient(ModelClient):
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.calls: list[list[Message]] = []

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        self.calls.append(list(messages))
        return self.responses.pop(0)


class ContextTests(unittest.TestCase):
    def test_loads_project_memory_and_builds_system_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".codeagent.md").write_text("Always run tests after edits.", encoding="utf-8")
            (root / "MEMORY.md").write_text("Prefer small functions.", encoding="utf-8")

            memory = load_project_memory(root)
            prompt = build_system_prompt("Base prompt.", memory)

            self.assertEqual([section[0] for section in memory.sections], [".codeagent.md", "MEMORY.md"])
            self.assertIn("Always run tests after edits.", prompt)
            self.assertIn("Prefer small functions.", prompt)

    def test_truncates_large_tool_output_before_next_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.txt"
            path.write_text("A" * 200 + "MIDDLE" + "Z" * 200, encoding="utf-8")
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
                    ModelResponse(content="done"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=Path(directory))),
                context_manager=ContextManager(max_tool_output_chars=120),
            )

            agent.run_turn("Read large file")

            tool_messages = [message for message in client.calls[1] if message.role == "tool"]
            self.assertEqual(len(tool_messages), 1)
            self.assertIn("InsightAgent V5 truncated tool output", tool_messages[0].content)
            self.assertNotIn("MIDDLE", tool_messages[0].content)

    def test_compacts_large_tool_output_after_final_answer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.txt"
            path.write_text("x" * 500, encoding="utf-8")
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
                    ModelResponse(content="done"),
                ]
            )
            agent = CodeAgent(
                client,
                tools=ToolRegistry(context=ToolContext(workspace=Path(directory))),
                context_manager=ContextManager(max_tool_output_chars=1000, compact_tool_output_chars=120),
            )

            result = agent.run_turn("Read large file")

            tool_messages = [message for message in result.messages if message.role == "tool"]
            self.assertEqual(len(tool_messages), 1)
            self.assertIn("InsightAgent V5 compacted completed tool output", tool_messages[0].content)
            self.assertLess(len(tool_messages[0].content), 220)


if __name__ == "__main__":
    unittest.main()
