from __future__ import annotations

import asyncio
import json
from pathlib import Path

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph
import pytest


def test_run_task_cli_delegates_to_graph_runner(monkeypatch, tmp_path: Path, capsys) -> None:
    called: dict[str, object] = {}

    def fake_run_task(**kwargs: object):
        called.update(kwargs)
        return type(
            "Outcome",
            (),
            {"final_answer": "完成", "thread_id": "t-1", "checkpoint_id": "c-1", "state": {"phase": "done"}},
        )()

    monkeypatch.setattr("insightagent.cli.run_task.run_task", fake_run_task)
    monkeypatch.setattr(
        "sys.argv",
        [
            "insightagent-run",
            "--no-trace",
            "--workspace",
            str(tmp_path),
            "--task",
            "创建 a.py",
        ],
    )
    from insightagent.cli.run_task import main

    main()

    assert called["task"] == "创建 a.py"
    assert capsys.readouterr().out.strip() == "完成"


def test_production_cli_does_not_import_code_agent() -> None:
    import insightagent.cli.run_task as run_task

    assert "CodeAgent" not in run_task.__dict__


@pytest.mark.parametrize("list_sessions", [False, True])
def test_cli_reports_invalid_runtime_config_as_configuration_error(
    monkeypatch, tmp_path: Path, capsys, list_sessions: bool
) -> None:
    config_dir = tmp_path / ".insightagent"
    config_dir.mkdir()
    (config_dir / "config.json").write_text("{not valid json", encoding="utf-8")
    arguments = ["insightagent-run", "--workspace", str(tmp_path)]
    if list_sessions:
        arguments.append("--list-sessions")
    monkeypatch.setattr("sys.argv", arguments)

    from insightagent.cli.run_task import main

    with pytest.raises(SystemExit) as error:
        main()

    assert error.value.code == 2
    assert "Configuration error:" in capsys.readouterr().err


def test_cli_passes_config_home_mcp_configuration_to_graph_runner(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    (config_home / "mcp_config.json").write_text(
        json.dumps({"mcpServers": {"demo": {"command": "demo-mcp"}}}), encoding="utf-8"
    )
    called: dict[str, object] = {}

    def fake_run_task(**kwargs: object):
        called.update(kwargs)
        return type("Outcome", (), {"final_answer": "完成", "state": {"phase": "done"}})()

    monkeypatch.setattr("insightagent.cli.run_task.run_task", fake_run_task)
    monkeypatch.setattr(
        "sys.argv",
        [
            "insightagent-run",
            "--workspace",
            str(tmp_path),
            "--config-home",
            str(config_home),
            "--enable-mcp-server",
            "demo",
        ],
    )

    from insightagent.cli.run_task import main

    main()

    mcp_config = called["mcp_config"]
    assert "demo" in mcp_config.servers  # type: ignore[union-attr]
    assert capsys.readouterr().out.strip() == "完成"


def test_cli_maps_mcp_startup_failure_to_configuration_exit_code(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    from insightagent.mcp.errors import MCPStartupError

    monkeypatch.setattr(
        "insightagent.cli.run_task.run_task",
        lambda **_kwargs: (_ for _ in ()).throw(MCPStartupError(["broken-server"])),
    )
    monkeypatch.setattr("sys.argv", ["insightagent-run", "--workspace", str(tmp_path)])

    from insightagent.cli.run_task import main

    with pytest.raises(SystemExit) as error:
        main()

    assert error.value.code == 2
    assert "broken-server" in capsys.readouterr().err


def test_cli_checkpoint_inspection_does_not_load_mcp_configuration(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    (tmp_path / "mcp_config.json").write_text("{not valid json", encoding="utf-8")
    called: dict[str, object] = {}

    def fake_run_task(**kwargs: object):
        called.update(kwargs)
        return type(
            "Outcome",
            (),
            {
                "final_answer": "历史结果",
                "thread_id": "thread-1",
                "state": {"phase": "done"},
            },
        )()

    monkeypatch.setattr("insightagent.cli.run_task.run_task", fake_run_task)
    monkeypatch.setattr(
        "sys.argv",
        [
            "insightagent-run",
            "--workspace",
            str(tmp_path),
            "--session-id",
            "thread-1",
            "--checkpoint-id",
            "checkpoint-1",
        ],
    )

    from insightagent.cli.run_task import main

    main()

    assert "mcp_config" not in called
    assert capsys.readouterr().out.strip() == "历史结果"


def test_export_transcript_reads_the_requested_checkpoint(tmp_path: Path) -> None:
    from insightagent.graph.checkpoints import open_checkpointer
    from insightagent.graph.sessions import GraphSessionService
    from insightagent.graph.state import AgentState

    def finish_turn(state: AgentState) -> AgentState:
        del state
        return {"messages": [AIMessage(content="完成")], "phase": "done"}

    async def scenario() -> None:
        store = await open_checkpointer(tmp_path / "sessions")
        try:
            workflow = StateGraph(AgentState)
            workflow.add_node("finish_turn", finish_turn)
            workflow.add_edge(START, "finish_turn")
            workflow.add_edge("finish_turn", END)
            sessions = GraphSessionService(workflow.compile(checkpointer=store.checkpointer), store)
            first = await sessions.start_turn("thread-1", tmp_path, "第一轮", deadline_monotonic=None)
            await sessions.start_turn("thread-1", tmp_path, "第二轮", deadline_monotonic=None)
            assert first.checkpoint_id is not None

            from insightagent.cli.run_task import _export_transcript

            destination = tmp_path / "first.md"
            await _export_transcript(
                tmp_path / "sessions", "thread-1", destination, checkpoint_id=first.checkpoint_id
            )
            transcript = destination.read_text(encoding="utf-8")
            assert "第一轮" in transcript
            assert "第二轮" not in transcript
        finally:
            await store.close()

    asyncio.run(scenario())
