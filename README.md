# InsightAgent V5.0

InsightAgent 的唯一生产运行时是 LangGraph。LangChain 负责聊天模型和工具抽象，Langfuse 负责模型、图节点和工具决策的可观测性。项目不再保留手写 agent 循环、供应商客户端、JSON 会话或旧追踪器作为兼容执行路径。

## 环境配置

在启动目录或目标工作区创建 `.env`。运行时只使用统一模型变量：

```bash
API_KEY=...
BASE_URL=https://api.siliconflow.cn/v1
MODEL_ID=Qwen/Qwen3.6-35B-A3B

LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_BASE_URL=

# Optional USD rates per 1K tokens, used only for run-manifest estimates
INSIGHTAGENT_INPUT_COST_PER_1K=
INSIGHTAGENT_OUTPUT_COST_PER_1K=
```

`API_KEY`、`BASE_URL`、`MODEL_ID` 分别是访问凭据、兼容 OpenAI API 的地址和模型标识。`--model` 与项目配置可覆盖 `MODEL_ID`，否则使用环境变量。不要配置或依赖供应商专有环境变量。

`.env` 按启动目录、工作区的顺序加载，且不会覆盖已经导出的 shell 变量。Langfuse 变量可选；未配置时图继续运行，只是不发送远程观测。

## 非交互任务

```bash
uv run insightagent-run \
  --workspace /path/to/repository \
  --session-id repair-001 \
  --task "修复失败的测试并运行验证命令"
```

常用参数：

```text
--tool-profile coding-basic|analysis|all|mcp-playwright|mcp-github
--allowed-tools read_file,grep_search
--enable-mcp-server playwright
--permission-mode read-only|workspace-write
--approval-mode deny|interrupt
--execution-mode host|sandbox
--sandbox-image python:3.11-slim
--trust-workspace-mcp
--max-wall-seconds 300
--max-tool-iterations 12
--trace-jsonl reports/repair.jsonl
--trace-max-chars 1000
--no-trace
```

`--no-trace` 只关闭控制台事件渲染，不会关闭 JSONL 调试导出或 Langfuse。`--trace-max-chars`（或配置中的 `tracing.max_chars`）限制 Langfuse、JSONL 和控制台单个文本字段的长度。JSONL 是经脱敏的图事件导出，包含阶段、工具事件、验证记录、最终状态和可用的 Langfuse trace ID。

图在工具执行后先按 `runtime.max_tool_output_chars` 截断模型可见结果，再在下一次模型调用前按 `runtime.compact_tool_output_chars` 压缩历史工具结果，并裁剪完整的历史消息组；system/task 上下文锚点不会被裁掉，也不会留下没有对应工具调用的 `ToolMessage`。provider 返回消息格式错误时，运行时会用最近完整工具组进行一次最小上下文恢复。

配置、模型凭据、工具 profile 或 MCP server 错误退出码为 `2`；图以 `failed` 终止时退出码为 `1`。

`approval_mode=interrupt` 会在写入、执行、MCP 或 external 工具真正产生副作用前暂停，并展示工具名、参数、权限和风险。交互式 CLI 输入 `y`/`approve` 后恢复；非交互 CLI 会以退出码 `2` 输出审批 payload，并可用同一 `--session-id --approval-mode interrupt --resume-approval approve|deny` 恢复。默认 `approval_mode=deny`，不会意外放开副作用。

`execution_mode=sandbox` 使用 Docker 运行 shell/verification：网络关闭、容器根文件系统只读、丢弃 capabilities，并限制 CPU、内存和 PID。Docker 不可用时返回 `sandbox_unavailable`，不会回退到宿主机；默认 `host` 仅适用于受控开发工作区。

## 会话与检查点

LangGraph SQLite 检查点是会话事实来源。默认数据库位于：

```text
<workspace>/.insightagent/sessions/checkpoints.sqlite3
```

可用 `--session-dir` 改写目录。一个 `--session-id` 对应图线程；在同一线程继续任务会保留历史消息，但重置本轮阶段字段。

```bash
# 列出线程，不启动模型或 MCP
uv run insightagent-run --workspace /path/to/repository --list-sessions

# 查看指定检查点，不启动模型或 MCP
uv run insightagent-run \
  --workspace /path/to/repository \
  --session-id repair-001 \
  --checkpoint-id <checkpoint-id>

# 导出当前线程或指定检查点的 Markdown 转录
uv run insightagent-run \
  --workspace /path/to/repository \
  --session-id repair-001 \
  --export-transcript reports/repair-001.md
```

`--checkpoint-id` 必须与 `--session-id` 一起使用。导出指定检查点时同样传入两者。

## 交互式 CLI

```bash
uv run insightagent --workspace /path/to/repository --session-id repair-001
```

交互式进程在 REPL 外创建一个长期 `GraphRunner`，每轮复用检查点、MCP 与模型资源，退出时才关闭它们。

```text
/status
/cost
/memory
/compact
/clear
/permissions
/export [path]
/mcp status
/mcp tools
/mcp restart <server>
/mcp refresh <server>
```

`/mcp restart` 和 `/mcp refresh` 会重建工具表和模型绑定；下一轮图不会调用旧 MCP 工具实例。

## MCP

MCP 配置见 [MCP_GUIDE.md](MCP_GUIDE.md)。默认 `coding-basic` 不启动 MCP。选择 MCP profile 或传入 `--enable-mcp-server` 后，官方 `langchain-mcp-adapters` 和 MCP SDK 负责协议与传输。

出于供应链安全，默认只加载用户级 `~/.insightagent/mcp_config.json`。工作区或启动目录的 `mcp_config.json` 必须显式传入 `--trust-workspace-mcp`，不应把仓库提交的 MCP command 当作管理员批准的 server manifest。

内置工具和 MCP 工具都经过同一权限、工作区、任务契约和剩余时间预算策略。MCP 调用超时或取消时，运行时会等待调用任务与会话结束；无法确认取消会记录为失败，不会伪报成功。

## SWE 风格评测

评测默认在当前进程直接调用图运行器，并总是在图失败、超时或异常后继续执行外部验证、分析部分改动和生成报告：

```bash
uv run python -m insightagent.evals.swe_style \
  --dataset tests/fixtures/swe_style/cases.jsonl \
  --run-id local-eval
```

评测默认从 `.env` 使用 `API_KEY`、`BASE_URL`、`MODEL_ID`。`--subprocess` 是显式隔离选项，运行 `python -m insightagent.cli.run_task`，仍由图驱动。`--fail-on-unresolved` 只在存在未解决的可评测 case 时返回 `1`。

## 验证

```bash
uv sync --extra dev
uv run python -m compileall src tests
uv run pytest -q
uv run python -c "import langgraph, langchain_core, langfuse"
git diff --check
```
