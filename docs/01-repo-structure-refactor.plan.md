# 实施计划：仓库目录结构重构（src 布局 + 领域分层）

> **配套规格**：见 `docs/01-repo-structure-refactor.spec.md`（决策、目标、验收标准以 spec 为准）。
> **执行者须知**：本计划按任务逐条执行；每条任务带文件路径、动作、验证命令与期望输出。
> **重要约束**：全程**不执行任何 git 提交动作**（无 commit/push/PR）。「frequent commits」一律替换为「验证检查点」。

**目标**：将 `insightagent` 包从平铺结构迁移到 `src/insightagent/` 布局并按领域分层，行为零变化（测试基线绿对绿）。

**架构**：纯结构性移动 + import 路径重写 + 新增 `pyproject.toml`。无运行时逻辑改动。

**技术栈**：Python（纯标准库，零三方依赖）、pytest、setuptools。

---

## 本计划的「TDD」含义

这是纯重构，不新增功能，**既有 29 个测试文件就是回归测试网**。RED/GREEN 在此体现为：

1. **GREEN（基线）**：迁移前先跑出稳定的测试基线（已知通过/跳过集合）。
2. **迁移**：移动文件 + 重写 import。
3. **GREEN（对照）**：迁移后用字节级相同的命令复跑，结果必须与基线一致。

任何「迁移后新出现的 fail/error」即视为重构引入的回归，必须定位修复，不得修改测试断言来掩盖。

---

## 任务总览

- Task 0：前置——安装测试依赖，记录迁移前基线
- Task 1：搭建 `src/insightagent/` 目录骨架与新建文件
- Task 2：移动各模块文件到目标位置（不改内容）
- Task 3：重写包内 import（按 spec 附录 B 逐文件）
- Task 4：新增 `pyproject.toml`
- Task 5：重写 `tests/` 内 import（按 spec §6 对照表）
- Task 6：删除仓库根旧 `insightagent/`，验证唯一源码树
- Task 7：导入冒烟 + 测试侧 grep 验收
- Task 8：迁移后测试基线对照
- Task 9：同步 README / MCP_GUIDE 的运行路径

---

## Task 0：前置——安装测试依赖并记录迁移前基线

**文件**：无（仅运行命令、记录输出）

- [ ] **Step 1：确认在仓库根目录**

```bash
cd /home/dinghanchen/stuckin/insightagent_v5 && pwd && ls insightagent/__init__.py
```
期望：打印仓库根路径，且旧包 `insightagent/__init__.py` 存在。

- [ ] **Step 2：安装 pytest 与 pytest-timeout（迁移前 src/pyproject 尚不存在，单独装）**

```bash
python3 -m pip install pytest pytest-timeout
```
期望：安装成功（或显示已满足）。

- [ ] **Step 3：记录迁移前基线（排除 trajectory 用例 + 超时）**

```bash
python3 -m pytest tests --ignore=tests/test_trajectories.py -p no:cacheprovider -q --timeout=60 | tee /tmp/baseline_before.txt
```
期望：命令在合理时间内结束（不卡死）。把末尾汇总行（如 `N passed, M skipped` / 失败清单）作为基线记录到 `/tmp/baseline_before.txt`。

- [ ] **Step 4：固化基线摘要**

```bash
tail -n 20 /tmp/baseline_before.txt
```
期望：可见明确的 pass/skip/fail 统计。**记下这组数字**，Task 8 将与之逐项对照。

> 若 Step 3 仍出现卡死，定位卡死文件：`for f in tests/test_*.py; do echo "== $f =="; timeout 60 python3 -m pytest "$f" -q -p no:cacheprovider || echo "TIMEOUT/FAIL: $f"; done`，把卡死文件加入 `--ignore` 并在基线说明中注明（迁移后同样 `--ignore`）。

---

## Task 1：搭建 `src/insightagent/` 目录骨架与新建文件

**文件**：
- Create: `src/insightagent/__init__.py`
- Create: `src/insightagent/api/__init__.py`、`src/insightagent/agent/__init__.py`、`src/insightagent/telemetry/__init__.py`、`src/insightagent/cli/__init__.py`
- Create: `src/insightagent/cli/__main__.py`

- [ ] **Step 1：创建目录**

```bash
mkdir -p src/insightagent/api src/insightagent/agent src/insightagent/telemetry src/insightagent/cli
```

- [ ] **Step 2：创建 `src/insightagent/__init__.py`（保留版本号）**

```python
"""InsightAgent V5.0 package."""

__version__ = "5.0.0"
```

- [ ] **Step 3：创建 4 个空子包 `__init__.py`**

`src/insightagent/api/__init__.py`、`src/insightagent/agent/__init__.py`、`src/insightagent/telemetry/__init__.py`、`src/insightagent/cli/__init__.py` 内容均为：

```python
```
（空文件即可，不做 re-export。）

- [ ] **Step 4：创建 `src/insightagent/cli/__main__.py`**

```python
from .main import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 5：验证骨架**

```bash
find src -type f | sort
```
期望：列出上述 7 个新建文件。

---

## Task 2：移动各模块文件到目标位置（不改内容）

**说明**：本任务只**搬运**文件，不动文件内容；import 修正在 Task 3 进行。先整体复制已分好的子包，再逐个搬运顶层模块。

- [ ] **Step 1：整体搬运 3 个已有子包与 config**

```bash
cp -r insightagent/mcp src/insightagent/mcp
cp -r insightagent/runtime src/insightagent/runtime
cp -r insightagent/tools src/insightagent/tools
cp insightagent/config.py src/insightagent/config.py
```

- [ ] **Step 2：搬运 api 层**

```bash
cp insightagent/messages.py    src/insightagent/api/messages.py
cp insightagent/providers.py   src/insightagent/api/providers.py
cp insightagent/resilience.py  src/insightagent/api/resilience.py
```

- [ ] **Step 3：搬运 agent 层（注意 agent.py → core.py）**

```bash
cp insightagent/agent.py       src/insightagent/agent/core.py
cp insightagent/context.py     src/insightagent/agent/context.py
cp insightagent/memory.py      src/insightagent/agent/memory.py
cp insightagent/task_state.py  src/insightagent/agent/task_state.py
cp insightagent/session.py     src/insightagent/agent/session.py
```

- [ ] **Step 4：搬运 tool_context 进 runtime**

```bash
cp insightagent/tool_context.py src/insightagent/runtime/tool_context.py
```

- [ ] **Step 5：搬运 telemetry 层**

```bash
cp insightagent/trace.py src/insightagent/telemetry/trace.py
cp insightagent/usage.py src/insightagent/telemetry/usage.py
```

- [ ] **Step 6：搬运 cli 层（注意 cli.py → main.py）**

```bash
cp insightagent/cli.py            src/insightagent/cli/main.py
cp insightagent/run_task.py       src/insightagent/cli/run_task.py
cp insightagent/smoke.py          src/insightagent/cli/smoke.py
cp insightagent/slash_commands.py src/insightagent/cli/slash_commands.py
cp insightagent/tool_profiles.py  src/insightagent/cli/tool_profiles.py
```

- [ ] **Step 7：核对搬运完整性**

```bash
find src/insightagent -name '*.py' | sort
```
期望：包含 spec 第 5 节目标结构的全部文件（api/agent/runtime/telemetry/cli/mcp/tools + config.py + 各 __init__）。此时 import 尚未修正，**先不要运行导入**。

---

## Task 3：重写包内 import（按 spec 附录 B 逐文件）

**文件（按 spec 附录 B 修改 import 行；不改其它逻辑）**：

- [ ] **Step 1：`src/insightagent/agent/core.py`** — 将顶部 import 改为：

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

- [ ] **Step 2：`agent/context.py`、`agent/memory.py`、`agent/session.py`** — 各自 `from .messages import ...` 改为 `from ..api.messages import ...`（被导入符号保持原样）。

- [ ] **Step 3：`telemetry/usage.py`** — `from .messages import Message, ModelResponse` 改为 `from ..api.messages import Message, ModelResponse`。

- [ ] **Step 4：`runtime/permissions.py`** — `from ..tool_context import ToolContext` 改为 `from .tool_context import ToolContext`（`.command_validation`、`.types` 两行不变）。

- [ ] **Step 5：`runtime/failure_classifier.py`** — `from ..tool_context import PermissionDenied, WorkspaceViolation` 改为 `from .tool_context import PermissionDenied, WorkspaceViolation`。

- [ ] **Step 6：`tools/` 下 6 个文件** — `base.py`、`file_tools.py`、`search_tools.py`、`state_tools.py`、`code_analysis_tools.py`、`execution_tools.py` 中的 `from ..tool_context import ToolContext` 改为 `from ..runtime.tool_context import ToolContext`（`from .base import ...` 不变）。

- [ ] **Step 7：`tools/registry.py`** — 改两行：`from ..resilience import ...` → `from ..api.resilience import ...`；`from ..tool_context import ToolContext` → `from ..runtime.tool_context import ToolContext`（其余 runtime/tools 引用不变）。

- [ ] **Step 8：`cli/main.py`** — 顶部 import 改为 spec 附录 B「cli/main.py」清单（`..config` / `..agent.context` / `..agent.core` / `..mcp.config` / `..mcp.manager` / `..api.providers` / `..agent.session` / `.slash_commands` / `.smoke` / `..runtime.tool_context` / `.tool_profiles` / `..tools`）。`tool_profiles` 的被导入符号列表照搬原 `cli.py`。

- [ ] **Step 9：`cli/run_task.py`** — 顶部 import 改为 spec 附录 B「cli/run_task.py」清单（含 `..telemetry.trace`）。`tool_profiles` 符号列表照搬原 `run_task.py`。

- [ ] **Step 10：`cli/smoke.py`** — `.agent`→`..agent.core`、`.providers`→`..api.providers`、`.tools`→`..tools`。

- [ ] **Step 11：`cli/slash_commands.py`** — `.agent`→`..agent.core`、`.context`→`..agent.context`、`.session`→`..agent.session`。

- [ ] **Step 12：`api/resilience.py`（执行期订正，需改 2 行）** — `from .runtime.failure_classifier import FailureKind` → `from ..runtime.failure_classifier import FailureKind`；`from .runtime.types import ToolExecutionResult` → `from ..runtime.types import ToolExecutionResult`（`from .messages import ToolCall` 不变）。

- [ ] **Step 13：`cli/tool_profiles.py`（执行期订正，需改 2 行）** — `from .mcp.config import MCPConfig` → `from ..mcp.config import MCPConfig`；`from .tools.base import Tool` → `from ..tools.base import Tool`。

> **教训**：凡是在旧顶层位置用单点 `.` 引用子包（`.runtime` / `.mcp` / `.tools`）的模块，下移一层后单点会指向错误子包，必须改双点 `..`。Task 4 的「导入全部子模块」冒烟能确定性捕获此类遗漏。

- [ ] **Step 14：确认无需改的文件保持原样** — `api/providers.py`、`config.py`、`mcp/*`、`runtime/types.py`、`runtime/command_validation.py`、`runtime/__init__.py`、`telemetry/trace.py`、`agent/task_state.py`、各 `tools/__init__.py` 同包引用、`mcp/__init__.py` 等不改动。

- [ ] **Step 13：本任务暂不运行导入**（缺 pyproject 的 pythonpath，需到 Task 4 之后统一冒烟）。

---

## Task 4：新增 `pyproject.toml`

**文件**：Create `pyproject.toml`（仓库根）

- [ ] **Step 1：创建 `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=61", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "insightagent"
version = "5.0.0"
description = "InsightAgent V5.0"
requires-python = ">=3.10"
dependencies = []

[project.optional-dependencies]
dev = ["pytest>=7", "pytest-timeout"]

[project.scripts]
insightagent = "insightagent.cli.main:main"
insightagent-run = "insightagent.cli.run_task:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

- [ ] **Step 2：导入全部子模块冒烟（机械验收，对应 spec §8.2）**

```bash
PYTHONPATH=src python3 -c "import importlib, pkgutil, insightagent; [importlib.import_module(m.name) for m in pkgutil.walk_packages(insightagent.__path__, insightagent.__name__ + '.')]"
```
期望：退出码 0，无 `ImportError` / `ModuleNotFoundError`。
若报错：依据报错的模块/符号回到 Task 3 修正对应 import，重跑本步直到通过。

- [ ] **Step 3：关键符号导入冒烟（对应 spec §8.4）**

```bash
PYTHONPATH=src python3 -c "import insightagent; from insightagent.agent.core import CodeAgent; from insightagent.api.providers import AnthropicClient; from insightagent.cli import main, run_task; print('ok')"
```
期望：打印 `ok`。

---

## Task 5：重写 `tests/` 内 import（按 spec §6 对照表）

**文件**：`tests/` 下所有引用旧路径的测试文件 + `tests/trajectories/run_trajectories.py`

- [ ] **Step 1：列出所有需要改的测试文件**

```bash
grep -rln "from insightagent\.\(messages\|providers\|resilience\|context\|memory\|session\|task_state\|tool_context\|trace\|usage\|run_task\|slash_commands\|smoke\|tool_profiles\)\|from insightagent\.agent import\|from insightagent import \(cli\|run_task\)" tests
```
期望：列出待改文件清单。

- [ ] **Step 2：按 spec §6 对照表逐文件替换 import 前缀**（机械替换）：
  - `insightagent.messages` → `insightagent.api.messages`
  - `insightagent.providers` → `insightagent.api.providers`
  - `insightagent.resilience` → `insightagent.api.resilience`
  - `from insightagent.agent import CodeAgent` → `from insightagent.agent.core import CodeAgent`
  - `insightagent.context` → `insightagent.agent.context`
  - `insightagent.memory` → `insightagent.agent.memory`
  - `insightagent.task_state` → `insightagent.agent.task_state`
  - `insightagent.session` → `insightagent.agent.session`
  - `insightagent.tool_context` → `insightagent.runtime.tool_context`
  - `insightagent.trace` → `insightagent.telemetry.trace`
  - `insightagent.usage` → `insightagent.telemetry.usage`
  - `insightagent.run_task` → `insightagent.cli.run_task`
  - `insightagent.slash_commands` → `insightagent.cli.slash_commands`
  - `insightagent.smoke` → `insightagent.cli.smoke`
  - `insightagent.tool_profiles` → `insightagent.cli.tool_profiles`
  - （`insightagent.config`、`insightagent.runtime.*`、`insightagent.mcp.*`、`insightagent.tools.*` 不变）

- [ ] **Step 3：特例——`tests/test_cli_tool_profile_args.py`**
  将第 5 行 `from insightagent import cli, run_task` 改为：

```python
from insightagent.cli import main as cli, run_task
```
（该文件其余使用 `cli.build_parser()` / `run_task.build_parser()` 的代码不动。）

- [ ] **Step 4：特例——`tests/test_tool_profiles.py`**
  - lazy import `from insightagent.tool_profiles import ...` → `from insightagent.cli.tool_profiles import ...`
  - 同步断言中的字面字符串 `insightagent.tool_profiles` → `insightagent.cli.tool_profiles`（先读该文件确认实际文案再改，保持与被测代码一致）。

- [ ] **Step 5：`tests/trajectories/run_trajectories.py`** 按同样对照表更新 import。

---

## Task 6：删除仓库根旧 `insightagent/`，验证唯一源码树

**文件**：Delete `insightagent/`（仓库根旧包）

- [ ] **Step 1：删除旧包**

```bash
rm -rf insightagent
```

- [ ] **Step 2：确认旧包已不存在、新包唯一**

```bash
test ! -e insightagent && echo "old package removed" ; ls -d src/insightagent
```
期望：打印 `old package removed`，且 `src/insightagent` 存在。

---

## Task 7：导入冒烟 + 测试侧 grep 验收

- [ ] **Step 1：删除旧包后重跑全子模块导入冒烟**

```bash
PYTHONPATH=src python3 -c "import importlib, pkgutil, insightagent; [importlib.import_module(m.name) for m in pkgutil.walk_packages(insightagent.__path__, insightagent.__name__ + '.')]; print('import-all ok')"
```
期望：打印 `import-all ok`（确认删除旧包后导入仍走 `src/`）。

- [ ] **Step 2：测试侧旧绝对路径 grep（对应 spec §8.3，命中必须为 0）**

```bash
grep -rnE "from insightagent\.(messages|providers|resilience|context|memory|session|task_state|tool_context|trace|usage|run_task|slash_commands|smoke|tool_profiles)\b|from insightagent\.agent import\b|from insightagent import (cli|run_task)\b" tests && echo "FOUND STALE (FAIL)" || echo "no stale test imports (PASS)"
```
期望：打印 `no stale test imports (PASS)`。

---

## Task 8：迁移后测试基线对照

- [ ] **Step 1：安装本包（dev）**

```bash
python3 -m pip install -e ".[dev]"
```
期望：安装成功，`insightagent` 与 console scripts 注册。

- [ ] **Step 2：用与 Task 0 完全相同的命令复跑测试**

```bash
python3 -m pytest tests --ignore=tests/test_trajectories.py -p no:cacheprovider -q --timeout=60 | tee /tmp/baseline_after.txt
```
> 若 Task 0 因卡死额外 `--ignore` 了某些文件，这里必须 `--ignore` 完全相同的集合。

- [ ] **Step 3：对照基线**

```bash
tail -n 20 /tmp/baseline_after.txt
diff <(grep -E "passed|failed|error|skipped" /tmp/baseline_before.txt | tail -1) <(grep -E "passed|failed|error|skipped" /tmp/baseline_after.txt | tail -1) && echo "BASELINE MATCH" || echo "BASELINE DIFF - INVESTIGATE"
```
期望：`BASELINE MATCH`（同一组 pass/skip，无新增 fail/error）。
若 `BASELINE DIFF`：逐个比较失败用例，定位是哪个 import/路径漏改导致，回到对应任务修正，重跑本步。**不得通过改测试断言来掩盖差异。**

---

## Task 9：同步 README / MCP_GUIDE 的运行路径

**文件**：Modify `README.md`、`MCP_GUIDE.md`

- [ ] **Step 1：定位旧路径引用**

```bash
grep -rnE "insightagent\.run_task|find insightagent " README.md MCP_GUIDE.md
```

- [ ] **Step 2：等价替换（仅命令字符串，不改正文语义）**
  - `python -m insightagent.run_task` / `python3 -m insightagent.run_task` → `python3 -m insightagent.cli.run_task`（或推荐 `insightagent-run`）
  - README 中 `find insightagent tests` → `find src/insightagent tests`
  - `python -m insightagent.cli` 保持不变（由 `cli/__main__.py` 支持），无需替换。

- [ ] **Step 3：确认无遗漏**

```bash
grep -rnE "insightagent\.run_task|find insightagent " README.md MCP_GUIDE.md && echo "STILL HAS OLD (FAIL)" || echo "docs updated (PASS)"
```
期望：`docs updated (PASS)`。

---

## 收尾验证（对应 spec §8 验收标准全条目）

- [ ] 目录结构与 spec 第 5 节一致，旧 `insightagent/` 已删（Task 6）。
- [ ] 全子模块导入冒烟通过（Task 7 Step 1）。
- [ ] 测试侧旧绝对路径 grep = 0（Task 7 Step 2）。
- [ ] 关键符号导入冒烟通过（Task 4 Step 3）。
- [ ] 迁移后测试基线与迁移前一致（Task 8）。
- [ ] README/MCP_GUIDE 路径已同步（Task 9）。
- [ ] **全程未执行任何 git 提交动作。**

---

## 自检（writing-plans Self-Review）

- **spec 覆盖**：spec 第 5 节结构（Task 1/2）、§6 import 规则与附录 B（Task 3/5）、§7 pyproject（Task 4）、§8 各验收项（Task 7/8 + 收尾）、文档同步（Task 9）、删除旧包（Task 6）——全部有对应任务。
- **占位符**：无 TBD/TODO；`tool_profiles` 导入符号列表明确要求「照搬原文件」，已在 spec 附录 B 与 Task 3 Step 8/9 说明。
- **类型/路径一致性**：`agent.py`→`agent/core.py`、`cli.py`→`cli/main.py` 在全计划中一致；`tool_context`→`runtime/tool_context` 在 Task 2/3/5/7 一致。
- **无提交**：所有原「commit」步骤已替换为验证检查点。
