"""Run a one-shot real coding task with V5.0 runtime services."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .agent import CodeAgent
from .config import RuntimeConfig, load_runtime_config
from .context import ContextManager, ProjectMemory, build_system_prompt, load_project_memory
from .mcp.config import load_mcp_config
from .mcp.manager import MCPManager
from .providers import AnthropicClient, OpenAICompatibleClient, ProviderError
from .session import Session, SessionStore
from .smoke import SILICONFLOW_BASE_URL, SILICONFLOW_DEFAULT_MODEL
from .tool_context import ToolContext
from .tools import ToolRegistry, default_tools
from .trace import ConsoleTracer


DEFAULT_TASK = """在工作区中创建一个 Python 文件 hello_agent.py。
要求：
1. 先用文字给出简短 plan，不要在普通文本里输出完整代码。
2. 调用 write_file 写入完整代码。
3. 代码实现 fibonacci(n) 并在直接运行时打印 fibonacci(10)。
4. 调用 execute_command 运行 python3 hello_agent.py 验证输出。
5. 最后总结你创建的文件和验证结果。"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a traced InsightAgent V5.0 coding task.")
    parser.add_argument("--provider", choices=["siliconflow", "openai", "anthropic"])
    parser.add_argument("--model", help="Override provider model.")
    parser.add_argument(
        "--workspace",
        default="demo_workspace",
        help="Directory the task should use for generated files.",
    )
    parser.add_argument("--task", default=DEFAULT_TASK, help="Task prompt.")
    parser.add_argument("--no-trace", action="store_true", help="Only print the final answer.")
    parser.add_argument("--timeout", type=int, help="Provider HTTP timeout in seconds.")
    parser.add_argument("--max-tool-iterations", type=int, help="Maximum model/tool loop iterations.")
    parser.add_argument("--trace-max-chars", type=int, help="Maximum characters shown per trace section.")
    parser.add_argument("--max-tool-output-chars", type=int, help="Maximum tool-result characters sent back to the model.")
    parser.add_argument("--compact-tool-output-chars", type=int, help="Tool-result characters kept in history after task completion.")
    parser.add_argument("--permission-mode", choices=["read-only", "workspace-write"])
    parser.add_argument("--session-id", help="Resume an existing session id.")
    parser.add_argument("--session-dir", help="Override session directory.")
    parser.add_argument("--export-transcript", help="Export the final session transcript to Markdown.")
    parser.add_argument("--config-home", help="Override user config home for tests or isolated runs.")
    parser.add_argument("--list-sessions", action="store_true", help="List sessions and exit.")
    parser.add_argument(
        "--allow-no-tool-final",
        action="store_true",
        help="Allow the model to finish without any tool call. By default run_task requires tool use.",
    )
    return parser


def build_agent(
    config: RuntimeConfig,
    workspace: Path,
    project_memory: ProjectMemory,
    session_store: SessionStore,
    session: Session,
    tools: ToolRegistry | None = None,
) -> CodeAgent:
    base_system_prompt = f"""You are InsightAgent V5.0, a complete coding-agent runtime with sessions, config, usage tracking, project memory, grep search, self-healing, and workspace-safe tools.
You are running a real coding-task demo.
Show a short plan in assistant text before using tools.
Do not place full source code in assistant text; put full source code in the write_file tool arguments.
Use grep_search to find code when useful. Use tools to inspect, write, edit, and run files.
Only create or modify files inside this workspace: {workspace}
Prefer edit_file for local changes to existing files.
When running shell commands, set cwd to this workspace when possible."""
    system_prompt = build_system_prompt(base_system_prompt, project_memory)
    if config.provider == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ProviderError("ANTHROPIC_API_KEY is required for provider=anthropic")
        client = AnthropicClient(model=config.model, base_url=config.base_url, timeout=config.timeout)
    elif config.provider == "siliconflow":
        api_key = os.environ.get("SILICONFLOW_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ProviderError("SILICONFLOW_API_KEY is required for provider=siliconflow")
        client = OpenAICompatibleClient(
            api_key=api_key,
            model=config.model or os.environ.get("SILICONFLOW_MODEL", SILICONFLOW_DEFAULT_MODEL),
            base_url=config.base_url or os.environ.get("SILICONFLOW_BASE_URL", SILICONFLOW_BASE_URL),
            timeout=config.timeout,
        )
    else:
        if not os.environ.get("OPENAI_API_KEY"):
            raise ProviderError("OPENAI_API_KEY is required for provider=openai")
        client = OpenAICompatibleClient(model=config.model, base_url=config.base_url, timeout=config.timeout)
    return CodeAgent(
        client,
        tools=tools,
        context_manager=ContextManager(
            max_tool_output_chars=config.max_tool_output_chars,
            compact_tool_output_chars=config.compact_tool_output_chars,
        ),
        system_prompt=system_prompt,
        max_tool_iterations=config.max_tool_iterations,
        session_store=session_store,
        session=session,
        require_tool_use=True,
    )


def main() -> None:
    args = build_parser().parse_args()
    workspace = Path(args.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    os.chdir(workspace)
    config = load_runtime_config(
        workspace,
        user_config_home=args.config_home,
        overrides={
            "provider": args.provider,
            "model": args.model,
            "timeout": args.timeout,
            "max_tool_iterations": args.max_tool_iterations,
            "max_tool_output_chars": args.max_tool_output_chars,
            "compact_tool_output_chars": args.compact_tool_output_chars,
            "permission_mode": args.permission_mode,
            "trace_max_chars": args.trace_max_chars,
            "session_dir": args.session_dir,
        },
    )
    session_root = Path(config.session_dir)
    if not session_root.is_absolute():
        session_root = workspace / session_root
    session_store = SessionStore(session_root)
    if args.list_sessions:
        for session_id in session_store.list_sessions():
            print(session_id)
        return
    session = session_store.load(args.session_id) if args.session_id else session_store.create(
        metadata={"workspace": str(workspace), "provider": config.provider, "model": config.model}
    )
    project_memory = load_project_memory(workspace)
    task = f"{args.task}\n\nWorkspace absolute path: {workspace}"
    tracer = None if args.no_trace else ConsoleTracer(max_chars=config.trace_max_chars)
    if tracer is not None:
        tracer(
            {
                "type": "session_started",
                "session_id": session.session_id,
                "session_dir": str(session_store.root),
                "loaded_config_files": config.loaded_files,
            }
        )
    if tracer is not None and not project_memory.is_empty:
        tracer(
            {
                "type": "memory_injected",
                "files": [filename for filename, _content in project_memory.sections],
            }
        )
    tool_context = ToolContext(workspace=workspace, permission_mode=config.permission_mode)
    mcp_config_home = Path(args.config_home).expanduser() if args.config_home else None
    mcp_config = load_mcp_config(workspace, user_config_home=mcp_config_home)
    if tracer is not None:
        tracer({"type": "mcp_config_loaded", "loaded_config_files": mcp_config.loaded_files})
    mcp_manager = MCPManager(mcp_config)
    mcp_manager.start_enabled(trace=tracer)
    tools = ToolRegistry(tools=default_tools(tool_context) + mcp_manager.get_tools(), context=tool_context)
    try:
        agent = build_agent(
            config,
            workspace,
            project_memory,
            session_store,
            session,
            tools=tools,
        )
        if args.allow_no_tool_final:
            agent.require_tool_use = False
        result = agent.run_turn_with_trace(task, trace=tracer)
    except ProviderError as error:
        print(f"\nProvider error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    finally:
        mcp_manager.stop_all(trace=tracer)
    if args.no_trace:
        print(result.content)
    if args.export_transcript:
        exported = session_store.export_markdown(session, args.export_transcript)
        print(f"exported transcript: {exported}")


if __name__ == "__main__":
    main()
