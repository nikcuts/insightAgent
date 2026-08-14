"""Statistics used by a small differential-expression research pipeline.

The module intentionally has no NumPy dependency: the runtime is also used in
lightweight reproducibility checks where only the Python standard library is
available.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def benjamini_hochberg(p_values: Iterable[float | None]) -> list[float | None]:
    """Return BH-adjusted p-values in the same order as the input.

    Missing values are kept as ``None``. Valid values must be finite and lie in
    ``[0, 1]``. The adjusted values must be monotone in sorted p-value order.
    """
    values = list(p_values)
    indexed: list[tuple[int, float]] = []
    adjusted: list[float | None] = [None] * len(values)
    for index, value in enumerate(values):
        if value is None:
            continue
        value = float(value)
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("p-values must be finite numbers in [0, 1]")
        indexed.append((index, value))
    ordered = sorted(indexed, key=lambda item: (item[1], item[0]))
    count = len(ordered)
    q_values = [
        min(1.0, value * count / rank)
        for rank, (_, value) in enumerate(ordered, 1)
    ]
    for rank in range(count - 2, -1, -1):
        q_values[rank] = min(q_values[rank], q_values[rank + 1])
    for (index, _), q_value in zip(ordered, q_values):
        adjusted[index] = q_value
    return adjusted


def bootstrap_mean_ci(
    values: Sequence[float],
    *,
    n_resamples: int = 2000,
    confidence: float = 0.95,
    seed: int | None = 0,
) -> tuple[float, float, float]:
    """Estimate a percentile bootstrap CI for the arithmetic mean."""
    if not values:
        raise ValueError("at least one observation is required")
    if n_resamples < 1:
        raise ValueError("n_resamples must be positive")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between 0 and 1")
    clean = [float(value) for value in values if math.isfinite(float(value))]
    if not clean:
        raise ValueError("at least one finite observation is required")
    rng = random.Random(seed)
    means = []
    for _ in range(n_resamples):
        sample = [rng.choice(clean) for _ in clean]
        means.append(sum(sample) / len(sample))
    means.sort()
    alpha = (1 - confidence) / 2
    lower = means[round(alpha * (len(means) - 1))]
    upper = means[round((1 - alpha) * (len(means) - 1))]
    return sum(clean) / len(clean), lower, upper


def summarize_replicates(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate replicate rows by ``(feature, condition)``.

    Each row contains ``feature``, ``condition``, ``value`` and optional
    ``weight``. Invalid or missing values are ignored; a group with no valid
    observations is omitted. The result is sorted for stable reports.
    """
    groups: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for row in rows:
        try:
            feature = str(row["feature"])
            condition = str(row["condition"])
            value = float(row["value"])
            weight = float(row.get("weight", 1.0))
        except (KeyError, TypeError, ValueError):
            continue
        if not feature or not condition or not math.isfinite(value) or not math.isfinite(weight):
            continue
        if weight <= 0:
            continue
        groups.setdefault((feature, condition), []).append((value, weight))

    result: list[dict[str, Any]] = []
    for (feature, condition), observations in groups.items():
        values = [value for value, _ in observations]
        weights = [weight for _, weight in observations]
        sum_weight = sum(weights)
        mean = sum(value * weight for value, weight in observations) / sum_weight
        sum_weight_sq = sum(weight * weight for weight in weights)
        denominator = sum_weight - sum_weight_sq / sum_weight
        if denominator <= 0:
            sem = 0.0
        else:
            variance = sum(
                weight * (value - mean) ** 2
                for value, weight in observations
            ) / denominator
            sem = math.sqrt(variance / sum_weight)
        result.append(
            {
                "feature": feature,
                "condition": condition,
                "n": len(values),
                "mean": mean,
                "sem": sem,
            }
        )
    return sorted(result, key=lambda item: (item["feature"], item["condition"]))
