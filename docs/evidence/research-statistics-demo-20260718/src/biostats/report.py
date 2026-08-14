"""Deterministic report assembly for a differential-expression experiment."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from .stats import benjamini_hochberg, summarize_replicates


def build_report(
    replicates: Iterable[Mapping[str, Any]],
    hypotheses: Iterable[Mapping[str, Any]],
) -> str:
    """Build a stable JSON report joining replicate summaries and hypotheses.

    Hypothesis rows contain ``feature`` and ``p_value``. The output includes
    BH-adjusted values and a 0.05 discovery flag, while preserving the input
    feature order only through explicit sorting.
    """
    summaries = summarize_replicates(replicates)
    hypothesis_rows = list(hypotheses)
    p_values = [row.get("p_value") for row in hypothesis_rows]
    adjusted = benjamini_hochberg(p_values)
    tests = []
    for row, q_value in zip(hypothesis_rows, adjusted):
        if not isinstance(row, Mapping) or "feature" not in row:
            continue
        tests.append(
            {
                "feature": str(row["feature"]),
                "p_value": row.get("p_value"),
                "q_value": q_value,
                "discovery": q_value is not None and q_value <= 0.05,
            }
        )
    payload = {"summaries": summaries, "tests": sorted(tests, key=lambda item: item["feature"])}
    # BUG: default separators and key ordering make byte-for-byte provenance unstable.
    return json.dumps(payload, allow_nan=False, sort_keys=True, separators=(",", ":"))

