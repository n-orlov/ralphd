# Phase 1 gate (task 044) — full suite + ruff

Commit under test: `6338f6e` (task 060: fix flaky --no-detach docker e2e assertion)
Host: ralphd job container, Python 3.12.13, ruff 0.16.2, docker socket present,
`playwright-cli` on PATH.

## Commands and results

```
$ .venv/bin/python -m ruff check .
All checks passed!

$ .venv/bin/python -m pytest -q
656 passed in 786.82s (0:13:06)
```

Summary counts: **656 passed, 0 failed, 0 skipped, 0 errors, 0 xfail** (13:06 wall clock).

Both optional tiers really executed on this host (no clean-skip needed):

```
$ .venv/bin/python -m pytest -q --collect-only -m docker
8/656 tests collected (648 deselected)

$ .venv/bin/python -m pytest -q --collect-only -m browser
13/656 tests collected (643 deselected)
```

i.e. the 656 passing tests include the 8 `docker`-marked container-level e2e tests
(docker socket available — siblings on the host daemon) and the 13 `browser`-marked
hub e2e tests (playwright-cli available). Raw log: `/tmp/full-suite.log` on the
run host; counts reproduced above verbatim.

Baseline for comparison: the plan recorded 393 tests / 69 modules at plan HEAD
(`5472453`). Tasks 001-043 + 060 added 27 new test modules; the suite is now 656 tests.

## Pre-existing tests modified, and the contract change that justifies each

Modified (as opposed to newly added) test files between `5472453..HEAD`:

| File | Change | Justifying contract change |
|---|---|---|
| `tests/test_fault_classifier.py` | table-driven rewrite/extension | 001-003: `classify_fault()` now faults on a non-empty `error_message` at exit 0; signature table extended; new `operator_abort` input |
| `tests/test_job_config_defaults.py`, `tests/test_config_effective.py` | new knobs asserted | 006: `infra_retry_backoff_max_s`, `infra_outage_budget_s` added to `JobConfig`/env/`effective()`; `infra_retry_max` is now "honoured only when set explicitly" |
| `tests/stub-pi/pi` | new documented knob | 005: `STUB_INBAND_ERROR_*` emits a `message_end` with `stopReason: error` + infra-shaped `errorMessage` while exiting 0 with zero tokens (test-harness surface, documented in the stub) |
| `tests/test_no_progress_instant_failures.py` | `iterationsUsed` expectation 0/1 + seam swap | 010: instant infra faults are now retried (and refunded) before the broken-environment fast-fail triggers, so a broken-credential run records the refunded count; the test drives the retry wrapper seam instead of the old inner call |
| `tests/test_cli_repair.py` | new dangling-container cases | 021: repair's `checked` list gained `"container"`; `--set-state aborted` now writes a reason + audit event |
| `tests/test_cli_docker.py` | argv assertions | 034: job container now carries `--label ralphd.role=job` and `RALPHD_SELF_CONTAINER_ID`; reaping still filters on the run id only |
| `tests/test_cli_resume.py` | argv/wiring assertions | 026/032: `auto_resume` survives resume via the recorded start-time wiring; resume appends a `running` state event |
| `tests/test_e2e.py` | events-replay loop now breaks on a *terminal* state event | 032: a `running` state event is appended on resume, so "last state event" is no longer necessarily terminal — consumers must reconcile against terminality, which is exactly the #13 fix |
| `tests/test_reflection.py` | failure branch added | 019: reflect outcome recorded in `status.json` + `artifacts/reflection/FAILED.md` |
| `tests/test_secret_redaction.py` | on-disk snapshot assertion added | 042: documented decision = write-time-only scrubbing, redaction map never persisted; the on-disk reader must therefore see no unscrubbed secret |
| `tests/test_docs_consistency.py` | new guards (+112 lines) | 025/030/036/043: single-story dead-run remedy, roadmap deferred note, sibling-only cleanup rule in all 4 doc copies, resilience section §10 |
| `tests/test_browser_hub.py`, `tests/test_cli_ui.py` | new degraded / dead-run / reflect-failed / snapshot cases | 012-014, 017, 020, 024, 039: `health`/`infraWait` status contract, retry-now button, warning treatment for a dead run, on-disk log snapshot label |
| `tests/test_docker_sibling_e2e.py` | `_first_json_object()` (raw_decode) instead of `json.loads(stdout)` | 060: `--no-detach --json` stdout is the meta object *followed by* streamed events; the old assertion only passed when the CLI lost the race to `/events`. Not a product contract change — a test-side bug fix |

No pre-existing test was deleted, weakened to pass, or skipped.

## Gate verdict

PASS — phase 1 (tasks 001-043 + 060) is green on all tiers with ruff clean.
Phase 2 (tasks 045+) may proceed.
