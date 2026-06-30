"""Model provider adapters for InsightAgent V1.0."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from socket import timeout as SocketTimeout
from typing import Any, Callable, Protocol

from .messages import Message, ModelResponse, TokenUsage, ToolCall
from .resilience import loads_lenient


class ModelClient(Protocol):
    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        tool_choice: Any | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> ModelResponse:
        ...


class ProviderError(RuntimeError):
    """Raised when a model provider returns an error."""


class ToolArgumentsParseError(ProviderError):
    """Raised when a provider returns malformed tool-call arguments."""

    def __init__(self, tool_name: str, raw_arguments: str, detail: str) -> None:
        self.tool_name = tool_name
        self.raw_arguments = raw_arguments
        self.detail = detail
        preview = raw_arguments[:500]
        super().__init__(
            f"invalid JSON arguments for tool {tool_name}: {detail}. "
            f"Argument preview: {preview!r}"
        )


class OpenAICompatibleClient:
    """Minimal OpenAI Chat Completions compatible client."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: int = 120,
        temperature: float = 0.01,
        top_p: float = 0.95,
        max_tokens: int = 4096,
        stream: bool = False,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.timeout = timeout
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.stream = stream
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required")

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        tool_choice: Any | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> ModelResponse:
        streaming = self.stream or on_delta is not None
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [self._message_to_openai(message) for message in messages],
            "stream": streaming,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = [self._tool_to_openai(tool) for tool in tools]
            payload["tool_choice"] = tool_choice or "auto"
        if streaming:
            payload["stream_options"] = {"include_usage": True}
            return self._complete_streaming(f"{self.base_url}/chat/completions", payload, on_delta)
        data = self._post(f"{self.base_url}/chat/completions", payload)
        choice = data["choices"][0]["message"]
        tool_calls = []
        for raw_call in choice.get("tool_calls") or []:
            function = raw_call.get("function") or {}
            raw_arguments = function.get("arguments") or "{}"
            tool_name = function["name"]
            parsed_arguments, _repaired = loads_lenient(raw_arguments)
            if not isinstance(parsed_arguments, dict):
                raise ToolArgumentsParseError(
                    tool_name, raw_arguments, "arguments are not valid JSON even after lenient repair"
                )
            tool_calls.append(
                ToolCall(
                    id=raw_call["id"],
                    name=tool_name,
                    arguments=parsed_arguments,
                )
            )
        return ModelResponse(
            content=choice.get("content") or "",
            tool_calls=tool_calls,
            usage=_openai_usage(data.get("usage")),
        )

    def _complete_streaming(
        self, url: str, payload: dict[str, Any], on_delta: Callable[[str], None] | None
    ) -> ModelResponse:
        accumulator = StreamAccumulator()
        for chunk in self._stream_post(url, payload):
            accumulator.add(chunk, on_delta=on_delta)
        return accumulator.build_response()

    def _stream_post(self, url: str, payload: dict[str, Any]):
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        yield json.loads(data)
                    except json.JSONDecodeError:
                        continue
        except (TimeoutError, SocketTimeout) as error:
            raise ProviderError(
                f"OpenAI-compatible provider timed out after {self.timeout}s during streaming."
            ) from error
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise ProviderError(f"OpenAI-compatible provider error {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise ProviderError(f"OpenAI-compatible provider connection error: {error.reason}") from error

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (TimeoutError, SocketTimeout) as error:
            raise ProviderError(
                f"OpenAI-compatible provider timed out after {self.timeout}s. "
                "Try a stronger function-calling model or increase --timeout."
            ) from error
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise ProviderError(f"OpenAI-compatible provider error {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise ProviderError(f"OpenAI-compatible provider connection error: {error.reason}") from error

    def _message_to_openai(self, message: Message) -> dict[str, Any]:
        if message.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "content": message.content,
            }
        payload: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.name,
                        "arguments": json.dumps(tool_call.arguments),
                    },
                }
                for tool_call in message.tool_calls
            ]
        return payload

    def _tool_to_openai(self, tool: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }


class AnthropicClient:
    """Minimal Anthropic Messages API client."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: int = 120,
        max_tokens: int = 2048,
    ) -> None:
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
        self.base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")).rstrip("/")
        self.timeout = timeout
        self.max_tokens = max_tokens
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY is required")

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        tool_choice: Any | None = None,
    ) -> ModelResponse:
        system_text = "\n\n".join(message.content for message in messages if message.role == "system")
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [self._message_to_anthropic(message) for message in messages if message.role != "system"],
            "tools": tools,
        }
        if system_text:
            payload["system"] = system_text
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

        data = self._post(f"{self.base_url}/v1/messages", payload)
        text_parts = []
        tool_calls = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block["id"],
                        name=block["name"],
                        arguments=block.get("input") or {},
                    )
                )
        return ModelResponse(
            content="\n".join(part for part in text_parts if part),
            tool_calls=tool_calls,
            usage=_anthropic_usage(data.get("usage")),
        )

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise ProviderError(f"Anthropic provider error {error.code}: {detail}") from error

    def _message_to_anthropic(self, message: Message) -> dict[str, Any]:
        if message.role == "tool":
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id,
                        "content": message.content,
                        "is_error": message.is_error,
                    }
                ],
            }
        if message.role == "assistant" and message.tool_calls:
            content: list[dict[str, Any]] = []
            if message.content:
                content.append({"type": "text", "text": message.content})
            content.extend(
                {
                    "type": "tool_use",
                    "id": tool_call.id,
                    "name": tool_call.name,
                    "input": tool_call.arguments,
                }
                for tool_call in message.tool_calls
            )
            return {"role": "assistant", "content": content}
        return {"role": message.role, "content": message.content}


class StreamAccumulator:
    """Reduce OpenAI streaming chunks into a single ModelResponse.

    Kept separate from the HTTP path so the (fiddly) delta-merging logic can be
    unit-tested deterministically without a network connection.
    """

    def __init__(self) -> None:
        self._content_parts: list[str] = []
        self._tool_state: dict[int, dict[str, Any]] = {}
        self._usage_raw: Any = None

    def add(self, chunk: dict[str, Any], on_delta: Callable[[str], None] | None = None) -> None:
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            piece = delta.get("content")
            if piece:
                self._content_parts.append(piece)
                if on_delta is not None:
                    on_delta(piece)
            for raw_call in delta.get("tool_calls") or []:
                index = raw_call.get("index", 0)
                slot = self._tool_state.setdefault(index, {"id": None, "name": None, "arguments": ""})
                if raw_call.get("id"):
                    slot["id"] = raw_call["id"]
                function = raw_call.get("function") or {}
                if function.get("name"):
                    slot["name"] = function["name"]
                if function.get("arguments"):
                    slot["arguments"] += function["arguments"]
        if chunk.get("usage"):
            self._usage_raw = chunk["usage"]

    def build_response(self) -> ModelResponse:
        tool_calls: list[ToolCall] = []
        for index in sorted(self._tool_state):
            slot = self._tool_state[index]
            name = slot["name"]
            if not name:
                continue
            raw_arguments = slot["arguments"] or "{}"
            parsed_arguments, _repaired = loads_lenient(raw_arguments)
            if not isinstance(parsed_arguments, dict):
                raise ToolArgumentsParseError(
                    name, raw_arguments, "streamed tool arguments are not valid JSON"
                )
            tool_calls.append(
                ToolCall(id=slot["id"] or f"stream_{index}", name=name, arguments=parsed_arguments)
            )
        return ModelResponse(
            content="".join(self._content_parts),
            tool_calls=tool_calls,
            usage=_openai_usage(self._usage_raw),
        )


def _openai_usage(raw: Any) -> TokenUsage | None:
    if not isinstance(raw, dict):
        return None
    prompt = raw.get("prompt_tokens")
    completion = raw.get("completion_tokens")
    if prompt is None and completion is None:
        return None
    return TokenUsage(input_tokens=int(prompt or 0), output_tokens=int(completion or 0))


def _anthropic_usage(raw: Any) -> TokenUsage | None:
    if not isinstance(raw, dict):
        return None
    input_tokens = raw.get("input_tokens")
    output_tokens = raw.get("output_tokens")
    if input_tokens is None and output_tokens is None:
        return None
    return TokenUsage(input_tokens=int(input_tokens or 0), output_tokens=int(output_tokens or 0))
