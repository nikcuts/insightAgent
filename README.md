# InsightAgent V5.0

InsightAgent V5.0 turns the earlier prototype into a small but complete coding-agent runtime.

Earlier checkpoints are preserved for comparison:

- `/home/dinghanchen/stuckin/insightagent`: V1.0 baseline loop
- `/home/dinghanchen/stuckin/insightagent_v2`: memory injection, truncation, micro-compaction
- `/home/dinghanchen/stuckin/insightagent_v3`: ToolContext, workspace safety, permissions, `edit_file`
- `/home/dinghanchen/stuckin/insightagent_v4`: `grep_search`, self-healing repair prompt
- `/home/dinghanchen/stuckin/insightagent_v5`: runtime system

V5.0 is inspired by the runtime structure in `/home/dinghanchen/stuckin/claw-code-parity`, especially its config loader, session persistence, compaction, usage tracking, CLI commands, and parity-harness mindset.

## What V5 Adds

V5.0 keeps all V4 behavior and adds:

- runtime config loading and precedence
- session creation, persistence, resume, listing, and transcript export
- usage estimation per model call
- slash command dispatcher
- interactive CLI with `/status`, `/cost`, `/memory`, `/compact`, `/clear`, `/permissions`, `/export`
- `run_task` integration with sessions and config
- task lifecycle state machine: `plan -> implement -> verify -> repair -> summarize`

This is the first version that behaves like a stateful runtime rather than a one-off demo script.

## Architecture

```text
config.py          # user/project/local config merge
session.py         # JSON session store and Markdown transcript export
task_state.py      # plan/implement/verify/repair/summarize lifecycle
usage.py           # token/cost-ish usage estimation
slash_commands.py  # slash command dispatcher
agent.py           # agent loop with usage + session sync
run_task.py        # non-interactive task runner
cli.py             # interactive REPL
```

The V5 flow:

```text
load config
-> create/resume session
-> load project memory
-> assemble system prompt
-> run model/tool loop
-> inject phase guidance and track task lifecycle
-> record usage per model call
-> persist messages and metadata
-> allow slash-command inspection/export/compact
```

## Configuration

Config precedence:

```text
~/.insightagent/config.json
<workspace>/.insightagent/config.json
<workspace>/.insightagent/local.json
CLI arguments
```

Example project config:

```json
{
  "model": {
    "provider": "siliconflow",
    "name": "Qwen/Qwen2.5-72B-Instruct",
    "base_url": "https://api.siliconflow.cn/v1"
  },
  "runtime": {
    "timeout": 300,
    "max_tool_iterations": 12,
    "max_tool_output_chars": 8000,
    "compact_tool_output_chars": 600
  },
  "permissions": {
    "mode": "workspace-write"
  },
  "tracing": {
    "max_chars": 1000
  },
  "sessions": {
    "dir": ".insightagent/sessions"
  }
}
```

Local config is intended for machine-specific overrides and should not contain shared secrets.

## Environment

SiliconFlow:

```bash
export SILICONFLOW_API_KEY="..."
export SILICONFLOW_BASE_URL="https://api.siliconflow.cn/v1"
export SILICONFLOW_MODEL="Qwen/Qwen2.5-72B-Instruct"
```

OpenAI-compatible:

```bash
export OPENAI_API_KEY="..."
export OPENAI_BASE_URL="https://api.openai.com/v1"
export OPENAI_MODEL="gpt-4o-mini"
```

Anthropic:

```bash
export ANTHROPIC_API_KEY="..."
export ANTHROPIC_MODEL="claude-sonnet-4-20250514"
```

Do not hard-code API keys in source files, config committed to Git, README examples, or shared logs.

## Non-Interactive Usage

```bash
cd /home/dinghanchen/stuckin/insightagent_v5

python3 -m insightagent.run_task \
  --provider siliconflow \
  --model "Qwen/Qwen2.5-72B-Instruct" \
  --timeout 300 \
  --workspace demo_v5 \
  --permission-mode workspace-write \
  --export-transcript demo_v5/transcript.md \
  --task "请创建一个 Python 纸牌游戏 card_war.py。先给 plan，写文件，运行 python3 -m py_compile card_war.py 和 python3 card_war.py。如果出现错误，请自动修复并重新验证。最后总结。"
```

The trace starts with a session block:

```text
--- SESSION ---
id=<session_id>
dir=<workspace>/.insightagent/sessions
config_files=...
```

List sessions:

```bash
python3 -m insightagent.run_task --workspace demo_v5 --list-sessions
```

Resume a session:

```bash
python3 -m insightagent.run_task \
  --workspace demo_v5 \
  --session-id <session_id> \
  --task "继续上一个任务，检查当前文件并总结状态。"
```

## Interactive CLI

```bash
python3 -m insightagent.cli \
  --provider siliconflow \
  --model "Qwen/Qwen2.5-72B-Instruct" \
  --workspace demo_v5
```

Slash commands:

```text
/help
/status
/cost
/memory
/compact
/clear
/permissions
/export transcript.md
```

## Session Storage

Sessions are saved as JSON:

```text
<workspace>/.insightagent/sessions/<session_id>.json
```

Each session records:

- session id
- created/updated timestamps
- metadata
- messages
- assistant tool calls
- tool results
- usage estimates

Markdown transcript export is supported through:

```bash
--export-transcript path/to/transcript.md
```

or interactively:

```text
/export transcript.md
```

## Runtime Limitations

V5.0 is much less toy-like than V1-V4, but it is still not a full Claude Code replacement:

- token usage is estimated from characters, not provider tokenizer data
- session storage is JSON file based, not concurrent or database backed
- `/compact` uses deterministic structural summary, not LLM-generated summary
- slash command support is useful but not a full terminal UI
- hook system is not implemented yet
- mock parity harness is not implemented yet
- LSP diagnostics are best-effort local syntax checks rather than a persistent language-server session
- MCP, plugins, and sub-agent orchestration are still future work

## Tests

```bash
python3 -m py_compile insightagent/*.py tests/*.py
python3 -m unittest discover -s tests -v
```

Expected result:

```text
Ran 37 tests
OK
```

Test coverage includes:

- agent loop
- task lifecycle state transitions
- self-healing repair prompt
- config merge precedence
- project memory injection
- tool output truncation and compaction
- provider message conversion
- session save/load/export
- slash command behavior
- workspace permission checks
- `edit_file`
- `grep_search`
- `glob_search`
- `git_status` and `git_diff`
- `todo_write`
- best-effort `lsp_diagnostics`
- usage estimation

## Next Work

The next major system step should be **V6.0: Hooks + Audit + Parity Harness**:

- `pre_tool_use`
- `post_tool_use`
- `post_tool_failure`
- structured audit log
- deterministic fake-model scenario runner
- parity scenarios for write allowed/denied, grep, repair, compaction, and resume

That would bring InsightAgent closer to the engineering shape of `claw-code-parity`.
