"""图工具运行时的有限重试策略。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from insightagent.runtime.failure_classifier import FailureKind


@dataclass
class RetryPolicy:
    """为可恢复的工具失败提供有界指数退避。"""

    base_delay: float = 0.5
    max_delay: float = 8.0
    max_attempts: int = 2
    sleep: Callable[[float], None] = time.sleep

    def backoff_delay(self, attempt: int) -> float:
        return min(self.max_delay, self.base_delay * (2 ** max(0, attempt - 1)))

    def wait(self, attempt: int) -> float:
        delay = self.backoff_delay(attempt)
        if delay > 0:
            self.sleep(delay)
        return delay


BACKOFF_FAILURE_KINDS = frozenset({FailureKind.NETWORK_ERROR, FailureKind.TIMEOUT})
PERMANENT_FAILURE_KINDS = frozenset(
    {
        FailureKind.PERMISSION_DENIED,
        FailureKind.TOOL_PROTOCOL_ERROR,
        FailureKind.ENVIRONMENT_ERROR,
        FailureKind.SANDBOX_UNAVAILABLE,
    }
)


__all__ = ["BACKOFF_FAILURE_KINDS", "PERMANENT_FAILURE_KINDS", "RetryPolicy"]
