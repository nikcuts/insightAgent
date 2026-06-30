"""Small-model resilience layer.

The agent loop is designed so that *weak* models (e.g. small 7B instruct models
that are unreliable at native function-calling) can still complete real coding
tasks. The functions here translate what weak models naturally produce -- code
blocks, textual tool-call protocols, slightly malformed JSON -- into executable
tool calls, and turn raw tool failures into diagnostic, goal-anchored repair
prompts.

Everything in this module is pure-Python (stdlib only) and deterministic so it
can be unit-tested without a network or a real model.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Callable

from .messages import ToolCall
from .runtime.failure_classifier import FailureKind
from .runtime.types import ToolExecutionResult


# ---------------------------------------------------------------------------
# Lenient JSON parsing (A4)
# ---------------------------------------------------------------------------

_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def loads_lenient(raw: str) -> tuple[dict | None, bool]:
    """Best-effort JSON object parse.

    Returns ``(parsed, repaired)``. ``parsed`` is ``None`` when even the
    repaired text is not valid JSON. ``repaired`` is ``True`` when the strict
    parse failed but a repaired parse succeeded.
    """

    raw = raw.strip()
    if not raw:
        return {}, False
    try:
        return json.loads(raw), False
    except json.JSONDecodeError:
        pass

    candidate = raw
    # Strip code fences a model may have wrapped the JSON in.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()
    # Drop a trailing comma before a closing brace/bracket.
    candidate = _TRAILING_COMMA.sub(r"\1", candidate)
    # Trim anything after the last balanced closing brace.
    last_brace = candidate.rfind("}")
    if last_brace != -1:
        candidate = candidate[: last_brace + 1]
    try:
        return json.loads(candidate), True
    except json.JSONDecodeError:
        return None, True


# ---------------------------------------------------------------------------
# Text -> tool-call extraction (A1 + A2)
# ---------------------------------------------------------------------------

_FILENAME_RE = re.compile(r"([A-Za-z0-9_./\-]+\.(?:py|txt|md|json|js|ts|tsx|html|css|sh|c|cpp|h|hpp|java|go|rs|rb|toml|yaml|yml))")
_TOOL_CALL_TAG_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_TOOL_CALL_FENCE_RE = re.compile(r"```(?:tool_call|tool|json)\s*(\{.*?\})\s*```", re.DOTALL)
_CODE_FENCE_RE = re.compile(r"```([A-Za-z0-9_+\-]*)\n(.*?)```", re.DOTALL)

_LANG_DEFAULT_FILENAME = {
    "python": "main.py",
    "py": "main.py",
    "javascript": "main.js",
    "js": "main.js",
    "typescript": "main.ts",
    "ts": "main.ts",
    "bash": "script.sh",
    "sh": "script.sh",
    "": "solution.txt",
}


@dataclass(frozen=True)
class ExtractedToolCall:
    tool_call: ToolCall
    source: str  # "protocol" | "codeblock"


class ToolCallExtractor:
    """Recover executable tool calls from assistant *text* when the provider
    returned no native ``tool_calls``.
    """

    def __init__(self, default_write_tool: str = "write_file") -> None:
        self.default_write_tool = default_write_tool

    def extract(
        self,
        content: str,
        available_tools: set[str],
        task: str = "",
        allow_codeblock_write: bool = True,
        counter_start: int = 1,
    ) -> list[ExtractedToolCall]:
        if not content:
            return []
        recovered: list[ExtractedToolCall] = []
        index = counter_start

        # Strategy 1+2: explicit textual tool-call protocol.
        for match in list(_TOOL_CALL_TAG_RE.finditer(content)) + list(_TOOL_CALL_FENCE_RE.finditer(content)):
            parsed, _repaired = loads_lenient(match.group(1))
            if not isinstance(parsed, dict):
                continue
            name = parsed.get("name")
            if not isinstance(name, str) or name not in available_tools:
                continue
            arguments = parsed.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            recovered.append(
                ExtractedToolCall(
                    tool_call=ToolCall(id=f"recovered_{index}", name=name, arguments=arguments),
                    source="protocol",
                )
            )
            index += 1
        if recovered:
            return recovered

        # Strategy 3: a bare code block -> synthesize a write_file call so the
        # weak model's "here is the code" answer still produces a real file.
        if allow_codeblock_write and self.default_write_tool in available_tools:
            fence = _CODE_FENCE_RE.search(content)
            if fence:
                lang = fence.group(1).strip().lower()
                code = fence.group(2)
                # Ignore code blocks that are clearly the tool-protocol itself.
                if code.strip() and not code.strip().startswith("{"):
                    filename = self._infer_filename(task, content, lang)
                    recovered.append(
                        ExtractedToolCall(
                            tool_call=ToolCall(
                                id=f"recovered_{index}",
                                name=self.default_write_tool,
                                arguments={"path": filename, "content": _ensure_trailing_newline(code)},
                            ),
                            source="codeblock",
                        )
                    )
        return recovered

    def _infer_filename(self, task: str, content: str, lang: str) -> str:
        for source in (task, content):
            match = _FILENAME_RE.search(source or "")
            if match:
                return match.group(1)
        return _LANG_DEFAULT_FILENAME.get(lang, "solution.txt")


def _ensure_trailing_newline(text: str) -> str:
    text = text.strip("\n")
    return text + "\n" if text else text


# ---------------------------------------------------------------------------
# Exponential backoff for environment/network/timeout failures (B2)
# ---------------------------------------------------------------------------

@dataclass
class RetryPolicy:
    """Exponential backoff for retryable environment failures.

    ``sleep`` is injectable so tests run with zero real delay.
    """

    base_delay: float = 0.5
    max_delay: float = 8.0
    max_attempts: int = 2
    sleep: Callable[[float], None] = time.sleep

    def backoff_delay(self, attempt: int) -> float:
        """Delay (seconds) before retry ``attempt`` (1-indexed)."""

        return min(self.max_delay, self.base_delay * (2 ** max(0, attempt - 1)))

    def wait(self, attempt: int) -> float:
        delay = self.backoff_delay(attempt)
        if delay > 0:
            self.sleep(delay)
        return delay


# Failures that exponential-backoff retry can plausibly recover from.
BACKOFF_FAILURE_KINDS = {FailureKind.NETWORK_ERROR, FailureKind.TIMEOUT}
# Failures where retrying the identical call is pointless.
PERMANENT_FAILURE_KINDS = {
    FailureKind.PERMISSION_DENIED,
    FailureKind.TOOL_PROTOCOL_ERROR,
    FailureKind.ENVIRONMENT_ERROR,
}


# ---------------------------------------------------------------------------
# Diagnostic, goal-anchored repair prompts (B3 - repair drift)
# ---------------------------------------------------------------------------

_MCP_UNKNOWN_RE = re.compile(r"unknown tool:\s*mcp_", re.IGNORECASE)


def _root_cause_hint(result: ToolExecutionResult) -> str:
    content = result.content or ""
    if _MCP_UNKNOWN_RE.search(content):
        return (
            "Root cause: a tool whose name starts with `mcp_` is unavailable. This almost always means the "
            "MCP server is not running or is missing from configuration. Check mcp_config.json / the enabled "
            "MCP servers. Do NOT try to install dependencies or pip packages to fix this; instead use a "
            "built-in tool or report the MCP configuration gap."
        )
    kind = result.failure_kind
    if kind == FailureKind.NETWORK_ERROR:
        return (
            "Root cause: this is a network / remote-service failure. Retrying the same call is very likely to "
            "fail again. Do not blindly retry; use a non-network approach or clearly report the network blocker."
        )
    if kind == FailureKind.TIMEOUT:
        return (
            "Root cause: the command timed out. Repeating it unchanged will probably time out again. Narrow the "
            "command, inspect incrementally, or report the blocker."
        )
    if kind == FailureKind.ENVIRONMENT_ERROR:
        return (
            "Root cause: a local environment / missing-dependency problem (missing file, command, or module). "
            "Inspect what is actually available before retrying; do not loop on the same failing command."
        )
    if kind == FailureKind.PERMISSION_DENIED:
        return (
            "Root cause: the runtime permission policy denied this operation. Choose an allowed tool or explain "
            "the limit; retrying the same call will be denied again."
        )
    if kind == FailureKind.TEST_FAILURE:
        return (
            "Root cause: a verification command reported a failure. Read the exact error, then EDIT the code to "
            "fix it before rerunning the verification."
        )
    if kind == FailureKind.CODE_ERROR:
        return (
            "Root cause: a code-level error (syntax/name/type/value). Open the relevant file, fix the specific "
            "line indicated by the traceback, then rerun."
        )
    return (
        "Inspect the exact error text above before deciding the next action. Avoid repeating an action that just "
        "failed without changing something concrete."
    )


def build_repair_prompt(result: ToolExecutionResult, task: str = "") -> str:
    """Construct a diagnostic, goal-anchored repair instruction."""

    if result.retryable:
        retry_guidance = "You may retry only if the next call changes strategy or arguments."
    else:
        retry_guidance = "Do not repeat the same tool call unchanged; the runtime marked it non-retryable."

    parts = [
        f"A tool call (`{result.name}`) failed. Enter repair mode.",
        _root_cause_hint(result),
        retry_guidance,
    ]
    if result.repair_guidance:
        parts.append(result.repair_guidance)
    if task:
        goal = task.strip().splitlines()[0][:300]
        parts.append(
            f"Keep the ORIGINAL GOAL in mind: \"{goal}\". Every repair action must move toward this goal -- "
            "before each tool call, make sure it is clearly related to the goal so you do not drift onto a "
            "different problem."
        )
    return " ".join(part for part in parts if part)
