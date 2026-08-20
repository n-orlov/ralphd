# ralphd Architecture

Status: design, targets v0.1 unless marked otherwise.

## 1. Overview

ralphd is a job-scoped autonomous coding loop. A **job** is one PRD (product
requirements document / task description) executed to verified completion. Each job
runs in its own Docker container. The container holds:

- the **engine** (`ralphd`, Python): loop supervisor + HTTP API server, PID 1
- the **agent runtime**: [pi](https://pi.dev) CLI, spawned as a subprocess once per
  iteration
- a **workspace**: the code being worked on (host bind-mount or named volume)

The host side is driven entirely by **`ralphctl`** (Python CLI): it prepares config,
starts the container, talks to the API, and maintains the run registry at
`~/.ralphd/`. Nothing in the engine assumes any particular cloud, CI system, or
network beyond reaching the configured LLM endpoint.

## 2. The loop

The loop has three phase types drawing on one shared **iteration budget** (`max_iterations`):

```
job start
  └─ approach 1..max_approaches (shared budget):
       ├─ PLANNING   (1 iteration)  read PRD → write tasks.json + notes.md
       ├─ WORKER     (n iterations) execute tasks one at a time until it
       │                            emits <promise>COMPLETE</promise>
       │    └─ [vigilant mode] VERIFY after each newly completed task;
       │                       failure ⇒ status=validation-failed, back to worker
       └─ REVIEW     (1 iteration)  independently re-check EVERY PRD requirement
            ├─ satisfied  ⇒ <promise>VERIFIED</promise> ⇒ job succeeds
            └─ not        ⇒ write review-findings.md ⇒ next approach with a
                            composite PRD (original + findings + attempt history)
budget exhausted or max_approaches reached ⇒ job fails (state preserved)
```

Design invariants:

- **The worker's word is never trusted.** `COMPLETE` only gates entry to review.
  Only the reviewer's `VERIFIED` ends the job successfully.
- **Fresh context per iteration.** Each iteration is a new `pi` process; continuity
  flows exclusively through state files (below). This bounds context growth and makes
  every iteration resumable and interruptible.
- **`tasks.json` is the source of truth**, written atomically (write-temp + rename).
  Killing an iteration mid-flight loses at most that iteration's uncommitted
  reasoning, never task state.
- **Signals are exact sentinels** (`<promise>COMPLETE</promise>`,
  `<promise>VERIFIED</promise>`, `<task-verified>id</task-verified>`) scanned from
  the agent's final output; prompts instruct the agent to emit them only as its
  last line.
- **One task per worker iteration.** Checkpointing, steering application, and
  vigilant verification all key off iteration boundaries; a worker that batches
  several tasks into one iteration silently bypasses all three. The worker
  prompt makes this the headline rule (with the *why*, since capable models
  treat unexplained rules as optional efficiency advice), and the engine
  detects violations by diffing task statuses around each worker iteration,
  emitting a warning `log` event when more than one task completed. The engine
  cannot roll extra completions back — detection is for operator visibility
  and prompt regression testing.
- **Iteration failures are contained.** Any engine-side error while running an
  iteration (stream overflow, OS error, agent crash) fails that iteration —
  recorded in its `meta.json` and events — and the loop continues; only budget
  exhaustion, an explicit abort, or an unrecoverable engine bug ends the job.
  The runner must never leave an orphaned agent process behind (kill the
  process group on any exit path).
- **Task status transitions are visible live, not just at iteration end.**
  The worker prompt makes flipping the picked task to `in-progress` in
  `tasks.json` the mandatory first write of the iteration — before any other
  file edit or shell command. While the agent subprocess for a worker
  iteration is running, the engine runs a lightweight background poller
  (`LoopSupervisor._poll_task_changes`, ~4/s) that re-reads `tasks.json` and
  emits `task` events for any status change immediately, instead of only
  once via the post-iteration `_emit_task_changes()` call. An operator
  watching `GET /events` or `GET /tasks` (or `ralphctl`) therefore sees
  `pending -> in-progress` for the exact task being worked while that
  iteration is still in flight, not only after it ends.

### Phase prompts

Prompt templates live in the image at `/opt/ralphd/prompts/` — one per phase
(`planning.md`, `worker.md`, `review.md`, `task-verify.md`, plus the workspace-level
agent instructions file `AGENT.md`). All are **overridable**: a file of the same name
in `/config/prompts/` (mapped by the CLI) takes precedence, and can be replaced at
runtime via the API (`PUT /config/prompts/{name}`), taking effect next iteration.
Prompts are authored fresh for this project.

### Vigilant mode

Optional (`vigilant: true` in job config). After a worker iteration, the engine
checks which tasks are currently `completed` and haven't yet received a passing
verify iteration; each such task gets a verification iteration against its
`successCriteria`. Failures set `status: validation-failed` with
`validationNotes`; the worker treats those like `pending` and reads the notes. Three
failed validations of the same task mark it `failed` (budget protection).

**Crash/resume-safe tracking (task 052).** "Hasn't yet received a passing verify
iteration" is answered from an engine-owned, disk-persisted record --
`<run-dir>/vigilant-verified.json`, a JSON list of task ids that have passed a
verify iteration (`RunDir.read_verified_tasks()` / `mark_task_verified()`) -- not
from an in-process before/after diff of `tasks.json` scoped to a single
`run_iteration("worker")` call. The earlier before/after-diff design had a gap: if
the engine crashed after a worker iteration wrote a task as `completed` but before
(or during) its verify iteration, a resumed process's very first `tasks.json`
snapshot already showed that task as `completed`, so the diff never fired for it
again and its mandatory verification was silently skipped for the rest of the job.
Because the verified-task record survives the crash (it's just a file in the run
dir, read fresh on every check) and is checked against every currently-`completed`
task -- not only ones that changed status inside this process -- a resumed engine
always still catches and runs the pending verify iteration for such a task before
the job can reach a terminal verdict. The record is purely a derived cache, never
edited by the agent (unlike `tasks.json`) and rebuildable in principle by rescanning
iteration `meta.json` files for `verifyOutcome: "pass"`.

A verify iteration that errors out mid-stream (agent/provider failure -- e.g. a
Bedrock 502 -- surfaced as an assistant `message_end` with `stopReason: "error"`)
before ever emitting the `<task-verified>{id}</task-verified>` sentinel is an
infrastructure fault, not a verdict, and must never be scored as a validation
failure: the engine retries verification (bounded, `MAX_VERIFY_ERROR_RETRIES = 3`)
without touching the task's `status` or `validationAttempts`. If every retry also
errors (or the iteration budget runs out first), the engine gives up on verifying
that task for now, logs an error event, and leaves its status exactly as the
worker left it -- it is not marked `validation-failed`, `failed`, or otherwise
penalized. The verify iteration's `meta.json` records `verifyOutcome: "error"`
for these attempts (distinct from `"pass"`/`"fail"`).

### Criteria fingerprinting and edited-after-failure detection (task 008)

Every task gets a `criteriaFingerprint` (sha256 of its `successCriteria` text)
recorded in `tasks.json`: a baseline on first sight (right after planning, or
on first observation for a task discovered mid-run), silently refreshed on
any subsequent edit -- *unless* the task already has `validationAttempts >= 1`
at the moment the text is observed to differ from the stored fingerprint, in
which case the engine also sets a persistent `criteriaEditedAfterValidationFailure:
true` marker on the task before refreshing the fingerprint. This catches a
worker that quietly rewrites the bar it just failed instead of doing the work,
without flagging ordinary criteria refinement that happens before any failure
or a task's untouched criteria. The marker, once set, is never cleared. Task
009 feeds the list of flagged tasks to the review prompt so at least one
independent re-verification of the new text happens before a `VERIFIED`
verdict, closing the gap where `_verify_task`'s `validationAttempts >= 3` skip
would otherwise let repeatedly-rewritten criteria dodge every check.

### Feeding flagged tasks into the review prompt (task 009)

`LoopSupervisor._flagged_criteria_review_context()` reads `tasks.json` fresh
before every `review` iteration (both the initial one and any re-review after
deferred steering is consumed) and, when at least one task carries
`criteriaEditedAfterValidationFailure: true`, renders a `## Criteria edited
after a validation failure` section listing each such task's id, title, and
CURRENT `successCriteria` text as `extra` context passed to `build_prompt`.
When no task is flagged, the context is the empty string and the review
prompt is byte-for-byte unaffected. `prompts/review.md` itself always carries
a standing instruction (present regardless of whether the section is
present this iteration) that when that section IS present, the reviewer
MUST independently re-verify each listed task id against its current
criteria text and state an explicit pass/fail conclusion per id before
emitting `VERIFIED` -- a task's own exhausted `validationAttempts` (the
`_verify_task` `>= 3` skip) never substitutes for this manual check.

### No-progress escalation guard vs. instant startup/infra failures (task 059)

The worker loop's stagnation guard (3 consecutive worker iterations with no
`tasks.json` change and no `COMPLETE`/interrupt ⇒ fail the approach) and
planning's "produced no `tasks.json`" check exist to detect a stuck
*approach* — an agent that keeps genuinely attempting the task and failing
or spinning. They must not fire on a completely different failure class:
the agent process itself never starting at all (missing/broken LLM
credentials, a provider outage before the first token, any startup fault),
which the engine can tell apart from a genuine attempt because it leaves no
observable work signal whatsoever — no assistant text, no token usage — and
returns in well under the time any real model call could ever take.

`LoopSupervisor._check_instant_failure()` classifies an iteration result as
an **instant failure** when all of: exit code nonzero, not interrupted, not
timed out, no `final_text`, no `usage`, and wall-clock duration under
`INSTANT_FAILURE_MAX_DURATION_S` (5s). Each instant failure increments an
engine-instance counter (`_instant_failure_streak`, reset to 0 the moment a
non-instant-failure result occurs, in *any* phase); an iteration classified
this way is excluded entirely from the stagnation guard's before/after diff
and from planning's empty-`tasks.json` approach-advance — it neither counts
as progress nor as no-progress, and the current approach is retried in
place rather than abandoned. Once the streak reaches
`MAX_CONSECUTIVE_INSTANT_FAILURES` (3), the engine gives up immediately:
it sets the job's abort reason to a diagnostic naming the likely cause
("... likely missing or broken LLM credentials, or an agent-startup
fault ... failing fast instead of burning through approaches via the
no-progress escalation guard"), which makes `budget_left()` false job-wide
— the job reaches a terminal state in exactly 3 iterations (`state:
aborted`, `verdict: unverified`, `reason: <the diagnostic>`), never having
switched approaches and never having been scored as an ordinary no-progress
failure. A worker that runs to completion every iteration but never
changes `tasks.json` (a genuine stall, not a crash) is unaffected and still
trips the ordinary 3-iterations-no-progress guard exactly as before.

### Infra-fault fail-fast + retry with backoff (task 001a)

> Historical design note for the *first cut* of this machinery. The shipped
> v0.5 whole — fault taxonomy incl. `faultClass` and the `aborted` carve-out,
> the outage-budget model, the deadline extension, the `health`/`infraWait`
> contract, manual retry-now, and the instant-failure carve-out — is §10.

Filed after a live incident distinct from task 059's: two consecutive
*worker* iterations each hung the **full** `iteration_timeout_s` (45 minutes
by default) on a transient `getaddrinfo ENOTFOUND` gateway-DNS glitch before
the process finally died with `exit=-2`, burning ~90 minutes of wall clock
and 2 iterations of budget. Task 059's instant-failure carve-out never saw
it: that guard only classifies exits under `INSTANT_FAILURE_MAX_DURATION_S`
(5s), and this failure mode is the opposite shape — the process doesn't
exit quickly, it *hangs*.

**1. Startup-window watchdog (fail fast on zero LLM traffic).** `PiRunner.run()`
accepts a `startup_timeout_s` argument; while the agent subprocess's stdout is
being pumped, a concurrent watchdog task waits for the first line that parses
as a pi NDJSON event at all (`PiRunner._scan_line()`'s return value — any
event, not specifically a `message_end`) inside that window. If none arrives,
the watchdog sets `IterationResult.no_traffic_timeout = True` and SIGINTs the
process immediately, rather than waiting for the full `timeout_s` to elapse.
`LoopSupervisor._run_iteration_once()` passes a real `startup_timeout_s`
(`cfg.infra_startup_timeout_s`, default 150s, overridable via
`RALPHD_INFRA_STARTUP_TIMEOUT` or the `infra_startup_timeout_s` job.yaml key)
for exactly the phases the infra-retry wrapper protects — since task 009 that
is **all five** (`planning`, `worker`, `review`, `verify`, `reflect`): a hung
invocation is only worth killing early if something is going to retry it.

**2. Classification.** `.faults.classify_fault()` is a pure function (no
engine state) that maps a finished iteration's failure signal to `"infra"`,
`"work"`, or `None` (not a failure at all): `no_traffic_timeout=True` is
always `"infra"`; a captured error string matching a known infra signature
(DNS/`ENOTFOUND`, `ECONNREFUSED`/`ECONNRESET`, TLS/SSL handshake failure, or
a gateway 5xx) is `"infra"` regardless of whether some traffic preceded it;
an iteration that produced real LLM traffic (assistant text or token usage)
and then exited nonzero/timed out/was interrupted with no recognized infra
text is `"work"`; any other no-traffic failure defaults to `"infra"` too
(deliberately — an unclassifiable no-traffic failure is far more likely to
be an environment/startup fault than genuine agent work).

**3. Retry with backoff, not escalation.** `LoopSupervisor._run_iteration_with_infra_retry()`
wraps `_run_iteration_once()` for every phase in
`LoopSupervisor.INFRA_RETRY_PHASES` — since task 009 all five (`planning`,
`worker`, `review`, `verify`, `reflect`), because an endpoint outage does not
care which prompt is running: before that, an infra-shaped `review` failure
rejected and archived the approach, an infra-shaped `verify` failure ate the
task's bounded error-retry budget, and `reflect` just lost its report. The
wrapper takes **precedence** over those phase-local budgets: an
infra-classified failure is retried and refunded here and consumes neither
`MAX_VERIFY_ERROR_RETRIES`, the review steering loop's iterations, nor a
task's `validationAttempts`; the phase's own logic only sees a result once it
is no longer an infra fault, or once the wrapper gave up (which sets
`_abort_reason`, so `budget_left()` is already False and those loops exit
rather than re-charging the same outage). Since task 010 (#5) an
infra-classified result that is *also* an instant failure
(sub-`INSTANT_FAILURE_MAX_DURATION_S`, no `no_traffic_timeout`, no observable
work) is retried here too, with task 059's streak-based carve-out keeping the
last word on a *run* of identically-failing attempts (§10.5); only an instant
failure that did reach the model (tokens billed) is returned immediately to
the phase's own bounded error retry, so the two mechanisms never race or
double-count the same failure. Any other infra-classified result
retries the *same* phase/iteration in place with escalating backoff
(`cfg.infra_retry_backoff_s`, default `[2, 5, 15, 30, 60, 120, 300]` seconds
with the last value repeating, clamped to `cfg.infra_retry_backoff_max_s`
(default 300s); overridable via `RALPHD_INFRA_RETRY_BACKOFF_S="s1,s2,..."` /
`RALPHD_INFRA_RETRY_BACKOFF_MAX_S`).

The stopping rule is wall-clock, not an attempt count: attempts are
**unlimited by default** and stop only when the *episode clock* has spent
`cfg.infra_outage_budget_s` (default 4h, `RALPHD_INFRA_OUTAGE_BUDGET_S`) on
waiting. An *episode* is one continuous outage — consecutive
infra-classified attempts with no iteration reaching the model in between;
`LoopSupervisor._reset_infra_episode()` clears the attempt counter and the
accumulated wait as soon as one does (success or genuine work failure), so a
job that hits a short glitch every hour is never slowly starved of retry
budget. Each wait is additionally clamped to what is left of the budget, so an
episode's cumulative wait never exceeds it. `cfg.infra_retry_max`
(`RALPHD_INFRA_RETRY_MAX`) remains an optional attempt cap honoured **only when
set explicitly** (back-compat and a hard-stop escape hatch); while unset,
nothing but the outage budget ends a retry episode. Each infra-classified attempt
increments `LoopSupervisor._infra_refunded`, which `budget_left()` subtracts
from `iterations_used` — an infra retry never counts against the job's
iteration budget (the attempt still gets its own iteration directory/number,
since `iterations_used` itself keeps incrementing monotonically; only the
*budget comparison* is adjusted). It also never touches
`_instant_failure_streak` or the worker loop's stagnation counter.

**4. Surfacing.** Each attempt emits a `type: infra_retry` event
(`phase`, `attempt`, `maxAttempts` — `null` when uncapped, `error`,
`noTrafficTimeout`, `backoffS` — the wait about to start, `null` when giving
up, `waitedS` — the episode's cumulative wait so far, `budgetS`) to
`events.jsonl`. While waiting out the backoff between attempts,
`status.json`'s `currentIteration.note` reads `"retrying after infra fault
(attempt N[/max], next in Xs): <error>"` (visible via `ralphctl status`/the
hub). When the outage budget runs out, `LoopSupervisor._abort_reason` is set to
`"infra fault: <phase> iteration failed throughout a <D>s infra outage (<N>
attempts, <W>s of the <B>s outage budget spent waiting): <error>"` (with an
explicit `infra_retry_max` it keeps the older `"...failed after <max>
attempts (<error>)"` wording) — picked up by the same `budget_left() == False` → `state: aborted` path task
059 uses, so the terminal `reason` names the infra fault plainly (e.g. the
literal `getaddrinfo ENOTFOUND` text, when the failure surfaced one) rather
than a generic timeout message. Since task 004 (#11) the same verdict is also
*recorded* per iteration as `faultClass` (§10.1).

**5. Since v0.5.** Each wait is interruptible (`POST /retry`, §10.4), extends
the job deadline by the seconds waited (§10.2), and publishes
`health: "degraded"` + `infraWait` (§10.3); an operator-initiated abort is
never an infra fault (§10.1's `aborted` carve-out).

A traceability row for this requirement belongs alongside task 013's
requirement→test table (see `artifacts/reports/traceability.md`, built by
that task) once it exists.

### Grace review at budget exhaustion (task 002)

Live incident this closes: a job exhausted its 8-iteration budget with
**all 7 tasks completed** but no review slot ever ran (the last worker
iteration finished the final task and burned the last budgeted iteration
doing it) → terminal `failed/unverified` despite the work being done. The
operator had to `ralphctl resume +3` just to get a review.

**Invariant:** a job whose tasks are all `completed` should get a review
verdict if at all possible, even if the iteration budget has already run
out.

**Design choice:** rather than reserving the final budget slot ahead of
time (which would require the loop to predict exhaustion before it
happens — tricky given the worker loop's own budget check runs *before*
each iteration, not after), the engine grants a single **off-budget**
review iteration at the moment `budget_left()` is discovered to be
`False`, if and only if every task in `tasks.json` is already `completed`.
This is simpler to reason about than slot-reservation bookkeeping earlier
in the loop, and is checked at both places the worker loop can exit with
budget exhausted: the top of the per-approach `for` loop (covers a
resumed run whose tasks were already all completed by a prior process)
and right after the worker `while budget_left()` loop exits (covers the
live incident's exact shape: the final iteration both completes the last
task and exhausts the budget in the same step, whether or not the worker
happened to also emit `COMPLETE` that iteration).

`LoopSupervisor._maybe_grace_review()` implements it: `_grace_review_granted`
(a set of approach numbers) guarantees **at most one grace review per
approach** — never a loop, never a second free review. The review
iteration itself increments `iterations_used` (gets its own iteration
directory/number) exactly like any other iteration, but also increments
`_grace_refunded`, which `budget_left()` subtracts alongside
`_infra_refunded` — the same refund mechanism task 001a uses for
infra-retry attempts — so it never counts against `cfg.iterations`. If the
grace review comes back `VERIFIED` (and no operator steering is left
pending unconsumed), the job goes terminal `succeeded`/`verified` with
`status.json`'s `graceReview: true` and a `reason` stating the grace
review ran and verified. If it does not verify, the job still ends
failed/aborted exactly as it would without this feature — no second
approach is attempted once budget is exhausted — except the terminal
`reason` now says a grace review ran and did not verify (via
`_terminal_reason_note`, kept separate from `_abort_reason` so a plain
unsatisfied grace review doesn't relabel the terminal `state` from
`failed` to `aborted`). The negative case (tasks NOT all completed when
budget exhausts) is unaffected: `_maybe_grace_review()` returns
immediately without touching anything, and the job fails exactly as
before this feature.

### Model strategy

Each phase resolves its model independently: per-phase override → strategy preset →
job default. Presets: `quality-first` (default; one strong model everywhere),
`cost-optimized` (strong model for planning only), `balanced` (strong for planning +
review). "Strong" and "fast" tiers are just two model IDs in job config — any model
pi can reach is valid in either slot. The engine sets the model per-iteration via
pi's model selection flag/env on the subprocess.

### Self-reflection phase (PRD req 24)

Optional (`reflect: true` in job config, `ralphctl start --reflect`). After the
job's normal loop (`LoopSupervisor._run_job_core()`) reaches a terminal state
(`succeeded`/`failed`/`aborted`), the engine runs exactly one extra `reflect`
iteration (`LoopSupervisor._run_reflection()`), using its own builtin prompt
(`src/ralphd/prompts/reflect.md`, phase name `reflect`, model resolved per the
job's model strategy exactly like any other phase). This iteration is *not* part
of the normal budget accounting gate (`budget_left()`); it always runs once, even
if the iteration budget was already exhausted when the job reached its terminal
state.

The reflect prompt instructs the agent to analyze the run's PRD, final
`tasks.json`, `notes.md`, and iteration records, and write a report plus an
optional unified diff of proposed prompt/skill improvements to
`artifacts/reflection/` — and to touch nothing else (workspace files, `tasks.json`,
`status.json`, `notes.md`, `review-findings.md`, steering files, or any
`iterations/` content). This is instructed, not sandboxed: the reflect iteration
runs with the same tool access as any other phase, so it is trusted the same way
review/verify iterations are trusted to follow their own prompts. The one
engine-side guarantee is that `run_job()`'s returned final state and the job's
terminal `status.json` fields (`state`, `verdict`, `endedAt`) are set *before*
the reflect iteration runs and are never touched by it; the engine does reset
`status.json`'s `phase` field back to `None` immediately after the reflect
iteration finishes (it's transiently set to `"reflect"` while it runs, the same
as every other phase), so a terminal job never appears to still be mid-phase.

With `reflect` absent/`false` (the default), no extra iteration runs at all —
`run_job()` is a no-op wrapper around the same core loop as before this feature.

## 3. State model

All run state lives in `/run` inside the container, which is **always a host
bind-mount** of `~/.ralphd/runs/<run-id>/`. This is what makes history survive the
container and lets the CLI read state even when the container is dead.

```
~/.ralphd/runs/<run-id>/          # mounted at /run in the container
├── job.json            # immutable job config as launched (redacted: no secrets)
├── status.json         # engine-maintained: phase, iteration, approach, verdict
├── prd.md              # original PRD
├── composite-prd.md    # approach ≥2 only
├── tasks.json          # task state (source of truth)
├── .tasks-last-good.json # fallback copy, written ONLY when a read of tasks.json fails
├── notes.md            # agent handoff notes, ≤50 lines enforced by prompt
├── review-findings.md  # written by failed reviews
├── steering/           # steering inbox: 001-*.md, 002-*.md …
├── iterations/         # per-iteration record
│   └── 0007/
│       ├── meta.json   # phase, model, start/end, exit code, signal seen, usage
│       └── output.jsonl# full agent transcript (pi session log)
├── approaches/         # archived tasks.json/notes.md per finished approach
├── artifacts/          # anything the agent is told to persist (reports, screenshots)
└── events.jsonl        # append-only event log (also fed to the SSE stream)
```

`tasks.json` schema (v1):

```json
{
  "version": 1,
  "runId": "brisk-otter-1408",
  "goal": "one-line goal distilled from the PRD",
  "scope": {"level": "single-repo", "reasoning": "..."},
  "repositories": ["https://github.com/acme/widget"],
  "tasks": [
    {
      "id": "001",
      "title": "Add JWT middleware",
      "status": "pending|in-progress|completed|validation-failed|failed|skipped",
      "successCriteria": "natural-language, independently checkable",
      "validationNotes": "present when validation-failed",
      "validationAttempts": 0,
      "dependsOn": ["000"],
      "priority": 0
    }
  ],
  "discovered": {}
}
```

`dependsOn` (list of task ids) and `priority` (number, higher = more
important) are OPTIONAL per task, added by the planner only when the plan
genuinely needs cross-task ordering or prioritisation (see
`prompts/planning.md`). The worker's pick rule (`prompts/worker.md`): among
`pending` tasks whose `dependsOn` are all `completed`, pick the highest
`priority` (missing = 0), ties broken by list order; a task blocked by a
`failed`/`skipped` dependency is never silently ground against — the worker
notes the blockage in the handoff notes file and moves on. With no
`dependsOn`/`priority` fields anywhere in the plan this is identical to
plain sequential list-order picking. This rule lives entirely in the phase
prompts (the engine does not parse or enforce it in code) — it is the agent
reading `tasks.json` and the prompt's instructions, same as task selection
always has been.

### Reading `tasks.json`: unknown is not zero (task 002, #15)

`tasks.json` is written by the **agent** (`pi`, per `prompts/worker.md`), never
by the engine, so no reader may assume an atomic write: a poll landing inside
the agent's rewrite window reads a truncated file. Turning that
`JSONDecodeError` into the reader's default — what every surface used to do —
is how a whole plan briefly vanished from the hub table and the `tasks: n/m`
counters went to zero for one poll cycle. Unknown is not zero; this is the same
principle `format_cost` applies to money.

One hardened read path therefore serves every surface
(`state.read_tasks_doc(run_root) -> TasksRead`), used by `RunDir.read_tasks()` /
`RunDir.read_tasks_result()`, `GET /tasks`, the `/status` task counts, the hub
server and `ralphctl tasks`:

1. **Bounded re-read.** A parse failure is retried a few times ~10 ms apart
   (`TASKS_READ_ATTEMPTS`/`TASKS_READ_DELAY`, ~30 ms total) — long enough to
   outlast the write window, short enough never to stall a request.
2. **Last-good fallback.** If it still will not parse, the last payload that
   *did* parse is served with `stale=True`.
3. **A three-way result**, so the three situations that collapsed into one
   default stay distinguishable:

| `TasksRead.source` | means | `stale` |
|---|---|---|
| `absent` | no `tasks.json` yet — an empty plan is the truth | no |
| `file` | parsed off disk (an empty `tasks` list here is also the truth) | no |
| `last-good` | unparseable; serving the previous payload | **yes** |
| `unreadable` | unparseable and no last-good exists — emptiness is ignorance | **yes** |

`TasksRead.tasks` is always a list, `.counts` is `task_counts()` over it, and
`.present` says whether a `tasks.json` exists at all (so a surface can tell
"0 tasks" apart from "no plan").

**Why this is a fallback and not a mirror.** The last-good payload lives in
process memory; the on-disk copy (`<run-dir>/.tasks-last-good.json`) is written
**only at the moment a read actually fails**, never on the happy path. So the
engine never maintains a second copy of the plan that could drift from
`tasks.json` or be mistaken for it — the cache exists purely so the fallback
survives an engine restart, and everything in it was read verbatim out of
`tasks.json`. Read-only viewers of somebody else's run dir pass `persist=False`
and write nothing at all. The two alternatives were considered and rejected: an
engine-maintained atomic mirror is a second source of truth, and telling an LLM
to write atomically is unenforceable (readers would still have to cope).

The **workspace** (`/workspace`) is separate from `/run`: it is the repo checkout the
agent edits. Two modes, chosen per job:

- `--workspace <host-dir>` — bind-mount an existing checkout (preferred; the
  operator's normal working copy or a dedicated clone)
- no workspace flag — the engine creates `/workspace` on the run dir
  (`runs/<id>/workspace/`) and the planning iteration clones the PRD-listed repos
  using whatever git credentials were injected

**Multi-workspace jobs** (PRD req 27): `--workspace` is repeatable. A single
bare `--workspace <dir>` keeps the single-mode behavior above (mounted at
`/workspace`). Two or more `--workspace` flags each require a `:name`
(`--workspace <dir>:<name>`) and are mounted at `/workspace/<name>` side by
side instead — one job container, several checked-out repos. The container
gets `RALPHD_WORKSPACES=<comma-separated names>`, which every phase prompt's
"Job context" section reads to list each mounted name/path explicitly, so
the agent never has to guess what's under `/workspace` from a directory
listing. `host.json` records the mapping as `workspaces` (name→host path)
instead of the single-mode `workspace` key, and `ralphctl resume` remounts
every named workspace the same way on the fresh container.

## 4. Container engine

Single Python process (`ralphd.engine`), PID 1 in the container, two concerns:

1. **Supervisor** — an asyncio task running the loop: builds each iteration's
   prompt, spawns `pi` with the right env (model, workspace cwd), streams its
   output to `iterations/N/output.jsonl`, scans for sentinels, updates
   `status.json`/`events.jsonl`, applies steering, enforces budgets and timeouts.
2. **API server** — FastAPI on `:7777` (uvicorn in the same process), serving the
   endpoints in [api.md](api.md). Reads state files; writes only to the steering
   inbox, config drop-ins, and the control channel to the supervisor (pause /
   interrupt / abort / resume).

Because both share a process, "interrupt" is a plain `SIGINT` to the `pi` child
process group; the supervisor records the iteration as interrupted and proceeds to
the next one (which will see any newly arrived steering).

### Diagnosability requirements

Failures must be visible without digging into transcripts:

- **Agent-level errors surface upward.** When an iteration's agent reports an
  error (pi `message_end` with `stopReason: "error"`), the engine records the
  `errorMessage` in the iteration's `meta.json`, in the `iteration.end` event,
  and as an error-level `log` event. A job that failed because of provider/auth
  errors must say so in `/status`-adjacent surfaces, not just exit silently.
- **`docker logs` is informative on its own**: the engine logs each iteration's
  start (phase, model) and end (exit code, sentinels, error summary) to stdout,
  so a dead-in-2-seconds job is diagnosable from container logs alone.

### Live job log (pretty console)

The per-iteration transcripts are raw pi NDJSON — correct as a record, unreadable
as a console. The engine therefore exposes a **whole-job log view** that spans
iteration boundaries (`GET /logs`, see [api.md](api.md)): a merged stream of every
iteration's transcript in order, with iteration/phase boundary markers, that can
be fetched as a bounded tail or followed live across iterations (no re-attach
dance when a new iteration starts). The engine serves **raw NDJSON**; making it
pretty is the CLI's job:

- `ralphctl logs` renders the stream human-first by default, Jenkins-console
  style: iteration/phase headers, assistant text as it streams, tool calls as
  one-liners (name + compact args + outcome), thinking elided to a marker,
  per-iteration usage/cost footers, agent errors highlighted.
- `--raw` emits the underlying NDJSON untouched (for machines and debugging).
- The syntax follows `tail`: `logs -100` (last 100 rendered lines), `-150f`
  (last 150, then follow), `logsf` (follow from now). See [cli.md](cli.md).

The renderer is pure presentation over the same events — nothing is stored
pretty; the run dir stays raw and replayable.

### Container lifecycle

```
created ─ starting ─ running ─┬─ succeeded ─┐
                              ├─ failed ────┼─ exit (default) — container exits
                              └─ aborted ───┘        └─ or idle (on_complete: idle, debugging opt-in)
```

- `on_complete: exit` (default) — container exits with 0 (succeeded) / 1
  (failed/aborted). Suits scripted/batch use; state remains in the run dir
  either way.
- `on_complete: idle` — engine stays up, API remains queryable, agent is
  never spawned again. An explicit debugging opt-in: the operator collects
  outputs / post-mortems, then `ralphctl stop`.

### Completion hook (PRD req 26)

Optional `on_complete_cmd: <shell command>` job option (`ralphctl start
--on-complete-cmd '<cmd>'`, or a job.yaml field a template can supply). The
engine runs the command exactly once, in-container, via `asyncio.create_
subprocess_shell`, strictly after the job has reached a terminal state
(`succeeded`/`failed`/`aborted`) — including after the reflect iteration
(PRD req 24) when `reflect: true`, since both share the single point in
`ralphd-engine`'s `amain()` right after `loop.run_job()` returns. The hook
receives the process's own environment plus three vars: `RALPHD_RUN_ID` (the
job's configured run id), `RALPHD_STATE` (the final state string), and
`RALPHD_VERDICT` (`status.json`'s `verdict`, `"verified"`/`"unverified"`, or
empty string if somehow absent). A nonzero exit, or the command failing to
spawn at all, is recorded as an `events.jsonl` `log` event at `level: error`
(with a tail of stderr/stdout) — it is purely observational: the hook can
never change the job's `state`/`verdict` or the engine process's own exit
code. With `on_complete_cmd` unset (the default), nothing extra runs.

A **job timeout** (wall clock, default 8h) and per-iteration timeout (default 45m)
bound runaway runs; hitting either aborts the current iteration and, for the job
timeout, fails the job.

### Steering

Steering files are markdown notes dropped into `steering/` (via API or by writing
the mounted dir directly). At each iteration start the engine checks for
*unconsumed* steering files, but only **actionable** phases -- `planning` and
`worker`, whose prompts explicitly instruct the agent to act on operator
guidance -- include the full steering text and mark it consumed (recorded in
`meta.json`'s `steeringConsumed` and in `steering/.consumed.json`). The
`review` and `verify` phases are pure verification roles ("Do NOT fix
anything yourself"; nothing in their prompts tells the agent to act on
steering), so if a steering file arrives while a worker iteration is in
flight and the very next iteration boundary happens to be a review or verify
iteration, that iteration only sees a passive notice (file names, no content,
not marked consumed) and leaves it pending -- it is picked up and consumed by
the next planning/worker iteration instead. This prevents steering from being
silently discarded (recorded as "consumed" yet never actually acted on) when
it happens to land just before a non-worker-bound phase.

**A `VERIFIED` verdict is refused while steering sits unconsumed.** Passive
notice at review time is not enough on its own: if a steering file arrives
while the worker is in flight and the very next iteration boundary is the
review that would otherwise end the job, the engine must not let the run go
terminal-succeeded with that steering permanently stranded (a terminal run
never reads pending steering again). So `_run_job_core` checks
`Run.pending_steering()` after every `VERIFIED` review verdict: if steering is
still pending, the verdict is discarded, one more (actionable) worker
iteration runs to consume it, and the approach is re-reviewed. Only a
`VERIFIED` verdict observed with no pending steering left is allowed to end
the job successfully.

**Any terminal state still surfaces unconsumed steering, belt-and-braces
(task 006).** The `VERIFIED`-refusal above closes the most common silent-drop
window, but other terminal paths -- an aborted run, a run that exhausts its
iteration/approach budget while a worker-bound iteration never comes back
round, or an unhandled engine error -- can still end with steering files
that were accepted (`POST /steering` returned `202`) but never marked
consumed. Every terminal `update_status(...)` call in `_run_job_core`
(succeeded, failed, and aborted alike) is patched with
`_unconsumed_steering_patch()`, which adds an `unconsumedSteering` field to
`status.json` listing the basenames of whatever `Run.pending_steering()`
still returns at that instant (empty list in the common, fully-consumed
case). This is a *reporting* guarantee, not a consumption one: it does not
retry or re-open a terminal run, it just makes the stranded-steering fact
impossible to miss from `status.json` alone -- surfaced further by
`ralphctl status` (a loud warning line, not just a `--json` field) and the
hub run-detail view (a `.steering-warning` banner).
`POST /interrupt` gives the "right now" variant: SIGINT the current iteration so the
next one starts immediately with the new guidance. Steering is guidance for the
agent; it does not mutate `tasks.json` directly.

### Self-protection

`ralphd-engine` is defensive about being invoked accidentally against a run dir
that is already live (a bare `ralphd-engine --help`, or a stray second process
pointed at the same `RALPHD_RUN_DIR`, must never double-write `events.jsonl` /
`status.json` into a running job):

- **`--help` / `--version` are argument-parsed up front** (`argparse`, in
  `build_arg_parser()`) and exit `0` via `SystemExit` *before* `amain()` runs —
  no config load, no directory creation, no server, no lock. This makes them
  safe to run bare, with no `RALPHD_*` env, from any cwd.
- **At startup the engine takes an exclusive, non-blocking `flock` on
  `<run-dir>/.lock`** (`RunDir.acquire_lock()` in `engine/state.py`). If another
  live engine already holds it, the new process prints a diagnostic naming the
  run dir to stderr/log and exits **`3`** (`EXIT_RUN_DIR_LOCKED` in
  `engine/main.py`) without touching any other state file; the holder is
  unaffected and keeps serving. The lock file records the holder's PID for
  diagnosis but the flock itself (not the PID content) is what's authoritative.
  Because `flock` is process-lifetime (kernel-held, not a lock file whose mere
  *existence* is checked), it is released automatically on any process exit —
  including `SIGKILL` — so a killed engine never leaves a stale false-positive
  lock behind; a fresh engine started immediately after can acquire it.

### Run-dir schema version (PRD req 18)

`status.json` carries a `schemaVersion` integer, stamped by the engine on every
startup (`RunDir.check_schema_version()` + the `update_status(...,
schemaVersion=CURRENT_SCHEMA_VERSION)` call in `engine/main.py`). It tracks the
run-dir *on-disk shape* (files/fields the engine relies on), not the job or
software version. Policy, checked immediately after the run-dir lock is
acquired and before anything else in the run dir is touched (no workspace
creation, no PRD copy, no `status.json`/`tasks.json` write):

- **Recorded version newer than this engine build's `CURRENT_SCHEMA_VERSION`**
  (an older engine pointed at a run dir a newer engine already advanced) →
  refuses to start: prints a diagnostic naming *both* versions to stderr/log
  and exits **`4`** (`EXIT_SCHEMA_TOO_NEW` in `engine/main.py`), touching
  nothing else.
- **Recorded version older than current, or absent** (a pre-schema run dir
  from before this feature existed reads as version `0`) → accepted; the
  engine proceeds normally and stamps `schemaVersion: CURRENT_SCHEMA_VERSION`
  into `status.json` on its first status update. There is currently only one
  schema version (`1`), so "upgrading" an older run dir is just this stamp;
  the check exists so a future on-disk shape change has a documented,
  enforced upgrade/refusal point to hang real migration logic off.
- **Recorded version equal to current** → no-op, same stamp is rewritten.

### Engine resume-from-existing-state (PRD req 16, engine side)

A container is disposable; the run dir is not. `ralphd-engine` can always be
started fresh over an *existing* run dir (same mounted `<run-dir>`, same or
adjusted `job.yaml` iteration budget) and picks up where the previous process
left off, instead of re-planning from scratch. This is what backs
`ralphctl resume <run-id>` (task 029, CLI side) and recovery after a killed
container (task 030, crash-consistency), but the detection and resumption
logic itself lives entirely engine-side and needs no CLI/API cooperation.

- **Iteration numbering.** `LoopSupervisor.__init__` seeds
  `self.iterations_used` from `RunDir.max_iteration_number()` -- the highest
  `iterations/<NNNN>/` directory whose `meta.json` already has an `endedAt`
  field (a genuinely *completed* iteration; a half-written directory left by
  a process killed mid-iteration is not counted and its number is reused).
  On a fresh run dir this is `0`, so numbering is unaffected; on a run dir
  with `N` completed iterations already on disk, the very next iteration is
  numbered `N+1`. This one seed is what makes numbering monotonic across
  restarts with no other change needed.
- **Skipping planning.** `LoopSupervisor._resume_point()` decides where
  `run_job()` starts: if `tasks.json` already has tasks *and* at least one
  completed iteration is on disk, it resumes the approach recorded in
  `status.json`'s `approach` field (that field persists across restarts --
  `update_status()` merges, it never resets fields it isn't given) and skips
  straight into the worker loop for that approach, emitting a `log` event
  ('resuming existing run-dir state: approach N, K iteration(s) already
  recorded; skipping planning') so the resume is operator-visible. A
  genuinely fresh run dir (no tasks yet, or `max_iteration_number() == 0`)
  is completely unaffected -- planning still runs first, exactly as before.
- **Budget accounting.** Because `iterations_used` starts from the prior
  count, `budget_left()`'s `iterations_used < cfg.iterations` check
  automatically reflects prior usage -- a bumped `iterations` value in the
  freshly-loaded `job.yaml` (the CLI-side top-up) is *remaining* budget on
  top of what was already spent, not a brand new allowance. The wall-clock
  `job_timeout_s` deadline, by contrast, is a fresh per-process guard
  (`self.deadline = time.monotonic() + cfg.job_timeout_s` in `__init__`) --
  each container invocation gets its own timeout window, it does not
  accumulate across restarts.
- This also covers resuming a *terminal* (e.g. `failed`/`aborted`) run dir
  after the operator increases the iteration budget and restarts: the same
  detection (`tasks.json` has tasks + completed iterations exist) applies
  regardless of what `state` the previous process left in `status.json`,
  since that field is unconditionally overwritten to `"running"` at the top
  of `run_job()` before the resume decision is made.
- Tests: `tests/test_resume.py` (no Docker, real `ralphd-engine` twice over
  the same run dir via `test_e2e.py`'s `engine_factory` fixture).
- **Vigilant-mode verification across a crash (task 052).** Resuming into an
  existing approach's worker loop (skipping planning) does not, by itself,
  re-derive which tasks still need a verify iteration from an in-process
  diff -- see "Crash/resume-safe tracking" under Vigilant mode above.
  Instead every pass through the worker loop (including the very first one
  after a resume) re-reads `<run-dir>/vigilant-verified.json` and verifies
  any currently-`completed` task not yet in it, so a task whose completion
  survived a crash but whose verify iteration never ran (or never finished)
  still gets verified before the job can reach a terminal verdict. Tests:
  `tests/test_vigilant_crash_resume.py` (SIGKILL a specific PID after a
  task is durably `completed` but its verify iteration's `meta.json` has
  `startedAt` and no `endedAt`; resume proves a `taskVerified` signal and a
  passing verify iteration for that task eventually appear).

## 5. Configuration & injection

Everything the job needs is mapped in by the CLI. Three mount points:

| Mount | Contents | Writable at runtime |
|-------|----------|--------------------|
| `/run` | run state (always host-mounted) | engine-owned |
| `/workspace` | code | agent-owned |
| `/config` | prompts overrides, skills, creds, pi settings | via API (`/config/*`) |

`/config` layout:

```
/config/
├── job.yaml            # job config (PRD ref, budgets, mode flags, model strategy)
├── prompts/            # optional phase-prompt overrides
├── skills/             # agent skills, exposed to pi as workspace skills
├── creds/              # operator-prepared credential env files + extras
│   ├── github.env      # KEY=value lines → placed at ~/.creds/github.env (0600)
│   ├── jenkins.env     # …one file per credential set, any names
│   └── setup.sh        # optional: run once before first iteration
└── pi/                 # pi settings fragments (models.json, provider config)
```

- **Skills**: directories of instruction files (`SKILL.md` convention), forwarded
  **explicitly and scoped per job** — `ralphctl start --skills <dir>` (repeatable),
  one directory per skill. There is deliberately no "forward all host skills"
  mode: the operator picks exactly what a job needs. Each named dir is *copied*
  into the job's config dir at start (so later host edits don't leak into a
  running job), mounted at `/config/skills/<name>`. **The engine itself**
  (`engine/skills.py:place_skills()`, run at startup from `engine/main.py`,
  before the job loop starts — not the container entrypoint script, mirroring
  the creds discipline below) symlinks the effective skill set into the
  location pi discovers skills from (`~/.pi/agent/skills/`).
  Gotcha: `--skills` treats its argument as *one* skill — passing a parent
  directory of many skills forwards it as a single mis-named skill. `ralphctl`
  rejects a `--skills` dir that has no `SKILL.md` unless every immediate child
  has one, in which case it expands to the children.
  Skills are full CRUD at runtime via the API (`GET/PUT/DELETE
  /config/skills/{name}`, tar bodies) — `PUT`/`DELETE` land in the writable
  overlay (`<overlay>/skills/<name>/`, an "api"-origin skill always wins over
  a same-named mounted one; `DELETE` writes a tombstone in
  `<overlay>/skills-deleted/<name>` so a mounted skill of that name isn't
  resurrected) and call `place_skills()` again immediately, so the mutation
  is live for the very next iteration without a container restart.
- **Credentials — env-file convention.** This mirrors the original Ralph's AWS
  Secrets Manager approach, made file-based: every credential set the job needs
  is prepared by the operator as one `<name>.env` file (`KEY=value` lines, `#`
  comments), e.g. `github.env`, `jenkins.env`, `sonarqube.env`. `ralphctl start
  --creds <dir>` copies `<dir>/*.env` into the job config; **the engine itself**
  (`engine/creds.py:place_creds()`, run at startup from `engine/main.py`, before
  the job loop starts) places them at **`~/.creds/*.env`** (agent-owned, mode
  `0600`) inside the container -- not the container entrypoint script, so that
  secret handling stays inside the same process that already guarantees
  values never reach `/run`, `events.jsonl`, stdout, or `job.json` (only file
  *names* are ever logged).
  **The agent knows where to look**: every phase prompt lists the available cred
  file names and the usage rule — *source the file you need, in the shell where
  you need it* (`set -a; . ~/.creds/github.env; set +a`). Values are **not**
  auto-exported into the engine or agent process environment; a credential is
  visible only to commands that explicitly source its file. Recognized
  non-env extras keep their conventional placement (`gitconfig`,
  `git-credentials`, `netrc`, `ssh/`), and an executable `setup.sh` still runs
  once at container start as the escape hatch. Nothing credential-shaped is
  ever written to `/run` (which is host-visible history) or logged.
  **The prompt-level rule that makes this hold in practice**: every tool
  call's arguments and stdout land verbatim in the run's iteration
  transcript, so the creds note (`loop.py:_creds_note()`) and the worker
  prompt's dedicated "Credential handling" section both explicitly forbid
  printing/`cat`-ing/`echo`-ing a credential file's contents or pasting a
  secret value into a command's arguments (query strings, `--token` flags,
  inline `Authorization:` headers) — either would permanently persist the
  secret in that transcript. The sanctioned pattern is exactly `set -a; .
  ~/.creds/<name>.env; set +a` followed by letting the tool read `$VARNAME`
  from its own environment. The same guidance forbids token-bearing git
  remote URLs (`https://<token>@host/...`, which leaks via `git remote -v`
  and `.git/config`) in favor of a credential helper / `~/.git-credentials`.
  Creds are full CRUD at runtime via the API (`GET/PUT/DELETE
  /config/creds/{name}`) — including read-back of values, an explicit design
  choice: **holding the API bearer token is defined as equivalent to holding the
  job's credentials** (see §6). Prompts see updated inventories at the next
  iteration.
- **LLM config**: env vars + pi config fragments produced by the CLI's LLM-profile
  resolution — see [llm-profiles.md](llm-profiles.md). The engine merges
  `/config/pi/*` into the container-local pi settings at startup and again on API
  update.

### Writable config overlay

In real containers `/config` is mounted **read-only** (`ro`) from the host, by
design: the operator-provided config is immutable job input, and the run dir
(`/run`) must never carry credential-shaped content. But the runtime
config-CRUD API (`/config/*` PUT/DELETE routes — skills, creds, prompts, llm)
needs *somewhere* writable to land mutations that must survive for the rest
of the job and be visible to the next iteration.

That somewhere is a **container-local writable overlay**,
`$HOME/.ralphd/config-overlay/` by default (override via
`RALPHD_CONFIG_OVERLAY_DIR`) — deliberately neither `/config` (read-only, and
host-visible input shouldn't be mutated by the running job) nor the run dir
(host-visible history; also where creds must never appear). It lives entirely
in the container's own writable filesystem layer and disappears with the
container.

Every config-relative read goes through a single resolution order, implemented
once in `engine/config.py`:

1. `$HOME/.ralphd/config-overlay/<rel>` — a runtime mutation via the API, if any.
2. `/config/<rel>` — the operator-mounted (possibly read-only) config.
3. A builtin default, if the caller has one (e.g. `src/ralphd/prompts/*.md`).

`engine/config.py:overlay_or_config(rel)` implements steps 1–2;
`overlay_write_path(rel)` is what API handlers call to get a writable
destination for step 1, creating parent directories as needed. The one
running example today is prompt overrides: `PUT /config/prompts/{name}`
writes to the overlay, and `LoopSupervisor.prompt_text()` resolves through
the order above on every iteration, so an override is effective starting with
the next iteration that builds that phase's prompt — without ever touching
the read-only `/config` mount. Skills CRUD follows the same pattern (see
above); creds/llm CRUD are a later milestone.

### Security: mechanical secret redaction

Prompt-level guidance (the creds note's "never `cat`/`echo`/paste a secret"
rule, task 049) is necessary but not sufficient: it has already failed twice
in this project's own self-hosted run — a worker `cat`-ed
`~/.git-credentials`, and a separate iteration ran `docker inspect` on the
*production* engine container and got back its real `AWS_BEARER_TOKEN_BEDROCK`
in the output. Both happened *after* the prompt guidance already existed, so
the engine also enforces this mechanically (`engine/redact.py`), the same
principle as Jenkins credential masking: scrub every known secret *value*
from everything the engine persists or serves, regardless of how an agent's
command happened to produce it.

**Redaction-set sources** (rebuilt fresh at engine startup, and again after
any `PUT`/`DELETE /config/creds/{name}` or `PUT /config/llm` — never
persisted, never returned by any API route, in-memory only for this process's
lifetime):
- process/LLM-forwarded env vars whose *name* matches `TOKEN`/`KEY`/`SECRET`/
  `PASSWORD` (case-insensitive) or is a known LLM provider var (e.g.
  `ANTHROPIC_API_KEY`, `AWS_BEARER_TOKEN_BEDROCK`) — covers both the
  process's own environment and any `PUT /config/llm` env override;
- values parsed best-effort from placed creds files: `~/.creds/*.env` (`KEY=
  value` lines), `~/.git-credentials` (the URL password), `~/.netrc`
  (`password` fields). Unparseable files are simply skipped.

**Scrub points** (defense-in-depth, several independent layers): every line
of a `pi` subprocess's stdout is scrubbed before being appended to that
iteration's `output.jsonl` (`runner.py`); every event is scrubbed as its
serialized JSON text before being appended to `events.jsonl`
(`state.py:RunDir.emit()`); `GET /logs` (both tail and follow modes) scrubs
again as it serves content, so a value only *recognized* as a secret after a
transcript line was originally written (e.g. a cred added mid-run) is still
caught retroactively when served.

**Length floor**: only values at least 8 characters long are ever redacted,
so short/common substrings (region codes, single words) are never mangled —
deliberately trading a narrow gap in coverage for never corrupting ordinary
transcript text.

**Memory-only guarantee**: the redaction set itself is exactly as sensitive
as the secrets it holds, so it is never written to disk (not even under the
writable overlay) and no API route (`GET /config`, `GET /config/creds`,
etc.) ever exposes it — those routes already only ever return credential
*names*, never values, and that discipline is unchanged by this feature.

**Host-side on-disk reads: write-time scrubbing is the guarantee** (decided
in v0.5, task 042). Since v0.5 the transcript merge is a shared module
(`src/ralphd/log_merge.py`) that host-side surfaces read *directly
from the run dir* when the engine's container is gone: `ralphctl logs`
(snapshot mode) and the hub's log tail (`ui_server.rendered_log_lines`,
`live: false`). Those readers run outside the engine process, so scrub
point 3 (`GET /logs` re-scrubbing at serve time) is not available to them.
The decision is to **accept write-time-only scrubbing there, and NOT to
persist the redaction map** — because:

- the redaction map is exactly as sensitive as the secrets in it, so
  writing it into the run dir to enable a second pass would place every
  secret value on disk in plaintext, next to the transcript it is meant to
  protect — strictly worse than the gap it closes, and it would break the
  memory-only guarantee above;
- the bytes an on-disk reader sees were *already* scrubbed before they were
  written (`runner.py` for `output.jsonl`, `state.py:RunDir.emit()` for
  `events.jsonl`), so a snapshot is no less scrubbed than the file itself;
  `log_merge` therefore takes its `scrub` callback as an injected argument
  (the engine passes `redact.scrub_text`; host-side callers pass nothing)
  rather than pretending to redact host-side.

**Consequence for hub/CLI snapshot output**: a snapshot inherits exactly the
write-time redaction set — the values the engine knew about *when the line
was written*. It does not get the retroactive catch that `GET /logs`
provides for a value only recognized as a secret later (e.g. a cred added
mid-run via `PUT /config/creds/{name}`, after a transcript line already
quoted it). While the run's container is alive, both surfaces read through
`GET /logs` and are covered; only a dead run's snapshot can expose that
narrow window. The bound is asserted by tests/test_secret_redaction.py,
which renders the same fixture run dir through the host-side on-disk merge
and the hub's fallback and requires no unscrubbed secret literal in either.

**Non-goals** (recorded as roadmap notes, not implemented here — see
[roadmap.md](roadmap.md)): PID-namespace isolation of agent iterations from
in-container kill signals, and a `ralphctl repair` command for hand-fixing
corrupted run state.

## 6. API security

- Default: container port published to `127.0.0.1` only, **no auth**.
- Optional: `--api-token <t>` (or auto-generate with `--api-token auto`) makes the
  engine require `Authorization: Bearer <t>` on every route; combine with
  `--api-bind 0.0.0.0` for LAN/remote access. The CLI stores the token in the run
  registry (`~/.ralphd/runs/<id>/.api-token`, mode 0600) and sends it automatically.
- `job.json` is stored redacted (no secret values). The creds API, however, is
  full CRUD **including read-back** — by design, the API bearer token *is* the
  job's credential boundary. Protect it accordingly: keep the default
  loopback-only bind unless the token is set, and treat `--api-bind 0.0.0.0`
  + token as granting whoever holds the token everything the job can do.

### Host-network jobs (`--network host`, task 007)

`ralphctl start --network host` (and the `ralphctl config set network host`
registry-wide fallback, §7) puts the job container into the host's network
namespace instead of the default bridge network. Use it when the job's
workload needs to reach host-only, VPN, or tailnet services that aren't
routable from a bridged container — e.g. an internal LLM gateway only
reachable on the host's tailnet interface, or a database bound to
`localhost` on the host outside docker entirely. Prefer the default bridge
network (no `--network`) whenever the job's own network needs are already
satisfiable through it; `host` is a wider trust boundary and should be
opted into deliberately, per job.

**Mechanism.** Docker's `-p`/port-publish flags are meaningless once a
container shares the host's network namespace (docker ignores them with a
warning), so `--network host` swaps that mechanism out for two env vars the
CLI injects instead of a `-p` flag (see `_network_args` in
`src/ralphd/cli/main.py`):

- `RALPHD_PORT` — the host TCP port the engine's HTTP API should listen on
  directly (chosen the same way as the bridge-network case: an explicit
  `--port`, or an ephemeral free port otherwise).
- `RALPHD_BIND` — the address the engine binds that port on, taken directly
  from `--api-bind` (default `127.0.0.1`).

The engine reads both at startup and binds accordingly; every other aspect
of the job (container lifecycle, run-dir schema, resume) is unaffected by
which network mode was used to reach the API.

**Security posture: the API bind address is the only boundary.** With the
default bridge network, docker's own `-p 127.0.0.1:<port>:7777` publish
rule is a second layer of isolation independent of what the engine binds
to internally. `--network host` removes that layer entirely — the
container's listening socket *is* a socket on the host's own network
stack, reachable by anything that can reach the host on that port. This
makes `--api-bind` (→ `RALPHD_BIND`) the *only* thing standing between the
job's HTTP API and the network, exactly as if the engine process ran
directly on the host outside any container:

- Leave `--api-bind 127.0.0.1` (the default) unless the job genuinely needs
  the API itself reachable from elsewhere — `--network host` alone does not
  widen who can reach the API; only combining it with a non-loopback
  `--api-bind` does.
- If the API must be reachable beyond loopback on a host-network job, set
  `--api-token` (or `auto`) too — the same rule as the bridge-network case
  in §6, just with a narrower safety net (no docker-level port isolation to
  fall back on if the bind address is misconfigured).
- `ralphctl doctor` (task 006) proactively flags this: when the
  configured/requested network is `host`, its report notes that the API
  binds `--api-bind` directly with no docker port-publish isolation, so an
  operator auditing a run's exposure sees the caveat without having to
  reconstruct the reasoning above.

### Docker socket opt-in (`--allow-docker`)

By default the job container has **no docker socket** (§8) — that stays the
baseline. `ralphctl start --allow-docker` is an explicit, per-job trust
escalation for PRDs that need to build/run containers (integration tests,
image builds):

- **Trust model: root-equivalent.** The mounted socket lets the job
  `docker run --privileged -v /:/host …` and own the machine; the non-root
  `agent` user and any container hardening are irrelevant once it holds the
  socket. There is no partial-trust variant (socket proxies that allow `run`
  at all still allow arbitrary mounts). ralphctl prints a loud warning at
  launch; use only with PRDs you trust as much as your own shell.
- **Mechanics.** ralphctl mounts the socket (default `/var/run/docker.sock`,
  overridable via `RALPHD_DOCKER_SOCK`) and adds the socket's group via
  `--group-add <gid>` (computed at launch — the gid differs per host). The
  containers the job starts are **siblings** on the host daemon, not children.
- **Path translation gotcha.** A sibling's `-v` is resolved by the *host*
  daemon: container-local paths (`/workspace`, `/run/ralphd`) mount as empty
  dirs and the daemon may auto-create them root-owned on the host. ralphctl
  therefore injects the host-side equivalents as env vars —
  `RALPHD_HOST_WORKSPACE` (when a single unnamed `--workspace` was given) or
  `RALPHD_HOST_WORKSPACES` (a name→host-path JSON object, for the
  multi-workspace case above), plus `RALPHD_HOST_RUN_DIR` and `RALPHD_RUN_ID`
  — and the engine appends a "Docker siblings" section to every phase prompt
  telling the agent to use them. (`docker build` contexts are exempt: the
  CLI streams the context itself.)
- **Label + reap lifecycle.** The job container always carries
  `--label ralphd.run=<run-id>` *and* `--label ralphd.role=job` (with or
  without `--allow-docker`), and is told its own identifier via
  `RALPHD_SELF_CONTAINER_ID` (the `ralphd-<run-id>` name `ralphctl` chose for
  it — docker accepts a name anywhere it accepts an id, and unlike the 64-hex
  id it is known *before* `docker run` returns). Prompts instruct the agent to
  label every sibling `ralphd.run=<run-id>` plus `ralphd.role=sibling` and
  prefer `--rm`; the `role` label is what lets sibling cleanup run from inside
  the job without deleting the job container itself. The idiom the prompt (and
  every doc here) teaches is therefore two-filter, never run-label-only:
  `docker ps -aq --filter label=ralphd.run=$RALPHD_RUN_ID --filter
  label=ralphd.role=sibling`. **Never clean up by the run label alone:** the
  one-filter form matches the job
  container too, so `docker rm -f` over it kills the run mid-iteration, loses
  that iteration's work and transcript, and leaves the run dir non-terminal
  (observed on run `deck-phase1`, issue #7). In-container cleanup is optional
  anyway: reaping is `ralphctl`'s job.
  `ralphctl stop` and `ralphctl rm` best-effort `docker rm -f` everything
  matching the label (idempotent, never fails the command) — host-side, run
  label only, deliberately: there the job container *should* go too; `ralphctl doctor`
  reports stray labeled containers whose run id no longer has a registry dir
  (report-only). Anything unlabeled and detached outlives the job — the daemon
  has no parentage notion between a job and its siblings. Reaping is
  container-only: labeled *images* and *volumes* survive `stop`/`rm` and are
  the operator's (or the job's own) cleanup.

### Toolchain in a sibling

The engine image is deliberately thin (§8) and the agent runs as non-root
`agent`, so a job cannot `apt-get install` a missing toolchain. Deriving a
new engine image per job is the heavyweight answer; the general one is
**run the toolchain work in a sibling container with the host workspace
bind-mounted**. This is the standing pattern for anything the image lacks —
Go, Rust, a JDK, tmux, a database — and the phase prompts teach it verbatim
whenever `--allow-docker` is in effect, so a PRD never has to explain it.

Shape (both files live in the *target* repo, not in ralphd, so the setup is
reproducible without the agent):

- `ci/Dockerfile` — a base image plus just that toolchain, built by the job
  itself (`docker build -t <repo>-ci --label ralphd.run=$RALPHD_RUN_ID ci/`;
  build contexts are exempt from the path-translation gotcha above because the
  CLI streams the context).
- `ci/run.sh` — a thin wrapper that runs an arbitrary command in a `--rm`
  sibling with the mounts/user/caches below, labeled
  `ralphd.run=$RALPHD_RUN_ID` **and** `ralphd.role=sibling`.
  `examples/skills/toolchain-sibling/`
  ships a generic one as a mountable skill (`--skills`).

Load-bearing details, each of them a failure mode when omitted:

1. **Host paths only.** Every sibling `-v` source must be the host-side path
   (`$RALPHD_HOST_WORKSPACE` / `$RALPHD_HOST_WORKSPACES`); mounting the
   container-local `/workspace` silently mounts an *empty* directory. This is
   the single most common mistake.
2. **`--user 1000:1000`.** The job container's `agent` and the host user are
   both uid 1000; a default-root sibling leaves root-owned files in the
   workspace that the agent can afterwards neither modify nor clean up.
3. **A named cache volume** for the toolchain's download/build dirs
   (`GOMODCACHE`/`GOCACHE`, `~/.cargo`, `~/.m2`) — without it every iteration
   re-downloads dependencies. Name it after the repo+toolchain
   (`<repo>-gocache`) and leave the run label off it, so it is deliberately
   shared across runs; a per-run volume is acceptable only if the job removes
   it before finishing. Naming or gating a shared volume on `$RALPHD_RUN_ID`
   makes the repo's own `run.sh` fail for every subsequent run — the trade-off
   is "shared and long-lived" vs "per-run and explicitly deleted", never
   "shared but run-id-checked".
4. **Networking.** Siblings get docker's default bridge network and normal
   internet (image pulls, dependency downloads) regardless of the job
   container's own `--network` (which may be `host`). They neither need nor
   should be given the job's LLM gateway access.
5. **Sibling-only cleanup.** Every sibling carries `ralphd.role=sibling` in
   addition to the run label so that mid-run cleanup can exclude the job
   container: `docker rm -f $(docker ps -aq --filter
   label=ralphd.run=$RALPHD_RUN_ID --filter label=ralphd.role=sibling)`.
   Filtering on `ralphd.run` alone matches the job container itself and kills
   the run mid-iteration (§6 above, issue #7); `$RALPHD_SELF_CONTAINER_ID` is
   the id never to touch, and end-of-run reaping is `ralphctl`'s job.

Verified empirically inside such a sibling (not aspirational): Go 1.25
`go build` and `go test` pass; real `tmux` 3.5a on a private `-L` socket
creates sessions and `capture-pane` reads them back; a real bubbletea TUI
spawned in a pty is driven with keystrokes and its rendered frames asserted;
and file visibility between the sibling and the job container's `/workspace`
is bidirectional and immediate.

## 7. Host-side: CLI and registry

`ralphctl` (see [cli.md](cli.md)) is stateless except for `~/.ralphd/`:

```
~/.ralphd/
├── config.yaml         # defaults: image, ports, on_complete, default llm profile
├── llm-profiles/       # named LLM profiles (see llm-profiles.md)
└── runs/<run-id>/      # one dir per job, bind-mounted as /run (above)
```

Run IDs are generated (`adjective-animal-HHMM` style) or supplied with `--run-id`.
`ralphctl runs` lists history by scanning `runs/*/status.json`; live status merges
in the container API when the container is up. `ralphctl watch` renders a TUI:
task table, current phase/iteration, tail of agent output, budget gauge.
`ralphctl ui [--port N]` (PRD reqs 21-22) serves the same registry over a
stdlib-only HTTP server (`http.server`; no `fastapi`/`uvicorn` on this path):
`GET /api/runs` (run list) and `GET /api/runs/<id>` (`/logs`, `/steer`) proxy
to each run's live container API when reachable and fall back to the on-disk
`status.json`/`tasks.json` snapshot otherwise, so a dead run degrades
gracefully instead of erroring (see [cli.md](cli.md)). The static bundle
served at non-`/api` paths is still pending (v0.3, task 034).

**Shared server-side log renderer (task 014).** `GET /api/runs/<id>/logs`
does not proxy the run's raw NDJSON `/logs` transcript verbatim to the
browser: it fetches the FULL raw backlog from the run's live API, then
renders it through `ralphd.cli.log_render.render_to_lines` -- the exact
same function `ralphctl logs` uses -- with `tty=False` (plain text, no
ANSI), and only THEN trims to the requested `tail` count of *rendered*
lines (mirroring the `ralphctl logs` non-follow tail contract, task 057:
`N` means N rendered lines, not N raw events). The response is
`{"live": bool, "lines": [str, ...]}`. `log_render` was pulled out of
`main.py` into its own module specifically so both `main.py` (the CLI)
and `ui_server.py` (the hub) can import it without a circular import
(`main.py` already imports `ui_server` for the `ralphctl ui` subcommand).
The static hub bundle's `app.js` no longer reimplements event-to-HTML
rendering client-side -- it just displays the lines the server already
rendered, one per DOM element. This is a correctness fix, not just a
DRY one: the pre-task-014 client-side renderer had no `thinking_seen`
guard, so a thinking block streamed across many `thinking_delta` events
flooded the tail with one element per delta; the shared renderer's
guard (already exercised by the CLI) collapses each thinking block to
exactly one `[thinking…]` line.

## 8. Docker image

`ghcr.io/…/ralphd` (multi-arch), containing: Python 3.12 (engine), Node 22 +
a **pinned** pi CLI version (npm silently resolves an old pi when the node
engine requirement isn't met — pin both, upgrade deliberately),
git, ripgrep, curl/jq, build essentials, and a non-root `agent` user. Deliberately
**thin on toolchains** — language runtimes beyond Python/Node are the operator's
business via `--base-image`/`--dockerfile` (ralphd layers the engine onto *their*
image, see below) or a job-level
`setup.sh`, or the job's own via the *toolchain-in-a-sibling* pattern (§6),
which is the preferred answer because it keeps this image unchanged.
Engine and API run as the non-root user; no docker socket, no host
network. The image does ship the static docker **client** binary (pinned
`DOCKER_VERSION`), but it is inert without the socket — which is only mounted
by the explicit `--allow-docker` opt-in (§6). It also bundles **playwright-cli** (pinned
`PLAYWRIGHT_CLI_VERSION`) with headless Google Chrome so jobs can drive real
web UIs — e2e verification of frontend changes, screenshots as artifacts.
Chrome, not chromium, because playwright-cli's default channel is `chrome`;
only that channel ships (chromium would add ~500 MB for no default-path gain).

### The job image is content-hashed, and supplied in one of three ways (v0.6, #20)

Until v0.6 `container/Dockerfile` existed and nothing built it: `--image` only
ever *selected* a tag somebody had built by hand, and two runs of this project
silently executed a ten-day-old engine. So the image is now a function of its
inputs, and "the image matches the source" is structural rather than something
an operator remembers. Three repositories, because the three hashes are not
comparable and a staleness check (`doctor`) must not confuse them:

| reference | means | hash covers |
|-----------|-------|-------------|
| `ralphd:<hash>` | the default job image, built from this checkout | `container/`, `pyproject.toml`, `src/ralphd/` (`cli/image.py: IMAGE_INPUTS`) |
| `ralphd-base:<hash>` | an operator's own Dockerfile, built as a **base** | that Dockerfile's name + its whole build context |
| `ralphd-derived:<hash>` | the engine + pi layered onto a base | the base reference + the image inputs + the generated recipe |

A supplied image or Dockerfile is an **ingredient, never the job image**: it has
no `ralphd-engine` in it, so ralphd generates a Dockerfile that layers the engine
and pi (at the version `container/Dockerfile` pins — copied, never restated) onto
it and runs the derived result. Each build is one `docker image inspect` and a
build only on a miss; nothing is ever tagged `latest`, and the base itself is
neither probed nor run.

The three supply keys — `image` (pin a finished image, no hash, no build),
`base_image` (an existing base) and `dockerfile` (a recipe to build one) — are
three answers to a single question, so they are resolved **as one unit** by the
most specific level that answers at all: the command line, then the
`--template`'s `job.yaml`, then the registry's `config.yaml`, then "build
`ralphd:<hash>` from source". A lower level is never consulted for the other two
keys, so `--dockerfile ci/Dockerfile` replaces a registry-wide `image:` pin
instead of colliding with it; two of the three within one level is a usage error,
since there is nothing to rank them by. The ingredients an operator supplied are
recorded in the run's `job.yaml`, so `resume` replays the recipe the run started
with rather than dropping back to `ralphd:dev`. `cli/image.py` owns the
declarative half (which files are inputs, what they hash to, the text of the
generated recipe) and is **docker-free by construction**; running builds, cache
lookups and precedence live in `cli/main.py`.

Because only the first of the three hashes is a function of ralphd's source
alone, `ralphctl doctor`'s staleness check has **four** answers, not two:
`fresh`, `stale`, `missing` and `unknowable` -- the last one for a pin, either
of the other two namespaces, and an install with no `container/` to hash. A
reference that cannot be compared to a source hash is never reported as up to
date (see [cli.md](cli.md#job-image-staleness-20-h4)). The same verdict is
applied per live run against the image its own `host.json` records, which is
the case the whole mechanism exists for: a running job executing an engine that
predates the fix it is watching for.

## 9. Failure containment

- Agent process crash / nonzero exit → iteration recorded as failed, loop continues
  (budget permitting). Repeated immediate failures (3 consecutive iterations with no
  task-state change) → job fails fast with a diagnostic event.
- Engine crash → container exits; run dir remains consistent (atomic writes);
  `ralphctl resume <run-id>` starts a fresh container against the same run dir and
  the loop continues from `tasks.json` (v0.2).
- Host reboot ⇒ same as engine crash; nothing lives only in container memory.

### CLI-side resume: reproducing `--llm` wiring (task 058)

Everything above is engine/run-dir-side: the run dir and config dir survive
a container's death untouched, and the engine picks up `tasks.json` where
it left off. But `ralphctl resume` still has to launch a *new* container
with the *same* docker-run wiring `start` used, and part of that wiring
(the `--llm`-derived env vars + extra mounts) never lived in a file at all
before task 058 -- it only ever existed as `-e KEY=VALUE` flags on the
original `docker run` invocation, resolved from whatever the *operator's
shell at `start` time* happened to have. A resumed container launched from
a *different* shell (a different terminal, a different day, a machine
that's since restarted its own shell env) had no way to see those values
again -- `resume` silently started the new container with zero LLM
credentials, and every iteration failed instantly with a provider/auth
error (operator steering 018, defect 1).

The fix: `ralphctl start` now persists whatever it resolved for `--llm`
wiring -- `--llm host`'s forwarded `HOST_LLM_ENV` values and its `~/.aws`
mount path if present, or a named profile's fully-resolved `env`/`mounts`
(after every `${env:}`/`${file:}`/`${cmd:}` reference has already been
evaluated) -- to `<config-dir>/llm-wiring.json`, mode `0600`. This reuses
the exact secret-at-rest pattern already established by
`<config-dir>/pi/models.json` (task 013/023-026's resolved `apiKey`) and
`<run-dir>/.api-token`: a private file under the job's own directories,
never served by any HTTP route, mounted read-only into the container like
everything else under `<config-dir>` -- no new secret-storage mechanism was
invented. `ralphctl resume` reads that file (if present -- a run started
before task 058 simply has none, and resumes exactly as it always did) and
adds its `env`/`mounts` to the new `docker run` invocation verbatim, before
ever consulting the resuming shell's own environment.

`--forward-env`/`--llm-env`/`--env` (generic, not `--llm`-derived) had the
same gap for the same reason (task 001, live incident: a job authenticated
via `--forward-env 'AWS_*'` -- a Bedrock bearer token -- resumed into a
credential-less container and died instantly on every iteration). `start`
now also persists the *resolved* `name=value` pairs those three flags
produced, in the exact order they were applied, to a second file next to
`llm-wiring.json`: `<config-dir>/env-wiring.json`, mode `0600`, same
at-rest pattern (private, config-dir-only, read-only mount, never served
over HTTP). `resume` replays `llm-wiring.json`'s `env`/`mounts` first, then
`env-wiring.json`'s pairs in their original order, so precedence on
replay matches `start`'s (a later duplicate name still wins, exactly as
the original `docker -e` flags did). A run started before task 001 has no
`env-wiring.json` and resumes exactly as before -- no error, no extra
`-e` flags.

`resume` currently has no per-invocation `--forward-env`/`--llm-env`/
`--env`/`--llm` override flags of its own -- only the persisted values are
replayed. If such an override flag is ever added, it must win over the
recorded value for that run (never silently ignored).

## 10. Resilience: transient endpoint outages (v0.5)

§9 covers the *coarse* failures (process crash, container death, host
reboot). This section is the fine-grained half added in v0.5 (PRD
`docs/prds/v0.5-resilience.md`, issues #5 and #11): the LLM endpoint itself
going away
for a while — a gateway restart, a DNS wobble, a provider 429/529 storm, a
Bedrock stream fault. The v0.4 loop scored those as *work* failures, which is
how a run could burn its iteration budget, its three approaches and a task's
validation attempts on iterations that never reached a model at all. The
invariant now: **a transient outage costs the job time and nothing else**.
The mechanics live in `src/ralphd/engine/faults.py` and
`LoopSupervisor._run_iteration_with_infra_retry()`; the operator-facing
contract is `docs/api.md` (`GET /status`, `GET /iterations`, `GET /events`,
`POST /retry`) and `docs/cli.md` (`ralphctl status`, `ralphctl retry`).
The two older subsections of §2 are the historical design notes for the
first cut of this machinery ("No-progress escalation guard vs. instant
startup/infra failures", "Infra-fault fail-fast + retry with backoff"); what
follows is the shipped whole.

### 10.1 Fault taxonomy: `faultClass`

Every finished iteration gets exactly one verdict from the pure function
`faults.classify_fault()`, derived from the `IterationResult` in one place
(`LoopSupervisor._classify_result()`) and recorded as `faultClass` in the
iteration's `meta.json`, in `GET /iterations`, and on the `iteration.end`
event:

| `faultClass` | meaning |
| --- | --- |
| `null` | not a failure: exit 0, **no error text recorded**, not interrupted, not timed out, no startup-window kill |
| `"infra"` | the endpoint/provider/network is at fault — retried and refunded by the wrapper (§10.2) |
| `"work"` | the agent reached the model and then genuinely failed — or the *operator* terminated it |

Because both the recorded field and the retry decision come from that one
call, an operator reading `faultClass: "infra"` can be sure that is why the
attempt was retried and refunded — the taxonomy is not a second, cosmetic
opinion about the failure.

Three deliberate details:

- **An error text is a failure signal in its own right** (task 001, #11). pi
  reports an in-band provider error as a `message_end` with
  `stopReason: "error"` and can still exit **0** with zero tokens billed.
  Keying "did this fail?" off the exit code alone scored those as successes:
  no retry, no refund, budget spent on iterations that never ran.
- **Signature families, one reviewable table.** `_INFRA_TEXT_PATTERNS` is a
  single table with one commented line per family, and it is the whole
  contract for "is this the provider's fault": DNS (`ENOTFOUND`,
  `EAI_AGAIN`, `getaddrinfo`), TCP connect/teardown (`ECONNREFUSED`,
  `ECONNRESET`, `ETIMEDOUT`, `EHOSTUNREACH`, `ENETUNREACH`), half-closed
  streams (`EPIPE`, `socket hang up`, `premature close`), TLS/certificate
  failures, the SDK's opaque `Connection error.`, gateway 5xx (`bad
  gateway`, `gateway timeout`, `service unavailable`,
  `ServiceUnavailable`, `internal server error`, `502/503/504`), provider
  back-pressure (`429`, `529`, rate limit, throttling, `overloaded`), and
  Bedrock/capacity shapes (`ModelStreamErrorException`, `quota`,
  `capacity`). A match is `"infra"` *even if traffic preceded it*; each
  family plus negative ("ordinary agent failure text") cases are asserted in
  `tests/test_fault_classifier.py`.
- **The `aborted` carve-out** (task 003, #11). pi records a SIGINT as the
  bare in-band error `"aborted"` with no traffic and no exit code of its own
  — textually identical whether a provider aborted the stream (transient,
  worth retrying) or the *operator* did (`POST /abort`, `POST /interrupt`).
  The text cannot decide it, so `"aborted"` is deliberately **not** in the
  signature table: `classify_fault(operator_abort=...)` takes the loop's real
  bookkeeping (`LoopSupervisor.operator_abort_requested`) and a recorded
  abort/interrupt is never `"infra"`, regardless of text,
  traffic or watchdog state. Otherwise the wrapper would sit in backoff
  re-running the very iteration the operator just stopped.

  That flag is true for a `POST /abort`, a `POST /interrupt` *and* for the
  engine giving up on its own (an exhausted outage budget, a signal from
  anywhere), and cannot tell them apart. The **explanation** surfaces
  (`ralphctl fault`, the hub dialog) therefore say "operator-requested" only
  when `LoopSupervisor._operator_abort_recorded` establishes it and otherwise
  say an abort/interrupt is recorded without naming a cause (steering 004); the
  `faultClass` itself stays coarse, which is issue #23.

Anything else that produced **no** LLM traffic at all is classified `"infra"`
too: an unclassifiable no-traffic failure is far likelier to be an
environment/startup fault than genuine agent work, and scoring it `"work"`
would let it eat approach/task bookkeeping instead of being retried.

### 10.2 The retry / outage-budget model

`run_iteration()` routes **all five** phases (`INFRA_RETRY_PHASES` =
planning, worker, review, verify, reflect) through
`_run_iteration_with_infra_retry()`, because an outage does not care which
prompt is running. Precedence, as implemented: an `"infra"` result is
handled *inside* the wrapper — retried in place, refunded — and consumes
**none** of the phase-local budgets (`MAX_VERIFY_ERROR_RETRIES`, the review
steering loop's iterations, a task's `validationAttempts`). The phase's own
logic only sees a result once it is no longer an infra fault, or once the
wrapper gave up — which sets `_abort_reason`, so `budget_left()` is already
false and those loops exit instead of re-charging the same outage.

- **Fail fast on a hang, don't wait out the iteration timeout.**
  `PiRunner.run()`'s startup-window watchdog SIGINTs an attempt that has not
  produced a single parseable pi event within `infra_startup_timeout_s`
  (150s) and sets `no_traffic_timeout` — always `"infra"`. Killing early is
  only safe because something retries, which is why the watchdog is armed
  exactly for the wrapper's phases.
- **Escalating backoff:** `infra_retry_backoff_s`, default
  `[2, 5, 15, 30, 60, 120, 300]`s with the last value repeating, clamped by
  `infra_retry_backoff_max_s` (300s) *and* by whatever is left of the
  budget. Fast at the start on purpose: most gateway wobbles are over in
  seconds.
- **The stopping rule is wall clock, not attempts.** Attempts are
  **unlimited by default**; an *episode* (one continuous outage) ends when
  its cumulative wait reaches `infra_outage_budget_s` (default 4h;
  `reflect` gets at most `REFLECT_OUTAGE_BUDGET_S` = 300s, because that
  iteration runs after the job is already terminal). Exhaustion sets
  `_abort_reason` to `infra fault: <phase> iteration failed throughout a
  <D>s infra outage (<N> attempts, <W>s of the <B>s outage budget spent
  waiting): <error>` — the terminal `reason` names the outage duration and
  the last error verbatim. `infra_retry_max` remains an attempt cap
  honoured **only when set explicitly** (back-compat / hard-stop escape
  hatch).
- **Episodes reset on contact.** `_reset_infra_episode()` clears the attempt
  counter, the accumulated wait and the instant-failure streak the moment
  any iteration reaches the model, so a job hitting a short glitch every
  hour is never slowly starved of retry budget by the earlier ones.
- **Refunds.** Each infra attempt increments `_infra_refunded`, which
  `budget_left()` subtracts from `iterations_used`: the attempt still gets
  its own iteration directory (numbering stays monotonic), but the *budget
  comparison* is adjusted. Approaches, the stagnation guard and
  `validationAttempts` are untouched.
- **Deadline extension** (task 011, #5). Waiting is not working time: every
  backoff wait adds its real seconds to `status.json`'s `infraWaitTotalS`
  and pushes both `self.deadline` and its published twin `deadlineAt` out by
  exactly that much, emitting `deadline_extended`. So `job_timeout_s` keeps
  its plain meaning — time the job was *able to work* — and a 4-hour outage
  cannot kill an 8-hour job for "timeout" having done nothing wrong. The
  seconds booked are the seconds actually spent, so the budget arithmetic
  and `infraWaitTotalS` can never disagree (a wait cut short by
  `POST /retry` books only what it waited).
- **Reflect** (tasks 018/019, #5) runs through the same wrapper and, when the
  job just ended on an infra-shaped failure, waits one backoff step
  (`reflect_infra_delay`) *before* its first attempt instead of firing into
  the same dead gateway in the same second — unless the operator aborted, who
  gets no countdown. A reflect that still fails is recorded (`status.json`'s
  `reflect: {ok: false, error: …}` plus `artifacts/reflection/FAILED.md`) and
  surfaced by `ralphctl status` and the hub, never silently discarded; the
  terminal state is unchanged either way.

### 10.3 Status contract: `health` and `infraWait`

`state` deliberately stays `running` through an outage — adding a
`"degraded"` state value would break every consumer's terminal-state logic,
`ralphctl watch` included. The degraded case is carried by two fields
instead (`status.json` and `GET /status`, documented field-by-field in
`docs/api.md`):

- `health`: `"ok"` | `"degraded"`. Degraded from the first infra-classified
  failure of an episode until an iteration reaches the model again — it
  stays degraded *between* two backoffs, because a run whose endpoint is
  still broken has not recovered just because it is mid-attempt.
- `infraWait`: `null` unless the loop is actually parked in a backoff wait;
  otherwise `since`, `attempt`, `error`, `phase`, `nextAttemptAt`,
  `waitedS`, `budgetS`, `remainingS`.

The same information is in the event stream, so it is visible to
`ralphctl watch` and not only to whoever polls `/status` at the right
moment: `infra_retry` (per attempt), `infra_wait` (the full payload as a
wait starts), `deadline_extended`, `infra_recovered` (episode over, health
back to `ok`), `infra_retry_now` (manual wake), `reflect_infra_delay`.
Surfaces: `ralphctl status` prints a `degraded:` line with the countdown,
attempt and error (byte-identical output for a healthy run), and the hub
run-detail card renders a distinct degraded treatment with the countdown —
`textContent` only, as everywhere in `app.js`.

### 10.4 Manual retry-now

A backoff wait is an interruptible `asyncio.Event` (`_retry_now`, the same
shape as the pause gate), never a bare `asyncio.sleep`: an operator who can
see the endpoint is healthy again must not have to stare at a 5-minute
countdown. `POST /retry` (→ `ralphctl retry <run-id>`, or the hub's *retry
now* button through the UI-server proxy) releases the wait immediately,
emits `infra_retry_now`, and **restarts the outage-budget clock** — the
operator asserting "it is back" is new information, and a run that has
already sat out most of its budget must not die one attempt later. The
attempt counter is *kept*, so repeated impatient retries keep escalating the
backoff instead of hammering a still-broken endpoint. `409` when the run is
not in an infra wait. `/retry` and `/resume` are independent by design:
`/retry` never unpauses a paused run, `/resume` never shortens a backoff, and
neither touches steering.

### 10.5 The instant-failure carve-out (why an outage waits and a broken credential doesn't)

Retrying an infra fault for hours is right for an outage and catastrophic for
a *misconfiguration*: a run with no LLM credentials would sit in backoff for
4 hours instead of telling the operator in seconds. Both shapes classify
`"infra"`, so the wrapper distinguishes them by *shape over time*, not by
text (task 010, #5):

- A transient fault **varies and takes time** — a gateway 502 arrives after
  a connect, a DNS glitch resolves itself, the error text moves around.
- A broken environment **fails identically in 0.6s, every time**.

So an instant (sub-`INSTANT_FAILURE_MAX_DURATION_S` = 5s), zero-work failure
*is* retried like any other infra fault, while every such attempt is also
scored by `_check_instant_failure()` against
`_instant_failure_signature()` — exit code plus error text with digits
normalised away (timestamps, ports, request ids). Once
`MAX_CONSECUTIVE_INSTANT_FAILURES` = 3 attempts have failed instantly, with
no traffic and the **same** signature, the wrapper stops with §2's
broken-environment diagnosis ("… likely missing or broken LLM credentials,
or an agent-startup fault …") instead of waiting out the outage budget.
"No work" means no assistant text and no *tokens*: pi zero-fills a usage
block on every `message_end`, so an in-band error's `usage` dict is
non-empty while nothing was ever billed. An instant failure that **did**
reach the model (a 0.3s Bedrock 502 with tokens billed) is handed straight
back to the phase's own bounded error retry, and any iteration that reaches
the model resets the streak — so the fail-fast path can only fire on a run
of genuinely identical, work-free failures.
