# InsightAgent MCP 指南

InsightAgent 通过官方 `langchain-mcp-adapters` 与 MCP SDK 接入 MCP server。自定义 JSON-RPC 客户端、stdio/HTTP 传输实现不再存在。

## 配置位置

MCP 配置按以下顺序合并，后读的同名 server 覆盖前面的字段：

```text
~/.insightagent/mcp_config.json
<start_dir>/.insightagent/mcp_config.json
<start_dir>/mcp_config.json
<workspace>/.insightagent/mcp_config.json
<workspace>/mcp_config.json
```

`<start_dir>` 是启动 `insightagent-run` 或 `insightagent` 时的当前目录。可用 `--config-home` 指定用户配置目录。

```json
{
  "mcpServers": {
    "playwright": {
      "transport": "stdio",
      "command": "npx",
      "args": ["@playwright/mcp@latest"],
      "enabled": true,
      "startup_timeout": 20,
      "request_timeout": 60,
      "tool_prefix": "mcp_playwright"
    }
  }
}
```

支持 `stdio` 和 `streamable_http`。远程 server 可设置 `url` 与 `headers`，密钥应使用 `${ENV_NAME}`，不能写入共享配置。

## 选择与调用

默认 `coding-basic` 不选择 MCP server。使用 MCP profile 或显式 server：

```bash
uv run insightagent-run \
  --workspace /path/to/repository \
  --tool-profile mcp-playwright \
  --task "使用 Playwright MCP 检查页面标题"

uv run insightagent-run \
  --workspace /path/to/repository \
  --enable-mcp-server playwright \
  --task "列出可用 MCP 工具"
```

`--enable-mcp-server` 与 profile 默认 server 取并集；`all` 表示所有配置 server。未知 server 在模型启动前作为配置错误退出。已选择 server 启动失败同样会终止本次运行并返回配置错误，避免模型在缺失工具的表面下继续执行。

MCP tool 名称是 `<tool_prefix>_<tool>`，例如 `mcp_playwright_navigate`。resources 与 prompts 也作为 LangChain 工具暴露。

## 权限、超时与刷新

MCP 工具和内置工具共享工作区权限、路径边界、SWE 任务契约、输出脱敏和 `--max-wall-seconds` 剩余预算。只有声明 `readOnlyHint` 的 MCP 工具可在 `read-only` 模式运行；其他 MCP 工具需要 MCP 权限与工作区写入策略。

交互式 CLI 提供：

```text
/mcp status
/mcp tools
/mcp restart <server>
/mcp refresh <server>
```

重启或刷新完成后，运行器重新获取 MCP tools、重新绑定聊天模型；下一轮图使用新一代工具，不会继续调用陈旧实例。

所有 MCP 生命周期事件、工具调用结果和错误都经过脱敏后写入 Langfuse 与可选 `--trace-jsonl`；单个文本字段受 `--trace-max-chars` 或 `tracing.max_chars` 限制。调用超时、外部取消、重启和关闭均会取消并等待活动任务；若会话收尾无法确认，结果为 `mcp_cancellation_unconfirmed`。
