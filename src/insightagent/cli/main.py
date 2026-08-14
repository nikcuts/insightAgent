"""Interactive CLI backed by one long-lived LangGraph runner."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from insightagent.config import RuntimeConfig, load_dotenv_files, load_runtime_config
from insightagent.graph.models import ModelConfigurationError
from insightagent.graph.runner import GraphRunner
from insightagent.mcp.config import load_mcp_config
from insightagent.mcp.errors import MCPConfigError

from .slash_commands import GraphSlashCommandProcessor
from .tool_profiles import (
    TOOL_PROFILE_CHOICES,
    parse_name_list,
    resolve_mcp_server_names,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the InsightAgent LangGraph interactive CLI.")
    parser.add_argument("--provider", choices=["siliconflow", "openai", "anthropic"])
    parser.add_argument("--model", help="Override MODEL_ID for this invocation.")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--session-id")
    parser.add_argument("--session-dir")
    parser.add_argument("--permission-mode", choices=["read-only", "workspace-write"])
    parser.add_argument("--approval-mode", choices=["deny", "interrupt"])
    parser.add_argument("--execution-mode", choices=["host", "sandbox"])
    parser.add_argument("--sandbox-image")
    parser.add_argument("--language")
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--max-tool-iterations", type=int)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--max-wall-seconds", type=float)
    parser.add_argument("--config-home")
    parser.add_argument("--tool-profile", default="coding-basic", choices=TOOL_PROFILE_CHOICES)
    parser.add_argument("--allowed-tools", action="append")
    parser.add_argument("--enable-mcp-server", action="append")
    parser.add_argument(
        "--trust-workspace-mcp",
        action="store_true",
        help="Allow workspace/start-directory MCP config files to launch tools.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(_run_repl(args))
    except (MCPConfigError, ModelConfigurationError, OSError, TypeError, ValueError) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        raise SystemExit(2) from error


async def _run_repl(args: argparse.Namespace) -> None:
    workspace = Path(args.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    start_dir = Path.cwd()
    load_dotenv_files(workspace, start_dir=start_dir)
    config = _load_config(args, workspace)
    session_dir = _session_dir(config, workspace)
    mcp_config = load_mcp_config(
        workspace,
        user_config_home=args.config_home,
        start_dir=start_dir,
        allow_workspace_config=args.trust_workspace_mcp,
    )
    async with GraphRunner(
        config=config,
        workspace=workspace,
        session_dir=session_dir,
        tool_profile=args.tool_profile,
        allowed_tools=parse_name_list(args.allowed_tools),
        enabled_mcp_servers=resolve_mcp_server_names(
            args.tool_profile, args.enable_mcp_server
        ),
        trace_jsonl=None,
        no_trace=False,
        mcp_config=mcp_config,
    ) as runner:
        thread_id = args.session_id or uuid.uuid4().hex
        slash = GraphSlashCommandProcessor(runner, thread_id=thread_id, workspace=workspace)
        print(f"InsightAgent session={slash.thread_id}. Type 'exit' or 'quit' to stop. Try /help.")
        while True:
            try:
                user_input = input("\nuser> ").strip()
            except EOFError:
                print()
                return
            if user_input.lower() in {"exit", "quit"}:
                return
            if not user_input:
                continue
            if user_input.startswith("/"):
                print(f"\n{await slash.handle(user_input)}")
                continue
            outcome = await runner.run_turn(user_input, session_id=slash.thread_id)
            while (approval := _interrupt_payload(outcome.state)) is not None:
                print("\napproval required:")
                print(json.dumps(approval, ensure_ascii=False, indent=2, default=str))
                try:
                    decision = input("approve tool call? [y/N] ").strip()
                except EOFError:
                    print("approval not provided; turn remains paused", file=sys.stderr)
                    return
                outcome = await runner.resume_turn(slash.thread_id, decision)
            print(f"\nassistant> {outcome.final_answer}")


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
            "approval_mode": args.approval_mode,
            "execution_mode": args.execution_mode,
            "sandbox_image": args.sandbox_image,
            "session_dir": args.session_dir,
            "response_language": args.language,
        },
    )


def _session_dir(config: RuntimeConfig, workspace: Path) -> Path:
    if not isinstance(config.session_dir, str) or not config.session_dir.strip():
        raise ValueError("session_dir must be a non-empty string")
    directory = Path(config.session_dir).expanduser()
    return directory if directory.is_absolute() else workspace / directory


def _interrupt_payload(state: object) -> object | None:
    """Return the first durable LangGraph interrupt payload, if present."""
    values = state if isinstance(state, dict) else {}
    interrupts = values.get("__interrupt__")
    if not isinstance(interrupts, (list, tuple)) or not interrupts:
        return None
    first = interrupts[0]
    payload = getattr(first, "value", first)
    return payload if payload is not None else {"type": "approval"}


if __name__ == "__main__":
    main()
