from __future__ import annotations

from insightagent.evals.capability import default_cases, run_suite


def test_capability_matrix_is_fixed_and_network_free() -> None:
    cases = default_cases()

    assert len(cases) == 9
    assert {case.expected_outcome for case in cases} == {"allowed", "denied", "failed"}
    assert all("http" not in str(case.arguments).lower() for case in cases)


def test_capability_suite_produces_reproducible_pass_fail_evidence() -> None:
    report = run_suite()

    assert report["schema_version"] == 1
    assert report["total"] == 9
    assert report["passed"] == 9
    assert report["failed"] == 0
    assert report["security_denial_rate"] == 1.0
    assert report["redaction_passed"] is True
    assert report["reliability_passed"] is True
    assert report["reliability"]["total"] == 2
    assert report["reliability"]["passed"] == 2
    assert all(result["passed"] for result in report["results"])
