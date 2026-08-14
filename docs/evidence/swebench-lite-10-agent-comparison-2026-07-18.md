# SWE-bench-Lite 10-Case Comparison

## Fixed sample

The same ten cases were used for baseline and both Agent runs:

- Flask: `pallets__flask-4045`, `pallets__flask-4992`, `pallets__flask-5063`
- Requests: `psf__requests-1963`, `psf__requests-2148`, `psf__requests-2317`, `psf__requests-2674`, `psf__requests-3362`
- Pylint: `pylint-dev__pylint-5859`, `pylint-dev__pylint-6506`

The case manifest is [`cases-final.jsonl`](../../reports/swe_style/swebench-lite-10-valid/cases-final.jsonl). Each checkout was cloned at the SWE-bench base commit with the test patch applied. The baseline command was run with repository-specific isolated environments and `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` where needed.

## Baseline gate

All 10 cases passed the evaluator gate: baseline exit code was `1` for every case, with no `invalid_baseline` or `invalid_environment` cases. Earlier Astropy/Django selection was rejected because its generated commands and dependencies were not valid on this host; those cases were excluded rather than counted as Agent failures.

## Agent runs

| Run | Runtime state | Cases | Resolved | Agent terminal failures | Total model tokens | Tool reads | Source edits | Inspection-budget failures |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `swe-lite-10-agent-20260718` | before latest runtime fixes | 10 | 0 | 10 | 638,774 | 131 | 2 | 17 |
| `swe-lite-10-agent-r2-20260718` | after snapshot/inspection/repair fixes | 10 | 0 | 10 | 596,829 | 137 | 0 | 3 |

The first run took about 740 seconds of summed per-case graph time; the second took about 410 seconds. These are model token estimates recorded by the runtime, not provider billing statements. Full raw traces and reports are in [`swe-lite-10-agent-20260718`](../../reports/swe_style/swe-lite-10-agent-20260718) and [`swe-lite-10-agent-r2-20260718`](../../reports/swe_style/swe-lite-10-agent-r2-20260718).

## Runtime changes driven by traces

- Prioritized implementation files in the repository snapshot so bounded context exposes `src/` and package source before tests.
- Changed inspection-budget feedback into recoverable guidance rather than consuming repair attempts.
- Allowed explicit source/test range reads after broad inspection is exhausted while still rejecting broad scans.
- Added deterministic execution of the declared exact verification command when a source revision is left unverified at the iteration limit.
- Allowed bounded repair turns after a failed last-iteration verification and increased the default repair budget from 3 to 5.
- Added regression tests for last-iteration verification, bounded repair, snapshot ordering, and inspection-budget behavior.

## Honest conclusion

The current Agent is not production-ready as an autonomous SWE-bench repair system. The evaluator is now reproducible and the runtime has stronger safety/convergence controls, but this fixed 10-case run achieved `0/10` resolutions. The dominant remaining failure is model planning/convergence: it spends the available turns reading test context and often never edits or verifies. Increasing the iteration budget, improving task-specific planning, and adding a stronger patch-quality/verification loop are still required before claiming production capability.
