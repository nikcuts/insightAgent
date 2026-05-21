"""Run a one-shot real coding task with trace output."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .agent import CodeAgent
from .config import AgentConfig
from .factory import build_agent_with_memory
from .providers import AnthropicClient, OpenAICompatibleClient, ProviderError
from .smoke import SILICONFLOW_BASE_URL, SILICONFLOW_DEFAULT_MODEL
from .trace import ConsoleTracer


DEFAULT_TASK = """在工作区中创建一个 Python 文件 hello_agent.py。
要求：
1. 先用文字给出简短 plan，不要在普通文本里输出完整代码。
2. 调用 write_file 写入完整代码。
3. 代码实现 fibonacci(n) 并在直接运行时打印 fibonacci(10)。
4. 调用 execute_command 运行 python3 hello_agent.py 验证输出。
5. 最后总结你创建的文件和验证结果。"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a traced InsightAgent V2.0 coding task.")
    parser.add_argument("--provider", choices=["siliconflow", "openai", "anthropic"], default="siliconflow")
    parser.add_argument("--model", help="Override provider model.")
    parser.add_argument(
        "--workspace",
        default="demo_workspace",
        help="Directory the task should use for generated files.",
    )
    parser.add_argument("--task", default=DEFAULT_TASK, help="Task prompt.")
    parser.add_argument("--no-trace", action="store_true", help="Only print the final answer.")
    parser.add_argument("--timeout", type=int, default=300, help="Provider HTTP timeout in seconds.")
    parser.add_argument("--max-tool-iterations", type=int, default=12, help="Maximum model/tool loop iterations.")
    parser.add_argument("--trace-max-chars", type=int, default=1000, help="Maximum characters shown per trace section.")
    return parser


def build_agent(
    provider: str,
    model: str | None,
    workspace: Path,
    timeout: int,
    max_tool_iterations: int,
) -> CodeAgent:
    system_prompt = f"""You are InsightAgent V2.0, a baseline coding agent with project memory and context control.
You are running a real coding-task demo.
Show a short plan in assistant text before using tools.
Do not place full source code in assistant text; put full source code in the write_file tool arguments.
Use tools to inspect, write, and run files.
Only create or modify files inside this workspace: {workspace}
When running shell commands, set cwd to this workspace when possible."""
    if provider == "anthropic":
        client = AnthropicClient(model=model, timeout=timeout)
    elif provider == "siliconflow":
        client = OpenAICompatibleClient(
            api_key=os.environ.get("SILICONFLOW_API_KEY") or os.environ.get("OPENAI_API_KEY"),
            model=model or os.environ.get("SILICONFLOW_MODEL", SILICONFLOW_DEFAULT_MODEL),
            base_url=os.environ.get("SILICONFLOW_BASE_URL", SILICONFLOW_BASE_URL),
            timeout=timeout,
        )
    else:
        client = OpenAICompatibleClient(model=model, timeout=timeout)
    config = AgentConfig(
        max_tool_iterations=max_tool_iterations,
        max_tool_result_chars=6000,
        compact_completed_turns=True,
    )
    return build_agent_with_memory(
        client,
        workspace=workspace,
        config=config,
        base_system_prompt=system_prompt,
    )


def main() -> None:
    args = build_parser().parse_args()
    workspace = Path(args.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    os.chdir(workspace)
    task = f"{args.task}\n\nWorkspace absolute path: {workspace}"
    agent = build_agent(args.provider, args.model, workspace, args.timeout, args.max_tool_iterations)
    tracer = None if args.no_trace else ConsoleTracer(max_chars=args.trace_max_chars)
    try:
        result = agent.run_turn_with_trace(task, trace=tracer)
    except ProviderError as error:
        print(f"\nProvider error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    if args.no_trace:
        print(result.content)


if __name__ == "__main__":
    main()
