from __future__ import annotations

import asyncio
from contextlib import contextmanager
from io import StringIO
import json
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, Generation, LLMResult

from insightagent.graph.nodes import GraphServices, call_model, prepare_task
from insightagent.graph.tools import ContractAwareToolInvoker
from insightagent.runtime.tool_context import ToolContext
from tests.graph.fakes import ScriptedRunnable


class _FakeSpan:
    def __init__(self, payloads: list[object]) -> None:
        self._payloads = payloads

    def update(self, **kwargs: object) -> None:
        self._payloads.append(kwargs)


class _FakeLangfuse:
    def __init__(self) -> None:
        self.payloads: list[object] = []
        self.flush_called = False
        self.shutdown_called = False

    @contextmanager
    def start_as_current_observation(self, **kwargs: object):
        self.payloads.append(kwargs)
        yield _FakeSpan(self.payloads)

    def flush(self) -> None:
        self.flush_called = True

    def shutdown(self) -> None:
        self.shutdown_called = True


class _EphemeralTraceLangfuse(_FakeLangfuse):
    def __init__(self) -> None:
        super().__init__()
        self.active = False

    @contextmanager
    def start_as_current_observation(self, **kwargs: object):
        self.active = True
        self.payloads.append(kwargs)
        try:
            yield _FakeSpan(self.payloads)
        finally:
            self.active = False

    def get_current_trace_id(self) -> str | None:
        return "trace-1" if self.active else None


class _CallbackCapture:
    def __init__(self) -> None:
        self.payloads: list[object] = []
        self.keyword_payloads: list[dict[str, object]] = []

    def on_llm_end(self, response: object, **_kwargs: object) -> None:
        self.payloads.append(response)

    def on_tool_end(self, output: object, **kwargs: object) -> None:
        self.payloads.append(output)
        self.keyword_payloads.append(kwargs)

    def on_chat_model_start(
        self, _serialized: object, messages: object, **_kwargs: object
    ) -> None:
        self.payloads.append(messages)


def test_observability_is_disabled_without_langfuse_credentials(monkeypatch) -> None:
    from insightagent.graph.observability import build_observability

    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    observability = build_observability(
        workspace="/tmp/work", thread_id="t-1", provider="openai", model="test-model"
    )

    assert observability.callbacks == []
    assert observability.enabled is False


def test_observability_uses_callbacks_spans_and_flushes_without_leaking_secrets(
    monkeypatch,
) -> None:
    from insightagent.graph.observability import build_observability

    fake_client = _FakeLangfuse()
    fake_callback = object()
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setattr("insightagent.graph.observability.get_client", lambda: fake_client)
    monkeypatch.setattr("insightagent.graph.observability.CallbackHandler", lambda: fake_callback)

    observability = build_observability(
        workspace="/tmp/work",
        thread_id="t-1",
        provider="openai",
        model="test-model",
        task="修复模块 api_key=canary-secret-123",
    )
    with observability.turn("insightagent-turn"):
        observability.record_tool_event(
            {
                "arguments": {"Authorization": "Bearer canary-secret-123"},
                "content": "password=canary-secret-123",
            }
        )
        with observability.node_span("execute_tools", {"token": "canary-secret-123"}):
            pass
    observability.flush()
    observability.close()

    serialized = repr(fake_client.payloads)
    assert len(observability.callbacks) == 1
    assert getattr(observability.callbacks[0], "delegate") is fake_callback
    assert "canary-secret-123" not in serialized
    assert "***REDACTED***" in serialized
    assert fake_client.flush_called is True
    assert fake_client.shutdown_called is True


def test_observability_keeps_trace_id_after_turn_span_exits(monkeypatch) -> None:
    from insightagent.graph.observability import build_observability

    fake_client = _EphemeralTraceLangfuse()
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setattr("insightagent.graph.observability.get_client", lambda: fake_client)
    monkeypatch.setattr("insightagent.graph.observability.CallbackHandler", object)
    observability = build_observability(
        workspace="/tmp/work", thread_id="t-1", provider="openai", model="test-model"
    )

    with observability.turn("insightagent-turn"):
        assert observability.trace_id == "trace-1"

    assert observability.trace_id == "trace-1"


def test_observability_applies_the_configured_trace_output_limit(monkeypatch) -> None:
    from insightagent.graph.observability import build_observability

    fake_client = _FakeLangfuse()
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setattr("insightagent.graph.observability.get_client", lambda: fake_client)
    monkeypatch.setattr("insightagent.graph.observability.CallbackHandler", object)
    observability = build_observability(
        workspace="/tmp/work",
        thread_id="t-1",
        provider="openai",
        model="test-model",
        trace_max_chars=32,
    )

    with observability.turn("insightagent-turn"):
        observability.record_tool_event({"content": "A" * 200})

    serialized = repr(fake_client.payloads)
    assert "A" * 40 not in serialized
    assert "[truncated]" in serialized


def test_sanitizer_redacts_nested_values_and_truncates_tool_output() -> None:
    from insightagent.graph.observability import sanitize_for_model_trace_and_persistence

    sanitized = sanitize_for_model_trace_and_persistence(
        {"nested": ["Authorization: Bearer canary-secret-123", {"api_key": "canary-secret-123"}]},
        max_chars=32,
    )

    serialized = repr(sanitized)
    assert "canary-secret-123" not in serialized
    assert "***REDACTED***" in serialized


def test_sanitizer_redacts_credential_values_inside_json_strings() -> None:
    from insightagent.graph.observability import sanitize_for_model_trace_and_persistence

    secret = "canary-secret-123"
    sanitized = sanitize_for_model_trace_and_persistence(
        json.dumps(
            {
                "api_key": secret,
                "nested": {"token": secret},
                "content": f"Authorization: Bearer {secret}",
            }
        )
    )

    assert isinstance(sanitized, str)
    assert secret not in sanitized
    assert "***REDACTED***" in sanitized


def test_sanitizer_preserves_ai_message_usage_token_detail_mappings() -> None:
    from insightagent.graph.observability import sanitize_for_model_trace_and_persistence

    message = AIMessage(
        content="完成",
        usage_metadata={
            "input_tokens": 12,
            "output_tokens": 3,
            "total_tokens": 15,
            "input_token_details": {"cache_read": 4},
            "output_token_details": {"reasoning": 2},
        },
        response_metadata={"token_usage": {"prompt_tokens": 12, "completion_tokens": 3}},
        additional_kwargs={"api_key": "canary-secret-123"},
    )

    sanitized = sanitize_for_model_trace_and_persistence(message.model_dump(mode="python"))

    assert isinstance(sanitized, dict)
    restored = AIMessage.model_validate(sanitized)
    assert restored.usage_metadata is not None
    assert restored.usage_metadata["input_token_details"] == {"cache_read": 4}
    assert restored.usage_metadata["output_token_details"] == {"reasoning": 2}
    assert restored.response_metadata["token_usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 3,
    }
    assert "canary-secret-123" not in repr(sanitized)


def test_sanitizer_redacts_command_line_and_high_entropy_credentials() -> None:
    from insightagent.graph.observability import sanitize_for_model_trace_and_persistence

    sanitized = sanitize_for_model_trace_and_persistence(
        "deploy --api-key canary-secret-123 --access-token canary-secret-123 --region cn; "
        "Authorization: Basic canary-secret-123; Authorization=Digest canary-secret-123; "
        "token=canary-secret-123; access_token=canary-secret-123; "
        "sk-abcdefghijklmnopqrstuvwxyz0123456789"
    )

    serialized = repr(sanitized)
    assert "canary-secret-123" not in serialized
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in serialized
    assert "***REDACTED***" in serialized


def test_sanitizer_redacts_inline_credential_cookie_and_client_secret() -> None:
    from insightagent.graph.observability import sanitize_for_model_trace_and_persistence

    credential = "credential-canary-123"
    cookie = "cookie-canary-123"
    client_secret = "client-secret-canary-123"
    sanitized = sanitize_for_model_trace_and_persistence(
        f"credential={credential}; cookie: {cookie}; client_secret={client_secret}; "
        f"--credential {credential} --cookie {cookie} --client-secret {client_secret}"
    )

    serialized = repr(sanitized)
    assert credential not in serialized
    assert cookie not in serialized
    assert client_secret not in serialized
    assert "***REDACTED***" in serialized


def test_sanitizer_redacts_every_value_in_inline_and_command_line_cookies() -> None:
    from insightagent.graph.observability import sanitize_for_model_trace_and_persistence

    session = "session-canary-123"
    csrf = "csrf-canary-123"
    sanitized = sanitize_for_model_trace_and_persistence(
        f"Cookie: session={session}; csrf={csrf}; "
        f'curl --cookie "session={session}; csrf={csrf}" https://example.test'
    )

    serialized = repr(sanitized)
    assert session not in serialized
    assert csrf not in serialized
    assert "***REDACTED***" in serialized


def test_sanitizer_redacts_quoted_spaced_and_short_option_cookies() -> None:
    from insightagent.graph.observability import sanitize_for_model_trace_and_persistence

    quoted = "quoted-cookie-canary-123"
    spaced = "spaced-cookie-canary-123"
    short_option = "short-cookie-canary-123"
    sanitized = sanitize_for_model_trace_and_persistence(
        json.dumps(
            {
                "headers": f'Cookie: session="prefix;{quoted}"; csrf = {spaced}',
                "command": f"curl -b 'session={short_option}; csrf={quoted}' https://example.test",
            }
        )
    )

    serialized = repr(sanitized)
    assert quoted not in serialized
    assert spaced not in serialized
    assert short_option not in serialized
    assert "***REDACTED***" in serialized


def test_langfuse_callback_proxy_receives_only_sanitized_outputs() -> None:
    from insightagent.graph.observability import SanitizingLangfuseCallback

    capture = _CallbackCapture()
    callback = SanitizingLangfuseCallback(capture)
    attached_cookie = "attached-cookie-canary-917"
    attached_cookie_command = f"curl -b'session={attached_cookie}; csrf=other'"
    callback.on_llm_end(
        LLMResult(
            generations=[
                [
                    ChatGeneration(
                        message=AIMessage(content=attached_cookie_command),
                        generation_info={"client_secret": attached_cookie_command},
                    )
                ],
                [Generation(text=attached_cookie_command)],
            ],
            llm_output={"command": attached_cookie_command},
        )
    )
    callback.on_tool_end(
        {"Authorization": "Basic canary-secret-123"},
        inputs={"api_key": "canary-secret-123"},
        invocation_params={"Authorization": "Digest canary-secret-123"},
    )
    callback.on_chat_model_start(
        {}, [[AIMessage(content="--access-token canary-secret-123")]], run_id=uuid4()
    )

    serialized = repr(capture.payloads)
    assert len(capture.payloads) == 3
    assert isinstance(capture.payloads[-1], list)
    assert isinstance(capture.payloads[-1][0][0], AIMessage)
    assert attached_cookie not in serialized
    assert "canary-secret-123" not in serialized
    assert "canary-secret-123" not in repr(capture.keyword_payloads)
    assert "***REDACTED***" in serialized


def test_model_response_is_sanitized_before_entering_graph_state(tmp_path: Path) -> None:
    context = ToolContext(workspace=tmp_path)
    services = GraphServices(
        model=ScriptedRunnable([AIMessage(content="token=canary-secret-123")]),
        tools={},
        tool_specs={},
        tool_context=context,
        tool_invoker=ContractAwareToolInvoker(context),
    )

    update = asyncio.run(call_model(services.initial_state("分析仓库"), {}, services))

    assert "canary-secret-123" not in repr(update)
    assert "***REDACTED***" in repr(update)


def test_project_memory_is_sanitized_before_entering_graph_state(tmp_path: Path) -> None:
    context = ToolContext(workspace=tmp_path)
    services = GraphServices(
        model=ScriptedRunnable([]),
        tools={},
        tool_specs={},
        tool_context=context,
        tool_invoker=ContractAwareToolInvoker(context),
        project_memory="部署说明：api_key=canary-secret-123",
    )

    update = asyncio.run(prepare_task(services.initial_state("分析仓库"), {}, services))

    assert "canary-secret-123" not in repr(update)
    assert "***REDACTED***" in repr(update)


def test_graph_debug_recorder_writes_only_sanitized_graph_state(tmp_path: Path) -> None:
    from insightagent.graph.observability import GraphDebugRecorder

    path = tmp_path / "graph-events.jsonl"
    recorder = GraphDebugRecorder(path)
    try:
        recorder.record_turn(
            "thread-1",
            {
                "phase": "failed",
                "tool_events": [{"content": "token=canary-secret-123"}],
                "verification_attempts": [{"command": "pytest -q", "exit_code": 1}],
                "final_answer": "api_key=canary-secret-123",
            },
            trace_id="trace-1",
        )
    finally:
        recorder.close()

    event = json.loads(path.read_text(encoding="utf-8"))
    assert event["type"] == "graph_turn"
    assert event["thread_id"] == "thread-1"
    assert event["phase"] == "failed"
    assert event["trace_id"] == "trace-1"
    assert "canary-secret-123" not in repr(event)
    assert "***REDACTED***" in repr(event)


def test_console_renderer_never_propagates_a_closed_output_pipe() -> None:
    from insightagent.graph.observability import GraphConsoleRenderer

    class BrokenStream(StringIO):
        def write(self, _value: str) -> int:
            raise BrokenPipeError("closed")

    renderer = GraphConsoleRenderer(BrokenStream())

    renderer.render({"type": "decision", "name": "phase_transition", "payload": {}})
    renderer.render({"type": "graph_turn", "phase": "done"})
