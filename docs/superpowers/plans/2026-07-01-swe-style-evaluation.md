# SWE-style Evaluation 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 增加一个可复现的本地 SWE-bench 风格评测闭环，并能用 `.env` 真实调用模型。

**架构：** 新增独立 eval 模块，复用现有 `run_task` 运行时。fixture case 用 JSONL 描述，runner 复制工作区、跑 baseline、调用 Agent、跑最终验证、输出报告。

**技术栈：** Python 标准库、`unittest`、现有 InsightAgent CLI。

---

### 任务 1：新增评测 runner

**文件：**
- 创建：`src/insightagent/evals/__init__.py`
- 创建：`src/insightagent/evals/swe_style.py`
- 测试：`tests/test_swe_style_eval.py`

- [x] **步骤 1：实现 case schema**

定义 `SweStyleCase`，字段为 `id`、`source_dir`、`issue`、`test_command`。

- [x] **步骤 2：实现 workspace 准备**

复制 fixture 到 `workspaces/evals/swe_style/<run-id>/<case-id>`，忽略 `.git`、缓存和 `.insightagent`。

- [x] **步骤 3：实现 baseline / verification 命令执行**

使用 `subprocess.run(..., shell=True, capture_output=True, timeout=...)`，记录 exit code、stdout、stderr、耗时和 timeout。

- [x] **步骤 4：实现 Agent 调用**

构造 `python -m insightagent.cli.run_task` 命令，传入 provider、model、workspace、task、trace、wall-clock 和工具迭代上限。

- [x] **步骤 5：实现报告输出**

写入 `reports/swe_style/<run-id>/results.jsonl` 和 `summary.md`。

### 任务 2：新增本地 SWE-style case

**文件：**
- 创建：`tests/fixtures/swe_style/cases.jsonl`
- 创建：`tests/fixtures/swe_style/local_calc_addition/calc.py`
- 创建：`tests/fixtures/swe_style/local_calc_addition/test_calc.py`

- [x] **步骤 1：构造基线失败**

`calc.py` 中 addition 错误返回 subtraction，`test_calc.py` 断言 addition/subtraction/multiplication。

- [x] **步骤 2：定义 issue 与验证命令**

case 使用 `python -m unittest discover -s . -v` 作为验证命令。

### 任务 3：验证闭环

**文件：**
- 修改：`tests/test_swe_style_eval.py`

- [x] **步骤 1：测试 JSONL case 加载**

验证相对 `source_dir` 能解析为 fixture 目录。

- [x] **步骤 2：测试 baseline 失败**

复制 fixture 后运行验证命令，断言 exit code 非 0 且 stderr 包含 `FAILED`。

- [x] **步骤 3：测试 dry-run 不调用 provider**

运行 `run_case(..., dry_run=True)`，断言只记录 baseline，不产生 agent/verification。

### 任务 4：根据真实 trace 加固 Agent 行为

**文件：**
- 修改：`src/insightagent/agent/core.py`
- 修改：`src/insightagent/cli/run_task.py`
- 修改：`src/insightagent/evals/swe_style.py`
- 修改：`tests/test_agent_loop.py`
- 修改：`tests/test_run_task_prompt.py`
- 修改：`tests/test_swe_style_eval.py`

- [x] **步骤 1：禁止 summarize 阶段误恢复代码块**

在 agent loop 调用 `ToolCallExtractor.extract()` 时，若当前 phase 是 `summarize`，传入 `allow_codeblock_write=False`。

- [x] **步骤 2：用测试复现总结代码块循环**

构造模型先写文件、验证成功、再输出带 Python 代码块的总结，断言不会触发 `tool_call_recovered`。

- [x] **步骤 3：修正 eval runner 的 Agent 调用方式**

把 `subprocess.list2cmdline(...) + shell=True` 改为参数数组 + `shell=False`，避免 PowerShell 解释多行 task 和反引号。

- [x] **步骤 4：增强 SWE-style 任务提示**

要求第一步 inspect 仓库、只改已有源码、运行精确验证命令，避免模型新建无关 demo 文件。

- [x] **步骤 5：调整通用 run_task system prompt**

删除 demo 优先语义，明确真实仓库修复优先、先 inspect、不要创建无关 standalone demo 文件。

### 任务 5：补强评测结果分析

**文件：**
- 修改：`src/insightagent/evals/swe_style.py`
- 修改：`tests/test_swe_style_eval.py`
- 修改：`README.md`

- [x] **步骤 1：新增 workspace 变更分析**

比较 fixture 源目录和运行后 workspace，输出 `modified_files`、`added_files`、`deleted_files`、`test_files_changed`、`source_files_changed`。

- [x] **步骤 2：把修改测试排除出 resolved**

只有 baseline 失败、最终验证通过、且 `test_files_changed` 为空时才计入 resolved；如果最终验证通过但修改了测试，标记 `invalid_test_modified`。

- [x] **步骤 3：报告变更文件**

Markdown 摘要新增 source changes、test changes、added files 三列，JSONL 结果包含完整 `changes` 对象。

- [x] **步骤 4：用既有 Qwen workspace 离线复盘**

确认上次 Qwen 评测只新增 `addition.py`，没有修改被测试导入的 `calc.py`，失败归因是“新建无关 demo 文件”。

- [x] **步骤 5：记录 patch 和 failure mode**

在 `WorkspaceChanges` 中生成 unified diff patch，在 `CaseRunResult` 中记录 failure mode，覆盖 `resolved`、`only_added_files`、`test_modified`、`no_patch`、`verification_failed` 等状态。

- [x] **步骤 6：支持已有 run 离线复盘**

新增 `analyze_existing_run()` 和 CLI 参数 `--analyze-run`，无需再次调用 provider 即可重建报告并分类失败模式。

### 任务 6：兼容 SWE-bench/Lite 本地数据

**文件：**
- 修改：`src/insightagent/evals/swe_style.py`
- 修改：`tests/test_swe_style_eval.py`
- 修改：`README.md`

- [x] **步骤 1：兼容官方字段**

`load_cases()` 支持 `instance_id`、`problem_statement`、`verification_command`，并把 `repo`、`base_commit`、`FAIL_TO_PASS` 保存在 metadata。

- [x] **步骤 2：支持 checkout-root 推导**

当 JSONL 不提供 `source_dir` 时，用 `--checkout-root/<sanitized-instance-id>` 定位本地 checkout。

- [x] **步骤 3：把 metadata 注入任务提示**

任务提示中展示 repository、base commit、fail-to-pass tests，便于模型以真实 issue 修复方式工作。

- [x] **步骤 4：验证 official-style dry-run**

用本地 synthetic SWE-bench 风格 JSONL + checkout-root 跑 `--dry-run`，确认 baseline 失败和报告生成。

### 任务 7：导出 SWE-bench predictions

**文件：**
- 修改：`src/insightagent/evals/swe_style.py`
- 修改：`tests/test_swe_style_eval.py`
- 修改：`README.md`

- [x] **步骤 1：实现 predictions JSONL 导出**

新增 `export_predictions()`，每行输出 `instance_id`、`model_name_or_path`、`model_patch`。

- [x] **步骤 2：CLI 每次运行写 predictions**

评测完成后在报告目录写出 `predictions.jsonl`，并打印路径。

- [x] **步骤 3：支持模型名覆盖**

新增 `--prediction-model-name`，用于控制官方预测文件里的 `model_name_or_path`。

- [x] **步骤 4：测试 resolved patch 导出**

用 fake agent 修复 `calc.py`，断言 `predictions.jsonl` 包含 `calc.py` 的 unified diff。
