# InsightAgent V5.0

InsightAgent V5.0 将前几个版本的原型推进为一个小而完整的 coding-agent runtime。

前序版本保留用于对比：

- `/home/dinghanchen/stuckin/insightagent`：V1.0 基础循环
- `/home/dinghanchen/stuckin/insightagent_v2`：memory 注入、截断、微型压缩
- `/home/dinghanchen/stuckin/insightagent_v3`：ToolContext、workspace 安全边界、权限、`edit_file`
- `/home/dinghanchen/stuckin/insightagent_v4`：`grep_search`、self-healing repair prompt
- `/home/dinghanchen/stuckin/insightagent_v5`：runtime 系统

V5.0 参考了 `/home/dinghanchen/stuckin/claw-code-parity` 的 runtime 结构，尤其是配置加载、session 持久化、上下文压缩、用量统计、CLI 命令和 parity-harness 思路。

## V5 新增能力

V5.0 保留 V4 的全部行为，并新增：

- runtime 配置加载和优先级合并
- session 创建、持久化、恢复、列表查询和 transcript 导出
- 每次模型调用的用量估算
- slash command dispatcher
- 交互式 CLI，支持 `/status`、`/cost`、`/memory`、`/compact`、`/clear`、`/permissions`、`/export`
- 带 session 和 config 集成的 `run_task`
- 任务生命周期状态机：`plan -> implement -> verify -> repair -> summarize`
- 结构化工具 package，以及 Python 代码分析工具：`parse_ast`、`get_function_signature`、`find_dependencies`、`get_code_metrics`
- MCP Runtime Layer：支持 `stdio`、`streamable_http`、MCP tools/resources/prompts 和 `/mcp` CLI 状态命令

这是第一个真正像“有状态 runtime”的版本，不再只是一次性 demo script。

## 架构

```text
config.py          # 用户/项目/local/CLI 配置合并
session.py         # JSON session 存储和 Markdown transcript 导出
task_state.py      # plan/implement/verify/repair/summarize 生命周期
usage.py           # token/cost-ish 用量估算
slash_commands.py  # slash command dispatcher
agent.py           # 带 usage 和 session sync 的 agent loop
run_task.py        # 非交互式任务 runner
cli.py             # 交互式 REPL
mcp/               # MCP config、protocol、transport、client、manager、adapter
  config.py
  protocol.py
  transports.py
  client.py
  manager.py
  adapters.py
tools/             # execution、file、search、state、code-analysis 工具
  base.py
  execution_tools.py
  file_tools.py
  search_tools.py
  state_tools.py
  code_analysis_tools.py
  registry.py
```

V5 流程：

```text
加载配置
-> 创建或恢复 session
-> 加载项目 memory
-> 启动 enabled MCP servers 并加载 MCP tools/resources/prompts
-> 组装 system prompt
-> 运行模型/工具循环
-> 注入阶段指导并跟踪任务生命周期
-> 记录每次模型调用的用量
-> 持久化 messages 和 metadata
-> 支持 slash command 检查、导出和压缩
```

## 配置

配置优先级：

```text
~/.insightagent/config.json
<workspace>/.insightagent/config.json
<workspace>/.insightagent/local.json
CLI 参数
```

项目配置示例：

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

local 配置用于机器本地覆盖，不应包含需要共享的密钥。

## MCP 配置

MCP 配置单独存放，加载顺序：

```text
~/.insightagent/mcp_config.json
<workspace>/.insightagent/mcp_config.json
<workspace>/mcp_config.json
```

示例见 [mcp_config.json.example](mcp_config.json.example)。

```json
{
  "mcpServers": {
    "playwright": {
      "transport": "stdio",
      "command": "npx",
      "args": ["@playwright/mcp@latest"],
      "enabled": true,
      "tool_prefix": "mcp_playwright"
    }
  }
}
```

MCP tool 会按 `<prefix>_<tool>` 暴露给模型，例如 `mcp_playwright_navigate`。MCP resources 和 prompts 会通过 `<prefix>_list_resources`、`<prefix>_read_resource`、`<prefix>_list_prompts`、`<prefix>_get_prompt` 暴露。

详细说明见 [MCP_GUIDE.md](MCP_GUIDE.md)。

## 环境变量

SiliconFlow：

```bash
export SILICONFLOW_API_KEY="..."
export SILICONFLOW_BASE_URL="https://api.siliconflow.cn/v1"
export SILICONFLOW_MODEL="Qwen/Qwen2.5-72B-Instruct"
```

OpenAI-compatible：

```bash
export OPENAI_API_KEY="..."
export OPENAI_BASE_URL="https://api.openai.com/v1"
export OPENAI_MODEL="gpt-4o-mini"
```

Anthropic：

```bash
export ANTHROPIC_API_KEY="..."
export ANTHROPIC_MODEL="claude-sonnet-4-20250514"
```

不要把 API key 写死在源码、提交到 Git 的配置、README 示例或共享日志里。

## 非交互式使用

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

trace 开头会包含 session 信息：

```text
--- SESSION ---
id=<session_id>
dir=<workspace>/.insightagent/sessions
config_files=...
```

列出 sessions：

```bash
python3 -m insightagent.run_task --workspace demo_v5 --list-sessions
```

恢复 session：

```bash
python3 -m insightagent.run_task \
  --workspace demo_v5 \
  --session-id <session_id> \
  --task "继续上一个任务，检查当前文件并总结状态。"
```

## 交互式 CLI

```bash
python3 -m insightagent.cli \
  --provider siliconflow \
  --model "Qwen/Qwen2.5-72B-Instruct" \
  --workspace demo_v5
```

Slash commands：

```text
/help
/status
/cost
/memory
/compact
/clear
/permissions
/export transcript.md
/mcp status
/mcp tools
/mcp restart <server>
/mcp refresh <server>
```

## Session 存储

Session 以 JSON 保存：

```text
<workspace>/.insightagent/sessions/<session_id>.json
```

每个 session 记录：

- session id
- created/updated 时间戳
- metadata
- messages
- assistant tool calls
- tool results
- 用量估算

支持通过命令行导出 Markdown transcript：

```bash
--export-transcript path/to/transcript.md
```

也支持交互式导出：

```text
/export transcript.md
```

## 当前限制

V5.0 已经明显不像 V1-V4 那样偏 demo，但还不是完整 Claude Code 替代品：

- token 用量是按字符估算，不是 provider tokenizer 的精确结果
- session 存储基于 JSON 文件，不支持并发或数据库级管理
- `/compact` 使用 deterministic structural summary，不是 LLM-generated summary
- slash command 有用，但还不是完整 terminal UI
- hook system 尚未实现
- mock parity harness 尚未实现
- LSP diagnostics 只是本地语法检查的 best-effort 版本，不是持久 language-server session
- MCP 已支持基础 runtime layer，但真实第三方 server 的可用性仍取决于本机 Node/npm、网络、远程服务和 server 自身行为
- plugins 和 sub-agent orchestration 仍是后续工作

## 测试

```bash
python3 -m py_compile $(find insightagent tests -name '*.py' -print)
python3 -m unittest discover -s tests -v
```

期望结果：

```text
Ran 75+ tests
OK
```

测试覆盖：

- agent loop
- 任务生命周期状态转移
- self-healing repair prompt
- config merge 优先级
- 项目 memory 注入
- tool output 截断和压缩
- provider message 转换
- session 保存、加载和导出
- slash command 行为
- MCP config、protocol、stdio/http transport、client、manager、adapter 和 `/mcp` 命令
- workspace permission checks
- `edit_file`
- `grep_search`
- `glob_search`
- `git_status` 和 `git_diff`
- `todo_write`
- best-effort `lsp_diagnostics`
- Python 代码分析工具：`parse_ast`、`get_function_signature`、`find_dependencies`、`get_code_metrics`
- 用量估算

## 下一步工作

下一阶段建议推进 **V6.0: Hooks + Audit + Parity Harness**：

- `pre_tool_use`
- `post_tool_use`
- `post_tool_failure`
- structured audit log
- deterministic fake-model scenario runner
- 覆盖 write allowed/denied、grep、repair、compaction、resume 的 parity scenarios

这会让 InsightAgent 更接近 `claw-code-parity` 的工程形态。
