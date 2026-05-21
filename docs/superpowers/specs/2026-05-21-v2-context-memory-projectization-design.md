# InsightAgent V2.0 Context, Memory, and Projectization Design

## Summary

InsightAgent V2.0 upgrades the current V1.0 baseline into a more credible research agent framework, runnable local CLI, and package-shaped Python project. The main functional goal is context control: project memory injection, tool-output truncation, and completed-turn compaction. The supporting engineering goal is to make the project look and feel less like a toy while preserving the small, versioned research structure.

This design intentionally keeps V2 focused on context and projectization. Workspace safety, command permission prompts, file guards, and search/self-healing remain later-version work.

## Goals

- Load project memory files from the active workspace.
- Inject memory into the system prompt with deterministic precedence.
- Truncate large tool results before they enter model-visible conversation history.
- Compact completed task history so large intermediate tool results do not keep occupying context.
- Emit trace events for memory loading, truncation, and compaction.
- Add a minimal but formal project package shape with `pyproject.toml`, clearer modules, stronger tests, and updated README documentation.
- Keep the implementation standard-library only unless a later requirement justifies dependencies.

## Non-Goals

- No strict workspace boundary enforcement in V2.
- No command permission system in V2.
- No safe edit tool in V2.
- No grep/search tool in V2.
- No source layout migration to `src/` in V2.
- No provider SDK adoption in V2.

## User-Facing Behavior

When a user runs the CLI or traced task runner from a workspace, InsightAgent looks for memory files and includes them in the system prompt. The agent can then follow project-specific instructions across turns without requiring the user to paste them into every request.

Supported memory files:

- `MEMORY.md`: general project memory.
- `.codeagent.md`: InsightAgent-specific project rules.

If both files exist, both are loaded. `MEMORY.md` is injected first as general context, and `.codeagent.md` is injected after it as more specific agent instructions. This gives `.codeagent.md` higher effective priority.

Long tool results are stored in truncated form before they are sent back to the model. The truncated content keeps a head section, a tail section, and an omitted-character count. Trace output shows when truncation happened.

After a turn reaches a final answer, completed-turn compaction replaces bulky intermediate tool messages with shorter summaries when they exceed the configured compaction threshold. This keeps future turns cleaner while preserving the fact that tools ran and whether they succeeded.

## Architecture

### `config.py`

Add `AgentConfig` as the central runtime configuration object.

Recommended fields:

- `max_messages: int = 20`
- `max_tool_iterations: int = 8`
- `max_tool_result_chars: int = 6000`
- `compact_completed_turns: bool = True`
- `compact_tool_result_chars: int = 1200`
- `memory_filenames: tuple[str, ...] = ("MEMORY.md", ".codeagent.md")`

The CLI and task runner should build an `AgentConfig` and pass its values into the agent assembly path. This avoids scattering defaults across `agent.py`, `memory.py`, `run_task.py`, and tests.

### `project_memory.py`

Add project-memory loading.

Responsibilities:

- Resolve memory files relative to a workspace path.
- Load supported UTF-8 memory files.
- Return a structured `ProjectMemory` object containing loaded entries.
- Preserve source filenames so trace and tests can inspect what was loaded.

Suggested types:

- `MemoryEntry(path: Path, name: str, content: str)`
- `ProjectMemory(entries: list[MemoryEntry])`

The loader should silently return empty memory when files do not exist. If a configured memory file exists but cannot be read as UTF-8, it should raise a clear exception.

### `context.py`

Add system-prompt construction.

Responsibilities:

- Accept the base system prompt and `ProjectMemory`.
- Produce the final system prompt text.
- Keep memory sections visually separated with stable headings.
- Preserve deterministic order.

The final prompt shape should be:

```text
<base system prompt>

## Project Memory: MEMORY.md
...

## Project Memory: .codeagent.md
...
```

### `compaction.py`

Add pure helpers for truncation and completed-turn compaction.

Responsibilities:

- `truncate_text(text, max_chars)` returns the text and metadata describing whether truncation occurred.
- `compact_tool_result(message, max_chars)` returns a compacted tool message when content is too large.
- `compact_completed_turn(messages, max_chars)` walks recent messages after a final answer and compacts bulky tool messages.

The truncation format should preserve the beginning and end of the original text:

```text
<head>

...[truncated N chars]...

<tail>
```

These helpers should be deterministic and easy to unit test.

### `memory.py`

Keep `SlidingWindowMemory` responsible for retaining all system messages and the latest `N` non-system messages. It should not know how to load project memory files. If completed-turn compaction is integrated here, it should call helpers from `compaction.py` instead of owning truncation logic.

### `agent.py`

Keep `CodeAgent` as the orchestration layer.

Changes:

- Accept config values for message limits, tool-iteration limits, tool-result truncation, and completed-turn compaction.
- Emit trace events for truncation and compaction.
- Truncate tool results before appending them to `self.messages`.
- Compact completed-turn history after producing a final answer when enabled.

`CodeAgent` should not read files from disk to discover memory. That belongs in the assembly path used by CLI and task runner.

### `trace.py`

Add rendering for:

- `memory_loaded`: list of loaded memory source names.
- `tool_result_truncated`: tool call id, tool name, original length, stored length.
- `history_compacted`: number of messages compacted.

Trace should stay readable and should not print full memory contents by default.

### CLI and Task Runner

`cli.py` and `run_task.py` should:

- Resolve a workspace path.
- Build `AgentConfig`.
- Load project memory.
- Build the final system prompt.
- Construct `CodeAgent`.

The CLI should gain practical options without becoming a large command framework:

- `--workspace`
- `--max-tool-result-chars`
- `--no-compact`

`run_task.py` should keep its traced demo behavior and use the same assembly path where possible.

## Engineering Projectization

Add `pyproject.toml` with minimal project metadata:

- project name
- version
- Python requirement
- package discovery or explicit package list
- console script entry point: `insightagent = "insightagent.cli:main"`

The project should remain standard-library only for V2.

Update README:

- Current status becomes V2.0.
- Document project memory files.
- Document truncation and compaction behavior.
- Add CLI options.
- Explain V1 to V2 comparison.
- Keep V3 safety limitations explicit.

Tests should remain under `tests/`. Add focused tests rather than broad snapshots.

## Error Handling

- Missing memory files: no error.
- Existing unreadable memory file: clear exception.
- Long tool output: truncate and continue.
- Tool error output: still truncate if needed, while preserving `is_error=True`.
- Compaction failure: should be avoided through pure tested helpers; no broad exception swallowing is planned.
- Provider errors: existing provider error behavior remains unchanged.

## Testing Plan

Add tests for:

- Project memory loading with no files.
- Project memory loading with `MEMORY.md`.
- Project memory loading with both `MEMORY.md` and `.codeagent.md` in deterministic order.
- System prompt construction with loaded memory sections.
- Long text truncation preserving head, tail, and omitted count.
- Tool result truncation before the next model request.
- Completed-turn compaction after final answer.
- Trace events for memory loading, truncation, and compaction.
- CLI/task-runner assembly defaults for workspace resolution, memory loading, truncation, and compaction.

Existing tests for agent loop, provider conversion, trace lifecycle, and sliding-window memory must continue to pass.

## Implementation Boundaries

This is a single V2 implementation effort, but it should be built in small steps:

1. Add config and pure memory/context/compaction helpers with tests.
2. Integrate prompt construction into CLI/task runner.
3. Integrate truncation and compaction into `CodeAgent`.
4. Add trace rendering for new events.
5. Add `pyproject.toml` and README updates.
6. Run syntax checks and unit tests.

Because the current `.git` directory is empty and Git does not recognize the project as a repository, committing this design or later implementation may require initializing or repairing Git metadata first.
