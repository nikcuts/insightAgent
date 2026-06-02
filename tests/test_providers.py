from __future__ import annotations

import json
import unittest

from insightagent.messages import Message, ToolCall
from insightagent.providers import AnthropicClient, OpenAICompatibleClient


class ProviderConversionTests(unittest.TestCase):
    def test_openai_tool_call_message_shape(self) -> None:
        client = OpenAICompatibleClient(api_key="test-key")
        message = Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="call_1", name="read_file", arguments={"path": "README.md"})],
        )

        payload = client._message_to_openai(message)

        self.assertEqual(payload["role"], "assistant")
        self.assertEqual(payload["tool_calls"][0]["function"]["name"], "read_file")
        self.assertEqual(json.loads(payload["tool_calls"][0]["function"]["arguments"]), {"path": "README.md"})

    def test_anthropic_tool_result_message_shape(self) -> None:
        client = AnthropicClient(api_key="test-key")
        message = Message(role="tool", content="file data", tool_call_id="toolu_1", is_error=False)

        payload = client._message_to_anthropic(message)

        self.assertEqual(payload["role"], "user")
        self.assertEqual(payload["content"][0]["type"], "tool_result")
        self.assertEqual(payload["content"][0]["tool_use_id"], "toolu_1")


if __name__ == "__main__":
    unittest.main()
