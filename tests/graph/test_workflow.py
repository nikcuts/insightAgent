from __future__ import annotations

import asyncio
from contextlib import nullcontext
import json
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict

from insightagent.graph.tools import ContractAwareToolInvoker
from insightagent.runtime.tool_context import ToolContext
from insightagent.runtime.types import ToolPermission, ToolRisk, ToolSpec
from tests.graph.fakes import ScriptedRunnable


class _PathArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str


class _EditArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str
    old: str
    new: str


class _VerificationArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command: str


class _PhaseObserver:
    def __init__(self) -> None:
        self.transitions: list[tuple[object, object]] = []

    def node_span(self, _name: str, _input: object = None):
        return nullcontext()

    def record_tool_event(self, _event: object) -> None:
        return None

    def record_decision(self, name: str, payload: dict[str, object]) -> None:
        if name == "phase_transition":
            self.transitions.append((payload.get("from_phase"), payload.get("phase")))

    def runnable_config(self) -> dict[str, object]:
        return {"metadata": {}}


def _tool_spec(name: str, *, mutates: bool = False, executes: bool = False) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=name,
        input_schema={},
        required_permission=(
            ToolPermission.EXECUTE
            if executes
            else ToolPermission.WORKSPACE_WRITE if mutates else ToolPermission.READ
        ),
        risk=ToolRisk.HIGH if executes else ToolRisk.MEDIUM if mutates else ToolRisk.LOW,
        mutates_workspace=mutates,
        executes_code=executes,
    )


def test_workflow_records_all_node_phase_transitions(tmp_path: Path) -> None:
    from insightagent.graph.workflow import GraphServices, build_graph

    observer = _PhaseObserver()
    context = ToolContext(workspace=tmp_path)
    services = GraphServices(
        model=ScriptedRunnable([AIMessage(content="分析完成。")]),
        tools={},
        tool_specs={},
        tool_context=context,
        tool_invoker=ContractAwareToolInvoker(context),
        observability=observer,  # type: ignore[arg-type]
    )

    result = asyncio.run(
        build_graph(services).ainvoke(
            services.initial_state("分析仓库"), {"configurable": {"thread_id": "phase-1"}}
        )
    )

    assert result["phase"] == "done"
    assert ("plan", "inspect") in observer.transitions
    assert ("inspect", "done") in observer.transitions


def test_model_call_places_system_context_before_user_messages(tmp_path: Path) -> None:
    from insightagent.graph.nodes import GraphServices, call_model

    model = ScriptedRunnable([AIMessage(content="完成")])
    context = ToolContext(workspace=tmp_path)
    services = GraphServices(
        model=model,
        tools={},
        tool_specs={},
        tool_context=context,
        tool_invoker=ContractAwareToolInvoker(context),
    )
    state = services.initial_state("分析仓库")
    state["messages"] = [
        HumanMessage(content="分析仓库"),
        SystemMessage(content="任务约束"),
        SystemMessage(content="仓库快照"),
    ]

    asyncio.run(call_model(state, {}, services))

    request = model.ainvoke_inputs[0]
    assert [message.type for message in request] == ["system", "human"]
    assert [message.content for message in request] == ["任务约束\n\n仓库快照", "分析仓库"]


def test_graph_compacts_tool_output_before_the_next_model_call(tmp_path: Path) -> None:
    from insightagent.graph.workflow import GraphServices, build_graph

    async def read_file(path: str) -> dict[str, object]:
        return {"is_error": False, "content": f"{path}:" + "A" * 400}

    read_tool = StructuredTool(
        name="read_file",
        description="Read a file.",
        args_schema=_PathArguments,
        coroutine=read_file,
    )
    model = ScriptedRunnable(
        [
            AIMessage(
                content="",
                tool_calls=[{"name": "read_file", "args": {"path": "first.py"}, "id": "read-1"}],
            ),
            AIMessage(
                content="",
                tool_calls=[{"name": "read_file", "args": {"path": "second.py"}, "id": "read-2"}],
            ),
            AIMessage(content="两个文件都已检查。"),
        ]
    )
    context = ToolContext(workspace=tmp_path)
    services = GraphServices(
        model=model,
        tools={"read_file": read_tool},
        tool_specs={"read_file": _tool_spec("read_file")},
        tool_context=context,
        tool_invoker=ContractAwareToolInvoker(context),
        max_tool_output_chars=120,
        compact_tool_output_chars=32,
        max_context_messages=6,
    )

    result = asyncio.run(
        build_graph(services).ainvoke(
            services.initial_state("检查两个文件。"),
            {"configurable": {"thread_id": "context-trim"}},
        )
    )

    next_request = model.ainvoke_inputs[1]
    next_tool_message = next(
        message for message in next_request if isinstance(message, ToolMessage)
    )
    next_payload = json.loads(str(next_tool_message.content))
    assert "已压缩工具输出" in next_payload["content"]
    assert len(next_payload["content"]) < 100
    assert len(result["messages"]) <= 7
    assert all(
        "A" * 100
        not in json.loads(str(message.content))["content"]
        for message in result["messages"]
        if isinstance(message, ToolMessage)
    )


def test_graph_retains_the_latest_complete_tool_message_group_over_the_context_limit(
    tmp_path: Path,
) -> None:
    from insightagent.graph.workflow import GraphServices, build_graph

    async def read_file(path: str) -> dict[str, object]:
        return {"is_error": False, "content": f"contents: {path}"}

    tool = StructuredTool(
        name="read_file",
        description="Read a file.",
        args_schema=_PathArguments,
        coroutine=read_file,
    )
    tool_calls = [
        {"name": tool.name, "args": {"path": f"file-{index}.py"}, "id": f"read-{index}"}
        for index in range(24)
    ]
    model = ScriptedRunnable(
        [
            AIMessage(content="", tool_calls=tool_calls),
            AIMessage(content="检查完成。"),
        ]
    )
    context = ToolContext(workspace=tmp_path)
    services = GraphServices(
        model=model,
        tools={tool.name: tool},
        tool_specs={tool.name: _tool_spec(tool.name)},
        tool_context=context,
        tool_invoker=ContractAwareToolInvoker(context),
        max_context_messages=24,
    )

    result = asyncio.run(
        build_graph(services).ainvoke(
            services.initial_state("检查仓库。"),
            {"configurable": {"thread_id": "retain-latest-tool-group"}},
        )
    )

    next_request = model.ainvoke_inputs[1]
    retained_tool_messages = [
        message for message in next_request if isinstance(message, ToolMessage)
    ]

    assert result["phase"] == "done"
    assert isinstance(next_request[0], AIMessage)
    assert [message.tool_call_id for message in retained_tool_messages] == [
        f"read-{index}" for index in range(24)
    ]
    assert len(next_request) == 25


def test_graph_repairs_failed_verification_then_completes(tmp_path: Path) -> None:
    from insightagent.graph.workflow import GraphServices, build_graph

    source = tmp_path / "src" / "calc.py"
    source.parent.mkdir()
    source.write_text("VALUE = 0\n", encoding="utf-8")
    verification_outcomes = ["exit_code: 1\nfailed", "exit_code: 0\npassed"]

    async def read_file(path: str) -> dict[str, object]:
        return {"is_error": False, "content": (tmp_path / path).read_text(encoding="utf-8")}

    async def edit_file(path: str, old: str, new: str) -> dict[str, object]:
        target = tmp_path / path
        target.write_text(target.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
        return {"is_error": False, "content": "edited"}

    async def run_verification(command: str) -> dict[str, object]:
        assert command == "python -m pytest tests/test_calc.py -q"
        return {"is_error": False, "content": verification_outcomes.pop(0)}

    tools: list[BaseTool] = [
        StructuredTool(
            name="read_file",
            description="Read a file.",
            args_schema=_PathArguments,
            coroutine=read_file,
        ),
        StructuredTool(
            name="edit_file",
            description="Edit a file.",
            args_schema=_EditArguments,
            coroutine=edit_file,
        ),
        StructuredTool(
            name="run_verification",
            description="Run verification.",
            args_schema=_VerificationArguments,
            coroutine=run_verification,
        ),
    ]
    model = ScriptedRunnable(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {"path": "src/calc.py"}, "id": "read-1"}
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "edit_file",
                        "args": {"path": "src/calc.py", "old": "VALUE = 0", "new": "VALUE = 1"},
                        "id": "edit-1",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "run_verification",
                        "args": {"command": "python -m pytest tests/test_calc.py -q"},
                        "id": "verify-1",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "edit_file",
                        "args": {"path": "src/calc.py", "old": "VALUE = 1", "new": "VALUE = 2"},
                        "id": "edit-2",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "run_verification",
                        "args": {"command": "python -m pytest tests/test_calc.py -q"},
                        "id": "verify-2",
                    }
                ],
            ),
            AIMessage(content="修复完成，验证已通过。"),
        ]
    )
    context = ToolContext(workspace=tmp_path)
    services = GraphServices(
        model=model,
        tools={tool.name: tool for tool in tools},
        tool_specs={
            "read_file": _tool_spec("read_file"),
            "edit_file": _tool_spec("edit_file", mutates=True),
            "run_verification": _tool_spec("run_verification", executes=True),
        },
        tool_context=context,
        tool_invoker=ContractAwareToolInvoker(context),
        max_iterations=8,
        max_repair_attempts=2,
    )
    graph = build_graph(services)

    result = asyncio.run(
        graph.ainvoke(
            services.initial_state(
                "修复 src/calc.py。Run this exact verification command before finalizing: "
                "python -m pytest tests/test_calc.py -q"
            ),
            {"configurable": {"thread_id": "repair-1"}},
        )
    )

    assert result["phase"] == "done"
    assert result["phase_history"] == [
        "plan",
        "inspect",
        "implement",
        "verify",
        "repair",
        "verify",
        "summarize",
        "done",
    ]
    assert result["verification_attempts"][-1]["exit_code"] == 0
    assert result["changed_files"] == ["src/calc.py"]
    assert result["final_answer"] == "修复完成，验证已通过。"
    assert source.read_text(encoding="utf-8") == "VALUE = 2\n"
    assert model.ainvoke_configs
    assert all(event["tool"] in {"read_file", "edit_file", "run_verification"} for event in result["tool_events"])


def test_graph_repair_allows_an_edit_after_reading_the_latest_verification_failure(
    tmp_path: Path,
) -> None:
    from insightagent.graph.workflow import GraphServices, build_graph

    source = tmp_path / "src" / "calc.py"
    source.parent.mkdir()
    source.write_text("VALUE = 0\n", encoding="utf-8")
    test_file = tmp_path / "tests" / "test_calc.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_calc():\n    assert True\n", encoding="utf-8")
    verification_outcomes = ["exit_code: 1\nfailed", "exit_code: 0\npassed"]

    async def read_file(path: str) -> dict[str, object]:
        return {"is_error": False, "content": (tmp_path / path).read_text(encoding="utf-8")}

    async def edit_file(path: str, old: str, new: str) -> dict[str, object]:
        target = tmp_path / path
        target.write_text(target.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
        return {"is_error": False, "content": "edited"}

    async def run_verification(command: str) -> dict[str, object]:
        assert command == "python -m pytest tests/test_calc.py -q"
        return {"is_error": False, "content": verification_outcomes.pop(0)}

    tools: list[BaseTool] = [
        StructuredTool(
            name="read_file",
            description="Read a file.",
            args_schema=_PathArguments,
            coroutine=read_file,
        ),
        StructuredTool(
            name="edit_file",
            description="Edit a file.",
            args_schema=_EditArguments,
            coroutine=edit_file,
        ),
        StructuredTool(
            name="run_verification",
            description="Run verification.",
            args_schema=_VerificationArguments,
            coroutine=run_verification,
        ),
    ]
    model = ScriptedRunnable(
        [
            AIMessage(
                content="",
                tool_calls=[{"name": "read_file", "args": {"path": "src/calc.py"}, "id": "read-1"}],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {"path": "tests/test_calc.py"}, "id": "read-test"}
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "edit_file",
                        "args": {"path": "src/calc.py", "old": "VALUE = 0", "new": "VALUE = 1"},
                        "id": "edit-1",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "run_verification",
                        "args": {"command": "python -m pytest tests/test_calc.py -q"},
                        "id": "verify-1",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[{"name": "read_file", "args": {"path": "src/calc.py"}, "id": "read-2"}],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "edit_file",
                        "args": {"path": "src/calc.py", "old": "VALUE = 1", "new": "VALUE = 2"},
                        "id": "edit-2",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "run_verification",
                        "args": {"command": "python -m pytest tests/test_calc.py -q"},
                        "id": "verify-2",
                    }
                ],
            ),
            AIMessage(content="修复完成，验证已通过。"),
        ]
    )
    context = ToolContext(workspace=tmp_path)
    services = GraphServices(
        model=model,
        tools={tool.name: tool for tool in tools},
        tool_specs={
            "read_file": _tool_spec("read_file"),
            "edit_file": _tool_spec("edit_file", mutates=True),
            "run_verification": _tool_spec("run_verification", executes=True),
        },
        tool_context=context,
        tool_invoker=ContractAwareToolInvoker(context),
        max_repair_attempts=3,
    )

    result = asyncio.run(
        build_graph(services).ainvoke(
            services.initial_state(
                "SWE-bench repository repair task. Fix src/calc.py. "
                "Run this exact verification command before finalizing: "
                "python -m pytest tests/test_calc.py -q"
            ),
            {"configurable": {"thread_id": "repair-after-failure"}},
        )
    )

    assert result["phase"] == "done"
    assert [attempt["exit_code"] for attempt in result["verification_attempts"]] == [1, 0]
    assert source.read_text(encoding="utf-8") == "VALUE = 2\n"


def test_graph_rejects_prose_only_response_while_repository_action_is_required(tmp_path: Path) -> None:
    from insightagent.graph.workflow import GraphServices, build_graph

    context = ToolContext(workspace=tmp_path)
    services = GraphServices(
        model=ScriptedRunnable(["我已经修复完成"]),
        tools={},
        tool_specs={},
        tool_context=context,
        tool_invoker=ContractAwareToolInvoker(context),
        max_iterations=2,
        max_repair_attempts=1,
    )
    graph = build_graph(services)

    result = asyncio.run(
        graph.ainvoke(
            services.initial_state("SWE-bench repository repair task. 修复已有仓库。"),
            {"configurable": {"thread_id": "nudge-1"}},
        )
    )

    assert result["phase"] == "failed"
    assert result["tool_events"][-1]["failure_kind"] == "action_required"


def test_graph_requires_successful_exact_verification_before_summary(tmp_path: Path) -> None:
    from insightagent.graph.workflow import GraphServices, build_graph

    source = tmp_path / "src" / "calc.py"
    source.parent.mkdir()
    source.write_text("VALUE = 0\n", encoding="utf-8")

    async def edit_file(path: str, old: str, new: str) -> dict[str, object]:
        target = tmp_path / path
        target.write_text(target.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
        return {"is_error": False, "content": "edited"}

    edit_tool = StructuredTool(
        name="edit_file",
        description="Edit a file.",
        args_schema=_EditArguments,
        coroutine=edit_file,
    )
    context = ToolContext(workspace=tmp_path)
    services = GraphServices(
        model=ScriptedRunnable(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "edit_file",
                            "args": {
                                "path": "src/calc.py",
                                "old": "VALUE = 0",
                                "new": "VALUE = 1",
                            },
                            "id": "edit-1",
                        }
                    ],
                ),
                AIMessage(content="修改已完成。"),
            ]
        ),
        tools={"edit_file": edit_tool},
        tool_specs={"edit_file": _tool_spec("edit_file", mutates=True)},
        tool_context=context,
        tool_invoker=ContractAwareToolInvoker(context),
    )

    result = asyncio.run(
        build_graph(services).ainvoke(
            services.initial_state(
                "修复 src/calc.py。Run this exact verification command before finalizing: "
                "python -m pytest tests/test_calc.py -q"
            ),
            {"configurable": {"thread_id": "verification-required"}},
        )
    )

    assert result["phase"] == "failed"
    assert result["tool_events"][-1]["failure_kind"] == "action_required"


def test_graph_rejects_verification_before_workspace_change(tmp_path: Path) -> None:
    from insightagent.graph.workflow import GraphServices, build_graph

    invoked: list[str] = []

    async def run_verification(command: str) -> dict[str, object]:
        invoked.append(command)
        return {"is_error": False, "content": "exit_code: 0\npassed"}

    verification_tool = StructuredTool(
        name="run_verification",
        description="Run verification.",
        args_schema=_VerificationArguments,
        coroutine=run_verification,
    )
    context = ToolContext(workspace=tmp_path)
    services = GraphServices(
        model=ScriptedRunnable(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "run_verification",
                            "args": {"command": "python -m pytest tests/test_calc.py -q"},
                            "id": "verify-1",
                        }
                    ],
                ),
                AIMessage(content="验证通过。"),
            ]
        ),
        tools={"run_verification": verification_tool},
        tool_specs={"run_verification": _tool_spec("run_verification", executes=True)},
        tool_context=context,
        tool_invoker=ContractAwareToolInvoker(context),
    )

    result = asyncio.run(
        build_graph(services).ainvoke(
            services.initial_state("修复 src/calc.py。"),
            {"configurable": {"thread_id": "verify-before-change"}},
        )
    )

    assert invoked == []
    assert result["phase"] == "failed"
    assert result["tool_events"][-1]["failure_kind"] == "action_required"


def test_graph_rejects_summary_after_an_unresolved_tool_error(tmp_path: Path) -> None:
    from insightagent.graph.workflow import GraphServices, build_graph

    async def failed_read(path: str) -> dict[str, object]:
        return {"is_error": True, "content": f"missing: {path}", "failure_kind": "not_found"}

    read_tool = StructuredTool(
        name="read_file",
        description="Read a file.",
        args_schema=_PathArguments,
        coroutine=failed_read,
    )
    context = ToolContext(workspace=tmp_path)
    services = GraphServices(
        model=ScriptedRunnable(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "read_file", "args": {"path": "missing.py"}, "id": "read-1"}
                    ],
                ),
                AIMessage(content="任务已经完成。"),
            ]
        ),
        tools={"read_file": read_tool},
        tool_specs={"read_file": _tool_spec("read_file")},
        tool_context=context,
        tool_invoker=ContractAwareToolInvoker(context),
        max_repair_attempts=1,
    )

    result = asyncio.run(
        build_graph(services).ainvoke(
            services.initial_state("读取缺失文件后给出解决方案。"),
            {"configurable": {"thread_id": "unresolved-tool-error"}},
        )
    )

    assert result["phase"] == "failed"
    assert result["tool_events"][-1]["failure_kind"] == "action_required"


def test_graph_does_not_call_model_after_repair_budget_is_exhausted(tmp_path: Path) -> None:
    from insightagent.graph.workflow import GraphServices, build_graph

    async def failed_read(path: str) -> dict[str, object]:
        return {"is_error": True, "content": f"missing: {path}", "failure_kind": "not_found"}

    read_tool = StructuredTool(
        name="read_file",
        description="Read a file.",
        args_schema=_PathArguments,
        coroutine=failed_read,
    )
    model = ScriptedRunnable(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {"path": "missing.py"}, "id": "read-1"}
                ],
            )
        ]
    )
    context = ToolContext(workspace=tmp_path)
    services = GraphServices(
        model=model,
        tools={"read_file": read_tool},
        tool_specs={"read_file": _tool_spec("read_file")},
        tool_context=context,
        tool_invoker=ContractAwareToolInvoker(context),
        max_repair_attempts=0,
    )

    result = asyncio.run(
        build_graph(services).ainvoke(
            services.initial_state("读取缺失文件。"),
            {"configurable": {"thread_id": "repair-budget-exhausted"}},
        )
    )

    assert result["phase"] == "failed"
    assert result["tool_events"][-1]["failure_kind"] == "repair_limit"
    assert len(model.ainvoke_configs) == 1


def test_graph_requires_reverification_after_a_later_workspace_change(tmp_path: Path) -> None:
    from insightagent.graph.workflow import GraphServices, build_graph

    source = tmp_path / "src" / "calc.py"
    source.parent.mkdir()
    source.write_text("VALUE = 0\n", encoding="utf-8")

    async def edit_file(path: str, old: str, new: str) -> dict[str, object]:
        target = tmp_path / path
        target.write_text(target.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
        return {"is_error": False, "content": "edited"}

    async def run_verification(command: str) -> dict[str, object]:
        assert command == "python -m pytest tests/test_calc.py -q"
        return {"is_error": False, "content": "exit_code: 0\npassed"}

    edit_tool = StructuredTool(
        name="edit_file",
        description="Edit a file.",
        args_schema=_EditArguments,
        coroutine=edit_file,
    )
    verification_tool = StructuredTool(
        name="run_verification",
        description="Run verification.",
        args_schema=_VerificationArguments,
        coroutine=run_verification,
    )
    context = ToolContext(workspace=tmp_path)
    services = GraphServices(
        model=ScriptedRunnable(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "edit_file",
                            "args": {
                                "path": "src/calc.py",
                                "old": "VALUE = 0",
                                "new": "VALUE = 1",
                            },
                            "id": "edit-1",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "run_verification",
                            "args": {"command": "python -m pytest tests/test_calc.py -q"},
                            "id": "verify-1",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "edit_file",
                            "args": {
                                "path": "src/calc.py",
                                "old": "VALUE = 1",
                                "new": "VALUE = 2",
                            },
                            "id": "edit-2",
                        }
                    ],
                ),
                AIMessage(content="最后一次修改也已完成。"),
            ]
        ),
        tools={"edit_file": edit_tool, "run_verification": verification_tool},
        tool_specs={
            "edit_file": _tool_spec("edit_file", mutates=True),
            "run_verification": _tool_spec("run_verification", executes=True),
        },
        tool_context=context,
        tool_invoker=ContractAwareToolInvoker(context),
    )

    result = asyncio.run(
        build_graph(services).ainvoke(
            services.initial_state(
                "修复 src/calc.py。Run this exact verification command before finalizing: "
                "python -m pytest tests/test_calc.py -q"
            ),
            {"configurable": {"thread_id": "reverification-required"}},
        )
    )

    assert result["phase"] == "failed"
    assert result["tool_events"][-1]["failure_kind"] == "action_required"


def test_graph_rejects_verification_without_an_explicit_exit_code(tmp_path: Path) -> None:
    from insightagent.graph.workflow import GraphServices, build_graph

    source = tmp_path / "src" / "calc.py"
    source.parent.mkdir()
    source.write_text("VALUE = 0\n", encoding="utf-8")

    async def edit_file(path: str, old: str, new: str) -> dict[str, object]:
        target = tmp_path / path
        target.write_text(target.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
        return {"is_error": False, "content": "edited"}

    async def run_verification(command: str) -> dict[str, object]:
        assert command == "python -m pytest tests/test_calc.py -q"
        return {"is_error": False, "content": "passed"}

    edit_tool = StructuredTool(
        name="edit_file",
        description="Edit a file.",
        args_schema=_EditArguments,
        coroutine=edit_file,
    )
    verification_tool = StructuredTool(
        name="run_verification",
        description="Run verification.",
        args_schema=_VerificationArguments,
        coroutine=run_verification,
    )
    context = ToolContext(workspace=tmp_path)
    services = GraphServices(
        model=ScriptedRunnable(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "edit_file",
                            "args": {
                                "path": "src/calc.py",
                                "old": "VALUE = 0",
                                "new": "VALUE = 1",
                            },
                            "id": "edit-1",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "run_verification",
                            "args": {"command": "python -m pytest tests/test_calc.py -q"},
                            "id": "verify-1",
                        }
                    ],
                ),
                AIMessage(content="验证通过。"),
            ]
        ),
        tools={"edit_file": edit_tool, "run_verification": verification_tool},
        tool_specs={
            "edit_file": _tool_spec("edit_file", mutates=True),
            "run_verification": _tool_spec("run_verification", executes=True),
        },
        tool_context=context,
        tool_invoker=ContractAwareToolInvoker(context),
    )

    result = asyncio.run(
        build_graph(services).ainvoke(
            services.initial_state(
                "修复 src/calc.py。Run this exact verification command before finalizing: "
                "python -m pytest tests/test_calc.py -q"
            ),
            {"configurable": {"thread_id": "explicit-exit-code"}},
        )
    )

    assert result["phase"] == "failed"
    assert result["verification_attempts"][-1]["exit_code"] != 0


def test_graph_skips_remaining_tool_calls_after_a_batch_error(tmp_path: Path) -> None:
    from insightagent.graph.workflow import GraphServices, build_graph

    executed_paths: list[str] = []

    async def read_file(path: str) -> dict[str, object]:
        executed_paths.append(path)
        if path == "missing.py":
            return {"is_error": True, "content": "missing: missing.py", "failure_kind": "not_found"}
        return {"is_error": False, "content": "unexpected second read"}

    read_tool = StructuredTool(
        name="read_file",
        description="Read a file.",
        args_schema=_PathArguments,
        coroutine=read_file,
    )
    context = ToolContext(workspace=tmp_path)
    services = GraphServices(
        model=ScriptedRunnable(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "read_file", "args": {"path": "missing.py"}, "id": "read-1"},
                        {"name": "read_file", "args": {"path": "other.py"}, "id": "read-2"},
                    ],
                ),
                AIMessage(content="任务已经完成。"),
            ]
        ),
        tools={"read_file": read_tool},
        tool_specs={"read_file": _tool_spec("read_file")},
        tool_context=context,
        tool_invoker=ContractAwareToolInvoker(context),
        max_repair_attempts=1,
    )

    result = asyncio.run(
        build_graph(services).ainvoke(
            services.initial_state("读取两个文件。"),
            {"configurable": {"thread_id": "tool-batch-error"}},
        )
    )

    assert executed_paths == ["missing.py"]
    assert result["phase"] == "failed"
    assert result["tool_events"][-1]["failure_kind"] == "action_required"
    assert result["tool_events"][-2]["failure_kind"] == "tool_batch_aborted"


def test_graph_rejects_summary_after_an_unresolved_error_and_later_read_success(
    tmp_path: Path,
) -> None:
    from insightagent.graph.workflow import GraphServices, build_graph

    async def read_file(path: str) -> dict[str, object]:
        if path == "missing.py":
            return {"is_error": True, "content": "missing: missing.py", "failure_kind": "not_found"}
        return {"is_error": False, "content": "existing source"}

    read_tool = StructuredTool(
        name="read_file",
        description="Read a file.",
        args_schema=_PathArguments,
        coroutine=read_file,
    )
    context = ToolContext(workspace=tmp_path)
    services = GraphServices(
        model=ScriptedRunnable(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "read_file", "args": {"path": "missing.py"}, "id": "read-1"}
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "read_file", "args": {"path": "existing.py"}, "id": "read-2"}
                    ],
                ),
                AIMessage(content="任务已经完成。"),
            ]
        ),
        tools={"read_file": read_tool},
        tool_specs={"read_file": _tool_spec("read_file")},
        tool_context=context,
        tool_invoker=ContractAwareToolInvoker(context),
        max_repair_attempts=2,
    )

    result = asyncio.run(
        build_graph(services).ainvoke(
            services.initial_state("读取仓库文件。"),
            {"configurable": {"thread_id": "unresolved-read-error"}},
        )
    )

    assert result["phase"] == "failed"
    assert result["tool_events"][-1]["failure_kind"] == "action_required"
