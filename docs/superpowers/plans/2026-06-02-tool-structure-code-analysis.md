# 工具结构与代码分析能力实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 InsightAgent V5.0 的工具层拆成清晰 package，并新增 Python 代码分析工具。

**Architecture:** 保持 `insightagent.tools` 公开导入路径兼容，把原 `tools.py` 的实现迁移到 `insightagent/tools/` package。新增代码分析工具复用 `ToolContext` 的 workspace 边界，作为默认 registry 的 additive tools 暴露给 agent loop。

**Tech Stack:** Python 3.10+ standard library, `unittest`, `ast`, existing `ToolContext`, existing `Message`/`ToolRegistry` contracts.

---

## 文件结构

- Create: `insightagent/tools/__init__.py`，导出公开工具 API。
- Create: `insightagent/tools/base.py`，放置 `Tool` protocol 与共享读取 helper。
- Create: `insightagent/tools/execution_tools.py`，放置 `ExecuteCommandTool`。
- Create: `insightagent/tools/file_tools.py`，放置 `ReadFileTool`、`WriteFileTool`、`EditFileTool`。
- Create: `insightagent/tools/search_tools.py`，放置 `GrepSearchTool`、`GlobSearchTool`。
- Create: `insightagent/tools/code_analysis_tools.py`，放置 `ParseAstTool`、`GetFunctionSignatureTool`、`FindDependenciesTool`、`GetCodeMetricsTool`。
- Create: `insightagent/tools/registry.py`，放置 `ToolRegistry` 和默认工具注册。
- Delete: `insightagent/tools.py`，因为 Python 不能同时稳定保留同名 module 和 package；改成 package 后 `from insightagent.tools import ToolRegistry` 继续可用。
- Modify: `README.md`，更新架构与工具说明。
- Create: `tests/test_code_analysis_tools.py`，覆盖新增代码分析工具。
- Modify: `tests/test_extended_tools.py`，确认默认 registry 包含新增工具，同时已有工具名不变。

## Task 1: 基线验证

**Files:** 无代码改动。

- [ ] **Step 1: 运行现有 py_compile**

Run:

```bash
python3 -m py_compile insightagent/*.py tests/*.py
```

Expected: exit 0。

- [ ] **Step 2: 运行现有测试**

Run:

```bash
python3 -m unittest discover -s tests -v
```

Expected: 现有测试全部通过。

## Task 2: 写代码分析工具失败测试

**Files:**
- Create: `tests/test_code_analysis_tools.py`
- Modify: `tests/test_extended_tools.py`

- [ ] **Step 1: 新增失败测试文件**

Create `tests/test_code_analysis_tools.py` with tests that import the future tools through `ToolRegistry.default(context)`, create sample Python files under a temp workspace, and assert JSON output for:

- `parse_ast`
- `get_function_signature`
- `find_dependencies`
- `get_code_metrics`
- syntax error handling
- non-Python file handling

- [ ] **Step 2: 扩展 registry schema 测试**

Modify `tests/test_extended_tools.py` so the default registry expected names include:

```python
"parse_ast",
"get_function_signature",
"find_dependencies",
"get_code_metrics",
```

- [ ] **Step 3: 运行失败测试**

Run:

```bash
python3 -m unittest tests.test_code_analysis_tools -v
python3 -m unittest tests.test_extended_tools -v
```

Expected: FAIL because the new tool names/classes are not implemented yet.

## Task 3: 拆分工具 package 并保持兼容

**Files:**
- Create: `insightagent/tools/__init__.py`
- Create: `insightagent/tools/base.py`
- Create: `insightagent/tools/execution_tools.py`
- Create: `insightagent/tools/file_tools.py`
- Create: `insightagent/tools/search_tools.py`
- Create: `insightagent/tools/registry.py`
- Delete: `insightagent/tools.py`

- [ ] **Step 1: 迁移现有工具代码**

Move existing tool classes into focused modules without changing tool names, schemas, or run behavior.

- [ ] **Step 2: 导出兼容 API**

`insightagent/tools/__init__.py` must export:

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

- [ ] **Step 3: 运行兼容测试**

Run:

```bash
python3 -m unittest tests.test_agent_loop tests.test_extended_tools tests.test_tool_context -v
```

Expected: tests that rely on existing tool behavior pass except failures specifically caused by missing code-analysis tools.

## Task 4: 实现代码分析工具

**Files:**
- Create: `insightagent/tools/code_analysis_tools.py`
- Modify: `insightagent/tools/__init__.py`
- Modify: `insightagent/tools/registry.py`

- [ ] **Step 1: 实现只读 Python 文件解析 helper**

Add helper logic that resolves workspace paths through `ToolContext`, verifies the file exists, is a file, has `.py` suffix, is not binary-looking, and respects `context.max_read_chars`.

- [ ] **Step 2: 实现 `parse_ast`**

Return deterministic JSON containing:

```json
{
  "file": "relative/path.py",
  "imports": [],
  "classes": [],
  "functions": [],
  "global_variables": []
}
```

- [ ] **Step 3: 实现 `get_function_signature`**

Return deterministic JSON with `name`, `signature`, `line`, `docstring`, `is_async`, and `kind` for top-level functions and class methods.

- [ ] **Step 4: 实现 `find_dependencies`**

Return deterministic JSON with `stdlib`, `third_party`, `local`, and `relative` import lists.

- [ ] **Step 5: 实现 `get_code_metrics`**

Return deterministic JSON with line counts and AST counts.

- [ ] **Step 6: 注册并导出新工具**

Default registry must include the four new tools, and `__init__.py` must export their classes.

- [ ] **Step 7: 运行新增测试**

Run:

```bash
python3 -m unittest tests.test_code_analysis_tools tests.test_extended_tools -v
```

Expected: pass。

## Task 5: 更新 README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: 更新 Architecture 小节**

Document the new `insightagent/tools/` package and responsibilities.

- [ ] **Step 2: 更新 What V5 Adds 或工具说明**

Add the four code-analysis tools and state that MCP remains future work.

- [ ] **Step 3: 运行文档无须构建检查**

Run:

```bash
rg -n "code analysis|代码分析|insightagent/tools" README.md
```

Expected: output includes the new documentation lines.

## Task 6: 完整验证与提交

**Files:** all changed files.

- [ ] **Step 1: 运行 py_compile**

Run:

```bash
python3 -m py_compile $(find insightagent tests -name '*.py' -print)
```

Expected: exit 0。

- [ ] **Step 2: 运行完整测试**

Run:

```bash
python3 -m unittest discover -s tests -v
```

Expected: all tests pass。

- [ ] **Step 3: 查看 git diff**

Run:

```bash
git status --short
git diff --stat
```

Expected: only planned files changed.

- [ ] **Step 4: 提交**

Run:

```bash
git add README.md insightagent tests docs/superpowers/plans/2026-06-02-tool-structure-code-analysis.md
git commit -m "Refactor tools and add code analysis"
```
