# 工具结构与代码分析能力升级设计

日期：2026-06-02

## 背景

InsightAgent V5.0 目前已经具备一个小而完整的 coding-agent runtime：会话持久化、运行时配置加载、slash commands、用量统计、任务生命周期引导、工作区范围内的工具安全边界，以及围绕核心循环的测试。

和参考项目 `/home/dinghanchen/stuckin/codeagent/Code_Agent` 相比，当前差距并不是“有没有 Agent loop”，而是工程结构、扩展能力和产品化呈现。第一阶段升级应该优先改善工具结构，并补充代码理解工具，同时不改变现有 runtime 契约。MCP、Web UI、prompt 目录迁移和 provider 重构都放到后续阶段。

## 差距审计表

| 能力 | InsightAgent V5.0 现状 | Code_Agent 参照 | 决策 |
| --- | --- | --- | --- |
| Agent 核心循环 | 有 tool-call loop、repair prompt、usage、session | ReAct JSON loop | 保留 InsightAgent 现有 loop |
| 会话持久化 | JSON session、resume、export | conversation history | 保留 InsightAgent session 系统 |
| 配置系统 | 用户/项目/local/CLI 优先级 | `.env` 和 config 示例 | 保留，后续扩展 |
| 工具安全边界 | `ToolContext`、workspace boundary、permission mode | 工具执行更直接 | 保留 InsightAgent 安全模型 |
| 工具组织 | 大多数工具集中在 `insightagent/tools.py` | 拆分为多个 tool 模块 | 第一阶段重构 |
| 代码分析 | 缺少 AST/signature/dependency/metrics 工具 | 有 `code_analysis_tools.py` | 第一阶段新增 |
| MCP | 缺失 | 有 config/client/manager/wrapper | 第二阶段实现 |
| Planner | 有 task phase 引导，没有独立 planner | 有独立 planner | 后续再做 |
| 上下文压缩 | deterministic compaction 和 sliding memory | LLM compressor | 保留现有方案 |
| Provider 结构 | 单个 provider 模块 | clients 分层 | 后续再做 |
| Prompt 管理 | prompt 写在代码里 | `prompts/` 目录 | 后续再做 |
| Web UI | 无 | Next.js frontend | 后续再评估 |
| 测试 | 已有单元测试 | 未观察到测试目录 | 保留并扩展测试优势 |

## 推荐升级路线

### 第一阶段：工具结构与代码分析

把工具拆成 package，新增代码分析工具，保持现有工具名称和 registry 行为，并更新测试与 README。

### 第二阶段：MCP 集成

新增 MCP 配置加载、client 进程管理、manager、tool wrapper、配置示例和使用文档。MCP 涉及 subprocess 和 JSON-RPC 行为，因此这一阶段要设计出可 mock 的边界。

### 第三阶段：产品化完善

把 prompts 移入独立目录，考虑 provider/client 分层，增加 `/tools`、`/mcp` 等更丰富的 slash commands，然后再评估 TUI 或 Web UI。

## 第一阶段目标

1. 让工具代码更容易理解、测试和扩展。
2. 增加代码分析工具，让 Agent 能更好地理解 Python 项目结构。
3. 尽量保持现有 import、测试和行为兼容。
4. 不改变模型循环、provider 行为、配置优先级、session 系统或 permission model。

## 非目标

1. 第一阶段不实现 MCP。
2. 不做 Web UI 或 API server。
3. 不重写 planner。
4. 不重构 provider/client。
5. 不引入 LLM-based compression。
6. 不大改 CLI 交互。

## 架构设计

将 `insightagent/tools.py` 从主要实现文件调整为兼容入口，让它从新的 `insightagent/tools/` package 重新导出公开工具 API。

目标结构：

```text
insightagent/tools/
  __init__.py
  base.py
  execution_tools.py
  file_tools.py
  search_tools.py
  code_analysis_tools.py
  registry.py
```

职责划分：

- `base.py`：`Tool` protocol 和共享 helper。
- `execution_tools.py`：`ExecuteCommandTool`。
- `file_tools.py`：`ReadFileTool`、`WriteFileTool`、`EditFileTool`。
- `search_tools.py`：`GrepSearchTool`、`GlobSearchTool`。
- `code_analysis_tools.py`：Python AST 和 metrics 相关工具。
- `registry.py`：`ToolRegistry`、schema 输出、工具执行分发和默认工具注册。
- `__init__.py`：对外导出已有模块依赖的公开 API。

旧导入路径必须继续可用：

```python
from insightagent.tools import ToolRegistry
```

## 代码分析工具

新增以下工具：

| 工具名 | 用途 | 输入 |
| --- | --- | --- |
| `parse_ast` | 总结 Python 文件的 imports、顶层 classes、functions 和 globals | `path` |
| `get_function_signature` | 返回函数或方法的签名元数据 | `path`, `function_name` |
| `find_dependencies` | 将 import 分类为 stdlib、third-party 或 local-ish | `path` |
| `get_code_metrics` | 返回行数、空行、注释、函数数、类数、import 数 | `path` |

所有工具必须满足：

1. 使用 `ToolContext.resolve_workspace_path`。
2. 在适用场景下拒绝非 Python 文件。
3. 对结构化结果返回 deterministic JSON。
4. 将语法错误作为清晰的工具输出返回，而不是让 agent loop 崩溃。
5. 避免读取二进制样式文件或无边界大文件；尽量复用现有文件大小保护。

## 数据流

1. `run_task.py` 创建 `ToolContext`。
2. `ToolRegistry.default(context)` 或等价默认构造逻辑注册 file、search、execution 和 code-analysis 工具。
3. `agent.py` 从 registry 获取 schemas。
4. provider 收到扩展后的工具列表。
5. tool call 通过 registry 分发到对应工具的 `run(arguments)`。
6. 工具结果继续走现有 truncation、repair、session 和 task-state 逻辑。

## 错误处理

registry 保持当前行为：未知工具和工具异常会转成 error `Message`，而不是导致进程崩溃。

代码分析工具需要对以下情况返回可读错误：

- 文件不存在。
- 路径超出 workspace。
- 路径不是文件。
- 文件不是 Python 文件。
- Python 语法错误。

权限规则仍由 `ToolContext` 控制；代码分析工具只读，不应该调用 `check_write_allowed`。

## 兼容性

现有测试应继续支持：

```python
from insightagent.tools import ToolRegistry
```

现有工具名保持不变：

- `execute_command`
- `read_file`
- `write_file`
- `edit_file`
- `grep_search`
- `glob_search`

新增工具名只做 additive change：

- `parse_ast`
- `get_function_signature`
- `find_dependencies`
- `get_code_metrics`

## 测试计划

新增聚焦测试：

1. Registry 仍暴露现有工具 schemas。
2. Registry 暴露新的代码分析工具。
3. `parse_ast` 能从样例 Python 文件返回 imports、functions、classes 和 globals。
4. `get_function_signature` 能处理普通函数、async 函数、方法、类型注解和缺失名称。
5. `find_dependencies` 在本地测试中能给出足够 deterministic 的分类。
6. `get_code_metrics` 能统计基础文件指标。
7. 非 Python 文件和语法错误会产生清晰输出。
8. 现有单元测试继续通过。

验证命令：

```bash
python3 -m py_compile insightagent/*.py tests/*.py
python3 -m unittest discover -s tests -v
```

当 `insightagent/tools.py` 转为 package 后，py_compile 命令需要扩展，例如：

```bash
python3 -m py_compile $(find insightagent tests -name '*.py' -print)
```

## 文档更新

更新 README 中的 architecture 和 tools 相关说明，覆盖：

- 新的 tool package 结构。
- 四个代码分析工具。
- MCP 仍然是第二阶段计划，不在第一阶段实现。

## 验收标准

1. `python3 -m unittest discover -s tests -v` 通过。
2. `python3 -m py_compile $(find insightagent tests -name '*.py' -print)` 通过。
3. 现有 `insightagent.tools` 公开导入继续可用。
4. 现有工具名和行为继续可用。
5. 新代码分析工具出现在 `ToolRegistry.schemas()` 中。
6. README 反映升级后的工具结构。
