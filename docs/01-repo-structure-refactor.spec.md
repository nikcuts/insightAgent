# 规格文档：仓库目录结构重构（src 布局 + 领域分层）

- **任务编号**：01
- **类型**：工程结构重构（不改变运行时行为）
- **状态**：待评审
- **创建日期**：2026-06-30

---

## 1. 背景与问题

`insightagent_v5/insightagent/` 当前采用「平铺」结构：17 个功能模块 + `__init__.py`（共 18 个顶层 `.py`）与 3 个已分好的子包（`mcp/`、`runtime/`、`tools/`）混在同一目录下，职责边界不清晰，文件堆在一起难以维护。

参考项目 `claw-code-parity` 的 Rust 侧采用清晰的领域分层（`crates/{api, runtime, tools, commands, telemetry, cli}`），Python 侧使用 `src/` 布局。本任务参照该工程实践，对 `insightagent` 进行**纯结构性重组**。

### 当前文件清单（package 内）

顶层模块：`agent.py`、`cli.py`、`config.py`、`context.py`、`memory.py`、`messages.py`、`providers.py`、`resilience.py`、`run_task.py`、`session.py`、`slash_commands.py`、`smoke.py`、`task_state.py`、`tool_context.py`、`tool_profiles.py`、`trace.py`、`usage.py`、`__init__.py`

已有子包：`mcp/`（adapters、client、config、errors、manager、protocol、transports）、`runtime/`（command_validation、failure_classifier、permissions、types）、`tools/`（base、code_analysis_tools、execution_tools、file_tools、registry、search_tools、state_tools）

### 关键事实（来自代码扫描）

- **零三方依赖**：package 仅使用 Python 标准库（22 个 stdlib 模块），无任何第三方包。
- **测试**：`tests/` 下 29 个 `test_*.py`，全部以顶层包形式 `from insightagent... import ...` 导入。
- **无打包/测试配置**：仓库无 `pyproject.toml`、`setup.py`、`conftest.py`、`pytest.ini`。当前测试能 import `insightagent` 仅因为在仓库根目录运行 pytest 时根目录被加入 `sys.path`。
- **内部依赖无环**：已绘出完整 import 图（见附录 A），迁移安全。
- **入口函数**：`cli.py` 与 `run_task.py` 均有 `main()`；二者还引用 `smoke.py` 中的常量 `SILICONFLOW_BASE_URL`、`SILICONFLOW_DEFAULT_MODEL`。

---

## 2. 目标

1. 将 `insightagent` 迁移到 `src/insightagent/` 布局，并**删除仓库根目录下的旧 `insightagent/` 包**，保证唯一源码树为 `src/insightagent/`（避免新旧双包 shadow）。
2. 把平铺的顶层模块按**领域**重组到子包中。
3. 重写包内与测试内的所有 import，保证迁移后 import 全部可用。
4. 新增 `pyproject.toml`（含 pytest 配置与 console scripts），使 `src/` 布局下测试与命令入口可正常运行。
5. 同步更新 `README.md`、`MCP_GUIDE.md` 中已失效的模块运行路径（详见第 7 节入口说明）。
6. **行为零变化**：重构前后测试结果一致（绿对绿）。

## 3. 非目标（YAGNI）

- 不修改任何运行时逻辑、算法、函数签名或公开行为。
- 不新增/删除功能，不重写实现。
- 不做与本次结构调整无关的代码清理或风格统一。
- 不执行任何 git 提交动作（commit/push/PR）。
- 不调整 `tests/`、`workspaces/`、`mcp_config.json` 等仓库根级文件的位置（仅可能更新测试内的 import 与新增配置）。
- 不重写 README/MCP_GUIDE 的正文内容，仅做模块运行路径（`python -m ...`）的等价替换。

### 已知接口变更（非兼容，已接受）

由于 `cli.py`、`run_task.py` 移入 `cli/` 子包：

- `python -m insightagent.cli`：**保留可用**（由新增的 `cli/__main__.py` 委托 `main()`，语义与旧版相同）。
- `python -m insightagent.run_task`：**路径变更**为 `python -m insightagent.cli.run_task`（旧路径不再可用）。
- 推荐统一改用 console scripts：`insightagent`（交互）/ `insightagent-run`（任务）。

详见第 7 节。

---

## 4. 决策（已与用户确认）

| 决策点 | 选择 |
|---|---|
| 包布局 | 迁移到 `src/insightagent/` |
| 分层粒度 | 领域分层 |
| 向后兼容垫片 | 不保留，干净迁移，直接改所有 import |
| 文档/注释语言 | 中文 |

---

## 5. 目标目录结构

```
src/
  insightagent/
    __init__.py                # 版本号（保持 __version__）
    config.py                  # RuntimeConfig, load_dotenv_files, load_runtime_config
    api/                       # 模型/协议层（对外模型客户端与核心消息类型）
      __init__.py
      messages.py              # Message, ModelResponse, ToolCall
      providers.py             # ModelClient, AnthropicClient, OpenAICompatibleClient, ProviderError, ToolArgumentsParseError
      resilience.py            # ToolCallExtractor, build_repair_prompt, loads_lenient, RetryPolicy, *_FAILURE_KINDS
    agent/                     # Agent 主循环与会话/上下文/状态
      __init__.py
      core.py                  # CodeAgent（原 agent.py）
      context.py               # ContextManager, ProjectMemory, build_system_prompt, load_project_memory
      memory.py                # SlidingWindowMemory
      task_state.py            # TaskPhase, TaskState, mark_final_answer, phase_instruction, transition_after_tool
      session.py               # Session, SessionStore
    runtime/                   # 工具执行运行时（权限/校验/分类/类型/执行上下文）
      __init__.py
      command_validation.py
      failure_classifier.py
      permissions.py
      types.py
      tool_context.py          # ToolContext, PermissionDenied, WorkspaceViolation（原顶层 tool_context.py）
    tools/                     # 工具实现（保持不变）
      __init__.py  base.py  code_analysis_tools.py  execution_tools.py
      file_tools.py  registry.py  search_tools.py  state_tools.py
    mcp/                       # MCP 子系统（保持不变）
      __init__.py  adapters.py  client.py  config.py  errors.py
      manager.py  protocol.py  transports.py
    telemetry/                 # 追踪与用量统计
      __init__.py
      trace.py                 # CompositeTracer, ConsoleTracer, JsonlTraceRecorder 等
      usage.py                 # UsageTracker
    cli/                       # 命令行入口与面向 CLI 的配置
      __init__.py
      __main__.py              # 委托 main.main()，保留 `python -m insightagent.cli`
      main.py                  # 交互式 CLI（原 cli.py），保留 main()
      run_task.py              # 非交互任务入口，保留 main()
      smoke.py                 # 冒烟入口 + SILICONFLOW 常量
      slash_commands.py        # SlashCommandProcessor
      tool_profiles.py         # 工具档位配置（依赖 mcp.config, tools.base）
```

### 模块迁移映射表（旧 → 新）

| 旧路径 | 新路径 |
|---|---|
| `insightagent/messages.py` | `insightagent/api/messages.py` |
| `insightagent/providers.py` | `insightagent/api/providers.py` |
| `insightagent/resilience.py` | `insightagent/api/resilience.py` |
| `insightagent/agent.py` | `insightagent/agent/core.py` |
| `insightagent/context.py` | `insightagent/agent/context.py` |
| `insightagent/memory.py` | `insightagent/agent/memory.py` |
| `insightagent/task_state.py` | `insightagent/agent/task_state.py` |
| `insightagent/session.py` | `insightagent/agent/session.py` |
| `insightagent/tool_context.py` | `insightagent/runtime/tool_context.py` |
| `insightagent/trace.py` | `insightagent/telemetry/trace.py` |
| `insightagent/usage.py` | `insightagent/telemetry/usage.py` |
| `insightagent/cli.py` | `insightagent/cli/main.py` |
| `insightagent/run_task.py` | `insightagent/cli/run_task.py` |
| `insightagent/smoke.py` | `insightagent/cli/smoke.py` |
| `insightagent/slash_commands.py` | `insightagent/cli/slash_commands.py` |
| `insightagent/tool_profiles.py` | `insightagent/cli/tool_profiles.py` |
| `insightagent/config.py` | `insightagent/config.py`（内容不动，随包迁到 `src/insightagent/config.py`） |
| `insightagent/__init__.py` | `src/insightagent/__init__.py`（仅保留 `__version__`，不 re-export） |
| `insightagent/mcp/*` | 随包迁到 `src/insightagent/mcp/*`，内部不动 |
| `insightagent/runtime/*` | 随包迁到 `src/insightagent/runtime/*`，内部不动（仅新增 `tool_context.py`） |
| `insightagent/tools/*` | 随包迁到 `src/insightagent/tools/*`，内部仅改 `..tool_context` → `..runtime.tool_context` |

### 新建文件（不在旧包中）

| 新建路径 | 内容 |
|---|---|
| `src/insightagent/api/__init__.py` | 空文件（不 re-export） |
| `src/insightagent/agent/__init__.py` | 空文件（不 re-export） |
| `src/insightagent/telemetry/__init__.py` | 空文件（不 re-export） |
| `src/insightagent/cli/__init__.py` | 空文件（不 re-export） |
| `src/insightagent/cli/__main__.py` | `from .main import main` + `if __name__ == "__main__": main()` |
| `pyproject.toml`（仓库根） | 见第 7 节 |

> `runtime/__init__.py` 的现有导出集合**保持不变**（不新增 `ToolContext` 导出），消费方统一用 `from ..runtime.tool_context import ...` / `from insightagent.runtime.tool_context import ...`。

### 分层归属理由

- **api/**：`messages` 是最底层数据类型；`providers` 与 `resilience` 紧贴模型调用与响应解析，三者构成「与模型/协议交互」的领域，对应参考项目的 `crates/api`。
- **agent/**：`core`（主循环）协调 `context`/`memory`/`task_state`/`session`，是「Agent 运行编排」领域。
- **runtime/**：权限、命令校验、失败分类、执行类型已在此；`tool_context`（执行沙箱/权限上下文）与之高度内聚，从顶层并入。
- **telemetry/**：`trace` 与 `usage` 都是可观测性，对应 `crates/telemetry`。
- **cli/**：所有入口（`main`/`run_task`/`smoke`）与面向 CLI 的配置（`slash_commands`/`tool_profiles`）归一，对应 `crates/cli` + `crates/commands`。

---

## 6. import 重写规则

迁移后所有相对 import 必须按新层级修正。规则示例（最终精确清单在 plan 中逐文件给出）：

- 同包内：`agent/core.py` 引用同包的 `from .context import ...`、`from .session import ...`；引用 api 层 `from ..api.messages import Message`、`from ..api.providers import ...`、`from ..api.resilience import ...`；引用 telemetry `from ..telemetry.usage import UsageTracker`；引用 tools `from ..tools import ToolRegistry`。
- `api/providers.py`：`from .messages import ...`、`from .resilience import loads_lenient`。
- `api/resilience.py`：`from .messages import ToolCall`、`from ..runtime.failure_classifier import FailureKind`、`from ..runtime.types import ToolExecutionResult`。
- `runtime/permissions.py`：`from .tool_context import ToolContext`（原 `..tool_context`）。
- `runtime/failure_classifier.py`：`from .tool_context import PermissionDenied, WorkspaceViolation`。
- `runtime/__init__.py`：**导出集合保持不变**，不新增 `tool_context` 中的符号。消费方统一用 `from ..runtime.tool_context import ...` / `from insightagent.runtime.tool_context import ...`。
- `tools/*`：原 `from ..tool_context import ToolContext` 改为 `from ..runtime.tool_context import ToolContext`。
- `tools/registry.py`：`from ..api.resilience import ...`、`from ..runtime.tool_context import ToolContext`，其余 runtime 引用不变。
- `cli/main.py`（原 `cli.py`，**不引用 trace**）：`from ..config import load_dotenv_files, load_runtime_config`、`from ..agent.context import ContextManager, build_system_prompt, load_project_memory`、`from ..agent.core import CodeAgent`、`from ..mcp.config import load_mcp_config`、`from ..mcp.manager import MCPManager`、`from ..api.providers import AnthropicClient, OpenAICompatibleClient`、`from ..agent.session import SessionStore`、`from .slash_commands import SlashCommandProcessor`、`from .smoke import SILICONFLOW_BASE_URL, SILICONFLOW_DEFAULT_MODEL`、`from ..runtime.tool_context import ToolContext`、`from .tool_profiles import ...`、`from ..tools import ToolRegistry, default_tools`。
- `cli/run_task.py`（原 `run_task.py`，**引用 trace**）：`from ..agent.core import CodeAgent`、`from ..config import RuntimeConfig, load_dotenv_files, load_runtime_config`、`from ..agent.context import ContextManager, ProjectMemory, build_system_prompt, load_project_memory`、`from ..mcp.config import load_mcp_config`、`from ..mcp.manager import MCPManager`、`from ..api.providers import AnthropicClient, ModelClient, OpenAICompatibleClient, ProviderError`、`from ..agent.session import Session, SessionStore`、`from .smoke import SILICONFLOW_BASE_URL, SILICONFLOW_DEFAULT_MODEL`、`from ..runtime.tool_context import ToolContext`、`from .tool_profiles import ...`、`from ..tools import ToolRegistry, default_tools`、`from ..telemetry.trace import CompositeTracer, ConsoleTracer, JsonlTraceRecorder`。
- `cli/smoke.py`：`from ..agent.core import CodeAgent`、`from ..api.providers import ...`、`from ..tools import ToolRegistry`。
- `cli/slash_commands.py`：`from ..agent.core import CodeAgent`、`from ..agent.context import ProjectMemory`、`from ..agent.session import SessionStore`。
- `cli/tool_profiles.py`：`from ..mcp.config import MCPConfig`、`from ..tools.base import Tool`。
- `telemetry/usage.py`：`from ..api.messages import Message, ModelResponse`。
- `agent/context.py`、`agent/memory.py`、`agent/session.py`、`agent/task_state.py`：`from ..api.messages import ...`。

### 测试 import 重写（全量对照表）

`tests/` 内的所有 `from insightagent.<old> import ...` 必须按下表更新。**权威规则**：以第 5 节映射表为准，迁移后全局 grep 旧路径必须为 0 命中（见 §8.2）。

| 旧 import 前缀 | 新 import 前缀 |
|---|---|
| `insightagent.messages` | `insightagent.api.messages` |
| `insightagent.providers` | `insightagent.api.providers` |
| `insightagent.resilience` | `insightagent.api.resilience` |
| `insightagent.agent`（模块本身，含 `from insightagent.agent import CodeAgent`） | `insightagent.agent.core` |
| `insightagent.context` | `insightagent.agent.context` |
| `insightagent.memory` | `insightagent.agent.memory` |
| `insightagent.task_state` | `insightagent.agent.task_state` |
| `insightagent.session` | `insightagent.agent.session` |
| `insightagent.tool_context` | `insightagent.runtime.tool_context` |
| `insightagent.trace` | `insightagent.telemetry.trace` |
| `insightagent.usage` | `insightagent.telemetry.usage` |
| `insightagent.run_task` | `insightagent.cli.run_task` |
| `insightagent.slash_commands` | `insightagent.cli.slash_commands` |
| `insightagent.tool_profiles` | `insightagent.cli.tool_profiles` |
| `insightagent.smoke` | `insightagent.cli.smoke` |
| `insightagent.config` | `insightagent.config`（不变） |
| `insightagent.runtime.*` / `insightagent.mcp.*` / `insightagent.tools.*` | 不变 |

**特例与注意点**：

- `tests/test_cli_tool_profile_args.py` 第 5 行 `from insightagent import cli, run_task` → 固定改为 `from insightagent.cli import main as cli, run_task`。该测试通过 `cli.build_parser()` / `run_task.build_parser()` 使用模块对象；迁移后 `cli/main.py` 与 `cli/run_task.py` 均提供 `build_parser()`，故此改法可行。
- `tests/test_tool_profiles.py`：第 14 行附近的 lazy import `from insightagent.tool_profiles import ...` 改为 `from insightagent.cli.tool_profiles import ...`；同时第 22 行附近断言中的字面字符串 `"insightagent.tool_profiles has not been implemented"` 同步改为 `"insightagent.cli.tool_profiles has not been implemented"`（实现前先按实际文案核对，保持与被测代码一致）。
- `tests/trajectories/run_trajectories.py` 同样按上表更新 import。
- `tests/test_run_task_prompt.py` 中 `insightagent.run_task` → `insightagent.cli.run_task`。

---

## 7. 打包与测试配置

新增 `pyproject.toml`（位于 `insightagent_v5/` 根），内容：

- `[build-system]`：`requires = ["setuptools>=61", "wheel"]`、`build-backend = "setuptools.build_meta"`。
- `[project]`：`name = "insightagent"`、`version = "5.0.0"`、`requires-python = ">=3.10"`（代码使用 `X | Y` 类型写法与新式 f-string，按现状取 3.10 下限）、`dependencies = []`（零三方依赖）。
- `[project.optional-dependencies]`：`dev = ["pytest>=7", "pytest-timeout"]`（pytest 7+ 才支持 `pythonpath` ini 选项；`pytest-timeout` 提供基线命令所需的 `--timeout`）。
- `[project.scripts]`：`insightagent = "insightagent.cli.main:main"`、`insightagent-run = "insightagent.cli.run_task:main"`。
- `[tool.setuptools.packages.find]`：`where = ["src"]`。
- `[tool.pytest.ini_options]`：`pythonpath = ["src"]`、`testpaths = ["tests"]`。

> `pythonpath = ["src"]`（pytest 7+）即可让测试在不安装包的情况下从 `src/` 导入。

### 命令入口（运行方式）说明

迁移后支持的入口：

| 方式 | 命令 |
|---|---|
| console script（推荐） | `insightagent`（交互）、`insightagent-run`（任务）——需先 `pip install -e .` |
| 模块运行 | `python -m insightagent.cli`（经 `cli/__main__.py` 委托）、`python -m insightagent.cli.run_task`——需 `PYTHONPATH=src` 或已安装包 |

文档同步（仅替换命令字符串，不改正文）：

- `python -m insightagent.cli` 保持不变（无需替换）。
- `python -m insightagent.run_task` → `python -m insightagent.cli.run_task`（替换 `README.md`、`MCP_GUIDE.md` 中所有该字样）。
- `README.md` 中 `python3 -m py_compile $(find insightagent tests -name '*.py' ...)` → 将 `find insightagent` 改为 `find src/insightagent`。

---

## 8. 验收标准

1. 目录结构与第 5 节完全一致；源码树唯一为 `src/insightagent/`，**仓库根目录下的旧 `insightagent/` 目录已被删除**（不存在新旧双包）。
2. **包内不存在悬空 import**——用「导入全部子模块」做机械验收（比 grep 更可靠，能抓出任何漏改的相对 import）：

   ```bash
   PYTHONPATH=src python -c "import importlib, pkgutil, insightagent; \
   [importlib.import_module(m.name) for m in pkgutil.walk_packages(insightagent.__path__, insightagent.__name__ + '.')]"
   ```
   要求：无 `ImportError` / `ModuleNotFoundError`，退出码 0。

   > 不再用「包内 grep = 0」作为验收，因为迁移后同包相对 import（如 `api/providers.py` 的 `from .messages import ...`、`runtime/permissions.py` 的 `from .tool_context import ...`、`cli/main.py` 的 `from .smoke import ...`）是**合法**的，会与朴素 grep 模式冲突。导入全部子模块能确定性地暴露任何残留的错误路径。

3. **测试侧无旧绝对路径**——在 `tests/` 内，下列 grep 命中数必须为 0（这些模块迁移后已无 `insightagent.<name>` 直接形式）：
   - `from insightagent\.(messages|providers|resilience|context|memory|session|task_state|tool_context|trace|usage|run_task|slash_commands|smoke|tool_profiles)\b`
   - `from insightagent\.agent import\b`（注意：`from insightagent.agent.core import ...` 是合法新路径，因后接 `.core` 不会被此模式命中）
   - `from insightagent import (cli|run_task)\b`
   - 合法且不变的引用（`insightagent.config`、`insightagent.runtime.*`、`insightagent.mcp.*`、`insightagent.tools.*`、以及上面 §6 对照表中的新路径）不应被这些模式命中。

4. `src/` 布局下导入冒烟（任一方式成功）：
   - `PYTHONPATH=src python -c "import insightagent; from insightagent.agent.core import CodeAgent; from insightagent.api.providers import AnthropicClient; from insightagent.cli import main, run_task"`；或先 `pip install -e .` 后裸 `python -c "..."`。
5. **测试基线对照**：迁移前先用下方「稳定基线命令」记录结果作为基线；迁移后（配置好 `pythonpath=["src"]`）用**完全相同**的命令与筛选条件运行，结果与基线一致（同一组 pass/skip，无新增 fail/error）。
6. 不产生任何 git 提交。

### 稳定基线命令

`pytest --collect-only` 在本机出现卡顿（疑似某些用例在 import/collect 阶段触发网络或长耗时操作）。为得到可重复基线，验收基线**排除 trajectory 相关用例并加超时**。基线命令（迁移前后均执行，两次命令文本完全相同，仅差在迁移后 `src/` 已配好）：

```bash
# 迁移前：在仓库根（旧包在 sys.path 上）
python -m pytest tests --ignore=tests/test_trajectories.py -p no:cacheprovider -q --timeout=60
# 迁移后：pyproject 的 pythonpath=["src"] 生效，同样命令
python -m pytest tests --ignore=tests/test_trajectories.py -p no:cacheprovider -q --timeout=60
```

> `pytest-timeout` 已列入 `dev` 依赖（§7），基线命令固定为上述含 `--timeout=60` 的形式。注意安装顺序：**迁移前**的基线在仓库根（旧包在 `sys.path` 上）运行，此时 `src/` 与 `pyproject.toml` 尚不存在，故需先单独 `pip install pytest pytest-timeout`，再跑「迁移前」基线；待 `pyproject.toml` 建好后用 `pip install -e ".[dev]"` 安装，再跑「迁移后」基线。两次 pytest 命令本身必须**字节级相同**，不得在中途更改筛选条件。

---

## 9. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 漏改某处 import 导致 ImportError | 迁移后按 §8.2「导入全部子模块」机械验收（确定性暴露残留错误路径）；按 §8.3 测试侧 grep 旧绝对路径；按 §8.4 导入冒烟；逐文件参照附录 B 的精确 import 清单 |
| 新旧双包 shadow（根目录旧包未删，pytest 与裸 python 导入到不同包） | §2/§8.1 明确要求删除根目录旧 `insightagent/`，唯一源码树为 `src/insightagent/` |
| 测试基线不稳定（collect 卡顿） | §8「稳定基线命令」排除 trajectory 用例并加超时，迁移前后用同一条命令对照 |
| `from insightagent import cli, run_task` 这类「导入模块对象」的测试写法在改名后失配 | §6 已固定改法：`from insightagent.cli import main as cli, run_task`（`main`/`run_task` 均有 `build_parser()`，已验证可行） |
| src 布局后包不可导入 | `pyproject.toml` 的 `pytest pythonpath=["src"]` 用于测试；裸 `python -c` 需 `PYTHONPATH=src` 或 `pip install -e .`（见 §8.3） |
| `python -m` 旧入口失效 | 新增 `cli/__main__.py` 保留 `python -m insightagent.cli`；`run_task` 的 `-m` 路径改为 `insightagent.cli.run_task`，并同步 README/MCP_GUIDE |
| 循环依赖 | 已确认当前无环；迁移不改变依赖方向，仅改路径 |

---

## 附录 A：当前内部 import 依赖图（迁移前）

```
messages        ← (无包内依赖，最底层)
tool_context    ← (无包内依赖)
config          ← (无包内依赖)
runtime/types   ← (无包内依赖)
runtime/command_validation ← (无包内依赖)
runtime/failure_classifier → tool_context
runtime/permissions        → tool_context, runtime/command_validation, runtime/types
resilience      → messages, runtime/failure_classifier, runtime/types
providers       → messages, resilience
context         → messages
memory          → messages
session         → messages
task_state      → (无包内依赖；被 agent 引用)
usage           → messages
trace           → (无包内依赖；被 cli/run_task 引用)
agent           → context, memory, messages, providers, resilience, session, task_state, tools, usage
tools/base      → tool_context
tools/file_tools/search_tools/state_tools/code_analysis_tools/execution_tools → tool_context, tools/base
tools/registry  → resilience, runtime/failure_classifier, runtime/permissions, runtime/types, tool_context, tools/*
tool_profiles   → mcp/config, tools/base
mcp/* (内部自洽)
slash_commands  → agent, context, session
smoke           → agent, providers, tools
run_task        → agent, config, context, mcp/config, mcp/manager, providers, session, smoke, tool_context, tool_profiles, tools, trace
cli             → config, context, agent, mcp/config, mcp/manager, providers, session, slash_commands, smoke, tool_context, tool_profiles, tools
```

---

## 附录 B：逐文件 import 重写精确清单（迁移后）

> 仅列出 import 行**有变化**的文件。未列出的文件（如 `config.py`、`mcp/*`、`runtime/types.py`、`runtime/command_validation.py`、`telemetry/trace.py`、`agent/task_state.py`、`api/providers.py`）的 import 行**无需改动**（其相对引用在新层级下仍然指向同一目标）。

> **执行期订正（实测）**：以下两个文件原判「无需改」是**错误**的——它们在旧顶层位置用单点 `.` 引用子包（`.runtime` / `.mcp` / `.tools`），下移一层后单点会指向错误的子包，必须改为双点 `..`。已由 Task 4 的导入冒烟实测捕获并修正：

### `api/providers.py`（无需改）

`from .messages import ...`、`from .resilience import loads_lenient` 均为 `api/` 同级，不变。

### `api/resilience.py`（**需改 2 行**）

```python
from .messages import ToolCall                          # 不变（api/ 同级）
from ..runtime.failure_classifier import FailureKind    # 原 .runtime（顶层时指 insightagent.runtime）
from ..runtime.types import ToolExecutionResult         # 原 .runtime
```

### `cli/tool_profiles.py`（**需改 2 行**）

```python
from ..mcp.config import MCPConfig    # 原 .mcp（顶层时指 insightagent.mcp）
from ..tools.base import Tool         # 原 .tools
```

### `agent/core.py`（原 `agent.py`）

```python
from .context import ContextManager
from .memory import SlidingWindowMemory
from ..api.messages import Message
from ..api.providers import ModelClient, ToolArgumentsParseError
from ..api.resilience import ToolCallExtractor, build_repair_prompt
from .session import Session, SessionStore
from .task_state import TaskPhase, TaskState, mark_final_answer, phase_instruction, transition_after_tool
from ..tools import ToolRegistry
from ..telemetry.usage import UsageTracker
```

### `agent/context.py` / `agent/memory.py` / `agent/session.py`

各自的 `from .messages import ...` → `from ..api.messages import ...`（其余行不变）。

### `telemetry/usage.py`

`from .messages import Message, ModelResponse` → `from ..api.messages import Message, ModelResponse`。

### `runtime/permissions.py`

```python
from .tool_context import ToolContext          # 原 ..tool_context
from .command_validation import CommandValidator   # 不变
from .types import PermissionDecision, ToolPermission, ToolSpec   # 不变
```

### `runtime/failure_classifier.py`

`from ..tool_context import PermissionDenied, WorkspaceViolation` → `from .tool_context import PermissionDenied, WorkspaceViolation`。

### `tools/base.py` / `tools/file_tools.py` / `tools/search_tools.py` / `tools/state_tools.py` / `tools/code_analysis_tools.py` / `tools/execution_tools.py`

各自的 `from ..tool_context import ToolContext` → `from ..runtime.tool_context import ToolContext`（`from .base import ...` 等同包引用不变）。

### `tools/registry.py`（仅 2 行变）

```python
from ..api.resilience import BACKOFF_FAILURE_KINDS, PERMANENT_FAILURE_KINDS, RetryPolicy   # 原 ..resilience
from ..runtime.failure_classifier import FailureClassifier, FailureKind   # 不变
from ..runtime.permissions import PermissionEnforcer   # 不变
from ..runtime.types import ToolExecutionResult, ToolPermission, ToolRisk, ToolSpec   # 不变
from ..runtime.tool_context import ToolContext   # 原 ..tool_context
from .base import Tool   # 及以下 tools 同包引用全部不变
```

### `cli/main.py`（原 `cli.py`）

```python
from ..config import load_dotenv_files, load_runtime_config
from ..agent.context import ContextManager, build_system_prompt, load_project_memory
from ..agent.core import CodeAgent
from ..mcp.config import load_mcp_config
from ..mcp.manager import MCPManager
from ..api.providers import AnthropicClient, OpenAICompatibleClient
from ..agent.session import SessionStore
from .slash_commands import SlashCommandProcessor
from .smoke import SILICONFLOW_BASE_URL, SILICONFLOW_DEFAULT_MODEL
from ..runtime.tool_context import ToolContext
from .tool_profiles import (  # 保持原有被导入符号列表
    ...
)
from ..tools import ToolRegistry, default_tools
```

### `cli/run_task.py`（原 `run_task.py`）

```python
from ..agent.core import CodeAgent
from ..config import RuntimeConfig, load_dotenv_files, load_runtime_config
from ..agent.context import ContextManager, ProjectMemory, build_system_prompt, load_project_memory
from ..mcp.config import load_mcp_config
from ..mcp.manager import MCPManager
from ..api.providers import AnthropicClient, ModelClient, OpenAICompatibleClient, ProviderError
from ..agent.session import Session, SessionStore
from .smoke import SILICONFLOW_BASE_URL, SILICONFLOW_DEFAULT_MODEL
from ..runtime.tool_context import ToolContext
from .tool_profiles import (  # 保持原有被导入符号列表
    ...
)
from ..tools import ToolRegistry, default_tools
from ..telemetry.trace import CompositeTracer, ConsoleTracer, JsonlTraceRecorder
```

### `cli/smoke.py`（原 `smoke.py`）

```python
from ..agent.core import CodeAgent          # 原 .agent
from ..api.providers import AnthropicClient, OpenAICompatibleClient   # 原 .providers
from ..tools import ToolRegistry            # 原 .tools
```

### `cli/slash_commands.py`（原 `slash_commands.py`）

```python
from ..agent.core import CodeAgent          # 原 .agent
from ..agent.context import ProjectMemory   # 原 .context
from ..agent.session import SessionStore    # 原 .session
```

### `cli/__main__.py`（新建）

```python
from .main import main

if __name__ == "__main__":
    main()
```

> 上表中标注省略号 `...` 的 `tool_profiles` 导入符号列表，需照搬原 `cli.py` / `run_task.py` 中的实际符号（迁移时原样保留，不增删）。
