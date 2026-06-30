from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from insightagent.agent.core import CodeAgent
from insightagent.api.messages import Message, ModelResponse
from insightagent.api.providers import ModelClient
from insightagent.agent.session import SessionStore
from insightagent.cli.slash_commands import SlashCommandProcessor
from insightagent.runtime.tool_context import ToolContext
from insightagent.tools import ToolRegistry


class FakeModelClient(ModelClient):
    def complete(self, messages: list[Message], tools: list[dict]) -> ModelResponse:
        return ModelResponse(content="done")


class SlashCommandTests(unittest.TestCase):
    def test_status_compact_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore(workspace / "sessions")
            session = store.create()
            agent = CodeAgent(
                FakeModelClient(),
                tools=ToolRegistry(context=ToolContext(workspace=workspace)),
                session_store=store,
                session=session,
            )
            for index in range(8):
                agent.messages.append(Message(role="user", content=f"message-{index}"))
            slash = SlashCommandProcessor(agent, session_store=store)

            self.assertIn("session=", slash.handle("/status"))
            self.assertIn("compacted_messages=", slash.handle("/compact"))
            self.assertIn("permission_mode=", slash.handle("/permissions"))
            self.assertIn("exported=", slash.handle(f"/export {workspace / 'out.md'}"))
            self.assertIn("/mcp", slash.handle("/help"))
            self.assertTrue((workspace / "out.md").is_file())


if __name__ == "__main__":
    unittest.main()
