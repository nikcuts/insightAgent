from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
import pytest

from insightagent.config import RuntimeConfig
from tests.graph.fakes import ScriptedRunnable


class _RecordingSpan:
    def __init__(self, client: "_RecordingLangfuseClient", name: str) -> None:
        self._client = client
        self._name = name

    def update(self, **payload: object) -> None:
        self._client.updates.append((self._name, payload))


class _RecordingLangfuseClient:
    def __init__(self) -> None:
        self.updates: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    @contextmanager
    def start_as_current_observation(self, *, as_type: str, name: str) -> Iterator[_RecordingSpan]:
        del as_type
        yield _RecordingSpan(self, name)

    def get_current_trace_id(self) -> str:
        return "lifecycle-trace"

    def flush(self) -> None:
        return None

    def shutdown(self) -> None:
        self.closed = True


class _LifecycleMCPManager:
    instances: list["_LifecycleMCPManager"] = []

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.start_trace: Any = None
        self.stop_trace: Any = None
        type(self).instances.append(self)

    async def start_enabled(self, trace: Any = None) -> int:
        self.start_trace = trace
        assert trace is not None
        trace(
            {
                "type": "mcp_server_starting",
                "authorization": "Basic ZGVtbzpwYXNzd29yZA==",
                "token": "sk-lifecycle-secret-1234567890",
            }
        )
        return 0

    async def get_tools(self) -> list[object]:
        return []

    async def get_tool_specs(self) -> dict[str, object]:
        return {}

    async def stop_all(self, trace: Any = None) -> None:
        self.stop_trace = trace
        assert trace is not None
        trace(
            {
                "type": "mcp_server_stopped",
                "authorization": "Digest username=demo, response=super-secret",
            }
        )


class _FailingMCPManager:
    instances: list["_FailingMCPManager"] = []

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.failed = {"broken-server": "Authorization: Basic secret-value"}
        self.stopped = False
        type(self).instances.append(self)

    async def start_enabled(self, trace: Any = None) -> int:
        assert trace is not None
        trace({"type": "mcp_server_failed", "server": "broken-server"})
        return 0

    async def stop_all(self, trace: Any = None) -> None:
        self.stopped = True
        if trace is not None:
            trace({"type": "mcp_server_stopped", "server": "broken-server"})


def test_manual_compaction_preserves_a_complete_tool_call_message_group() -> None:
    from insightagent.graph.state import trim_message_prefix

    oldest = HumanMessage(id="oldest", content="old context")
    request = AIMessage(
        id="request-1",
        content="",
        tool_calls=[{"name": "read_file", "args": {"path": "a.py"}, "id": "call-1"}],
    )
    response = ToolMessage(id="response-1", content="contents", tool_call_id="call-1")
    trailing = [HumanMessage(id=f"after-{index}", content=str(index)) for index in range(19)]

    removals, retained = trim_message_prefix([oldest, request, response, *trailing], 20)

    assert [message.id for message in removals] == ["oldest"]
    assert retained[0] is request
    assert [message.tool_call_id for message in retained if isinstance(message, ToolMessage)] == [
        "call-1"
    ]
    assert len(retained) == 21


def test_graph_runner_persists_a_turn_and_returns_checkpoint(
    monkeypatch, tmp_path: Path
) -> None:
    from insightagent.graph.runner import GraphRunner
    from insightagent.graph.observability import GraphObservability

    model = ScriptedRunnable([AIMessage(content="分析完成。")])
    monkeypatch.setattr("insightagent.graph.runner.build_chat_model", lambda _config, _tools: model)
    monkeypatch.setattr(
        "insightagent.graph.runner.build_observability",
        lambda **_kwargs: GraphObservability(
            callbacks=[],
            enabled=False,
            _metadata={"langfuse_session_id": "thread-1"},
        ),
    )

    async def scenario() -> None:
        runner = GraphRunner(
            config=RuntimeConfig(),
            workspace=tmp_path,
            session_dir=tmp_path / "sessions",
            tool_profile="analysis",
            allowed_tools=None,
            enabled_mcp_servers=set(),
            trace_jsonl=None,
            no_trace=True,
        )
        async with runner:
            outcome = await runner.run_turn("分析仓库", session_id="thread-1")
            assert outcome.thread_id == "thread-1"
            assert outcome.checkpoint_id is not None
            assert outcome.state.get("phase") == "done"
            assert outcome.final_answer == "分析完成。"
            assert await runner.list_sessions() == ["thread-1"]
            model_config = model.ainvoke_configs[0]
            assert model_config is not None
            assert model_config.get("callbacks") is not None
            metadata = model_config.get("metadata")
            assert isinstance(metadata, dict)
            assert metadata["langfuse_session_id"] == "thread-1"

    asyncio.run(scenario())


def test_graph_runner_rejects_selected_mcp_servers_that_fail_to_start(
    monkeypatch, tmp_path: Path
) -> None:
    from insightagent.graph.observability import GraphObservability
    from insightagent.graph.runner import GraphRunner
    from insightagent.mcp.config import MCPConfig, MCPServerConfig
    from insightagent.mcp.errors import MCPStartupError

    monkeypatch.setattr("insightagent.graph.runner.MCPManager", _FailingMCPManager)
    monkeypatch.setattr(
        "insightagent.graph.runner.build_chat_model",
        lambda _config, _tools: (_ for _ in ()).throw(AssertionError("不应构造模型")),
    )
    monkeypatch.setattr(
        "insightagent.graph.runner.build_observability",
        lambda **_kwargs: GraphObservability(callbacks=[], enabled=False),
    )
    _FailingMCPManager.instances.clear()

    async def scenario() -> None:
        runner = GraphRunner(
            config=RuntimeConfig(),
            workspace=tmp_path,
            session_dir=tmp_path / "sessions",
            tool_profile="analysis",
            allowed_tools=None,
            enabled_mcp_servers={"broken-server"},
            trace_jsonl=None,
            no_trace=True,
            mcp_config=MCPConfig(
                servers={"broken-server": MCPServerConfig(name="broken-server", command="demo")}
            ),
        )
        with pytest.raises(MCPStartupError, match="broken-server") as error:
            await runner.__aenter__()
        assert "secret-value" not in str(error.value)
        assert _FailingMCPManager.instances[0].stopped is True

    asyncio.run(scenario())


def test_graph_runner_compacts_checkpointed_history(monkeypatch, tmp_path: Path) -> None:
    from insightagent.graph.observability import GraphObservability
    from insightagent.graph.runner import GraphRunner

    model = ScriptedRunnable([AIMessage(content=f"第 {index} 轮完成。") for index in range(6)])
    monkeypatch.setattr("insightagent.graph.runner.build_chat_model", lambda _config, _tools: model)
    monkeypatch.setattr(
        "insightagent.graph.runner.build_observability",
        lambda **_kwargs: GraphObservability(callbacks=[], enabled=False),
    )

    async def scenario() -> None:
        async with GraphRunner(
            config=RuntimeConfig(),
            workspace=tmp_path,
            session_dir=tmp_path / "sessions",
            tool_profile="analysis",
            allowed_tools=None,
            enabled_mcp_servers=set(),
            trace_jsonl=None,
            no_trace=True,
        ) as runner:
            for index in range(6):
                await runner.run_turn(f"第 {index} 轮", session_id="compact-thread")
            before = await runner.latest_state("compact-thread")
            before_messages = list(before.values.get("messages", []))

            result = await runner.compact_thread("compact-thread")
            after = await runner.latest_state("compact-thread")

            assert len(before_messages) > 20
            assert result["removed_messages"] == len(before_messages) - 20
            assert len(after.values.get("messages", [])) == 20

    asyncio.run(scenario())


def test_mcp_refresh_slash_command_rebinds_the_next_graph_turn(monkeypatch, tmp_path: Path) -> None:
    from langchain_core.tools import tool

    from insightagent.cli.slash_commands import GraphSlashCommandProcessor
    from insightagent.graph.observability import GraphObservability
    from insightagent.graph.runner import GraphRunner
    from insightagent.mcp.config import MCPConfig, MCPServerConfig
    from insightagent.runtime.types import ToolPermission, ToolRisk, ToolSpec

    @tool
    def mcp_demo_old() -> str:
        """Old MCP tool."""
        old_calls.append(True)
        return "old"

    @tool
    def mcp_demo_new() -> str:
        """New MCP tool."""
        new_calls.append(True)
        return "new"

    def spec(name: str) -> ToolSpec:
        return ToolSpec(
            name=name,
            description=name,
            input_schema={},
            required_permission=ToolPermission.MCP,
            risk=ToolRisk.HIGH,
            mutates_workspace=True,
            uses_network=True,
        )

    class RefreshingMCPManager:
        instances: list["RefreshingMCPManager"] = []

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.failed: dict[str, str] = {}
            self.generation = 0
            type(self).instances.append(self)

        async def start_enabled(self, trace: Any = None) -> int:
            del trace
            return 1

        async def get_tools(self) -> list[BaseTool]:
            return [mcp_demo_old] if self.generation == 0 else [mcp_demo_new]

        async def get_tool_specs(self) -> dict[str, ToolSpec]:
            name = "mcp_demo_old" if self.generation == 0 else "mcp_demo_new"
            return {name: spec(name)}

        async def restart_server(self, _name: str, trace: Any = None) -> bool:
            self.generation += 1
            if trace is not None:
                trace({"type": "mcp_server_started", "server": "demo"})
            return True

        async def refresh_server(self, name: str, trace: Any = None) -> bool:
            return await self.restart_server(name, trace=trace)

        async def status(self) -> dict[str, dict[str, object]]:
            return {"demo": {"state": "running", "tools": 1}}

        async def stop_all(self, trace: Any = None) -> None:
            del trace

    bound_tool_names: list[set[str]] = []
    old_calls: list[bool] = []
    new_calls: list[bool] = []

    def build_model(_config: RuntimeConfig, tools: list[BaseTool]) -> ScriptedRunnable:
        names = {tool.name for tool in tools}
        bound_tool_names.append(names)
        if "mcp_demo_new" not in names:
            return ScriptedRunnable([AIMessage(content="unused")])
        return ScriptedRunnable(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "mcp_demo_new", "args": {}, "id": "new-1"}],
                ),
                AIMessage(content="新工具已执行。"),
            ]
        )

    monkeypatch.setattr("insightagent.graph.runner.MCPManager", RefreshingMCPManager)
    monkeypatch.setattr("insightagent.graph.runner.build_chat_model", build_model)
    monkeypatch.setattr(
        "insightagent.graph.runner.build_observability",
        lambda **_kwargs: GraphObservability(callbacks=[], enabled=False),
    )

    async def scenario() -> None:
        async with GraphRunner(
            config=RuntimeConfig(),
            workspace=tmp_path,
            session_dir=tmp_path / "sessions",
            tool_profile="analysis",
            allowed_tools=None,
            enabled_mcp_servers={"demo"},
            trace_jsonl=None,
            no_trace=True,
            mcp_config=MCPConfig(servers={"demo": MCPServerConfig(name="demo", command="demo")}),
        ) as runner:
            assert "mcp_demo_old" in bound_tool_names[-1]
            slash = GraphSlashCommandProcessor(runner, thread_id="refresh-1", workspace=tmp_path)
            assert "refreshed" in await slash.handle("/mcp refresh demo")
            assert "mcp_demo_new" in bound_tool_names[-1]
            assert "mcp_demo_old" not in bound_tool_names[-1]
            assert await runner.mcp_tool_names() == ["mcp_demo_new"]
            outcome = await runner.run_turn("执行刷新后的 MCP 工具", session_id="refresh-1")
            assert outcome.state["phase"] == "done"
            assert old_calls == []
            assert new_calls == [True]

    asyncio.run(scenario())


def test_graph_runner_no_trace_only_disables_console_event_rendering(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    from insightagent.graph.observability import GraphObservability
    from insightagent.graph.runner import GraphRunner

    models = [ScriptedRunnable([AIMessage(content="可见。")]), ScriptedRunnable([AIMessage(content="静默。")])]
    monkeypatch.setattr(
        "insightagent.graph.runner.build_chat_model", lambda _config, _tools: models.pop(0)
    )
    monkeypatch.setattr(
        "insightagent.graph.runner.build_observability",
        lambda **kwargs: GraphObservability(
            callbacks=[], enabled=False, event_sink=kwargs.get("event_sink")
        ),
    )

    async def run_once(*, no_trace: bool, trace_name: str) -> None:
        async with GraphRunner(
            config=RuntimeConfig(),
            workspace=tmp_path,
            session_dir=tmp_path / "sessions",
            tool_profile="analysis",
            allowed_tools=None,
            enabled_mcp_servers=set(),
            trace_jsonl=str(tmp_path / trace_name),
            no_trace=no_trace,
        ) as runner:
            await runner.run_turn("分析仓库", session_id=f"{trace_name}-thread")

    asyncio.run(run_once(no_trace=False, trace_name="visible.jsonl"))
    visible = capsys.readouterr().err
    assert "decision:phase_transition" in visible
    assert '"name": "phase_transition"' in (tmp_path / "visible.jsonl").read_text(encoding="utf-8")

    asyncio.run(run_once(no_trace=True, trace_name="silent.jsonl"))
    assert capsys.readouterr().err == ""
    assert '"name": "phase_transition"' in (tmp_path / "silent.jsonl").read_text(encoding="utf-8")


def test_graph_runner_loads_project_memory_before_building_graph(
    monkeypatch, tmp_path: Path
) -> None:
    from insightagent.graph.observability import GraphObservability
    from insightagent.graph.runner import GraphRunner

    (tmp_path / "MEMORY.md").write_text("保持现有模块边界。\n", encoding="utf-8")
    model = ScriptedRunnable([AIMessage(content="已读取项目说明。")])
    monkeypatch.setattr("insightagent.graph.runner.build_chat_model", lambda _config, _tools: model)
    monkeypatch.setattr(
        "insightagent.graph.runner.build_observability",
        lambda **_kwargs: GraphObservability(callbacks=[], enabled=False),
    )

    async def scenario() -> None:
        async with GraphRunner(
            config=RuntimeConfig(),
            workspace=tmp_path,
            session_dir=tmp_path / "sessions",
            tool_profile="analysis",
            allowed_tools=None,
            enabled_mcp_servers=set(),
            trace_jsonl=None,
            no_trace=True,
        ) as runner:
                outcome = await runner.run_turn("分析仓库", session_id="memory-thread")
                assert runner.project_memory_filenames == ("MEMORY.md",)
                assert any(
                    isinstance(message.content, str) and "保持现有模块边界。" in message.content
                    for message in outcome.state.get("messages", [])
                )

    asyncio.run(scenario())


def test_run_task_inspects_checkpoint_without_starting_model_or_mcp(
    monkeypatch, tmp_path: Path
) -> None:
    from insightagent.graph.observability import GraphObservability
    from insightagent.graph.runner import GraphRunner, run_task

    session_dir = tmp_path / "sessions"
    model = ScriptedRunnable([AIMessage(content="已完成。")])
    monkeypatch.setattr("insightagent.graph.runner.build_chat_model", lambda _config, _tools: model)
    monkeypatch.setattr(
        "insightagent.graph.runner.build_observability",
        lambda **_kwargs: GraphObservability(callbacks=[], enabled=False),
    )

    async def create_checkpoint() -> str:
        async with GraphRunner(
            config=RuntimeConfig(session_dir=str(session_dir)),
            workspace=tmp_path,
            session_dir=session_dir,
            tool_profile="analysis",
            allowed_tools=None,
            enabled_mcp_servers=set(),
            trace_jsonl=None,
            no_trace=True,
        ) as runner:
            outcome = await runner.run_turn("分析仓库", session_id="checkpoint-thread")
            assert outcome.checkpoint_id is not None
            return outcome.checkpoint_id

    checkpoint_id = asyncio.run(create_checkpoint())
    monkeypatch.setattr(
        "insightagent.graph.runner.build_chat_model",
        lambda _config, _tools: (_ for _ in ()).throw(AssertionError("不应构造模型")),
    )
    monkeypatch.setattr(
        "insightagent.graph.runner.MCPManager",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不应启动 MCP")),
    )

    outcome = run_task(
        task="不应执行",
        workspace=tmp_path,
        config=RuntimeConfig(session_dir=str(session_dir)),
        session_id="checkpoint-thread",
        checkpoint_id=checkpoint_id,
        tool_profile="analysis",
        allowed_tools=None,
        enabled_mcp_servers=set(),
        trace_jsonl=None,
        no_trace=True,
    )

    assert outcome.checkpoint_id == checkpoint_id
    assert outcome.thread_id == "checkpoint-thread"
    assert outcome.final_answer == "已完成。"
    assert outcome.state.get("task") == "分析仓库"


def test_run_task_rejects_checkpoint_without_session_id(tmp_path: Path) -> None:
    from insightagent.graph.runner import run_task

    try:
        run_task(
            task="不应执行",
            workspace=tmp_path,
            config=RuntimeConfig(session_dir=str(tmp_path / "sessions")),
            session_id=None,
            checkpoint_id="checkpoint-1",
            tool_profile="analysis",
            allowed_tools=None,
            enabled_mcp_servers=set(),
            trace_jsonl=None,
            no_trace=True,
        )
    except ValueError as error:
        assert "session_id" in str(error)
    else:
        raise AssertionError("checkpoint inspection should require session_id")


def test_graph_runner_records_sanitized_mcp_lifecycle_events(
    monkeypatch, tmp_path: Path
) -> None:
    from insightagent.graph.observability import GraphObservability
    from insightagent.graph.runner import GraphRunner

    clients: list[_RecordingLangfuseClient] = []

    def build_recording_observability(**_kwargs: object) -> GraphObservability:
        client = _RecordingLangfuseClient()
        clients.append(client)
        return GraphObservability(callbacks=[], enabled=True, _client=client)

    monkeypatch.setattr("insightagent.graph.runner.MCPManager", _LifecycleMCPManager)
    monkeypatch.setattr(
        "insightagent.graph.runner.build_chat_model",
        lambda _config, _tools: ScriptedRunnable([AIMessage(content="unused")]),
    )
    monkeypatch.setattr(
        "insightagent.graph.runner.build_observability",
        build_recording_observability,
    )
    _LifecycleMCPManager.instances.clear()

    async def scenario() -> None:
        trace_path = tmp_path / "trace.jsonl"
        runner = GraphRunner(
            config=RuntimeConfig(),
            workspace=tmp_path,
            session_dir=tmp_path / "sessions",
            tool_profile="analysis",
            allowed_tools=None,
            enabled_mcp_servers=set(),
            trace_jsonl=str(trace_path),
            no_trace=False,
        )
        async with runner:
            assert len(_LifecycleMCPManager.instances) == 1
            assert _LifecycleMCPManager.instances[0].start_trace is not None
        manager = _LifecycleMCPManager.instances[0]
        assert manager.stop_trace is not None
        assert clients[0].closed is True

        lifecycle_updates = [
            payload for name, payload in clients[0].updates if name == "decision:mcp_lifecycle"
        ]
        startup_input = lifecycle_updates[0]["input"]
        assert isinstance(startup_input, dict)
        assert startup_input["authorization"] == "***REDACTED***"
        assert startup_input["token"] == "***REDACTED***"
        trace_text = trace_path.read_text(encoding="utf-8")
        assert "super-secret" not in trace_text
        assert "sk-lifecycle-secret-1234567890" not in trace_text
        assert len([json.loads(line) for line in trace_text.splitlines()]) == 2

    asyncio.run(scenario())
