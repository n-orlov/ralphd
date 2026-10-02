# ralphd Container API

Each job container serves this HTTP API (default `:7777`, published to
`127.0.0.1:<host-port>` by `ralphctl`). Content type is JSON unless noted. When an
API token is configured, every route requires `Authorization: Bearer <token>` and
replies `401` otherwise. **This is an operator opt-in and is orthogonal to the
container boundary below** — see "The container boundary" for the gate that
holds by default, with or without `--api-token`.

Errors are JSON in FastAPI's `{"detail": {...}}` envelope, and the body inside
it is problem-*shaped* — `{"title", "status", "detail"}`, built by the one
helper `ralphd.engine.api.problem`. It is not RFC 7807 on the wire: there is no
`type` member and the media type is plain `application/json`. The one route-less
reply is the auth middleware's `401`, which has no `detail` envelope around it:
`{"title": "unauthorized", "status": 401}`.

## The container boundary (requirement A, #62)

An iteration (the agent process the engine spawns for planning/worker/
verify/review/reflect) runs inside the same container and network
namespace as the engine's own control-plane API, over the same loopback
port an operator's `ralphctl` also reaches. Fact 5
(`docs/prds/v0.9-declarative-loop.md`) measured a probe agent doing nothing
but reading its own environment reaching every mutating route below — the
iteration is not a sandboxed peer of the operator, and nothing in this
boundary claims it is a sandbox at all (fact 17: with a real docker socket
mounted, an iteration that wants the control plane badly enough can reach
it some other way; what this closes is the one demonstrated, cheap route).

Two independent gates, and only the second is the boundary:

- **`api_token`** (`Authorization: Bearer <token>`) — optional, operator
  chosen (`ralphctl start --api-token auto|<value>`), and when set it gates
  *every* route, mutating or not. Unset by default. This predates
  requirement A and is unaffected by it.
- **`control_token`** (`X-Ralphd-Control-Token: <token>`) — gates *only*
  the mutating routes listed below, and `ralphctl start`/`resume` provision
  one **unconditionally**, whether or not the operator ever passes
  `--api-token`. `PiRunner` never hands a spawned iteration's process the
  coordinate that would let it construct this header
  (`RALPHD_CONTROL_TOKEN` is one of the names `engine/config.py`'s
  `CONTROL_PLANE_ENV_VARS` strips — same mechanism as `RALPHD_PORT` and
  `RALPHD_API_TOKEN`, task 003), so an iteration's own HTTP client can never
  present it — asserted per route in
  `tests/test_control_plane_auth.py`.

**Mutating routes** (require `control_token` when one is configured, i.e.
unconditionally for any run started or resumed by `ralphctl` after task
004): `POST /steering`, `POST /rule`, `POST /interrupt`, `POST /pause`, `POST /resume`,
`POST /retry`, `POST /abort`, `POST /shutdown`, `PATCH /config/budget`,
`PUT /config/prompts/{name}`, `PUT`/`DELETE /config/skills/{name}`,
`PUT`/`DELETE /config/creds/{name}`, `PUT /config/llm`. This list is
code-derived, not hand-maintained: `tests/test_route_inventory.py`'s
`EXPECTED_MUTATING` is exactly this set, checked against the live app's own
`app.routes` on every test run.

**Observation routes** (never gated by `control_token`; an iteration may
read all of these): `GET /healthz`, `GET /version`, `GET /status`,
`GET /tasks`, `GET /prd`, `GET /notes`, `GET /iterations`,
`GET /iterations/{n}`, `GET /iterations/{n}/output`, `GET /logs`,
`GET /events`, `GET /artifacts` and `GET /artifacts/{path}`, `GET /config`,
`GET /config/prompts`, `GET /config/skills`, `GET /config/skills/{name}`,
`GET /config/creds`, `GET /steering`.

`--api-token` was never a container boundary and its docstrings/CLI help
say so: it is a network control (who on the network the operator exposed
the port to may call the API at all), not a defence against the iteration
running inside the same container the API is served from.

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
  "engineVersion": "0.21.0",       // the running engine's own ralphd.__version__, written every process start (v0.21 requirement F.4, task 030), drift or not
  "workspaceVersion": "0.20.9",    // present whenever the workspace's own __version__ literal is readable, matching or not (v0.21 requirement G.3, task 035); absent only when the workspace names no version at all
  "schemaVersion": 1,              // run-dir schema this state was written under
  "state": "running",              // starting|running|paused|finishing|timed_out|succeeded|failed|aborted
  "phase": "worker",               // planning|worker|verify|review|null; never "paused"
  "pauseReason": null,             // why an ENGINE-recorded pause is held, task 036; null unless `state` is `paused`
  "pauseDetail": null,             // the pause's sub-reason (a provider refusal, the rate-limit ceiling); null otherwise
  "approach": 1,
  "maxApproaches": 3,           // approach denominator; null for a pre-v0.6 run dir
  "model": "amazon-bedrock/eu.anthropic.claude-opus-5",  // model observed in use
  "modelRaw": "eu.anthropic.claude-opus-5",             // gateway id, when different
  "image": "ralphd:9f2c1a4b7d80",   // job image this run is running (see below)
  "imageId": "sha256:0f1e2d3c…",   // the image the container actually got
  "imageSource": "built",          // pinned|cached|built|unhashable|recorded|default
  "imageHash": "9f2c1a4b7d80",     // content hash it was tagged by; null if not content-derived
  "imageBase": null,               // base it was derived from, when it is a derived image
  "imageDockerfile": null,         // operator Dockerfile that base was built from
  "iteration": 7,
  "iterationsBudget": 50,
  "iterationsUsed": 7,
  "iterationsRefunded": {"infra": 2, "steer": 0, "adjudication": 0},  // earned refunds; survives a resume
  "verdict": null,                 // "verified" | "verified-with-residuals" | "unverified" | "unreviewed" | null while running -- also null for a non-terminal `timed_out` hold (v0.24 requirement B.3, #169); settled (non-null, or the `timed_out` null) once `state` leaves `running`/`worker` for a hold or reaches `finishing`, task 012/#56; "verified-with-residuals" (requirement D.3) is a review VERIFIED verdict whose structured findings channel still carries one or more `severity: "residual"` entries; "unreviewed" is the narrower negative for an approach review never rendered a judgement on at all -- review's own contract ladder exhausted with no readable verdict claim out of the hat (#209 defect 2, `reason`: "review never produced a parseable contract payload; no verdict was ever scored"), reached only when the budget is also exhausted at that point. A deadline landing mid-review (self-loop exhaustion) carries `null` instead (v0.24 requirement B.2, #169, `reason`: "deadline during review"), not "unreviewed" -- the run holds `timed_out` and stays resumable. Neither writer overrides an operator abort, which keeps "unverified" -- SPEC.md §5.2 carries the full rule
  "reason": null,                  // why a non-succeeded run ended, including the `timed_out` hold's own note
  "onComplete": "idle",
  "createdAt": "2026-08-08T13:08:09Z",
  "startedAt": "2026-08-08T13:08:11Z",
  "updatedAt": "2026-08-08T14:02:33Z",
  "endedAt": null,                 // this episode's terminal write; reset when an episode starts
  "previousEndings": [],           // earlier episodes' {endedAt, reason, verdict}, oldest first
  "deadlineAt": "2026-08-08T21:08:11Z",
  "infraWaitTotalS": 62.5,
  "pausedTotalS": 0,               // cumulative time credited back for operator pauses, task 016
  "health": "ok",                  // "ok" | "degraded"
  "infraWait": null,               // populated only while waiting out an infra outage
  "rateLimitWait": null,           // populated only while waiting for a rate-limit window to reset
  "rateLimitWaitTotalS": 0,        // cumulative rate-limit wait; written once a wait has happened
  "reflect": null,                 // absent/null until the reflect phase ends, see below
  "currentIteration": {
    "number": 7, "phase": "worker",
    "model": "anthropic/claude-opus-5",
    "startedAt": "2026-08-08T14:02:33Z"
  },
  "tasks": {"total": 9, "completed": 4, "inProgress": 1,
            "pending": 2, "validationFailed": 1, "failed": 1,
            "failedRequirementUnmet": 1},   // sub-counts, see below
  "tasksStale": false,             // see "Stale task reads" below
  "tasksSource": "file",           // absent|file|last-good|unreadable
  "steering": {"pending": 0, "consumed": 2},
  "unconsumedSteering": [],        // steering files still pending at the terminal write
  "steerSettled": null,            // last steer-hat settle claim: null, or {"settled", "at", "notes"} -- task 013, #98
  "usage": {
    "input": 812345, "output": 90123, "totalTokens": 902468, "costUSD": 14.20,
    "byPhase": {
      "planning": {"input": 40000, "output": 4000, "totalTokens": 44000, "costUSD": 0.60},
      "worker": {"input": 700000, "output": 80000, "totalTokens": 780000, "costUSD": 12.00},
      "review": {"input": 72345, "output": 6123, "totalTokens": 78468, "costUSD": 1.60}
    },
    "byApproach": {
      "1": {"input": 812345, "output": 90123, "totalTokens": 902468, "costUSD": 14.20}
    },
    "byTier": {
      "normal": {"input": 812345, "output": 90123, "totalTokens": 902468,
                 "costUSD": 14.20, "iterations": 6}
    }
  }
}
```

The payload is the run dir's own `status.json` **verbatim**, plus the fields
only a live engine can add: the task counts and their `tasksStale`/`tasksSource`
provenance, the `steering` counts, and the host-recorded `image*` record. The
keys with no section of their own below:

| field | meaning |
|-------|---------|
| `schemaVersion` | the run-dir schema this state was written under; a run dir whose recorded version is newer than the engine build refuses to start (see `docs/architecture.md`) |
| `createdAt` / `startedAt` | first status write (state `starting`) / when the loop entered `running` |
| `updatedAt` | last write of any field — every `update_status` stamps it |
| `endedAt` | this **episode**'s terminal write; `null` while the run is still going, and reset when a resumed episode starts (so it can never predate `startedAt`). Written the moment the outcome is settled, which with `reflect: true` is *before* `state` becomes terminal -- see `pendingTerminalState` |
| `pendingTerminalState` | present only while `state` is `finishing` (task 012, #56): the terminal state (`succeeded`\|`failed`\|`aborted`) this run is going to become the moment the post-terminal `reflect` iteration is over. `verdict` is already the settled value at that point too -- nothing after `state` becomes `finishing` can change either one |
| `reason` | why a non-succeeded run ended, including the `timed_out` hold's own note (v0.24 requirement B.3, #169); also episode-scoped, so a resumed run never reports the previous engine's reason |
| `previousEndings` | earlier episodes' endings, oldest first — `{endedAt, reason, verdict}` per superseded ending, `[]` for a run still in its first episode, capped at the 50 most recent. Kept because the terminal `state` event in `events.jsonl` records the state but not the reason or verdict |
| `termination` | present once the run was told to stop — see below |
| `iterationsUsed` | the **charged** iteration count: raw attempts minus infra AND steer refunds (task 014, #98). `iteration` (the raw number of the attempt running now) is the higher figure whenever anything was refunded, and `iterationsUsed` is what every budget surface prints and what `PATCH /config/budget` validates against |
| `iterationsRefunded` | `{infra, steer, adjudication}` — the refunds this run has earned, written as they are earned and read back by the next engine process, so a `resume` (or an auto-resume after a crash) no longer re-charges refunds granted before it (issue #32). Absent, and read as zeroes, until the run earns its first refund; a pre-v0.7 run dir therefore keeps its recorded `iterationsUsed`. `infra` also counts the refunded attempts of a rate-limit wait and of a provider refusal (`faultClass` `"rate_limit"`/`"refused"`). Infra and steer refunds are subtracted from `iterationsUsed`; an adjudication episode's refund (v0.24 task 015, requirement E.1, #169, renamed this key from the retired off-budget grace review's own `grace`) is deliberately not (it stays visible as a used iteration) but is still subtracted from the budget comparison |
| `unconsumedSteering` | steering files still pending at the terminal write |
| `steerSettled` | the most recent `steer` hat iteration's own `settled` claim (`{"settled": bool, "at": timestamp, "notes": string\|null}`), `null` until the first one runs. `settled` is a harness-observable fact distinct from `done`: it means only "the hat processed this iteration's pending steering message(s) and applied its changes to the plan" -- it says nothing about whether the tasks it inserted or changed will pass. That verdict is the ordinary lifecycle's, read off those tasks' own `status` plus their `origin: "steer"` marker (task 011); the exit gate (`GET /tasks`, the terminal `state`/`verdict`) waits on THAT, never on this field (task 013, #98) |
| `steering` | live counts — `{pending, consumed}` over `steering/NNN-*.md`, added by the API rather than stored |
| `approachStartHeads` | the workspace HEAD an approach started at (v0.20.1 requirement A.1, task 001) — `{"<approach>": "<sha>"|null}`, written by `_approach_start_head` the FIRST time `_step_worker` reads it for that approach and read back unchanged on every later entry for the SAME approach — another iteration of the same process, an in-process resume, or a genuinely fresh process/`LoopSupervisor` over the same run-state tree — so the stagnation-exhaustion "did this approach gain commits" comparison's own origin can never be silently reset to whatever HEAD happens to be at the moment of a restart. `null` for a non-git workspace, still recorded. Absent for an approach whose `_step_worker` has never run |
| `blockerSignatures` | per-approach worker-stagnation blocker signature (task 101, #61) — `{"<approach>": "<signature>"}`, written the moment an approach fails on stagnation; the `replan` guard compares the entry for the approach that just failed against its predecessor's, and a genuine match ends the run instead of spending another approach on the same structural wall. Absent for a run that has never stagnated |
| `blockerSignatureOrigins` | per-approach origin of the matching `blockerSignatures` entry (task 012, requirement C.2, #124) — `{"<approach>": "stagnation"}`, written alongside each `blockerSignatures` entry by `_record_stagnation_signature`. Task 003 (requirement C) retired plan-ingestion's use of this shared, approach-keyed map entirely — a zero-deliverable planning death deliberately never advances the approach counter, so "stagnation" is the only value this map has ever held since; kept as its own field (rather than folded away) for the run history it already accumulated before task 003. Absent for a run that has never stagnated |
| `planningDeathSignature` | plan-ingestion's own two-strike signature (task 003, requirement C) — a normalised lexical-lint signature persisted directly on status.json, NOT keyed by approach, because a zero-deliverable planning death (an ingestion episode that produces no usable task at all) deliberately never spends a product approach; `_step_planning` compares a failing ingestion's signature against this field, unkeyed, to tell a genuinely repeated failure from a distinct one across consecutive planning attempts on the SAME approach. Reset to `null` the moment any ingestion succeeds. Absent for a run that has never had a plan proposal refused at ingestion |
| `planningDeathDetail` | the RAW ingestion-error text (task 007, requirement B.3, #135 wording preserved) paired with `planningDeathSignature` — quoted verbatim into the terminal reason when the two-strike guard fires on it. Absent for a run that has never had a plan proposal refused at ingestion |
| `planReviewRevisions` | per-approach peer plan-review revision counter (task 018, PRD requirement K, #101) — `{"<approach>": <count>}`, incremented each time the opt-in `plan_review` hat objects to a plan and a revision is still available; `_step_plan_review`'s `plan_review_revision_bound_reached` guard compares the entry for the current approach against `peer_plan_review_max_revisions` (default 2) to decide when the plan proceeds regardless of further objections. Absent for a run with `peer_plan_review: false` (the default) or one that has never had an approach objected to |
| `planReviewApproved` | per-approach marker that the peer plan review let the plan proceed (`approved`, or `plan_review_revision_bound_reached`) -- `{"<approach>": true}`, written by `_step_plan_review` (#161). `_step_planning(skip_planning=True)` on a resume takes `planning_skipped_plan_already_reviewed` -> `worker` when the entry is present (or when any task is already past `pending`), instead of re-running `plan_review`; `_step_plan_review` itself refuses a revision over a worked plan (`plan_review_revision_refused_work_recorded`) because ingesting a fresh proposal resets every task to `pending`. Absent for a run with `peer_plan_review: false` or one whose plan review has not spoken yet |
| `midPlanReviewEpisodes` | per-approach mid-plan review episode marker (task 022, PRD requirement K.2, #169) — `{"<approach>": true}`, written by `LoopSupervisor._record_mid_plan_review_done` at dispatch — strictly before the episode's own `run_iteration` call is awaited — so an episode killed mid-flight (a crash, an operator interrupt, the job deadline cancelling the iteration task) still counts as spent and a resumed process never re-runs the ONE episode an approach is entitled to. `LoopSupervisor._maybe_run_mid_plan_review_episode` (called from the same `_step_worker` seam the mid-run petition-adjudication episode already uses) runs the episode once the declared fraction (`loop.MID_PLAN_REVIEW_FRACTION`) of the plan has already reached terminal-success, scoped to those requirements; its findings open a cure task per blocking finding through the existing `state.insert_cure_tasks` path. Absent for a run with `mid_plan_review: false` (the default) or one that has not yet reached the declared fraction |
| `workerSplitProposals` | per-approach worker split-proposal counter (task 021, PRD requirement N, #102) — `{"<approach>": <count>}`, the running total of tasks a worker's optional `proposed_tasks` contract field (`transitions.HAT_SCHEMAS["worker"]`) has actually inserted for that approach; `LoopSupervisor._apply_worker_proposed_tasks` refuses a proposal whole (never partially) once accepting it would push the total past `worker_split_budget_per_approach` (default 3). Absent for a run that has never had an accepted split proposal |
| `workerRepointUpdates` | per-approach worker `dependsOn` re-point counter (#209 defect 7) — `{"<approach>": <count>}`, the running total of EXISTING tasks a worker's optional `proposed_task_updates` contract field (`transitions.HAT_SCHEMAS["worker"]`) has actually re-pointed for that approach; `LoopSupervisor._apply_worker_task_updates` refuses a batch whole (never partially) once accepting it would push the total past `worker_repoint_budget_per_approach` (default 3). A separate counter from `workerSplitProposals` above because a re-point inserts no task. Absent for a run that has never had an accepted re-point |
| `steerRedeliveries` | per-MESSAGE steering redelivery counter (task 006, PRD requirement B.1, #122) — `{"<steering-file-name>": <count>}`, incremented each time a clean `steer` iteration honestly reports `settled: false` for that message: `LoopSupervisor._handle_unsettled_steering` UNCONSUMES the file (`RunDir.unconsume_steering`) so it is genuinely redelivered to the next actionable iteration, up to `steer_redelivery_max` (default 3) redeliveries. Once a message's count reaches the cap it stops being redelivered and is instead recorded settled-BY-EXHAUSTION, with the reason, in the internal steering ledger that `state.steering_ledger` renders into every following hat's prompt (`settledByExhaustion`/`exhaustionReason` entries — not fields of this endpoint's own payload). `settled: true` never touches this counter — it consumes exactly once, as before this task. Absent for a run that has never had a `settled: false` steer reply |
| `verdictGatePeeks` | v0.17 requirement D.5 (#125 A): a list of `{approach, iteration, calls}` entries, one per `verdict_gate` iteration (the ordinary `_step_verdict_gate` -- v0.24 task 015/E.1 retired its off-budget `_grace_verdict_gate` twin, the SECOND call site this used to name) whose OWN transcript contained a tool call reading past the gate's honor-system blindness (`iterations/`, `events.jsonl` or the per-iteration transcript file -- `state.detect_verdict_gate_peek`). A RECORDED OBSERVATION only: it never changes `verdict`, and a run can reach `succeeded` with a non-empty list here. Absent for a run with `peer_verdict_gate: false` or one whose gate iterations never peeked |
| `pauseReason` | v0.18 requirement D.1/D.2 (#142, task 036): why the ENGINE paused itself, recorded together with `state: "paused"` the moment the decision is made and cleared (back to `null`, with `state` back to `running`) by `POST /resume`. Nine values: `"rejected_last_approach"` — the last approach was rejected on blocking findings that are all `curable: true` with its one cure pass already spent, so there is no approach budget left to cure them in and the run holds for an operator ruling instead of ending `failed` on paperwork, and `resume` re-enters `review` over the SAME approach; `"stagnation"` (v0.19 requirement B.3, #153, task 012) — the last approach's worker stagnation guard would otherwise fail the run silently, so the run holds instead, and `resume` re-enters `worker` over the SAME approach (no verdict pending, only unfinished tasks); and `"rejected_repeated_signature"` (v0.20.1 requirement E.5, #190, task 016) — an ORDINARY rejection's own blocking-clause signature (`rejectionSignatures`, above) repeated the immediately preceding approach's own recorded rejection signature, so the run holds instead of spending a fresh approach on a wall it has already lost one approach to, and `resume` re-enters `review` over the SAME approach, exactly like `"rejected_last_approach"`; `"unschedulable_dependency_cycle"` and `"unresolvable_dependency_edge"` (v0.22 requirement E, #206) — the plan's own actionable remainder is blocked by a `dependsOn` cycle, or by an edge naming a task id the plan does not carry, so the run holds for an operator's `tasks.json` edit rather than grinding through three no-progress worker iterations first, and `resume` re-enters `worker` over the SAME approach, exactly like `"stagnation"`; and `"review_contract_unrepaired"` (GH #209 defect 2) — `review` ran to completion but its own verdict claim was never readable across `contract_retry_max + 1` attempts, so no verdict was ever rendered and the run holds instead of attributing a rejection to a judge that never spoke (the validation errors ride along on the `operator_escalation` event as `contractErrors`), and `resume` re-enters `review` over the SAME approach, exactly like `"rejected_last_approach"`; `"review_findings_unusable"` (v0.23 requirement C.2, #219, task 011) — the discriminating sibling of `"review_contract_unrepaired"`: review's own verdict claim WAS legible, but the REVIEW CONTRACT CHANNEL the ladder re-asked over never became usable (a present-but-invalid findings sidecar, a `verified` claim contradicting a blocking finding, a curable finding with no `cured_when`, or — v0.23 requirement C.3, task 012 — a stray/wrong-typed field elsewhere in the payload's own closed schema), so there is no rejection to classify off a channel the engine itself cannot trust and the run holds instead, and `resume` re-enters `review` over the SAME approach, exactly like the edge above; and `"operator_input_needed"` (v0.23 requirement A.1, #224, task 014) — an ORDINARY rejection that would otherwise archive the approach (whether the next step would have been a fresh `replan` or the terminal `rejected_no_approach_left` outcome) holds instead, ON by default (`JobConfig.operator_input_needed_pause`, disabled with `ralphctl start --no-operator-input-needed-pause`) so an operator can answer — with a ruling (`ralphctl rule <run> --file <ruling.md> --name <slug>`, task 013) or a plain `ralphctl unpause <run>` — before that spend happens, and `resume` re-enters `review` over the SAME approach, exactly like every edge above; and `"verdict_gate_operator_input_needed"` (v0.27 requirement C.1, task 008) — the peer verdict gate's own mirror, narrower than `"operator_input_needed"`: a `verdict_gate` rejection landing while the run-wide `cure_pass_ceiling` is already spent holds instead of its ordinary edge (archive/replan-or-finish for an all-curable set, the immediate merits tail for a mixed or all-incurable one) -- for every blocking-set shape (task 002): all-curable, mixed and all-incurable alike -- ON by default through the SAME `JobConfig.operator_input_needed_pause` flag, and `resume` re-enters `verdict_gate` (never `review`, which has already spoken `verified` for this approach) over the SAME approach; releasing it always needs a separate `ralphctl unpause <run>` (a ruling filed against it alone never releases it, unlike `"operator_input_needed"`/`"rejected_repeated_signature"`). The task-boundary commit-secret-scan hold (task 030, requirement I.3, #210) records this SAME `pauseReason` from `worker` with no review verdict behind it: a worker iteration's own commit carried a known secret value, the push is refused and the assigned task is faulted `failed`, and the run holds. That pause emits NO `operator_escalation` event -- its record is the `commit_secret_scan` event (`sha`, `part`, `variable`, never the value) -- so neither `parkedQuestion` nor the resume-into-`review` routing applies to it, and whether a ruling releases it depends on its approach's own `rejectionSignatures` entry (cure task cure-01-01-9, PRD A.1/A.6, D.1): with none (the common case — a fresh approach, or one with no earlier reasoned hold), a ruling filed against it is recorded but the run stays paused, and only `ralphctl unpause <run>` answers it; with one (an earlier `"operator_input_needed"`/`"rejected_repeated_signature"` pause on the SAME approach, already answered by a ruling before this hold ever landed), `ralphctl rule <run> --file <ruling.md> --name <slug>` (`POST /rule`) releases it too, since `resume_if_ruling_answers_pause` reads only this field and that signature, never which pause raised it, so either lever answers it. `resume` re-enters `worker` over the SAME approach, exactly like `"stagnation"`. Either way the approach is **not** archived and `approach`/`phase` are left untouched. For these three reasons (`"operator_input_needed"`, `"rejected_repeated_signature"` and -- task 003, this wave -- `"rejected_incurable"`), the `operator_escalation` event that landed the pause always carries the A.7 fields `taskIdsValidatedToPending`, `insertCount`, `costSpentThisApproach`, `tokensSpentThisApproach`, `approachOfMax` and `hubLink`, and also `nextStepIfUnanswered` (task 007, v0.26, #269) — `"cure_pass"` \| `"archive_and_replan"` \| `"finishing"` (cure task cure-01-05, PRD v0.27 requirement C.4, DoD 3, added the third value), a fresh, read-only prediction of which of the three the pause is about to take if never answered — `"finishing"` only when this approach is ALSO the run's last (`replanAvailable: false`): the pause archives this approach either way, but ends the run instead of replanning a fresh one, since `_step_review`/`_step_verdict_gate` only start a new approach when one is still budgeted. Task 011 (v0.27 requirement C.4, #275), extended by cure task cure-01-05 and widened once more by task 003 (this wave): that preview always names the edge `nextStepIfUnanswered` itself names, never the raw findings' own reopen prediction when the two disagree -- reads `taskIdsValidatedToPending: []`/`insertCount: 0` exactly when `LoopSupervisor._review_cure_pass_available` says no, whatever the reason: the run-wide `cure_pass_ceiling` spent (the `escalation` text then names `ralphctl budget <run> --cure-passes +N\|N` as the lever that would change that outcome), this approach's own no-progress comparison finding the rejection re-files a previously targeted finding with nothing new to reopen (raising the ceiling would not change THAT outcome, so no such note), or (task 003) the findings were never curable at all -- mixed, all-incurable, a criteria-tamper finding (no such note either, for the same reason). `"rejected_incurable"`'s own pause, which by construction always carries an all-incurable or mixed blocking set, therefore always reads the zeroed preview and `nextStepIfUnanswered: "archive_and_replan"` (its own caller-side guard only ever fires it over an approach still short of `cfg.max_approaches`). `"verdict_gate_operator_input_needed"` carries this SAME six-item payload too (task 011; it carried none before), always the zeroed preview, since that pause fires only on the ceiling-spent edge, and the budget note only for an all-curable rejection — a mixed or all-incurable one takes its merits tail whatever the ceiling, so neither its `escalation` text nor its delivered notification digest names `--cure-passes`. Every pause that names the budget note names it identically in the event text and the delivered digest, and contrasts the cure edge with the edge an unanswered pause actually takes (archive-and-replan, or finishing the run on the last approach); `"rejected_last_approach"` carries it too when the run-wide ceiling is what refuses its all-curable rejection a cure pass — and the hub's `GET /api/runs/<id>` shapes that same escalation into a `parkedQuestion` field for exactly these two reasons and no other, `null` otherwise — `{reason, approach, maxApproaches, approachOfMax, parkedAt, replanAvailable, blockingCount, residualCount, findings, consequence}`, where `findings` lists EVERY finding, blocking ones first (`id: "F<n>"`, the escalation's own numbering) then residual ones off the on-disk `review-findings.json` sidecar (`id: "R<n>"`), each `{id, severity, clause, evidence, cured_when, invalidates}` with its evidence whole, and `consequence` is `{taskIdsValidatedToPending, insertCount, costSpentThisApproach, tokensSpentThisApproach, nextStepIfUnanswered}` (see [cli.md](cli.md)'s `GET /api/runs/<id>` and SPEC.md §11.3). `null` (or absent) for every other run — an operator `POST /pause` records neither this field nor a `state` change |
| `pauseDetail` | the sub-reason of an engine pause that shares `pauseReason: "operator_input_needed"` but has no review rejection behind it, written together with `pauseReason` and cleared with it by `POST /resume`. For a refused iteration (`faultClass: "refused"`, below) it is `{"reason": "provider_refused", "family", "message", "httpStatus", "phase", "iteration"}`: the refusal family, the provider's error (scrubbed, at most 500 characters), its HTTP status, and the phase and iteration it refused. When a rate-limit wait would take the run past `rate_limit_max_wait_s` it is `{"reason": "rate_limit_ceiling", "resetsAt", "waitedS", "maxWaitS", "plannedS", "rateLimitType", "phase"}`: the window's reset (ISO), the seconds waited since the last ceiling unpause, the ceiling, the wait that did not start, and the phase; `POST /resume` resets the count and re-probes the same work, and `PATCH /config/budget` `rateLimitMaxWaitS` raises the ceiling. While it is set, `POST /resume` grants no `noRulingProceed` one-shot and a ruling filed through `POST /rule` does not release the pause. Fix the profile or credentials (`PUT /config/llm`), then `POST /resume` (`ralphctl unpause`) re-runs the same work. `null` otherwise |
| `curePasses` | v0.18 requirement C.1/C.2 (#141) — per-approach cure-pass counter, `{"<approach>": <count>}`: taken when review's verdict is `rejected`, every blocking finding is `curable: true`, the run-wide cure-pass total (this counter's values plus `gateCurePasses`' own, summed over every approach — `LoopSupervisor._cure_passes_for_run`) is below `JobConfig.cure_pass_ceiling` (job.yaml key `cure_pass_ceiling`, default **3** — v0.21 requirement A.2, #192, task 002; a per-approach comparison against a module-level `CURE_PASS_CEILING` literal before that task, so a replanned approach no longer resets the cure budget and a fresh approach's FIRST rejection is refused too once the run's ceiling is spent), and — on a SECOND or later rejection over the same approach (v0.20 requirement E.1, #167, task 030) — this rejection is not a repeat of the preceding granted pass's own target: on an ORDINARY (non-confirmation) review, its own blocking clause set is not a superset (by `clause`) of `cureBlocking`'s entry, below; on a CONFIRMATION review (v0.27 requirement B.4, #274, task 007 — `LoopSupervisor._is_confirmation_review`, true once any GRANTED `cure-lineage.json` entry names the approach (`kind: "granted"`, `state.insert_cure_tasks`'s own `pass_kind` default — review's own granted pass or the peer gate's granted pass; never a REFUTED-remedy insertion's own entry, `kind: "remedy"`, cure-01-01/VG-01), clause text is ignored and the set of `reopens` cure task ids its blocking findings carry, each followed back through `tasks[].reopensCureTaskId` to its root finding, is not a superset of the preceding pass's own targeted root set (read from the MOST RECENT `kind: "granted"` `cure-lineage.json` entry, `LoopSupervisor._latest_granted_cure_pass`, never a later remedy entry), so a rejection reopening every previously targeted finding is refused however it rewords the clauses and one reopening a strict subset is granted (only an ACTUAL first review of the approach — no `kind: "granted"` `cure-lineage.json` entry names it — is granted on the run-wide ceiling alone; `curePasses[approach] == 0` is not by itself a first review, because a gate-granted pass increments `gateCurePasses` and never this counter, so a confirmation review after a gate-only pass still reads `0` here yet takes the identity comparison, checked before this counter is consulted, and reopening every root that gate pass targeted is refused); incrementing it routes `review -> worker` over the SAME approach with cure tasks appended (`origin: "review-cure"`) instead of replanning. A second-or-later `rejected` that IS a superset, one at the ceiling, an incurable finding, or a criteria-tamper finding (v0.18 task 039: the engine's own `successCriteria`-authority finding is flagged `curable: true`, but a tampered approach is never cured -- it is archived and replanned) takes the ordinary replan/pause edge regardless of this counter. Disk-backed and survives a resume. Absent for a run that has never taken a cure pass |
| `cureBlocking` | v0.20 requirement E.1 (#167, task 030) — companion to `curePasses`: `{"<approach>": [<clause>, ...]}`, the blocking clause set (`findings.blocking_finding_clauses`, sorted) the MOST RECENT granted cure pass for that approach recorded, written in the same status update that increments `curePasses[approach]`; on an ORDINARY (non-confirmation) review a later rejection's own blocking clause set is compared against this entry to decide whether it earns another pass (not a superset) or falls through to the ordinary replan/pause edges (a superset, including an unchanged repeat); a CONFIRMATION review (v0.27 requirement B.4, #274, task 007) never reads this entry and compares reopened root-finding identity from `cure-lineage.json` instead (see `curePasses`, above). Disk-backed and survives a resume. Absent for a run that has never taken a cure pass |
| `gateCurePasses` | v0.21 requirement A.1 (#192, task 001) — the PEER VERDICT GATE's own per-approach cure-pass counter, `{"<approach>": <count>}`, kept under a separate key from `curePasses` so a cure pass the gate takes is never conflated with one review already took on the same approach: taken when the gate's own verdict is `rejected`, every blocking finding in `RunDir.verdict_gate_findings_json_file` is `curable: true`, none is a criteria-tamper finding, and the run-wide cure-pass total (this counter's values plus `curePasses`' own, summed over every approach — `LoopSupervisor._cure_passes_for_run`) is below `JobConfig.cure_pass_ceiling` (job.yaml key `cure_pass_ceiling`, default **3** — v0.21 requirement A.2, #192, task 002; the SAME configured, run-wide ceiling `curePasses` above is bounded by, so a cure pass either channel takes spends from one shared per-run budget); incrementing it routes `verdict_gate -> cure` over the SAME approach with cure tasks appended (`origin: "review-cure"`, `state.insert_cure_tasks` — the same call review's own cure pass uses) instead of archiving the approach and replanning, or ending the run once the approach budget is spent. Disk-backed and survives a resume. Absent for a run that has never taken a gate cure pass |
| `rateLimitMaxWaitS` | the rate-limit wait ceiling, in seconds, as last set in flight by `PATCH /config/budget` `rateLimitMaxWaitS` (`ralphctl budget <run-id> --rate-limit-wait +N\|N`); absent until then, when job.yaml's `rate_limit_max_wait_s` (default 86400) is the ceiling. Written by `LoopSupervisor.set_rate_limit_max_wait`; a fresh container seeds the ceiling from `max(status.json's rateLimitMaxWaitS, job.yaml's rate_limit_max_wait_s)` (`main._seed_rate_limit_max_wait`), so it survives a resume |
| `curePassCeiling` | v0.27 requirement C.3 (task 010) — the LIVE, run-wide cure-pass ceiling both `curePasses` and `gateCurePasses` above are bounded by, present only after `PATCH /config/budget`'s `curePassCeiling` knob (`ralphctl budget <run-id> --cure-passes +N\|N`) has changed it at least once; absent status.json defaults to `JobConfig.cure_pass_ceiling` (job.yaml's own `cure_pass_ceiling`, default **3**) instead. Written by `LoopSupervisor.set_cure_pass_ceiling` every time the knob is applied, and read back by a fresh container's own startup seeding (`main._seed_cure_pass_ceiling`, mirroring `iterationsBudget`'s own `main._seed_iteration_budget`): the effective ceiling at startup is `max(status.json's curePassCeiling, job.yaml's cure_pass_ceiling)`, so a live grant survives a resume the same way `iterationsBudget` does. Disk-backed. |
| `cure-lineage.json` (own run-dir file, not a `status.json` key) | v0.27 requirement B.5 foundation (#274) — every call to `state.insert_cure_tasks` that inserts at least one cure task persists one appended entry, through `state.insert_cure_tasks` itself, covering all THREE of its callers: review's own `_take_cure_pass` and the peer verdict gate's `_take_gate_cure_pass` (both granted cure passes), and `_insert_refuted_remedy_tasks` (a REFUTED-petition remedy insertion, which spends neither `curePasses` nor `gateCurePasses`) — `LoopSupervisor._is_confirmation_review` reads true off the MOST RECENT of those entries naming the approach whose `kind` is `"granted"` only, never off a `kind: "remedy"` entry alone (cure-01-01/VG-01: a remedy answers one obligation, never PRD B.1's own "at least one cure pass"). Each entry is `{"approach", "preCureHead", "findings", "recordedAt"}` on this run-scope, disk-backed ledger. `findings` is `{"<cureTaskId>": {"clause", "curedWhen"}}`, one entry per blocking finding that call inserted a cure task for, keyed by that cure task's own id; `preCureHead` is the workspace HEAD sha the call was made at (`null` for a non-git workspace). `kind` (cure-01-01/VG-01) tags the entry `"granted"` (the default, from the first two callers) or `"remedy"` (from the third) — the ONE distinction a reader needs to tell a genuine cure pass apart from a remedy insertion that also grew this list. `state.read_cure_lineage`/`cure_lineage_path` read it back, re-read from disk on every call — a fresh `LoopSupervisor` over the same run dir (a resume) sees byte-for-byte the same record. `LoopSupervisor._is_confirmation_review`/`_confirmation_review_listed_cure_task_ids`/`_confirmation_review_context`/`_cure_pass_targeted_roots` all read the MOST RECENT `kind: "granted"` entry only, through the one shared reader `_latest_granted_cure_pass` — a remedy insertion never replaces or narrows what a confirmation review, its listed ids or its no-progress root comparison see, however many of them land after the granted pass they all still key on. A cure task built from a finding carrying `reopens` (the cure task id of a listed finding still open) also carries `tasks[].reopensCureTaskId` in `tasks.json`, naming the cure task it descends from (`state.cure_task_from_finding`) — see SPEC.md's tasks-fields table and its run-dir file listing. Absent for a run whose `insert_cure_tasks` has never inserted a cure task at all, on any of its three callers |
| `gateRejectionEnding` | v0.21 requirement A.4 (#192, task 004) — which of THREE endings the peer verdict gate's MOST RECENT rejection was, one of `"rejected_on_the_merits"` (no curable path existed at all — the gate's own mirror of A.3's mixed/incurable case), `"rejected_cure_taken"` (this rejection took the `gateCurePasses` cure-pass edge above), or `"rejected_cure_unavailable"` (every blocking finding WAS curable, but the run-wide cure-pass ceiling was already spent — the budget-exhausted case A.2 bounds). Written by `LoopSupervisor._step_verdict_gate` (`_gate_rejection_ending`) on every gate rejection, before the cure/replan/tail edge that rejection takes, so it survives into whichever terminal record eventually results; `ralphctl status` renders each value with its own distinct wording (`cli.main._format_gate_rejection_ending_lines`) rather than leaving all three to read as the same `state: failed` / `verdict: unverified`, the exact ambiguity v0.20's #192 left an operator no way to resolve. Disk-backed and survives a resume, overwritten by each later gate rejection. Absent for a run that has never had a gate rejection |
| `rejectionSignatures` | v0.20.1 requirement E.5 (#190, task 016) — per-approach findings-channel REJECTION signature, `{"<approach>": "<signature>"}`: the sorted, normalised blocking-clause set (`LoopSupervisor._review_rejection_signature`) of an ORDINARY rejection (one that took none of `rejected_incurable`/`rejected_curable_pass_available`/`rejected_last_approach` above), written the moment that rejection is about to archive the approach and replan. `_step_review` compares the entry for the approach that just rejected against its predecessor's; a genuine match takes the `rejected_repeated_signature` pause edge instead of archiving/replanning — a repeated findings-channel wall pauses for an operator ruling rather than spending, and almost certainly losing, a fresh approach on it. Absent for a run that has never taken this ordinary-rejection branch |
| `rulingReparked` | v0.23 requirement A.5 (#224, task 016) — the list of operator ruling `seq` numbers (`state.ruling_entries`) that have already spent their ONE re-park: when a re-run `review` re-rejects on the SAME finding signature a recorded ruling addressed (`LoopSupervisor._ruling_honoured_for_approach`), the `operator_input_needed` pause fires once for that ruling and appends its `seq` here (`_record_ruling_reparked`); a LATER rejection matching that same ruling's own signature (`_ruling_already_reparked`) is bounded — it proceeds to the ordinary archive/replan tail instead of re-parking a second time. A fresh ruling filed later (a new `seq`), even against the identical signature, is a DIFFERENT ruling and is not covered by an earlier entry. Additive and disk-backed for the life of the RUN, never reset per approach. Absent for a run that has never re-parked on a ruled signature |
| `noRulingProceed` | v0.27 requirement C.2 (#274, task 009) — `{"<reason>": "<signature>"}`, a one-shot 'proceed' keyed by pause reason (`"operator_input_needed"` \| `"verdict_gate_operator_input_needed"`), granted by `LoopSupervisor.resume` the moment a bare `POST /resume`/`ralphctl unpause <run>` releases that EXACT pause with NO ruling filed to answer it, keyed on the SAME signature the pause was raised over (`rejectionSignatures[str(approach)]` for review's own pause; `LoopSupervisor._gate_rejection_signature` off the gate's own findings for its `"verdict_gate_operator_input_needed"` sibling). The very next rejection with the IDENTICAL signature consumes it (`_take_matching_no_ruling_proceed`) before any edge is chosen, whichever edge it then takes, and raises no same-signature operator gate while doing so: with a cure pass available (for example one granted through `PATCH /config/budget`'s `curePassCeiling` before the `POST /resume`) it takes the cure edge, otherwise the ordinary archive-and-replan (or finishing) edge instead of parking again — the same one-shot bound `rulingReparked` already gives a ruling, extended to cover a bare unpause with none filed. A grant never survives a cure to authorise a later archive; the next same-signature rejection after it parks afresh. A DIFFERENT signature never matches and still parks. Disk-backed and survives a resume; absent for a run that has never unpaused either pause with no ruling filed |
| `discretionaryGateSpent` | v0.23 requirement A.3 (#224, task 017) — the ONE shared discretionary gate trigger conditions 2 and 3 draw from, `{"<approach>": true}`: trigger condition 2 (`state.cure_pass_preview` shows a cure pass would re-open task(s) already `validated`) and trigger condition 3 (`LoopSupervisor._repeated_rejection_signature`, a rejection whose blocking-clause signature repeats the immediately preceding approach's) each check `_discretionary_gate_available` before pausing and `_spend_discretionary_gate` the moment they do — whichever of the two fires FIRST in an approach spends the entry for that approach's own string key; the other, should it also fire later in the SAME approach, falls through to its ordinary edge instead (the cure pass is taken silently; the repeated signature is recorded and the approach archived/replanned exactly as if it had never repeated). Condition 1's own `operator_input_needed` gate (task 014) is a SEPARATE, unbudgeted pause and never reads this map — an ordinary rejection (one whose blocking-clause signature does NOT repeat the preceding approach's) later in the same approach still raises it even after this budget is spent. The one rejection that does not raise it is the condition-3 match this budget just refused: the same rejection cannot be both a spent discretionary trigger and an unbudgeted condition-1 gate, or the shared budget would be a no-op whenever the flag is on, so that rejection goes to the archive/replan tail. A reviewer-raised gate (`raise_operator_gate`, requirement A.6) is a fresh per-verdict decision and overrides that, exactly as it overrides the flag itself. Disk-backed and survives a resume; a fresh approach (a replan, or the run's very first) always reads its own key back unset. Absent for a run that has never spent this gate |

`state` reaches the value `paused` for any of several reasons, and it is
never an operator pause: the engine pausing ITSELF on a reasoned
escalation edge (v0.18 requirement D.1/D.2, task 036) records `state:
"paused"` plus `pauseReason` (SPEC.md §5.2's own `pauseReason` field
table) so a host can see that the run is parked waiting for a human
before that reason's own answer command releases it, rather than
working. The one exception is `rejected_incurable` (task 006): its own
`_pause_and_escalate_incurable_rejection` takes the reason-less,
in-memory `self.pause()` (cure task cure-01-01-9), so `state` keeps
reading whatever it already was -- the hold is derived from
`events.jsonl` instead (`ralphctl status`'s own
`⏸ held on rejected_incurable escalation` marker,
`state.escalation_pause_markers`). `POST /pause` also records nothing at
all — it holds the loop at the next iteration boundary without changing
`state` or `phase` (see `POST /pause` below). There is no `paused`
**phase** in either case: `phase` keeps naming the phase that ran last.
The full `status.json`
field table is SPEC §5.

`usage.byPhase` breaks the same running totals down by iteration phase
(`planning`/`worker`/`verify`/`review`), `usage.byApproach` by approach
number (as a string key, since JSON object keys are always strings), and
`usage.byTier` (task 017, #68 half 2) by the task-annotated provisioning tier
(`trivial`/`normal`/`hard`) that dispatch actually resolved for that
iteration (only ever the task-scoped hats, worker and per-task verify --
planning/reflect/the run-level review always dispatch at their own fixed
rung and are never split out here). All three are accumulated alongside the
top-level totals on every iteration through the same `_merge_usage`
accumulator (no separate pricing path), so for any token/cost field
`sum(byPhase[*][field]) == sum(byApproach[*][field]) == sum(byTier[*][field])
== usage[field]`. A phase/approach/tier only appears once at least one
iteration for it has ended. `byTier` buckets carry one extra field no other
bucket does, `iterations`: a plain per-tier count of how many iterations
landed in that bucket, never merged as a token/cost field.

The cost fields carry the same unknown/partial honesty as an individual
iteration's (`GET /iterations` below). A bucket — the top-level total *and*
every `byPhase`/`byApproach`/`byTier` entry — describes how it mixes priced and
unpriced iterations with an optional `costStatus`:

| shape | meaning |
|-------|---------|
| no `costStatus` | every iteration in the bucket was priced by the provider (or billed nothing at all): `costUSD` is the whole cost |
| `costStatus: "partial"` | the bucket mixes reported prices with billed-but-unpriced tokens: `costUSD` is the **priced subtotal**, i.e. a lower bound |
| `costStatus: "unknown"` | tokens were billed and nothing in the bucket was ever priced: there is no `costUSD` (except a literal `0` contributed by no-traffic iterations) and no meaningful cost figure |
| `costStatus: "derived"` | nothing is unknown, but part of the money came from the host-side pricing map rather than the provider: `costDerivedUSD` holds that part (see below) |

`costStatus` is monotone: once a bucket contains unpriced traffic it can never
go back to fully known, and a later priced iteration only upgrades `unknown` to
`partial`. The `sum(byPhase[*][field]) == usage[field]` invariant still holds
for every field including `costUSD`, but when a bucket is `partial`/`unknown`
that sum is a sum of priced subtotals, not of costs — which is exactly why the
marker exists rather than a silent subset total. Renderers must show
`partial`/`unknown` cost as unavailable instead of `$0.0000` — every shipped
surface (`ralphctl status`, the `ralphctl logs` iteration footer, the hub's
usage panel) does so through the single formatter
`engine/state.format_cost`, which renders `unknown` as `unavailable` and
`partial` as `$<priced subtotal>+ (partial, rest unavailable)`. The hub gets
that string server-side as `usage.costDisplay` (plus `costDisplay` on each
`byPhase`/`byApproach`/`byTier` bucket) alongside the untouched raw fields.

#### Derived cost (optional host-side pricing map)

When a `pricing:` map is configured (see `docs/cli.md`, and the resolved rates
in `GET /config`), a bucket or iteration whose tokens the provider quoted **no**
price for also carries `costDerivedUSD`: USD computed from that map's
per-million rates. It is a separate field on purpose — it is never added into
`costUSD`, so `costUSD` always means "money the provider itself quoted" and no
consumer can silently mix the two. `format_cost` renders derived money with a
`~` and the word `derived` (`~$0.45 derived`, or `$0.56 + ~$0.45 derived` when
both kinds are present, or `$0.56 + ~$0.45 derived, partial (rest unavailable)`
when part of the cost is still unknown). With no map configured nothing
changes: an unpriced bucket stays `unknown`.

**A `~$… derived` figure is a lower bound whenever any hat's model has no rate
entry in either table** — the operator map or the built-in AWS one
(`priceTables` above). `PricingMap.rate_for`/`rate_for` in
`engine/pricing_aws.py` return `None` for an id neither table prices at all, so
that hat's tokens contribute nothing to `costDerivedUSD`: the tokens are still
counted, the money for them is simply absent from the sum.

**The observable a reader uses to tell, in two steps.** First, per hat, in
`GET /status`'s `usage.byPhase`: a phase key IS the hat's own name
(`planning`, `worker`, `verify`, `review`, `plan_review`, `verdict_gate`,
`steer`, `router`, `contract`), and an uncovered hat's bucket carries token
counters with **no `costDerivedUSD` of its own** while other buckets have one —
that is the tell that always answers, because it needs no marker to be set.
When that hat's iterations also recorded their tokens as billed-but-unpriced
(`costPriced: false`, `costDerived: false` — see `GET /iterations`),
`_merge_cost_status` marks the same gap: that bucket reads `costStatus:
"unknown"` (or `"partial"`, if it did collect some money), the run total reads
`"partial"`, and `format_cost` renders `~$… derived, partial (rest
unavailable)`. The absence of that marker is **not** evidence of coverage,
which is why the bucket-level check comes first: a total can read a clean
`costStatus: "derived"` and still understate the run.

Second, per model id, to confirm *which* id is uncovered: read the hat's
resolved id (`GET /iterations/<n>`'s `modelResolved`, the pinned `model`/`models:`
entry, or `model_for(hat)`) and look it up in each table. `GET /config`'s
`pricing.models` answers directly — it is the operator map's real id→rate
object. `priceTables.tables[*].models` **cannot** answer it: that field is an
integer *count* of priced ids (`len(RATES)`, `165` for the built-in table in
this build), not a list, so the built-in table's per-model coverage is checked
against the table itself — `pricing_aws.pricing_map().rate_for("<id>")`, `None`
for an id no exact key, alias hop or wildcard covers (`openai/gpt-6-astra-pro` →
`None`; `aigw-openai/openai.gpt-5.6-sol` → a `ModelRate`). A model no table
prices means every `~$… derived` total that includes that hat's traffic
undercounts it. Real example, as read live and recorded at
`docs/prds/README.md`'s `selfdev-v020-a-commit-is-progress` row: `~$648.75
derived` understates that run because `aigw-openai/openai.gpt-6-astra`, the
model behind its `plan_review`/`verdict_gate` hats, had no rate entry in
either table at the time — and that reading was taken with the run total's own
`costStatus` reading `derived`, the clean-looking case the two steps above
exist to catch.

`deadlineAt` is `startedAt + jobTimeoutS` **plus every second this run has
spent waiting out an infra outage** (`infraWaitTotalS`, the cumulative infra
backoff wait — see the infra budgets under `GET /config`). Waiting for a
broken LLM endpoint is not work the job's timeout should pay for: each backoff
wait extends the deadline by exactly the waited time and emits a
`deadline_extended` event, so `jobTimeoutS` keeps its plain meaning (time
available to the agent) and a 4-hour gateway outage cannot silently consume
half an 8-hour job. `infraWaitTotalS` is `0` for a run that never hit an
outage and survives `ralphctl resume` (the per-process deadline does not).

Task 016: an operator `POST /pause` is credited back the same way. The time
the loop actually spends parked at the pause wait (measured at the wait site
itself, never inside the pause/resume handlers) extends `deadlineAt` and
`self.deadline` identically to an infra wait, is accumulated in
`pausedTotalS` (published beside `infraWaitTotalS`, same `0`-when-never-
paused/survives-resume shape), and emits its own `deadline_extended` event
whose `reason` reads `"operator pause"` — never `"infra wait"` — so the two
credit sources stay distinguishable in `events.jsonl`.

A rate-limit wait (`rateLimitWait`, below) is credited back the same way: the
seconds actually waited extend `deadlineAt`, accumulate in
`rateLimitWaitTotalS` (separate from `infraWaitTotalS`; survives `ralphctl
resume`; absent until the first wait) and emit a `deadline_extended` event
whose `reason` reads `"rate limit wait"`.

#### `approach` and `maxApproaches`

`approach` is the current approach number (1-based) and `maxApproaches` its
denominator — the run's `max_approaches` budget, the same number `GET /config`
reports as `budgets.maxApproaches`. It is written by the engine with the very
first `status.json` write (state `starting`), so every surface can render
`approach 2/3` from one status read and no consumer has to make a second call
to `/config` to find the denominator. Both numbers survive to the terminal
snapshot, so a finished run still says which approach it ended on and out of
how many it was allowed.

A run dir written by a pre-v0.6 engine has no `maxApproaches` in its
`status.json`; `GET /status` then reports it as an explicit `null` (never a
missing key), which means *no denominator is known* — renderers show a bare
`2` rather than inventing `2/?`. `approach` itself is absent/null until the
loop starts, in which case there is nothing to render at all.

#### `model` and `modelRaw`

The model this run is actually talking to, as **pi resolved it** — not the ref
the operator asked for. pi reports the model it used on every assistant message
(a `provider` plus a provider-side `model` id), and the engine records both
halves of that through one helper (`ralphd.engine.state.model_ids`):

| field | meaning |
|-------|---------|
| `model` | the pi-style `provider/model` ref — the same string `--model` takes, and the string the pricing tables match against |
| `modelRaw` | the provider-side (gateway) id, populated **only** when it differs from `model`, i.e. when the provider prefix was added; `null` otherwise, so one string is never presented twice as two facts |

Why it is the *observed* id and not the configured one: when the operator pins
nothing, `job.yaml`'s `model` is `null` and pi picks its own default — the case
where run state used to report `model: null` while every message on the wire
named a concrete id, leaving "why is this route unpriced" unanswerable from run
state (#14). Both fields are `null` until an iteration observes a model, and an
iteration that observes none (an instant startup failure, an in-band error with
zero traffic) never overwrites an id already recorded. A pre-v0.6 run dir has
neither key; `GET /status` reports explicit `null`s, never a missing key and
never the live config's ref guessed in.

The value is the **latest** observation, which is what "which model is this run
using" means for a run whose hats may use different models (the per-hat
`models`/`model_overrides` table).
Per-iteration ids stay in `iterations/NNNN/meta.json`, where `model` is the ref
that was requested (possibly `null`) and `modelResolved`/`modelRaw` are what
actually answered.

#### `image` and the job-image record

Which job image this run is running — and, `imageId`, exactly *which* image that
reference resolved to. A process inside a container cannot see the image it was
created from (nothing in `/proc` names it, and the docker socket is not there
unless the operator mounted it), so this is the **host's record**: `ralphctl
start`/`resume` write it into the run dir's `host.json` and the engine reads it
back per request (a `resume` rewrites it, so it is never cached at startup).

| field | meaning |
|-------|---------|
| `image` | the reference the container was started with (`ralphd:<hash>`, `ralphd-derived:<hash>`, or whatever was pinned) |
| `imageId` | the daemon's content id for the image the container **actually got**, observed from the container itself rather than assumed from the reference — so a pinned tag the daemon pulled, or a tag that moves tomorrow, is still identified |
| `imageSource` | how the reference was arrived at: `pinned` (`--image`/`RALPHD_IMAGE`/template/registry), `cached` or `built` (a content-hashed tag looked up or built), `unhashable` (no source tree to hash), `recorded` (a `resume` reproducing what run state recorded), `default` (a pre-v0.6 run dir that recorded neither an image nor a recipe) |
| `imageHash` | the content hash the image was tagged by; `null` whenever the reference was not derived from content, which means *staleness is unknowable* rather than *up to date* |
| `imageBase` | the base image this one was derived from, for a derived image; `null` for the default image or a pin |
| `imageDockerfile` | the operator Dockerfile that base was built from (`--dockerfile`), when there was one |

All six are explicit `null`s for a run dir whose `host.json` records nothing
(pre-v0.6, or a `host.json` written before the container's id could be
observed) — absence is never a third case. `ralphctl status` renders the pair as
`image: ralphd:9f2c1a4b7d80  (id 0f1e2d3c4b5a)`, and `ralphctl resume` prefers
this record over re-deriving a tag from possibly-changed sources, so a resume
continues on the image the run started on instead of silently swapping the
engine mid-run (see docs/cli.md).

#### `health` and `infraWait`

`state`'s values are
`starting|running|paused|finishing|timed_out|succeeded|failed|aborted` (task 012, #56,
adds `finishing`; task 036, #142, adds `paused` for the engine's own
`rejected_last_approach` escalation hold — never for an operator `POST /pause`;
v0.24 task 008, requirement B.1, #169 adds `timed_out` — the iteration/wall-clock
budget exhausted without an operator abort and without review ever rendering a
verdict, `verdict: null`, resume-eligible and never a member of the terminal set)
— there is **no** `degraded` state value, because that would break every consumer's
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
run-detail card. A wait can be cut short with `POST /retry`. The whole model
(fault taxonomy, outage budget, deadline extension, retry-now, the
instant-failure fail-fast carve-out) is described in `docs/architecture.md`
§10 “Resilience: transient endpoint outages”.

#### `rateLimitWait`

A `claude-code` iteration the provider rate-limited (`faultClass:
"rate_limit"`) is not an outage: `health` stays `"ok"` and `infraWait` stays
`null`. The loop waits for the window to reset and re-runs the same work, and
publishes the wait in its own field, `null` whenever no such wait is in
progress:

```json
"rateLimitWait": {
  "type": "five_hour",                    // the provider's rateLimitType (null if not reported)
  "resetsAt": "2026-10-02T15:00:00Z",     // when the window resets (or the 15-minute re-probe)
  "since": "2026-10-02T14:12:40Z"         // when the wait started
}
```

The wait lasts until `resetsAt` plus 0–60 s of jitter, does not consume an
iteration (the attempt is refunded under `iterationsRefunded.infra`) and does
not touch the outage budget. It is surfaced by `ralphctl status` and `ralphctl
fault` as `waiting:   rate limit (<type>) until <local time>`, and by the hub
run-detail card with a "retry now" button. `POST /retry` wakes it.

#### `reflect`

Absent from `status.json` (and `null` in `GET /status`) unless the run was
started with `reflect: true` and the post-terminal reflect iteration has
finished. Then:

```json
"reflect": {"ok": false, "error": "Connection error.",
            "errorClass": "fatal", "endedAt": "2026-08-18T09:31:20Z"}
```

`ok: true` means `artifacts/reflection/report.md` exists; otherwise `error`
carries the agent's error text (or `the reflect iteration wrote no
artifacts/reflection/report.md` when it exited cleanly having written
nothing) and `artifacts/reflection/FAILED.md` names the same error on disk.
`errorClass` (present only on a failure) is one of two spellings that are
deliberately never the same word (task 014, #87): `"degraded"` when the
iteration ran to a clean exit but left no valid deliverable (no report, an
unparseable/invalid contract, or a contract disclaiming `report_written`) --
issue #87's own incident, a deadline-fired reflect returning an unparseable
contract payload -- and `"fatal"` when the iteration itself never produced a
trustworthy attempt (never ran to completion, interrupted, timed out, an
in-band error, a nonzero exit). Neither class changes `state`/`verdict`/
`reason` below: no reflect failure of either class can fail a run, by
construction (`_finish()`/`_promote_finishing_state()`, `loop.py`) --
`"fatal"` is reserved so a future change that ever wants a reflect failure to
be able to fail a run cannot do so by quietly reusing `"degraded"`.
A successful attempt **removes** any `FAILED.md` an earlier attempt (of an
earlier episode) left behind, emitting `reflect_tombstone_cleared`, so a
fail → resume → succeed run never advertises a reflection failure it does not
have — and `artifacts ls` stops offering the `reflect-failed` alias with it.
A failed reflection **never** changes the run's `state`, `verdict` or
`reason` — the job is already over when reflect runs. Surfaced by
`ralphctl status` and the hub run-detail card, and emitted as a
`reflect_done` event.

A third shape says the phase produced **no verdict at all**, and that this was
the engine's own doing: a `SIGTERM`/`SIGINT` reached the engine (`ralphctl
stop`, a raw `docker stop`, the container runtime taking the host down), so the
child killer has already fired and `SIGKILL` is on its way.

```json
"reflect": {"ok": null, "attempted": false, "error": null,
            "skipped": "signal 15 ended the engine before the reflect phase could start, so no reflect iteration was attempted",
            "endedAt": "2026-08-22T09:31:20Z"}
```

No iteration is spawned and **no `artifacts/reflection/FAILED.md` is written** —
the tombstone means "the reflection was tried and failed", which here is false.
An earlier attempt's tombstone is equally left alone on this path: the signal
does not make that failure untrue, and only a report contradicts it.
`attempted: true` with the same `ok: null` is the narrower case where the signal
arrived *during* the attempt, so what the iteration returned describes the
teardown rather than the reflection. `ok: null` keeps every consumer that gates
on `ok === false` (this CLI, the hub) from reporting a failure that did not
happen; both cases emit a `reflect_skipped` event and print their own
`reflection: not attempted (…)` / `not completed (…)` line in `ralphctl status`.

#### `termination`

Absent from `status.json` (and `null` in `GET /status`) unless something told
this run to stop. Then it says **which class** of termination it was:

```json
"termination": {
  "class": "self-inflicted", "action": "abort", "signal": "15",
  "at": "2026-08-18T09:31:20Z",
  "reason": "self-inflicted termination: signal 15 ended the engine with no operator abort recorded, ...",
  "evidence": {"iteration": 37, "tool": "bash",
               "args": "{\"command\": \"pkill -f ralphd-engine\"}",
               "transcript": "iterations/0037/output.jsonl"}
}
```

`class` is `"operator"` when the abort was asked for through `POST /abort`
(which is what `ralphctl abort`, `ralphctl stop` and the hub's abort button all
do), and `"self-inflicted"` when a `SIGTERM`/`SIGINT` reached the engine with no
such request behind it — an iteration killing its own supervisor from inside the
container being the case this exists for. A process cannot see who signalled it,
so the class is decided by *attribution*: an abort somebody claimed versus a
signal nobody did. `signal` is the signal number as a string, or `null` for an
abort that arrived through the API.

`evidence` is the last tool call the run's transcripts recorded before the
signal, read back out of `iterations/NNNN/output.jsonl` — `null` when there is
none (killed before its first tool call) and, for an operator abort, nothing
worth showing. Arguments are truncated and pass through the same secret
scrubbing as `events.jsonl`.

The class is load-bearing rather than cosmetic: host-side auto-resume refuses to
resurrect an **operator**-class termination (`ralphctl doctor --fix`, see
[cli.md](cli.md)) and resumes a self-inflicted one like any other crashed
container. The same record, with the same fields, is written into the run dir as
`operator-termination.json` before the loop unwinds, so a container that dies
mid-abort still leaves the class on disk; a marker with no `class` field (any
run dir written before v0.7) reads as `operator`. `ralphctl status` prints a
`termination:` line for the self-inflicted class only.

`termination` describes the CURRENT episode only (task 005, #173): a resume
that opens a fresh episode folds the superseded episode's `termination` (if
any) into the `previousEndings` entry that already carries its `endedAt`/
`reason`/`verdict`, archives `operator-termination.json` out of the run dir
root into `previous-endings/NN-operator-termination.json`, and REMOVES the
top-level key (not `termination: null` — presence is how a consumer asks
"was this run told to stop?") — so a run stopped two episodes ago and since
resumed into a clean success carries no `termination` key at all, rather than
reading as an aborted run.

### `GET /tasks`
Full `tasks.json`, plus the same `tasksStale`/`tasksSource` pair `GET /status`
carries and the derived `taskFailureKinds` map (both appended after the file's
own keys, so a plan key of any of those names cannot forge them).

#### Both meanings of a `failed` task (`taskFailureKinds`, task 025, issue #33)

`failed` means either *a verifier judged this requirement unmet* or *the engine
spent this task's validation rounds*, told apart by the `failureKind` label on
the record (SPEC §5.3). Both endpoints publish which, without inventing a sixth
status:

| field | endpoint | shape |
|---|---|---|
| `taskFailureKinds` | `GET /tasks` | `{"014": "validation-exhausted"}` — one entry per failed task, **absent entirely** when nothing failed (for a derived map, empty and absent state the same fact) |
| `tasks.failedValidationExhausted` / `tasks.failedRequirementUnmet` | `GET /status` | the same answer in aggregate, present only for the kinds actually in the plan |

The kind counts are **sub-counts of `failed`**, not statuses: the status keys
still sum to `total` and the two kind keys sum to `failed`, so a consumer can
keep tallying exactly as before. Both are **derived** through
`ralphd.engine.state.task_failure_kind`, so a `tasks.json` written before v0.7
(no `failureKind` key at all) answers too — at or past three recorded
`validationAttempts` it reads as `validation-exhausted`, otherwise as
`requirement-unmet` — and an unrecognised label can never produce a third
meaning.

#### Stale task reads (`tasksStale`/`tasksSource`)

`tasks.json` is written by the *agent*, not the engine, so a request can land
inside a rewrite. Both endpoints read it through the one hardened reader
(`ralphd.engine.state.read_tasks_doc`, see docs/architecture.md §3): a
mid-write file yields the **last payload that did parse**, so the `/tasks`
list and the `/status` `tasks` counts never collapse to empty/`total: 0` for a
file that exists and previously parsed. These two fields say which happened:

| `tasksSource` | `tasksStale` | meaning |
|---|---|---|
| `absent` | `false` | no `tasks.json` yet — the empty plan is the truth |
| `file` | `false` | parsed off disk (an empty `tasks` list here is also the truth) |
| `last-good` | `true` | unparseable right now; the previous payload is being served |
| `unreadable` | `true` | unparseable and no last-good exists — `total: 0` here is ignorance, not a plan with no tasks, and renderers must label it rather than print `0` |

`tasksStale` is always present (`false` on the happy path), so its absence only
ever means "written by a pre-0.6 engine", never "fresh". The counts stay pure
counts — the flag is deliberately a sibling of `tasks`, not a key inside it, so
summarisers that iterate the counts (`ralphctl status`) cannot mistake it for a
task status.

### `GET /prd`
`text/markdown` — the PRD in effect (composite PRD when approach ≥ 2).
`GET /prd?original=true` returns the original regardless. `404` when the run
dir holds no PRD at all. Which file that is (`composite-prd.md` when present,
else `prd.md`) is decided by one shared helper,
`ralphd.engine.state.prd_path`, which the hub's host-side on-disk fallback
(`GET /api/runs/<id>/prd`, behind the run-detail **view PRD** dialog — see
docs/cli.md) uses too, so a live and an on-disk read of the same run dir can
never disagree about which text is "the PRD".

### `GET /notes`
`text/markdown` — current `notes.md` handoff notes.

### `GET /iterations`
Array of every iteration's `meta.json` (number, phase, approach, model, timestamps,
exit code, sentinel seen, token usage, steering delivered/consumed).

`steeringDelivered` names the steering files this iteration's prompt carried;
`steeringConsumed` names the ones its outcome actually marked applied. They are
equal for a clean iteration and `steeringConsumed` is empty for one that failed,
was interrupted or timed out — those notes stay pending for the next actionable
iteration (issue #34: consumption is earned by a finished iteration, not by
delivery, so a note is never recorded as applied when nothing acted on it).

Each entry carries three model fields, which answer different questions:
`model` is the ref the engine *requested* (`null` when nothing was pinned and pi
chose), while `modelResolved` and `modelRaw` are what pi reported actually
answering — see `model`/`modelRaw` under `GET /status` above.

Each finished iteration also carries `faultClass` — the engine's own fault verdict
for that iteration (`src/ralphd/engine/faults.py:classify_fault`), the same verdict
the infra-retry wrapper acts on:

| `faultClass` | meaning |
|--------------|---------|
| `null` | not a failure: clean exit, no error recorded, not interrupted/timed out |
| `"infra"` | the LLM endpoint/provider/network broke (no traffic within the startup window, a recognized infra error signature, or a bare `aborted` with a clean exit and no abort recorded for the run, at any duration — a stream the provider hung up on) — the attempt is retried and refunded, never charged to the iteration budget |
| `"work"` | the agent really ran (LLM traffic observed) and then failed on its own — including a bare `aborted` that arrived more than two minutes in, which is no longer distinguishable from an agent that worked and then aborted — or an abort/interrupt was recorded for the run — an operator's, or the engine giving up on its own — which is never retried as an outage (the *explanation* surfaces name which of the two only when it can be established; the class cannot tell them apart, issue #49) |
| `"signal"` | the agent reached the model and was then ended by a signal, with no abort recorded for this run (an OOM kill, a stray `pkill`, a `docker stop` of its process group) — it was terminated before it could fail, so it is neither the agent's work failure nor an endpoint outage, and it is **not** retried (a signal usually means something outside the run wants it to stop) |
| `"rate_limit"` | the provider reported its rate-limit window exhausted, in structured form (a `claude-code` iteration only: a fresh rate-limit report with `status: "rejected"`, an error type `rate_limit`, or HTTP 429 with a rate-limit type) — the attempt is refunded, the loop waits for the window to reset (`rateLimitWait` under `GET /status`) and re-runs the same work. A pi text `429` stays `"infra"` |
| `"refused"` | the provider refused the call in a way waiting does not fix, from either harness: an HTTP 400, 401, 403 or 404 whose error names `invalid_request_error`, `authentication_error`/`authentication_failed`, `permission_error`, `not_found_error`/`model_not_found` or the subscription's "Third-party apps now draw from your extra usage" refusal; or the `claude-code` harness refusing to start (`claude_code_version_too_old`, `claude_code_cli_not_found`, `claude_code_runs_as_root`). A 429 or a 5xx is never `"refused"`. The attempt is refunded and **not** retried, the outage budget is untouched, and the run pauses with `pauseReason: "operator_input_needed"` and `pauseDetail.reason: "provider_refused"` (under `GET /status`); `POST /resume` re-runs the same work |

When the runner reported structured provider evidence, `meta.json` also
carries it: `rateLimit` (the last rate-limit report: `status`, `resetsAt`,
`rateLimitType`, `unifiedWindows`, …) with `rateLimitFresh` (whether it arrived
during this iteration), `httpStatus` and `errorType`. Each is absent when not
reported.

The field is absent only while an iteration is still in flight (before its
`endedAt` is written). The signature families behind `"infra"`, and the
`aborted`/operator-abort carve-out, are documented in
`docs/architecture.md` §10.1.

An iteration that the *engine* could not carry out is still one of these
records, not a missing one: whatever the agent subprocess or its event pump
throws is caught at the loop's iteration containment boundary and written out as
an ordinary failed iteration whose `error` starts `engine iteration failure:`
(with `exitCode: null`), so it has an `endedAt`, a `faultClass` and its usage
like any other. A timed-out iteration is the same story with `timedOut: true`:
it costs one iteration and never the run (issue #28, `docs/architecture.md`
§9).

An iteration's `usage` records cost only when the provider actually quoted a
price. `usage.costPriced` is the marker:

| shape | meaning |
|-------|---------|
| `costUSD: 0.42`, `costPriced: true` | provider-reported price |
| no `costUSD`, `costPriced: false` | tokens were billed and **no** price was reported — cost is *unknown*, not $0 |
| `costUSD` present, `costPriced: false` | mixed: the value is the priced subtotal only, so treat it as partial |
| `costUSD: 0`, no `costPriced` | nothing was billed at all (e.g. pi's zero-filled `usage` on an in-band error) |
| no `costUSD`, `costPriced: false`, `costZeroQuoted: true` | the provider quoted **exactly 0** for billable tokens — an *implausible zero*, treated as unknown (v0.6, see below) |
| `costUSD: 0.0`, `costFree: true`, `costBasis: "subscription"`, `listEquivalentUSD: 0.40` | a `claude-code` iteration: billed nothing, with the SDK's list price kept apart and never added to `costUSD`; buckets add `costBasis` (`subscription` or `mixed`) and sum `listEquivalentUSD` |
| `costUSD: 0.0`, `costPriced: true`, `costFree: true` | the route is **declared** free in `pricing.free`, so this $0 is real and renders `$0.00` |
| `costDerivedUSD: 0.31`, `costDerived: true` | every unpriced message was covered by the host-side pricing map — cost is *derived*, and kept out of `costUSD` |
| `costDerived: false` | at least one unpriced message had no rate in the map (or no map is configured), so part of the cost stays unknown |

How a run total / `byPhase` / `byApproach` bucket summarises a mix of priced and
unpriced iterations is the `costStatus` contract under `GET /status` above.

#### Implausible zero quotes (v0.6)

A quoted cost of **exactly 0 alongside non-zero billable tokens** is not a
price: pi zero-fills its `cost` block when the resolved model definition
carries no rates, so a gateway can report `costUSD: 0` for half a million
tokens. Such a
zero is classified `unknown` on every surface — never `costPriced: true`, never
rendered `$0.00` — and is derivable from the host-side pricing map exactly like
an absent price.

The classification is applied **on read** as well as on write, so a run dir
written by a pre-v0.6 engine (an `int` `0` with no `costStatus`) is reported
honestly by `GET /status`, `GET /iterations/{n}`, `ralphctl status` and the hub.

Two zeros stay real, and neither is inferred from the zero itself: nothing was
billed (the sentinel row above), or the operator **declared** the route free in
`pricing.free` (`costFree: true`, carried into the run/`byPhase`/`byApproach`
rollups so a reader cannot mistake it for the anomaly).

### `GET /iterations/{n}`
One iteration's `meta.json`.

A slot can have been attempted twice: an engine killed mid-iteration leaves its
directory with no `endedAt`, and the resumed engine reuses that number. The dead
attempt is not overwritten — its `prompt.md`, transcript and partial `meta.json`
are moved to `iterations/{n}/attempts/NN/` first (one `iteration.attempt_archived`
event, oldest attempt `01`), so this route always returns the *newest* attempt
while the crashed one stays readable on disk. The iteration-detail payload behind
`ralphctl iteration --json` and the hub dialog counts them as `archivedAttempts`
(`0` for the normal single-attempt slot); nothing counts an archived attempt as an
iteration of its own, so `iterationsUsed` and the transcript merge are unaffected.

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
| `state` | lifecycle transition: `state` is `running` (emitted by every engine process as it starts the job loop, resume included -- except a process that resumes a run dir already recorded `finishing`, task 012/#56 below, which has no job loop left to start and promotes straight to the terminal event), `finishing` (task 012, #56: the verdict is settled but the post-terminal `reflect` iteration has not run yet -- see status.json's `pendingTerminalState`), the non-terminal `timed_out` hold (v0.24 task 010, requirement B.3, #169: the deadline/budget hold froze on the spot, `verdict: null`, resume-eligible, skips the `finishing`/reflect dance entirely) or the terminal `succeeded`/`failed`/`aborted`; the startup event also carries `resumed` (`true` when this process picked up a run dir that already held recorded work) |
| `phase` | phase entered (planning/worker/verify/review), approach number |
| `iteration.start` / `iteration.end` | number, phase, model / exit, sentinel, error, `faultClass` (`null` \| `"infra"` \| `"work"` \| `"signal"` \| `"rate_limit"` \| `"refused"`, identical to the iteration's `meta.json` — see `GET /iterations`) |
| `iteration.attempt_archived` | a reused iteration number's earlier, crashed attempt was archived before the new one wrote: `number`, `attempt` (`1` for the oldest), `path` (`iterations/NNNN/attempts/NN`), `files` |
| `task` | task id + old/new status — emitted live while a worker iteration is still running (polled every ~0.25s against `tasks.json`), not only after the iteration ends, so `pending -> in-progress` is observable in real time |
| `steering.received` / `steering.consumed` | steering file name; `steering.consumed` also carries the `iteration` and is emitted only after that iteration finished cleanly (never twice for the same file) |
| `signal` | COMPLETE / VERIFIED / task-verified detected |
| `log` | engine-level notices (timeouts, retries, failures) |
| `infra_retry` | an infra-classified attempt is being retried: phase, attempt, error, `backoffS` (`null` when giving up), `waitedS`, `budgetS` |
| `infra_wait` | a backoff wait started: the full `infraWait` payload (`since`, `attempt`, `error`, `phase`, `nextAttemptAt`, `waitedS`, `budgetS`, `remainingS`) plus `backoffS`; the run is now `health: "degraded"` |
| `infra_recovered` | an iteration reached the model again: `health: "ok"`, `infraWaitTotalS` — the outage episode is over and `infraWait` is back to `null` |
| `infra_retry_now` | an operator woke a backoff wait via `POST /retry`: phase, attempt, error, `source: "operator"`; for a rate-limit wait: phase, `rateLimitWait` (`type`, `resetsAt`, `since`), `source` |
| `rate_limit_wait_started` | a rate-limit wait started: phase, `rateLimitType`, `resetsAt`, `since`, `waitS` (the planned wait), `resetsKnown`, `reason` (`"rate_limit"` for a rate-limited iteration, `"preempt"` for a wait taken before starting a `claude-code` iteration whose window is nearly spent, which also carries `utilization`); the notifier is told |
| `rate_limit_wait_ended` | the rate-limit wait ended: phase, `rateLimitType`, `waitedS`, `wokenBy` (`"reset"` \| `"retry"` \| `"abort"`), `reason`; the notifier is told |
| `rate_limit_ceiling_reached` | a rate-limit wait would have taken the run past `rate_limit_max_wait_s`, so it did not start and the run paused for the operator (`pauseDetail.reason: "rate_limit_ceiling"`): phase, `rateLimitType`, `resetsAt`, `waitedS` (waited since the last ceiling unpause), `maxWaitS`, `plannedS`, `reason` (`"rate_limit"` \| `"preempt"`); the notifier is told, naming the reset in local time. Never an `operator_escalation` |
| `provider_refused` | an iteration was classified `"refused"` and the run paused for the operator: phase, `iteration`, `family`, `description`, `match` (the matched text), `httpStatus`, `errorType`, `harness`, `message` (the provider's error, scrubbed); the notifier is told, quoting that message. Never an `operator_escalation` |
| `reflect_infra_delay` | the job ended on an infra-shaped failure, so the post-terminal `reflect` iteration waits before its first attempt instead of firing into the same dead endpoint: `delayS`, `error`, `budgetS` (reflect's own, capped, outage budget). Followed by an ordinary `infra_wait` with `attempt: 0` |
| `reflect_done` | the post-terminal `reflect` iteration finished: `ok`, plus `error`/`errorClass` when it failed (same verdict as status.json's `reflect`) |
| `reflect_skipped` | the post-terminal `reflect` phase produced no verdict because a signal was already taking the engine down: `attempted` (false = never started, true = cut short mid-attempt), `signal`, `reason`. No `FAILED.md` is written on this path — see status.json's `reflect` |
| `reflect_tombstone_cleared` | a successful `reflect` attempt removed a stale `reflection/FAILED.md` written by an earlier one: `path`. The alias `reflect-failed` disappears from `artifacts ls` with the file |
| `deadline_extended` | the job deadline moved out after an infra wait, an operator pause OR a rate-limit wait: `waitedS`, new `deadlineAt`, `reason` (`"infra wait"` — also phase/attempt/`infraWaitTotalS` — or `"operator pause"` — also `pausedTotalS` — or `"rate limit wait"` — also phase/`rateLimitWaitTotalS`) |
| `budget_changed` | the iteration budget was changed in flight via `PATCH /config/budget`: `field: "iterations"` (also `"maxApproaches"`/`"iterationTimeoutS"`/`"jobDeadlineEpochS"`/`"curePassCeiling"`/`"rateLimitMaxWaitS"` — task 003/#94 (a) and task 010/v0.27 requirement C.3 widen the knob, never the event's own shape), `previous`, the new value under the field's own key (`iterations`/`maxApproaches`/`iterationTimeoutS`/`jobDeadlineEpochS`/`curePassCeiling`/`rateLimitMaxWaitS`), `delta`, `iterationsUsed` (`iterations` field only), `source: "api"` |
| `notification.sent` / `notification.failed` | the engine-owned `notify` hat tool (or the engine's own operator-escalation notifier) POSTed to the configured notifier URL: `endpoint`, `textSha256`, `httpStatus`. A successful send (2xx) emits `notification.sent`; an unconfigured URL, a connection error, or a non-2xx status all emit the distinct `notification.failed` instead -- never `notification.sent` for a send that did not actually succeed (requirement B.1, #197). v0.23 requirement A.7 (#224, task 017): a failing POST is retried exactly ONCE, immediately, with no backoff, before either event is recorded -- `httpStatus` on the event is always the LAST attempt's own status, and exactly one of the two events is ever recorded per call (two failures in a row still record one `notification.failed`, not two). Task cure-01-01-3 (v0.26 requirement A.4's own DoD 1): this per-call retry-once is the one, non-escalation caller's behaviour; an operator-escalation delivery attempt (below) disables it (`max_attempts=1`) so its own outer retries are the ONLY retrying that happens, and each escalation attempt still emits exactly one of these two events, one real POST per event |
| `notification.escalation_undelivered` | v0.26 requirement A.4 (task 004, #269), tightened by task cure-01-01-3 (the same requirement's DoD 1, "at most 3 escalation delivery attempts"): an operator-escalation notification (`_pause_and_escalate_*`/`_pause_for_commit_secret_hit`) never reached the notifier after up to 3 delivery attempts with bounded backoff between them (60s, then 120s -- 180s total) -- and those 3 attempts are also the TOTAL count of real notifier HTTP POSTs made, never 3 attempts each silently paying for a second internal retry: `reason` (the escalating pause's own `operator_escalation.reason` string -- also its persisted `pauseReason`, except `rejected_incurable`, whose reason-less in-memory pause never persists one). Each of the up-to-3 attempts still records its own honest `notification.sent`/`notification.failed` event above, unchanged; this is the ADDITIONAL, distinct event marking that the escalation itself was never delivered at all -- never emitted when any attempt succeeds |
| `task.criteria_repaired` | one of the two lexical plan-ingestion lints had already computed a lossless, lint-clean rewrite of a criteria and the engine applied it instead of refusing the whole proposal and re-asking the hat (#209): `taskId`, `oldCriteria`, `newCriteria`, `lints` (the lint names applied), `source` (`"plan-ingestion"`, which also carries `hat`/`attempt`, or `"cure-insertion"` when the text was a curable finding's `cured_when` becoming a cure task's criteria) |
| `petition.remedy_task_inserted` | a REFUTED petition adjudication whose remedy is WORK rather than advice (`remedy_type` `substance`) became one cure task, on the same path a curable rejection's cure task takes (#209): `approach`, `obligation`, `taskId`, `finding` (the synthesized blocking-finding record). Emitted from both the mid-run adjudication episode AND an ordinary run-ending review pass alike (v0.23 requirement D.2, task 019, #220), and it spends no cure-pass budget |
| `task.dependency_repointed` | an existing task's whole `dependsOn` list was replaced (#209, task 003, task 004): `taskId`, `removed`, `added`, `dependsOn` (the new full list), `reason`, `source` (one of `"worker-repoint"` -- a worker's optional `proposed_task_updates` report re-pointed a task the harness itself derives as blocked off a `failed`/`skipped` ancestor -- or `"cure-insertion"` -- either the engine's own `state._repoint_dependents_onto_cure_task` re-pointing a dependent of a `failed` task onto the cure task inserted for it, or `petitions.apply_confirmed_petition_adjudications` re-pointing a dependent off a `petitioned` task a CONFIRMED adjudication just moved to `skipped`). The accepted-write sibling of `task.dependency_edge_rejected`; for `"worker-repoint"` the same `reason` is also appended to that task's own `notes`, so the re-point is legible from the task record as well as from the event stream |
| `task.pre_cure_head_recorded` | `state.insert_cure_tasks`'s `invalidates` walk just re-opened a plan's own lane-evidence RECORD task (v0.23 requirement B.2, task 006, #221): `taskId` (the record task's own id), `sha` (the workspace HEAD the cure pass that re-opened it started at -- `"<sha>"|null`, the SAME value just written into `status.json`'s `preCureHeads` map under the same `taskId` key), `priorStatus` (cure-01-05, PRD requirement B.3/B.4, F05, #221 -- the record's own ACTUAL status immediately before this reopen, the SAME value just written into `status.json`'s `preCureStatuses` map under the same `taskId` key), `approach`, `cureTaskId` (the cure task this re-open's edge now chains to). Never emitted for a verification or freshly-appended retake task -- only the record itself. Read back by `state.records_pending_reconciliation` (v0.23 requirement B.4, task 008, #221), keyed on this event's own `cureTaskId`, once that same cure task reaches `completed`/`validated` — and, when several cure tasks of one pass re-opened the same record against the same `sha`, only once every one of those owners has |
| `cure.sweep_reopened` | PRD v0.29 requirement B.2/B.3 (#280, task 003): `state.insert_cure_tasks` just re-opened a `tasks[].sweep: true` task -- `validated`/`completed` back to `pending` -- because this call inserted at least one cure task: `taskId` (the sweep task's own id), `oldStatus` (the status it held immediately before this reopen), `cureTaskIds` (every cure task id this pass inserted, in finding order -- not only the one belonging to whichever finding's `invalidates` happened to name the sweep's own path, since the sweep's `dependsOn` edge is added for every cure task this pass inserted). Its pre-cure head is persisted into `status.json`'s `preCureHeads`/`preCureStatuses` maps and emitted on `task.pre_cure_head_recorded` (one entry per cure task id in `cureTaskIds`), the exact same path the `invalidates` walk already uses. A sweep this SAME call's `invalidates` walk already re-opened is re-opened once, not twice, and emits exactly one of these events too (`oldStatus` is the status it held before the call began; its pre-cure head is the walk's own `task.pre_cure_head_recorded`, not a second write). Never emitted for a sweep that was not `validated`/`completed` to begin with (it still gains the `dependsOn` edge on every cure task this pass inserted, silently) |
| `contract.recovered` | PRD v0.29 requirement C.1 (#281, task 005): `_step_worker`'s last non-blank line was not a JSON **object carrying a `verdict` key** at all (never merely schema-invalid -- a present-but-invalid `verdict`, e.g. `{"verdict": "done"}` or `{"verdict": null}` (key presence decides, never the value), skips this path entirely and takes the pre-existing generic schema ladder instead, which fires `contract.violation`/`contract.agent_fault`, not this event, even if ITS own recovery happens to land on the same fenced block), and `_extract_validated_fenced_payload` found and validated a fenced ```json block elsewhere in the reply (the iteration-22 shape): `hat` (always `"worker"` from this call site), `source` (always `"fenced"`), `taskId` (the task assigned this iteration, if any). The recovered payload is used exactly as if it had been the last line, with no model call at all -- the same zero-call scan `_ask_contract_fix` already runs before any re-ask |
| `worker.verdict_absent` | PRD v0.29 requirement C.3 (#281, task 005): the worker reply's last non-blank line was not a JSON **object** at all -- plain prose, a bare JSON scalar/list, or the retired `<promise>COMPLETE</promise>` whole-plan marker, which drives no decision here either: `taskId` (the task assigned this iteration, if any). Never fired when the last line IS a JSON object, with or without a usable `verdict` -- a verdict-less object enters the ladder instead (C.2), and this event never fires for it even on that ladder's own exhaustion (`contract.agent_fault` is that path's own record). No model call, no ladder, and the reply text itself is never carried on this event |
| `task.verify_skipped_exhausted` | a task reached the verify dispatch in `awaiting-validation` with `validationAttempts >= VALIDATION_ATTEMPT_LIMIT`: `taskId`, `validationAttempts`, `limit`. No verifier runs; the task moves to `failed` with `failureKind: validation-exhausted`, so it is routed to review instead of stalling the worker loop |
| `task.repoll_backoff` | PRD v0.29 requirement A.1-A.4 (#279, task 001): a **re-poll** -- a worker iteration on its assigned task that ends `no_verdict` with a `progress_claim` reading `corroborated: false` and `deltaSize: 0` -- is the SECOND or later one in a row on that task: `taskId`, `consecutiveCount` (the ladder position, `2`/`3`/`4`/...), `backoffSeconds` (`min(600 * 2 ** (consecutiveCount - 2), 1800)` -- 600/1200/1800, capped), `deferredUntil`. The engine defers the task back to `pending` (`deferredUntil`/`deferredReason` naming the backoff and the count) instead of letting the ordinary four-witness stagnation formula see this pass, so witness (d) (`tool_activity`) never resets on a re-poll's own tool calls and the SECOND-through-later re-polls never touch the stagnation counter either -- only the FIRST re-poll of a run of them is counted by the ordinary formula. A corroborated claim on the task resets the consecutive count, so the next re-poll after it does not back off |
| `task.un_reopened` | `state.apply_task_status_change` took the declared un-reopen edge (v0.23 requirement B.3, task 007, #221): `taskId`, `previous` (always `"pending"`), `status` (the restored terminal status -- since cure-01-05 (PRD requirement B.3/B.4, F05, #221) and task 001 (F04), whichever of `"completed"`/`"validated"`/`"failed"`/`"skipped"`/`"superseded"`/`"petitioned"` the record genuinely held before the reopen, read from `preCureStatuses` rather than guessed from `cfg.vigilant`; all six are declared out of `pending` in BOTH variants, so this event is emitted for the same restore whichever verification mode is in force), `source` (always `"cure-reconciliation"`, naming `LoopSupervisor._reconcile_pre_cure_heads`, the completion-path hook that diffs a re-opened record's persisted `preCureHeads` sha against HEAD on the cure task's own completion — the LAST completion, when one pass's several cure tasks re-opened the same record — and takes this edge only when that diff never touched the record's cited path: this is the engine's OWN reconciliation of a status it itself persisted, never an operator ruling's or the steer hat's `task-update` claim -- both reach the same `apply_task_status_change`, but `transitions.task_status_report_errors` refuses the un-reopen edge for either caller unconditionally (`allow_un_reopen_restore=True` only for `source == "cure-reconciliation"`, cure-01-05-2, generalized to every non-engine caller at `88fe9d69`), so this field never reads anything else), `reason`. Emitted INSTEAD of `amendment_status_applied` for exactly this one transition (`transitions.is_un_reopen_transition`) -- every other transition through the same function still emits that generic audit event unchanged. Cure-01-01 (PRD requirement B.2/B.5, DoD 2, #280): never emitted for a `tasks[].sweep: true` task -- `state.records_pending_reconciliation` excludes every sweep outright, before computing any diff, so a re-opened sweep stays `pending` whatever paths the cure's own diff touches and this event never names it |
| `review.verdict_synonym_normalised` | the review payload said `fail`/`failed` where the contract's own negative verdict is `rejected`, so the engine rewrote the one field and acted on what the hat plainly meant instead of burning its whole re-ask ladder over the wording (#209): `approach`, `claimed_verdict` (the word the hat literally wrote), `normalised_verdict`. One-directional — no synonym maps to `verified`, so a wrong guess can only land on the negative word; from there the payload is treated exactly like a literal `rejected`, which still includes `review.verdict_recomputed`'s correction of a refusal whose own findings are all residual |
| `router.decision` | one completed router call on either routed state (task 002, #278 B.7): `state` (`"task_selection"` or `"plan_review"`/the ambiguous state's own name), `candidateIds` (the task ids or the state's own edge targets considered), `deterministicPick`, `choice` (what was actually taken), `reason` (the router's own one-line reason on the task-selection axis; empty string on the edge-based axis, which never asks for one), `fallback` (bool), `fallbackCause` (`"unparseable"` \| `"non_candidate_id"` \| `"timeout"` \| `"illegal_target"` \| `null` -- set iff `fallback` is true), the resolved `model`, and the call's own `tokens`/`costUSD`. Emitted from `LoopSupervisor._emit_router_decision_event`, called once per completed decision from both `_route_with_router` (the `plan_review` entry over its real peer-gate edges, deterministic pick `worker` via `plan_review_revision_bound_reached`; `_step_plan_review` itself never calls the router, so the live peer gate emits none) and `_route_task_selection` (task-selection, asked only when `scheduler.pending_candidates` finds 2+ runnable tasks and `route_task_selection` is on) |
| `review.finding_downgraded` | v0.27 requirement B.3 (#274, task 006); cure-01-01 (VG-01): on a CONFIRMATION review (at least one `cure-lineage.json` entry naming the approach under review has `kind` `"granted"` -- `LoopSupervisor._is_confirmation_review` -- and the rule reads the MOST RECENT of those, `_latest_granted_cure_pass`; a REFUTED-petition remedy insertion's own `kind: "remedy"` entry never triggers this on its own, and never replaces or narrows which granted pass's findings this rule reads), a blocking finding carrying no `reopens` value that RESOLVES to one of the current confirmation section's own listed cure task ids (cure-01-02-2, PRD B.3/B.5: `LoopSupervisor._confirmation_review_listed_cure_task_ids`, itself keyed on that same most recent GRANTED pass; an unrelated or unresolved `reopens` string is treated exactly like no `reopens` at all, never a bypass of the confirmation scope) and no `regression: true` (the failure is caused by a change inside the diff range) is downgraded to `severity: "residual"` before the verdict is computed (`findings.apply_confirmation_review_downgrade`, applied inside `LoopSupervisor._review_findings_channel`, so the review contract ladder and `_review_verdict` both judge the post-downgrade list and a `verified` claim beside such a finding is accepted without a re-ask): `approach`, `clause`, `evidence` (the downgraded finding's own text); once per downgraded finding per review pass. Never emitted on a first review of an approach (`LoopSupervisor._is_confirmation_review` is `False`) or from a mid-run adjudication episode or mid-plan review, and never for a finding that carries a resolving `reopens` or `regression: true`, or that claims one of the two things that always stay blocking regardless via the finding's own optional `always_blocking_exception` field (`findings.confirmation_review_exempt`, never a text match over `clause`/`evidence`) -- `"exposed_secret"` or `"absent_behavior"`, its only two accepted values |

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
`NNN-<slug>.md` into the steering inbox. Delivered to the next `planning`/`worker`
iteration and marked consumed once that iteration finishes cleanly — an iteration
that dies leaves it pending for the next one (`GET /steering`).
`202 {"file": "003-optional-slug.md"}`.

### `GET /steering`
Lists this run's steering messages, oldest first — one entry per
`steering/NNN-<slug>.md` file:

| field | meaning |
| --- | --- |
| `file` | the file name — the engine's own identifier for an entry (`POST /steering` returns it, the `steering.received`/`steering.consumed` events name it) |
| `seq` | the engine-assigned sequence number, as an int |
| `name` | the operator-supplied slug (the file stem without its sequence prefix) |
| `ts` | when the message arrived (the file's mtime — it is written once, atomically) |
| `state` | `pending` \| `outstanding` \| `applied` (task 007, #122): `applied` is consumed for good (`settled: true`, or task 006's redelivery cap giving up on it); `outstanding` is a message that HAS been delivered and judged `settled: false` (task 006's `steerRedeliveries` above) and is not currently consumed on disk, waiting for the next actionable iteration; `pending` is a message no iteration has ever looked at yet |
| `consumed` | the same fact as a bool (the pre-v0.6 key, unchanged) |
| `bytes` / `hasBody` | size, and whether there is any non-blank text |
| `body` | the message text |

A run nobody steered answers `[]`. The entries come from the single shared
reader `ralphd.engine.state.steering_entries`, which the hub also uses to read
`<run>/steering/` directly when a run's container is gone (`GET
/api/runs/<id>/steering`, docs/cli.md), so a live answer and a
container-gone answer describe the same run identically. Which *iteration*
consumed an entry is in the `steering.consumed` event (`GET /logs`), not here.

An entry stays `pending` until an actionable iteration that carried it in its
prompt has *finished cleanly*: a failed, interrupted or timed-out iteration
leaves it `pending`, and the next `planning`/`worker` iteration is handed it
again. Delivery is at-least-once, application at-most-once — the marker append is
idempotent, so re-delivery can never make one note `applied` twice. What each
attempt was handed vs. what it consumed is in that iteration's `meta.json`
(`steeringDelivered`/`steeringConsumed`, `GET /iterations`).

### `POST /rule`
Task 013 (v0.23 requirement A.2, #224): the operator answer channel. Body:
`{"text": "<markdown>", "name": "optional-slug"}`. Writes the next
`NNN-<slug>.md` into `<run-root>/rulings/` — deliberately NOT the same
directory `amend` writes to (`<config-root>/amendments/`), since `POST
/rule` is handled in-container and needs write access there. `202
{"file": "003-optional-slug.md"}`; `422` when `text` is missing or blank;
`409` once the job is finished.

The ruling's `signature` (rendered next to it in every subsequent prompt) is
resolved from THIS RUN'S OWN `status.json` at write time —
`rejectionSignatures[str(approach)]` — never from anything in the request
body, so a ruling can never be misattributed to a signature the caller
merely claims. A distinct command from both `steer` (discounted by review
by design) and `amend` (binds review but not verify, and is orphaned by a
replan renumbering task ids): a ruling reaches BOTH review and verify, and
is keyed to the rejection signature rather than a task id, so it survives
exactly the replan an amendment does not.

Task 015 cure (#224), widened by cure-01-01-3 (PRD A.3/A.4, #224): when the
run is CURRENTLY parked on the `operator_input_needed` pause (task 014, PRD
A.3 trigger condition 1) **or** the `rejected_repeated_signature` pause
(task 017, PRD A.3 trigger condition 3) and this ruling's own resolved
signature is a real (non-`None`) match for that pause's own
`rejectionSignatures[str(approach)]` entry, filing it releases that pause
itself — `state` flips back to `running` and `pauseReason` clears in the
SAME request, no separate `POST /resume`/`ralphctl resume` needed. A
ruling filed while parked for any OTHER reason (an operator's plain
`POST /pause`, `rejected_last_approach`, `stagnation`, ...), or with no
current rejection signature to match, leaves whatever pause is in effect
exactly as it was. A pause with a `pauseDetail` (a provider refusal or the
rate-limit ceiling) is never released by a ruling, whatever the approach's
signature.
The task-boundary commit-scan hold records that same `operator_input_
needed` reason, so the rule applies to it unchanged: it has no signature
of its own, and a ruling releases it only when an earlier `operator_
input_needed`/`rejected_repeated_signature` pause on the SAME approach,
already answered by a ruling, left a `rejectionSignatures` entry behind
(see the `pauseReason` row of `GET /status` and `POST /pause`, below).

### `POST /interrupt`
v0.25 task 028b (PRD requirement K.3/K.4, #267) reworked this: it used to
lie with a `409 "no iteration running"` the instant nothing was running,
*after* an accompanying `{"message": ...}` had already been enqueued via
`POST /steering`'s own path — the caller was told the request failed about
a message that had, in fact, landed. Now:

- While the loop is alive but idle — between iterations, in a deferred
  wait, in a backoff wait — this SIGINTs the current agent process if one
  is running (the iteration ends as `interrupted`; the loop proceeds to
  the next iteration immediately, picking up any pending steering), OR, if
  none is running, wakes the idle wait itself (the same bell a deferred
  wait and an infra-backoff wait already race their own timers against) so
  the loop re-enters its dispatch pass immediately rather than sitting out
  its bound unreachably. Both cases answer `200`, with a body that says
  which one happened: `{"interrupted": true, "target": "iteration",
  "signalled": true, "woken": false}` when a running iteration was
  signalled, `{"interrupted": true, "target": "idle", "signalled": false,
  "woken": true}` when an idle loop was woken.
- `409` is returned ONLY when the job has already finished (`state` is
  `succeeded`/`failed`/`aborted`/`timed_out`) — there is nothing left to
  interrupt, running or idle, and this is checked BEFORE any steering in
  the request body is enqueued, so a refusal never leaves an enqueued
  message behind either.

Body optional: `{"message": "..."}` — convenience combo, equivalent to
`POST /steering` then interrupt; the message is enqueued whenever the
handler does not refuse, whichever of the two 200 shapes follows.

**The hub's `now` field (task 001, #277).** `ui_server.py`'s `POST
/api/runs/<id>/steer` proxy (docs/cli.md) reads this exact route: a `now:
true` in ITS OWN request body forwards `{"message": ..., "name": ...}`
to this route rather than to `POST /steering`, passing this route's own
status code and body (including `target`) straight through with no
reshaping — a rejection (`409`, `400`, `401`) as much as a `200`; only an
unreachable run answers the hub's own `503`. `now` absent or `false`
proxies to `POST /steering` instead. The hub's `now` flag never reaches this
route's own body; it is purely a hub-side routing decision between the
two proxied targets.

Waking a loop that was parked in an **infra-backoff** wait ends that wait
early but deliberately does **not** restart the outage-budget episode clock:
that is `POST /retry`'s own meaning ("the endpoint is back — try it now", and
the only thing that emits `infra_retry_now`). An interrupt only gets the
operator's words in front of a loop that was otherwise unreachable; it
asserts nothing about the endpoint, so the episode keeps its spent budget and
its attempt count.

**Which states wake, which signal (v0.25 task 028c, PRD requirement K.6/K.7,
#267).** "A running iteration" above is phase-blind by construction
(`LoopSupervisor.interrupt()` calls `runner.interrupt()` unconditionally) --
it covers `planning`, `plan_review` and `review` exactly the same as
`worker`/`verify`/`reflect`/`steer`/`verdict_gate`, and K adds **no new
interrupt semantics for these three judging hats**: `--now`/`POST
/interrupt` against a running one of them is signalled, never routed to the
idle-wake branch, and the pre-existing cost of an interrupted attempt is
unchanged -- `plan_review`'s own #161 policy (one interrupted pass is
re-asked in place for free; a second in a row over a fresh, unworked plan
still spends a revision and re-ingests every task `pending`, exactly as
before K), `planning`'s in-place retry (`_one_planning_attempt`), and
`review`'s `review_interrupted` self-loop (requirement A, #72) are all
untouched. Only a genuinely IDLE loop -- between iterations, in a deferred
wait (K.1/K.2), or in an infra-backoff wait (K.5) -- takes the `target:
"idle"` wake branch. Both branches answer `200`; `409` is reserved for a
finished run, in every case, regardless of phase.

### `POST /pause` / `POST /resume`
Pause finishes the current iteration, then holds before the next one; resume
releases it. `200 {"paused": true}` / `200 {"resumed": true}`.

An operator pause is deliberately **not** a state: `state` stays `running` and
`phase` keeps naming the phase that ran last — there is no `phase: "paused"`,
for the same reason there is no `state: "degraded"` (it would break every
consumer's terminal-state logic). What is observable is the `log` event the
pause emits (`paused at next iteration boundary`, then `resumed`), in
`GET /events` and `events.jsonl`. `POST /pause` is `409` on a finished run;
`POST /resume` is always accepted (releasing a run that was never paused is a
no-op).

An **engine-initiated** hold is different, and it is the one case that does
record a lifecycle value (v0.18 requirement D.1/D.2, task 036): when review
rejects the LAST approach on blocking findings that are all curable and its
cure pass is already spent, the engine takes the `rejected_last_approach` edge,
emits `operator_escalation` and pauses itself — writing both
`state: "paused"` and `pauseReason: "rejected_last_approach"` to status.json at
that moment, because nobody typed the pause and nothing else would tell the
host the run is waiting for a ruling. `paused` is non-terminal (`state.NONTERMINAL_STATES`): a live
engine still owns the run, so no destructive action opens on it and a killed
engine leaves it dangling like any other non-terminal run. `POST /resume`
clears both fields (`state` back to `running`, `pauseReason` back to `null`)
and leaves `approach` and `phase` alone, so the run re-enters `review` over the
same, still-unarchived approach. CLI: `ralphctl unpause <run-id>` (deliberately
named `unpause`, never `resume` — `ralphctl resume` is a different command
entirely, docs/cli.md). Hub (task 006): the run page's "unpause" button, via
the hub's `POST /api/runs/<id>/unpause` proxy to this SAME `/resume` route —
no second unpause mechanism — 404 for an unknown run, 503 for an unreachable
one, the engine's own status code passed straight through otherwise
(docs/cli.md's `ralphctl ui` section).

**Every engine-initiated pause notifies the operator with a bounded digest,
never the raw diagnostic text** (v0.26 requirement A, #269). When a
`notifier_url` is configured, the notification POSTed for a pause is capped
at `MAX_NOTIFY_CHARS` (below the notifier's own 16,000-character cap) and
always carries the run id, `pauseReason`, `approach n of m`, blocking and
residual counts, tasks reopened and tasks inserted, spend this approach,
the hub URL (only when `hub_base_url` is configured) and the exact
command(s) that answer the pause. The coverage is complete: the four
finding-bearing reasons (`operator_input_needed` from review,
`rejected_repeated_signature`, `rejected_last_approach`,
`rejected_incurable`) list each blocking finding by id and `clause` only,
never its `evidence`; `stagnation`, `review_contract_unrepaired`,
`review_findings_unusable`, `unschedulable_dependency_cycle` and
`unresolvable_dependency_edge` add a short fault summary; and the
task-boundary commit-secret-scan hold (`pauseReason:
"operator_input_needed"` from `worker`) adds the hit's `sha`, `part` and
`variable` NAME, never the matched value. A pause with no review findings
channel or cure-pass preview (every reason but the four finding-bearing
ones) reads `unavailable` for blocking, residual, tasks reopened and tasks
inserted, never an invented `0`. Every pause that waits on an operator
ruling (the four finding-bearing reasons and `stagnation`) names both
`ralphctl rule <run> --file <ruling.md> --name <slug>` and `ralphctl
unpause <run>`, but the two live-run levers are not always interchangeable
single-step alternatives (cure task cure-01-01-9): for `operator_input_
needed`/`rejected_repeated_signature`, filing the ruling alone releases
the pause (`resume_if_ruling_answers_pause`), so the answer line reads
`rule ..., or unpause ... to accept the default`; for `rejected_last_
approach`/`rejected_incurable`/`stagnation`, that check never applies, so
`rule` alone leaves the run paused and the answer line instead reads an
explicit `rule ... then unpause ...` sequence beside the working
unpause-alone alternative. The full `operator_escalation.escalation` text follows the same rule (cure task cure-01-01-9): for `rejected_last_approach`, `rejected_incurable` and `stagnation` (both variants) it says only `ralphctl unpause <run>` releases the pause, that filing a ruling records it but leaves the run paused, and gives the `ralphctl rule ... then ralphctl unpause ...` sequence beside the unpause-alone alternative, and for `rejected_repeated_signature` it says a ruling releases the pause on its own, or `ralphctl unpause <run>` resumes with none recorded to accept the default. The dependency and review-contract pauses name
their own remedy plus `ralphctl unpause <run>`, never a rule command. The
commit-scan hold's own answer command depends on whether its approach
ALREADY carries a recorded rejection signature (an earlier `operator_
input_needed`/`rejected_repeated_signature` pause on the SAME approach,
answered by a ruling, before this hold ever landed): with none (the
common case), it names only `ralphctl unpause <run>` and says a ruling
does not release it; with one, `POST /rule` releases this hold too
(`resume_if_ruling_answers_pause` reads only the pause reason and the
approach's own signature, never which pause raised it), so it names both
levers as interchangeable alternatives instead. Delivery is retried up to 3 times
with bounded backoff and an exhausted retry records
`notification.escalation_undelivered` (below). The full text still lands
on the `operator_escalation` event's own `escalation` field -- except for
the commit-scan hold, whose record is its `commit_secret_scan` event.
SPEC.md §4.6.

### `POST /retry`
Wakes an infra backoff wait immediately instead of waiting for
`infraWait.nextAttemptAt`: the failed phase/iteration is retried at once and
the outage-budget **episode clock is reset** (the cumulative `waitedS` starts
from zero again, while the attempt counter — and therefore the escalating
backoff — is kept). Emits `infra_retry_now`. `200 {"retrying": true}`.
CLI: `ralphctl retry <run-id>` (docs/cli.md). Hub: the "retry now" button on
a degraded run-detail card, via the hub's `POST /api/runs/<id>/retry` proxy
(docs/cli.md's `ralphctl ui` section).

It also wakes a rate-limit wait (`rateLimitWait` under `GET /status`): the
same work re-runs at once, only the seconds actually waited extend the
deadline, and the outage budget is not involved. `infra_retry_now` then
carries `rateLimitWait`.

`409` when the run is in neither wait (`health: "ok"` / `infraWait: null` /
`rateLimitWait: null`, or the job already finished) — the problem detail says
so.
Naming: `/retry` is about a **degraded** run waiting out an endpoint outage;
`/resume` is about a **paused** run waiting for the operator. They are
independent — `/retry` never unpauses a paused run and never touches steering,
`/resume` never shortens a backoff. Design notes: `docs/architecture.md`
§10.4.

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
              "infraRetryBackoffMaxS": 300.0, "infraRetryMax": null, "infraOutageBudgetS": 14400.0,
              "rateLimitMaxWaitS": 86400, "rateLimitPreemptUtilization": 0.95,
              "routerTimeoutS": 60.0},
  "flags": {"vigilant": false, "onComplete": "idle", "onCompleteCmd": null, "reflect": false, "peerPlanReview": false, "peerVerdictGate": false, "operatorInputNeededPause": true, "midPlanReview": false, "routeTaskSelection": true},
  "contracts": {"retryMax": 2},  // response-contract re-prompt policy, task 010
  "model": {"model": null, "routerModel": null, "overrides": {}, "thinking": null,
             "thinkingOverrides": {}, "harnesses": {"planning": "pi", "worker": "pi"}},
  "pricing": null,                 // or the resolved host-side rate table, see below
  "priceStrategy": "none",         // "none" | "aws" — may a built-in rate table derive a cost?
  "priceTables": {"names": [], "answers": "neither", "tables": []},  // which table may price this run, in order
  "prompts": [{"name": "planning", "source": "builtin"}, ...],
  "skills": [{"name": "...", "origin": "mounted"}, ...],
  "creds": ["github", ...],
  "llmEnvKeys": ["..."]
}
```

`model.routerModel` (`job.yaml` key `router_model`, env `RALPHD_ROUTER_MODEL`)
is the router's own model key (task 018, requirement E, #60 pillar 2): checked
after `model.overrides.router` and before falling back to `model` directly
— see `routerTimeoutS` below for the router's latency budget and
`usage.byPhase.router` for how its cost is accounted, folded in alongside the
five hat phases without ever counting as a work iteration.

`flags.peerPlanReview`/`flags.peerVerdictGate` (`job.yaml` keys
`peer_plan_review`/`peer_verdict_gate`, `ralphctl start --peer-plan-review`/
`--peer-verdict-gate`) are the two opt-in peer gates (task 018/020, PRD
requirements K/L, both OFF by default): the first inserts a peer model's
approval/objection between `planning` and `worker`; the second inserts a
second, blind peer's re-verification between `review`'s `verified` edge and
terminal success. Neither has a `resume` flag — like `vigilant`/`reflect`,
they are a `start`-time choice only, replayed unchanged by `job.yaml` on
every later `resume`.

`flags.midPlanReview` (`job.yaml` key `mid_plan_review`, `ralphctl start
--mid-plan-review`) is a THIRD opt-in gate (task 022, PRD requirement K.2,
#169, OFF by default): once per approach, once the declared fraction
(`loop.MID_PLAN_REVIEW_FRACTION`) of the plan has already reached
terminal-success, the engine runs one review iteration scoped to those
requirements, opening a cure task per blocking finding through the
existing cure machinery (`state.insert_cure_tasks`) — see
`midPlanReviewEpisodes` under `GET /status` above. No `resume` equivalent,
for the same reason the two peer gates above have none.

`flags.operatorInputNeededPause` (`job.yaml` key
`operator_input_needed_pause`, `ralphctl start
--no-operator-input-needed-pause` to disable) is task 014's pause-before-
archive gate (PRD requirement A.1, #224) — ON by default, unlike the two
peer gates above: a rejection that would otherwise archive the approach
holds instead, with `pauseReason: "operator_input_needed"`, so an operator
can answer before that spend happens. Same `start`-time-only shape as
`vigilant`/`reflect`/the two peer gates — no `resume` flag.

`model.overrides` (`model_overrides:`/`models:` in `job.yaml`) is the per-hat
model table keyed by `planning`/`worker`/`review`/`verify`/`reflect`/`router`
(`MODEL_HATS`); `model.harnesses` (`harnesses:`) is the agent harness each hat in `MODEL_HATS` runs on (`pi` or `claude-code`); `model.thinking` is every hat's default `pi --thinking`
level and `model.thinkingOverrides` (`thinking_overrides:`) is the same
per-hat table for thinking/reasoning settings — task 007's own field, kept
separate from `model.overrides` because a model ref and its thinking setting
are two independent axes, not one merged table. Each entry is either a plain
`pi --thinking` string or a provider-shaped `{"thinking": ...}` /
`{"effort": ...}` object; `engine.tiers.resolve_bundle` and
`JobConfig.bundle_for` are the one place both tables are read together into
a hat's `(model, thinking)` bundle (§3.5, §4).

The infra-fault (LLM endpoint/network outage) budgets, all settable in
`job.yaml` and via env:

| Field | `job.yaml` key / env | Default | Meaning |
|-------|----------------------|---------|---------|
| `infraStartupTimeoutS` | `infra_startup_timeout_s` / `RALPHD_INFRA_STARTUP_TIMEOUT` | `150.0` | how long an iteration may run with zero observed LLM traffic before it is killed as an infra fault. The window is re-armed while the agent process is provably still consuming CPU (starved by a contended host rather than hung), so the kill lands on a *blocked* child and never on a slow cold start; the iteration timeout still bounds a silent child that spins |
| `infraRetryBackoffS` | `infra_retry_backoff_s` / `RALPHD_INFRA_RETRY_BACKOFF_S` (`"s1,s2,..."`) | `[2, 5, 15, 30, 60, 120, 300]` | escalating wait between retries of the same phase/iteration; the last value repeats for further attempts |
| `infraRetryBackoffMaxS` | `infra_retry_backoff_max_s` / `RALPHD_INFRA_RETRY_BACKOFF_MAX_S` | `300.0` | cap on a single backoff wait (clamps the repeating tail) |
| `infraRetryMax` | `infra_retry_max` / `RALPHD_INFRA_RETRY_MAX` | `null` | optional attempt cap, **honoured only when set explicitly**; `null` means no cap — retry for as long as the outage budget allows |
| `infraOutageBudgetS` | `infra_outage_budget_s` / `RALPHD_INFRA_OUTAGE_BUDGET_S` | `14400.0` (4h) | wall-clock budget for one fault episode: retries continue while the cumulative wait stays under it; the episode resets on any successful iteration. The post-terminal `reflect` iteration gets `min(this, 300s)` — the job is already over, so a still-dead endpoint must not hold the container open for hours (a `reflect_infra_delay` event covers the wait before its first attempt) |
| `rateLimitMaxWaitS` | `rate_limit_max_wait_s` | `86400` (1 day) | the ceiling on the run's total rate-limit waiting: a rate-limit wait that would pass it does not start, and the run pauses `operator_input_needed` with `pauseDetail.reason: "rate_limit_ceiling"`; `POST /resume` resets the count. Changeable in flight with `PATCH /config/budget` `rateLimitMaxWaitS` |
| `rateLimitPreemptUtilization` | `rate_limit_preempt_utilization` | `0.95` | a `claude-code` iteration does not start while that harness's last rate-limit report is `allowed_warning` at or above this utilization (0–1); the loop waits for that window's reset first (a `rate_limit_wait_started` with `reason: "preempt"`). A run with no report, and a pi hat, never wait here |
| `routerTimeoutS` | `router_timeout_s` / `RALPHD_ROUTER_TIMEOUT_S` | `60.0` | task 018 (requirement E): the wall-clock cap on a single router call (`engine.router.decide`, via `LoopSupervisor._ask_router`, model resolved from `model.routerModel`/`router_model` — see above) — a hung router call cannot stall the loop; a timeout counts as one failed attempt against `contracts.retryMax`, then the declared fallback edge is taken |

`contracts.retryMax` (`contract_retry_max` / `RALPHD_CONTRACT_RETRY_MAX`, default
`2`) is how many re-prompts a hat gets after its first attempt before a
malformed, absent or wrong-shape response contract payload (task 010, see
`engine/contract_enforcement.py`) is scored as one agent-fault iteration
against the budget — `retryMax + 1` total attempts, each failed one logged to
`events.jsonl` (`contract.violation`) the moment it happens, with a final
`contract.agent_fault` if none of them validate.

Each of those re-prompts is itself the uncharged contract-repair channel,
`LoopSupervisor._ask_contract_fix()` (task 001 requirement B, task 004) --
built directly on `_ask_router`'s own precedent above, so it is uncounted the
same way and for the same reason: no iteration slot, no `iterations_used`
increment, no `meta.json` (`loop.py` never calls `_run_iteration_once` or
touches `self.iterations_used` from inside it). It states the validation
errors verbatim and asks ONLY for the corrected final payload, never the
hat's own task prompt again. Each call gets its own transcript under
`artifacts/contract/NNNN.jsonl` (a plain counter, `loop.py:2963-2966`, not an
iteration directory), and its usage folds into `usage.byPhase["contract"]`
(`loop.py:2982`) -- kept out of `"router"`'s bucket because the two
uncharged channels answer structurally different questions (which edge to
take, versus what the corrected payload is), so a run's cost stays
auditable even for the calls that never cost an iteration. The re-ask runs
on the offending hat's own model (`self.cfg.model_for(hat)`), never a fixed
`"contract"` lookup, so the repair stays inside the tier that made the
mistake.

`creds` lists credential *names* only (no values, no sizes here — see
`GET /config/creds` for that); `llmEnvKeys` lists the *names* of any env
overrides set via `PUT /config/llm`, never their values.

`pricing` is the optional host-side rate table (`pricing` in `job.yaml` /
`RALPHD_PRICING` as JSON, normally inlined from `<registry>/config.yaml` by
`ralphctl start` — see `docs/cli.md`), reported as resolved so an operator can
see which rates produced a derived cost:

```json
{"table": "operator map",
 "models": {"openai/gpt-5": {"input": 1.25, "output": 10.0, "cacheRead": 0.125, "cacheWrite": 1.25}},
 "aliases": {"aigw-openai/*": "openai/*"},
 "free": ["ollama/*"]}
```

Rates are USD per **million** tokens keyed like the usage counters; an absent
cache rate falls back to the `input` rate (never to a silent `$0`). Aliases map
gateway-local model ids onto canonical ones (`"aigw-openai/*": "openai/*"`
keeps the tail). `free` lists model-id patterns the operator **declares** cost
nothing (matched with the same rules, after aliasing) — the only way a `$0` over
billable tokens is believed rather than treated as an implausible zero. The
default is `null` — no map, and unpriced traffic then stays
`unknown` rather than being guessed at. A configured map is consulted **only**
when the provider quoted no price, and its output is published separately as
`costDerivedUSD` (see the usage contract above).

`priceStrategy` (v0.6, #14) is the separate switch for the **built-in** rate
tables ralphd ships (currently `aws`, the Bedrock table in
`src/ralphd/engine/pricing_aws.py`):

| Value | Meaning |
|-------|---------|
| `"none"` (default) | no built-in table is consulted; a route the provider did not price stays `unknown`/`unavailable` |
| `"aws"` | the built-in AWS Bedrock table may price an unquoted route, published as `costDerivedUSD` (`~$0.45 derived`), never as `costUSD` |

Set it in `job.yaml` (`price_strategy`), per run via `RALPHD_PRICE_STRATEGY`,
in an LLM profile (`price_strategy:`, see `docs/llm-profiles.md`), registry-wide
with `ralphctl config set price_strategy aws`, or per job with `ralphctl start
--price-strategy aws` (which persists it into the run's `job.yaml`, so a later
`resume` uses the same strategy). An operator `pricing:` map is independent of
this knob and always applies. The reported value is always the **effective**
one: an unrecognised configured value degrades to `"none"` (with a warning in
the engine log) rather than failing the job, so what this field says is what
the run actually does.

`priceTables` (v0.6, #14) answers *which* table may produce a rate for this
run, before any money is derived — the operator map, the built-in AWS table,
both (in the order they are consulted) or neither:

```json
{"names": ["operator map", "builtin-aws-bedrock"],
 "answers": "operator map, then builtin-aws-bedrock",
 "tables": [{"name": "operator map", "models": 2, "aliases": 1, "free": 0},
            {"name": "builtin-aws-bedrock", "asOf": "2026-08-20", "asOfValid": true,
             "ageDays": 12, "staleAfterDays": 180, "stale": false, "models": 114,
             "aliases": 456, "source": "...", "refresh": "..."}]}
```

The order is precedence: the operator's `pricing:` map is always consulted
first, so a rate an operator typed for *their* gateway wins over a shipped
table's idea of the same model id, and exactly one table prices any given
message (never a sum or an average of both). `answers` is the one human string
surfaces use; `"neither"` means nothing can price this run's routes, which is
why an unpriced cost then reads `unavailable`. The built-in entry carries its
own as-of date and staleness so an operator can decide how much to trust a
derived number (see "Built-in AWS Bedrock rate table" in `docs/cli.md`).

### `PATCH /config/budget`
Raises (or lowers) the **iteration budget of a running job** without restarting
the container. Body:

```json
{"iterations": "+10"}   // relative top-up: current budget + 10
{"iterations": 40}      // absolute new budget
```

Same two forms as `ralphctl resume --iterations` (`"+N"` relative, a bare
integer absolute — so a bare `-5` is an *absolute* -5 and is rejected, never a
decrement; lower a budget by passing the absolute value you want).
`200 {"iterations": 40, "previous": 25, "iterationsUsed": 17}`.

The new value is live at the next iteration boundary (the loop re-reads it on
every turn — a job parked at `iterationsUsed == iterationsBudget` keeps going
after a top-up) and is immediately visible in `GET /status`
(`iterationsBudget`) and `GET /config` (`budgets.iterations`). Every accepted
change emits a `budget_changed` audit event.

Rejections carry a problem detail and change nothing:

| Status | When |
|--------|------|
| `422 iterations required` | no body, or no `iterations` key |
| `422 invalid iterations` | not an integer or `"+N"` string (`"abc"`, `12.5`, `[10]`), an absolute value below `1`, or a negative relative top-up |
| `409 budget below iterations used` | the resulting budget is below `status.iterationsUsed` (the *charged* count — infra-refunded retries are not charged); setting it *equal* is allowed and simply stops the run at the current boundary |
| `409 job finished` | terminal run — bump the budget with `ralphctl resume <run-id> --iterations +N`, which starts a fresh container over the run dir |

This is a change to the **live engine only**: `/config` is a read-only mount, so
the engine never rewrites `job.yaml`. A resumed container reads `job.yaml`
again, so use `resume --iterations +N` when the new budget has to survive a
restart.

Task 010 (v0.27 requirement C.3): the SAME route also accepts a fifth key,
`curePassCeiling` (`{"curePassCeiling": "+1"}` / `{"curePassCeiling": 5}`,
the operator front-end is `ralphctl budget <run-id> --cure-passes +N|N`),
raising the RUN-WIDE `cure_pass_ceiling` both cure channels
(`curePasses`/`gateCurePasses`, below) spend from one shared pot. Same
`"+N"`/absolute grammar; `409 cure pass ceiling below cure passes used`
when the resolved value is below the run's own cure passes already taken
(`curePasses`' and `gateCurePasses`' combined total) — the same shape as
the `iterations` 409 above, worded for this knob. Unlike `iterations`
above being the only knob that survives a resume before this task,
`curePassCeiling` ALSO persists: it is written to `status.json` here, and
a fresh container's own startup seeds `cure_pass_ceiling` from
`max(status.json's curePassCeiling, job.yaml's cure_pass_ceiling)`,
mirroring `iterations`' own seeding.

The route also accepts `rateLimitMaxWaitS` (`{"rateLimitMaxWaitS": "+3600"}` /
`{"rateLimitMaxWaitS": 172800}`, seconds; the operator front-end is `ralphctl
budget <run-id> --rate-limit-wait +N|N`), the ceiling on the run's total
rate-limit waiting (`rate_limit_max_wait_s`). Same grammar; `422 invalid
rateLimitMaxWaitS` when it does not parse. The response carries
`rateLimitMaxWaitS` and `previousRateLimitMaxWaitS`. It is read before the next
rate-limit wait, written to `status.json` as `rateLimitMaxWaitS`, and persists
across a resume like `curePassCeiling` (seeded from `max(status.json's
rateLimitMaxWaitS, job.yaml's rate_limit_max_wait_s)`).

### Prompts — override + listing

| Method & path | Body / response |
|---------------|-----------------|
| `GET /config/prompts` | list: `{"name": ..., "source": ...}` for every phase (`planning`, `worker`, `review`, `task-verify`); `source` is `builtin` / `mounted` (`/config/prompts/{name}.md`) / `api` (after a `PUT`) |
| `PUT /config/prompts/{name}` | `text/markdown` body. Replaces a phase prompt override; `name` must be one of the four phase names above, else `422`; `204` on success. Effective next iteration that builds this phase's prompt. Gated by `control_token` (see "Two independent gates" above) unconditionally on any run `ralphctl` started or resumed, and `PiRunner` strips `RALPHD_CONTROL_TOKEN` from a spawned iteration's own environment (task 003/#62, requirement A) — so no phase's agent process can ever call this route itself; it is, in practice, host-only. |

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
applies env to subsequent iterations. Mid-run endpoint/key rotation, called
directly: there is no `ralphctl llm set` wrapper in v0.6 (docs/cli.md). `204`.

## Versioning

`GET /version` → `{"ralphd": "0.31.0", "api": 1}` — the package version and the
API contract version (`ralphd.API_VERSION`), and nothing else: the engine does
not report pi's version here (`GET /iterations` reports the model each iteration
actually used, which is the version-shaped question operators ask). Breaking API
changes bump `api`. Nothing enforces that yet — `ralphctl` does **not** call this
route or compare versions on connect in v0.6; it is published for operators and
other clients to check.
