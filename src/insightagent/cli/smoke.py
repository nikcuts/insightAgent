"""Real-provider smoke test for InsightAgent V5.0."""

from __future__ import annotations

import argparse
import os

from ..agent.core import CodeAgent
from ..api.providers import AnthropicClient, OpenAICompatibleClient
from ..tools import ToolRegistry


SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
SILICONFLOW_DEFAULT_MODEL = "Qwen/Qwen2.5-7B-Instruct"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a one-shot real-provider smoke test.")
    parser.add_argument(
        "--provider",
        choices=["openai", "anthropic", "siliconflow"],
        default="siliconflow",
    )
    parser.add_argument("--model", help="Override provider model.")
    parser.add_argument(
        "--prompt",
        default="Reply with exactly: pong",
        help="Prompt for the one-shot smoke test.",
    )
    return parser


def build_agent(provider: str, model: str | None) -> CodeAgent:
    if provider == "anthropic":
        return CodeAgent(AnthropicClient(model=model))
    if provider == "siliconflow":
        api_key = os.environ.get("SILICONFLOW_API_KEY") or os.environ.get("OPENAI_API_KEY")
        return CodeAgent(
            OpenAICompatibleClient(
                api_key=api_key,
                model=model or os.environ.get("SILICONFLOW_MODEL", SILICONFLOW_DEFAULT_MODEL),
                base_url=os.environ.get("SILICONFLOW_BASE_URL", SILICONFLOW_BASE_URL),
            ),
            tools=ToolRegistry([]),
        )
    return CodeAgent(OpenAICompatibleClient(model=model))


def main() -> None:
    args = build_parser().parse_args()
    agent = build_agent(args.provider, args.model)
    result = agent.run_turn(args.prompt)
    print(result.content)


if __name__ == "__main__":
    main()
