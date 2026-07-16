"""由运行时配置构造并绑定工具的 LangChain 聊天模型。"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Protocol, cast

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from insightagent.config import RuntimeConfig


class ModelConfigurationError(ValueError):
    """模型供应商、凭据或模型名称未被正确配置。"""


class _OpenAIConstructor(Protocol):
    """补充 ChatOpenAI 类型桩未声明的 Pydantic 参数别名。"""

    def __call__(
        self,
        *,
        model: str,
        api_key: SecretStr,
        base_url: str | None,
        timeout: int,
        max_tokens: int,
        max_retries: int,
        temperature: float,
        top_p: float,
    ) -> ChatOpenAI: ...


class _AnthropicConstructor(Protocol):
    """补充 ChatAnthropic 类型桩未声明的 Pydantic 参数别名。"""

    def __call__(
        self,
        *,
        model: str,
        api_key: SecretStr,
        base_url: str | None,
        timeout: int,
        max_tokens: int,
        max_retries: int,
        temperature: float,
        top_p: float,
    ) -> ChatAnthropic: ...


def build_chat_model(config: RuntimeConfig, tools: Sequence[BaseTool]) -> Runnable:
    """构造配置指定的模型，并以稳定列表绑定 LangChain 工具。"""
    fake_model = os.environ.get("INSIGHTAGENT_FAKE_MODEL")
    if fake_model:
        return _build_test_model(fake_model)
    if not isinstance(config.provider, str):
        raise ModelConfigurationError(f"Unsupported model provider: {config.provider}")
    provider = config.provider.lower()
    if provider not in {"openai", "siliconflow", "anthropic"}:
        raise ModelConfigurationError(f"Unsupported model provider: {config.provider}")

    api_key = _required_secret(os.environ.get("API_KEY"), "API_KEY")
    model_name = _configured_or_environment_value(config.model, "MODEL_ID")
    base_url = _configured_or_environment_value(config.base_url, "BASE_URL")
    model: ChatOpenAI | ChatAnthropic
    if provider in {"openai", "siliconflow"}:
        openai_constructor = cast(_OpenAIConstructor, ChatOpenAI)
        model = openai_constructor(
            model=model_name,
            api_key=api_key,
            base_url=base_url,
            timeout=config.timeout,
            max_tokens=config.max_output_tokens,
            max_retries=config.max_retries,
            temperature=config.temperature,
            top_p=config.top_p,
        )
    else:
        anthropic_constructor = cast(_AnthropicConstructor, ChatAnthropic)
        model = anthropic_constructor(
            model=model_name,
            api_key=api_key,
            base_url=base_url,
            timeout=config.timeout,
            max_tokens=config.max_output_tokens,
            max_retries=config.max_retries,
            temperature=config.temperature,
            top_p=config.top_p,
        )
    return model.bind_tools(list(tools))


def _required_value(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelConfigurationError(f"{name} is required")
    return value


def _configured_or_environment_value(value: object, environment_name: str) -> str:
    if value is None:
        value = os.environ.get(environment_name)
    return _required_value(value, environment_name)


def _required_secret(value: object, name: str) -> SecretStr:
    return SecretStr(_required_value(value, name))


def _build_test_model(name: str) -> Runnable:
    """构造仅用于安装冒烟测试的确定性工具调用模型。"""
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        raise ModelConfigurationError(
            "INSIGHTAGENT_FAKE_MODEL is available only while pytest is running"
        )
    if name != "write-and-verify":
        raise ModelConfigurationError(f"unknown INSIGHTAGENT_FAKE_MODEL: {name}")
    responses = iter(
        (
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"path": "hello.py", "content": "print('hello')\n"},
                        "id": "fake-write",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "run_verification",
                        "args": {"command": "python -m py_compile hello.py"},
                        "id": "fake-verify",
                    }
                ],
            ),
            AIMessage(content="已创建并验证 hello.py。"),
        )
    )

    async def respond(_input: object, **_kwargs: object) -> AIMessage:
        return next(responses, AIMessage(content="已完成。"))

    return RunnableLambda(respond)
