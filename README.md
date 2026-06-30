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
-> 按 tool profile 选择内置工具和显式启用的 MCP servers
-> 组装 system prompt
-> 运行模型/工具循环
-> 注入阶段指导并跟踪任务生命周期
-> 记录每次模型调用的用量
-> 持久化 messages 和 metadata
-> 支持 slash command 检查、导出和压缩
```

## Runtime Harness

当前版本把参考工程 `/home/dinghanchen/stuckin/claw-code-parity` 的 harness 思路改编为 Python 运行时：

- `insightagent/runtime/types.py`：定义 `ToolSpec`、权限、风险等级和结构化 `ToolExecutionResult`
- `insightagent/runtime/permissions.py`：在工具执行前统一做权限裁决
- `insightagent/runtime/command_validation.py`：识别 shell 命令意图，例如 test、build、install、network、mutating、destructive
- `insightagent/runtime/failure_classifier.py`：把失败归类为 code/test/environment/network/permission/timeout 等类型
- `ToolRegistry.execute()`：模型工具调用统一经过 spec、permission、command validation、failure classification 和重复失败熔断
- `JsonlTraceRecorder`：可把每次运行的结构化事件写成 JSONL，供后续统计轨迹成功率和失败类型

非重试型失败，例如网络不可达、环境缺失、权限拒绝，会被标记为不可重复重试；同一个失败工具调用再次出现时，runtime 会直接抑制重复调用并把原因写入 trace。

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

MCP 配置单独存放。非交互式 `run_task` 和交互式 `cli` 都会记录启动命令时所在目录，并按下面顺序读取 MCP 配置。后读取的同名 server 会覆盖先读取的字段：

```text
~/.insightagent/mcp_config.json
<start_dir>/.insightagent/mcp_config.json
<start_dir>/mcp_config.json
<workspace>/.insightagent/mcp_config.json
<workspace>/mcp_config.json
```

这样从项目根目录启动、但把 `--workspace` 指到 `workspaces/<task>` 这类临时工作区时，也能加载项目根目录的 `mcp_config.json`，不需要再把配置复制到每个 workspace 里。读取配置不等于启动 MCP server；默认 `--tool-profile coding-basic` 只暴露核心内置工具。

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

需要 MCP 时显式选择：

```bash
python3 -m insightagent.run_task \
  --tool-profile mcp-playwright \
  --trace-jsonl reports/mcp_tools_trace.jsonl \
  --workspace workspaces/mcp_tools \
  --task "使用 Playwright MCP 打开 https://example.com 并总结页面标题。"
```

也可以用 `--enable-mcp-server github` 或 `--enable-mcp-server all` 只对本次运行启用指定配置。`--allowed-tools read_file,grep_search` 可在当前 profile 内进一步收窄内置工具集合。

详细说明见 [MCP_GUIDE.md](MCP_GUIDE.md)。

## 环境变量

`run_task` 和 `cli` 会自动读取启动目录和 workspace 下的 `.env` 文件。读取规则是：

- 先读取 `<start_dir>/.env`，再读取 `<workspace>/.env`
- 只填充当前环境里还不存在的变量，不覆盖 shell 中已经 export 的变量
- `.env` 已被 `.gitignore` 忽略，避免误提交密钥

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
  --workspace workspaces/card_war \
  --permission-mode workspace-write \
  --export-transcript workspaces/card_war/transcript.md \
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
python3 -m insightagent.run_task --workspace workspaces/card_war --list-sessions
```

恢复 session：

```bash
python3 -m insightagent.run_task \
  --workspace workspaces/card_war \
  --session-id <session_id> \
  --task "继续上一个任务，检查当前文件并总结状态。"
```

## 交互式 CLI

```bash
python3 -m insightagent.cli \
  --provider siliconflow \
  --model "Qwen/Qwen2.5-72B-Instruct" \
  --workspace workspaces/card_war
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
- MCP 已支持基础 runtime layer 和真实 Playwright MCP smoke 路径，但第三方 server 的可用性仍取决于本机 Node/npm、网络、远程服务和 server 自身行为
- plugins 和 sub-agent orchestration 仍是后续工作

## 测试

```bash
python3 -m py_compile $(find insightagent tests -name '*.py' -print)
python3 -m unittest discover -s tests -v
```

期望结果：

```text
Ran 72 tests
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
