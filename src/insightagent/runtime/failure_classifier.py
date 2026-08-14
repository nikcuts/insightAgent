"""Failure classification for tool execution results."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .tool_context import PermissionDenied, SandboxUnavailable, WorkspaceViolation


class FailureKind(str, Enum):
    NONE = "none"
    CODE_ERROR = "code_error"
    TEST_FAILURE = "test_failure"
    ENVIRONMENT_ERROR = "environment_error"
    NETWORK_ERROR = "network_error"
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    TOOL_PROTOCOL_ERROR = "tool_protocol_error"
    UNKNOWN_ERROR = "unknown_error"


@dataclass(frozen=True)
class FailureClassification:
    kind: FailureKind
    retryable: bool
    repair_guidance: str


class FailureClassifier:
    network_patterns = (
        r"could not resolve host",
        r"temporary failure in name resolution",
        r"network is unreachable",
        r"connection refused",
        r"name or service not known",
        r"ssl.*error",
        r"timed out",
        r"timeout was reached",
    )
    environment_patterns = (
        r"command not found",
        r"no such file or directory",
        r"file does not exist",
        r"path is not a file",
        r"cannot open file",
        r"module not found",
        r"modulenotfounderror",
        r"permission denied",
    )
    test_patterns = (
        r"\bfailed\b",
        r"assertionerror",
        r"assert .* failed",
        r"tests? failed",
    )
    code_patterns = (
        r"syntaxerror",
        r"nameerror",
        r"typeerror",
        r"valueerror",
        r"indentationerror",
        r"traceback \(most recent call last\)",
    )

    def classify(
        self,
        tool_name: str,
        content: str,
        is_error: bool,
        exception: BaseException | None = None,
    ) -> FailureClassification:
        if not is_error and exception is None:
            return FailureClassification(FailureKind.NONE, retryable=False, repair_guidance="")
        if exception is not None:
            return self.classify_exception(exception, tool_name=tool_name)
        lowered = content.lower()
        if _matches(lowered, self.network_patterns):
            return FailureClassification(
                FailureKind.NETWORK_ERROR,
                retryable=False,
                repair_guidance=(
                    "This looks like a network or remote-service failure. Repeating the same tool call is unlikely "
                    "to help; explain the environment issue or choose a non-network fallback."
                ),
            )
        if _matches(lowered, self.environment_patterns):
            return FailureClassification(
                FailureKind.ENVIRONMENT_ERROR,
                retryable=False,
                repair_guidance=(
                    "This looks like a local environment or missing-dependency problem. Do not blindly retry the "
                    "same command; inspect available files/tools or report the environment blocker."
                ),
            )
        if tool_name in {"execute_command", "run_verification"} and (
            "replacement index" in lowered or "indexerror" in lowered
        ):
            return FailureClassification(
                FailureKind.CODE_ERROR,
                retryable=False,
                repair_guidance=(
                    "The verification failed inside the application with a format string IndexError. "
                    "Inspect the relevant source lines around the format string, then make the number of "
                    "placeholders, column widths, headers, and row arguments match."
                ),
            )
        if tool_name in {"execute_command", "run_verification"} and "did not raise" in lowered:
            return FailureClassification(
                FailureKind.TEST_FAILURE,
                retryable=False,
                repair_guidance=(
                    "A pytest.raises expectation did not raise at the displayed call site. Inspect the "
                    "constructor or function invoked on that failing line and add validation there; do not "
                    "patch a later registration path that is not executed by the failing call."
                ),
            )
        if (
            tool_name in {"execute_command", "run_verification"}
            and "pytest.raises(valueerror)" in lowered
            and "assertionerror" in lowered
        ):
            return FailureClassification(
                FailureKind.TEST_FAILURE,
                retryable=False,
                repair_guidance=(
                    "The failing path already detects the invalid input with an assert, but the test expects "
                    "an explicit ValueError. Inspect the stack line containing the assert and replace that "
                    "specific assertion with a ValueError raise while preserving surrounding behavior."
                ),
            )
        if _matches(lowered, self.test_patterns) and tool_name in {"execute_command", "run_verification"}:
            return FailureClassification(
                FailureKind.TEST_FAILURE,
                retryable=False,
                repair_guidance="A verification command failed. Inspect the failure and edit the code before rerunning tests.",
            )
        if tool_name == "edit_file" and "old text not found" in lowered:
            return FailureClassification(
                FailureKind.CODE_ERROR,
                retryable=False,
                repair_guidance=(
                    "The exact edit_file old text was not found. Do not reread a whole large file or retry the "
                    "same snippet. Use grep_search for a unique symbol, method name, or nearby error text, then "
                    "read the narrow file section and retry edit_file with enough unique surrounding context."
                ),
            )
        if _matches(lowered, self.code_patterns):
            return FailureClassification(
                FailureKind.CODE_ERROR,
                retryable=False,
                repair_guidance="This looks like a code-level error. Inspect and modify the relevant code before rerunning.",
            )
        return FailureClassification(
            FailureKind.UNKNOWN_ERROR,
            retryable=False,
            repair_guidance="The tool failed. Inspect the error before deciding whether another tool call is useful.",
        )

    def classify_exception(self, exception: BaseException, tool_name: str = "") -> FailureClassification:
        if isinstance(exception, PermissionDenied):
            return FailureClassification(
                FailureKind.PERMISSION_DENIED,
                retryable=False,
                repair_guidance="The runtime denied this operation. Choose an allowed tool or explain the policy limit.",
            )
        if isinstance(exception, WorkspaceViolation):
            return FailureClassification(
                FailureKind.PERMISSION_DENIED,
                retryable=False,
                repair_guidance="The operation tried to access a path outside the workspace. Stay inside the workspace.",
            )
        if isinstance(exception, SandboxUnavailable):
            return FailureClassification(
                FailureKind.SANDBOX_UNAVAILABLE,
                retryable=False,
                repair_guidance=(
                    "Sandbox execution is enabled but the configured runtime is unavailable. "
                    "Do not fall back to host execution; provision the sandbox or explicitly choose host mode."
                ),
            )
        if isinstance(exception, subprocess.TimeoutExpired):
            return FailureClassification(
                FailureKind.TIMEOUT,
                retryable=False,
                repair_guidance="The command timed out. Repeating unchanged is unlikely to help; narrow the command or inspect incrementally.",
            )
        return self.classify(tool_name, f"{type(exception).__name__}: {exception}", is_error=True)


def _matches(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)
