from __future__ import annotations

from io import BytesIO
import json
import unittest
import urllib.error

from insightagent.api.messages import Message, ToolCall
from insightagent.api.providers import AnthropicClient, OpenAICompatibleClient, ProviderError


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

    def test_openai_compatible_force_tool_choice_uses_required(self) -> None:
        class RecordingClient(OpenAICompatibleClient):
            def __init__(self) -> None:
                super().__init__(api_key="test-key")
                self.payload: dict | None = None

            def _post(self, url: str, payload: dict) -> dict:  # noqa: ANN001
                self.payload = payload
                return {"choices": [{"message": {"content": "ok"}}]}

        client = RecordingClient()

        client.complete(
            [Message(role="user", content="read")],
            [
                {
                    "name": "read_file",
                    "description": "read a file",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
            tool_choice={"force_tool": "read_file"},
        )

        self.assertIsNotNone(client.payload)
        self.assertEqual(client.payload["tool_choice"], "required")

    def test_openai_compatible_retries_rate_limit_once(self) -> None:
        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
                return None

            def read(self) -> bytes:
                return b'{"choices": [{"message": {"content": "ok"}}]}'

        calls = 0
        slept: list[float] = []

        def fake_urlopen(request, timeout):  # noqa: ANN001
            nonlocal calls
            calls += 1
            if calls == 1:
                raise urllib.error.HTTPError(
                    request.full_url,
                    429,
                    "Too Many Requests",
                    hdrs={},
                    fp=BytesIO(b'{"message":"rate limit"}'),
                )
            return FakeResponse()

        client = OpenAICompatibleClient(
            api_key="test-key",
            max_retries=1,
            retry_base_delay=0.0,
            sleep=slept.append,
            urlopen=fake_urlopen,
        )

        response = client.complete([Message(role="user", content="hello")], [])

        self.assertEqual(response.content, "ok")
        self.assertEqual(calls, 2)
        self.assertEqual(slept, [0.0])

    def test_openai_compatible_empty_choices_raises_provider_error(self) -> None:
        class EmptyChoicesClient(OpenAICompatibleClient):
            def _post(self, url: str, payload: dict) -> dict:  # noqa: ANN001
                return {"choices": []}

        client = EmptyChoicesClient(api_key="test-key", max_retries=0)

        with self.assertRaisesRegex(ProviderError, "no choices"):
            client.complete([Message(role="user", content="hello")], [])


if __name__ == "__main__":
    unittest.main()
