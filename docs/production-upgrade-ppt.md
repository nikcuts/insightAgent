# InsightAgent 生产级升级汇报（10 页草案）

> 口径：所有带“已测”的数字来自仓库内可复现实验；没有真实模型运行结果的地方写“待测”，不填预测值。

## 1. 研究问题：功能完整不等于生产可用

- 目标：把能调用工具的 Agent，提升为可控、可恢复、可审计、可验证的 Agent Runtime。
- 核心问题：模型会犯错、工具有副作用、外部服务会失败，系统如何在不扩大风险的情况下继续工作？
- 评价标准：安全边界、失败收敛、证据完整性、真实任务修复能力。

## 2. 当前系统与可复用基础

- LangGraph 状态图：计划、检查、实现、验证、修复、总结阶段。
- ToolRuntime：ToolSpec、权限模式、命令分类、工作区边界、重试和时间预算。
- MCP：工具发现、服务启动、调用失败与重建路径。
- Checkpoint / Session：保留状态、工具事件、验证尝试和 token usage。
- Observability：Langfuse callback 与脱敏 JSONL trace。

## 3. 改造前基线与数据边界

**已测**

- 全量回归：`286 passed`，`16.55s`（`uv run pytest -q`，含审批、MCP trust、manifest 和 sandbox fail-closed 测试）。
- 历史确定性轨迹：12 条；legacy/resilient 报告为历史证据，未冒充本轮重跑结果。
- 本地 SWE-style：4 条 case，4/4 baseline 退出码 `1`。
- 真实模型 case：4 个本地小样本均 resolved；另有 2 个官方 SWE-bench-Lite Flask issue resolved，均未修改测试，trace 可用。
- 本地能力基准：9/9 通过；安全拒绝率 `100%`；脱敏回归通过。
- 可靠性故障场景：2/2 通过。

**尚未宣称**

- 真实模型 SWE 修复成功率、成本、延迟和泛化能力：目前只有 2 个官方真实仓库 case，不能外推。

## 4. 当前缺口：为什么还不能宣称生产上线

- 当前定位是 **production-demo ready**，不是 production deployment ready；审批、MCP trust policy、run manifest 和 sandbox prototype 已落地，但服务级治理仍缺失。
- 已完成工具权限、路径边界、失败分类、checkpoint、脱敏 trace 和 SWE 证据闭环。
- 仍缺少外部 durable store、worker lease、沙箱 CI/镜像治理、服务认证和容量治理。
- 2 个官方 case 足以证明流程可复现，不足以估计泛化成功率或生产 SLA。

## 5. 升级后的总体架构

```text
Model -> Contract / Planner -> Capability Policy -> ToolRuntime
                                      |                 |
                              deny / audit       retry / timeout
                                      |                 |
                         Checkpoint + Event Trace + Verification
                                      |
                         Reproducible Evaluation Reports
```

- 把“模型想做什么”和“运行时允许做什么”分离。
- 每个动作同时产出执行结果和可回放证据。
- 高风险动作在真正执行前进入 approval gate；批准、拒绝和暂停状态进入持久化 session/manifest，CLI 可用 `approve/deny` 恢复同一 thread。
- `execution_mode=sandbox` 通过 Docker 无网络、只读根文件系统、降权和资源限制运行；Docker 不可用时 fail closed，不回退宿主机。
- 长任务上下文保留 system/task 锚点；provider 消息错误触发最小上下文恢复，避免发送孤立的 assistant/tool 历史。
- 验证通过后若模型只重复只读检查，运行时基于验证证据直接收敛；若产生新修改则要求重新验证。

## 6. 亮点一：Capability-aware Security

- 默认拒绝高风险、越界路径、只读模式写入、安装和破坏性命令；显式 `approval_mode=interrupt` 时改为可审阅、可恢复的 approval gate。
- Tool event schema v1：`tool_call_id`、`permission`、`risk`、`outcome`、`failure_kind`、`retryable`、`suppressed`。
- 重复的永久拒绝动作进入抑制表，避免无效循环。
- 现场演示：`rm -rf`、`../outside.txt`、只读 `write_file` 均被拒绝并留下结构化事件。
- 已测：5 个安全拒绝场景全部通过，拒绝率 `100%`。

## 7. 亮点二：Fault-aware / Resumable Runtime

- 可重试失败：有限次数、指数退避、剩余预算优先。
- 永久失败：明确分类、抑制重复调用、向模型提供修复方向。
- 时间预算：在启动工具前检查剩余时间，防止回合超时后继续产生副作用。
- 已实现：checkpoint interrupt 后的人类审批恢复，工具参数、风险、审批决定写入 run manifest；测试确认批准后副作用只执行一次。
- 真实 Flask 运行中 provider 1214、验证后重复检查和编辑错误分类均被 trace 捕获，并分别通过上下文锚点、验证后收敛和失败类型区分策略修复。
- 已测：网络错误有限重试与第三次抑制、时间预算阻断执行，2/2 通过。

## 8. 亮点三：Evidence-driven SWE Loop

1. 固定 issue、checkout、验证命令和模型配置。
2. 先验证 baseline 必须失败，失败原因分类并留档。
3. Agent 只能修改源文件，禁止修改测试制造通过。
4. 执行原始验证命令，记录 patch、trace、迭代数、失败模式。
5. 只有 baseline 失败且最终验证通过的 case 才计入 resolved。

- 当前 4 个本地 case 与 2 个官方 Flask case 已完成真实模型运行；成功率只按这个明确样本报告，不外推。

## 9. 案例与结果页

| 案例 | 当前证据 | 汇报口径 |
| --- | --- | --- |
| 只读写入 / 破坏性命令 | 被拒绝，事件可审计 | 已完成 |
| 路径越界 | `permission_denied` | 已完成 |
| 网络故障 | 有界重试并抑制重复 | 已完成 |
| calculator addition | baseline 失败，3/3 测试通过，未改测试 | 已完成 `1/1` |
| slugify trim | baseline 失败，真实模型通过，未改测试 | 已完成本地小样本 |
| config deep merge | baseline 失败，真实模型通过，未改测试 | 已完成本地小样本 |
| JSON Lines blank | baseline 失败，真实模型通过，未改测试 | 已完成本地小样本 |
| Flask `pallets__flask-4045` | 官方 SWE-bench-Lite；baseline 2 fail，Agent 修改 1 个源文件，2/2 通过，未改测试 | 已完成 1 个真实仓库案例；不外推 |
| Flask `pallets__flask-4992` | 官方 SWE-bench-Lite；baseline 1 fail，Agent 修改 `src/flask/config.py`，1/1 通过，未改测试 | 已完成第 2 个真实仓库案例；不外推 |

## 10. 创新性、限制与下一步

**创新性**

- **Evidence-coupled approval gate**：审批 payload 同时携带工具、参数、权限、风险和 workspace；决定与结果进入同一 run manifest，恢复时不重放副作用。
- **Workspace revision barrier**：工具结果记录文件变化与验证 revision；验证通过后若没有新修改可直接收敛，避免模型重复只读调用。
- **Failure-aware repair loop**：将权限拒绝、网络故障、环境缺失、sandbox 不可用和代码/测试错误分层，分别采取拒绝、有限重试、抑制或修复。

**限制**

- 当前只有 2 个官方真实仓库 case，不能宣称生产 SLA 或泛化能力。
- 命令策略仍是解释型分类器，不是操作系统级沙箱。
- sandbox 仍是 Docker prototype，未在 CI 中实跑；当前没有外部队列/worker lease、服务认证、签名审批和多租户隔离。

**下一步**

- P0 已完成 runtime 版本：approval gate + `interrupt/resume`、MCP trust policy、run manifest 和 sandbox fail-closed prototype。
- P1：接入外部 durable checkpoint、任务队列、worker lease、取消/幂等和服务认证。
- 继续扩充真实 SWE-bench-Lite 与对抗性评测，报告成功率、成本、延迟和失败分类。
