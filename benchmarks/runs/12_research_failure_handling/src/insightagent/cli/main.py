"""Interactive CLI for InsightAgent V5.0."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from ..config import language_directive, load_dotenv_files, load_runtime_config
from ..agent.context import ContextManager, build_system_prompt, load_project_memory
from ..agent.core import CodeAgent
from ..mcp.config import load_mcp_config
from ..mcp.manager import MCPManager
from ..api.providers import AnthropicClient, OpenAICompatibleClient
from ..agent.session import SessionStore
from .slash_commands import SlashCommandProcessor
from .smoke import SILICONFLOW_BASE_URL, SILICONFLOW_DEFAULT_MODEL
from ..runtime.tool_context import ToolContext
from .tool_profiles import (
    TOOL_PROFILE_CHOICES,
    filter_tools,
    parse_name_list,
    resolve_mcp_server_names,
    select_mcp_config,
)
from ..tools import ToolRegistry, default_tools


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run InsightAgent V5.0 interactive CLI")
    parser.add_argument("--provider", choices=["siliconflow", "openai", "anthropic"])
    parser.add_argument("--model")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--session-id")
    parser.add_argument("--session-dir")
    parser.add_argument("--permission-mode", choices=["read-only", "workspace-write"])
    parser.add_argument(
        "--language",
        help="Language for the model's replies (e.g. Chinese, English). "
        "Defaults to auto, which mirrors the user's language.",
    )
    parser.add_argument("--config-home")
    parser.add_argument(
        "--tool-profile",
        default="coding-basic",
        choices=TOOL_PROFILE_CHOICES,
        help="Tool surface exposed to the model.",
    )
    parser.add_argument(
        "--allowed-tools",
        action="append",
        help="Further narrow the selected profile. Repeat or use comma-separated names.",
    )
    parser.add_argument(
        "--enable-mcp-server",
        action="append",
        help="Opt in to MCP servers by config name. Repeat or use comma-separated names; use all for every configured server.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    workspace = Path(args.workspace).expanduser().resolve()
    start_dir = Path.cwd().resolve()
    load_dotenv_files(workspace, start_dir=start_dir)
    workspace.mkdir(parents=True, exist_ok=True)
    config = load_runtime_config(
        workspace,
        user_config_home=args.config_home,
        overrides={
            "provider": args.provider,
            "model": args.model,
            "permission_mode": args.permission_mode,
            "session_dir": args.session_dir,
            "response_language": args.language,
        },
    )
    session_root = Path(config.session_dir)
    if not session_root.is_absolute():
        session_root = workspace / session_root
    session_store = SessionStore(session_root)
    session = session_store.load(args.session_id) if args.session_id else session_store.create(
        metadata={"workspace": str(workspace), "provider": config.provider, "model": config.model}
    )
    project_memory = load_project_memory(workspace)
    base_prompt = f"""You are InsightAgent V5.0 interactive CLI.
Workspace: {workspace}
Use workspace-safe tools. Use slash commands only when the user types them directly.
{language_directive(config.response_language)}"""
    system_prompt = build_system_prompt(base_prompt, project_memory)

    if config.provider == "anthropic":
        client = AnthropicClient(model=config.model, base_url=config.base_url, timeout=config.timeout)
    elif config.provider == "siliconflow":
        client = OpenAICompatibleClient(
            api_key=os.environ.get("SILICONFLOW_API_KEY") or os.environ.get("OPENAI_API_KEY"),
            model=config.model or os.environ.get("SILICONFLOW_MODEL", SILICONFLOW_DEFAULT_MODEL),
            base_url=config.base_url or os.environ.get("SILICONFLOW_BASE_URL", SILICONFLOW_BASE_URL),
            timeout=config.timeout,
        )
    else:
        client = OpenAICompatibleClient(model=config.model, base_url=config.base_url, timeout=config.timeout)

    tool_context = ToolContext(workspace=workspace, permission_mode=config.permission_mode)
    mcp_config_home = Path(args.config_home).expanduser() if args.config_home else None
    mcp_config = load_mcp_config(workspace, user_config_home=mcp_config_home, start_dir=start_dir)
    try:
        allowed_tools = parse_name_list(args.allowed_tools)
        builtin_tools = filter_tools(default_tools(tool_context), profile=args.tool_profile, allowed_tools=allowed_tools)
        selected_mcp_names = resolve_mcp_server_names(args.tool_profile, args.enable_mcp_server)
        selected_mcp_config = select_mcp_config(mcp_config, selected_mcp_names)
    except ValueError as error:
        raise SystemExit(f"Tool configuration error: {error}") from error
    mcp_manager = MCPManager(selected_mcp_config)
    mcp_manager.start_enabled()
    agent = CodeAgent(
        client,
        tools=ToolRegistry(tools=builtin_tools + mcp_manager.get_tools(), context=tool_context),
        context_manager=ContextManager(
            max_tool_output_chars=config.max_tool_output_chars,
            compact_tool_output_chars=config.compact_tool_output_chars,
        ),
        system_prompt=system_prompt,
        max_tool_iterations=config.max_tool_iterations,
        session_store=session_store,
        session=session,
    )
    slash = SlashCommandProcessor(
        agent,
        session_store=session_store,
        project_memory=project_memory,
        mcp_manager=mcp_manager,
    )

    print(f"InsightAgent V5.0 session={session.session_id}. Type 'exit' or 'quit' to stop. Try /help.")
    try:
        while True:
            try:
                user_input = input("\nuser> ").strip()
            except EOFError:
                print()
                break
            if user_input.lower() in {"exit", "quit"}:
                break
            if not user_input:
                continue
            if user_input.startswith("/"):
                print(f"\n{slash.handle(user_input)}")
                continue
            result = agent.run_turn(user_input)
            print(f"\nassistant> {result.content}")
    finally:
        mcp_manager.stop_all()


if __name__ == "__main__":
    main()
