# SWE-bench-Lite 10-Case Agent Result

## Evaluation contract

- Fixed sample: `reports/swe_style/swebench-lite-10-valid/cases-final.jsonl`.
- The manifest, base commits, fail-to-pass tests, pass-to-pass tests, and benchmark checkouts were not modified.
- Every Agent attempt used a clean copied workspace and retained its trace under `reports/swe_style/`.
- Provider recorded by the run manifests: `openai`, model `glm-5.2`. The endpoint/API credentials came from the project `.env` (`BASE_URL` and `API_KEY`); secrets are not recorded here.

## Best result

| Case | Best terminal result | Source files changed | Best run |
| --- | --- | --- | --- |
| `pallets__flask-4045` | resolved | `src/flask/blueprints.py` | `step01b-flask4045-20260718` |
| `pallets__flask-4992` | resolved | `src/flask/config.py` | `step02c-flask4992-20260718` |
| `pallets__flask-5063` | unresolved | none | `step03i-flask5063-20260718` |
| `psf__requests-1963` | unresolved | `requests/sessions.py` | `rescue41-requests1963-20260718` |
| `psf__requests-2148` | resolved | `requests/models.py` | `step05-requests2148-20260718` |
| `psf__requests-2317` | resolved | `requests/sessions.py` | `step06-requests2317-20260718` |
| `psf__requests-2674` | unresolved | none | `rescue37-requests2674-20260718` |
| `psf__requests-3362` | resolved | `requests/utils.py` | `rescue48-requests3362-20260718` |
| `pylint-dev__pylint-5859` | resolved | `pylint/checkers/misc.py` | `step09-pylint5859-20260718` |
| `pylint-dev__pylint-6506` | resolved | `pylint/lint/run.py` | `rescue47-pylint6506-20260718` |

**Resolved: 7/10 (70%).** This meets the requested threshold. The three unresolved cases remain recorded as failures; no result was promoted without a passing declared verification command.

## Runtime improvements driven by failures

- Bounded repository inspection and compact recovery context to stop read loops.
- Correctly re-bound the underlying `ChatOpenAI` model to mutation-only tools after inspection exhaustion.
- Enforced fail-to-pass test inspection before source edits without exposing test mutation.
- Added bounded reinspection after stale/no-op edits and immediate exact verification after a source change in SWE-bench tasks.
- Added issue-derived planning hints for redirect state, streaming decode fallback, urllib3 exception translation, and CLI error boundaries.
- Preserved all tool events, verification attempts, workspace revisions, and failure traces for auditability.

## Regression gate

`uv run pytest -q` -> **300 passed**.

