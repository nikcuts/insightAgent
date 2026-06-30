# Agent Runtime 落地改造实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 修复已复现的 runtime 中断问题，并建立 InsightAgent 落地改造的 P0/P1 清单。

**架构：** 保持现有 agent loop 和工具注册结构不变，先补齐 trace、配置、入口和验证层的稳定性。所有修复同步到根目录包和 `src/` 包中对应文件，直到仓库完成单入口迁移。

**技术栈：** Python 3.10+、unittest、stdlib provider client、InsightAgent runtime tools。

---

## 文件结构

- 创建：`docs/superpowers/specs/2026-07-01-agent-runtime-hardening-design.md`，记录能力边界和落地路线。
- 创建：`docs/superpowers/plans/2026-07-01-agent-runtime-hardening.md`，记录可执行任务清单。
- 修改：`insightagent/trace.py`，修复当前实际入口的 console trace 崩溃。
- 修改：`src/insightagent/telemetry/trace.py`，同步修复安装入口使用的 trace 实现。
- 修改：`tests/test_runtime_harness.py`，增加 `ConsoleTracer` 回归测试。

### 任务 1：修复 ConsoleTracer 崩溃

**文件：**
- 修改：`tests/test_runtime_harness.py`
- 修改：`insightagent/trace.py`
- 修改：`src/insightagent/telemetry/trace.py`

- [x] **步骤 1：编写失败的回归测试**

在 `tests/test_runtime_harness.py` 增加测试，构造 `model_response` 事件，包含 `write_file` 的长 `content` 参数，调用 `ConsoleTracer(max_chars=800)` 并断言不会抛出 `AttributeError`，输出中包含 `"full content sent to tool"`。

- [x] **步骤 2：运行测试验证失败**

运行：`python -m unittest tests.test_runtime_harness.RuntimeHarnessTests.test_console_tracer_summarizes_tool_call_arguments -v`

预期：修复前失败，错误包含 `AttributeError: 'ConsoleTracer' object has no attribute '_summarize_arguments'`。

- [x] **步骤 3：实现最小修复**

把 `_summarize_arguments(arguments: dict[str, Any]) -> dict[str, Any]` 移到或复制到 `ConsoleTracer`，保持现有摘要格式：

```python
def _summarize_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
    summarized = dict(arguments)
    content = summarized.get("content")
    if isinstance(content, str) and len(content) > 240:
        summarized["content"] = {
            "chars": len(content),
            "preview": content[:160],
            "note": "full content sent to tool; trace display summarized",
        }
    return summarized
```

- [x] **步骤 4：运行测试验证通过**

运行：`python -m unittest tests.test_runtime_harness.RuntimeHarnessTests.test_console_tracer_summarizes_tool_call_arguments -v`

预期：`OK`。

- [x] **步骤 5：运行相关回归测试**

运行：`python -m unittest tests.test_runtime_harness tests.test_config tests.test_run_task_prompt tests.test_providers -v`

预期：全部通过。

### 任务 2：保留真实 API smoke 证据

**文件：**
- 不修改源码
- 使用：`workspaces/capability_probe_trace`

- [x] **步骤 1：运行带 trace 的最小任务**

运行：`python -m insightagent.run_task --provider siliconflow --model "Qwen/Qwen2.5-72B-Instruct" --workspace workspaces/capability_probe_trace --permission-mode workspace-write --timeout 180 --max-wall-seconds 240 --max-tool-iterations 8 --trace-max-chars 1200 --task "在当前 workspace 创建 capability_probe.py，实现 add(a, b)，再运行 python capability_probe.py 验证输出为 5。必须使用 write_file 和 execute_command，最后简短总结。"`

预期：任务不会因 trace 渲染崩溃。若 provider 超时或远端报错，记录为外部依赖失败，而非 runtime trace 失败。

- [x] **步骤 2：验证生成文件**

运行：`python workspaces/capability_probe_trace/capability_probe.py`

预期：输出 `5`。

### 任务 3：入口一致性审计

**文件：**
- 修改：后续计划单独展开

- [ ] **步骤 1：列出根目录包和 `src` 包差异**

运行：`python -c "import insightagent; print(insightagent.__file__)"`

预期：记录当前直接运行源码时会优先加载根目录 `insightagent` 包。

- [ ] **步骤 2：形成单入口迁移计划**

把迁移拆成独立 spec/plan，避免和 trace 修复混在同一个提交范围。

### 任务 4：组会能力边界说明

**文件：**
- 修改：后续可创建 `docs/superpowers/reports/2026-07-01-agent-capability-boundary.md`

- [ ] **步骤 1：整理现状表**

覆盖能力、证据、风险、下一步四列。

- [ ] **步骤 2：整理路线图**

按 P0/P1/P2/P3 输出一页组会可讲版本。

## 自检

- 每个任务都有明确文件和验证命令。
- P0 trace 修复可以独立完成并验证。
- 后续入口统一和组会报告没有混入首个修复任务。
