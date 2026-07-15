# LangGraph/LangChain/Langfuse Full Rewrite Design

## Goal

Rewrite InsightAgent around one framework stack:

- LangGraph is the only agent orchestration runtime.
- LangChain is the only model and tool abstraction layer.
- Langfuse is the primary observability integration for graph, model, and tool execution.

The rewrite intentionally does not preserve the old hand-written agent loop, provider clients, JSON session runtime, or custom trace pipeline as compatibility layers. Existing code may be reused only when it is domain logic that still belongs under the new framework boundary.

## Non-goals

- Do not keep `CodeAgent.run_turn` as the real execution path.
- Do not keep `api/providers.py` as the main provider abstraction.
- Do not maintain two tracing systems with equal authority.
- Do not keep session JSON as the source of truth once LangGraph checkpointing is introduced.
- Do not do an incremental compatibility migration where old and new runtimes both support production execution.

## External Framework Direction

Current LangGraph documentation describes LangGraph as the low-level orchestration runtime for long-running, stateful agents, with LangChain components commonly used for models and tools. Current LangChain tool documentation defines tools as callable functions with schemas passed to chat models. Current Langfuse documentation recommends the LangChain/LangGraph callback integration and requires explicit flush or shutdown for short-lived applications.

References:

- https://docs.langchain.com/oss/python/langgraph/overview
- https://docs.langchain.com/oss/python/langgraph/quickstart
- https://docs.langchain.com/oss/python/langchain/tools
- https://langfuse.com/integrations/frameworks/langchain

## Target Architecture

Create a new `insightagent.graph` package and move the runtime center there.

```text
insightagent/
  graph/
    __init__.py
    state.py          # LangGraph state schema and reducers
    workflow.py       # graph construction and routing
    nodes.py          # prepare/model/tool/verify/summarize/fail nodes
    models.py         # LangChain chat model initialization
    tools.py          # LangChain StructuredTool definitions
    checkpoints.py    # checkpoint/store setup
    observability.py  # Langfuse callback and local debug hooks
    sessions.py       # thread listing and transcript export from checkpoints
    runner.py         # CLI-facing graph runner
```

The CLI becomes a thin launcher:

```text
load config/env
load workspace and MCP config
build LangChain model
build LangChain tools
build LangGraph workflow with checkpointing and callbacks
invoke or stream graph
flush Langfuse
print final result
```

## Runtime Dependencies

`pyproject.toml` must declare the framework stack as runtime dependencies, not rely on a developer machine that happens to have packages installed:

- `langgraph`
- `langchain`
- `langchain-core`
- `langchain-openai`
- `langchain-anthropic`
- `langgraph-checkpoint-sqlite`
- `langfuse`

Development dependencies continue to include `pytest` and `pytest-timeout`. The implementation plan must pin minimum versions after checking the installed APIs, especially for checkpointing and Langfuse callback imports.

## Graph State

`AgentState` is a typed LangGraph state containing:

- `messages`: LangChain messages, append-only through a reducer.
- `workspace`: absolute workspace path as string.
- `task`: original user task.
- `phase`: `plan | inspect | implement | verify | repair | summarize | done | failed`.
- `iteration`: model/tool loop counter.
- `max_iterations`: graph guardrail.
- `deadline_monotonic`: optional wall-clock deadline computed by the runner from `max_wall_seconds`.
- `verification_command`: optional expected command extracted from the task.
- `last_tool_error`: most recent failed tool result.
- `changed_files`: files changed by write/edit tools.
- `inspected_files`: files read or searched.
- `verification_attempts`: executed verification commands and exit status.
- `repair_attempts`: repair loop count.
- `phase_history`: ordered phase transitions for testing and debugging.
- `tool_events`: compact structured records for tool name, arguments summary, permission, risk, command kind, retryability, suppression, and failure kind.
- `final_answer`: final assistant-facing response.

Old `TaskState`, `SlidingWindowMemory`, and `ContextManager` are removed as primary runtime objects. Their useful behavior is reimplemented as graph nodes or reducers.

## Workflow

The main graph is explicit:

```text
START
-> prepare_task
-> inject_repository_snapshot
-> call_model
-> route_model_output
   -> execute_tools
      -> update_phase
      -> call_model
   -> summarize
      -> END
   -> fail
      -> END
```

Routing rules:

- If model emits tool calls, execute tools through LangChain tool invocation.
- If no tool call and the phase still requires action, route to a repair/nudge node instead of accepting prose.
- If verification succeeds after workspace mutation, route to summarize.
- If iteration or repair budget is exhausted, route to fail.
- Before each model or tool node, if `deadline_monotonic` has expired, route to fail with a `time_budget_exceeded` event and preserve partial state.
- During model and tool execution, derive remaining wall-clock budget from `deadline_monotonic`, cap provider/tool/shell timeouts to that remaining budget, and convert in-flight timeout or cancellation into `phase=failed` with `time_budget_exceeded`.

This replaces the hand-written `for iteration in range(...)` loop.

The graph uses a custom `execute_tools` node, not a plain prebuilt tool node, because graph control state cannot be derived from model-visible tool text alone. The node invokes LangChain tools, appends `ToolMessage`s to `messages`, and returns explicit state deltas for `changed_files`, `inspected_files`, `last_tool_error`, `verification_attempts`, `phase`, `phase_history`, and `tool_events`.

## Models

Remove custom provider clients as production runtime code. Use LangChain chat model initialization instead:

- OpenAI uses `langchain_openai.ChatOpenAI`.
- SiliconFlow uses `langchain_openai.ChatOpenAI` with explicit `api_key`, `base_url`, `model`, `timeout`, and `max_tokens` from config/env. The factory must not assume `SILICONFLOW_API_KEY` is recognized by LangChain automatically.
- Anthropic uses `langchain_anthropic.ChatAnthropic`.

The project config keeps provider/model/base URL fields, but `insightagent.graph.models` maps them to LangChain model objects. Timeout, max output tokens, retry count, temperature, and top-p are applied in this factory. Tools are bound with `model.bind_tools(tools)` before graph execution. Tests should use fake LangChain chat models rather than fake `ModelClient`.

## Tools

Rewrite tool exposure as LangChain `StructuredTool` objects.

The old tool implementation code can be used as source material, but the new contract is:

- Each tool has a typed schema.
- Each tool returns structured text or a small JSON-serializable object.
- Permission checks, command validation, workspace path resolution, and failure classification are wrappers around tool execution.
- Tool execution emits Langfuse-observable spans through LangChain callbacks.

The old `ToolRegistry` is removed as the agent-facing surface. It can be replaced by a `ToolRuntime` or `ToolExecutor` helper that is not visible to the model.

`StructuredTool` return values are only the model-visible result. Tool side effects and routing metadata are captured by the custom `execute_tools` graph node and stored in `tool_events`. This preserves the useful parts of the current `ToolExecutionResult` contract without exposing the old registry as the model-facing abstraction.

## Task Contracts And SWE Guardrails

The rewrite must preserve the existing SWE-style safety and evaluation guardrails as graph runtime behavior. These are not prompt-only requirements.

Create `insightagent.graph.contracts` to extract and enforce task contracts before and after tool execution. Enforcement lives in the custom `execute_tools` node or a pre-tool policy wrapper and returns model-visible tool errors when the model violates the contract.

Required contract behavior:

- Detect exact expected verification commands and reject different verification commands for tasks that specify one.
- Require repository inspection before workspace mutation for repository repair tasks.
- Require reading fail-to-pass test files before patching when the task identifies failing tests.
- Reject edits to test files for SWE-style repair tasks unless the task explicitly requests test changes.
- Reject standalone demo/new-file-only fixes when the task requires patching an existing repository.
- Reject unrequested optional flags, alternate commands, or alternate entrypoints that hide the default failing path.
- Detect destructive rewrites that remove many existing Python symbols from non-test files.
- After failed verification, require inspection before the next mutation.

These rules replace the old `CodeAgent._enforce_task_contract` behavior and must be covered by graph tests.

## Memory And Checkpointing

Replace custom session JSON with LangGraph checkpointing.

Phase 1 uses `langgraph-checkpoint-sqlite` as the local persistent checkpointer. A custom checkpointer is out of scope for this rewrite. The implementation must configure graph invocations with `thread_id`, support `checkpoint_id` inspection/replay through LangGraph state history APIs, and run checkpoint resume tests against the SQLite-backed checkpointer.

The public session id maps to LangGraph `thread_id`:

```python
config = {"configurable": {"thread_id": session_id}}
```

`--checkpoint-id` may resume or inspect a specific checkpoint:

```python
config = {"configurable": {"thread_id": session_id, "checkpoint_id": checkpoint_id}}
```

The CLI keeps user-visible session operations, backed by checkpoint history rather than old JSON session files:

- `--session-id`: use or create the LangGraph thread id.
- `--session-dir`: location of the SQLite checkpoint database and session metadata index.
- `--list-sessions`: list known thread ids from the session metadata index.
- `--export-transcript`: render a Markdown transcript from checkpointed LangChain messages and tool events.

Starting a new user turn on an existing `thread_id` is an explicit runner operation. `runner.start_turn` appends the new user message to the checkpointed transcript, preserves long-lived conversation history and project memory context, and resets per-turn fields: `task`, `phase`, `iteration`, `deadline_monotonic`, `verification_command`, `last_tool_error`, `changed_files`, `inspected_files`, `verification_attempts`, `repair_attempts`, `phase_history`, `tool_events`, and `final_answer`. A completed or failed previous turn must not prevent the next turn from executing.

`checkpoint_id` is for explicit inspection, replay, or forked execution. Normal `--session-id` resume starts from the latest checkpoint for the thread and then runs `start_turn`.

Project memory becomes a `prepare_task` input step that adds system/context messages, not a separate memory manager. Context trimming becomes a graph node that summarizes or prunes tool-heavy history before model invocation.

Usage accounting no longer reads old `ModelResponse` objects. It is derived from LangChain response metadata and callback data, then stored in graph state and Langfuse metadata.

## MCP

MCP remains a first-class tool source, but MCP tools are converted to LangChain tools before graph construction. The graph does not know whether a tool is built-in or MCP; all tools share the same LangChain interface and permission wrapper.

The MCP manager may remain as connection lifecycle code, but adapters must output LangChain `BaseTool`/`StructuredTool` instances with typed schemas. MCP lifecycle events are recorded as graph debug events and Langfuse observations.

## Observability

Langfuse becomes the primary trace sink.

- Create a Langfuse callback handler in `observability.py`.
- Pass callbacks through LangGraph invoke/stream config.
- Add trace metadata: workspace, session/thread id, provider, model, tool profile, task hash.
- Flush or shutdown Langfuse at CLI exit because `run_task` is short-lived.
- Wrap non-model graph nodes, permission checks, task-contract rejections, tool suppression, phase changes, MCP lifecycle events, and context pruning in explicit Langfuse observations/spans so Langfuse covers runtime decisions, not only LLM and tool calls.

Local console output remains a presentation layer, not the authoritative trace model. JSONL traces may be kept only as a debug export generated from graph events, not as the primary telemetry architecture.

The implementation uses `from langfuse.langchain import CallbackHandler` and `from langfuse import get_client`.

## Eval And Debug Trace Interface

The SWE-style eval runner remains supported and calls `insightagent.graph.runner` directly. If a test requires subprocess isolation, `python -m insightagent.cli.run_task` must still be graph-backed and expose equivalents for:

- `--trace-jsonl`
- `--tool-profile`
- `--allowed-tools`
- `--enable-mcp-server`
- `--max-wall-seconds`
- `--max-tool-iterations`
- `--max-output-tokens`
- `--language`
- `--no-trace`

`--trace-jsonl` becomes a debug export of graph events, including phase transitions, tool events, contract rejections, verification attempts, final state, and Langfuse trace ids when available. Eval reports may continue to link this path.

`--max-wall-seconds` is enforced by the graph runner, graph routing, and per-call timeout wrappers. The runner computes a monotonic deadline, stores it in state, and every long-running loop boundary checks it before model and tool execution. The model node and tool node derive remaining time before each call; provider calls, Python tool calls, MCP calls, and shell commands are capped to that remaining budget. Timeout finalization sets `phase=failed`, records `time_budget_exceeded` in graph debug events, exports partial state, and still runs Langfuse flush/shutdown in `finally`.

## CLI And Public Commands

Keep script names for user convenience:

- `insightagent`
- `insightagent-run`

But internally they call the graph runner. CLI options that only exist for the old runtime are removed or renamed. Compatibility flags are not required unless they map cleanly to the new graph runtime.

Interactive slash commands are rewritten against graph/session services:

- `/status`: latest graph state, phase, thread id, checkpoint id, and workspace.
- `/cost`: usage derived from LangChain/Langfuse metadata.
- `/memory`: project memory injected by `prepare_task`.
- `/compact`: graph state update that prunes/summarizes message history.
- `/clear`: new thread or state reset operation.
- `/permissions`: current permission mode and tool policy.
- `/export`: transcript rendered from checkpoints.
- `/mcp`: MCP connection/tool status from the MCP manager.

## Removal Plan

Remove or retire as production code:

- `insightagent.agent.core`
- `insightagent.agent.task_state`
- `insightagent.agent.memory`
- `insightagent.agent.context` runtime trimming responsibilities
- `insightagent.api.providers`
- `insightagent.api.messages`
- old trace event lifecycle as the primary trace model

Keep or rewrite under new ownership:

- repository snapshot logic, rewritten as a graph preparation node.
- config loading and dotenv behavior.
- runtime permission and command validation semantics.
- file/search/execution/code-analysis tool behavior, rewritten as LangChain tools.
- MCP config and manager, with LangChain tool conversion.
- SWE-style eval runner, updated to invoke the graph runner.
- usage tracking, rewritten from LangChain response metadata and Langfuse callback data.
- transcript export, rewritten from checkpointed LangChain messages.

## Testing Strategy

Tests move from old class-level behavior to graph behavior:

- Unit-test graph nodes directly.
- Unit-test routing decisions with fake state.
- Unit-test tools as LangChain tools with permission wrappers.
- Unit-test task-contract rejection and recovery paths.
- Unit-test checkpoint resume using a fixed thread id.
- Unit-test Langfuse callback creation without requiring credentials.
- Unit-test Langfuse callback propagation and flush/shutdown on success and failure.
- Keep CLI smoke tests with fake model/tool graph where possible.
- Update SWE-style eval tests to assert graph runner behavior and final verification.
- Add a test migration matrix documenting every old runtime test file as rewritten, deleted with replacement coverage, or retained for non-production helper behavior.

The old `FakeModelClient` tests are deleted or rewritten to fake LangChain chat model responses.

## Acceptance Criteria

- `python -m pytest` passes.
- From a clean environment, `pip install -e .[dev]` succeeds and `python -c "import langgraph, langchain_core, langfuse"` succeeds.
- `insightagent-run --no-trace --workspace <tmp> --task <simple coding task>` executes through LangGraph, not `CodeAgent`.
- A static guard has no production CLI/runtime hits for `insightagent.agent.core`, `insightagent.api.providers`, `CodeAgent`, `ModelClient`, or old `TaskState`, except documented non-production migration tests if any remain.
- LangChain tools are instances of `BaseTool`/`StructuredTool`, expose typed schemas, execute through permission/path/command/task-contract wrappers, propagate callback config, and return model-visible text or JSON-serializable objects.
- Built-in tools and MCP tools share the same LangChain tool interface before graph construction.
- A fake-model graph test observes `plan -> implement -> verify -> repair -> verify -> summarize -> done`, with assertions on `last_tool_error`, `changed_files`, `verification_attempts`, `iteration`, and `phase_history`.
- Prose-only model output is rejected while action is still required.
- Task-contract tests cover exact verification command enforcement, inspect-before-mutate, failing-test read, test-file protection, existing non-test patch requirement, standalone demo-file rejection, optional-entrypoint rejection, destructive rewrite rejection, and post-failure inspection.
- A session can be resumed by `thread_id`; a specific checkpoint can be inspected or replayed by `checkpoint_id`; transcript export is generated from checkpointed LangChain messages.
- A new turn on an existing `thread_id` preserves transcript history but resets per-turn graph fields, so a previous `done` or `failed` phase cannot block the next task.
- Langfuse callback is attached when credentials are configured, no-ops cleanly without credentials, receives workspace/session/provider/model/tool metadata, is passed into graph invoke/stream config, records explicit runtime observations, and is flushed or shut down in `finally` on success and failure.
- SWE-style eval can run against the graph runner and still writes reports with verification status, patch data, failure mode, and debug trace path.
- A slow in-flight fake model, Python tool, MCP tool, and shell command with `--max-wall-seconds` exit cleanly as `phase=failed`, record `time_budget_exceeded`, preserve partial state, and flush/shut down Langfuse in `finally`.
- No production CLI path imports the old `CodeAgent` loop or old provider clients.

## Rollout

This is a breaking internal rewrite. The implementation should happen in small commits, but not by keeping a dual runtime:

1. Add dependencies and graph package skeleton.
2. Implement state, model factory, tool factory, and a minimal graph.
3. Implement SQLite checkpointing, graph session service, and transcript export.
4. Port built-in and MCP tools to LangChain tool contracts with state-producing `execute_tools`.
5. Port task contracts and SWE guardrails into graph policy.
6. Add Langfuse callbacks and explicit runtime observations.
7. Move CLI, slash commands, and eval runner to graph runner.
8. Delete old runtime modules and rewrite tests.
9. Update README and docs.
