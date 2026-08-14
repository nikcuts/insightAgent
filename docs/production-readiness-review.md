# InsightAgent 生产就绪性审查

## 结论

当前版本不是“可直接上线的生产 Agent”，而是一个**可验证的生产化研究原型**：它已经具备状态图、工具权限、工作区边界、失败分类、检查点、审批中断、MCP 信任策略、脱敏 trace、运行清单和 SWE 验证闭环，足以支撑技术汇报和受控演示；但还没有覆盖真实生产系统必须面对的多 worker、服务化、容量和运营责任。

建议对外口径：

> InsightAgent 已完成 runtime hardening 和 evidence-driven evaluation，达到 production-demo readiness；已实现可恢复审批和 sandbox prototype，但距离 production deployment 还缺少外部 durable multi-worker execution、service auth、容量治理和 adversarial evaluation。

## 已达到的范围

- 工具调用经过 `ToolSpec`、权限模式、路径边界和命令分类。
- 永久拒绝、重复拒绝、超时和协议错误有结构化失败类型。
- LangGraph 状态图保留阶段、验证尝试、工具事件和 token usage。
- SQLite checkpoint、线程锁和 workspace binding 已覆盖单机恢复与并发保护。
- trace 和模型输入经过敏感字段脱敏。
- `approval_mode=interrupt` 会在写入、执行、MCP 和 external 工具前暂停，CLI 支持 `approve/deny` 恢复；默认仍为 `deny`，避免无意放开副作用。
- 每个 turn 写入 run manifest：`run_id`、policy version、git SHA、wall time、token usage、失败分类、审批历史和估算成本；暂停状态由 LangGraph SQLite checkpoint 持久化，manifest 文件保存可审计摘要，避免破坏 interrupt pending task。
- MCP 默认只读取用户级配置；工作区/启动目录配置必须显式 `--trust-workspace-mcp`。
- `execution_mode=sandbox` 提供 Docker 无网络、资源限制、降权和 fail-closed 原型；Docker 不可用时不会静默回退到宿主机。
- 固定的 10 个官方 SWE-bench-Lite case 有 baseline、patch、原始验证命令和未修改测试证据；最佳终态为 7/10，3 个 unresolved 保留为失败。

## 仍然是 demo 的关键缺口

### 1. 审批闭环已实现，但还不是服务级审批系统

运行时已经用持久化 `interrupt/resume` 展示工具名、参数、权限和风险，并用同一 thread 恢复；当前入口是 CLI/本地 session，仍缺少组织级审批人、超时策略、审批 API、签名决定和多租户审计。

### 2. Sandbox 是原型，不是已验证的生产隔离

默认 `execution_mode=host` 仍由解释型命令策略保护；`sandbox` 模式已有 Docker driver（无网络、只读根、降权、CPU/内存/PID 限制），但当前没有在 CI 中运行 Docker，也没有镜像签名、宿主内核隔离证明、挂载白名单和供应链扫描。不能把原型等同于完整安全边界。

### 3. 持久化是单机 SQLite，不是生产 worker durability

当前 checkpoint 能支持单机续跑，但没有外部 durable store、worker lease、心跳、任务队列、幂等键和跨进程故障接管。多个 worker 或机器故障时，不能宣称 exactly-once side effect。

### 4. 缺少服务层责任

当前主要是 CLI runtime，没有租户认证、授权、速率限制、配额、取消 API、审计查询 API、后台 worker、告警和 SLO。没有这些边界，无法把“能运行”定义成生产服务。

### 5. 评测规模仍不足以支持泛化结论

固定 10 个官方仓库案例上的 7/10 是明确样本的最佳终态结果，不是泛化成功率估计。还需要跨语言、失败类型、权限攻击、MCP 恶意输入和长任务恢复的更大固定测试集，并统计成本、延迟、恢复时间和失败分布。

### 6. MCP 还有管理员 allowlist 和供应链责任

运行时默认不加载 workspace 与启动目录中的 MCP 配置，必须显式 `--trust-workspace-mcp`；生产模式仍应只允许签名或管理员批准的 server manifest，禁止仓库内容自动引入可执行 MCP command，并对 server、tool、network scope 单独授权。

## 借鉴成熟 Agent 的改造方向

保留 LangGraph 作为底层 runtime，不重写成另一套 agent loop；吸收以下模式：

1. **Human control / plan mode**：高风险动作先形成可审阅计划，再用 durable interrupt 等待批准。
2. **Tool-level guardrails**：每个工具执行前后做输入校验、权限校验、输出 schema 校验和副作用 diff 校验。
3. **Durable execution**：使用外部 checkpoint/store、worker lease、幂等副作用记录和可恢复队列。
4. **Sandbox driver**：把 shell、MCP 和仓库修改放入可销毁的隔离执行单元，默认关闭网络。
5. **Trace + evaluation contract**：每次 run 固定 workflow、policy version、model、git SHA、工具事件、审批事件、成本和最终验收结果。
6. **Memory isolation**：短期 thread state 与跨任务 memory 分离，组织级 memory 只能由应用写入，不能让 Agent 自己修改共享策略。

## 推荐实施顺序

### P0：生产演示已补齐，仍需服务化加固

- 已完成：approval gate + `interrupt/resume`，覆盖 write、execute、MCP 和 external 高风险动作。
- 已完成：run manifest 与 metrics、MCP workspace trust policy、Docker sandbox fail-closed prototype。
- 待补：审批 API/签名决定、镜像治理、沙箱 CI 运行和真实服务入口。

### P1：受控试运行

- 外部 durable checkpoint、任务队列、worker lease、取消和重试幂等。
- 服务 API 的认证、授权、配额、审计查询和告警。
- 20--50 条真实/对抗性评测，按任务类型报告成功率和 p95 成本/延迟。

### P2：生产部署

- 多租户隔离、密钥托管、供应链扫描、灾备演练、SLO 和 incident runbook。
- 对模型、工具、MCP server 和 policy 进行版本化发布与回滚。

## 官方参考

- [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/)：Agent loop、handoffs、guardrails 和 tracing。
- [OpenAI Agents SDK Guardrails](https://openai.github.io/openai-agents-python/guardrails/)：工具调用前后置校验与 tripwire。
- [OpenAI Agents SDK Tracing](https://openai.github.io/openai-agents-python/tracing/)：generation、tool、guardrail 和 workflow span。
- [LangGraph Overview](https://docs.langchain.com/oss/python/langgraph/overview)：durable execution、streaming、human-in-the-loop 和 persistence 的 runtime 定位。
- [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)：持久化 interrupt、thread resume 和副作用幂等规则。
- [LangChain Going to Production](https://docs.langchain.com/oss/python/deepagents/going-to-production)：外部持久化、故障恢复、time travel、敏感操作审计和 memory isolation。
- [Anthropic Trustworthy Agents](https://www.anthropic.com/research/trustworthy-agents)：human control、权限、透明度、隐私与安全的系统性框架。
