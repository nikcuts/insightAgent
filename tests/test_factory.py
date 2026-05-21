from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from insightagent.config import AgentConfig
from insightagent.factory import build_agent_with_memory
from insightagent.messages import Message, ModelResponse
from insightagent.providers import ModelClient


class FakeModelClient(ModelClient):
    def complete(self, messages: list[Message], tools: list[dict[str, object]]) -> ModelResponse:
        return ModelResponse(content="ok")


class FactoryTests(unittest.TestCase):
    def test_build_agent_with_memory_injects_workspace_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "MEMORY.md").write_text("Always run tests.", encoding="utf-8")
            events: list[dict[str, object]] = []

            agent = build_agent_with_memory(
                model_client=FakeModelClient(),
                workspace=workspace,
                config=AgentConfig(),
                trace=events.append,
            )

        self.assertIn("## Project Memory: MEMORY.md", agent.messages[0].content)
        self.assertIn("Always run tests.", agent.messages[0].content)
        self.assertEqual(events[0]["type"], "memory_loaded")
        self.assertEqual(events[0]["sources"], ["MEMORY.md"])


if __name__ == "__main__":
    unittest.main()
