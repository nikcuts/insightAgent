from __future__ import annotations

import io
import json
import unittest
from unittest import mock
from urllib.error import HTTPError

from insightagent.api.messages import Message, ToolCall
from insightagent.api.providers import AnthropicClient, OpenAICompatibleClient, ProviderError


class OpenAIRetryTests(unittest.TestCase):
    def _ok_response(self, content: str) -> mock.MagicMock:
        handle = mock.MagicMock()
        handle.__enter__.return_value.read.return_value = json.dumps(
            {"choices": [{"message": {"content": content}}]}
        ).encode("utf-8")
        return handle

    def test_post_retries_transient_5xx_then_succeeds(self) -> None:
        client = OpenAICompatibleClient(api_key="k", model="m", base_url="http://x", max_retries=2)
        err = HTTPError("http://x", 503, "busy", {}, io.BytesIO(b"overloaded"))
        with mock.patch("insightagent.api.providers.urllib.request.urlopen", side_effect=[err, self._ok_response("hi")]) as urlopen, \
             mock.patch("insightagent.api.providers.time.sleep"):
            response = client.complete([Message(role="user", content="x")], [])

        self.assertEqual(response.content, "hi")
        self.assertEqual(urlopen.call_count, 2)

    def test_post_does_not_retry_client_error(self) -> None:
        client = OpenAICompatibleClient(api_key="k", model="m", base_url="http://x", max_retries=2)
        err = HTTPError("http://x", 400, "bad", {}, io.BytesIO(b"bad request"))
        with mock.patch("insightagent.api.providers.urllib.request.urlopen", side_effect=err) as urlopen, \
             mock.patch("insightagent.api.providers.time.sleep"):
            with self.assertRaises(ProviderError):
                client.complete([Message(role="user", content="x")], [])

        self.assertEqual(urlopen.call_count, 1)


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
