# ralphd selfdev roadmap 4 — resume env wiring, budget/review fairness, status UX

## Goal

Close the defects and polish items discovered while live-operating the
`--network host` feature and the `tg-selftest` run (2026-08-09/10). The
workspace is the ralphd repo itself at HEAD `a997d54` (`--network` feature,
all 325 tests green including the new `test_cli_network.py`).

## Context

You are ralphd developing ralphd. Same rules as previous waves: the run dir
is the source of truth; standing git policy applies (commit per task as
`task NNN: <title>`, push to origin/main, commit identity
`Nik <nikolaiorl@gmail.com>` via the repo-local git config — do not
override it; credentials from /config/creds/github.env; never print or
persist secret values; never `docker inspect`/`ps`/`logs` any container you
did not create yourself). The engine has mechanical secret redaction —
treat it as a safety net, not permission.

Live incident narrative behind requirements A–C (from the operator's
control-plane session): a job started with
`--forward-env 'AWS_*' --forward-env 'ANTHROPIC_*'` (Bedrock bearer-token
LLM auth) exhausted its 8-iteration budget with **all 7 tasks completed**
but no review slot → terminal `failed/unverified` despite the work being
done. The operator then ran `ralphctl resume` — and the fresh container
had **no LLM credentials at all**: iterations 9–11 died instantly with
"No API key found for amazon-bedrock". The no-progress fail-fast guard
correctly aborted with an excellent `reason` string… which `ralphctl
status` never displays. The operator recovered by hand-editing
`llm-wiring.json` to add the missing env vars; the second resume went
straight to review and VERIFIED.

## Requirements

### A. `resume` must reproduce the job's full env wiring (defect, highest priority)

`_write_llm_wiring` persists only the resolved `HOST_LLM_ENV` allowlist
(and profile env). Values injected via `--forward-env`, `--llm-env`, and
`--env` at start time are **lost on resume**, so any job whose LLM auth
arrives that way (e.g. `AWS_BEARER_TOKEN_BEDROCK` through
`--forward-env 'AWS_*'`) resumes into a credential-less container.

- Persist the *resolved* name=value pairs from `--forward-env`,
  `--llm-env`, and `--env` at start time alongside (or inside) the
  existing `llm-wiring.json` mechanism, and replay them on `resume` —
  same at-rest pattern as today (config dir, 0600, never under the run
  dir, never served over HTTP).
- Precedence on replay must match start-time behavior; a `resume`-time
  explicit override (if any flag exists for it) wins over the recorded
  value. Do not re-read the resuming shell's environment implicitly —
  byte-for-byte reproduction is the point (same principle as task 058 /
  steering 018).
- Migration: a run started before this feature (no recorded env wiring)
  must resume exactly as it does today — no error.
- Test bar: extend the stub-docker recording tests — start with
  `--forward-env`/`--env`/`--llm-env` values, wipe/change the shell env,
  resume, assert the recorded `docker run` argv carries the original
  values. Plus the migration case (old-style wiring file / no file).

### B. Budget exhaustion must not strand a finished job unreviewed (defect)

Observed: the worker completed the last task in the final budgeted
iteration; budget hit zero before any review could run; the job went
terminal `failed/unverified` with 7/7 tasks completed. The operator had to
resume with `+3` iterations just to get a review slot.

Invariant: **a job whose tasks are all completed should get a review
verdict if at all possible.**

- When the iteration budget is exhausted (or would be exhausted by the
  next worker iteration) and ALL tasks are in a completed state, run the
  review phase anyway — either by reserving the final budget slot for
  review once every task is completed, or by granting a single off-budget
  review iteration in exactly this case (design freedom; state the choice
  and its rationale in the task notes and docs/architecture.md).
- This must not create a loop: at most one such review per approach, and
  a review that comes back unsatisfied with zero budget left still ends
  the approach/job exactly as today.
- The terminal `reason` for a budget-exhausted job should say whether the
  grace review ran and what it concluded.
- Test bar: e2e with a stub agent — budget sized so the last task
  completes in the final iteration; assert the run still gets a review
  verdict (VERIFIED with a satisfied stub review), plus the negative case
  (tasks NOT all complete at exhaustion → no grace review, fails as
  today).

### C. Surface the engine's `reason` in `ralphctl status` and the hub (defect)

The engine writes a high-quality `reason` into `status.json` (e.g. the
no-progress fail-fast explanation) but `ralphctl status` and the hub
run-detail page never show it. An operator staring at
`state: aborted, verdict: unverified` has to cat the JSON to learn why.

- `ralphctl status`: print `reason` when present (wrap long text
  readably).
- Hub run detail: show it prominently for terminal failed/aborted states.
- While in there, make the `tasks:` and `usage:` lines of `ralphctl
  status` human-readable summaries instead of raw JSON dumps (e.g.
  `tasks: 7/7 completed` with per-status counts; `usage: $0.56, 625k
  tokens (planning $0.05 / worker $0.45 / review $0.06)`). Keep the full
  detail available under `--json`.
- Test bar: unit tests on the status rendering (reason present/absent,
  tasks/usage summarization), hub browser test asserting the reason is
  visible on a terminal run.

### D. `--network` follow-through (polish)

The `--network` flag (commit `a997d54`) works and is live-validated. Round
it out:

- Registry default: `ralphctl config set network <value>` as a fallback
  for `start` (same precedence pattern as `image`/`on_complete`), so an
  operator whose jobs always need tailnet access doesn't repeat the flag.
- `ralphctl doctor`: when the configured/requested network is `host`,
  note that the API binds `--api-bind` directly (no docker port-publish
  isolation).
- docs/architecture.md: short section on host-network jobs (when to use,
  the RALPHD_PORT/RALPHD_BIND mechanism, security posture: API bind
  address is the only boundary).
- Test bar: config-fallback test mirroring the existing `image` fallback
  test; doctor output test.

### E. `ralphctl repair` (pulled forward from the deferred list)

Two waves in a row the operator had to hand-edit files to recover a run
(`status.json` in roadmap-2, `llm-wiring.json` this wave). Promote the
deferred `ralphctl repair` roadmap item into a minimal, sanctioned
implementation:

- `ralphctl repair <run-id>` — interactive-free diagnosis + fix of known
  inconsistent states. At minimum: (1) validate `status.json`,
  `tasks.json`, `host.json` against their schemas and report what's
  wrong; (2) `--set-state <state>` guarded escape hatch for a run whose
  container died without writing a terminal state; (3) `--env KEY=VAL`
  to add/update a recorded env value in the persisted wiring (the exact
  hand-edit the operator performed this wave, done safely with 0600
  preserved and the value never echoed).
- Every repair action appends an audit line to `events.jsonl`
  (`type: repair`, what changed, no secret values).
- Refuse to touch a run whose container is currently running.
- Docs: docs/cli.md section; remove the item from roadmap.md's deferred
  list.
- Test bar: unit/e2e per action, including the refuse-while-running case
  and the audit-event assertion.

### F. Roadmap note only — do NOT implement

Add to docs/roadmap.md deferred list: first-class named env-wiring
profiles (reusable `--forward-env`/creds bundles referenced by name at
start, like LLM profiles) — rationale: operators repeat the same 6-flag
incantation per run; requirement A records it per-run, a profile would
name it once.

## Non-goals

- No refactors beyond what the requirements need.
- PID-namespace isolation stays deferred (roadmap note exists).
- Docker image publish / pipx packaging remain out of scope.
- Do not modify completed prior-wave work except where a requirement
  touches it.

## Quality bar (unchanged)

- Every requirement gets a traceability row with real test node IDs.
- Full suite green including docker and browser tiers actually executed.
- `ruff check .` clean.
- Docs updated as part of each task's definition of done.
- Commit+push per task per standing git policy.
