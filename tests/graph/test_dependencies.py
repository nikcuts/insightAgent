"""运行时框架依赖的安装契约。"""

import importlib
import importlib.metadata

import pytest


@pytest.mark.parametrize(
    "module_name",
    [
        "langgraph",
        "langchain",
        "langchain_core",
        "langchain_openai",
        "langchain_anthropic",
        "langchain_mcp_adapters",
        "langfuse",
        "langgraph.checkpoint.sqlite",
        "filelock",
    ],
)
def test_runtime_framework_dependency_is_importable(module_name: str) -> None:
    """完整运行时声明的每个框架包都必须可以导入。"""
    assert importlib.import_module(module_name) is not None


def test_langfuse_major_version_is_3() -> None:
    """Langfuse v3 接口是重构运行时的兼容性基线。"""
    assert importlib.metadata.version("langfuse").split(".", maxsplit=1)[0] == "3"


def test_runtime_entry_points_are_importable() -> None:
    """工作流、可观测性和 MCP 运行时所需入口必须存在。"""
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langfuse.langchain import CallbackHandler
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    assert AsyncSqliteSaver is not None
    assert CallbackHandler is not None
    assert MultiServerMCPClient is not None
