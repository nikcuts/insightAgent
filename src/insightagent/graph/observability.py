"""Langfuse v3 callbacks, spans and safe observability payloads."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from copy import deepcopy
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, TextIO

from langfuse import get_client
from langfuse.langchain import CallbackHandler
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage


_SENSITIVE_KEY = re.compile(
    r"(?i)(?:^|[_-])(?:api[_-]?key|authorization|password|secret|token|credential|cookie)(?:$|[_-])"
)
_TOKEN_USAGE_METADATA_KEY = re.compile(
    r"(?i)^(?:token[_-]usage|(?:input|output)[_-]token[_-]details)$"
)
_INLINE_SECRET = re.compile(
    r"(?i)(?<![A-Za-z0-9])(api[_-]?key|authorization|password|(?:client[_-]?)?secret|(?:access[_-]?)?token|credential|cookie)([\"']?)\s*([:=])\s*([\"']?)([^\s,;\"'\]}]+)"
)
_COMMAND_LINE_SECRET = re.compile(
    r"(?i)(--(?:api[_-]?key|authorization|password|(?:client[_-]?)?secret|(?:access[_-]?)?token|credential|cookie))\s+([^\s,;]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_AUTHORIZATION_HEADER = re.compile(
    r"(?i)\bauthorization\s*[:=]\s*[^,;\r\n]+"
)
_COOKIE_HEADER = re.compile(r"(?i)\bcookie\s*[:=][^\r\n]*")
_COMMAND_LINE_COOKIE = re.compile(
    r"""(?ix)
    (?<![A-Za-z0-9])
    (?P<flag>--cookie|-b)
    (?P<separator>\s*=\s*|\s*)
    (?:
        (?P<quoted_double>\"[^\"]*\")
        |(?P<quoted_single>'[^']*')
        |(?P<raw>[^\s;]+(?:\s*;\s*[^\s;]+)*)
    )
    """
)
_HIGH_ENTROPY_CREDENTIAL = re.compile(r"(?<![A-Za-z0-9_-])(?:sk|rk|lf|sf)-[A-Za-z0-9_-]{16,}")
_CALLBACK_CONTROL_FIELDS = {"run_id", "parent_run_id"}
GraphEventSink = Callable[[Mapping[str, object]], None]


class SanitizingLangfuseCallback(BaseCallbackHandler):
    """Forward LangChain callbacks only after their model-visible data is sanitized."""

    def __init__(self, delegate: object, *, max_chars: int = 8_000) -> None:
        self.delegate = delegate
        self.max_chars = max_chars

    def on_llm_start(self, serialized: dict[str, object], prompts: list[str], **kwargs: object) -> None:
        self._forward(
            "on_llm_start",
            _sanitize_callback_value(serialized, max_chars=self.max_chars),
            _sanitize_callback_value(prompts, max_chars=self.max_chars),
            **kwargs,
        )

    def on_chat_model_start(
        self,
        serialized: dict[str, object],
        messages: list[list[BaseMessage]],
        **kwargs: object,
    ) -> None:
        self._forward(
            "on_chat_model_start",
            _sanitize_callback_value(serialized, max_chars=self.max_chars),
            _sanitize_callback_value(messages, max_chars=self.max_chars),
            **kwargs,
        )

    def on_llm_end(self, response: object, **kwargs: object) -> None:
        self._forward("on_llm_end", _sanitize_llm_result(response, max_chars=self.max_chars), **kwargs)

    def on_llm_error(self, error: BaseException, **kwargs: object) -> None:
        self._forward(
            "on_llm_error",
            RuntimeError(str(sanitize_for_model_trace_and_persistence(error, max_chars=self.max_chars))),
            **kwargs,
        )

    def on_tool_start(self, serialized: dict[str, object], input_str: str, **kwargs: object) -> None:
        self._forward(
            "on_tool_start",
            _sanitize_callback_value(serialized, max_chars=self.max_chars),
            str(sanitize_for_model_trace_and_persistence(input_str, max_chars=self.max_chars)),
            **kwargs,
        )

    def on_tool_end(self, output: object, **kwargs: object) -> None:
        self._forward(
            "on_tool_end", _sanitize_callback_value(output, max_chars=self.max_chars), **kwargs
        )

    def on_tool_error(self, error: BaseException, **kwargs: object) -> None:
        self._forward(
            "on_tool_error",
            RuntimeError(str(sanitize_for_model_trace_and_persistence(error, max_chars=self.max_chars))),
            **kwargs,
        )

    def on_chain_start(self, serialized: dict[str, object], inputs: object, **kwargs: object) -> None:
        self._forward(
            "on_chain_start",
            _sanitize_callback_value(serialized, max_chars=self.max_chars),
            _sanitize_callback_value(inputs, max_chars=self.max_chars),
            **kwargs,
        )

    def on_chain_end(self, outputs: object, **kwargs: object) -> None:
        self._forward(
            "on_chain_end", _sanitize_callback_value(outputs, max_chars=self.max_chars), **kwargs
        )

    def on_chain_error(self, error: BaseException, **kwargs: object) -> None:
        self._forward(
            "on_chain_error",
            RuntimeError(str(sanitize_for_model_trace_and_persistence(error, max_chars=self.max_chars))),
            **kwargs,
        )

    def _forward(self, method_name: str, *args: object, **kwargs: object) -> None:
        method = getattr(self.delegate, method_name, None)
        if callable(method):
            method(*args, **_sanitize_callback_kwargs(kwargs, max_chars=self.max_chars))


def sanitize_for_model_trace_and_persistence(value: object, *, max_chars: int = 8_000) -> object:
    """Recursively redact credential-shaped values and bound large text payloads."""
    if isinstance(value, Mapping):
        return {
            str(key): "***REDACTED***"
            if _is_sensitive_key(str(key))
            else sanitize_for_model_trace_and_persistence(item, max_chars=max_chars)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [sanitize_for_model_trace_and_persistence(item, max_chars=max_chars) for item in value]
    if isinstance(value, str):
        try:
            json_value = json.loads(value)
        except json.JSONDecodeError:
            json_value = None
        if isinstance(json_value, Mapping | list):
            sanitized_json = sanitize_for_model_trace_and_persistence(json_value, max_chars=max_chars)
            return _truncate(json.dumps(sanitized_json, ensure_ascii=False, default=str), max_chars)
        redacted = _AUTHORIZATION_HEADER.sub("Authorization: ***REDACTED***", value)
        redacted = _COMMAND_LINE_COOKIE.sub(_redact_command_line_cookie, redacted)
        redacted = _COOKIE_HEADER.sub("Cookie: ***REDACTED***", redacted)
        redacted = _INLINE_SECRET.sub(
            lambda match: f"{match.group(1)}{match.group(2)}{match.group(3)}{match.group(4)}***REDACTED***",
            redacted,
        )
        redacted = _COMMAND_LINE_SECRET.sub(
            lambda match: f"{match.group(1)} ***REDACTED***", redacted
        )
        redacted = _BEARER_TOKEN.sub("Bearer ***REDACTED***", redacted)
        redacted = _HIGH_ENTROPY_CREDENTIAL.sub("***REDACTED***", redacted)
        return _truncate(redacted, max_chars)
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return _truncate(str(value), max_chars)


def _is_sensitive_key(key: str) -> bool:
    return not _TOKEN_USAGE_METADATA_KEY.fullmatch(key) and bool(_SENSITIVE_KEY.search(key))


def _redact_command_line_cookie(match: re.Match[str]) -> str:
    """Replace only the cookie value so source/code-shaped output stays parseable."""
    if match.group("quoted_double") is not None:
        value = '"***REDACTED***"'
    elif match.group("quoted_single") is not None:
        value = "'***REDACTED***'"
    else:
        value = "***REDACTED***"
    return f"{match.group('flag')}{match.group('separator')}{value}"


@dataclass
class GraphObservability:
    """Per-turn Langfuse integration that remains inert without credentials."""

    callbacks: list[object]
    enabled: bool
    _client: Any | None = None
    _metadata: dict[str, object] = field(default_factory=dict)
    event_sink: GraphEventSink | None = None
    max_chars: int = 8_000
    _closed: bool = False
    _trace_id: str | None = None

    @contextmanager
    def turn(self, name: str) -> Iterator[None]:
        if not self.enabled or self._client is None:
            with nullcontext():
                yield
            return
        with self._client.start_as_current_observation(as_type="span", name=name) as span:
            self._trace_id = self._current_trace_id() or self._trace_id
            span.update(
                input=sanitize_for_model_trace_and_persistence(
                    self._metadata, max_chars=self.max_chars
                )
            )
            yield

    @contextmanager
    def node_span(self, name: str, input_value: object | None = None) -> Iterator[None]:
        if not self.enabled or self._client is None:
            with nullcontext():
                yield
            return
        with self._client.start_as_current_observation(as_type="span", name=name) as span:
            if input_value is not None:
                span.update(
                    input=sanitize_for_model_trace_and_persistence(
                        input_value, max_chars=self.max_chars
                    )
                )
            yield

    def record_tool_event(self, event: Mapping[str, object]) -> None:
        sanitized = sanitize_for_model_trace_and_persistence(event, max_chars=self.max_chars)
        self._emit_event({"type": "tool_event", "event": sanitized})
        if not self.enabled:
            return
        with self.node_span("tool_event", sanitized):
            return

    def record_decision(self, name: str, payload: Mapping[str, object]) -> None:
        sanitized = sanitize_for_model_trace_and_persistence(payload, max_chars=self.max_chars)
        self._emit_event({"type": "decision", "name": name, "payload": sanitized})
        if not self.enabled:
            return
        with self.node_span(f"decision:{name}", sanitized):
            return

    def _emit_event(self, event: Mapping[str, object]) -> None:
        if self.event_sink is None:
            return
        sanitized = sanitize_for_model_trace_and_persistence(event, max_chars=self.max_chars)
        if isinstance(sanitized, Mapping):
            self.event_sink(dict(sanitized))

    def runnable_config(self) -> dict[str, object]:
        """Return the redacted callback configuration passed to LangGraph calls."""
        metadata = sanitize_for_model_trace_and_persistence(self._metadata, max_chars=self.max_chars)
        return {
            "callbacks": list(self.callbacks),
            "run_name": "insightagent-turn",
            "metadata": metadata,
        }

    @property
    def trace_id(self) -> str | None:
        return self._trace_id or self._current_trace_id()

    def _current_trace_id(self) -> str | None:
        if self._client is not None:
            current_trace = getattr(self._client, "get_current_trace_id", None)
            if callable(current_trace):
                value = current_trace()
                if isinstance(value, str) and value:
                    return value
        for callback in self.callbacks:
            value = getattr(callback, "last_trace_id", None)
            if isinstance(value, str) and value:
                return value
        return None

    def flush(self) -> None:
        if self.enabled and self._client is not None:
            self._client.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.enabled and self._client is not None:
            self._client.shutdown()


class GraphDebugRecorder:
    """Write a sanitized, graph-state-derived JSONL debugging stream."""

    def __init__(self, path: str | Path, *, max_chars: int = 8_000) -> None:
        self.path = Path(path).expanduser()
        self._max_chars = max_chars
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8")

    def record_turn(
        self,
        thread_id: str,
        state: Mapping[str, object],
        *,
        trace_id: str | None = None,
    ) -> None:
        event = {
            "type": "graph_turn",
            "thread_id": thread_id,
            "phase": state.get("phase"),
            "phase_history": state.get("phase_history", []),
            "tool_events": state.get("tool_events", []),
            "verification_attempts": state.get("verification_attempts", []),
            "last_tool_error": state.get("last_tool_error"),
            "final_answer": state.get("final_answer"),
            "usage": state.get("usage", {}),
            "run_manifest": state.get("run_manifest", {}),
            "trace_id": trace_id,
        }
        sanitized = sanitize_for_model_trace_and_persistence(event, max_chars=self._max_chars)
        self._file.write(json.dumps(sanitized, ensure_ascii=False, default=str) + "\n")
        self._file.flush()

    def record_event(self, event: Mapping[str, object]) -> None:
        sanitized = sanitize_for_model_trace_and_persistence(event, max_chars=self._max_chars)
        self._file.write(json.dumps(sanitized, ensure_ascii=False, default=str) + "\n")
        self._file.flush()

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()


class GraphConsoleRenderer:
    """Render the sanitized graph debug event stream to stderr."""

    def __init__(self, stream: TextIO | None = None, *, max_chars: int = 8_000) -> None:
        self._stream = stream or sys.stderr
        self._max_chars = max_chars
        self._disabled = False

    def render(self, event: Mapping[str, object]) -> None:
        if self._disabled:
            return
        sanitized = sanitize_for_model_trace_and_persistence(event, max_chars=self._max_chars)
        if not isinstance(sanitized, Mapping):
            return
        event_type = str(sanitized.get("type", "event"))
        if event_type == "decision":
            label = f"decision:{sanitized.get('name', 'unknown')}"
            payload = sanitized.get("payload", {})
        elif event_type == "tool_event":
            label = "tool_event"
            payload = sanitized.get("event", {})
        else:
            label = event_type
            payload = dict(sanitized)
        try:
            self._stream.write(
                f"[insightagent] {label} {json.dumps(payload, ensure_ascii=False, default=str)}\n"
            )
            self._stream.flush()
        except OSError:
            self._disabled = True


def build_observability(
    *,
    workspace: str,
    thread_id: str,
    provider: str,
    model: str,
    task: str = "",
    run_id: str | None = None,
    tool_profile: str = "coding-basic",
    event_sink: GraphEventSink | None = None,
    trace_max_chars: int = 8_000,
) -> GraphObservability:
    """Build one Langfuse v3 integration without reading dotenv files directly."""
    if trace_max_chars < 1:
        raise ValueError("trace_max_chars must be positive")
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    if not public_key or not secret_key:
        return GraphObservability(
            callbacks=[], enabled=False, event_sink=event_sink, max_chars=trace_max_chars
        )
    metadata: dict[str, object] = {
        "langfuse_session_id": thread_id,
        "run_id": run_id or thread_id,
        "workspace": workspace,
        "provider": provider,
        "model": model,
        "tool_profile": tool_profile,
        "task_hash": hashlib.sha256(task.encode("utf-8")).hexdigest(),
    }
    return GraphObservability(
        callbacks=[SanitizingLangfuseCallback(CallbackHandler(), max_chars=trace_max_chars)],
        enabled=True,
        _client=get_client(),
        _metadata=metadata,
        event_sink=event_sink,
        max_chars=trace_max_chars,
    )


def _truncate(value: str, max_chars: int) -> str:
    if max_chars < 1:
        return ""
    return value if len(value) <= max_chars else f"{value[:max_chars]}...[truncated]"


def _sanitize_callback_value(value: object, *, max_chars: int = 8_000) -> object:
    if isinstance(value, BaseMessage):
        payload = sanitize_for_model_trace_and_persistence(
            value.model_dump(mode="python"), max_chars=max_chars
        )
        if isinstance(payload, Mapping):
            return type(value).model_validate(dict(payload))
    if isinstance(value, list):
        return [_sanitize_callback_value(item, max_chars=max_chars) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_callback_value(item, max_chars=max_chars) for item in value)
    if isinstance(value, Mapping):
        return {
            str(key): "***REDACTED***"
            if _SENSITIVE_KEY.search(str(key))
            else _sanitize_callback_value(item, max_chars=max_chars)
            for key, item in value.items()
        }
    return sanitize_for_model_trace_and_persistence(value, max_chars=max_chars)


def _sanitize_callback_kwargs(
    kwargs: Mapping[str, object], *, max_chars: int = 8_000
) -> dict[str, object]:
    return {
        key: value
        if key in _CALLBACK_CONTROL_FIELDS
        else _sanitize_callback_value(value, max_chars=max_chars)
        for key, value in kwargs.items()
    }


def _sanitize_llm_result(response: object, *, max_chars: int = 8_000) -> object:
    sanitized: Any = deepcopy(response)
    generations = getattr(sanitized, "generations", None)
    if isinstance(generations, list):
        for row in generations:
            if not isinstance(row, list):
                continue
            for generation in row:
                message = getattr(generation, "message", None)
                if isinstance(message, BaseMessage):
                    generation.message = _sanitize_callback_value(message, max_chars=max_chars)
                text = getattr(generation, "text", None)
                if isinstance(text, str):
                    generation.text = str(
                        sanitize_for_model_trace_and_persistence(text, max_chars=max_chars)
                    )
                generation_info = getattr(generation, "generation_info", None)
                if isinstance(generation_info, Mapping):
                    generation.generation_info = sanitize_for_model_trace_and_persistence(
                        generation_info, max_chars=max_chars
                    )
    llm_output = getattr(sanitized, "llm_output", None)
    if isinstance(llm_output, Mapping):
        sanitized.llm_output = sanitize_for_model_trace_and_persistence(
            llm_output, max_chars=max_chars
        )
    return sanitized


__all__ = [
    "GraphDebugRecorder",
    "GraphConsoleRenderer",
    "GraphObservability",
    "SanitizingLangfuseCallback",
    "build_observability",
    "sanitize_for_model_trace_and_persistence",
]
