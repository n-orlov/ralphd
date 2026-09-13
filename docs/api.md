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
004): `POST /steering`, `POST /interrupt`, `POST /pause`, `POST /resume`,
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
  "schemaVersion": 1,              // run-dir schema this state was written under
  "state": "running",              // starting|running|paused|finishing|succeeded|failed|aborted
  "phase": "worker",               // planning|worker|verify|review|null; never "paused"
  "pauseReason": null,             // why an ENGINE-recorded pause is held, task 036; null unless `state` is `paused`
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
  "iterationsRefunded": {"infra": 2, "grace": 0, "steer": 0},  // earned refunds; survives a resume
  "verdict": null,                 // "verified" | "verified-with-residuals" | "unverified" | null while running -- settled (non-null) once `state` reaches `finishing`, task 012/#56; "verified-with-residuals" (requirement D.3) is a review VERIFIED verdict whose structured findings channel still carries one or more `severity: "residual"` entries
  "reason": null,                  // why a non-succeeded run ended (also the grace-review note)
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
| `reason` | why a non-succeeded run ended, or the note an off-budget grace review left; also episode-scoped, so a resumed run never reports the previous engine's reason |
| `previousEndings` | earlier episodes' endings, oldest first — `{endedAt, reason, verdict}` per superseded ending, `[]` for a run still in its first episode, capped at the 50 most recent. Kept because the terminal `state` event in `events.jsonl` records the state but not the reason or verdict |
| `termination` | present once the run was told to stop — see below |
| `graceReview` | present and `true` when an off-budget grace review verified the run |
| `iterationsUsed` | the **charged** iteration count: raw attempts minus infra AND steer refunds (task 014, #98). `iteration` (the raw number of the attempt running now) is the higher figure whenever anything was refunded, and `iterationsUsed` is what every budget surface prints and what `PATCH /config/budget` validates against |
| `iterationsRefunded` | `{infra, grace, steer}` — the refunds this run has earned, written as they are earned and read back by the next engine process, so a `resume` (or an auto-resume after a crash) no longer re-charges refunds granted before it (issue #32). Absent, and read as zeroes, until the run earns its first refund; a pre-v0.7 run dir therefore keeps its recorded `iterationsUsed`. Infra and steer refunds are subtracted from `iterationsUsed`; a grace review's refund is deliberately not (it stays visible as a used iteration) but is still subtracted from the budget comparison |
| `unconsumedSteering` | steering files still pending at the terminal write |
| `steerSettled` | the most recent `steer` hat iteration's own `settled` claim (`{"settled": bool, "at": timestamp, "notes": string\|null}`), `null` until the first one runs. `settled` is a harness-observable fact distinct from `done`: it means only "the hat processed this iteration's pending steering message(s) and applied its changes to the plan" -- it says nothing about whether the tasks it inserted or changed will pass. That verdict is the ordinary lifecycle's, read off those tasks' own `status` plus their `origin: "steer"` marker (task 011); the exit gate (`GET /tasks`, the terminal `state`/`verdict`) waits on THAT, never on this field (task 013, #98) |
| `steering` | live counts — `{pending, consumed}` over `steering/NNN-*.md`, added by the API rather than stored |
| `approachStartHeads` | the workspace HEAD an approach started at (v0.20.1 requirement A.1, task 001) — `{"<approach>": "<sha>"|null}`, written by `_approach_start_head` the FIRST time `_step_worker` reads it for that approach and read back unchanged on every later entry for the SAME approach — another iteration of the same process, an in-process resume, or a genuinely fresh process/`LoopSupervisor` over the same run-state tree — so the stagnation-exhaustion "did this approach gain commits" comparison's own origin can never be silently reset to whatever HEAD happens to be at the moment of a restart. `null` for a non-git workspace, still recorded. Absent for an approach whose `_step_worker` has never run |
| `blockerSignatures` | per-approach worker-stagnation blocker signature (task 101, #61) — `{"<approach>": "<signature>"}`, written the moment an approach fails on stagnation; the `replan` guard compares the entry for the approach that just failed against its predecessor's, and a genuine match ends the run instead of spending another approach on the same structural wall. Absent for a run that has never stagnated |
| `blockerSignatureOrigins` | per-approach origin of the matching `blockerSignatures` entry (task 012, requirement C.2, #124) — `{"<approach>": "stagnation"|"ingestion"}`, written alongside each `blockerSignatures` entry by `_record_stagnation_signature`; `_step_replan`'s `repeated_blocker_signature` guard reads it back, keyed the same way, to tell an ingestion-exhaustion repeat from an ordinary #61 worker-stagnation repeat — only a repeat where BOTH approaches' signatures originated from ingestion failure marks the run `health="degraded"`. Absent for a run that has never stagnated |
| `planReviewRevisions` | per-approach peer plan-review revision counter (task 018, PRD requirement K, #101) — `{"<approach>": <count>}`, incremented each time the opt-in `plan_review` hat objects to a plan and a revision is still available; `_step_plan_review`'s `plan_review_revision_bound_reached` guard compares the entry for the current approach against `peer_plan_review_max_revisions` (default 2) to decide when the plan proceeds regardless of further objections. Absent for a run with `peer_plan_review: false` (the default) or one that has never had an approach objected to |
| `planReviewApproved` | per-approach marker that the peer plan review let the plan proceed (`approved`, or `plan_review_revision_bound_reached`) -- `{"<approach>": true}`, written by `_step_plan_review` (#161). `_step_planning(skip_planning=True)` on a resume takes `planning_skipped_plan_already_reviewed` -> `worker` when the entry is present (or when any task is already past `pending`), instead of re-running `plan_review`; `_step_plan_review` itself refuses a revision over a worked plan (`plan_review_revision_refused_work_recorded`) because ingesting a fresh proposal resets every task to `pending`. Absent for a run with `peer_plan_review: false` or one whose plan review has not spoken yet |
| `workerSplitProposals` | per-approach worker split-proposal counter (task 021, PRD requirement N, #102) — `{"<approach>": <count>}`, the running total of tasks a worker's optional `proposed_tasks` contract field (`transitions.HAT_SCHEMAS["worker"]`) has actually inserted for that approach; `LoopSupervisor._apply_worker_proposed_tasks` refuses a proposal whole (never partially) once accepting it would push the total past `worker_split_budget_per_approach` (default 3). Absent for a run that has never had an accepted split proposal |
| `steerRedeliveries` | per-MESSAGE steering redelivery counter (task 006, PRD requirement B.1, #122) — `{"<steering-file-name>": <count>}`, incremented each time a clean `steer` iteration honestly reports `settled: false` for that message: `LoopSupervisor._handle_unsettled_steering` UNCONSUMES the file (`RunDir.unconsume_steering`) so it is genuinely redelivered to the next actionable iteration, up to `steer_redelivery_max` (default 3) redeliveries. Once a message's count reaches the cap it stops being redelivered and is instead recorded settled-BY-EXHAUSTION, with the reason, in the internal steering ledger that `state.steering_ledger` renders into every following hat's prompt (`settledByExhaustion`/`exhaustionReason` entries — not fields of this endpoint's own payload). `settled: true` never touches this counter — it consumes exactly once, as before this task. Absent for a run that has never had a `settled: false` steer reply |
| `verdictGatePeeks` | v0.17 requirement D.5 (#125 A): a list of `{approach, iteration, calls}` entries, one per `verdict_gate` iteration (either call site -- the ordinary `_step_verdict_gate` or the off-budget `_grace_verdict_gate`) whose OWN transcript contained a tool call reading past the gate's honor-system blindness (`iterations/`, `events.jsonl` or the per-iteration transcript file -- `state.detect_verdict_gate_peek`). A RECORDED OBSERVATION only: it never changes `verdict`, and a run can reach `succeeded` with a non-empty list here. Absent for a run with `peer_verdict_gate: false` or one whose gate iterations never peeked |
| `pauseReason` | v0.18 requirement D.1/D.2 (#142, task 036): why the ENGINE paused itself, recorded together with `state: "paused"` the moment the decision is made and cleared (back to `null`, with `state` back to `running`) by `POST /resume`. Three values: `"rejected_last_approach"` — the last approach was rejected on blocking findings that are all `curable: true` with its one cure pass already spent, so there is no approach budget left to cure them in and the run holds for an operator ruling instead of ending `failed` on paperwork, and `resume` re-enters `review` over the SAME approach; `"stagnation"` (v0.19 requirement B.3, #153, task 012) — the last approach's worker stagnation guard would otherwise fail the run silently, so the run holds instead, and `resume` re-enters `worker` over the SAME approach (no verdict pending, only unfinished tasks); and `"rejected_repeated_signature"` (v0.20.1 requirement E.5, #190, task 016) — an ORDINARY rejection's own blocking-clause signature (`rejectionSignatures`, above) repeated the immediately preceding approach's own recorded rejection signature, so the run holds instead of spending a fresh approach on a wall it has already lost one approach to, and `resume` re-enters `review` over the SAME approach, exactly like `"rejected_last_approach"`. Either way the approach is **not** archived and `approach`/`phase` are left untouched. `null` (or absent) for every other run — an operator `POST /pause` records neither this field nor a `state` change |
| `curePasses` | v0.18 requirement C.1/C.2 (#141) — per-approach cure-pass counter, `{"<approach>": <count>}`: taken when review's verdict is `rejected`, every blocking finding is `curable: true`, and either the entry for that approach reads `0` OR (v0.20 requirement E.1, #167, task 030) the count is below `JobConfig.cure_pass_ceiling` (job.yaml key `cure_pass_ceiling`, default **3** — v0.21 requirement A.2, #192, task 002; a module-level `CURE_PASS_CEILING` literal before that task) and this rejection's own blocking clause set is not a superset (by `clause`) of `cureBlocking`'s entry, below; incrementing it routes `review -> worker` over the SAME approach with cure tasks appended (`origin: "review-cure"`) instead of replanning. A second-or-later `rejected` that IS a superset, one at the ceiling, an incurable finding, or a criteria-tamper finding (v0.18 task 039: the engine's own `successCriteria`-authority finding is flagged `curable: true`, but a tampered approach is never cured -- it is archived and replanned) takes the ordinary replan/pause edge regardless of this counter. Disk-backed and survives a resume. Absent for a run that has never taken a cure pass |
| `cureBlocking` | v0.20 requirement E.1 (#167, task 030) — companion to `curePasses`: `{"<approach>": [<clause>, ...]}`, the blocking clause set (`findings.blocking_finding_clauses`, sorted) the MOST RECENT granted cure pass for that approach recorded, written in the same status update that increments `curePasses[approach]`; a later rejection's own blocking clause set is compared against this entry to decide whether it earns another pass (not a superset) or falls through to the ordinary replan/pause edges (a superset, including an unchanged repeat). Disk-backed and survives a resume. Absent for a run that has never taken a cure pass |
| `gateCurePasses` | v0.21 requirement A.1 (#192, task 001) — the PEER VERDICT GATE's own per-approach cure-pass counter, `{"<approach>": <count>}`, kept under a separate key from `curePasses` so a cure pass the gate takes is never conflated with one review already took on the same approach: taken when the gate's own verdict is `rejected`, every blocking finding in `RunDir.verdict_gate_findings_json_file` is `curable: true`, none is a criteria-tamper finding, and the count for that approach is below `JobConfig.cure_pass_ceiling` (job.yaml key `cure_pass_ceiling`, default **3** — v0.21 requirement A.2, #192, task 002; the SAME configured ceiling `curePasses` above is bounded by); incrementing it routes `verdict_gate -> cure` over the SAME approach with cure tasks appended (`origin: "review-cure"`, `state.insert_cure_tasks` — the same call review's own cure pass uses) instead of archiving the approach and replanning, or ending the run once the approach budget is spent. Disk-backed and survives a resume. Absent for a run that has never taken a gate cure pass |
| `rejectionSignatures` | v0.20.1 requirement E.5 (#190, task 016) — per-approach findings-channel REJECTION signature, `{"<approach>": "<signature>"}`: the sorted, normalised blocking-clause set (`LoopSupervisor._review_rejection_signature`) of an ORDINARY rejection (one that took none of `rejected_incurable`/`rejected_curable_pass_available`/`rejected_last_approach` above), written the moment that rejection is about to archive the approach and replan. `_step_review` compares the entry for the approach that just rejected against its predecessor's; a genuine match takes the `rejected_repeated_signature` pause edge instead of archiving/replanning — a repeated findings-channel wall pauses for an operator ruling rather than spending, and almost certainly losing, a fresh approach on it. Absent for a run that has never taken this ordinary-rejection branch |

`state` reaches the value `paused` for exactly one reason, and it is never an
operator pause: the engine pausing ITSELF on the `rejected_last_approach`
escalation edge (v0.18 requirement D.1/D.2, task 036) records `state:
"paused"` plus `pauseReason` so a host can see that the run is parked waiting
for a human ruling rather than working. `POST /pause` records nothing at all —
it holds the loop at the next iteration boundary without changing `state` or
`phase` (see `POST /pause` below). There is no `paused` **phase** in either
case: `phase` keeps naming the phase that ran last. The full `status.json`
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
integer *count* of priced ids (`len(RATES)`, `121` for the built-in table in
this build), not a list, so the built-in table's per-model coverage is checked
against the table itself — `pricing_aws.pricing_map().rate_for("<id>")`, `None`
for an id no exact key, alias hop or wildcard covers (`openai/gpt-6-astra` →
`None`; `aigw-openai/openai.gpt-5.6-sol` → a `ModelRate`). A model no table
prices means every `~$… derived` total that includes that hat's traffic
undercounts it. Real example, as read live and recorded at
`docs/prds/README.md`'s `selfdev-v020-a-commit-is-progress` row: `~$648.75
derived` understates that run because `aigw-openai/openai.gpt-6-astra`, the
model behind its `plan_review`/`verdict_gate` hats, has no rate entry in
either table — and that reading was taken with the run total's own
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
`starting|running|paused|finishing|succeeded|failed|aborted` (task 012, #56,
adds `finishing`; task 036, #142, adds `paused` for the engine's own
`rejected_last_approach` escalation hold — never for an operator `POST /pause`)
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
| `costUSD: 0.0`, `costPriced: true`, `costFree: true` | the route is **declared** free in `pricing.free`, so this $0 is real and renders `$0.00` |
| `costDerivedUSD: 0.31`, `costDerived: true` | every unpriced message was covered by the host-side pricing map — cost is *derived*, and kept out of `costUSD` |
| `costDerived: false` | at least one unpriced message had no rate in the map (or no map is configured), so part of the cost stays unknown |

How a run total / `byPhase` / `byApproach` bucket summarises a mix of priced and
unpriced iterations is the `costStatus` contract under `GET /status` above.

#### Implausible zero quotes (v0.6)

A quoted cost of **exactly 0 alongside non-zero billable tokens** is not a
price: pi zero-fills its `cost` block when the resolved model definition
carries no rates, so a gateway can report `costUSD: 0` for half a million
tokens (`artifacts/reports/pricing-anomaly.md` §7 has the live payload). Such a
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
| `state` | lifecycle transition: `state` is `running` (emitted by every engine process as it starts the job loop, resume included -- except a process that resumes a run dir already recorded `finishing`, task 012/#56 below, which has no job loop left to start and promotes straight to the terminal event), `finishing` (task 012, #56: the verdict is settled but the post-terminal `reflect` iteration has not run yet -- see status.json's `pendingTerminalState`) or the terminal `succeeded`/`failed`/`aborted`; the startup event also carries `resumed` (`true` when this process picked up a run dir that already held recorded work) |
| `phase` | phase entered (planning/worker/verify/review), approach number |
| `iteration.start` / `iteration.end` | number, phase, model / exit, sentinel, error, `faultClass` (`null` \| `"infra"` \| `"work"`, identical to the iteration's `meta.json` — see `GET /iterations`) |
| `iteration.attempt_archived` | a reused iteration number's earlier, crashed attempt was archived before the new one wrote: `number`, `attempt` (`1` for the oldest), `path` (`iterations/NNNN/attempts/NN`), `files` |
| `task` | task id + old/new status — emitted live while a worker iteration is still running (polled every ~0.25s against `tasks.json`), not only after the iteration ends, so `pending -> in-progress` is observable in real time |
| `steering.received` / `steering.consumed` | steering file name; `steering.consumed` also carries the `iteration` and is emitted only after that iteration finished cleanly (never twice for the same file) |
| `signal` | COMPLETE / VERIFIED / task-verified detected |
| `log` | engine-level notices (timeouts, retries, failures) |
| `infra_retry` | an infra-classified attempt is being retried: phase, attempt, error, `backoffS` (`null` when giving up), `waitedS`, `budgetS` |
| `infra_wait` | a backoff wait started: the full `infraWait` payload (`since`, `attempt`, `error`, `phase`, `nextAttemptAt`, `waitedS`, `budgetS`, `remainingS`) plus `backoffS`; the run is now `health: "degraded"` |
| `infra_recovered` | an iteration reached the model again: `health: "ok"`, `infraWaitTotalS` — the outage episode is over and `infraWait` is back to `null` |
| `infra_retry_now` | an operator woke a backoff wait via `POST /retry`: phase, attempt, error, `source: "operator"` |
| `reflect_infra_delay` | the job ended on an infra-shaped failure, so the post-terminal `reflect` iteration waits before its first attempt instead of firing into the same dead endpoint: `delayS`, `error`, `budgetS` (reflect's own, capped, outage budget). Followed by an ordinary `infra_wait` with `attempt: 0` |
| `reflect_done` | the post-terminal `reflect` iteration finished: `ok`, plus `error`/`errorClass` when it failed (same verdict as status.json's `reflect`) |
| `reflect_skipped` | the post-terminal `reflect` phase produced no verdict because a signal was already taking the engine down: `attempted` (false = never started, true = cut short mid-attempt), `signal`, `reason`. No `FAILED.md` is written on this path — see status.json's `reflect` |
| `reflect_tombstone_cleared` | a successful `reflect` attempt removed a stale `reflection/FAILED.md` written by an earlier one: `path`. The alias `reflect-failed` disappears from `artifacts ls` with the file |
| `deadline_extended` | the job deadline moved out after an infra wait OR an operator pause: `waitedS`, new `deadlineAt`, `reason` (`"infra wait"` — also phase/attempt/`infraWaitTotalS` — or `"operator pause"` — also `pausedTotalS`) |
| `budget_changed` | the iteration budget was changed in flight via `PATCH /config/budget`: `field: "iterations"`, `previous`, `iterations` (new value), `delta`, `iterationsUsed`, `source: "api"` |
| `notification.sent` / `notification.failed` | the engine-owned `notify` hat tool (or the engine's own operator-escalation notifier) POSTed to the configured notifier URL: `endpoint`, `textSha256`, `httpStatus`. A successful send (2xx) emits `notification.sent`; an unconfigured URL, a connection error, or a non-2xx status all emit the distinct `notification.failed` instead -- never `notification.sent` for a send that did not actually succeed (requirement B.1, #197) |

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

### `POST /interrupt`
SIGINTs the current agent process (the iteration ends as `interrupted`; the loop
proceeds to the next iteration immediately, picking up any pending steering).
No-op `409` if no iteration is running. Body optional:
`{"message": "..."}` — convenience combo, equivalent to `POST /steering` then
interrupt.

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
same, still-unarchived approach.

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
              "routerTimeoutS": 60.0},
  "flags": {"vigilant": false, "onComplete": "idle", "onCompleteCmd": null, "reflect": false, "peerPlanReview": false, "peerVerdictGate": false},
  "contracts": {"retryMax": 2},  // response-contract re-prompt policy, task 010
  "model": {"model": null, "routerModel": null, "overrides": {}, "thinking": null,
             "thinkingOverrides": {}},
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

`model.overrides` (`model_overrides:`/`models:` in `job.yaml`) is the per-hat
model table keyed by `planning`/`worker`/`review`/`verify`/`reflect`/`router`
(`MODEL_HATS`); `model.thinking` is every hat's default `pi --thinking`
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

`GET /version` → `{"ralphd": "0.20.2", "api": 1}` — the package version and the
API contract version (`ralphd.API_VERSION`), and nothing else: the engine does
not report pi's version here (`GET /iterations` reports the model each iteration
actually used, which is the version-shaped question operators ask). Breaking API
changes bump `api`. Nothing enforces that yet — `ralphctl` does **not** call this
route or compare versions on connect in v0.6; it is published for operators and
other clients to check.
