from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from insightagent.config import RuntimeConfig
from insightagent.cli.slash_commands import GraphSlashCommandProcessor
from tests.graph.fakes import ScriptedRunnable


class _FakeGraphRunner:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.tool_profile = "analysis"
        self.permission_mode = "workspace-write"
        self.project_memory_filenames = ("MEMORY.md",)
        self.compacted_threads: list[str] = []
        self.restarted_servers: list[str] = []
        self.refreshed_servers: list[str] = []

    async def latest_state(self, _thread_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            values={
                "phase": "done",
                "iteration": 2,
                "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
            },
            config={"configurable": {"checkpoint_id": "checkpoint-1"}},
        )

    def last_trace_id(self, _thread_id: str) -> str | None:
        return "trace-1"

    async def compact_thread(self, thread_id: str) -> dict[str, int]:
        self.compacted_threads.append(thread_id)
        return {"removed_messages": 3, "summarized_tool_events": 2}

    async def export_transcript(self, _thread_id: str, destination: Path) -> Path:
        destination.write_text("# transcript\n", encoding="utf-8")
        return destination

    async def mcp_status(self) -> dict[str, dict[str, object]]:
        return {"demo": {"state": "running", "tools": 2, "last_error": ""}}

    async def mcp_tool_names(self) -> list[str]:
        return ["mcp_demo_lookup"]

    async def restart_mcp(self, server: str) -> bool:
        self.restarted_servers.append(server)
        return True

    async def refresh_mcp(self, server: str) -> bool:
        self.refreshed_servers.append(server)
        return True


def test_graph_slash_commands_read_graph_runtime_state(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner = _FakeGraphRunner(tmp_path)
        slash = GraphSlashCommandProcessor(runner, thread_id="interactive-1", workspace=tmp_path)

        assert "thread_id=interactive-1" in await slash.handle("/status")
        assert "checkpoint_id=checkpoint-1" in await slash.handle("/status")
        assert "total_tokens=18" in await slash.handle("/cost")
        assert "trace_id=trace-1" in await slash.handle("/cost")
        assert "MEMORY.md" in await slash.handle("/memory")
        assert "permission_mode=workspace-write" in await slash.handle("/permissions")

        assert "removed_messages=3" in await slash.handle("/compact")
        assert runner.compacted_threads == ["interactive-1"]
        export_path = tmp_path / "out.md"
        assert f"exported={export_path}" in await slash.handle(f"/export {export_path}")
        assert export_path.is_file()

        assert "demo: state=running" in await slash.handle("/mcp status")
        assert "mcp_demo_lookup" in await slash.handle("/mcp tools")
        assert "restarted" in await slash.handle("/mcp restart demo")
        assert "refreshed" in await slash.handle("/mcp refresh demo")
        assert runner.restarted_servers == ["demo"]
        assert runner.refreshed_servers == ["demo"]

        previous_thread_id = slash.thread_id
        assert "thread_id=" in await slash.handle("/clear")
        assert slash.thread_id != previous_thread_id
        assert "/mcp" in await slash.handle("/help")

    asyncio.run(scenario())


def test_graph_slash_commands_read_a_real_checkpointed_turn(
    monkeypatch, tmp_path: Path
) -> None:
    from insightagent.graph.observability import GraphObservability
    from insightagent.graph.runner import GraphRunner

    monkeypatch.setattr(
        "insightagent.graph.runner.build_chat_model",
        lambda _config, _tools: ScriptedRunnable(
            [
                AIMessage(
                    content="完成。",
                    usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
                )
            ]
        ),
    )
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
            await runner.run_turn("分析仓库", session_id="interactive-1")
            slash = GraphSlashCommandProcessor(runner, thread_id="interactive-1", workspace=tmp_path)

            assert "phase=done" in await slash.handle("/status")
            assert "total_tokens=5" in await slash.handle("/cost")
            destination = tmp_path / "transcript.md"
            assert f"exported={destination}" in await slash.handle(f"/export {destination}")
            assert destination.is_file()
            assert await slash.handle("/mcp status") == "MCP servers: none"

    asyncio.run(scenario())
