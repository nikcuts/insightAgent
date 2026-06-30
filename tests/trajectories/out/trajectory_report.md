# InsightAgent V5 — 12 任务测试轨迹报告

总任务数：12（确定性脚本化模型 + 真实运行时，无需联网）

## 轨迹结局分布

| 结局 | 数量 | 占比 |
| --- | --- | --- |
| 正常完成 (done) | 8 | 66.7% |
| 合理失败并解释 (failed, explained) | 3 | 25.0% |
| 迭代耗尽 (iteration exhausted) | 0 | 0.0% |
| 卡在 plan 阶段 (stuck in plan) | 1 | 8.3% |

## 逐任务轨迹

### t01_native_calculator — 计算器（原生 tool_calls，能力达标的模型）

- 结局：**正常完成 (done)** ✅ 成功
- 弱点模拟：无（基线：模型正常发出 tool_calls）
- 迭代轮数：3
- 判定原因：任务完成并通过验证
- 执行工具：write_file, execute_command
- 文本恢复：（无）
- 关键机制：（无）
- 最终输出：Done: calculator.py created, prints 5.

### t02_codeblock_only — 计算器（弱模型只会输出代码块，不发 tool_calls）

- 结局：**正常完成 (done)** ✅ 成功
- 弱点模拟：弱模型把代码塞进 markdown 代码块，不发原生 tool_calls
- 迭代轮数：3
- 判定原因：任务完成并通过验证
- 执行工具：write_file, execute_command
- 文本恢复：write_file(codeblock), execute_command(protocol)
- 关键机制：（无）
- 最终输出：Done: recovered from code block, verified output.

### t03_text_protocol — 文件创建（弱模型用文本协议 <tool_call> 调用）

- 结局：**正常完成 (done)** ✅ 成功
- 弱点模拟：模型不支持原生 function-calling，只能走文本协议
- 迭代轮数：3
- 判定原因：任务完成并通过验证
- 执行工具：write_file, execute_command
- 文本恢复：write_file(protocol), execute_command(protocol)
- 关键机制：（无）
- 最终输出：Done via text protocol.

### t04_malformed_json — 参数 JSON 有尾随逗号（宽容解析修复）

- 结局：**正常完成 (done)** ✅ 成功
- 弱点模拟：弱模型产出的 JSON 参数有尾随逗号等小错误
- 迭代轮数：3
- 判定原因：任务完成并通过验证
- 执行工具：write_file, execute_command
- 文本恢复：write_file(protocol), execute_command(protocol)
- 关键机制：（无）
- 最终输出：Done: lenient JSON repair handled the trailing comma.

### t05_self_heal — 自修复：先写出 bug，验证失败后修好再验证

- 结局：**正常完成 (done)** ✅ 成功
- 弱点模拟：首版代码有 NameError，需要按诊断修复
- 迭代轮数：5
- 判定原因：任务完成并通过验证
- 执行工具：write_file, execute_command, edit_file, execute_command
- 文本恢复：（无）
- 关键机制：自修复 触发
- 最终输出：Done: fixed NameError and verified output 10.

### t06_forced_tool_choice — 升级式修复：模型空谈两轮后被强制工具调用

- 结局：**正常完成 (done)** ✅ 成功
- 弱点模拟：模型反复用散文空谈，不肯行动
- 迭代轮数：5
- 判定原因：任务完成并通过验证
- 执行工具：write_file, execute_command
- 文本恢复：（无）
- 关键机制：强制 tool_choice 升级
- 最终输出：Done after escalation forced a tool call.

### t07_dedup_readonly — 读取去重：重复 read_file 被合成消息拦截

- 结局：**正常完成 (done)** ✅ 成功
- 弱点模拟：模型反复读取同一个文件，浪费迭代
- 迭代轮数：4
- 判定原因：任务完成并通过验证
- 执行工具：read_file, read_file, read_file
- 文本恢复：（无）
- 关键机制：去重/抑制 生效
- 最终输出：Done: notes.txt has alpha and beta.

### t08_multifile — 多文件任务：写模块 + 测试并运行

- 结局：**正常完成 (done)** ✅ 成功
- 弱点模拟：无（多步骤组合任务）
- 迭代轮数：4
- 判定原因：任务完成并通过验证
- 执行工具：write_file, write_file, execute_command
- 文本恢复：（无）
- 关键机制：（无）
- 最终输出：Done: prints 5 from two modules.

### t09_network_down — 网络不可用：退避后给出明确结论（合理失败）

- 结局：**合理失败并解释 (failed, explained)** 🟠 合理失败
- 弱点模拟：环境网络不通；旧版会盲目重试到迭代耗尽
- 迭代轮数：4
- 判定原因：进入 FAILED 后用文字明确解释失败原因（未死锁、未耗尽迭代）
- 执行工具：execute_command, execute_command, execute_command
- 文本恢复：（无）
- 关键机制：去重/抑制 生效, 自修复 触发
- 最终输出：This is a network failure (could not resolve host). Retrying is futile; the environment has no network access, so the package cannot be fetched.

### t10_mcp_missing — MCP 工具不可用：诊断提示指向配置（合理失败）

- 结局：**合理失败并解释 (failed, explained)** 🟠 合理失败
- 弱点模拟：调用 mcp_ 工具但 MCP server 未启动
- 迭代轮数：4
- 判定原因：进入 FAILED 后用文字明确解释失败原因（未死锁、未耗尽迭代）
- 执行工具：mcp_playwright_navigate, mcp_playwright_navigate, mcp_playwright_navigate
- 文本恢复：（无）
- 关键机制：自修复 触发
- 最终输出：The mcp_playwright tool is unavailable. Per the diagnostic, the MCP server is not running / not configured (check mcp_config.json). I will not install dependenc

### t11_unfixable — 无法在限度内修复：耗尽 repair 次数后失败收尾

- 结局：**合理失败并解释 (failed, explained)** 🟠 合理失败
- 弱点模拟：持续性错误，超过最大修复次数
- 迭代轮数：5
- 判定原因：进入 FAILED 后用文字明确解释失败原因（未死锁、未耗尽迭代）
- 执行工具：write_file, execute_command, execute_command, execute_command
- 文本恢复：（无）
- 关键机制：自修复 触发
- 最终输出：I cannot fix this within the repair limit: broken.py raises RuntimeError unconditionally by design.

### t12_hopeless_prose — 彻底空谈的模型：框架优雅终止（卡在 plan）

- 结局：**卡在 plan 阶段 (stuck in plan)** 🔴 异常
- 弱点模拟：模型只会输出散文，连文本协议都不给——框架必须能优雅停下
- 迭代轮数：3
- 判定原因：模型始终未发出可执行动作；框架在 nudge 上限后优雅终止
- 执行工具：（无）
- 文本恢复：（无）
- 关键机制：强制 tool_choice 升级
- 最终输出：In conclusion, sorting matters. (still no tool call)
