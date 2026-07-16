"""One-shot command-line entry point for the LangGraph runtime."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from insightagent.config import RuntimeConfig, load_dotenv_files, load_runtime_config
from insightagent.graph.checkpoints import open_checkpointer
from insightagent.graph.models import ModelConfigurationError
from insightagent.graph.runner import run_task
from insightagent.graph.sessions import GraphSessionService, build_checkpoint_reader
from insightagent.mcp.config import load_mcp_config
from insightagent.mcp.errors import MCPConfigError

from .tool_profiles import (
    TOOL_PROFILE_CHOICES,
    parse_name_list,
    resolve_mcp_server_names,
)


DEFAULT_TASK = "分析当前工作区并报告需要完成的下一步。"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one InsightAgent LangGraph coding turn.")
    parser.add_argument("--provider", choices=["siliconflow", "openai", "anthropic"])
    parser.add_argument("--model", help="Override MODEL_ID for this invocation.")
    parser.add_argument("--workspace", default=".", help="Target workspace directory.")
    parser.add_argument("--task", default=DEFAULT_TASK, help="User task prompt.")
    parser.add_argument("--no-trace", action="store_true", help="Disable console event rendering.")
    parser.add_argument("--timeout", type=int, help="Model request timeout in seconds.")
    parser.add_argument("--max-tool-iterations", type=int)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--max-wall-seconds", type=float)
    parser.add_argument("--trace-max-chars", type=int)
    parser.add_argument("--trace-jsonl", help="Write sanitized graph events to JSONL.")
    parser.add_argument("--tool-profile", default="coding-basic", choices=TOOL_PROFILE_CHOICES)
    parser.add_argument("--allowed-tools", action="append")
    parser.add_argument("--enable-mcp-server", action="append")
    parser.add_argument("--permission-mode", choices=["read-only", "workspace-write"])
    parser.add_argument("--language")
    parser.add_argument("--session-id")
    parser.add_argument("--checkpoint-id")
    parser.add_argument("--session-dir")
    parser.add_argument("--list-sessions", action="store_true")
    parser.add_argument("--export-transcript")
    parser.add_argument("--config-home", help="Override the user configuration home.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        _run(args)
    except (MCPConfigError, ModelConfigurationError, OSError, TypeError, ValueError) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        raise SystemExit(2) from error


def _run(args: argparse.Namespace) -> None:
    workspace = Path(args.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    start_dir = Path.cwd()
    load_dotenv_files(workspace, start_dir=start_dir)
    config = _load_config(args, workspace)
    session_dir = _session_dir(config, workspace)
    if args.list_sessions:
        for session_id in asyncio.run(_list_sessions(session_dir)):
            print(session_id)
        return
    if args.checkpoint_id is not None:
        outcome = run_task(
            task=args.task,
            workspace=workspace,
            config=config,
            session_id=args.session_id,
            checkpoint_id=args.checkpoint_id,
            tool_profile=args.tool_profile,
            allowed_tools=None,
            enabled_mcp_servers=set(),
            trace_jsonl=args.trace_jsonl,
            no_trace=args.no_trace,
        )
    else:
        mcp_config = load_mcp_config(
            workspace,
            user_config_home=args.config_home,
            start_dir=start_dir,
        )
        outcome = run_task(
            task=args.task,
            workspace=workspace,
            config=config,
            session_id=args.session_id,
            checkpoint_id=None,
            tool_profile=args.tool_profile,
            allowed_tools=parse_name_list(args.allowed_tools),
            enabled_mcp_servers=resolve_mcp_server_names(
                args.tool_profile, args.enable_mcp_server
            ),
            trace_jsonl=args.trace_jsonl,
            no_trace=args.no_trace,
            mcp_config=mcp_config,
        )
    print(outcome.final_answer)
    if args.export_transcript:
        if not outcome.thread_id:
            raise SystemExit(1)
        exported = asyncio.run(
            _export_transcript(
                session_dir,
                outcome.thread_id,
                Path(args.export_transcript),
                checkpoint_id=args.checkpoint_id,
            )
        )
        print(f"exported transcript: {exported}")
    if outcome.state.get("phase") == "failed":
        raise SystemExit(1)


def _load_config(args: argparse.Namespace, workspace: Path) -> RuntimeConfig:
    return load_runtime_config(
        workspace,
        user_config_home=args.config_home,
        overrides={
            "provider": args.provider,
            "model": args.model,
            "timeout": args.timeout,
            "max_tool_iterations": args.max_tool_iterations,
            "max_output_tokens": args.max_output_tokens,
            "max_wall_seconds": args.max_wall_seconds,
            "permission_mode": args.permission_mode,
            "trace_max_chars": args.trace_max_chars,
            "session_dir": args.session_dir,
            "response_language": args.language,
        },
    )


def _session_dir(config: RuntimeConfig, workspace: Path) -> Path:
    if not isinstance(config.session_dir, str) or not config.session_dir.strip():
        raise ValueError("session_dir must be a non-empty string")
    directory = Path(config.session_dir).expanduser()
    return directory if directory.is_absolute() else workspace / directory


async def _list_sessions(session_dir: Path) -> list[str]:
    store = await open_checkpointer(session_dir)
    try:
        return await GraphSessionService(None, store).list_threads()
    finally:
        await store.close()


async def _export_transcript(
    session_dir: Path,
    thread_id: str,
    destination: Path,
    *,
    checkpoint_id: str | None = None,
) -> Path:
    store = await open_checkpointer(session_dir)
    try:
        graph = build_checkpoint_reader(store.checkpointer)
        return await GraphSessionService(graph, store).export_markdown(
            thread_id, destination, checkpoint_id=checkpoint_id
        )
    finally:
        await store.close()


if __name__ == "__main__":
    main()
