# ralphd Container API

Each job container serves this HTTP API (default `:7777`, published to
`127.0.0.1:<host-port>` by `ralphctl`). Content type is JSON unless noted. When an
API token is configured, every route requires `Authorization: Bearer <token>` and
replies `401` otherwise.

Errors use RFC 7807 problem+json: `{"type", "title", "status", "detail"}`.

## Conventions

- All state-reading endpoints work in every lifecycle state, including `idle` after
  completion — that is the point of idle mode.
- Mutating endpoints that no longer make sense (e.g. steering a `succeeded` job)
  return `409` with an explanatory detail.
- Timestamps are RFC 3339 UTC.

## Status & observation

### `GET /healthz`
Liveness. `200 {"ok": true}` — no auth required (the only unauthenticated route).

### `GET /status`
The one-call summary. Response:

```json
{
  "runId": "brisk-otter-1408",
  "state": "running",              // starting|running|succeeded|failed|aborted
  "phase": "worker",               // planning|worker|verify|review|null
  "approach": 1,
  "maxApproaches": 3,
  "iteration": 7,
  "iterationsBudget": 50,
  "iterationsUsed": 7,
  "verdict": null,                 // "verified" | "unverified" | null while running
  "onComplete": "idle",
  "startedAt": "2026-08-08T13:08:11Z",
  "deadlineAt": "2026-08-08T21:08:11Z",
  "infraWaitTotalS": 62.5,
  "health": "ok",                  // "ok" | "degraded"
  "infraWait": null,               // populated only while waiting out an infra outage
  "reflect": null,                 // absent/null until the reflect phase ends, see below
  "currentIteration": {
    "number": 7, "phase": "worker",
    "model": "anthropic/claude-opus-5",
    "startedAt": "2026-08-08T14:02:33Z"
  },
  "tasks": {"total": 9, "completed": 4, "inProgress": 1,
            "pending": 3, "validationFailed": 1, "failed": 0},
  "steering": {"pending": 0, "consumed": 2},
  "usage": {
    "input": 812345, "output": 90123, "totalTokens": 902468, "costUSD": 14.20,
    "byPhase": {
      "planning": {"input": 40000, "output": 4000, "totalTokens": 44000, "costUSD": 0.60},
      "worker": {"input": 700000, "output": 80000, "totalTokens": 780000, "costUSD": 12.00},
      "review": {"input": 72345, "output": 6123, "totalTokens": 78468, "costUSD": 1.60}
    },
    "byApproach": {
      "1": {"input": 812345, "output": 90123, "totalTokens": 902468, "costUSD": 14.20}
    }
  }
}
```

`usage.byPhase` breaks the same running totals down by iteration phase
(`planning`/`worker`/`verify`/`review`) and `usage.byApproach` by approach
number (as a string key, since JSON object keys are always strings) — both
are accumulated alongside the top-level totals on every iteration, so for
any token/cost field `sum(byPhase[*][field]) == sum(byApproach[*][field])
== usage[field]`. A phase/approach only appears once at least one iteration
for it has ended.

`deadlineAt` is `startedAt + jobTimeoutS` **plus every second this run has
spent waiting out an infra outage** (`infraWaitTotalS`, the cumulative infra
backoff wait — see the infra budgets under `GET /config`). Waiting for a
broken LLM endpoint is not work the job's timeout should pay for: each backoff
wait extends the deadline by exactly the waited time and emits a
`deadline_extended` event, so `jobTimeoutS` keeps its plain meaning (time
available to the agent) and a 4-hour gateway outage cannot silently consume
half an 8-hour job. `infraWaitTotalS` is `0` for a run that never hit an
outage and survives `ralphctl resume` (the per-process deadline does not).

#### `health` and `infraWait`

`state` stays `starting|running|succeeded|failed|aborted` — there is **no**
`degraded` state value, because that would break every consumer's
terminal-state logic (`ralphctl watch` included). A run that is sitting out an
infra outage says so with two dedicated fields instead:

| field | meaning |
|-------|---------|
| `health` | `"ok"` normally; `"degraded"` from the first infra-classified failure of an outage episode until an iteration reaches the model again (or the run ends on the outage) |
| `infraWait` | `null` whenever the run is not actually in a backoff wait; otherwise the object below |

```json
"health": "degraded",
"infraWait": {
  "since": "2026-08-18T09:14:02Z", "attempt": 4,
  "error": "getaddrinfo EAI_AGAIN aigw…", "phase": "worker",
  "nextAttemptAt": "2026-08-18T09:15:02Z",
  "waitedS": 52, "budgetS": 14400, "remainingS": 14348
}
```

`waitedS`/`budgetS`/`remainingS` are this episode's cumulative backoff wait,
the `infraOutageBudgetS` it is spent against, and what is left of it (see the
infra budgets under `GET /config`). `infraWait` goes back to `null` when the
wait ends and the next attempt starts, while `health` stays `"degraded"` until
an iteration actually reaches the model — a run between two backoffs has not
recovered yet. Both are also emitted as events (`infra_wait` /
`infra_recovered`), so the whole episode is visible in the event stream
`ralphctl watch` follows and not only to a client polling `/status` at the
right moment. Surfaced by `ralphctl status`'s `degraded:` line and the hub
run-detail card. A wait can be cut short with `POST /retry`.

#### `reflect`

Absent from `status.json` (and `null` in `GET /status`) unless the run was
started with `reflect: true` and the post-terminal reflect iteration has
finished. Then:

```json
"reflect": {"ok": false, "error": "Connection error.",
            "endedAt": "2026-08-18T09:31:20Z"}
```

`ok: true` means `artifacts/reflection/report.md` exists; otherwise `error`
carries the agent's error text (or `the reflect iteration wrote no
artifacts/reflection/report.md` when it exited cleanly having written
nothing) and `artifacts/reflection/FAILED.md` names the same error on disk.
A failed reflection **never** changes the run's `state`, `verdict` or
`reason` — the job is already over when reflect runs. Surfaced by
`ralphctl status` and the hub run-detail card, and emitted as a
`reflect_done` event.

### `GET /tasks`
Full `tasks.json`.

### `GET /prd`
`text/markdown` — the PRD in effect (composite PRD when approach ≥ 2).
`GET /prd?original=true` returns the original regardless.

### `GET /notes`
`text/markdown` — current `notes.md` handoff notes.

### `GET /iterations`
Array of every iteration's `meta.json` (number, phase, approach, model, timestamps,
exit code, sentinel seen, token usage, steering consumed).

Each finished iteration also carries `faultClass` — the engine's own fault verdict
for that iteration (`src/ralphd/engine/faults.py:classify_fault`), the same verdict
the infra-retry wrapper acts on:

| `faultClass` | meaning |
|--------------|---------|
| `null` | not a failure: clean exit, no error recorded, not interrupted/timed out |
| `"infra"` | the LLM endpoint/provider/network broke (no traffic within the startup window, or a recognized infra error signature) — the attempt is retried and refunded, never charged to the iteration budget |
| `"work"` | the agent really ran (LLM traffic observed) and then failed, or an operator-initiated abort/interrupt ended it — never retried as an outage |

The field is absent only while an iteration is still in flight (before its
`endedAt` is written).

### `GET /iterations/{n}`
One iteration's `meta.json`.

### `GET /iterations/{n}/output`
`application/x-ndjson` — the full agent transcript for iteration *n*. Supports
`?tail=<lines>`. For the in-flight iteration, `?follow=true` streams new lines until
the iteration ends (chunked).

### `GET /logs`
`application/x-ndjson` — the **whole-job log**: every iteration's transcript
merged in order, with a synthetic boundary line injected between iterations:

```json
{"type":"ralphd.iteration","number":7,"phase":"worker","event":"start",
 "model":"...","approach":1}
```

(and a matching `"event":"end"` line carrying exit code, sentinels, error, usage).
All other lines are pi transcript lines passed through as-is, except that any
known secret value (LLM env vars, placed creds file values) is mechanically
scrubbed and replaced with `[REDACTED:<source>]` as a defense-in-depth layer
on top of the same scrubbing already applied when `output.jsonl`/`events.jsonl`
were written (see `docs/architecture.md`'s "Security: mechanical secret
redaction" section).

Query params: `?tail=<lines>` bounds the initial backlog (counted over transcript
lines, boundaries not counted); `?follow=true` keeps streaming **across iteration
boundaries** until the job reaches a terminal state (then closes). `tail` and
`follow` combine (tail backlog, then live). This endpoint serves raw NDJSON only —
pretty rendering is `ralphctl logs`' job.

### `GET /events`
`text/event-stream` (SSE). Replays from `?since=<eventId>` (default: live only),
then follows. Event types:

| type | payload highlights |
|------|--------------------|
| `state` | lifecycle transition: `state` is `running` (emitted by every engine process as it starts the job loop, resume included) or the terminal `succeeded`/`failed`/`aborted`; the startup event also carries `resumed` (`true` when this process picked up a run dir that already held recorded work) |
| `phase` | phase entered (planning/worker/verify/review), approach number |
| `iteration.start` / `iteration.end` | number, phase, model / exit, sentinel, error, `faultClass` (`null` \| `"infra"` \| `"work"`, identical to the iteration's `meta.json` — see `GET /iterations`) |
| `task` | task id + old/new status — emitted live while a worker iteration is still running (polled every ~0.25s against `tasks.json`), not only after the iteration ends, so `pending -> in-progress` is observable in real time |
| `steering.received` / `steering.consumed` | steering file name |
| `signal` | COMPLETE / VERIFIED / task-verified detected |
| `log` | engine-level notices (timeouts, retries, failures) |
| `infra_retry` | an infra-classified attempt is being retried: phase, attempt, error, `backoffS` (`null` when giving up), `waitedS`, `budgetS` |
| `infra_wait` | a backoff wait started: the full `infraWait` payload (`since`, `attempt`, `error`, `phase`, `nextAttemptAt`, `waitedS`, `budgetS`, `remainingS`) plus `backoffS`; the run is now `health: "degraded"` |
| `infra_recovered` | an iteration reached the model again: `health: "ok"`, `infraWaitTotalS` — the outage episode is over and `infraWait` is back to `null` |
| `infra_retry_now` | an operator woke a backoff wait via `POST /retry`: phase, attempt, error, `source: "operator"` |
| `reflect_infra_delay` | the job ended on an infra-shaped failure, so the post-terminal `reflect` iteration waits before its first attempt instead of firing into the same dead endpoint: `delayS`, `error`, `budgetS` (reflect's own, capped, outage budget). Followed by an ordinary `infra_wait` with `attempt: 0` |
| `reflect_done` | the post-terminal `reflect` iteration finished: `ok`, plus `error` when it failed (same verdict as status.json's `reflect`) |
| `deadline_extended` | the job deadline moved out after an infra wait: phase, attempt, `waitedS`, `infraWaitTotalS`, new `deadlineAt`, `reason` |

Every event also lands in `events.jsonl` in the run dir with a monotonically
increasing `id`. That file is append-only **across resumes**, so a terminal
`state` event can sit in the middle of the log — the `running` state event a
resuming engine emits supersedes it, which is what lets a follower tell a
historical marker from the run's real terminus (see `ralphctl watch` in
[cli.md](cli.md)).

### `GET /artifacts` and `GET /artifacts/{path}`
List (recursive, with sizes) and download files from the artifacts dir.

## Steering & control

### `POST /steering`
Body: `{"message": "<markdown>", "name": "optional-slug"}`. Writes the next
`NNN-<slug>.md` into the steering inbox. Consumed at the next iteration start.
`202 {"file": "003-optional-slug.md"}`.

### `GET /steering`
Lists steering files with `consumed` flags and the iteration that consumed them.

### `POST /interrupt`
SIGINTs the current agent process (the iteration ends as `interrupted`; the loop
proceeds to the next iteration immediately, picking up any pending steering).
No-op `409` if no iteration is running. Body optional:
`{"message": "..."}` — convenience combo, equivalent to `POST /steering` then
interrupt.

### `POST /pause` / `POST /resume`
Pause finishes the current iteration, then holds before the next one
(`state: running`, `phase: paused` reported in `/status`). Resume releases it.

### `POST /retry`
Wakes an infra backoff wait immediately instead of waiting for
`infraWait.nextAttemptAt`: the failed phase/iteration is retried at once and
the outage-budget **episode clock is reset** (the cumulative `waitedS` starts
from zero again, while the attempt counter — and therefore the escalating
backoff — is kept). Emits `infra_retry_now`. `200 {"retrying": true}`.
CLI: `ralphctl retry <run-id>` (docs/cli.md). Hub: the "retry now" button on
a degraded run-detail card, via the hub's `POST /api/runs/<id>/retry` proxy
(docs/cli.md's `ralphctl ui` section).

`409` when the run is not actually in an infra wait (`health: "ok"` /
`infraWait: null`, or the job already finished) — the problem detail says so.
Naming: `/retry` is about a **degraded** run waiting out an endpoint outage;
`/resume` is about a **paused** run waiting for the operator. They are
independent — `/retry` never unpauses a paused run and never touches steering,
`/resume` never shortens a backoff.

### `POST /abort`
Body: `{"reason": "..."}`. Interrupts the current iteration and terminates the job
as `aborted` (honors `onComplete` — idles or exits).

### `POST /shutdown`
Only valid when the job is finished (`succeeded|failed|aborted`); exits the
container. `409` while running — use `/abort` first.

## Runtime configuration

### `GET /config`
Effective job config (redacted — no secret values, no credential file
contents, no LLM env values):

```json
{
  "runId": "...",
  "budgets": {"iterations": 25, "maxApproaches": 3, "jobTimeoutS": 28800, "iterationTimeoutS": 2700,
              "infraStartupTimeoutS": 150.0, "infraRetryBackoffS": [2, 5, 15, 30, 60, 120, 300],
              "infraRetryBackoffMaxS": 300.0, "infraRetryMax": null, "infraOutageBudgetS": 14400.0},
  "flags": {"vigilant": false, "onComplete": "idle"},
  "model": {"strategy": "quality-first", "model": null, "fastModel": null, "overrides": {}, "thinking": null},
  "prompts": [{"name": "planning", "source": "builtin"}, ...],
  "skills": [{"name": "...", "origin": "mounted"}, ...],
  "creds": ["github", ...],
  "llmEnvKeys": ["..."]
}
```

The infra-fault (LLM endpoint/network outage) budgets, all settable in
`job.yaml` and via env:

| Field | `job.yaml` key / env | Default | Meaning |
|-------|----------------------|---------|---------|
| `infraStartupTimeoutS` | `infra_startup_timeout_s` / `RALPHD_INFRA_STARTUP_TIMEOUT` | `150.0` | how long an iteration may run with zero observed LLM traffic before it is killed as an infra fault |
| `infraRetryBackoffS` | `infra_retry_backoff_s` / `RALPHD_INFRA_RETRY_BACKOFF_S` (`"s1,s2,..."`) | `[2, 5, 15, 30, 60, 120, 300]` | escalating wait between retries of the same phase/iteration; the last value repeats for further attempts |
| `infraRetryBackoffMaxS` | `infra_retry_backoff_max_s` / `RALPHD_INFRA_RETRY_BACKOFF_MAX_S` | `300.0` | cap on a single backoff wait (clamps the repeating tail) |
| `infraRetryMax` | `infra_retry_max` / `RALPHD_INFRA_RETRY_MAX` | `null` | optional attempt cap, **honoured only when set explicitly**; `null` means no cap — retry for as long as the outage budget allows |
| `infraOutageBudgetS` | `infra_outage_budget_s` / `RALPHD_INFRA_OUTAGE_BUDGET_S` | `14400.0` (4h) | wall-clock budget for one fault episode: retries continue while the cumulative wait stays under it; the episode resets on any successful iteration. The post-terminal `reflect` iteration gets `min(this, 300s)` — the job is already over, so a still-dead endpoint must not hold the container open for hours (a `reflect_infra_delay` event covers the wait before its first attempt) |

`creds` lists credential *names* only (no values, no sizes here — see
`GET /config/creds` for that); `llmEnvKeys` lists the *names* of any env
overrides set via `PUT /config/llm`, never their values.

### Prompts — override + listing

| Method & path | Body / response |
|---------------|-----------------|
| `GET /config/prompts` | list: `{"name": ..., "source": ...}` for every phase (`planning`, `worker`, `review`, `task-verify`); `source` is `builtin` / `mounted` (`/config/prompts/{name}.md`) / `api` (after a `PUT`) |
| `PUT /config/prompts/{name}` | `text/markdown` body. Replaces a phase prompt override; `name` must be one of the four phase names above, else `422`; `204` on success. Effective next iteration that builds this phase's prompt. |

### Skills — full CRUD

| Method & path | Body / response |
|---------------|-----------------|
| `GET /config/skills` | list: name, origin (`mounted` / `api`), file count |
| `GET /config/skills/{name}` | `application/x-tar` — the skill directory |
| `PUT /config/skills/{name}` | `application/x-tar` — a skill directory (must contain `SKILL.md`); unpacked, visible next iteration; `204` |
| `DELETE /config/skills/{name}` | removes the skill (mounted or api-added); `204`, `404` if absent |

### Credentials — full CRUD (env-file convention)

Credential files follow the env-file convention (see architecture §5): each
`{name}.env` is `KEY=value` lines placed at `~/.creds/{name}.env` (0600) inside
the container; prompts tell the agent to source the file it needs. **Read-back is
allowed by design**: the API bearer token is defined as equivalent to holding the
job's credentials — protect the token, not individual routes.

| Method & path | Body / response |
|---------------|-----------------|
| `GET /config/creds` | list: name, size, mtime — no values |
| `GET /config/creds/{name}` | `text/plain` — the env file contents |
| `PUT /config/creds/{name}` | `text/plain` env-file body → `~/.creds/{name}.env` (0600); recognized extras (`gitconfig`, `netrc`, `ssh/`) get conventional re-placement; `204` |
| `DELETE /config/creds/{name}` | removes the file from `~/.creds/`; `204` |

Changes to skills and creds take effect at the next iteration (prompts embed the
current inventory at iteration start). Credential values never appear in
`/run`, events, or engine logs.

### `PUT /config/llm`
Body: `{"env": {"KEY": "value", ...}, "pi": { ...models.json fragment... }}`.
Replaces the LLM endpoint configuration; the engine re-merges pi settings and
applies env to subsequent iterations. Used by `ralphctl llm set` for mid-run
endpoint/key rotation. `204`.

## Versioning

`GET /version` → `{"ralphd": "0.1.0", "api": 1, "pi": "<pi version>"}`. Breaking
API changes bump `api`; `ralphctl` checks compatibility on connect.
