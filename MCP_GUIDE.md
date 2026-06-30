# InsightAgent MCP 使用指南

InsightAgent V5.0 已经内置 MCP Runtime Layer，可以把外部 MCP server 暴露的 `tools`、`resources` 和 `prompts` 接入当前 agent loop。

当前支持两种标准传输：

- `stdio`：适合本地 MCP server，例如 Playwright、filesystem、sqlite。
- `streamable_http`：适合远程 MCP endpoint。

旧版 HTTP+SSE 传输不是默认支持范围。

## 配置加载顺序

InsightAgent 会按下面顺序读取 MCP 配置，后面的同名 server 覆盖前面的配置：

```text
~/.insightagent/mcp_config.json
<start_dir>/.insightagent/mcp_config.json
<start_dir>/mcp_config.json
<workspace>/.insightagent/mcp_config.json
<workspace>/mcp_config.json
```

`<start_dir>` 是运行 `python3 -m insightagent.run_task` 或 `python3 -m insightagent.cli` 时所在的目录。这个设计是为了支持常见运行方式：在项目根目录放真实 `mcp_config.json`，同时把 `--workspace` 指到 `workspaces/<task>` 这类临时工作区。这样 MCP 配置仍会被加载，不需要复制到每个 workspace。

推荐把团队可共享的 MCP server 示例放在项目配置里，把真实密钥放在环境变量或本机 local 配置中。

## `.env` 自动加载

`run_task` 和 `cli` 会自动读取：

```text
<start_dir>/.env
<workspace>/.env
```

读取 `.env` 时只处理简单的 `KEY=VALUE` 行，支持单引号或双引号包裹值，不覆盖 shell 中已经存在的环境变量。`.env` 已在 `.gitignore` 中忽略，不要把真实 API key 写进 README、提交记录或共享日志。

## stdio 示例

项目根目录创建 `mcp_config.json`：

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

如果服务器没有全局 `npx`，可以直接写绝对路径：

```json
{
  "mcpServers": {
    "playwright": {
      "transport": "stdio",
      "command": "/home/dinghanchen/.local/nodejs/bin/npx",
      "args": [
        "-y",
        "@playwright/mcp@latest",
        "--headless",
        "--executable-path",
        "/home/dinghanchen/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome"
      ],
      "enabled": true,
      "startup_timeout": 45,
      "request_timeout": 90,
      "tool_prefix": "mcp_playwright"
    }
  }
}
```

上面的 `--executable-path` 是一个实用绕过方案：当 Playwright MCP 自己下载浏览器失败或网络较慢时，可以使用已经安装好的 Chromium for Testing。

启动非交互任务时，InsightAgent 会读取 MCP 配置，但只有显式选择 MCP profile 或 server 时才会启动对应 server：

```bash
python3 -m insightagent.run_task \
  --tool-profile mcp-playwright \
  --workspace workspaces/mcp_tools \
  --task "列出当前可用 MCP 工具。"
```

默认 `--tool-profile coding-basic` 不启动 MCP。也可以使用 `--enable-mcp-server playwright` 或 `--enable-mcp-server all` 对本次运行启用配置中的 server。

## Streamable HTTP 示例

```json
{
  "mcpServers": {
    "remote-docs": {
      "transport": "streamable_http",
      "url": "https://example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${REMOTE_MCP_TOKEN}"
      },
      "enabled": true,
      "request_timeout": 60,
      "tool_prefix": "mcp_docs"
    }
  }
}
```

运行前设置环境变量：

```bash
export REMOTE_MCP_TOKEN="..."
```

不要把真实 token 写进仓库里的 JSON 文件。

## 工具命名

MCP tools 会被包装成 InsightAgent 工具：

```text
mcp_<server>_<tool>
```

如果配置了 `tool_prefix`，则使用：

```text
<tool_prefix>_<tool>
```

例如 Playwright 的 `navigate` 可能会暴露为：

```text
mcp_playwright_navigate
```

MCP resources 和 prompts 也会被包装为工具：

```text
<prefix>_list_resources
<prefix>_read_resource
<prefix>_list_prompts
<prefix>_get_prompt
```

## CLI 命令

交互式 CLI 支持：

```text
/mcp status
/mcp tools
/mcp restart <server>
/mcp refresh <server>
```

示例：

```text
/mcp status
/mcp tools
/mcp restart playwright
```

## 安全说明

- 使用 `${ENV_NAME}` 引用密钥。
- trace、status 和错误输出会尽量脱敏敏感 header/env。
- 单个 MCP server 启动失败不会影响内置工具继续使用。
- MCP 工具调用失败会作为工具错误返回给 agent，由现有 repair loop 处理。

## 可选 smoke test

如果本机已经安装 Node.js/npm，并且已经准备好 Playwright Chromium，可以尝试真实 MCP smoke test：

```bash
cd /home/dinghanchen/stuckin/insightagent_v5

PATH=$HOME/.local/nodejs/bin:$PATH python3 -m insightagent.run_task \
  --provider siliconflow \
  --model "Qwen/Qwen2.5-72B-Instruct" \
  --timeout 120 \
  --max-tool-iterations 8 \
  --trace-max-chars 3000 \
  --tool-profile mcp-playwright \
  --workspace workspaces/mcp_smoke \
  --task "请必须调用 mcp_playwright_browser_navigate 打开 https://example.com，然后调用 mcp_playwright_browser_snapshot 读取页面快照。不要只描述工具调用，必须实际调用工具。最后总结页面标题和你调用过的 MCP 工具。"
```

成功时 trace 中应能看到类似工具调用：

```text
mcp_playwright_browser_navigate
mcp_playwright_browser_snapshot
Page Title: Example Domain
```

也可以做一个更短的工具列表检查：

```bash
python3 -m insightagent.run_task \
  --tool-profile mcp-playwright \
  --workspace workspaces/mcp_smoke \
  --task "列出当前可用 MCP 工具，并尝试使用 Playwright 打开 https://example.com 获取页面标题。"
```

这个 smoke test 依赖外部环境，不属于默认单元测试。
