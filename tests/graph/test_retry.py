from __future__ import annotations

from insightagent.graph.retry import (
    BACKOFF_FAILURE_KINDS,
    PERMANENT_FAILURE_KINDS,
    RetryPolicy,
)
from insightagent.runtime.failure_classifier import FailureKind


def test_graph_retry_policy_owns_tool_retry_categories() -> None:
    policy = RetryPolicy(base_delay=0.5, max_delay=1.0, max_attempts=3, sleep=lambda _delay: None)

    assert policy.backoff_delay(1) == 0.5
    assert policy.backoff_delay(3) == 1.0
    assert FailureKind.TIMEOUT in BACKOFF_FAILURE_KINDS
    assert FailureKind.PERMISSION_DENIED in PERMANENT_FAILURE_KINDS
