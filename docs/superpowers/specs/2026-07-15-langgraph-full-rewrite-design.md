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

## Graph State

`AgentState` is a typed LangGraph state containing:

- `messages`: LangChain messages, append-only through a reducer.
- `workspace`: absolute workspace path as string.
- `task`: original user task.
- `phase`: `plan | inspect | implement | verify | repair | summarize | done | failed`.
- `iteration`: model/tool loop counter.
- `max_iterations`: graph guardrail.
- `verification_command`: optional expected command extracted from the task.
- `last_tool_error`: most recent failed tool result.
- `changed_files`: files changed by write/edit tools.
- `inspected_files`: files read or searched.
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

This replaces the hand-written `for iteration in range(...)` loop.

## Models

Remove custom provider clients as production runtime code. Use LangChain chat model initialization instead:

- OpenAI-compatible providers use LangChain OpenAI-compatible chat model support.
- Anthropic uses LangChain Anthropic integration.
- SiliconFlow is configured through OpenAI-compatible base URL and API key.

The project config keeps provider/model/base URL fields, but `insightagent.graph.models` maps them to LangChain model objects. Tests should use fake LangChain chat models rather than fake `ModelClient`.

## Tools

Rewrite tool exposure as LangChain `StructuredTool` objects.

The old tool implementation code can be used as source material, but the new contract is:

- Each tool has a typed schema.
- Each tool returns structured text or a small JSON-serializable object.
- Permission checks, command validation, workspace path resolution, and failure classification are wrappers around tool execution.
- Tool execution emits Langfuse-observable spans through LangChain callbacks.

The old `ToolRegistry` is removed as the agent-facing surface. It can be replaced by a `ToolRuntime` or `ToolExecutor` helper that is not visible to the model.

## Memory And Checkpointing

Replace custom session JSON with LangGraph checkpointing.

Phase 1 uses a local file-backed checkpoint adapter if available in the installed LangGraph version; otherwise, implement a small repository-local checkpoint backend behind LangGraph's checkpointer interface. Session ids map to LangGraph `thread_id`.

Project memory becomes a `prepare_task` input step that adds system/context messages, not a separate memory manager. Context trimming becomes a graph node that summarizes or prunes tool-heavy history before model invocation.

## MCP

MCP remains a first-class tool source, but MCP tools are converted to LangChain tools before graph construction. The graph does not know whether a tool is built-in or MCP; all tools share the same LangChain interface and permission wrapper.

## Observability

Langfuse becomes the primary trace sink.

- Create a Langfuse callback handler in `observability.py`.
- Pass callbacks through LangGraph invoke/stream config.
- Add trace metadata: workspace, session/thread id, provider, model, tool profile, task hash.
- Flush or shutdown Langfuse at CLI exit because `run_task` is short-lived.

Local console output remains a presentation layer, not the authoritative trace model. JSONL traces may be kept only as a debug export generated from graph events, not as the primary telemetry architecture.

## CLI And Public Commands

Keep script names for user convenience:

- `insightagent`
- `insightagent-run`

But internally they call the graph runner. CLI options that only exist for the old runtime are removed or renamed. Compatibility flags are not required unless they map cleanly to the new graph runtime.

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

## Testing Strategy

Tests move from old class-level behavior to graph behavior:

- Unit-test graph nodes directly.
- Unit-test routing decisions with fake state.
- Unit-test tools as LangChain tools with permission wrappers.
- Unit-test checkpoint resume using a fixed thread id.
- Unit-test Langfuse callback creation without requiring credentials.
- Keep CLI smoke tests with fake model/tool graph where possible.
- Update SWE-style eval tests to assert graph runner behavior and final verification.

The old `FakeModelClient` tests are deleted or rewritten to fake LangChain chat model responses.

## Acceptance Criteria

- `python -m pytest` passes.
- `insightagent-run --no-trace --workspace <tmp> --task <simple coding task>` executes through LangGraph, not `CodeAgent`.
- LangChain tools are visible to the model and execute through the unified tool wrapper.
- A failed verification routes through repair and then re-verification in graph state.
- A session can be resumed by thread id/checkpoint id.
- Langfuse callback is attached when credentials are configured and is flushed on process exit.
- No production CLI path imports the old `CodeAgent` loop or old provider clients.

## Rollout

This is a breaking internal rewrite. The implementation should happen in small commits, but not by keeping a dual runtime:

1. Add dependencies and graph package skeleton.
2. Implement state, model factory, tool factory, and a minimal graph.
3. Move CLI to graph runner.
4. Port built-in tools to LangChain tool contracts.
5. Add checkpointing and Langfuse callbacks.
6. Delete old runtime modules and rewrite tests.
7. Update README and docs.
