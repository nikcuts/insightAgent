"""Model provider adapters for InsightAgent V1.0."""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.request
from socket import timeout as SocketTimeout
from typing import Any, Callable, Protocol

from .messages import Message, ModelResponse, ToolCall
from .resilience import loads_lenient


class ModelClient(Protocol):
    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        tool_choice: Any | None = None,
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


def _is_retryable_http_error(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code < 600


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
        max_retries: int = 3,
        retry_base_delay: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.timeout = timeout
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.sleep = sleep
        self.urlopen = urlopen
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required")

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        tool_choice: Any | None = None,
    ) -> ModelResponse:
        payload = {
            "model": self.model,
            "messages": [self._message_to_openai(message) for message in messages],
            "stream": False,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = [self._tool_to_openai(tool) for tool in tools]
            payload["tool_choice"] = self._tool_choice_to_openai(tool_choice) if tool_choice is not None else "auto"
        choices: list[dict[str, Any]] = []
        data: dict[str, Any] = {}
        for attempt in range(self.max_retries + 1):
            data = self._post(f"{self.base_url}/chat/completions", payload)
            choices = data.get("choices") or []
            if choices:
                break
            if attempt < self.max_retries:
                self.sleep(self.retry_base_delay * (2**attempt))
        if not choices:
            raise ProviderError(f"OpenAI-compatible provider returned no choices: {str(data)[:500]}")
        choice = choices[0]["message"]
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
        for attempt in range(self.max_retries + 1):
            try:
                with self.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (TimeoutError, SocketTimeout) as error:
                raise ProviderError(
                    f"OpenAI-compatible provider timed out after {self.timeout}s. "
                    "Try a stronger function-calling model or increase --timeout."
                ) from error
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")
                if _is_retryable_http_error(error.code) and attempt < self.max_retries:
                    self.sleep(self.retry_base_delay * (2**attempt))
                    continue
                raise ProviderError(f"OpenAI-compatible provider error {error.code}: {detail}") from error
            except urllib.error.URLError as error:
                raise ProviderError(f"OpenAI-compatible provider connection error: {error.reason}") from error
            except http.client.RemoteDisconnected as error:
                if attempt < self.max_retries:
                    self.sleep(self.retry_base_delay * (2**attempt))
                    continue
                raise ProviderError(f"OpenAI-compatible provider connection error: {error}") from error
        raise ProviderError("OpenAI-compatible provider request failed after retries")

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

    def _tool_choice_to_openai(self, tool_choice: Any) -> Any:
        if isinstance(tool_choice, dict) and tool_choice.get("force_tool"):
            return "required"
        return tool_choice


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
