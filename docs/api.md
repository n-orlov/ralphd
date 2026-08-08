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
  "currentIteration": {
    "number": 7, "phase": "worker",
    "model": "anthropic/claude-opus-5",
    "startedAt": "2026-08-08T14:02:33Z"
  },
  "tasks": {"total": 9, "completed": 4, "inProgress": 1,
            "pending": 3, "validationFailed": 1, "failed": 0},
  "steering": {"pending": 0, "consumed": 2},
  "usage": {"inputTokens": 812345, "outputTokens": 90123, "costUSD": 14.20}
}
```

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

### `GET /iterations/{n}`
One iteration's `meta.json`.

### `GET /iterations/{n}/output`
`application/x-ndjson` — the full agent transcript for iteration *n*. Supports
`?tail=<lines>`. For the in-flight iteration, `?follow=true` streams new lines until
the iteration ends (chunked).

### `GET /events`
`text/event-stream` (SSE). Replays from `?since=<eventId>` (default: live only),
then follows. Event types:

| type | payload highlights |
|------|--------------------|
| `state` | lifecycle transition |
| `phase` | phase entered (planning/worker/verify/review), approach number |
| `iteration.start` / `iteration.end` | number, phase, model / exit, sentinel, usage |
| `task` | task id + old/new status |
| `steering.received` / `steering.consumed` | steering file name |
| `signal` | COMPLETE / VERIFIED / task-verified detected |
| `log` | engine-level notices (timeouts, retries, failures) |

Every event also lands in `events.jsonl` in the run dir with a monotonically
increasing `id`.

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

### `POST /abort`
Body: `{"reason": "..."}`. Interrupts the current iteration and terminates the job
as `aborted` (honors `onComplete` — idles or exits).

### `POST /shutdown`
Only valid when the job is finished (`succeeded|failed|aborted`); exits the
container. `409` while running — use `/abort` first.

## Runtime configuration

### `GET /config`
Effective job config (redacted — no secret values, no credential file contents).

### `PUT /config/prompts/{name}`
`text/markdown` body. Replaces a phase prompt override
(`planning|worker|review|task-verify|agent`). Effective next iteration.

### `PUT /config/skills/{name}`
`application/x-tar` body — a skill directory (must contain `SKILL.md`). Unpacked
into the skills dir; visible to the agent next iteration. `DELETE` removes it.

### `GET /config/skills`
Lists installed skills and their origin (baked / mounted / api).

### `PUT /config/creds/{name}`
`application/octet-stream` — drops a credential file into the creds dir (mode 0600)
and re-runs recognized-placement (gitconfig/netrc/ssh). Never echoed back by any
endpoint. `204`.

### `PUT /config/llm`
Body: `{"env": {"KEY": "value", ...}, "pi": { ...models.json fragment... }}`.
Replaces the LLM endpoint configuration; the engine re-merges pi settings and
applies env to subsequent iterations. Used by `ralphctl llm set` for mid-run
endpoint/key rotation. `204`.

## Versioning

`GET /version` → `{"ralphd": "0.1.0", "api": 1, "pi": "<pi version>"}`. Breaking
API changes bump `api`; `ralphctl` checks compatibility on connect.
