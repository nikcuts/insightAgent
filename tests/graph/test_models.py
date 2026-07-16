"""LangChain 聊天模型工厂的统一环境变量边界测试。"""

from __future__ import annotations

import os
import socket
import warnings
from dataclasses import replace
from typing import NoReturn, TypedDict, cast

import pytest
from langchain_core.runnables import Runnable, RunnableBinding
from langchain_core.tools import BaseTool, tool
from pydantic import SecretStr

from insightagent.config import RuntimeConfig
from insightagent.graph.models import ModelConfigurationError, build_chat_model


class RecordingChatModel:
    """记录工厂构造与工具绑定参数，不执行模型调用。"""

    def __init__(self, constructor: str, **kwargs: object) -> None:
        self.constructor = constructor
        self.kwargs = kwargs
        self.bound_tools: list[BaseTool] | None = None

    def bind_tools(self, tools: list[BaseTool]) -> "RecordingChatModel":
        self.bound_tools = tools
        return self


class RuntimeConfigArguments(TypedDict):
    timeout: int
    max_output_tokens: int
    max_retries: int
    temperature: float
    top_p: float


class LegacyEnvironmentTrap:
    """确保模型工厂完全不读取供应商专有变量。"""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, key: str, default: object = None) -> object:
        if key.startswith(("SILICONFLOW_", "OPENAI_", "ANTHROPIC_")):
            raise AssertionError(f"不应读取旧环境变量：{key}")
        return self._values.get(key, default)


def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    environment: dict[str, str] = {}
    monkeypatch.setattr(os, "environ", environment)
    return environment


@pytest.fixture(autouse=True)
def clear_environment(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    return _isolate_environment(monkeypatch)


@pytest.fixture
def model_factories(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, list[RecordingChatModel]]:
    instances = {"openai": [], "anthropic": []}

    def make_constructor(name: str):
        def construct(**kwargs: object) -> RecordingChatModel:
            model = RecordingChatModel(name, **kwargs)
            instances[name].append(model)
            return model

        return construct

    monkeypatch.setattr(
        "insightagent.graph.models.ChatOpenAI", make_constructor("openai")
    )
    monkeypatch.setattr(
        "insightagent.graph.models.ChatAnthropic", make_constructor("anthropic")
    )
    return instances


def runtime_config_kwargs() -> RuntimeConfigArguments:
    return {
        "timeout": 37,
        "max_output_tokens": 1234,
        "max_retries": 5,
        "temperature": 0.2,
        "top_p": 0.7,
    }


def expected_model_kwargs() -> dict[str, object]:
    return {
        "api_key": SecretStr("test-key"),
        "timeout": 37,
        "max_tokens": 1234,
        "max_retries": 5,
        "temperature": 0.2,
        "top_p": 0.7,
    }


@tool
def echo_tool(value: str) -> str:
    """回显传入的字符串。"""
    return value


def test_environment_helper_replaces_and_restores_mapping_identity() -> None:
    sentinel_environment = {"SENTINEL": "sentinel"}
    with pytest.MonkeyPatch.context() as sentinel_patch:
        sentinel_patch.setattr(os, "environ", sentinel_environment)
        with pytest.MonkeyPatch.context() as isolated_patch:
            environment = _isolate_environment(isolated_patch)

            assert os.environ is environment
            assert environment == {}
            isolated_patch.setenv("TEST_ONLY_KEY", "test")
            assert os.environ.get("TEST_ONLY_KEY") == "test"

        assert os.environ is sentinel_environment
        assert sentinel_environment == {"SENTINEL": "sentinel"}


@pytest.mark.parametrize(
    ("provider", "constructor"),
    [
        ("openai", "openai"),
        ("siliconflow", "openai"),
        ("anthropic", "anthropic"),
    ],
)
def test_all_providers_use_unified_environment_defaults_and_correct_client(
    monkeypatch: pytest.MonkeyPatch,
    model_factories: dict[str, list[RecordingChatModel]],
    provider: str,
    constructor: str,
) -> None:
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("MODEL_ID", "environment-model")
    monkeypatch.setenv("BASE_URL", "https://environment.example/v1")

    bound = build_chat_model(
        RuntimeConfig(provider=provider, **runtime_config_kwargs()), [echo_tool]
    )
    model = model_factories[constructor][0]

    assert bound is model
    assert model.constructor == constructor
    assert model.kwargs == {
        "model": "environment-model",
        "base_url": "https://environment.example/v1",
        **expected_model_kwargs(),
    }
    assert model.bound_tools == [echo_tool]


@pytest.mark.parametrize("provider", ["openai", "siliconflow", "anthropic"])
def test_config_model_and_base_url_override_unified_environment_defaults(
    monkeypatch: pytest.MonkeyPatch,
    model_factories: dict[str, list[RecordingChatModel]],
    provider: str,
) -> None:
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("MODEL_ID", "environment-model")
    monkeypatch.setenv("BASE_URL", "https://environment.example/v1")

    build_chat_model(
        RuntimeConfig(
            provider=provider,
            model="configured-model",
            base_url="https://configured.example/v1",
        ),
        [],
    )
    constructor = "anthropic" if provider == "anthropic" else "openai"
    model = model_factories[constructor][0]

    assert model.kwargs["model"] == "configured-model"
    assert model.kwargs["base_url"] == "https://configured.example/v1"


def test_factory_never_reads_legacy_provider_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
    model_factories: dict[str, list[RecordingChatModel]],
) -> None:
    environment = LegacyEnvironmentTrap(
        {
            "API_KEY": "test-key",
            "MODEL_ID": "environment-model",
            "BASE_URL": "https://environment.example/v1",
        }
    )
    monkeypatch.setattr(
        "insightagent.graph.models.os",
        type("FakeOS", (), {"environ": environment}),
    )

    build_chat_model(RuntimeConfig(provider="siliconflow"), [])

    assert model_factories["openai"][0].kwargs["model"] == "environment-model"


@pytest.mark.parametrize("provider", ["openai", "siliconflow", "anthropic"])
def test_legacy_variables_cannot_supply_unified_api_key(
    monkeypatch: pytest.MonkeyPatch,
    model_factories: dict[str, list[RecordingChatModel]],
    provider: str,
) -> None:
    for name in (
        "SILICONFLOW_API_KEY",
        "SILICONFLOW_BASE_URL",
        "SILICONFLOW_MODEL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
    ):
        monkeypatch.setenv(name, "legacy-value")

    with pytest.raises(ModelConfigurationError, match=r"^API_KEY is required$"):
        build_chat_model(
            RuntimeConfig(
                provider=provider,
                model="configured-model",
                base_url="https://configured.example/v1",
            ),
            [],
        )

    assert model_factories["openai"] == []
    assert model_factories["anthropic"] == []


@pytest.mark.parametrize(
    ("environment", "config", "error_name"),
    [
        (
            {},
            RuntimeConfig(
                provider="openai",
                model="configured-model",
                base_url="https://configured.example/v1",
            ),
            "API_KEY",
        ),
        (
            {"API_KEY": "test-key"},
            RuntimeConfig(provider="openai", base_url="https://configured.example/v1"),
            "MODEL_ID",
        ),
        (
            {"API_KEY": "test-key", "MODEL_ID": "environment-model"},
            RuntimeConfig(provider="openai", model="configured-model"),
            "BASE_URL",
        ),
    ],
)
def test_missing_unified_values_raise_their_unified_variable_name(
    monkeypatch: pytest.MonkeyPatch,
    model_factories: dict[str, list[RecordingChatModel]],
    environment: dict[str, str],
    config: RuntimeConfig,
    error_name: str,
) -> None:
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ModelConfigurationError, match=rf"^{error_name} is required$"):
        build_chat_model(config, [])

    assert model_factories["openai"] == []
    assert model_factories["anthropic"] == []


@pytest.mark.parametrize(
    ("field", "value", "error_name"),
    [
        ("model", "", "MODEL_ID"),
        ("model", cast(str | None, 123), "MODEL_ID"),
        ("base_url", "", "BASE_URL"),
        ("base_url", cast(str | None, 123), "BASE_URL"),
    ],
)
def test_explicit_invalid_model_or_base_url_does_not_fall_back_to_environment(
    monkeypatch: pytest.MonkeyPatch,
    model_factories: dict[str, list[RecordingChatModel]],
    field: str,
    value: str | None,
    error_name: str,
) -> None:
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("MODEL_ID", "environment-model")
    monkeypatch.setenv("BASE_URL", "https://environment.example/v1")
    config = RuntimeConfig(
        provider="siliconflow",
        model="configured-model",
        base_url="https://configured.example/v1",
    )
    config = replace(config, **{field: value})

    with pytest.raises(ModelConfigurationError, match=rf"^{error_name} is required$"):
        build_chat_model(config, [])

    assert model_factories["openai"] == []


@pytest.mark.parametrize("value", ["", cast(str, 123)])
def test_invalid_api_key_is_a_unified_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
    model_factories: dict[str, list[RecordingChatModel]],
    value: str,
) -> None:
    environment = _isolate_environment(monkeypatch)
    environment["API_KEY"] = value

    with pytest.raises(ModelConfigurationError, match=r"^API_KEY is required$"):
        build_chat_model(
            RuntimeConfig(
                provider="anthropic",
                model="configured-model",
                base_url="https://configured.example/v1",
            ),
            [],
        )

    assert model_factories["anthropic"] == []


@pytest.mark.parametrize(
    ("provider", "model_name", "base_url"),
    [
        ("openai", "gpt-test", "https://openai.example/v1"),
        ("siliconflow", "Qwen/test", "https://silicon.example/v1"),
        ("anthropic", "claude-test", "https://anthropic.example"),
    ],
)
def test_real_chat_models_bind_tools_without_network_or_warnings(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    model_name: str,
    base_url: str,
) -> None:
    def reject_socket_connection(*args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        raise AssertionError("Network socket connections are disabled in this test")

    monkeypatch.setattr(socket.socket, "connect", reject_socket_connection)
    monkeypatch.setattr(socket, "create_connection", reject_socket_connection)
    monkeypatch.setenv("API_KEY", "test-key")

    with pytest.raises(AssertionError, match="Network socket connections are disabled"):
        socket.create_connection(("localhost", 1))
    with socket.socket() as socket_instance:
        with pytest.raises(
            AssertionError, match="Network socket connections are disabled"
        ):
            socket_instance.connect(("localhost", 1))

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        bound = build_chat_model(
            RuntimeConfig(provider=provider, model=model_name, base_url=base_url),
            [echo_tool],
        )

    assert isinstance(bound, Runnable)
    assert isinstance(bound, RunnableBinding)
    assert "tools" in bound.kwargs
    assert bound.kwargs["tools"]


def test_chat_openai_constructor_error_propagates_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_constructor(**kwargs: object) -> NoReturn:
        del kwargs
        raise RuntimeError("openai constructor failed")

    monkeypatch.setattr("insightagent.graph.models.ChatOpenAI", fail_constructor)
    monkeypatch.setenv("API_KEY", "test-key")

    with pytest.raises(RuntimeError, match="openai constructor failed"):
        build_chat_model(
            RuntimeConfig(
                provider="openai",
                model="gpt-test",
                base_url="https://openai.example/v1",
            ),
            [echo_tool],
        )


def test_non_string_provider_is_a_configuration_error(
    model_factories: dict[str, list[RecordingChatModel]],
) -> None:
    with pytest.raises(
        ModelConfigurationError, match="Unsupported model provider: 123"
    ):
        build_chat_model(RuntimeConfig(provider=cast(str, 123), model="test"), [])

    assert model_factories["openai"] == []
    assert model_factories["anthropic"] == []
