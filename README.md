# InsightAgent

InsightAgent is a versioned CodeAgent research project. Its purpose is to build a clear, testable progression from a native baseline agent to a more Claude Code style coding agent, while keeping every version easy to compare in reports and experiments.

The project is developed in strict version steps:

1. Build one version.
2. Run offline and real-provider tests.
3. Commit that version as a stable checkpoint when Git metadata is available.
4. Move to the next version.

The local reference repository `/home/dinghanchen/stuckin/claw-code-parity` is used as a high-level mechanism reference, especially for context handling, tool behavior, output management, permission boundaries, and parity-style testing.

## Current Status

Current version: **V2.0 context and projectization**

V2.0 adds:

- Project memory loading from `MEMORY.md` and `.codeagent.md`.
- Deterministic memory injection into the system prompt.
- Tool-result truncation before large outputs enter model-visible history.
- Completed-turn compaction for bulky intermediate tool messages.
- Trace events for memory loading, truncation, and compaction.
- A formal `pyproject.toml` package shape and `insightagent` console entry point.
- Expanded offline unit tests for V2 context behavior.

V1.0 remains the baseline comparison point: a minimal agent loop, provider adapters, simple tools, sliding-window memory, and trace output.

## Features

### Agentic Loop

`insightagent.agent.CodeAgent` implements the core loop:

```text
user input
-> model request
-> assistant text or tool calls
-> local tool execution
-> tool result truncation
-> tool results appended to conversation
-> next model request
-> final answer
-> completed-turn compaction
```

The loop supports multiple tool iterations and returns a final `AgentResult`.

### Project Memory

InsightAgent V2.0 loads workspace memory files before constructing the agent system prompt:

- `MEMORY.md` for general project context.
- `.codeagent.md` for InsightAgent-specific instructions.

When both exist, `MEMORY.md` is injected first and `.codeagent.md` second, giving the agent-specific file higher effective priority.

### Context Control

Large tool results are truncated before entering model-visible conversation history. Truncated results keep the head, tail, and omitted character count.

After a final answer, completed-turn compaction can shorten bulky intermediate tool messages for future turns. This keeps follow-up turns cleaner without hiding the fact that tools ran or whether they failed.

### Model Providers

`insightagent.providers` includes:

- `OpenAICompatibleClient` for OpenAI-compatible Chat Completions APIs.
- `AnthropicClient` for Anthropic Messages API.
- SiliconFlow support through the OpenAI-compatible client.

Secrets are read from environment variables. API keys must not be hard-coded in source files, README examples, tests, or command history intended for sharing.

### Tools

V2.0 keeps the deliberately simple V1.0 tools:

- `execute_command`: runs a shell command and returns exit code, stdout, and stderr.
- `read_file`: reads a complete UTF-8 file.
- `write_file`: overwrites a complete UTF-8 file.

These tools remain basic so V2 can focus on context control rather than safety.

### Trace Output

`insightagent.trace.ConsoleTracer` prints observable execution events:

- user message
- memory loaded
- model request
- model response
- tool call
- tool result
- tool result truncated
- history compacted
- final answer

Long `write_file.content` values are summarized in trace output so demos remain readable while the actual tool still receives the full write content.

## Project Layout

```text
insightagent/
├── README.md
├── pyproject.toml
├── insightagent/
│   ├── __init__.py
│   ├── agent.py            # core agent loop
│   ├── cli.py              # interactive CLI
│   ├── compaction.py       # truncation and completed-turn compaction
│   ├── config.py           # V2 runtime defaults
│   ├── context.py          # system prompt construction
│   ├── factory.py          # shared agent assembly
│   ├── memory.py           # sliding-window memory
│   ├── messages.py         # provider-neutral message dataclasses
│   ├── project_memory.py   # workspace memory loading
│   ├── providers.py        # OpenAI-compatible and Anthropic clients
│   ├── run_task.py         # traced real coding task runner
│   ├── smoke.py            # one-shot real-provider smoke test
│   ├── tools.py            # basic tools
│   └── trace.py            # console trace rendering
└── tests/
    ├── test_agent_loop.py
    ├── test_cli.py
    ├── test_compaction.py
    ├── test_config.py
    ├── test_context.py
    ├── test_factory.py
    ├── test_project_memory.py
    └── test_providers.py
```

## Environment

OpenAI-compatible provider:

```bash
export OPENAI_API_KEY="..."
export OPENAI_MODEL="gpt-4o-mini"
export OPENAI_BASE_URL="https://api.openai.com/v1"
```

Anthropic provider:

```bash
export ANTHROPIC_API_KEY="..."
export ANTHROPIC_MODEL="claude-sonnet-4-20250514"
```

SiliconFlow:

```bash
export SILICONFLOW_API_KEY="..."
export SILICONFLOW_BASE_URL="https://api.siliconflow.cn/v1"
export SILICONFLOW_MODEL="Qwen/Qwen2.5-72B-Instruct"
```

The SiliconFlow base URL defaults to `https://api.siliconflow.cn/v1`.

## Usage

Run a SiliconFlow smoke test:

```bash
python3 -m insightagent.smoke --provider siliconflow --model "Qwen/Qwen2.5-72B-Instruct"
```

Run the interactive CLI:

```bash
python3 -m insightagent.cli \
  --provider openai \
  --workspace . \
  --max-tool-result-chars 6000
```

After package installation, the console entry point is:

```bash
insightagent --provider openai --workspace .
```

Run a traced coding task:

```bash
python3 -m insightagent.run_task \
  --provider siliconflow \
  --model "Qwen/Qwen2.5-72B-Instruct" \
  --timeout 300 \
  --workspace demo_card_game \
  --task "请在工作区创建一个 Python 纸牌游戏 card_war.py。要求：先给出简短 plan，不要在普通文本里输出完整代码；调用 write_file 写入完整代码；实现 War 纸牌游戏的自动模拟版本，包含创建牌组、洗牌、双方各抽一张比较大小、累计分数、运行 10 回合；只能用 Python 标准库；调用 execute_command 运行 python3 card_war.py 验证输出；最后总结文件和验证结果。"
```

Run tests:

```bash
python3 -m py_compile insightagent/*.py tests/*.py
python3 -m unittest discover -s tests -v
```

## V1 To V2 Comparison

```text
V1.0: context grows with raw tool output until sliding-window drop.
V2.0: project memory is injected, large tool output is truncated, and completed turns are compacted.
```

V2.0 improves context behavior and project shape. It does not yet make tools safe.

## Current Limitations

- No strict workspace boundary enforcement.
- No permission model for destructive commands.
- No safe edit tool; `write_file` still overwrites whole files.
- No grep/search tool.
- No self-healing repair state machine.
- No broad mock parity harness.
- Git metadata in this workspace may need repair because the current `.git` directory is empty.

## Roadmap

### V1.0: Native CodeAgent Baseline

Status: implemented and tested.

Scope:

- Basic agent loop.
- OpenAI-compatible and Anthropic providers.
- `execute_command`, `read_file`, `write_file`.
- Sliding-window memory.
- Trace output for demos.

### V2.0: Context, Memory, And Projectization

Status: implemented.

Scope:

- Project memory loading.
- System prompt injection.
- Tool-result truncation.
- Completed-turn compaction.
- V2 trace events.
- Formal package metadata.
- Expanded unit tests.

### V3.0: Robust Tools And Safety

Planned later.

Scope:

- `ToolContext` with workspace, permissions, and immutable runtime config.
- Workspace-bound file tools.
- Local edit tool instead of full-file overwrite.
- Read-only vs write permission modes.
- Confirmation prompts for destructive commands.
- File size, binary file, symlink escape, and path traversal guards inspired by the reference repository.

### V4.0: Search And Self-Healing

Planned later.

Scope:

- High-performance grep tool.
- Syntax-check aware repair loop.
- Bash error repair loop.
- Multi-step recovery state for failed tool calls.
