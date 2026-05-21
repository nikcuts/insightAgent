# InsightAgent V2 Context Memory Projectization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build InsightAgent V2.0 with project memory injection, tool-result truncation, completed-turn compaction, trace visibility, and a more formal package structure.

**Architecture:** Keep the current small package layout, but split V2 responsibilities into focused modules: config, project memory loading, prompt context building, and compaction. `CodeAgent` remains the loop orchestrator; CLI and task runner assemble configured agents from workspace, provider, memory, and prompt components.

**Tech Stack:** Python standard library, dataclasses, unittest, argparse, urllib-based provider clients.

---

## File Structure

- Create `insightagent/config.py`: `AgentConfig` runtime defaults for V2.
- Create `insightagent/project_memory.py`: memory-file discovery and UTF-8 loading.
- Create `insightagent/context.py`: system prompt construction from base prompt plus project memory.
- Create `insightagent/compaction.py`: deterministic text truncation and tool-message compaction helpers.
- Create `insightagent/factory.py`: shared agent assembly for CLI and task runner.
- Modify `insightagent/agent.py`: accept config-driven truncation and compaction, emit new trace events.
- Modify `insightagent/cli.py`: add workspace/config options and use factory.
- Modify `insightagent/run_task.py`: use factory and V2 config.
- Modify `insightagent/trace.py`: render `memory_loaded`, `tool_result_truncated`, and `history_compacted`.
- Modify `insightagent/__init__.py`: expose V2 version.
- Create `tests/test_project_memory.py`: project memory loading tests.
- Create `tests/test_context.py`: prompt construction tests.
- Create `tests/test_compaction.py`: truncation and compaction tests.
- Create `tests/test_factory.py`: configured agent assembly tests using fake clients or disabled provider construction where possible.
- Modify `tests/test_agent_loop.py`: add V2 truncation and completed-turn compaction loop tests.
- Modify `tests/test_providers.py`: keep existing provider conversion tests unchanged unless imports need adjustment.
- Create `pyproject.toml`: package metadata and console script.
- Modify `README.md`: document V2 behavior, CLI options, package shape, and remaining limitations.

## Task 1: Add V2 Runtime Config

**Files:**
- Create: `insightagent/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing config tests**

Create `tests/test_config.py`:

```python
from __future__ import annotations

import unittest

from insightagent.config import AgentConfig


class AgentConfigTests(unittest.TestCase):
    def test_defaults_match_v2_design(self) -> None:
        config = AgentConfig()

        self.assertEqual(config.max_messages, 20)
        self.assertEqual(config.max_tool_iterations, 8)
        self.assertEqual(config.max_tool_result_chars, 6000)
        self.assertTrue(config.compact_completed_turns)
        self.assertEqual(config.compact_tool_result_chars, 1200)
        self.assertEqual(config.memory_filenames, ("MEMORY.md", ".codeagent.md"))

    def test_can_override_runtime_limits(self) -> None:
        config = AgentConfig(max_messages=5, max_tool_iterations=3, max_tool_result_chars=200)

        self.assertEqual(config.max_messages, 5)
        self.assertEqual(config.max_tool_iterations, 3)
        self.assertEqual(config.max_tool_result_chars, 200)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_config -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'insightagent.config'`.

- [ ] **Step 3: Implement `AgentConfig`**

Create `insightagent/config.py`:

```python
"""Runtime configuration for InsightAgent V2."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentConfig:
    """Configuration values shared by CLI, task runner, and CodeAgent."""

    max_messages: int = 20
    max_tool_iterations: int = 8
    max_tool_result_chars: int = 6000
    compact_completed_turns: bool = True
    compact_tool_result_chars: int = 1200
    memory_filenames: tuple[str, ...] = ("MEMORY.md", ".codeagent.md")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_config -v`

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add insightagent/config.py tests/test_config.py
git commit -m "feat: add v2 runtime config"
```

Expected: commit succeeds. If Git still reports `fatal: not a git repository`, record the blocker and continue without committing.

## Task 2: Add Project Memory Loading

**Files:**
- Create: `insightagent/project_memory.py`
- Test: `tests/test_project_memory.py`

- [ ] **Step 1: Write failing project memory tests**

Create `tests/test_project_memory.py`:

```python
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from insightagent.project_memory import load_project_memory


class ProjectMemoryTests(unittest.TestCase):
    def test_missing_memory_files_returns_empty_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            memory = load_project_memory(Path(directory), ("MEMORY.md", ".codeagent.md"))

        self.assertEqual(memory.entries, [])
        self.assertFalse(memory.has_entries)
        self.assertEqual(memory.source_names(), [])

    def test_loads_memory_file_from_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "MEMORY.md").write_text("Use concise answers.", encoding="utf-8")

            memory = load_project_memory(workspace, ("MEMORY.md", ".codeagent.md"))

        self.assertTrue(memory.has_entries)
        self.assertEqual(memory.source_names(), ["MEMORY.md"])
        self.assertEqual(memory.entries[0].content, "Use concise answers.")

    def test_loads_multiple_files_in_configured_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".codeagent.md").write_text("Agent rule.", encoding="utf-8")
            (workspace / "MEMORY.md").write_text("General memory.", encoding="utf-8")

            memory = load_project_memory(workspace, ("MEMORY.md", ".codeagent.md"))

        self.assertEqual(memory.source_names(), ["MEMORY.md", ".codeagent.md"])
        self.assertEqual([entry.content for entry in memory.entries], ["General memory.", "Agent rule."])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_project_memory -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'insightagent.project_memory'`.

- [ ] **Step 3: Implement project memory loader**

Create `insightagent/project_memory.py`:

```python
"""Project memory loading for InsightAgent V2."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MemoryEntry:
    path: Path
    name: str
    content: str


@dataclass(frozen=True)
class ProjectMemory:
    entries: list[MemoryEntry]

    @property
    def has_entries(self) -> bool:
        return bool(self.entries)

    def source_names(self) -> list[str]:
        return [entry.name for entry in self.entries]


def load_project_memory(workspace: Path, filenames: tuple[str, ...]) -> ProjectMemory:
    root = workspace.expanduser().resolve()
    entries: list[MemoryEntry] = []
    for filename in filenames:
        path = root / filename
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8").strip()
        entries.append(MemoryEntry(path=path, name=filename, content=content))
    return ProjectMemory(entries=entries)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_project_memory -v`

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add insightagent/project_memory.py tests/test_project_memory.py
git commit -m "feat: load project memory files"
```

Expected: commit succeeds, or Git blocker is recorded.

## Task 3: Add System Prompt Context Builder

**Files:**
- Create: `insightagent/context.py`
- Test: `tests/test_context.py`

- [ ] **Step 1: Write failing context tests**

Create `tests/test_context.py`:

```python
from __future__ import annotations

import unittest
from pathlib import Path

from insightagent.context import build_system_prompt
from insightagent.project_memory import MemoryEntry, ProjectMemory


class ContextTests(unittest.TestCase):
    def test_returns_base_prompt_when_memory_is_empty(self) -> None:
        prompt = build_system_prompt("Base prompt.", ProjectMemory(entries=[]))

        self.assertEqual(prompt, "Base prompt.")

    def test_appends_memory_sections_in_order(self) -> None:
        memory = ProjectMemory(
            entries=[
                MemoryEntry(path=Path("/repo/MEMORY.md"), name="MEMORY.md", content="General memory."),
                MemoryEntry(path=Path("/repo/.codeagent.md"), name=".codeagent.md", content="Agent rules."),
            ]
        )

        prompt = build_system_prompt("Base prompt.", memory)

        self.assertIn("Base prompt.", prompt)
        self.assertLess(prompt.index("## Project Memory: MEMORY.md"), prompt.index("## Project Memory: .codeagent.md"))
        self.assertIn("General memory.", prompt)
        self.assertIn("Agent rules.", prompt)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_context -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'insightagent.context'`.

- [ ] **Step 3: Implement context builder**

Create `insightagent/context.py`:

```python
"""System prompt construction for InsightAgent V2."""

from __future__ import annotations

from .project_memory import ProjectMemory


def build_system_prompt(base_prompt: str, project_memory: ProjectMemory) -> str:
    sections = [base_prompt.strip()]
    for entry in project_memory.entries:
        sections.append(f"## Project Memory: {entry.name}\n{entry.content.strip()}")
    return "\n\n".join(section for section in sections if section)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_context -v`

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add insightagent/context.py tests/test_context.py
git commit -m "feat: build system prompt with project memory"
```

Expected: commit succeeds, or Git blocker is recorded.

## Task 4: Add Truncation and Compaction Helpers

**Files:**
- Create: `insightagent/compaction.py`
- Test: `tests/test_compaction.py`

- [ ] **Step 1: Write failing compaction tests**

Create `tests/test_compaction.py`:

```python
from __future__ import annotations

import unittest

from insightagent.compaction import compact_completed_turn, compact_tool_result, truncate_text
from insightagent.messages import Message


class CompactionTests(unittest.TestCase):
    def test_short_text_is_unchanged(self) -> None:
        result = truncate_text("short", 20)

        self.assertEqual(result.text, "short")
        self.assertFalse(result.truncated)
        self.assertEqual(result.original_chars, 5)
        self.assertEqual(result.stored_chars, 5)

    def test_long_text_keeps_head_tail_and_count(self) -> None:
        text = "A" * 20 + "B" * 20 + "C" * 20

        result = truncate_text(text, 30)

        self.assertTrue(result.truncated)
        self.assertEqual(result.original_chars, 60)
        self.assertLessEqual(result.stored_chars, 60)
        self.assertIn("...[truncated", result.text)
        self.assertIn("chars]...", result.text)
        self.assertTrue(result.text.startswith("A"))
        self.assertTrue(result.text.endswith("C" * 9))

    def test_compact_tool_result_preserves_metadata(self) -> None:
        message = Message(role="tool", content="x" * 80, tool_call_id="call_1", is_error=True)

        compacted, result = compact_tool_result(message, 40)

        self.assertTrue(result.truncated)
        self.assertEqual(compacted.role, "tool")
        self.assertEqual(compacted.tool_call_id, "call_1")
        self.assertTrue(compacted.is_error)
        self.assertIn("...[truncated", compacted.content)

    def test_compact_completed_turn_compacts_only_large_tool_messages(self) -> None:
        messages = [
            Message(role="system", content="system"),
            Message(role="user", content="question"),
            Message(role="tool", content="x" * 80, tool_call_id="call_1"),
            Message(role="assistant", content="final"),
        ]

        compacted, count = compact_completed_turn(messages, 40)

        self.assertEqual(count, 1)
        self.assertEqual(compacted[0].content, "system")
        self.assertIn("...[truncated", compacted[2].content)
        self.assertEqual(compacted[3].content, "final")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_compaction -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'insightagent.compaction'`.

- [ ] **Step 3: Implement compaction helpers**

Create `insightagent/compaction.py`:

```python
"""Context truncation and compaction helpers for InsightAgent V2."""

from __future__ import annotations

from dataclasses import dataclass

from .messages import Message


@dataclass(frozen=True)
class TruncationResult:
    text: str
    truncated: bool
    original_chars: int
    stored_chars: int
    omitted_chars: int = 0


def truncate_text(text: str, max_chars: int) -> TruncationResult:
    original_chars = len(text)
    if max_chars <= 0 or original_chars <= max_chars:
        return TruncationResult(text=text, truncated=False, original_chars=original_chars, stored_chars=original_chars)

    marker_template = "\n\n...[truncated {omitted} chars]...\n\n"
    marker = marker_template.format(omitted=0)
    available = max(max_chars - len(marker), 2)
    head_chars = available // 2
    tail_chars = available - head_chars
    omitted = original_chars - head_chars - tail_chars
    marker = marker_template.format(omitted=omitted)
    compacted = f"{text[:head_chars]}{marker}{text[-tail_chars:]}"
    return TruncationResult(
        text=compacted,
        truncated=True,
        original_chars=original_chars,
        stored_chars=len(compacted),
        omitted_chars=omitted,
    )


def compact_tool_result(message: Message, max_chars: int) -> tuple[Message, TruncationResult]:
    result = truncate_text(message.content, max_chars)
    if not result.truncated:
        return message, result
    return (
        Message(
            role=message.role,
            content=result.text,
            tool_calls=list(message.tool_calls),
            tool_call_id=message.tool_call_id,
            is_error=message.is_error,
        ),
        result,
    )


def compact_completed_turn(messages: list[Message], max_chars: int) -> tuple[list[Message], int]:
    compacted_messages: list[Message] = []
    compacted_count = 0
    for message in messages:
        if message.role != "tool":
            compacted_messages.append(message)
            continue
        compacted, result = compact_tool_result(message, max_chars)
        if result.truncated:
            compacted_count += 1
        compacted_messages.append(compacted)
    return compacted_messages, compacted_count
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_compaction -v`

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add insightagent/compaction.py tests/test_compaction.py
git commit -m "feat: add context compaction helpers"
```

Expected: commit succeeds, or Git blocker is recorded.

## Task 5: Integrate V2 Config, Truncation, and Compaction into CodeAgent

**Files:**
- Modify: `insightagent/agent.py`
- Modify: `tests/test_agent_loop.py`

- [ ] **Step 1: Add failing agent integration tests**

Append these tests inside `AgentLoopTests` in `tests/test_agent_loop.py`:

```python
    def test_truncates_large_tool_result_before_next_model_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.txt"
            path.write_text("A" * 80 + "Z" * 80, encoding="utf-8")
            client = FakeModelClient(
                [
                    ModelResponse(tool_calls=[ToolCall(id="call_1", name="read_file", arguments={"path": str(path)})]),
                    ModelResponse(content="Done."),
                ]
            )
            agent = CodeAgent(client, max_tool_result_chars=60, compact_completed_turns=False)

            result = agent.run_turn("Read large file")

            self.assertEqual(result.content, "Done.")
            tool_messages = [message for message in client.calls[1] if message.role == "tool"]
            self.assertEqual(len(tool_messages), 1)
            self.assertIn("...[truncated", tool_messages[0].content)
            self.assertLess(len(tool_messages[0].content), 160)

    def test_emits_truncation_and_compaction_trace_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.txt"
            path.write_text("A" * 80 + "Z" * 80, encoding="utf-8")
            client = FakeModelClient(
                [
                    ModelResponse(tool_calls=[ToolCall(id="call_1", name="read_file", arguments={"path": str(path)})]),
                    ModelResponse(content="Done."),
                ]
            )
            agent = CodeAgent(
                client,
                max_tool_result_chars=60,
                compact_completed_turns=True,
                compact_tool_result_chars=40,
            )
            events: list[dict[str, Any]] = []

            agent.run_turn_with_trace("Read large file", trace=events.append)

            event_types = [event["type"] for event in events]
            self.assertIn("tool_result_truncated", event_types)
            self.assertIn("history_compacted", event_types)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_agent_loop -v`

Expected: FAIL with `TypeError: CodeAgent.__init__() got an unexpected keyword argument 'max_tool_result_chars'`.

- [ ] **Step 3: Modify `CodeAgent` constructor and tool execution path**

In `insightagent/agent.py`, add import:

```python
from .compaction import compact_completed_turn, compact_tool_result
from .config import AgentConfig
```

Change `CodeAgent.__init__` signature to:

```python
    def __init__(
        self,
        model_client: ModelClient,
        tools: ToolRegistry | None = None,
        memory: SlidingWindowMemory | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_tool_iterations: int | None = None,
        config: AgentConfig | None = None,
        max_tool_result_chars: int | None = None,
        compact_completed_turns: bool | None = None,
        compact_tool_result_chars: int | None = None,
    ) -> None:
        self.config = config or AgentConfig()
        self.model_client = model_client
        self.tools = tools or ToolRegistry()
        self.memory = memory or SlidingWindowMemory(max_messages=self.config.max_messages)
        self.max_tool_iterations = max_tool_iterations or self.config.max_tool_iterations
        self.max_tool_result_chars = max_tool_result_chars or self.config.max_tool_result_chars
        self.compact_completed_turns = (
            self.config.compact_completed_turns if compact_completed_turns is None else compact_completed_turns
        )
        self.compact_tool_result_chars = compact_tool_result_chars or self.config.compact_tool_result_chars
        self.messages: list[Message] = [Message(role="system", content=system_prompt)]
```

After `_execute_tool_call(...)` returns in `run_turn_with_trace`, compact before storing:

```python
                raw_tool_result = self._execute_tool_call(tool_call.id, tool_call.name, tool_call.arguments)
                tool_result, truncation = compact_tool_result(raw_tool_result, self.max_tool_result_chars)
                if truncation.truncated:
                    self._emit(
                        trace,
                        {
                            "type": "tool_result_truncated",
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "original_chars": truncation.original_chars,
                            "stored_chars": truncation.stored_chars,
                            "omitted_chars": truncation.omitted_chars,
                        },
                    )
```

Then emit `tool_result` using `tool_result`, and append `tool_result`.

Before returning final `AgentResult`, compact completed history:

```python
                if self.compact_completed_turns:
                    self.messages, compacted_count = compact_completed_turn(self.messages, self.compact_tool_result_chars)
                    if compacted_count:
                        self._emit(trace, {"type": "history_compacted", "compacted_count": compacted_count})
```

Apply the same final compaction before returning the max-iteration warning result.

- [ ] **Step 4: Run agent loop tests**

Run: `python3 -m unittest tests.test_agent_loop -v`

Expected: PASS.

- [ ] **Step 5: Run all tests so far**

Run: `python3 -m unittest discover -s tests -v`

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add insightagent/agent.py tests/test_agent_loop.py
git commit -m "feat: compact agent tool context"
```

Expected: commit succeeds, or Git blocker is recorded.

## Task 6: Add Trace Rendering for V2 Events

**Files:**
- Modify: `insightagent/trace.py`
- Modify: `tests/test_agent_loop.py`

- [ ] **Step 1: Add focused trace rendering test**

Append this test inside `AgentLoopTests`:

```python
    def test_trace_events_include_v2_context_events(self) -> None:
        client = FakeModelClient([ModelResponse(content="Done.")])
        agent = CodeAgent(client)
        events: list[dict[str, Any]] = []

        agent._emit(events.append, {"type": "memory_loaded", "sources": ["MEMORY.md", ".codeagent.md"]})
        agent._emit(
            events.append,
            {
                "type": "tool_result_truncated",
                "id": "call_1",
                "name": "read_file",
                "original_chars": 100,
                "stored_chars": 50,
                "omitted_chars": 50,
            },
        )
        agent._emit(events.append, {"type": "history_compacted", "compacted_count": 1})

        self.assertEqual([event["type"] for event in events], ["memory_loaded", "tool_result_truncated", "history_compacted"])
```

This verifies event shapes used by `ConsoleTracer`. Existing visual output is covered by smoke/manual use.

- [ ] **Step 2: Run test**

Run: `python3 -m unittest tests.test_agent_loop -v`

Expected: PASS before rendering changes, because event emission is generic.

- [ ] **Step 3: Add rendering branches**

In `insightagent/trace.py`, add branches inside `ConsoleTracer.__call__`:

```python
        elif event_type == "memory_loaded":
            sources = event.get("sources") or []
            rendered = ", ".join(sources) if sources else "none"
            self._section("MEMORY LOADED", rendered)
        elif event_type == "tool_result_truncated":
            self._section(
                "TOOL RESULT TRUNCATED",
                (
                    f"{event['name']} id={event['id']} "
                    f"original={event['original_chars']} stored={event['stored_chars']} "
                    f"omitted={event['omitted_chars']}"
                ),
            )
        elif event_type == "history_compacted":
            self._section("HISTORY COMPACTED", f"messages={event['compacted_count']}")
```

Update module docstring from V1.0 to V2.0.

- [ ] **Step 4: Run all tests**

Run: `python3 -m unittest discover -s tests -v`

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add insightagent/trace.py tests/test_agent_loop.py
git commit -m "feat: render v2 trace events"
```

Expected: commit succeeds, or Git blocker is recorded.

## Task 7: Add Shared Agent Factory and Memory Trace Event

**Files:**
- Create: `insightagent/factory.py`
- Test: `tests/test_factory.py`

- [ ] **Step 1: Write failing factory tests**

Create `tests/test_factory.py`:

```python
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from insightagent.config import AgentConfig
from insightagent.factory import build_agent_with_memory
from insightagent.messages import Message, ModelResponse
from insightagent.providers import ModelClient


class FakeModelClient(ModelClient):
    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        return ModelResponse(content="ok")


class FactoryTests(unittest.TestCase):
    def test_build_agent_with_memory_injects_workspace_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "MEMORY.md").write_text("Always run tests.", encoding="utf-8")
            events: list[dict[str, Any]] = []

            agent = build_agent_with_memory(
                model_client=FakeModelClient(),
                workspace=workspace,
                config=AgentConfig(),
                trace=events.append,
            )

        self.assertIn("## Project Memory: MEMORY.md", agent.messages[0].content)
        self.assertIn("Always run tests.", agent.messages[0].content)
        self.assertEqual(events[0]["type"], "memory_loaded")
        self.assertEqual(events[0]["sources"], ["MEMORY.md"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_factory -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'insightagent.factory'`.

- [ ] **Step 3: Implement shared factory**

Create `insightagent/factory.py`:

```python
"""Shared agent assembly helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .agent import CodeAgent, DEFAULT_SYSTEM_PROMPT
from .config import AgentConfig
from .context import build_system_prompt
from .memory import SlidingWindowMemory
from .project_memory import load_project_memory
from .providers import ModelClient
from .tools import ToolRegistry

TraceHandler = Callable[[dict[str, Any]], None]


def build_agent_with_memory(
    model_client: ModelClient,
    workspace: Path,
    config: AgentConfig | None = None,
    base_system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    tools: ToolRegistry | None = None,
    trace: TraceHandler | None = None,
) -> CodeAgent:
    resolved_config = config or AgentConfig()
    project_memory = load_project_memory(workspace, resolved_config.memory_filenames)
    if trace is not None:
        trace({"type": "memory_loaded", "sources": project_memory.source_names()})
    system_prompt = build_system_prompt(base_system_prompt, project_memory)
    return CodeAgent(
        model_client,
        tools=tools,
        memory=SlidingWindowMemory(max_messages=resolved_config.max_messages),
        system_prompt=system_prompt,
        config=resolved_config,
    )
```

- [ ] **Step 4: Run factory test**

Run: `python3 -m unittest tests.test_factory -v`

Expected: PASS.

- [ ] **Step 5: Run all tests**

Run: `python3 -m unittest discover -s tests -v`

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add insightagent/factory.py tests/test_factory.py
git commit -m "feat: assemble agents with project memory"
```

Expected: commit succeeds, or Git blocker is recorded.

## Task 8: Update CLI and Task Runner for V2 Assembly

**Files:**
- Modify: `insightagent/cli.py`
- Modify: `insightagent/run_task.py`
- Test: add CLI parser tests in `tests/test_cli.py`

- [ ] **Step 1: Write failing CLI parser tests**

Create `tests/test_cli.py`:

```python
from __future__ import annotations

import unittest

from insightagent.cli import build_parser


class CliTests(unittest.TestCase):
    def test_parser_accepts_v2_context_options(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "--provider",
                "openai",
                "--workspace",
                "/tmp/work",
                "--max-tool-result-chars",
                "500",
                "--no-compact",
            ]
        )

        self.assertEqual(args.provider, "openai")
        self.assertEqual(args.workspace, "/tmp/work")
        self.assertEqual(args.max_tool_result_chars, 500)
        self.assertTrue(args.no_compact)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cli -v`

Expected: FAIL because parser does not know V2 options.

- [ ] **Step 3: Update CLI parser and agent building**

In `insightagent/cli.py`, import:

```python
from pathlib import Path

from .config import AgentConfig
from .factory import build_agent_with_memory
```

Add parser options:

```python
    parser.add_argument("--workspace", default=".", help="Workspace used for project memory loading.")
    parser.add_argument("--max-tool-result-chars", type=int, default=6000)
    parser.add_argument("--no-compact", action="store_true", help="Disable completed-turn compaction.")
```

Change `build_agent` to:

```python
def build_agent(provider: str, workspace: str = ".", max_tool_result_chars: int = 6000, no_compact: bool = False) -> CodeAgent:
    if provider == "anthropic":
        client = AnthropicClient()
    else:
        client = OpenAICompatibleClient()
    config = AgentConfig(max_tool_result_chars=max_tool_result_chars, compact_completed_turns=not no_compact)
    return build_agent_with_memory(client, Path(workspace), config=config)
```

Change `main` agent construction:

```python
    agent = build_agent(args.provider, args.workspace, args.max_tool_result_chars, args.no_compact)
    print("InsightAgent V2.0. Type 'exit' or 'quit' to stop.")
```

- [ ] **Step 4: Update `run_task.py` to use factory**

In `insightagent/run_task.py`, import `AgentConfig` and `build_agent_with_memory`.

Inside `build_agent`, construct provider client as today, then:

```python
    config = AgentConfig(
        max_tool_iterations=max_tool_iterations,
        max_tool_result_chars=6000,
        compact_completed_turns=True,
    )
    return build_agent_with_memory(
        client,
        workspace=workspace,
        config=config,
        base_system_prompt=system_prompt,
    )
```

Keep provider selection and task prompt behavior unchanged.

- [ ] **Step 5: Run CLI tests and all tests**

Run:

```bash
python3 -m unittest tests.test_cli -v
python3 -m unittest discover -s tests -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add insightagent/cli.py insightagent/run_task.py tests/test_cli.py
git commit -m "feat: use v2 agent assembly in cli"
```

Expected: commit succeeds, or Git blocker is recorded.

## Task 9: Add Project Package Metadata

**Files:**
- Create: `pyproject.toml`
- Modify: `insightagent/__init__.py`

- [ ] **Step 1: Add package metadata**

Create `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "insightagent"
version = "2.0.0"
description = "A versioned research coding agent framework."
requires-python = ">=3.10"
readme = "README.md"
authors = [
  { name = "InsightAgent contributors" }
]

[project.scripts]
insightagent = "insightagent.cli:main"

[tool.setuptools]
packages = ["insightagent"]
```

- [ ] **Step 2: Update package version**

Replace `insightagent/__init__.py` with:

```python
"""InsightAgent package."""

__version__ = "2.0.0"
```

- [ ] **Step 3: Verify package metadata does not break tests**

Run:

```bash
python3 -m py_compile insightagent/*.py tests/*.py
python3 -m unittest discover -s tests -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

Run:

```bash
git add pyproject.toml insightagent/__init__.py
git commit -m "chore: add python package metadata"
```

Expected: commit succeeds, or Git blocker is recorded.

## Task 10: Update README for V2

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update current status and features**

Change current version to `V2.0 context and projectization`.

Add feature bullets:

```markdown
### Project Memory

InsightAgent V2.0 loads workspace memory files before constructing the agent system prompt:

- `MEMORY.md` for general project context.
- `.codeagent.md` for InsightAgent-specific instructions.

When both exist, `MEMORY.md` is injected first and `.codeagent.md` second.
```

- [ ] **Step 2: Document context controls**

Add:

```markdown
### Context Control

Large tool results are truncated before entering model-visible conversation history. Truncated results keep the head, tail, and omitted character count. After a final answer, completed-turn compaction can shorten bulky intermediate tool messages for future turns.
```

- [ ] **Step 3: Update usage commands**

Add CLI example:

```bash
python3 -m insightagent.cli \
  --provider openai \
  --workspace . \
  --max-tool-result-chars 6000
```

Add package entry point note:

```bash
insightagent --provider openai --workspace .
```

- [ ] **Step 4: Update limitations**

Ensure limitations still say:

```markdown
- No strict workspace boundary enforcement.
- No permission model for destructive commands.
- No safe edit tool.
- No grep/search tool.
- No self-healing repair state machine.
```

- [ ] **Step 5: Run documentation-adjacent verification**

Run:

```bash
python3 -m py_compile insightagent/*.py tests/*.py
python3 -m unittest discover -s tests -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add README.md
git commit -m "docs: document insightagent v2"
```

Expected: commit succeeds, or Git blocker is recorded.

## Task 11: Final Verification

**Files:**
- No new files.

- [ ] **Step 1: Run syntax verification**

Run:

```bash
python3 -m py_compile insightagent/*.py tests/*.py
```

Expected: no output and exit code 0.

- [ ] **Step 2: Run full unit test suite**

Run:

```bash
python3 -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 3: Run CLI help smoke**

Run:

```bash
python3 -m insightagent.cli --help
```

Expected: help output includes `--workspace`, `--max-tool-result-chars`, and `--no-compact`.

- [ ] **Step 4: Run task runner help smoke**

Run:

```bash
python3 -m insightagent.run_task --help
```

Expected: help output still shows provider, workspace, task, timeout, and trace options.

- [ ] **Step 5: Inspect changed files**

Run:

```bash
git status --short
```

Expected: if Git metadata has been repaired, only intentional V2 files appear. If Git remains broken, record that `.git` is empty and `git status` cannot be used.

## Spec Coverage Checklist

- Project memory loading: Tasks 2, 3, 7, 8, 10.
- Deterministic memory precedence: Tasks 2 and 3.
- Tool-result truncation: Tasks 4 and 5.
- Completed-turn compaction: Tasks 4 and 5.
- Trace events: Tasks 5, 6, 7.
- CLI/task-runner assembly: Tasks 7 and 8.
- Package structure: Task 9.
- README/projectization: Task 10.
- Verification: Task 11.

## Known Execution Note

The current project directory contains an empty `.git` directory, so Git commands may fail with `fatal: not a git repository`. Do not let that block implementation. Record the failure at commit steps and continue. Repairing or reinitializing Git metadata is a separate workspace-maintenance task.
