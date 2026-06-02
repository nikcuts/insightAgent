# 工具结构与代码分析能力实现计划

> **给 agentic workers：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 按任务逐步实现本计划。步骤使用 checkbox（`- [ ]`）语法追踪。

**目标：** 将 InsightAgent V5.0 的工具层拆成清晰 package，并新增 Python 代码分析工具。

**架构：** 保持 `insightagent.tools` 公开导入路径兼容，把原 `tools.py` 的实现迁移到 `insightagent/tools/` package。新增代码分析工具复用 `ToolContext` 的 workspace 边界，作为默认 registry 的 additive tools 暴露给 agent loop。

**技术栈：** Python 3.10+ 标准库、`unittest`、`ast`、现有 `ToolContext`、现有 `Message`/`ToolRegistry` 契约。

---

## 文件结构

- 新建：`insightagent/tools/__init__.py`，导出公开工具 API。
- 新建：`insightagent/tools/base.py`，放置 `Tool` protocol 与共享读取 helper。
- 新建：`insightagent/tools/execution_tools.py`，放置 `ExecuteCommandTool`。
- 新建：`insightagent/tools/file_tools.py`，放置 `ReadFileTool`、`WriteFileTool`、`EditFileTool`。
- 新建：`insightagent/tools/search_tools.py`，放置 `GrepSearchTool`、`GlobSearchTool`。
- 新建：`insightagent/tools/code_analysis_tools.py`，放置 `ParseAstTool`、`GetFunctionSignatureTool`、`FindDependenciesTool`、`GetCodeMetricsTool`。
- 新建：`insightagent/tools/registry.py`，放置 `ToolRegistry` 和默认工具注册。
- 删除：`insightagent/tools.py`，因为 Python 不能同时稳定保留同名 module 和 package；改成 package 后 `from insightagent.tools import ToolRegistry` 继续可用。
- 修改：`README.md`，更新架构与工具说明。
- 新建：`tests/test_code_analysis_tools.py`，覆盖新增代码分析工具。
- 修改：`tests/test_extended_tools.py`，确认默认 registry 包含新增工具，同时已有工具名不变。

## Task 1：基线验证

**文件：** 无代码改动。

- [ ] **Step 1：运行现有 py_compile**

运行：

```bash
python3 -m py_compile insightagent/*.py tests/*.py
```

期望：exit 0。

- [ ] **Step 2：运行现有测试**

运行：

```bash
python3 -m unittest discover -s tests -v
```

期望：现有测试全部通过。

## Task 2：写代码分析工具失败测试

**文件：**
- 新建：`tests/test_code_analysis_tools.py`
- 修改：`tests/test_extended_tools.py`

- [ ] **Step 1：新增失败测试文件**

创建 `tests/test_code_analysis_tools.py`。测试通过未来的 `ToolRegistry(context=...)` 调用新增工具，在临时 workspace 中创建样例 Python 文件，并断言以下工具的 JSON 输出：

- `parse_ast`
- `get_function_signature`
- `find_dependencies`
- `get_code_metrics`
- 语法错误处理
- 非 Python 文件处理

- [ ] **Step 2：扩展 registry schema 测试**

修改 `tests/test_extended_tools.py`，让默认 registry 期望工具名包含：

```python
"parse_ast",
"get_function_signature",
"find_dependencies",
"get_code_metrics",
```

- [ ] **Step 3：运行失败测试**

运行：

```bash
python3 -m unittest tests.test_code_analysis_tools -v
python3 -m unittest tests.test_extended_tools -v
```

期望：失败，原因是新工具名或类尚未实现。

## Task 3：拆分工具 package 并保持兼容

**文件：**
- 新建：`insightagent/tools/__init__.py`
- 新建：`insightagent/tools/base.py`
- 新建：`insightagent/tools/execution_tools.py`
- 新建：`insightagent/tools/file_tools.py`
- 新建：`insightagent/tools/search_tools.py`
- 新建：`insightagent/tools/registry.py`
- 删除：`insightagent/tools.py`

- [ ] **Step 1：迁移现有工具代码**

将现有工具类迁移到聚焦模块中，不改变工具名称、schema 或 `run` 行为。

- [ ] **Step 2：导出兼容 API**

`insightagent/tools/__init__.py` 必须导出：

```python
Tool
ExecuteCommandTool
ReadFileTool
WriteFileTool
EditFileTool
GrepSearchTool
GlobSearchTool
ToolRegistry
```

- [ ] **Step 3：运行兼容测试**

运行：

```bash
python3 -m unittest tests.test_agent_loop tests.test_extended_tools tests.test_tool_context -v
```

期望：依赖现有工具行为的测试继续通过；如果有失败，只能来自尚未实现的代码分析工具。

## Task 4：实现代码分析工具

**文件：**
- 新建：`insightagent/tools/code_analysis_tools.py`
- 修改：`insightagent/tools/__init__.py`
- 修改：`insightagent/tools/registry.py`

- [ ] **Step 1：实现只读 Python 文件解析 helper**

新增 helper：通过 `ToolContext` 解析 workspace 路径，验证文件存在、是普通文件、后缀为 `.py`、不是二进制样式文件，并遵守 `context.max_read_chars`。

- [ ] **Step 2：实现 `parse_ast`**

返回 deterministic JSON，结构为：

```json
{
  "file": "relative/path.py",
  "imports": [],
  "classes": [],
  "functions": [],
  "global_variables": []
}
```

- [ ] **Step 3：实现 `get_function_signature`**

返回 deterministic JSON，包含顶层函数和类方法的 `name`、`signature`、`line`、`docstring`、`is_async`、`kind`。

- [ ] **Step 4：实现 `find_dependencies`**

返回 deterministic JSON，包含 `stdlib`、`third_party`、`local`、`relative` import 列表。

- [ ] **Step 5：实现 `get_code_metrics`**

返回 deterministic JSON，包含行数和 AST 计数。

- [ ] **Step 6：注册并导出新工具**

默认 registry 必须包含四个新工具，`__init__.py` 必须导出它们的类。

- [ ] **Step 7：运行新增测试**

运行：

```bash
python3 -m unittest tests.test_code_analysis_tools tests.test_extended_tools -v
```

期望：通过。

## Task 5：更新 README

**文件：**
- 修改：`README.md`

- [ ] **Step 1：更新架构小节**

记录新的 `insightagent/tools/` package 和各模块职责。

- [ ] **Step 2：更新 V5 新增能力或工具说明**

加入四个代码分析工具，并说明 MCP 仍是后续工作。

- [ ] **Step 3：运行文档检查**

运行：

```bash
rg -n "code analysis|代码分析|insightagent/tools" README.md
```

期望：输出包含新的文档行。

## Task 6：完整验证与提交

**文件：** 所有变更文件。

- [ ] **Step 1：运行 py_compile**

运行：

```bash
python3 -m py_compile $(find insightagent tests -name '*.py' -print)
```

期望：exit 0。

- [ ] **Step 2：运行完整测试**

运行：

```bash
python3 -m unittest discover -s tests -v
```

期望：全部测试通过。

- [ ] **Step 3：查看 git diff**

运行：

```bash
git status --short
git diff --stat
```

期望：只有计划内文件发生变化。

- [ ] **Step 4：提交**

运行：

```bash
git add README.md insightagent tests docs/superpowers/plans/2026-06-02-tool-structure-code-analysis.md
git commit -m "Refactor tools and add code analysis"
```
