# MCP Runtime Layer 成品化集成设计

日期：2026-06-02

## 背景

InsightAgent V5.0 已经完成第一阶段工具结构升级：内置工具被拆分为 `insightagent/tools/` package，并新增 Python 代码分析工具。下一阶段不应该只做一个“能启动几个 MCP server 的 demo”，而应该把 MCP 作为 InsightAgent 的正式扩展层接入。

本设计目标是新增一个成品化的 MCP Runtime Layer，让 InsightAgent 能通过标准 MCP 协议接入本地和远程工具、资源与提示模板，同时保留当前 runtime 的核心优点：会话持久化、工具安全边界、repair loop、trace、配置优先级和测试体系。

本设计参考 MCP 2025-06-18 规范：

- Transport：标准传输包括 `stdio` 和 `Streamable HTTP`。
- Lifecycle：连接必须先 `initialize`，完成能力协商后发送 `notifications/initialized`，再进入正常操作。
- Server primitives：MCP server 可以暴露 `tools`、`resources` 和 `prompts`。

参考链接：

- https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
- https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle
- https://modelcontextprotocol.io/specification/2025-06-18/server/tools
- https://modelcontextprotocol.io/specification/2025-06-18/server/prompts

## 目标

1. 新增独立的 `insightagent/mcp/` 子系统，作为正式 runtime 能力，而不是临时工具包装。
2. 支持 `stdio` 和 `Streamable HTTP` 两种标准 MCP transport。
3. 支持 MCP `tools`、`resources`、`prompts` 三类 server primitives。
4. 将 MCP tools 适配为 InsightAgent 当前 `Tool` protocol，使 agent loop 不需要重写。
5. 在 CLI 和 non-interactive runner 中自动加载启用的 MCP server。
6. 增加 `/mcp` slash command，让用户能查看 server 状态、工具列表和重启 server。
7. 提供中文 `MCP_GUIDE.md`、`mcp_config.json.example` 和 README 更新。
8. 建立 fake MCP server 测试，保证日常测试不依赖真实 npm 包、外部网络或第三方服务。

## 非目标

1. 本阶段不开发 MCP server，只实现 InsightAgent 作为 MCP client/host 的集成能力。
2. 本阶段不实现旧版 HTTP+SSE 兼容传输；如果未来遇到必须使用旧 server 的场景，再单独评估。
3. 本阶段不实现 MCP client-side sampling、elicitation 或 roots 的完整高级能力，只保留可扩展接口。
4. 本阶段不把 MCP 配置合并进主 `config.json` 的复杂层级；MCP 使用独立配置文件。
5. 本阶段不构建 Web UI 或 marketplace，只提供 CLI 可观测能力和配置示例。

## 总体架构

新增目录：

```text
insightagent/mcp/
  __init__.py
  config.py          # MCP 配置读取、合并、校验、环境变量展开
  protocol.py        # JSON-RPC 请求、响应、通知、错误和协议版本
  transports.py      # StdioTransport、StreamableHttpTransport
  client.py          # 单个 MCP server 的协议客户端
  manager.py         # 多 server 生命周期、状态、重启、工具刷新
  adapters.py        # MCP tool/resource/prompt 到 InsightAgent Tool 的适配
  errors.py          # MCP 专用异常
```

职责边界：

- `insightagent/tools/` 继续只管理内置工具。
- `insightagent/mcp/` 负责外部 MCP server 的配置、连接、协议通信、生命周期和适配。
- `ToolRegistry` 继续对 agent loop 暴露统一工具 schema 和 `run(name, arguments)`。
- `run_task.py`、`cli.py` 只负责在启动时创建 MCP manager，并将 MCP tools 合并进 registry。

数据流：

```text
加载 runtime config
-> 创建 ToolContext
-> 加载内置 default_tools
-> 读取 MCP 配置
-> 启动 enabled MCP servers
-> initialize + notifications/initialized
-> 读取 tools/resources/prompts
-> 将 MCP 能力适配成 InsightAgent tools
-> 创建 ToolRegistry
-> 运行 agent loop
-> 任务结束或 CLI 退出时关闭 MCP manager
```

## 配置设计

MCP 使用独立配置文件。加载顺序：

```text
~/.insightagent/mcp_config.json
<workspace>/.insightagent/mcp_config.json
<workspace>/mcp_config.json
```

后面的配置覆盖前面的同名 server。这样可以让用户在全局配置常用 server，在项目里覆盖启用状态或参数。

示例：

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
    },
    "remote-docs": {
      "transport": "streamable_http",
      "url": "https://example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${REMOTE_MCP_TOKEN}"
      },
      "enabled": false,
      "request_timeout": 60,
      "tool_prefix": "mcp_docs"
    }
  }
}
```

字段规则：

| 字段 | 说明 |
| --- | --- |
| `transport` | `stdio` 或 `streamable_http`，缺省为 `stdio` |
| `command` | `stdio` server 启动命令 |
| `args` | `stdio` server 启动参数 |
| `env` | `stdio` server 环境变量，支持 `${VAR}` 展开 |
| `url` | `streamable_http` MCP endpoint |
| `headers` | HTTP 请求头，支持 `${VAR}` 展开 |
| `enabled` | 是否启用，缺省为 `true` |
| `startup_timeout` | server 启动和初始化超时 |
| `request_timeout` | 单次 MCP 请求超时 |
| `tool_prefix` | 对外工具名前缀，缺省为 `mcp_<server>` |
| `disabled_tools` | 禁用指定 MCP tool |

安全规则：

1. 配置中的环境变量值允许从当前环境展开。
2. trace、日志、错误输出不得打印敏感 header 或 env 的完整值。
3. `enabled=false` 的 server 不启动、不出现在 `/mcp status` 的 running 列表中。
4. 缺失必需环境变量时，该 server 启动失败，但不影响内置工具和其他 MCP server。

## 协议设计

`protocol.py` 负责构建和解析 JSON-RPC 2.0 消息：

- request：包含 `jsonrpc`、`id`、`method`、可选 `params`。
- notification：包含 `jsonrpc`、`method`、可选 `params`，没有 `id`。
- response：包含 `jsonrpc`、`id`、`result` 或 `error`。

默认协议版本：

```text
2025-06-18
```

初始化流程：

```text
client -> initialize
server -> InitializeResult
client -> notifications/initialized
client -> tools/list
client -> resources/list
client -> prompts/list
```

如果 server 返回的协议版本不在 InsightAgent 支持列表中，client 应断开连接，并将该 server 标记为 `failed`。

## Transport 设计

### StdioTransport

职责：

1. 使用 `subprocess.Popen` 启动 server。
2. 通过 stdin/stdout 发送和接收 newline-delimited JSON-RPC 消息。
3. 捕获 stderr 到有限长度 ring buffer，供 trace 和 `/mcp status` 显示摘要。
4. 支持 request timeout。
5. stop 时先关闭 stdin，再等待进程退出，超时后 terminate，再超时后 kill。

约束：

- server stdout 只能包含 MCP JSON-RPC 消息。
- stderr 可作为日志捕获，但不进入模型上下文，除非工具失败需要摘要。
- stdout 读取必须在后台线程中进行，避免工具调用时阻塞。

### StreamableHttpTransport

职责：

1. 对 MCP endpoint 发送 HTTP POST JSON-RPC 消息。
2. 请求头包含 `Accept: application/json, text/event-stream`。
3. 初始化后，后续请求包含协商出的 `MCP-Protocol-Version`。
4. 如果 server 返回 `Mcp-Session-Id`，后续请求携带该 header。
5. 支持 `application/json` 响应。
6. 支持 `text/event-stream` 响应，至少能读取与当前 request 对应的 JSON-RPC response。

本阶段不做独立后台 GET SSE 长连接监听。原因是 InsightAgent 当前 agent loop 是工具调用驱动，先支持请求响应路径即可；后台 server-to-client notification 可以作为后续增强。

## MCP Client 设计

`MCPClient` 管理单个 server：

```text
created -> starting -> initialized -> running -> stopped
                         |
                         -> failed
```

核心方法：

| 方法 | 说明 |
| --- | --- |
| `start()` | 建立 transport 并完成 initialize |
| `stop()` | 关闭 transport |
| `refresh_capabilities()` | 读取 tools/resources/prompts |
| `call_tool(name, arguments)` | 调用 `tools/call` |
| `list_resources()` | 调用 `resources/list` |
| `read_resource(uri)` | 调用 `resources/read` |
| `list_prompts()` | 调用 `prompts/list` |
| `get_prompt(name, arguments)` | 调用 `prompts/get` |
| `status()` | 返回 server 状态、能力、错误摘要 |

client 记录：

- server name
- transport type
- negotiated protocol version
- server capabilities
- serverInfo
- instructions
- tools/resources/prompts cache
- last_error
- stderr summary

## Manager 设计

`MCPManager` 管理多个 client：

| 方法 | 说明 |
| --- | --- |
| `start_enabled()` | 启动所有 enabled server |
| `stop_all()` | 关闭所有 server |
| `restart_server(name)` | 重启指定 server |
| `get_tools()` | 返回已适配的 InsightAgent tools |
| `status()` | 返回所有 server 状态 |
| `refresh_server(name)` | 重新读取 server 能力 |

启动策略：

1. enabled server 逐个启动。
2. 单个 server 失败不阻止其他 server。
3. 启动失败的 server 进入 `failed` 状态，记录错误摘要。
4. manager 对外提供已成功运行 server 的工具。

工具名冲突策略：

1. 默认名称为 `mcp_<server>_<tool>`。
2. 如果配置了 `tool_prefix`，名称为 `<tool_prefix>_<tool>`。
3. 如果最终名称与内置工具或其他 MCP 工具冲突，后启动的工具不注册，并记录冲突错误。

## Adapter 设计

MCP tools 适配为当前 `Tool` protocol：

```python
class Tool(Protocol):
    name: str
    description: str
    input_schema: dict[str, Any]

    def run(self, arguments: dict[str, Any]) -> str:
        ...
```

映射规则：

| MCP 字段 | InsightAgent 字段 |
| --- | --- |
| `tool.name` | `mcp_<server>_<tool>` |
| `tool.description` | `[MCP:<server>] <description>` |
| `tool.inputSchema` | `input_schema` |
| `tools/call` result | `run()` 返回字符串 |

MCP result 转字符串规则：

1. `content[].type == "text"`：拼接 text。
2. `content[].type == "resource"` 或 resource link：输出 URI、mimeType 和简短摘要。
3. 存在 `structuredContent`：以 JSON 格式追加。
4. `isError=true`：返回带错误标记的字符串，让 agent repair loop 可识别。

Resources 和 prompts 也通过 adapter 暴露为工具：

| 工具名 | MCP 方法 |
| --- | --- |
| `<prefix>_list_resources` | `resources/list` |
| `<prefix>_read_resource` | `resources/read` |
| `<prefix>_list_prompts` | `prompts/list` |
| `<prefix>_get_prompt` | `prompts/get` |

这样做的原因是当前 InsightAgent agent loop 只理解 tools。先把 resources/prompts 映射为 tools，可以在不重写 provider 和 agent loop 的前提下支持 MCP 三类能力。

## CLI 和 Runner 集成

`run_task.py`：

1. 加载 runtime config。
2. 创建 `ToolContext`。
3. 加载内置工具。
4. 加载 MCP 配置并启动 enabled server。
5. 将 MCP tools 合并到 registry。
6. 运行任务。
7. `finally` 中关闭 MCP manager。

`cli.py`：

1. 启动时加载并启动 MCP manager。
2. 创建 agent 时注入合并后的 registry。
3. 退出 CLI 时关闭 MCP manager。
4. 增加 `/mcp` slash command。

Slash command：

```text
/mcp status
/mcp tools
/mcp restart <server>
/mcp refresh <server>
```

输出应保持中文，并避免打印密钥。

## Trace 和可观测性

新增 trace event：

| event | 说明 |
| --- | --- |
| `mcp_config_loaded` | 读取了哪些 MCP 配置文件 |
| `mcp_server_starting` | server 开始启动 |
| `mcp_server_started` | server 启动成功，包含工具/资源/prompt 数量 |
| `mcp_server_failed` | server 启动失败 |
| `mcp_tool_call` | MCP 工具调用开始 |
| `mcp_tool_result` | MCP 工具调用结束，包含耗时和是否错误 |
| `mcp_server_stopped` | server 停止 |

trace 只记录摘要，不记录敏感 env/header 原文。

## 错误处理

错误类型：

| 类型 | 说明 |
| --- | --- |
| `MCPConfigError` | 配置格式错误或必需字段缺失 |
| `MCPTransportError` | transport 启动、连接或读写失败 |
| `MCPProtocolError` | JSON-RPC 格式、版本协商或 capability 错误 |
| `MCPRequestTimeout` | 单次请求超时 |
| `MCPToolError` | MCP tool 返回 `isError=true` 或调用失败 |

策略：

1. 配置文件 JSON 语法错误应直接报错，因为这通常是用户需要修复的本地配置问题。
2. 单个 server 启动失败不终止整个 agent。
3. MCP 工具调用失败返回工具错误字符串，交给现有 repair loop 处理。
4. HTTP 404 session 过期时，client 重新 initialize 一次；再次失败则标记 server failed。
5. 所有 request 都必须有 timeout，避免 agent 卡死。

## 测试计划

新增测试文件：

```text
tests/test_mcp_config.py
tests/test_mcp_protocol.py
tests/test_mcp_stdio_transport.py
tests/test_mcp_http_transport.py
tests/test_mcp_client.py
tests/test_mcp_manager.py
tests/test_mcp_adapters.py
tests/test_mcp_slash_commands.py
```

测试原则：

1. 日常单元测试不依赖真实 `npx`、Playwright、Context7 或外部网络。
2. 使用 fake stdio MCP server 脚本验证 initialize、initialized、tools/list、tools/call。
3. 使用本地 fake HTTP server 验证 Streamable HTTP POST、session header 和 JSON 响应。
4. SSE 响应用最小事件流测试，确保能读到当前 request 的 response。
5. 测试敏感 env/header 在 status 和 trace 中被脱敏。
6. 测试 server 启动失败不会影响内置工具。
7. 测试工具名冲突时后注册 MCP 工具被跳过。
8. 测试 `/mcp status`、`/mcp tools`、`/mcp restart <server>` 的中文输出。

验证命令：

```bash
python3 -m py_compile $(find insightagent tests -name '*.py' -print)
python3 -m unittest discover -s tests -v
```

可选 smoke test：

```bash
python3 -m insightagent.run_task \
  --workspace demo_mcp \
  --task "列出当前可用 MCP 工具，并用 Playwright 打开 https://example.com 获取页面标题。"
```

该 smoke test 不进入默认 CI，因为它依赖 Node/npm、网络和真实 MCP server。

## 文档计划

新增中文文档：

```text
MCP_GUIDE.md
mcp_config.json.example
```

README 更新：

1. 架构图增加 `mcp/`。
2. V5 新增能力增加 MCP Runtime Layer。
3. 配置章节增加 MCP 配置入口。
4. CLI 章节增加 `/mcp` 命令。
5. 工具章节说明 MCP 工具命名规则。

## 验收标准

1. `insightagent/mcp/` 子系统存在，并按本设计拆分职责。
2. `stdio` MCP server 能完成 initialize、initialized、tools/list、tools/call。
3. `streamable_http` MCP server 能完成 initialize、initialized、tools/list、tools/call。
4. MCP resources 和 prompts 能通过适配工具访问。
5. `run_task.py` 和 `cli.py` 能自动加载 enabled MCP server。
6. CLI 支持 `/mcp status`、`/mcp tools`、`/mcp restart <server>`。
7. MCP server 启动失败不会影响内置工具使用。
8. MCP 工具调用失败能进入现有 repair loop。
9. 敏感 env/header 不出现在 trace、status 或错误输出中。
10. 所有新增和既有单元测试通过。
11. README、`MCP_GUIDE.md`、`mcp_config.json.example` 均为中文说明。

## 分阶段实现建议

虽然目标是成品化，但实现仍应按可验证切片推进：

### 切片一：配置、协议和 stdio 基础

实现配置加载、JSON-RPC protocol、stdio transport、initialize 流程和 tools/list/tools/call。完成 fake stdio server 测试。

### 切片二：Manager、adapter 和 runner/CLI 接入

实现 MCPManager、tool adapter、resources/prompts adapter，并接入 `run_task.py`、`cli.py` 和 `/mcp` slash command。

### 切片三：Streamable HTTP 和可观测性

实现 Streamable HTTP transport、session header、SSE response 读取、trace event、敏感信息脱敏和 HTTP fake server 测试。

### 切片四：文档、示例和 smoke test

新增中文 MCP 文档、配置示例、README 更新，以及可选 Playwright smoke test 说明。

这种切片方式不是把目标降级为 demo，而是让成品化能力每一步都可以被测试和提交。
