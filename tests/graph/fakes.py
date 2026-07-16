"""供 LangGraph 测试共享的可脚本化 LangChain Runnable 伪件。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable, RunnableConfig


class ScriptedRunnable(Runnable[Any, AIMessage]):
    """按脚本顺序返回 AI 消息，并保存每次调用的配置。"""

    def __init__(self, responses: Sequence[AIMessage | str]) -> None:
        self._responses = [
            response if isinstance(response, AIMessage) else AIMessage(content=response)
            for response in responses
        ]
        self.invoke_configs: list[RunnableConfig | None] = []
        self.ainvoke_configs: list[RunnableConfig | None] = []
        self.invoke_kwargs: list[dict[str, Any]] = []
        self.ainvoke_kwargs: list[dict[str, Any]] = []
        self.invoke_inputs: list[Any] = []
        self.ainvoke_inputs: list[Any] = []

    def _next_response(self) -> AIMessage:
        if not self._responses:
            raise AssertionError("ScriptedRunnable 的响应脚本已耗尽")
        return self._responses.pop(0)

    def invoke(
        self, input: Any, config: RunnableConfig | None = None, **kwargs: Any
    ) -> AIMessage:
        self.invoke_inputs.append(input)
        self.invoke_configs.append(config)
        self.invoke_kwargs.append(dict(kwargs))
        return self._next_response()

    async def ainvoke(
        self,
        input: Any,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> AIMessage:
        self.ainvoke_inputs.append(input)
        self.ainvoke_configs.append(config)
        self.ainvoke_kwargs.append(dict(kwargs))
        return self._next_response()
