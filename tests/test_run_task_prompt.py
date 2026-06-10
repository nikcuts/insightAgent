from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from insightagent.config import RuntimeConfig
from insightagent.context import ProjectMemory
from insightagent.messages import Message, ModelResponse
from insightagent.providers import ModelClient
from insightagent.run_task import build_agent
from insightagent.session import SessionStore
from insightagent.tools import ToolRegistry


class FakeModelClient(ModelClient):
    def complete(self, messages: list[Message], tools: list[dict]) -> ModelResponse:
        return ModelResponse(content="done")


class RunTaskPromptTests(unittest.TestCase):
    def test_system_prompt_forbids_textual_tool_call_simulation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore(workspace / "sessions")
            session = store.create()

            agent = build_agent(
                RuntimeConfig(provider="fake"),
                workspace,
                ProjectMemory(()),
                store,
                session,
                tools=ToolRegistry([]),
                client=FakeModelClient(),
            )

        system_prompt = agent.messages[0].content
        self.assertIn("tool_calls", system_prompt)
        self.assertIn("不要用普通文本、Markdown 或 JSON 片段模拟工具调用", system_prompt)
        self.assertIn("只有名称以 mcp_ 开头的工具才算 MCP 工具", system_prompt)
        self.assertIn("不要重复调用已经获得足够证据的工具", system_prompt)


if __name__ == "__main__":
    unittest.main()
