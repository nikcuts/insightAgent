"""Interactive CLI for InsightAgent V2.0."""

from __future__ import annotations

import argparse
from pathlib import Path

from .agent import CodeAgent
from .config import AgentConfig
from .factory import build_agent_with_memory
from .providers import AnthropicClient, OpenAICompatibleClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run InsightAgent V2.0")
    parser.add_argument(
        "--provider",
        choices=["openai", "anthropic"],
        default="openai",
        help="Model provider to use. OpenAI mode also supports OpenAI-compatible base URLs.",
    )
    parser.add_argument("--workspace", default=".", help="Workspace used for project memory loading.")
    parser.add_argument("--max-tool-result-chars", type=int, default=6000)
    parser.add_argument("--no-compact", action="store_true", help="Disable completed-turn compaction.")
    return parser


def build_agent(
    provider: str,
    workspace: str = ".",
    max_tool_result_chars: int = 6000,
    no_compact: bool = False,
) -> CodeAgent:
    if provider == "anthropic":
        client = AnthropicClient()
    else:
        client = OpenAICompatibleClient()
    config = AgentConfig(max_tool_result_chars=max_tool_result_chars, compact_completed_turns=not no_compact)
    return build_agent_with_memory(client, Path(workspace), config=config)


def main() -> None:
    args = build_parser().parse_args()
    agent = build_agent(args.provider, args.workspace, args.max_tool_result_chars, args.no_compact)
    print("InsightAgent V2.0. Type 'exit' or 'quit' to stop.")
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
        result = agent.run_turn(user_input)
        print(f"\nassistant> {result.content}")


if __name__ == "__main__":
    main()
