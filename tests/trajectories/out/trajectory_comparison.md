# InsightAgent V5 — 对照实验：修复前 (legacy) vs 修复后 (resilient)

同一组 12 个任务、同一批脚本化模型行为，分别在 legacy 模式与 resilient 模式下真实运行。

## 结局分布对比

| 结局 | 修复前 legacy | 修复后 resilient |
| --- | --- | --- |
| 正常完成 (done) | 5 (41.7%) | 8 (66.7%) |
| 合理失败并解释 (failed, explained) | 0 (0.0%) | 3 (25.0%) |
| 迭代耗尽 (iteration exhausted) | 7 (58.3%) | 0 (0.0%) |
| 卡在 plan 阶段 (stuck in plan) | 0 (0.0%) | 1 (8.3%) |

## 逐任务对比

| 任务 | 修复前 | 修复后 | 说明 |
| --- | --- | --- | --- |
| t01_native_calculator | 正常完成 (done) | 正常完成 (done) | 无（基线：模型正常发出 tool_calls）  |
| t02_codeblock_only | 迭代耗尽 (iteration exhausted) | 正常完成 (done) | 弱模型把代码塞进 markdown 代码块，不发原生 tool_calls → 改善 |
| t03_text_protocol | 迭代耗尽 (iteration exhausted) | 正常完成 (done) | 模型不支持原生 function-calling，只能走文本协议 → 改善 |
| t04_malformed_json | 迭代耗尽 (iteration exhausted) | 正常完成 (done) | 弱模型产出的 JSON 参数有尾随逗号等小错误 → 改善 |
| t05_self_heal | 正常完成 (done) | 正常完成 (done) | 首版代码有 NameError，需要按诊断修复  |
| t06_forced_tool_choice | 正常完成 (done) | 正常完成 (done) | 模型反复用散文空谈，不肯行动  |
| t07_dedup_readonly | 正常完成 (done) | 正常完成 (done) | 模型反复读取同一个文件，浪费迭代  |
| t08_multifile | 正常完成 (done) | 正常完成 (done) | 无（多步骤组合任务）  |
| t09_network_down | 迭代耗尽 (iteration exhausted) | 合理失败并解释 (failed, explained) | 环境网络不通；旧版会盲目重试到迭代耗尽 → 改善 |
| t10_mcp_missing | 迭代耗尽 (iteration exhausted) | 合理失败并解释 (failed, explained) | 调用 mcp_ 工具但 MCP server 未启动 → 改善 |
| t11_unfixable | 迭代耗尽 (iteration exhausted) | 合理失败并解释 (failed, explained) | 持续性错误，超过最大修复次数 → 改善 |
| t12_hopeless_prose | 迭代耗尽 (iteration exhausted) | 卡在 plan 阶段 (stuck in plan) | 模型只会输出散文，连文本协议都不给——框架必须能优雅停下  |