# SWE-style Evaluation 设计

## 目标

以 SWE-bench 的核心判定方式为目标：给 Agent 一个带失败测试的代码仓库和 issue 描述，Agent 修改代码后重新运行同一验证命令，只有“基线失败、修复后通过”才算 resolved。

## 边界

- 本阶段实现本地 SWE-style 评测闭环，不直接接入官方 SWE-bench Docker harness。
- 真实模型调用复用现有 `insightagent.cli.run_task`，因此继续使用 `.env`、provider、model、trace 和工具 profile 配置。
- 评测结果落盘为 JSONL 和 Markdown，保留每个 case 的 workspace 与 trace，便于复盘失败轨迹。
- 默认模型面向用户指定的 `Qwen/Qwen2.5-72B-Instruct`，但 CLI 允许覆盖 provider/model。

## 架构

新增 `insightagent.evals.swe_style` 模块，负责加载 JSONL case、复制 fixture 仓库、运行 baseline 测试、调用 Agent、运行最终验证并汇总结果。case fixture 放在 `tests/fixtures/swe_style/`，单测只跑 dry-run 和本地命令，不触发真实 API。

## 成功标准

- `python -m unittest discover -s tests -v` 通过。
- `python -m insightagent.evals.swe_style --dry-run --limit 1` 能产生 baseline 失败报告。
- 使用 `.env` 中的 `SILICONFLOW_API_KEY` 运行 Qwen case 后，结果报告能明确显示 resolved/unresolved，并保留 trace。

## 真实 trace 暴露的问题

第一次 Qwen 真实调用中，模型没有先检查仓库，而是新建了无关的 `addition.py` demo 文件，并用 demo 命令验证成功后总结。运行时随后把总结中的普通代码块误恢复为 `write_file`，导致重复写文件直到迭代上限。对应修复是：评测任务提示明确要求先 inspect、只改现有源码、运行精确验证命令；通用 `run_task` system prompt 从 demo 优先改为真实仓库修复优先；agent loop 在 summarize 阶段不再把普通代码块恢复成写文件工具调用。

离线复盘同一 workspace 时，变更分析显示 `modified=[]`、`added=['addition.py']`、`test_changed=[]`、`source_changed=['addition.py']`，并生成 unified diff patch。`failure_mode=only_added_files` 说明失败不是验证器误判，而是模型没有定位到被测试导入的现有源码文件。评测报告因此新增 source changes、test changes、added files 和 failure mode，用于把“新建无关 demo”“改测试过关”“未产生 patch”“验证仍失败”这几类 SWE-bench 常见失败模式显式化。

后续运行时改造不再只依赖提示词，而是在 Agent 工具执行前加入 SWE-style task contract。契约会阻止：未检查仓库就写文件、修改测试文件、只新增 standalone/demo 文件后验证、通过 `run_verification` 或 `execute_command` 跑非指定验证命令。这使弱模型即使选择了错误工具，也会收到可修复的 tool error，而不是把错误路径计为成功。

为了降低首轮直接写 demo 文件的概率，Agent 在 SWE-style 任务开始时会注入一个 repository snapshot。snapshot 只列出仓库文件路径，不包含源码内容，并过滤 `.env`、密钥和证书类敏感文件名。它的作用是给模型一个仓库地图，引导下一步调用 `read_file`、`grep_search`、`glob_search` 等工具定位真实源码；它不会让运行时认为仓库已经被检查，因此“修改前必须 inspect”的契约仍然生效。

权限允许真实 API 后，用 `.env` 配置的 `Qwen/Qwen2.5-72B-Instruct` 复跑本地 SWE-style case。`qwen-local-20260701-r3` 和 `r4` 已经从最初的 `only_added_files` 进步到能修改 `calc.py`，但仍因 `verification_failed` 未通过：模型先把两个相同的 `return left - right` 都改成加法，随后需要重新读文件修复减法。`r3` 暴露了 read-only dedupe cache 在文件修改后未失效的问题；`r4` 暴露了默认 `max_repairs=3` 对真实 repair 流程过紧，两个 `edit_file` 参数错误就让任务提前进入 `FAILED`。对应修复是：任何非只读工具执行后清空只读 dedupe cache；默认修复预算提升到 5，但显式 `max_repairs=3` 的失败收敛测试保留。修复后 `qwen-local-20260701-r5` resolved：baseline 失败、Agent 只修改 `calc.py`、未修改测试、最终指定命令 `python -m unittest discover -s . -v` 通过。

## 组会改造清单

- 已完成：Windows 下 `python3` 命令归一化为当前解释器，保证 `.env` 和本地测试链路可复现。
- 已完成：新增 SWE-style 本地评测 runner，记录 baseline、agent、verification、trace 和 resolved 状态。
- 已完成：新增本地失败 fixture，验证“基线失败、修复后通过”的核心指标。
- 已完成：新增 workspace 变更分析，报告 source changes、test changes、added files；修改测试不会计入 resolved。
- 已完成：新增 unified diff patch 和 failure mode，并支持不再次调用模型的 `--analyze-run` 离线复盘。
- 已完成：修复总结阶段代码块被误执行的循环问题。
- 已完成：默认 system prompt 改为真实仓库修复优先，降低模型新建 demo 文件的概率。
- 已完成：新增 SWE-style task contract，运行时拦截未检查即写、改测试、只新增 demo 后验证、错误验证命令以及 `execute_command` 绕过。
- 已完成：新增 SWE-style repository snapshot 注入，首轮提供仓库结构地图，并过滤敏感文件名。
- 已完成：修复文件修改后 read-only dedupe cache 未失效的问题，避免模型读到过期上下文。
- 已完成：默认 repair budget 从 3 提升到 5，允许真实 issue 修复中常见的工具参数错误恢复。
- 已完成：真实 `.env` + Qwen 本地 SWE-style case 从 `r3/r4 unresolved` 迭代到 `qwen-local-20260701-r5 resolved`。
- 下一步：增加 SWE-bench Lite 数据转换器，把官方 instance 转成当前 JSONL case schema。
- 下一步：接入 Linux/Docker 执行器，复用官方 SWE-bench harness 的镜像和测试命令。
- 下一步：做模型矩阵评测，至少比较 Qwen2.5-72B、DeepSeek、GPT 系列的 resolved rate、平均迭代数、工具失败类型。
- 下一步：增加 patch-only 产物和 diff 评分，避免模型通过改测试、绕测试或生成无关文件获得假阳性。
- 下一步：把 trace 中的失败类型聚合成 dashboard，用于驱动下一轮 prompt、tool policy 和上下文检索优化。

## 后续扩展

下一阶段可以把 SWE-bench Lite 实例转换为相同 case schema：`source_dir` 替换为 checkout 后的 repo 路径，`issue` 来自 problem statement，`test_command` 来自实例级验证命令或官方 harness 生成的测试脚本。

当前 runner 已兼容本地 SWE-bench/Lite 风格 JSONL：`instance_id` 映射为 case id，`problem_statement` 映射为 issue，`verification_command` 映射为验证命令，`repo`、`base_commit`、`FAIL_TO_PASS` 保存在 metadata 并注入任务提示。若 JSONL 不包含 `source_dir`，可用 `--checkout-root` 指向按 sanitized `instance_id` 命名的本地 checkout 目录。

每次运行会额外导出 `predictions.jsonl`，字段为 `instance_id`、`model_name_or_path`、`model_patch`。其中 `model_patch` 来自 workspace 与原始 checkout 的 unified diff，可直接作为后续官方 SWE-bench harness 的输入候选；`--prediction-model-name` 可覆盖预测文件中的模型名。
