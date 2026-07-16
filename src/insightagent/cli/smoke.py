"""One-shot smoke entry point using the production LangGraph runner."""

from __future__ import annotations

import argparse
from pathlib import Path

from insightagent.config import load_dotenv_files, load_runtime_config
from insightagent.graph.runner import run_task


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a one-shot real-provider graph smoke test.")
    parser.add_argument("--provider", choices=["openai", "anthropic", "siliconflow"])
    parser.add_argument("--model", help="Override MODEL_ID for this invocation.")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--prompt", default="Reply with exactly: pong")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    workspace = Path(args.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    load_dotenv_files(workspace, start_dir=Path.cwd())
    config = load_runtime_config(
        workspace,
        overrides={"provider": args.provider, "model": args.model},
    )
    outcome = run_task(
        task=args.prompt,
        workspace=workspace,
        config=config,
        session_id=None,
        checkpoint_id=None,
        tool_profile="analysis",
        allowed_tools=None,
        enabled_mcp_servers=set(),
        trace_jsonl=None,
        no_trace=True,
    )
    print(outcome.final_answer)
    if outcome.state.get("phase") != "done":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
