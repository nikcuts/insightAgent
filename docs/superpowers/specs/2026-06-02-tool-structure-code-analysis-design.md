# Tool Structure and Code Analysis Upgrade Design

Date: 2026-06-02

## Context

InsightAgent V5.0 currently has a compact but useful coding-agent runtime: session persistence, runtime config loading, slash commands, usage tracking, task lifecycle guidance, workspace-scoped tools, and tests around the core loop. Compared with the reference project at `/home/dinghanchen/stuckin/codeagent/Code_Agent`, the main gap is not the existence of an agent loop. The gap is productized structure and extension surface.

The first upgrade should improve structure and add code-understanding tools without changing the runtime contract. MCP, Web UI, prompt directory migration, and provider restructuring stay out of this first phase.

## Gap Audit

| Capability | InsightAgent V5.0 status | Code_Agent reference | Decision |
| --- | --- | --- | --- |
| Agent loop | Tool-call loop, repair prompts, usage, sessions | ReAct JSON loop | Keep InsightAgent loop |
| Session persistence | JSON sessions, resume, export | Conversation history | Keep InsightAgent session system |
| Config | User/project/local/CLI precedence | Env/config examples | Keep and extend later |
| Tool safety | `ToolContext`, workspace boundaries, permissions | More direct tool execution | Keep InsightAgent safety model |
| Tool organization | Most tools in `insightagent/tools.py` | Split tool modules | Refactor in phase 1 |
| Code analysis | Missing AST/signature/dependency/metrics tools | Present in `code_analysis_tools.py` | Add in phase 1 |
| MCP | Missing | Config/client/manager/wrapper | Phase 2 |
| Planner | Task phase guidance, no standalone planner | Standalone planner | Later |
| Context compression | Deterministic compaction and sliding memory | LLM compressor | Keep current approach |
| Provider structure | Single provider module | Split clients | Later |
| Prompt management | Inline prompt | `prompts/` directory | Later |
| Web UI | Missing | Next.js frontend | Later |
| Tests | Existing unit tests | No test directory observed | Preserve and expand tests |

## Recommended Roadmap

### Phase 1: Tool Structure and Code Analysis

Refactor tools into a package, add code analysis tools, preserve existing tool names and registry behavior, and update tests and README.

### Phase 2: MCP Integration

Add MCP config loading, client process management, manager, tool wrapper, examples, and guide. This phase should use mockable boundaries because MCP involves subprocess and JSON-RPC behavior.

### Phase 3: Productization

Move prompts into a prompt package, consider provider/client splitting, add richer slash commands such as `/tools` and `/mcp`, and only then evaluate TUI or Web UI options.

## Phase 1 Goals

1. Make tool code easier to understand and extend.
2. Add code-analysis tools that help the agent inspect Python projects.
3. Preserve compatibility for existing imports and tests where practical.
4. Avoid changing the model loop, provider behavior, config precedence, sessions, or permission model.

## Non-Goals

1. No MCP implementation in phase 1.
2. No Web UI or API server.
3. No planner rewrite.
4. No provider/client restructuring.
5. No LLM-based compression.
6. No broad CLI redesign.

## Architecture

Convert `insightagent/tools.py` from the main implementation file into a compatibility module that re-exports the public tool API from a new `insightagent/tools/` package.

Target structure:

```text
insightagent/tools/
  __init__.py
  base.py
  execution_tools.py
  file_tools.py
  search_tools.py
  code_analysis_tools.py
  registry.py
```

Responsibilities:

- `base.py`: `Tool` protocol and shared helper functions.
- `execution_tools.py`: `ExecuteCommandTool`.
- `file_tools.py`: `ReadFileTool`, `WriteFileTool`, `EditFileTool`.
- `search_tools.py`: `GrepSearchTool`, `GlobSearchTool`.
- `code_analysis_tools.py`: Python AST and metrics tools.
- `registry.py`: `ToolRegistry`, schemas, execution dispatch, and default tool construction.
- `__init__.py`: public exports used by existing modules.

The old import path `from insightagent.tools import ToolRegistry` must keep working.

## Code Analysis Tools

Add these tools:

| Tool | Purpose | Inputs |
| --- | --- | --- |
| `parse_ast` | Summarize Python imports, top-level classes, functions, and globals | `path` |
| `get_function_signature` | Return function/method signature metadata | `path`, `function_name` |
| `find_dependencies` | Classify imports as stdlib, third-party, or local-ish | `path` |
| `get_code_metrics` | Return lines, blank lines, comments, functions, classes, imports | `path` |

All tools must:

1. Use `ToolContext.resolve_workspace_path`.
2. Reject non-Python paths where applicable.
3. Return deterministic JSON where the result is structured.
4. Surface syntax errors as clear tool output rather than crashing the agent loop.
5. Avoid reading binary-looking or unbounded files; reuse existing file-size protections where practical.

## Data Flow

1. `run_task.py` creates a `ToolContext`.
2. `ToolRegistry.default(context)` or equivalent default construction registers file, search, execution, and code-analysis tools.
3. `agent.py` asks the registry for schemas.
4. The provider receives the expanded tool list.
5. Tool calls route through the registry to each tool's `run(arguments)` method.
6. Results are passed through the existing truncation, repair, session, and task-state logic.

## Error Handling

The registry should keep current behavior: unknown tools and tool exceptions become error `Message` objects rather than process crashes.

Code-analysis tools should return human-readable errors for:

- Missing files.
- Paths outside the workspace.
- Non-file paths.
- Non-Python files.
- Python syntax errors.

Permission rules remain controlled by `ToolContext`; analysis tools are read-only and should not call `check_write_allowed`.

## Compatibility

Existing tests should continue to import:

```python
from insightagent.tools import ToolRegistry
```

Existing tool names should remain unchanged:

- `execute_command`
- `read_file`
- `write_file`
- `edit_file`
- `grep_search`
- `glob_search`

New names are additive:

- `parse_ast`
- `get_function_signature`
- `find_dependencies`
- `get_code_metrics`

## Testing

Add focused tests for:

1. Registry still exposes existing tool schemas.
2. Registry exposes new code-analysis tools.
3. `parse_ast` returns imports, functions, classes, and globals for a sample Python file.
4. `get_function_signature` handles normal functions, async functions, methods, annotations, and missing names.
5. `find_dependencies` classifies imports deterministically enough for local tests.
6. `get_code_metrics` counts basic file metrics.
7. Non-Python files and syntax errors produce clear outputs.
8. Existing unit suite still passes.

Verification commands:

```bash
python3 -m py_compile insightagent/*.py tests/*.py
python3 -m unittest discover -s tests -v
```

The py_compile command may need to be expanded after converting `insightagent/tools.py` into a package, for example:

```bash
python3 -m py_compile $(find insightagent tests -name '*.py' -print)
```

## Documentation

Update README architecture and tool sections to describe:

- The new tool package structure.
- The four code-analysis tools.
- The fact that MCP remains a planned phase 2 feature.

## Acceptance Criteria

1. `python3 -m unittest discover -s tests -v` passes.
2. `python3 -m py_compile $(find insightagent tests -name '*.py' -print)` passes.
3. Existing public imports of `insightagent.tools` still work.
4. Existing tool names and behavior remain available.
5. New code-analysis tools are available through `ToolRegistry.schemas()`.
6. README reflects the upgraded structure.
