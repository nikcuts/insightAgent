# 真实 SWE-bench-Lite Agent 能力评测实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 用 `.env` 真实模型配置在真实 SWE-bench-Lite case 子集上测量 InsightAgent 的实际能力边界，并根据真实 trace 迭代项目代码。

**架构：** 评测分为 case intake、baseline gate、真实模型运行、trace 聚类、Agent 代码改进、证据报告六段。dry-run 只允许用于 baseline gate，不允许作为 Agent 能力结果；有效成绩只来自 baseline 失败、Agent 后验证通过、未改测试的真实 case。

**技术栈：** Python `unittest`、`insightagent.evals.swe_bench_lite`、`insightagent.evals.swe_style`、SiliconFlow `.env`、`Qwen/Qwen2.5-72B-Instruct`、SWE-bench-Lite parquet。

---

## 文件结构

- 修改：`src/insightagent/evals/swe_style.py`
  - 维护真实评测 runner、baseline gate、报告统计、trace 路径、predictions 导出。
- 修改：`src/insightagent/evals/swe_bench_lite.py`
  - 维护官方 SWE-bench-Lite 数据准备、checkout clone、test patch 应用。
- 修改：`src/insightagent/agent/core.py`
  - 根据真实 trace 改进 Agent 阶段控制、验证强制、失败恢复。
- 修改：`src/insightagent/agent/task_state.py`
  - 根据真实 trace 改进 repair budget 和工具错误计数策略。
- 修改：`src/insightagent/tools/file_tools.py`
  - 根据真实 trace 改进编辑工具可靠性，避免语法损坏和无效替换。
- 修改：`src/insightagent/runtime/failure_classifier.py`
  - 根据真实 pytest 输出生成更准确的修复指导。
- 创建或修改：`reports/swe_style/<run-id>/summary.md`
  - 记录每个真实模型 run 的状态、patch、验证结果。
- 创建或修改：`reports/swe_style/<run-id>/results.jsonl`
  - 记录结构化结果，作为后续聚合来源。
- 创建或修改：`reports/swe_style/<run-id>/*.trace.jsonl`
  - 记录模型工具调用轨迹，用于失败聚类。
- 创建或修改：`reports/swe_style/capability-boundary-20260701.md`
  - 汇总能力边界、样本结果、失败模式、代码改进闭环。
- 测试：`tests/test_swe_style_eval.py`
  - 覆盖 baseline gate、有效样本统计、报告口径。
- 测试：`tests/test_swe_bench_lite_prepare.py`
  - 覆盖真实 checkout 准备、轻量 clone、case JSONL 生成。
- 测试：`tests/test_runtime_harness.py`
  - 覆盖真实 trace 暴露的工具编辑能力问题。
- 测试：`tests/test_agent_loop.py`
  - 覆盖真实 trace 暴露的 Agent 流程控制问题。

---

### 任务 1：锁定真实能力评测口径

**文件：**
- 修改：`src/insightagent/evals/swe_style.py`
- 测试：`tests/test_swe_style_eval.py`

- [ ] **步骤 1：编写失败测试，要求 summary 区分有效样本和无效样本**

```python
def test_summary_separates_evaluable_cases_from_invalid_baselines(self) -> None:
    results = [
        _case_result("pallets__flask-4045", "resolved", True, baseline=1, agent=0, verification=0),
        _case_result("pallets__flask-5063", "resolved", True, baseline=1, agent=0, verification=0),
        _case_result("pallets__flask-4992", "invalid_baseline", False, baseline=0),
        _case_result("psf__requests-3362", "invalid_environment", False, baseline=4),
    ]

    summary = render_summary(results)

    self.assertIn("- Evaluable cases: 2", summary)
    self.assertIn("- Resolution rate: 100.0%", summary)
    self.assertIn("- Raw resolution rate: 50.0%", summary)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`$env:PYTHONPATH='src'; python -m unittest tests.test_swe_style_eval.SweStyleEvalTests.test_summary_separates_evaluable_cases_from_invalid_baselines -v`
预期：FAIL，旧 summary 只用 total cases 做分母。

- [ ] **步骤 3：实现有效样本统计**

```python
def _is_evaluable_result(result: CaseRunResult) -> bool:
    return result.status not in {"invalid_baseline", "invalid_environment", "dry_run"}
```

`render_summary()` 同时输出 `Total cases`、`Evaluable cases`、`Invalid baseline cases`、`Invalid environment cases`、`Resolution rate`、`Raw resolution rate`。

- [ ] **步骤 4：运行测试验证通过**

运行：`$env:PYTHONPATH='src'; python -m unittest tests.test_swe_style_eval -v`
预期：所有 `tests.test_swe_style_eval` 测试通过。

---

### 任务 2：建立真实 case intake 和 baseline gate

**文件：**
- 修改：`src/insightagent/evals/swe_bench_lite.py`
- 修改：`src/insightagent/evals/swe_style.py`
- 测试：`tests/test_swe_bench_lite_prepare.py`
- 测试：`tests/test_swe_style_eval.py`

- [ ] **步骤 1：准备官方真实 case**

运行：

```powershell
$env:PYTHONPATH='src'
python -m insightagent.evals.swe_bench_lite `
  --dataset-url reports/swe_style/swebench-lite-cache/test-00000-of-00001.parquet `
  --instance-id pytest-dev__pytest-11143 `
  --output reports/swe_style/swebench-lite-pytest-11143/cases.jsonl `
  --checkout-root workspaces/evals/swebench_lite/checkouts `
  --clone `
  --git-no-proxy
```

预期：生成真实 `cases.jsonl`，checkout 位于 `workspaces/evals/swebench_lite/checkouts/pytest-dev__pytest-11143`。

- [ ] **步骤 2：运行 baseline gate，不调用模型**

运行：

```powershell
$env:PYTHONPATH='src'
python -m insightagent.evals.swe_style `
  --dataset reports/swe_style/swebench-lite-pytest-11143/cases.jsonl `
  --checkout-root workspaces/evals/swebench_lite/checkouts `
  --case-id pytest-dev__pytest-11143 `
  --run-id pytest11143-baseline-r1 `
  --run-root D:\ia_swe_style `
  --dry-run `
  --test-timeout 180
```

预期：如果 baseline 是目标测试失败，进入任务 3；如果 baseline 是环境错误，标记 `invalid_environment`，不调用模型。

- [ ] **步骤 3：如果 baseline 是 collection/import/build error，补 gate 规则**

```python
def classify_invalid_baseline(baseline: CommandResult) -> str | None:
    if baseline.exit_code == 0:
        return "invalid_baseline"
    if baseline.timed_out:
        return "invalid_environment"
    if _is_pytest_command(baseline.command) and baseline.exit_code != 1:
        return "invalid_environment"
    if _looks_like_pytest_collection_or_import_error(f"{baseline.stdout}\n{baseline.stderr}"):
        return "invalid_environment"
    return None
```

- [ ] **步骤 4：运行 gate 回归测试**

运行：`$env:PYTHONPATH='src'; python -m unittest tests.test_swe_style_eval tests.test_swe_bench_lite_prepare -v`
预期：所有评测准备和 gate 测试通过。

---

### 任务 3：真实模型运行，不接受 dry-run 作为能力结果

**文件：**
- 创建：`reports/swe_style/<real-run-id>/summary.md`
- 创建：`reports/swe_style/<real-run-id>/results.jsonl`
- 创建：`reports/swe_style/<real-run-id>/*.trace.jsonl`

- [ ] **步骤 1：选择 baseline 有效 case**

优先选择已经证明 baseline 有效的真实 case：

```text
pallets__flask-4045
pallets__flask-5063
```

新增 case 只有在 baseline gate 不是 `invalid_baseline`、`invalid_environment`、`dry_run` 后才允许进入模型评测。

- [ ] **步骤 2：调用真实模型运行 Agent**

运行：

```powershell
$env:PYTHONPATH='src'
python -m insightagent.evals.swe_style `
  --dataset reports/swe_style/swebench-lite-pallets__flask-5063/cases.jsonl `
  --checkout-root workspaces/evals/swebench_lite/checkouts `
  --case-id pallets__flask-5063 `
  --provider siliconflow `
  --model "Qwen/Qwen2.5-72B-Instruct" `
  --run-id qwen-swebench-lite-flask-5063-capability-r1 `
  --max-wall-seconds 900 `
  --provider-timeout 300 `
  --test-timeout 120 `
  --max-tool-iterations 80 `
  --prediction-model-name "Qwen/Qwen2.5-72B-Instruct"
```

预期：`agent` 字段不为 `null`，`trace_jsonl` 存在，真实模型被调用。

- [ ] **步骤 3：读取结果并分类**

运行：

```powershell
Get-Content -Raw reports/swe_style/qwen-swebench-lite-flask-5063-capability-r1/summary.md
Get-Content -Raw reports/swe_style/qwen-swebench-lite-flask-5063-capability-r1/results.jsonl
```

预期：能明确看到 `resolved`、`unresolved`、`agent_error` 或具体失败模式。

---

### 任务 4：从真实 trace 提取能力指标

**文件：**
- 创建或修改：`reports/swe_style/capability-boundary-20260701.md`
- 修改：`src/insightagent/evals/swe_style.py`

- [ ] **步骤 1：统计工具调用和 patch 规模**

从 `*.trace.jsonl` 和 `results.jsonl` 提取：

```text
case_id
status
model
agent_exit_code
verification_exit_code
iterations
tool_calls
read_file_calls
edit_file_calls
run_verification_calls
patch_changed_files
patch_added_lines
patch_deleted_lines
failure_mode
```

- [ ] **步骤 2：输出能力矩阵**

`reports/swe_style/capability-boundary-20260701.md` 必须包含：

```markdown
| 能力项 | 当前证据 | 结论 |
| --- | --- | --- |
| 读失败测试定位源码 | flask-4045 trace | 已证明，样本少 |
| 修改已有源码 | flask-4045/flask-5063 patch | 已证明，样本少 |
| 大型跨文件改动 | 无充分真实证据 | 未证明 |
| 复杂依赖环境准备 | requests/pytest invalid env | 当前薄弱 |
| 多轮失败后恢复 | flask-4045 trace | 部分证明 |
```

- [ ] **步骤 3：输出真实成绩和无效样本说明**

报告必须明确：

```text
Toy fixture excluded.
Dry-run excluded.
Invalid baseline excluded from scored denominator.
Invalid environment excluded from scored denominator.
```

---

### 任务 5：按真实失败 trace 修改 Agent 代码

**文件：**
- 修改：`src/insightagent/tools/file_tools.py`
- 修改：`src/insightagent/agent/core.py`
- 修改：`src/insightagent/agent/task_state.py`
- 修改：`src/insightagent/runtime/failure_classifier.py`
- 测试：`tests/test_runtime_harness.py`
- 测试：`tests/test_agent_loop.py`
- 测试：`tests/test_task_state.py`

- [ ] **步骤 1：如果 trace 显示 edit_file 损坏 Python，先写失败测试**

```python
def test_edit_file_converts_multiline_assert_with_message_to_raise(self) -> None:
    path = Path(self.workspace.name) / "sample.py"
    path.write_text(
        "def f(view_func):\n"
        "    assert (\n"
        "        \".\" not in view_func.__name__\n"
        "    ), \"Blueprint view function name should not contain dots\"\n",
        encoding="utf-8",
    )

    result = self.tool.run(
        path="sample.py",
        old='assert (\n        "." not in view_func.__name__\n    )',
        new="if '.' in view_func.__name__:\n    raise ValueError('Blueprint view function name should not contain dots')",
    )

    self.assertFalse(result.is_error)
    self.assertNotIn('), "Blueprint view function name should not contain dots"', path.read_text(encoding="utf-8"))
```

- [ ] **步骤 2：运行测试验证失败**

运行：`$env:PYTHONPATH='src'; python -m unittest tests.test_runtime_harness.RuntimeHarnessTests.test_edit_file_converts_multiline_assert_with_message_to_raise -v`
预期：FAIL，说明 edit_file 没扩展删除 assert message。

- [ ] **步骤 3：实现最小修复**

在 `src/insightagent/tools/file_tools.py` 添加：

```python
def _extend_python_assert_message_span(text: str, start: int, end: int, old: str) -> int:
    compact_old, _old_offsets = _compact_code_with_offsets(old)
    if not compact_old.startswith("assert"):
        return end
    index = end
    while index < len(text) and text[index] in " \t":
        index += 1
    if index >= len(text) or text[index] != ",":
        return end
    line_end = text.find("\n", index)
    return len(text) if line_end == -1 else line_end
```

- [ ] **步骤 4：运行相关回归**

运行：

```powershell
$env:PYTHONPATH='src'
python -m unittest `
  tests.test_runtime_harness `
  tests.test_task_state `
  tests.test_run_task_prompt `
  tests.test_providers `
  tests.test_code_analysis_tools `
  tests.test_agent_loop -v
```

预期：相关回归通过；如果失败，只修与真实 trace 改动相关的问题。

---

### 任务 6：形成可复现组会报告

**文件：**
- 创建或修改：`reports/swe_style/capability-boundary-20260701.md`
- 创建或修改：`reports/swe_style/qwen-swebench-lite-current-subset-20260701/summary.md`
- 创建或修改：`reports/swe_style/qwen-swebench-lite-current-subset-20260701/results.jsonl`

- [ ] **步骤 1：聚合真实 case 结果**

聚合输入只允许包含真实 SWE-bench-Lite case：

```text
reports/swe_style/qwen-swebench-lite-flask-4045-r20/results.jsonl
reports/swe_style/qwen-swebench-lite-flask-5063-r13/results.jsonl
reports/swe_style/qwen-swebench-lite-flask-4992-r2/results.jsonl
reports/swe_style/qwen-swebench-lite-requests-3362-r2/results.jsonl
reports/swe_style/qwen-swebench-lite-flask-5063-capability-r1/results.jsonl
```

- [ ] **步骤 2：写入组会结论**

报告必须包含：

```markdown
## 当前结论
- 真实模型调用次数
- 有效 case 数
- resolved case 数
- invalid baseline 数
- invalid environment 数
- 不能证明的能力边界

## 下一轮改造
- 环境准备能力
- 长任务稳定性
- patch 规模统计
- official SWE-bench harness 接入
```

- [ ] **步骤 3：验证报告证据存在**

运行：

```powershell
Test-Path reports/swe_style/capability-boundary-20260701.md
Test-Path reports/swe_style/qwen-swebench-lite-current-subset-20260701/summary.md
Test-Path reports/swe_style/qwen-swebench-lite-current-subset-20260701/results.jsonl
```

预期：三个路径都返回 `True`。

