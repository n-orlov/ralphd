# Feature: Vigilant mode (per-task verification)

## Goal

Implement vigilant mode in the ralphd engine: after each worker iteration, newly
completed tasks are independently verified by a separate verification iteration
before the loop proceeds. This is already fully specified in the project's design
docs — read `docs/architecture.md` (sections "The loop" and "Vigilant mode") and
`docs/api.md` in the workspace before writing any code.

## Context

The workspace is the ralphd repository itself. v0.1 config plumbing for vigilant
mode already exists and must be used, not duplicated:

- `JobConfig.vigilant` flag (`src/ralphd/engine/config.py`)
- model strategy already resolves a `verify` phase (`JobConfig.model_for("verify")`)
- the loop supervisor lives in `src/ralphd/engine/loop.py`
- black-box e2e test harness: `tests/test_e2e.py` + stub agent `tests/stub-pi/pi`

## Requirements

1. **Verification prompt**: add `src/ralphd/prompts/task-verify.md`, consistent in
   style with the existing planning/worker/review prompts. The verifier receives
   the normal job context plus (via the prompt built by the engine) the specific
   task under verification (id, title, successCriteria). It must independently
   check that task's successCriteria and then EITHER:
   - emit the exact sentinel `<task-verified>ID</task-verified>` (ID = task id)
     as its final line when the criteria genuinely hold, OR
   - set the task's status to `validation-failed` in tasks.json and add a
     `validationNotes` field explaining concretely what is wrong, and NOT emit
     the sentinel.

2. **Loop wiring** (only when `vigilant: true` in job config):
   - the engine snapshots task statuses before and after each worker iteration
   - for each task that newly transitioned to `completed`, run one verification
     iteration (phase name `verify`, model via `model_for("verify")`)
   - if the sentinel `<task-verified>ID</task-verified>` for the right task id is
     seen, the task stays `completed`
   - if not seen, the engine ensures the task ends up as `validation-failed`
     (the verifier normally sets this itself; the engine must enforce it if the
     verifier failed to) and increments `validationAttempts` on that task
   - a task whose `validationAttempts` reaches 3 is set to `failed` by the engine
     and not verified again
   - verification iterations draw from the same shared iteration budget
   - non-vigilant behavior must be completely unchanged

3. **Worker prompt**: the existing worker prompt already tells workers to pick up
   `validation-failed` tasks first and read `validationNotes` — verify this still
   matches the implemented semantics and adjust the wording only if inaccurate.

4. **Signals/observability**:
   - iteration `meta.json` for verify iterations records the verified task id and
     the outcome
   - events: emit `signal` event with `taskVerified` + task id on success; the
     existing `task` change events already cover the failure path
   - `/status` continues to work; verify iterations appear in `/iterations`

5. **Stub agent support**: extend `tests/stub-pi/pi` with a verifier branch
   (recognizing the task-verify prompt) and env knobs, following the existing
   `STUB_*` convention, to control how many times verification fails for a task.

6. **Black-box e2e tests**: add tests to `tests/test_e2e.py` in the existing
   style (real `ralphd-engine` process + stub pi, observation via HTTP API and
   run-dir files only, no engine imports):
   - vigilant happy path: every completed task gets exactly one verify iteration;
     phases sequence planning, worker, verify, ..., review; job succeeds
   - verification failure path: a task fails verification once (status
     `validation-failed`, `validationAttempts` 1, worker retries it, second
     verification passes, job succeeds)
   - 3-strikes path: a task that keeps failing verification ends `failed` after
     exactly 3 attempts and the job does not succeed
   - non-vigilant regression: with `vigilant` unset, no verify iterations occur

7. **Quality gates** (all must pass, run them yourself before claiming done):
   - install the workspace package first: `pip install --user -e '.[dev]'`
     (the container has a system ralphd installed — tests must exercise the
     workspace code via the editable install; check `which ralphd-engine`
     resolves to the editable one, `~/.local/bin` comes first on PATH)
   - `python -m pytest tests/ -q` → ALL tests pass (existing 8 + new ones)
   - `python -m ruff check src/ tests/` → clean
   - update `docs/architecture.md` ONLY if implemented behavior deviates from
     what it already says (it should not)

## Non-goals

- Do NOT implement the runtime config API, resume, or anything else from the
  roadmap.
- Do NOT modify `ralphctl` beyond what already exists (`--vigilant` flag exists).
- Do NOT commit or push; leave changes in the working tree.
- Do NOT touch `.git` config, remotes, or history.

## Notes

- Work only inside the workspace. Docker is not available inside your
  environment — the pytest suite is the verification harness, it runs the
  engine as a plain process.
- Keep the code style of the existing modules (comment density, naming,
  line length 100).
