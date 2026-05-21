"""Model provider adapters for InsightAgent V1.0."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from socket import timeout as SocketTimeout
from typing import Any, Protocol

from .messages import Message, ModelResponse, ToolCall


class ModelClient(Protocol):
    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        ...


class ProviderError(RuntimeError):
    """Raised when a model provider returns an error."""


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
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.timeout = timeout
        self.temperature = temperature
        self.top_p = top_p
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required")

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        payload = {
            "model": self.model,
            "messages": [self._message_to_openai(message) for message in messages],
            "stream": False,
            "temperature": self.temperature,
            "top_p": self.top_p,
        }
        if tools:
            payload["tools"] = [self._tool_to_openai(tool) for tool in tools]
            payload["tool_choice"] = "auto"
        data = self._post(f"{self.base_url}/chat/completions", payload)
        choice = data["choices"][0]["message"]
        tool_calls = []
        for raw_call in choice.get("tool_calls") or []:
            function = raw_call.get("function") or {}
            raw_arguments = function.get("arguments") or "{}"
            tool_calls.append(
                ToolCall(
                    id=raw_call["id"],
                    name=function["name"],
                    arguments=json.loads(raw_arguments),
                )
            )
        return ModelResponse(content=choice.get("content") or "", tool_calls=tool_calls)

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

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        system_text = "\n\n".join(message.content for message in messages if message.role == "system")
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [self._message_to_anthropic(message) for message in messages if message.role != "system"],
            "tools": tools,
        }
        if system_text:
            payload["system"] = system_text

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
        return ModelResponse(content="\n".join(part for part in text_parts if part), tool_calls=tool_calls)

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
