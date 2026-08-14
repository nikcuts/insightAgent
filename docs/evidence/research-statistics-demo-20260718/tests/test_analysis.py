from __future__ import annotations

import json
import math

import pytest

from biostats.report import build_report
from biostats.stats import benjamini_hochberg, bootstrap_mean_ci, summarize_replicates


def test_bh_is_monotone_and_preserves_input_order_and_missing_values():
    adjusted = benjamini_hochberg([0.04, None, 0.001, 0.02, 0.9])
    assert adjusted[1] is None
    assert adjusted[2] == pytest.approx(0.004)
    assert adjusted[0] == pytest.approx(0.0533333333)
    assert adjusted[3] == pytest.approx(0.04)
    assert adjusted[4] == pytest.approx(0.9)
    pairs = [(p, q) for p, q in zip([0.04, None, 0.001, 0.02, 0.9], adjusted) if p is not None]
    assert [q for _, q in sorted(pairs)] == sorted(q for _, q in pairs)
    with pytest.raises(ValueError):
        benjamini_hochberg([1.2])


def test_bootstrap_is_seeded_local_and_filters_nonfinite_values():
    values = [1.0, 2.0, 3.0, float("nan"), float("inf")]
    first = bootstrap_mean_ci(values, n_resamples=400, seed=17)
    second = bootstrap_mean_ci(values, n_resamples=400, seed=17)
    assert first == second
    assert first[0] == pytest.approx(2.0)
    assert first[1] <= first[0] <= first[2]
    assert bootstrap_mean_ci([2.0, 2.0], n_resamples=20, seed=4) == (2.0, 2.0, 2.0)


def test_replicate_summary_uses_positive_weights_and_standard_error():
    rows = [
        {"feature": "il6", "condition": "treated", "value": 10, "weight": 2},
        {"feature": "il6", "condition": "treated", "value": 14, "weight": 1},
        {"feature": "il6", "condition": "treated", "value": None},
        {"feature": "il6", "condition": "control", "value": 4},
        {"feature": "bad", "condition": "control", "value": "nan"},
    ]
    summary = summarize_replicates(rows)
    treated = next(item for item in summary if item["condition"] == "treated")
    assert treated["n"] == 2
    assert treated["mean"] == pytest.approx(11.3333333333)
    expected_sem = math.sqrt(((10 - 11.3333333333) ** 2 * 2 + (14 - 11.3333333333) ** 2) / (3 - 1)) / math.sqrt(2)
    assert treated["sem"] == pytest.approx(expected_sem)


def test_report_is_deterministic_and_joins_adjusted_hypotheses():
    replicates = [
        {"feature": "b", "condition": "control", "value": 2},
        {"feature": "a", "condition": "control", "value": 1},
    ]
    hypotheses = [
        {"feature": "b", "p_value": 0.02},
        {"feature": "a", "p_value": 0.001},
        {"feature": "missing", "p_value": None},
    ]
    report = build_report(replicates, hypotheses)
    assert report == build_report(replicates, hypotheses)
    payload = json.loads(report)
    assert [row["feature"] for row in payload["tests"]] == ["a", "b", "missing"]
    assert payload["tests"][0]["q_value"] == pytest.approx(0.002)
    assert payload["tests"][0]["discovery"] is True
    assert payload["tests"][2]["q_value"] is None
    assert "\n" not in report
