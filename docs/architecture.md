# ralphd Architecture

Status: as shipped in v0.9.0 unless marked otherwise (subsections dated to an
earlier version are historical design notes, kept because the reasoning still
explains the code). This line stayed a wave behind v0.9's own release while
#70's SPEC/architecture gaps were open; it moves to v0.9.0 now that task 011
closes them (the router/`router_model` and the `PUT /config/prompts/{name}`
correction above), and stays a wave behind v0.10 in turn until that wave's own
doc task closes out its remaining gaps.

## 1. Overview

ralphd is a job-scoped autonomous coding loop. A **job** is one PRD (product
requirements document / task description) executed to verified completion. Each job
runs in its own Docker container. The container holds:

- the **engine** (`ralphd-engine`, Python): loop supervisor + HTTP API server, PID 1
- the **agent runtime**: [pi](https://pi.dev) CLI, spawned as a subprocess once per
  iteration
- a **workspace**: the code being worked on, either an existing host checkout
  bind-mounted in or a directory the engine creates on the run dir and the
  planning iteration clones into (§3 "State model"; there is no named-volume
  mode)

The host side is driven entirely by **`ralphctl`** (Python CLI): it prepares config,
starts the container, talks to the API, and maintains the run registry at
`~/.ralphd/`. Nothing in the engine assumes any particular cloud, CI system, or
network beyond reaching the configured LLM endpoint.

## 2. The loop

The loop has three phase types drawing on one shared **iteration budget**
(`iterations` in `job.yaml`, `JobConfig.iterations`, published as
`budgets.iterations`):

```
job start
  └─ approach 1..max_approaches (shared budget):
       ├─ PLANNING   (1 iteration)  read PRD → write tasks.json + notes.md
       ├─ WORKER     (n iterations) execute tasks one at a time until a
       │                            "plan_complete" verdict is honoured
       │    └─ [vigilant mode] VERIFY after each newly completed task;
       │                       failure ⇒ status=validation-failed, back to worker
       └─ REVIEW     (1 iteration)  independently re-check EVERY PRD requirement
            ├─ satisfied  ⇒ {"verdict": "verified"} ⇒ job succeeds
            └─ not        ⇒ {"verdict": "rejected", ...} + review-findings.md ⇒
                            next approach with a composite PRD (original +
                            findings + attempt history)
budget exhausted or max_approaches reached ⇒ job fails (state preserved)
```

Design invariants:

- **The worker's word is never trusted.** `COMPLETE` only gates entry to review.
  Only the reviewer's own `"verified"` contract verdict ends the job
  successfully (task 119, SPEC §4.3) -- not the retired `<promise>VERIFIED</promise>`
  sentinel. Entry to review is the
  *engine's* decision, not the worker's: it also happens on the off-budget grace
  review and when a plan has nothing actionable left but a `failed` task in it
  (below).
- **Fresh context per iteration.** Each iteration is a new `pi` process; continuity
  flows exclusively through state files (below). This bounds context growth and makes
  every iteration resumable and interruptible.
- **`tasks.json` is the source of truth.** The engine's own writes to it
  (fingerprints, validation notes, approach archiving) are atomic (write-temp +
  rename), so killing an iteration mid-flight loses at most that iteration's
  uncommitted reasoning, never task state. The *agent* writes it too, and its
  writes are not atomic — which is why every reader goes through one hardened
  read path (§3, "Reading `tasks.json`: unknown is not zero") instead of
  assuming it can never catch a half-written file.
- **The whole-plan claim is a contract verdict, not a bare sentinel** (v0.14
  requirement O, #100 -- the retired `<promise>COMPLETE</promise>` marker's
  successor). `worker`'s reply is parsed the same JSON-contract way `verify`
  and `review` already report through (task 118, task 119, SPEC §4.3), and the
  literal marker text is still matched by `IterationResult.saw_complete` for
  an operator diagnostic only -- it drives no decision any more. The promise
  claim itself must count **only as the final non-blank line** of that
  output, so an agent quoting the protocol — or explaining why it is
  *withholding* the signal — cannot accidentally emit it; mentioning one
  anywhere else is logged as "mentioned but did not end with it".
  And a `"plan_complete"` verdict, valid only when no task was assigned this
  iteration, is honoured only after the engine has cross-checked the plan it
  just re-read: any task not `completed`/`skipped` refuses it and the worker loop
  keeps going (SPEC §4.3). Narration never decides; the record does.
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
- **In-step liveness is the log stream; task status changes only at a step
  transition (task 022, requirement F).** A task's `status` field never
  changes while a hat's subprocess is executing — `_emit_task_changes()`
  runs only at the harness's own at-transition writes (a worker/verify
  iteration ending, a vigilant claim relabelled, a verify verdict applied),
  never on a timer. Before task 022, a 0.25s background poller
  (`LoopSupervisor._poll_task_changes`) re-read `tasks.json` while the
  agent subprocess for a worker iteration was still running, so a status
  written mid-iteration (the worker prompt's old "flip to `in-progress`
  first" instruction) surfaced as a `task` event immediately. That poller
  is retired: `GET /tasks` already re-reads `tasks.json` from disk on
  every call regardless of any poll, so an operator's `GET /tasks` /
  `ralphctl tasks` view was never actually dependent on it; the one real
  loss is a `task` *event* for a transient in-iteration state, which is
  now never observed at all (`_emit_task_changes()`'s diff jumps straight
  from the status before the iteration to the status after it). What an
  operator watching a long-running iteration needs instead — proof the
  run is still alive and doing something, not stalled — is what
  `GET /logs?follow` (tailing `output.jsonl` as the agent subprocess
  writes it) already provided independently of the poller, and continues
  to provide with no change. See `artifacts/reports/poll-retirement.md`
  for the itemised before/after and the test proving the hub's run-detail
  view keeps updating (never blank) for the length of a long worker
  iteration purely from the log stream.

  **Task 116 update:** the picture above still holds for the harness's
  OWN pre-spawn claim — the pending -> in-progress move `_step_worker`
  now writes and emits itself, strictly before the worker subprocess is
  spawned (the scheduler, task 115, picks the task; the agent no longer
  edits `tasks.json` at all, fact 9). That write is a genuine at-transition
  event, not the transient mid-iteration state task 022 retired the poller
  over, so it DOES surface as its own `task` event now — an operator
  watching `GET /tasks`/`ralphctl tasks` sees the claim the instant it
  happens, through the event stream as well as the on-disk re-read. The
  in-iteration gap task 022 describes still exists for everything AFTER
  that claim: no further status event for the same task appears until the
  subprocess has exited and the harness applies its reported verdict.

### The declared transition table (task 008/014/016, requirement B)

States, legal transitions and guard-predicate names live as one data
structure in `src/ralphd/engine/transitions.py` (`TABLE` plus the
`VIGILANT_DELTA` that is the *only* difference between vigilant and
non-vigilant); `engine.loop.LoopSupervisor._advance()` walks it through
`transitions.TransitionExecutor.advance()`, which raises
`IllegalTransition` for any edge the table for the variant in force does
not list — refusal happens at the one runtime primitive every state move
passes through, not by a test reading the table back. The table below is
generated from that module, not hand-copied; see
`scripts/render_transition_table.py`'s docstring and
`tests/test_transitions.py` (task 027, requirement D.6) for how drift is caught.

<!-- BEGIN GENERATED TRANSITION TABLE (scripts/render_transition_table.py) -->

Generated from `src/ralphd/engine/transitions.py` by `scripts/render_transition_table.py` — do not hand-edit this block; regenerate it with `python scripts/render_transition_table.py --write` and let `tests/test_transitions.py` catch drift. Entry state: `planning`. Run-terminal states (`status.json`'s top-level `state`, not a step state): `succeeded`, `failed`, `aborted`.

| State | Hat | Variant | Target | Guard |
|---|---|---|---|---|
| `planning` | planning | both | `planning` | `instant_failure_streak` |
| `planning` | planning | both | `replan` | `empty_plan` |
| `planning` | planning | both | `worker` | `planning_produced_tasks` |
| `planning` | planning | peer plan review only | `plan_review` | `planning_produced_tasks_peer_review_enabled` |
| `planning` | planning | peer plan review only | `worker` | `planning_skipped_plan_already_reviewed` |
| `worker` | worker | both | `steer` | `steering_pending` |
| `worker` | worker | both | `worker` | `no_verdict_yet` |
| `worker` | worker | both | `review` | `verdict_ready` |
| `worker` | worker | both | `review` | `blocked_with_failures` |
| `worker` | worker | both | `replan` | `stagnation_exhausted` |
| `worker` | worker | both | `review` | `doomed_plan_unverifiable` |
| `worker` | worker | both | `review` | `petition_held_no_task_bound` |
| `worker` | worker | both | `worker` | `unsatisfiable_targeted_replan` |
| `worker` | worker | both | `review` | `unsatisfiable_and_nothing_left` |
| `worker` | worker | both | `worker` | `worker_deferred_reordered` |
| `worker` | worker | both | `paused` | `stagnation_last_approach` |
| `worker` | worker | both | `paused` | `unschedulable_dependency_cycle` |
| `worker` | worker | both | `paused` | `unresolvable_dependency_edge` |
| `worker` | worker | vigilant only | `verify` | `completed_task_pending_verification` |
| `verify` | verify | vigilant only | `worker` | `verify_failed_retry_same_task` |
| `verify` | verify | vigilant only | `worker` | `verify_passed_next_task` |
| `review` | review | both | `worker` | `steering_pending_at_verified` |
| `review` | review | both | `worker` | `review_cure_pending_at_verified` |
| `review` | review | both | `finishing` | `verified` |
| `review` | review | both | `replan` | `rejected_approach_left` |
| `review` | review | both | `review` | `rejected_incurable` |
| `review` | review | both | `cure` | `rejected_curable_pass_available` |
| `review` | review | both | `paused` | `rejected_last_approach` |
| `review` | review | both | `paused` | `rejected_repeated_signature` |
| `review` | review | both | `paused` | `review_contract_unrepaired` |
| `review` | review | both | `paused` | `review_findings_unusable` |
| `review` | review | both | `paused` | `operator_input_needed` |
| `review` | review | both | `finishing` | `rejected_no_approach_left` |
| `review` | review | both | `review` | `review_interrupted` |
| `review` | review | both | `finishing` | `review_unjudged_at_budget_end` |
| `review` | review | peer verdict gate only | `verdict_gate` | `verified_peer_verdict_gate_enabled` |
| `replan` | — | both | `finishing` | `repeated_blocker_signature` |
| `replan` | — | both | `planning` | `budget_and_approach_left` |
| `replan` | — | both | `finishing` | `budget_or_approach_exhausted` |
| `reflect` | reflect | both | `succeeded` | `reflect_done_outcome_succeeded` |
| `reflect` | reflect | both | `failed` | `reflect_done_outcome_failed` |
| `reflect` | reflect | both | `aborted` | `reflect_done_outcome_aborted` |
| `steer` | steer | both | `worker` | `steer_settled` |
| `plan_review` | plan_review | peer plan review only | `worker` | `plan_review_approved` |
| `plan_review` | plan_review | peer plan review only | `planning` | `plan_review_objected_revision_available` |
| `plan_review` | plan_review | peer plan review only | `worker` | `plan_review_revision_bound_reached` |
| `plan_review` | plan_review | peer plan review only | `worker` | `plan_review_revision_refused_work_recorded` |
| `verdict_gate` | verdict_gate | peer verdict gate only | `finishing` | `verdict_gate_approved` |
| `verdict_gate` | verdict_gate | peer verdict gate only | `cure` | `verdict_gate_rejected_curable_pass_available` |
| `verdict_gate` | verdict_gate | peer verdict gate only | `replan` | `verdict_gate_rejected_approach_left` |
| `verdict_gate` | verdict_gate | peer verdict gate only | `finishing` | `verdict_gate_rejected_no_approach_left` |
| `cure` | — | both | `worker` | `cure_pass_appended` |
| `paused` | — | both | `review` | `paused_resumed` |
| `paused` | — | both | `worker` | `paused_resumed_stagnation` |
| `paused` | — | both | `worker` | `paused_resumed_unschedulable_cycle` |
| `paused` | — | both | `worker` | `paused_resumed_unresolvable_edge` |
| `finishing` | — | both | `reflect` | `reflect_enabled` |
| `finishing` | — | both | `succeeded` | `reflect_disabled_outcome_succeeded` |
| `finishing` | — | both | `failed` | `reflect_disabled_outcome_failed` |
| `finishing` | — | both | `aborted` | `reflect_disabled_outcome_aborted` |
| `succeeded` | — | — | `— (terminal)` | `—` |
| `failed` | — | — | `— (terminal)` | `—` |
| `aborted` | — | — | `— (terminal)` | `—` |

Guards whose declared condition is wider than their name (`transitions.GUARD_CONDITION_NOTES`, rendered here so the table states it instead of a reader inferring it):

- `stagnation_last_approach` — fires on EVERY approach that progressed, not only the last one (v0.20 requirement B.3, #156, task 018): the run's last approach stalling three iterations running, OR any earlier approach that gained a workspace commit or left a `superseded` task behind from a forced split and then stalled, OR a `superseded` parent's own split child spending its own `progressBudget` (escalated once per child, naming it on the `operator_escalation` event's `exhaustedSplitChild` field) instead of forcing a second split. A genuinely silent approach with a successor left still takes `stagnation_exhausted` to `replan` exactly as before, and either pause resumes into `worker` over the SAME approach.

<!-- END GENERATED TRANSITION TABLE -->

The same table rendered as an FSM diagram, generated by
`scripts/render_transition_diagram.py` from
`transitions.table_for(vigilant=False)` — the non-vigilant variant, which
already contains `review -> cure` and `review -> paused` unconditionally
(the vigilant-only, peer-plan-review-only and peer-verdict-gate-only edges
above are not drawn here); see `tests/test_transitions.py` (task 027,
requirement D.6) for how drift is caught.

<!-- BEGIN GENERATED TRANSITION DIAGRAM (scripts/render_transition_diagram.py) -->

Generated from `src/ralphd/engine/transitions.py` (the non-vigilant table, `transitions.table_for(vigilant=False)`) by `scripts/render_transition_diagram.py` — do not hand-edit this block; regenerate it with `python scripts/render_transition_diagram.py --write` and let `tests/test_transitions.py` catch drift.

```mermaid
stateDiagram-v2
    [*] --> planning
    state verify
    state plan_review
    state verdict_gate
    planning --> planning : instant_failure_streak
    planning --> replan : empty_plan
    planning --> worker : planning_produced_tasks
    worker --> steer : steering_pending
    worker --> worker : no_verdict_yet
    worker --> review : verdict_ready
    worker --> review : blocked_with_failures
    worker --> replan : stagnation_exhausted
    worker --> review : doomed_plan_unverifiable
    worker --> review : petition_held_no_task_bound
    worker --> worker : unsatisfiable_targeted_replan
    worker --> review : unsatisfiable_and_nothing_left
    worker --> worker : worker_deferred_reordered
    worker --> paused : stagnation_last_approach
    worker --> paused : unschedulable_dependency_cycle
    worker --> paused : unresolvable_dependency_edge
    review --> worker : steering_pending_at_verified
    review --> worker : review_cure_pending_at_verified
    review --> finishing : verified
    review --> replan : rejected_approach_left
    review --> review : rejected_incurable
    review --> cure : rejected_curable_pass_available
    review --> paused : rejected_last_approach
    review --> paused : rejected_repeated_signature
    review --> paused : review_contract_unrepaired
    review --> paused : review_findings_unusable
    review --> paused : operator_input_needed
    review --> finishing : rejected_no_approach_left
    review --> review : review_interrupted
    review --> finishing : review_unjudged_at_budget_end
    replan --> finishing : repeated_blocker_signature
    replan --> planning : budget_and_approach_left
    replan --> finishing : budget_or_approach_exhausted
    reflect --> succeeded : reflect_done_outcome_succeeded
    reflect --> failed : reflect_done_outcome_failed
    reflect --> aborted : reflect_done_outcome_aborted
    steer --> worker : steer_settled
    cure --> worker : cure_pass_appended
    paused --> review : paused_resumed
    paused --> worker : paused_resumed_stagnation
    paused --> worker : paused_resumed_unschedulable_cycle
    paused --> worker : paused_resumed_unresolvable_edge
    finishing --> reflect : reflect_enabled
    finishing --> succeeded : reflect_disabled_outcome_succeeded
    finishing --> failed : reflect_disabled_outcome_failed
    finishing --> aborted : reflect_disabled_outcome_aborted
    succeeded --> [*]
    failed --> [*]
    aborted --> [*]
```

<!-- END GENERATED TRANSITION DIAGRAM -->

### `superseded`, and the every-approach pause widening (v0.20 requirement B, #156, tasks 013/016/018/019)

**`superseded` is a task status, not a step in the diagram above.** It is a
declared status of the *task-status* mini-FSM -- a second, separate table
(`transitions.TASK_STATUS_TABLE` / `TASK_STATUS_VIGILANT_DELTA`, rendered
below) governing one `tasks.json` record's `status` field rather than the
run's own FSM position -- and the one declared move in it that the engine
forces itself. Its only writer is `state.supersede_task()`
(`LoopSupervisor._apply_forced_split_report`), called the moment a task's own
`progressBudget` (SPEC §5.2, `{taskId, used, budget}`, requirement A.3/A.4,
task 013) is spent with no `proposed_tasks` split arriving to relieve it: the
harness forces the split itself, inserts the worker-origin child(ren) with the
parent's own `dependsOn` inherited onto every one of them (task 016's
`_fold_inherited_dependsOn`, so a child never schedules ahead of what the
parent itself was waiting on), and moves the parent to `superseded` in the
same write -- the declared `in-progress -> superseded` edge, present
identically in both variants because a spent budget is not a verification
concern. `superseded` is terminal: no reader ever needs a transition out of
it, because the work it names has already moved to the child(ren) the same
write created. Three tuples in `engine.state` say so explicitly: `TASK_COMPLETION_NONBLOCKING_STATUSES` (a `superseded` parent never
holds the steering-origin exit gate open, same reasoning as `awaiting-
validation`), `TASK_DEPENDENCY_SATISFIED_STATUSES` (a task whose `dependsOn`
names a now-`superseded` parent is schedulable, not blocked -- the parent's
own obligation moved, it did not vanish) and `TASK_TERMINAL_STATUSES` (nothing
further moves a `superseded` task on its own, exactly like `petitioned`
before it, for the same reason: terminality here means "nothing further moves
it on its own", not "the plan is done").

**Declared, but claimable by nobody.** Being in the table is what makes the
engine's own write legal and legible (`ralphctl doctor` reads a `superseded`
record as a known status, the generated table and diagram below show the
edge); it is emphatically not permission for anyone else to take it.
`transitions.ENGINE_FORCED_TASK_STATUSES` names it, and two refusals derive
from that one tuple: `task_status_report_errors` refuses a hat that reports
`task_status: "superseded"` through the contract channel (a worker asking for
a split carries `proposed_tasks` instead, and the harness decides), and
`ralphctl amend --status` refuses it as an operator ruling target
(`OPERATOR_RULEABLE_TASK_STATUSES` is `TASK_STATUSES` minus exactly that
tuple) -- an operator never RULES a task superseded, the engine only ever
forces the split itself.

**Cure-01-05 (PRD requirement B.3/B.4, F05, #221) adds one more edge INTO
`superseded`, from `pending` -- still not a second writer for a fresh
split, only a RESTORE.** A cure pass's `invalidates` walk
(`state.insert_cure_tasks`) re-opens a plan's own lane-evidence record from
WHATEVER terminal status it genuinely held, `superseded` included (a
forced-split parent later named in a finding's `invalidates`), persisting
that exact status first (`preCureStatuses`). When the completion-path hook
(`LoopSupervisor._reconcile_pre_cure_heads`) later finds the cure's real
diff never touched the record's cited path, it walks the prediction back
through `state.apply_task_status_change` to that persisted status -- for a
record that had held `superseded`, this is the one call site allowed to
target it (`allow_un_reopen_restore=True`, gated on `source ==
"cure-reconciliation"` alone); every hat-report call site still refuses it,
unchanged. This never creates a SECOND `superseded` parent or a second
split -- it only ever un-does a reopen this same engine made on a
prediction that turned out to be wrong.

**That whole edge is declared for BOTH variants, and the semantic layer --
not the table -- is what keeps a hat out of it.** The restore target is
DATA the engine persisted at reopen time (`preCureStatuses`), so the only
question the table has to answer is which statuses that map can hold;
`cfg.vigilant`, the flag in force at reconciliation time, is no evidence
about what a record held at reopen time (a job resumed with the flag
flipped, a `tasks.json` written by an earlier configuration, or a
genuinely `validated` record in a non-vigilant run are all the same
shape). Task 007 originally split the edge -- `pending -> completed` in
the base table, `pending -> validated` in the vigilant delta -- which made
the restore itself mode-dependent: F05 measured a non-vigilant run that
persisted `preCureStatuses = {"058": "validated"}`, asked a non-vigilant
table whether `pending -> validated` was legal, got `False`, and left the
record `pending` with no `task.un_reopened` event and no notification.
All five targets now sit in the BASE table
(`transitions.UN_REOPEN_TASK_STATUS_TARGETS`, spliced into its `pending`
entry), and the vigilant delta carries no `pending` entry at all, so
`is_legal_task_status_transition` answers identically whichever flag is in
force. Widening the table this way widens nothing a HAT may claim:
`task_status_report_errors` refuses a reported `superseded` out of any
status, as before, and refuses the OTHER variant's claim terminal out of
`pending` (`transitions.variant_claim_terminal` -- `completed` is refused
in a vigilant run, so no worker routes around verification, and
`validated` in a non-vigilant one, where no verify step exists to earn
it), leaving each variant's own claim terminal exactly as reportable as it
was. The notification the hook sends names each restored record together
with the status it was restored TO, so a promotion could not read the same
way as a genuine restore.

<!-- BEGIN GENERATED TASK-STATUS TABLE (scripts/render_transition_table.py) -->

Generated from `src/ralphd/engine/transitions.py`'s task-status mini-FSM (`TASK_STATUS_TABLE` / `TASK_STATUS_VIGILANT_DELTA`, composed by `task_status_table_for`) by `scripts/render_transition_table.py` — do not hand-edit this block; regenerate it with `python scripts/render_transition_table.py --write` and let `tests/test_transitions.py` catch drift. This table governs one `tasks.json` record's `status` field, not the run's own FSM position. Every move here is applied by the harness at a step transition, never written by an agent itself; `superseded` is forced by the engine alone — no hat may report it (`task_status_report_errors`) and no operator may rule it (`ralphctl amend --status`, which validates against `OPERATOR_RULEABLE_TASK_STATUSES`).

| Status | Variant | Target |
|---|---|---|
| `pending` | both | `in-progress` |
| `pending` | both | `validated` |
| `pending` | both | `completed` |
| `pending` | both | `failed` |
| `pending` | both | `skipped` |
| `pending` | both | `superseded` |
| `in-progress` | non-vigilant only | `completed` |
| `in-progress` | both | `failed` |
| `in-progress` | both | `skipped` |
| `in-progress` | both | `superseded` |
| `in-progress` | vigilant only | `awaiting-validation` |
| `awaiting-validation` | vigilant only | `validated` |
| `awaiting-validation` | vigilant only | `validation-failed` |
| `awaiting-validation` | vigilant only | `failed` |
| `awaiting-validation` | vigilant only | `petitioned` |
| `validated` | — | `— (terminal)` |
| `validation-failed` | both | `in-progress` |
| `validation-failed` | both | `failed` |
| `validation-failed` | both | `skipped` |
| `completed` | — | `— (terminal)` |
| `failed` | both | `skipped` |
| `skipped` | — | `— (terminal)` |
| `petitioned` | — | `— (terminal)` |
| `superseded` | — | `— (terminal)` |

<!-- END GENERATED TASK-STATUS TABLE -->

<!-- BEGIN GENERATED TASK-STATUS DIAGRAM (scripts/render_transition_diagram.py) -->

Generated from `src/ralphd/engine/transitions.py`'s task-status mini-FSM (`transitions.task_status_table_for(vigilant=True)`, the superset variant) by `scripts/render_transition_diagram.py` — do not hand-edit this block; regenerate it with `python scripts/render_transition_diagram.py --write` and let `tests/test_transitions.py` catch drift. The one non-vigilant edge the vigilant variant replaces (`in-progress -> completed`) is drawn as `non_vigilant_only`; every other edge is declared in both variants.

```mermaid
stateDiagram-v2
    state "pending" as pending
    state "in-progress" as in_progress
    state "awaiting-validation" as awaiting_validation
    state "validated" as validated
    state "validation-failed" as validation_failed
    state "completed" as completed
    state "failed" as failed
    state "skipped" as skipped
    state "petitioned" as petitioned
    state "superseded" as superseded
    [*] --> pending
    pending --> in_progress
    pending --> validated
    pending --> completed
    pending --> failed
    pending --> skipped
    pending --> superseded
    in_progress --> awaiting_validation
    in_progress --> failed
    in_progress --> skipped
    in_progress --> superseded
    awaiting_validation --> validated
    awaiting_validation --> validation_failed
    awaiting_validation --> failed
    awaiting_validation --> petitioned
    validation_failed --> in_progress
    validation_failed --> failed
    validation_failed --> skipped
    failed --> skipped
    in_progress --> completed : non_vigilant_only
```

<!-- END GENERATED TASK-STATUS DIAGRAM -->

**The every-approach pause widening reuses the SAME declared edge, unchanged
by name.** `worker --> paused : stagnation_last_approach` in the step-state
table and diagram above is exactly the edge task 012 (#153) added for a
stagnant LAST approach only; task 018 (#156) widens the *guard's own
condition*, not the edge, its target or its resume path -- so the row and its
mermaid arrow are unchanged, and regenerating both from `transitions.py`
reproduces them byte-for-byte. Because a guard name is an identifier and not
a specification, the widened condition is declared as data
(`transitions.GUARD_CONDITION_NOTES`) and rendered into the generated
step-state table block above, under "Guards whose declared condition is wider
than their name", so the table itself states it. The widened condition:
any approach that PROGRESSED (a workspace commit somewhere in the approach,
or a `superseded` task left behind by a forced split --
`LoopSupervisor._approach_progressed`) but still stalled for three
consecutive no-progress worker iterations now takes this edge instead of
falling through to the ordinary `stagnation_exhausted` -> `replan` edge one
row up, which still fires exactly as before for a genuinely silent approach
with a successor left. A SECOND budget exhaustion is folded into the same
widened condition: once a `superseded` parent's own split child spends its
own `progressBudget` too, `_exhausted_split_child_of_superseded_parent()`
recognises the shape (`splitFrom` present, naming a parent whose status is
already `superseded`) and takes this pause instead of forcing yet another
split, naming the exhausted child on the `operator_escalation` event's
`exhaustedSplitChild` field (SPEC §4.6) -- bounded at one escalation per
child, so an operator who resumes past it gets the ordinary forced split on
that child's own next exhaustion. Either way `_step_paused` reads which
reason parked the run and routes `stagnation_last_approach` straight back to
`worker` over the SAME approach (never `review` -- there is no verdict
pending), exactly as task 012 already declared for the last-approach case;
widening the condition changed nothing about the target or the resume path.

### The router: one constrained question, deterministic-first (task 018, requirement E)

Not every state above resolves to a single edge from disk alone. Where
`transitions.successors(state, vigilant)` returns more than one legal edge
(and only then — most states have ≤1 and never reach this path),
`_step_generic_hat` asks `engine.router.decide()` one JSON question,
validated against `router.ROUTER_SCHEMA` and the table's own legal targets,
re-prompted up to `contract_retry_max` times and bounded by
`JobConfig.router_timeout_s` (env `RALPHD_ROUTER_TIMEOUT_S`, default `60.0`,
published as `GET /config`'s `budgets.routerTimeoutS`); a timeout or an
answer that never validates falls back to the state's own declared choice
(`transitions.fallback_guard`/`ROUTER_FALLBACK`) rather than ever taking an
edge the table does not list. The router's own model is a dedicated config
key, `router_model` (env `RALPHD_ROUTER_MODEL`, `job.yaml` key `router_model`,
surfaced as `GET /config`'s `model.routerModel`) — checked after
`model_overrides["router"]` and before falling back to `model` directly;
`router` carries no strategy-tier row (task 006 retired the tiered mechanism
entirely) -- a one-question classification never needs anything beyond its
own key or the plain default. Every call
is its own transcript under `artifacts/router/NNNN.jsonl` (a plain counter,
not an iteration slot: `LoopSupervisor._ask_router()` never increments
`iterations_used` or writes a `meta.json`), and its usage folds into
`usage.byPhase["router"]` so the cost of a call that is never charged as a
work iteration is still visible, not hidden.

A contract re-ask is the same shape, uncounted for the same reason.
Every `contract_enforcement.enforce_contract` retry ladder (task 010) --
the schema/task-status site in `_step_generic_hat` and the worker's own
verdict sites -- passes `LoopSupervisor._ask_contract_fix()` as its
`respond` once a hat's payload fails `contracts.validate_payload` or its
semantic sibling check. It states the validation errors verbatim and asks
ONLY for the corrected final payload, never the hat's own task prompt
again, and it is built directly on `_ask_router`'s own precedent: no
iteration slot, no `iterations_used` increment, no `meta.json` (`loop.py`
never calls `_run_iteration_once` or touches `self.iterations_used` from
inside it). Each call gets its own transcript under
`artifacts/contract/NNNN.jsonl` (a plain counter, `loop.py:2963-2966`, not
an iteration directory), and its usage folds into
`usage.byPhase["contract"]` (`loop.py:2982`) -- kept separate from
`"router"`'s bucket because the two uncharged channels answer structurally
different questions. The re-ask runs on the offending hat's own model
(`self.cfg.model_for(hat)`, task 004), never a fixed `"contract"` lookup,
so the repair stays inside the tier that made the mistake.

### Phase prompts

Prompt templates ship inside the installed package (`src/ralphd/prompts/`) —
one per phase: `planning.md`, `worker.md`, `review.md`, `task-verify.md`,
`reflect.md`, `steer.md` (task 009, PRD requirement G), `plan_review.md`
(task 018, PRD requirement K — the opt-in peer plan-review gate) and
`verdict_gate.md` (task 020, PRD requirement L — the opt-in, genuinely
BLIND peer verdict gate). Every one
of them is **overridable by mount**: a file of the same
name in `/config/prompts/` (mapped by the CLI) takes precedence, resolved per
iteration through the config overlay order (§5). Runtime replacement over the
API (`PUT /config/prompts/{name}`, taking effect next iteration) covers the
four phases `engine/config.py: PROMPT_NAMES` lists — `reflect`, `steer`,
`plan_review` and `verdict_gate` are deliberately not among them (none of
the four is listed
by `GET /config/prompts` or `ralphctl prompts ls` either), so the
post-terminal reflection prompt, the steer hat's prompt, the peer
plan-review hat's prompt and the peer verdict-gate hat's prompt can only be
replaced by a mount or in the image.
`PUT /config/prompts/{name}` is one of
the routes `control_token` gates unconditionally (task 005, requirement A /
#62, SPEC §13.3), and no spawned iteration's own process can ever present
that header (`PiRunner` strips `RALPHD_CONTROL_TOKEN` from its environment) —
so this surface is, in practice, host-only: an operator calling it from
outside the job container, never a phase configuring itself from inside one.
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

**The quiet gate is a warning (task 026, #29's carried-forward suggestion).** The
same record is also the single point of failure: if it claims a completed task is
already verified, `pending_verify` computes empty and the engine keeps running
worker iterations, which from outside is indistinguishable from a run with
nothing left to verify (#29 was silent for 17 iterations of a real run). The
first worker iteration that finds the gate empty while some currently-`completed`
task was never verified by this process therefore emits one `log` event at level
`warning` (`LoopSupervisor._warn_if_verify_gate_empty()`), naming the task ids
and the approach, once per engine process rather than per iteration. A legitimate
resume looks the same, so the message names both explanations rather than
claiming a bug.

A verify iteration that errors out mid-stream (agent/provider failure -- e.g. a
Bedrock 502 -- surfaced as an assistant `message_end` with `stopReason: "error"`)
before ever reporting a `verdict` through the `verify` contract (task 118) is an
infrastructure fault, not a verdict, and must never be scored as a validation
failure: the engine retries verification (bounded, `MAX_VERIFY_ERROR_RETRIES = 3`)
without touching the task's `status` or `validationAttempts`. If every retry also
errors (or the iteration budget runs out first), the engine gives up on verifying
that task for now, logs an error event, and leaves its status exactly as the
worker left it -- it is not marked `validation-failed`, `failed`, or otherwise
penalized. The verify iteration's `meta.json` records `verifyOutcome: "error"`
for these attempts (distinct from `"pass"`/`"fail"`).

Task 012 (#45) generalized that from "errored out" to **reached no verdict**,
because an in-band error is only one of the ways an attempt can end without a
verifier ever finishing: the full `iteration_timeout_s` firing (`timed_out`), the
startup-window watchdog killing a silent agent (`no_traffic_timeout`) and a
signal (`interrupted`) all record no `error_message` at all, so they used to fall
through to the verdict-miss bookkeeping -- burning a validation attempt and
writing a `validationNotes` that blamed the worker for the engine's own timeout.
All four shapes are now the same case, decided in one place
(`LoopSupervisor._verify_no_verdict`): retried within the same bounded budget,
and if no attempt reaches a verdict, `status`, `validationAttempts` and
`validationNotes` come out byte-for-byte as they went in (as they do when the
iteration budget ran out before any verify iteration could run at all). The
record distinguishes the two outcomes in words as well as in `verifyOutcome`: a
negative verdict says a verifier ran to completion and reported nothing the
harness could treat as `verified`, while a no-verdict path says the verifier
never reached a verdict and names what ended it.

### Criteria fingerprinting and edited-after-failure detection (task 008)

Every task gets a `criteriaFingerprint` (sha256 of its `successCriteria`
text) recorded in `tasks.json`, alongside the verbatim baseline text as
`criteriaOfRecord`: set on first sight (right after planning, or on first
observation for a task discovered mid-run). Any subsequent edit is flagged
UNCONDITIONALLY -- including while `validationAttempts` is still 0 --
setting a persistent `criteriaEditedAfterValidationFailure: true` marker
(once set, never cleared) and, when the task carries a recoverable
`criteriaOfRecord`, RESTORING `successCriteria` to that recorded text rather
than letting the rewrite stand; the restore is witnessed with its own
`task.criteria_edit_refused` event carrying both the refused and the
restored text. The marker is also what makes the refusal outlast the pass
that caught it: the engine-authored blocking finding it feeds
(`findings.flagged_criteria_findings`) RECURS on every later `review` pass
until the task is resolved, so one rewrite keeps blocking acceptance instead
of being noticed once and forgotten. (A run resumed from before this
recording existed, where the pre-rewrite text was never captured, falls back
to the pre-existing flag-and-track-forward behaviour, with that recurring
engine-forced blocking finding as the whole of the enforcement.) This catches a worker or planner
that quietly rewrites the bar it just failed instead of doing the work.
There are exactly two legal ways to move a task's criteria forward, each of
which advances the fingerprint and `criteriaOfRecord` atomically with the
replacement text so a legitimate change is never mistaken for one of these
unauthorized rewrites: a judge-authored amendment on a CONFIRMED petition
(`_apply_criteria_amendments`), and an operator-issued steer `task-update`
naming `successCriteria` (`steer_task_update`, #132). Task 009 feeds the
list of flagged tasks to the review prompt so at least one independent
re-verification of the new text happens before a `VERIFIED` verdict,
closing the gap where `_verify_task`'s `validationAttempts >= 3` skip would
otherwise let repeatedly-rewritten criteria dodge every check.

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

### The two meanings of `failed`, and why a `failed` task no longer strands a run (task 024, #33)

`failed` on a task used to mean both "a verifier judged this requirement unmet"
and "the engine consumed the last of this task's validation rounds". The engine
now labels its own verdict: when `_verify_task()` sees `validationAttempts` reach
`VALIDATION_ATTEMPT_LIMIT` it writes `failureKind: "validation-exhausted"` beside
the status, and an agent's own `failed` reads as `requirement-unmet`.
`state.task_failure_kind()` is the single reader and *derives* the kind for a
`tasks.json` written before the label existed (at or past the limit ⇒
exhausted), so an old plan still reads correctly.

The vocabulary matters because of what a `failed` task used to do to a run:
`worker.md`'s completion signal recognises only `completed` and `skipped`, so no
whole-plan completion claim was ever legitimate again, the run never reached
review, and the stagnation guard eventually replanned the wave against a
finished repo -- escapable only by an operator hand-editing `tasks.json`. The
worker gets exactly one iteration to apply one of its two honest resolutions
(carve the residual gap into a new task, or relabel `skipped` without claiming
the criteria were met -- both change `tasks.json`, so both reset the stagnation
counter); a worker that changes nothing hands the run to review instead:
`LoopSupervisor._unresolved_failures()` reports the `failed` tasks of a plan with
no actionable task left (`state.TASK_ACTIONABLE_STATUSES`), the loop emits a
warning `log` event naming them and their kinds, and enters `review`. Entry only
-- the reviewer still decides whether the run succeeded. A doomed *dependency*
chain gets the same routing one hop earlier, when it covers the entire
actionable remainder rather than leaving `_unresolved_failures()` anything to
find (v0.14 requirement D, #99, `doomed_plan_unverifiable` in the table below).
Task 003 (#166) reuses the SAME `_unresolved_failures()` check one layer up,
at the worker's own contract-dispatch site: a `plan_complete` verdict (§4.3
above) refused for no reason other than this exact shape is routed to
`review` with `blocked_with_failures` directly, before `contract_
enforcement.enforce_contract`'s re-ask ladder is ever invoked -- the ladder
still runs for every other refusal shape (a task was assigned this
iteration, or an outstanding task is genuinely `pending`/`in-progress`/
`validation-failed`, in which case `_unresolved_failures()` returns `[]` and
that task is this iteration's `assigned_task_id` anyway).

Every surface that *counts* task statuses reports both meanings (task 025, #33)
without learning a sixth status: `state.task_counts()` keeps counting both kinds
under `failed` -- so its status keys still sum to `total` -- and adds the
sub-counts `failedValidationExhausted`/`failedRequirementUnmet`, which sum to
`failed`. The words come from the same two places for everyone
(`TASK_STATUS_LABELS` for a tally, `format_task_status()` for one record), and
the per-task map `state.task_failure_kinds()` is served as `taskFailureKinds` by
`GET /tasks`, `ralphctl tasks --json` and the hub's run-detail payload, so the
browser renders a derived kind rather than re-deriving it in JS. `ralphctl runs`
and the hub's TASKS cell flag a `failed` plan as trouble under either meaning.

### The debt ledger's single write path (v0.19 requirement A.3, #155, task 006)

`task-verify.md` may verify a task anyway while leaving structured `notes`
on the contract payload (v0.19 requirement A.2, #154, task 005 --
`HAT_SCHEMAS["verify"]`'s optional `notes` array, each item shaped
`{clause, observed, suggested}`) when a clause is met only in form, not in
substance -- the gap requirement A.1's "be strict" rewrite tells a verifier
to NOTE rather than reject over. `_verify_task()` copies that list onto the
task record verbatim as `verifyNotes` in the very same write that sets
`status: "validated"` (SPEC §5.3) -- but a task-scoped field alone would
lose the note the moment a later approach rewrites `tasks.json` fresh from
"001", so nothing durable would survive the run that raised it.

`RunDir.record_debt(task_id, notes)` is the ONE write path for the second,
durable copy: called unconditionally from the same `_verify_task()` success
branch, immediately after the `verifyNotes` write, it delegates to `state.
record_debt_entries()`, which appends one entry per note (keyed
`"<task_id>/<n>"`, `n` the 1-based count of entries THIS task id already
holds in the ledger -- not a run-global counter) to `debt.json`, then
re-renders `debt.md` from the WHOLE updated list in the same call, so the
two files can never disagree about what the ledger holds. A non-list or
empty `notes` is a documented no-op, which is what makes calling it
unconditionally from every verified path safe. No second caller ever
writes `debt.json` directly: `render_debt_markdown()`/`debt_entry_line()`
are the same rendering `ralphctl docs`/the hub's document panel serve
`debt.md` through (`state.RUN_DOCUMENTS`, key `debt`) and that
`run_record.py` echoes for the run record's own open/resolved split --
three readers, one writer, one shared line-renderer so no two of them can
ever describe the same entry differently. An entry's `status` starts
`"open"`; nothing in this task ever writes `"resolved"` -- a later worker or
adjudication remedy resolving one is a future task's write, against the
same ledger, through the same reader/renderer pair.

### The multi-witness progress counter and its hand-off to verify (v0.19 requirements B.1/B.2, #153, tasks 010/011)

The worker loop's stagnation guard used to read one signal only: did
`tasks.json` change between the start and the end of a worker iteration? A
worker that kept landing real commits against the same task, pass after
pass, without ever tripping a `tasks.json` write, stagnated on paper while
doing genuine work -- and, symmetrically, a worker that spent a whole
iteration reading and searching without touching either the plan or the
repo still reset the counter merely by mentioning `COMPLETE`/being
interrupted. Task 010 widens the single check to an OR over **four
progress witnesses**, any one of which resets `stagnant` back to `0` --
only an iteration with NONE of them still increments it:

1. **(a) `tasks.json` changed** -- the original, sole signal: `before !=
   after` on the plan document itself.
2. **(b) a commit landed** -- the workspace's own git `HEAD` moved between
   `head_before`/`head_after`, captured immediately around the worker
   iteration's own subprocess call. A non-git workspace reads `None` on
   both sides and is never a witness by itself.
3. **(c) contract progress** -- a petition/`unsatisfiable`/`plan_complete`
   payload was actually APPLIED this pass; necessary because a worker
   petition can ride on an outer `no_verdict` reply, which would otherwise
   fall straight through to the stale `before == after` check with no
   other witness to catch it.
4. **(d) tool activity above a threshold** -- this iteration's own
   transcript recorded at least `LoopSupervisor.STAGNATION_TOOL_ACTIVITY_
   MIN_CALLS` (3) `tool_execution_start` events (`state.count_tool_calls`,
   a whole-transcript scan, the same shape `detect_verdict_gate_peek`
   already uses). A THRESHOLD, deliberately not "any tool call counts": a
   single exploratory `read` must not reset the counter every pass forever
   and defeat the guard's purpose.

"An iteration with commits and no `tasks.json` change does not increment
the counter" now falls straight out of the OR -- witness (a) alone no
longer decides anything on its own.

**A task that absorbed iterations used to be judged, not starved (task
011, retired at task 004, v0.20.1 PRD requirement B.1, gate finding
VG-002).** The widened stagnation counter above answers the opposite
failure -- an iteration with no progress at all -- so it never caught the
different failure v0.19's requirement B.2 named: one task absorbing pass
after pass of real commits with no verdict ever settling it, because
witness (b) resets `stagnant` to `0` every single time.
`LoopSupervisor._record_task_absorption_streak` used to answer that,
called every vigilant worker iteration immediately before
`_relabel_vigilant_claims`, tracking a SEPARATE counter keyed to one task
id (`status.json`'s `taskAbsorptionStreak`, `{taskId, count}`): reaching
`count == task_absorption_max_iterations` (default 3) forced the task
straight to `awaiting-validation` and into `verify` on the worker's own
behalf -- with NO regard for whether any claim on the task was ever
corroborated, which is exactly what gate finding VG-002 flagged: a task
under its own progress budget, with real work landing every pass, still
got yanked into verification on commit count alone.

Task 004 deletes `_record_task_absorption_streak` and its call site
outright rather than widening its exemption further -- nothing anywhere
reads `cfg.task_absorption_max_iterations` any more (the field itself is
kept, inert, only so two frozen v0.20 tests that configure it as a
`JobConfig` kwarg keep constructing). A task that keeps absorbing
commit-bearing `no_verdict` iterations now simply stays `in-progress`
through this whole block. What governs exhaustion instead is the
pre-existing progress-budget/forced-split path (task 016,
`PROGRESS_BUDGET_STATUS_KEY`, `estimatedIterations + 2`, `forced_split_
required`, read BEFORE each dispatch) -- see `tests/test_worker_hat.py::
test_three_consecutive_commit_only_iterations_never_force_verify_dispatch`
for the replacement pin.

### No-progress escalation guard vs. instant startup/infra failures (task 059)

The worker loop's stagnation guard (3 consecutive worker iterations with
none of the four progress witnesses above ⇒ fail the approach) and
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

Task 017 (v0.19 requirement D, #151): `_check_instant_failure` IGNORES an
attempt whose error text matched a genuine `INFRA_SIGNATURES` row (DNS,
TCP, TLS, a gateway 5xx, back-pressure, …) entirely — it touches neither
the streak nor its signature, leaving that attempt to the infra outage
budget and backoff ladder alone (§10.5). Before this task, three identical
instant `ECONNREFUSED` attempts tripped this same counter exactly like
three identical broken-credential ones, ending a recoverable outage in
seconds instead of letting the (hours-long, by default) outage budget
govern it. An *unclassified* no-traffic failure — still `classify_fault()`'s
`"infra"` by its own deliberate catch-all, but with no signature match — is
NOT exempted: that shape is exactly the missing/broken-credential incident
this counter exists for, so it still counts and can still fail fast well
inside the outage budget.

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
**Since v0.20 the window measures agent silence, not the host's scheduling
delay**: on expiry the watchdog reads the child's own CPU counter
(`runner.process_cpu_seconds()`, `/proc/<pid>/stat`'s
`utime+stime+cutime+cstime`) and, if it *advanced* during the window, re-arms
the same window instead of returning a verdict — a child that keeps
consuming CPU is being starved by a contended box, not hung, and the wall
clock of OS process creation plus the child's own runtime cold start is
something the agent has no control over. A child that has consumed CPU and
then stops consuming it is blocked, which is the shape this watchdog exists
for (a `getaddrinfo` wait), and it is still killed on the first such window;
no deadline is widened anywhere, and the re-arm cannot run forever because
the ordinary `timeout_s` wait still bounds the iteration (so a silent child
that merely *spins* is ended by that timer instead). When the counter cannot
be read at all (no `/proc`, process already reaped) the window expiring is
the verdict, exactly as before. This is what made
`tests/flows/test_flow_crash_resume_refunds.py` and
`tests/flows/test_flow_infra_ladder.py` red on the nightly self-hosted box
(one 4-core runner, `max-parallel 2` across `gate`, `heavy` and `nightly`):
a healthy `planning` invocation that had not been given a CPU slice yet was
killed and retried as an infra fault, inflating the flow's own step sequence
by one `infra_retry:planning:1` round.
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
*budget comparison* is adjusted), and since task 023 (#32) the counter is
persisted in `status.json` (`iterationsRefunded`) and seeded back on resume
instead of restarting at zero. It also never touches
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
infra-retry attempts, persisted the same way (`iterationsRefunded`, #32) — so
it never counts against `cfg.iterations`. The `_grace_review_granted` set
itself stays per-process: a resumed engine may grant one more, which is why
the *refund* is persisted and the *grant* is not. If the
grace review comes back `VERIFIED` (and no operator steering is left
pending unconsumed), the job goes terminal `succeeded`/`verified` with
`status.json`'s `graceReview: true` and a `reason` stating the grace
review ran and verified — **unless the opt-in peer verdict gate
(`peer_verdict_gate: true`, task 020, PRD requirement L) is on**, in which
case this path gets its own single, equally BLIND off-budget
`verdict_gate` iteration first (`_grace_verdict_gate()`, refunded through
the same `grace` channel, bounded by the same `_grace_review_granted`
guard, and called with no `extra` context so the grace review's own
verdict/`reason` never reach the peer). The gate is a property of the run
reaching terminal success, not of one particular edge into `finishing`:
`_maybe_grace_review()` is the only other site that writes
`succeeded`/`verified`, and a peer rejection (or any unusable reply,
`_verdict_gate_verdict()`'s shared safe default) leaves the ordinary
failed/unverified terminal write below — there is no approach to replan
into, since exhausted budget is why this path ran at all. If it does not verify, the job still ends
failed/aborted exactly as it would without this feature — no second
approach is attempted once budget is exhausted — except the terminal
`reason` now says a grace review ran and did not verify, or that the off-budget blind peer verdict
gate did not verify (via
`_terminal_reason_note`, kept separate from `_abort_reason` so a plain
unsatisfied grace review doesn't relabel the terminal `state` from
`failed` to `aborted`). The negative case (tasks NOT all completed when
budget exhausts) is unaffected: `_maybe_grace_review()` returns
immediately without touching anything, and the job fails exactly as
before this feature.

### Verdict values beyond plain verified/unverified (v0.18 requirements D.3/F.3/F.1)

SPEC §5.2 documents `status.json`'s `verdict` field contract; this section is
the design rationale for the two extra terminal values and the one legal
mid-run non-terminal value that is never written to `status.json` at all.

- **`verified-with-residuals` (task 012, requirement D.3).** A `verified`
  review that CONTRADICTS its own structured findings (`{"verdict":
  "verified"}` alongside a `severity: "blocking"` finding) is already a
  contract violation the ladder above rejects; a `verified` review whose
  findings channel carries `severity: "residual"` findings ONLY is not a
  violation at all -- residual findings are, by the rubric's own vocabulary,
  things that do not block a pass. Writing that run's terminal `verdict` as
  plain `verified` would silently drop them from the one field every
  surface (`ralphctl status`, the hub's run list, `run-record.md`'s own
  header) reads first. `LoopSupervisor._review_success_verdict_label()` is
  the ONE place that decides between the two words, called from every site
  about to write `_finish("succeeded", ...)` off a VERIFIED outcome (the
  ordinary review pass, the peer verdict-gate approval, and the grace
  review) -- it never itself decides curability or blocking-ness, only
  whether a genuinely verified approach still has something worth naming.
- **`unreviewed` (task 016, requirement F.3, #146).** Before this task, a
  deadline that took the last budgeted attempt while `review` was still
  mid-flight -- the ordinary self-loop's own exhaustion, or the off-budget
  grace attempt -- landed on the SAME `unverified` verdict a genuine
  rejection or an operator abort would, so a run a reviewer never actually
  looked at read identically to one a reviewer looked at and rejected.
  `_review_deadline_unreviewed` (an instance flag set the moment the
  deadline is detected mid-review, alongside a `review_deadline_unreviewed`
  event) is read by the tail of `_finish_tail_unverified()`: when it is set
  and this was not an operator abort, the terminal write is `verdict:
  "unreviewed"` with `reason: "deadline during review"` instead of
  `unverified`. Resume routes back into `review` over the same approach
  (`_unreviewed_awaiting_resume`) rather than treating the abandoned
  attempt as a rejection to replan away from.
  - **`unreviewed` also covers "review's own contract ladder exhausted"
    (GH #209 defect 2).** A review iteration that ran to completion but
    whose own verdict claim was unreadable — no parseable object at all, or
    a `verdict` outside `HAT_SCHEMAS["review"]`'s two-word enum — in the
    reply the run was CHARGED for, with the ladder then exhausting without
    ever returning a repaired payload, sets
    `_review_contract_unrepaired`, and `_finish_tail_unverified()` reads it
    for a second `unreviewed` branch with its own reason string ("review
    never produced a parseable contract payload; no verdict was ever
    scored"), never the deadline's. A run with budget left never reaches
    that terminal write at all: `_step_review` takes the declared
    `review_contract_unrepaired` edge and HOLDS (`pauseReason:
    "review_contract_unrepaired"`, the approach left unarchived, no
    composite PRD), so only the off-budget grace review lands here. The
    same word is also what `approach.verdict_recorded` carries for that
    approach's `review` channel, in place of the `rejected` it used to
    attribute to a judge that never spoke. One thing the parent bullet's
    last sentence does NOT extend to this writer: `_unreviewed_awaiting_
    resume` keys on the `review_deadline_unreviewed` EVENT, which this
    branch deliberately never emits (no deadline was involved), so a FRESH
    process resuming a run that ended here falls to `_resume_point`'s
    default — skip planning, re-enter `worker` — and spends one throwaway
    worker iteration over an all-completed plan before review is asked
    again. That is the same precedented cost the `review_contract_
    unrepaired` PAUSE already pays for having no `_resume_point` bucket
    (`rejected_repeated_signature`, `unschedulable_dependency_cycle` and
    `unresolvable_dependency_edge` all pause with none either); giving this
    branch the deadline's event to buy the routing would put "deadline
    during review" back on a run no deadline touched.
- **`"adjudicated"` (task 014, requirement F.1, #144) is a MID-RUN reply
  value, never a `status.json` verdict.** The bounded, off-budget petition
  adjudication episode (`_maybe_run_midrun_adjudication_episode`, next
  section) answers the review contract's `verdict` field but scores no
  approach and writes no terminal state; `transitions.MIDRUN_REVIEW_SCHEMA`
  is the separate, wider schema this one call's contract ladder validates
  against (never `HAT_SCHEMAS["review"]`), adding `"adjudicated"` as a
  third legal value and making `verdict` itself optional -- so a reply with
  nothing terminal to claim about the approach is never read as a contract
  violation the way it would be from an ordinary run-ending review.

### Mid-run petition adjudication: settling an impossibility petition without waiting for the run to end (v0.18 requirement F.1, task 014, #144)

An impossibility petition (`docs/petition-format.md`) filed against a task
can sit OPEN for the rest of an approach with no ordinary review in sight --
review only runs once the worker loop as a whole signals completion, and a
petitioned task's own `validationAttempts` keep burning against a verify
ladder that will never resolve favourably while the petition stays
unadjudicated. `LoopSupervisor._maybe_run_midrun_adjudication_episode()`
runs, unconditionally, right before `_step_worker`'s own `pending_verify`
loop hands any task back to `verify` -- a no-op in the overwhelming common
case (no petition open at all), and otherwise exactly ONE bounded, review-
hat-model iteration per set of newly-open obligations
(`self._petition_adjudication_seen`, so the SAME open petition is never
re-asked forever just because it stays open). The episode reuses
`_review_verdict(..., mid_run=True)` for the ENGINE side effect an ordinary
review iteration already applies from the identical `petition_adjudications`
payload field -- CONFIRMED entries persist at run scope
(`petitions.record_confirmed_adjudications`), REFUTED entries move the
matching `petitioned` task straight to `failed` carrying the remedy, and a
REFUTED `substance` remedy ALSO becomes one cure task through
`state.insert_cure_tasks`, the same call review's own cure pass makes
(`LoopSupervisor._insert_refuted_remedy_tasks`, #209 defect 4) -- and,
either way, the obligation is RETIRED from the open set: the read site both
this episode and an ordinary review pass scope themselves to
(`petitions.open_unconfirmed_petitions`) drops every obligation the
run-scope record already settled, through `petitions.settled_obligations`
(CONFIRMED **and** REFUTED, suppression only -- never an exemption; the
exemption readers stay CONFIRMED-only).
The in-process `self._petition_adjudication_seen` guard above was not
enough for that on its own: it is per-process, so it resets on an engine
restart, and it does not cover an ordinary review pass's own prompt at all
-- which is how one REFUTED obligation came to be re-argued on six
consecutive review passes, 13 `petition_refuted` events for 3 recorded
entries (#209 §5). Retirement is not deletion: the entry, its remedy, its
`petition_refuted` event and the run record's rendering of all three are
unchanged. This episode never `_advance`s a state, never `_finish`es, and
is refunded through the same off-budget `grace` channel the grace review
already uses, so it never competes with the run's iteration budget or a
task's own `validationAttempts`. See the verdict-values section above for
the `"adjudicated"` reply value this episode alone may use.

### A `petitioned` task holds its dependents, pending exactly this adjudication (v0.19 requirement C.1, #149, task 013)

A task routed to `petitioned` (the verify-exhaustion branch just above,
v0.18 requirement C.1, #114) is not `failed`/`skipped`, so `state.
task_blocked_by()`'s ordinary dead/doomed fixpoint (SPEC §5.3) never marks
it, or anything depending on it, as doomed -- it can still resolve. But
leaving a dependent reading merely `pending` is its own lie: nothing can
actually complete it while its ancestor's obligation is still open, and
the worker has no way to tell "genuinely actionable" apart from "waiting on
someone else's petition" from the plan alone.

`task_blocked_by(..., include_petitioned=True)` is the resolution: a
SEPARATE, opt-in cause joins the same map doom already populates -- a task
whose `dependsOn` names a `petitioned` ancestor DIRECTLY is listed with
that ancestor as its cause, one hop only, never propagated through the
doomed fixpoint (a petitioned task's own dependents are held; a doomed
task's dependents downstream of THOSE are a different, harder fact this
widening deliberately does not claim to answer). This is a HOLD, not a
verdict: the four display surfaces that pass `include_petitioned=True`
(`ralphctl tasks`, `GET /tasks`, the hub's task-detail dialog, `worker.md`'s
own prose) say a dependent is blocked on a specific petitioned ancestor,
never that it is doomed alongside it.

The hold's own resolution is the mid-run adjudication episode just above,
and nothing else -- there is no separate poll or timer. The SAME petition
that put the ancestor into `petitioned` is exactly what
`_maybe_run_midrun_adjudication_episode` settles the next time it runs
(before the next `pending_verify` hand-off): CONFIRMED leaves the ancestor
exactly where it is (still `petitioned`, dependents still held, an operator
ruling is the next move); REFUTED moves the ancestor to `failed` with the
remedy attached, which -- being a `failed` status, not `petitioned` -- now
DOES fall under the ordinary dead/doomed fixpoint, so every task that was
merely held on it becomes properly doomed -- except that for a `substance`
remedy the ENGINE now does the re-pointing itself (#209 defect 4): the same
`insert_cure_tasks` call that builds the remedy task recognises the
just-failed ancestor by id in the synthesized finding's own evidence
(`_failed_task_ids_named_in`) and hands every one of its dependents to that
remedy task (`_repoint_dependents_onto_cure_task`), so the doom is no longer
left to the worker to escape. A `form` remedy changes no deliverable and
creates no task, and a remedy against an obligation with no filed OPEN
petition creates none either, so in those cases the doom is ordinary and the
worker's own two honest resolutions -- or its `proposed_task_updates`
re-point of a dependent the harness itself derives as doomed (#209 defect 7),
or an operator ruling -- remain the escape, same as any other `failed` task.
A held dependent is therefore never
permanently stuck: either the ancestor resolves and the hold lifts, or it
fails and the hold becomes an actionable doom -- with, where the refutation
named work, a replacement task already carrying its dependents.

### The adjudication MAC and its run-secrets mount (v0.19 requirement E.4, #150/#152, task 020)

`petition_adjudications_recorded` (`petitions.record_confirmed_adjudications`)
keys a MAC over the CONFIRMED-adjudication record with a per-run secret that
lives ONLY in the engine process's own memory for the episode
(`petitions._AUTHORITIES`, never a file under the agent-owned run dir --
see that module's `key_store_dir` docstring for why). That is enough to
catch a worker-phase rewrite within one episode, but a `ralphctl resume`
starts a fresh engine process with no memory of the old key, so without
somewhere to persist it, an EARLIER episode's record could no longer
authenticate after a restart and would read as tampered/empty.

`ralphctl start`/`resume` (`src/ralphd/cli/main.py`, `_bind_run_secrets_mount`)
bind a mode-0700 host directory, `~/.ralphd/configs/<run>/run-secrets/`
(`run_secrets_dir(run_id)`, next to that run's `job.yaml`, never inside the
agent-owned run dir), at the fixed container path
`petitions.DEFAULT_RUN_SECRETS_DIR` (`/var/lib/ralphd/run-secrets`) on every
`start` and every `resume` -- created and mode-fixed unconditionally each
time, so a run dir that predates this task self-heals on its first resume
rather than needing a manual migration. Inside the container,
`petitions.key_store_dir()` only actually uses that mount when
`privsep.separated()` is true (the engine started as root and dropped to the
agent uid -- the real container's shape, not this repo's own test suite,
which runs single-uid): there the mount is root-owned 0700 with a root-owned
0600 key file inside it, neither readable nor replaceable at the agent uid,
so a later episode's read of an earlier episode's key is trustworthy.

**The read is elevated too, symmetrically with the write.** "Not readable at
the agent uid" includes the ENGINE, whose own effective uid is the agent's:
a plain `open()` of that root-owned 0600 file fails `EACCES` inside the
engine as surely as inside a hat process. So `petitions._read_stored_key`
tries the ordinary read first -- that is the whole read for an operator's
agent-readable `$RALPHD_RUN_SECRETS_DIR` store, and for a key file an older
pre-boundary build wrote agent-owned, neither of which takes any elevation
-- and only when that yields nothing and `privsep.separated()` is true does
it retry inside `privsep.root_for_write()`, the same momentary raise back to
root `_persist_key` already used, needing no privilege the engine does not
already hold (real and saved uid are 0 exactly when `separated()` is true).
Without that retry the store was write-only in the one configuration that
populates it by default: episode 2 read episode 1's own CONFIRMED record as
unauthenticated and fell back to empty. `artifacts/reports/v0.19-e4-privsep-key-read.md`
records the reproduction under a real root engine, before and after.

**The documented fallback.** With no uid boundary (or an operator override
via `$RALPHD_RUN_SECRETS_DIR` naming nowhere readable), `key_store_dir()`
returns `None` and there is no on-disk key at all -- a later episode cannot
authenticate an earlier episode's CONFIRMED record and treats it as empty,
failing closed rather than trusting an unverifiable claim. The fallback
check for "did this run's adjudications actually happen" in that condition
is the same `events.jsonl` witness every episode already writes:
`ADJUDICATIONS_RECORDED_EVENT` (`petition_adjudications_recorded`) carries
the MAC of the write that produced it, independent of whatever
`petition-adjudications.json` says on disk right now. `ralphctl status`
reports the two conditions SEPARATELY per run, because the mount existing
is not the key existing: `runSecretsMount.bound` is whether
`start`/`resume` created and bound the directory, and
`runSecretsMount.keyAvailable` (with `keyFiles`, from
`cli/main.run_secrets_key_files`, which looks for the very file
`petitions.stored_key_filename` names for the run root `/run/ralphd`) is
whether a non-empty key actually survived into it. Whenever no key is
available -- the ordinary single-uid shape, where the directory is bound
and stays empty for the whole run, as much as a directory that was never
bound at all -- the text line says `no MAC key available` and names this
fallback check in the same line, and `runSecretsMount.fallbackCheck`
carries the same sentence in `--json` (it is `null` exactly when a key is
there). `ralphctl doctor` names the mount and its fallback
unconditionally, registry-wide.

### The run's own terminal record: run-record.md/.json (v0.18 requirement B.1, #140, cured by task 035)

Every closing report a worker or reviewer writes is the AGENT's account of
the run; nothing stops it from omitting an inconvenient rejection or citing
an approach that never happened. Requirement B.1 asks for a second, engine-
authored account that cannot make that mistake because nothing in it is agent
prose: `src/ralphd/engine/run_record.py` builds a record entirely out of
files the engine itself already owns and writes -- `status.json`,
`approach-metadata.json`, `petition-adjudications.json`, the steering ledger,
`CONFIG_DIR/amendments/`, and (only as a fallback for an approach judged
before task 035 existed) the archived `approaches/NN/` findings sidecars --
and renders it two ways: `build_run_record()` for `artifacts/run-record.json`,
`render_run_record_markdown()` for the identical record as `artifacts/
run-record.md`. `write_run_record()` writes both together, called from
`LoopSupervisor._write_run_record()` at every entry to `finishing`
(`_finish()`) and rewritten on the later terminal transition that promotes a
recorded `finishing` write (`_promote_finishing_state()`/
`_promote_recorded_finishing_state()`), so the pair on disk always reflects
the most recent terminal decision even across a container restart mid-
`finishing`.

**Why the per-approach judge verdicts live in their own engine-owned file
rather than being read off the findings sidecars alone (task 035, cure for
task 006).** `review-findings.json`/`verdict-gate-findings.json` are written
by the JUDGE hat's own process, are optional (a judge that verifies clean
writes no sidecar at all), and are cleared/archived by the SAME approach-
archiving step the run record has to describe -- so a record built only from
them cannot tell "this approach verified with nothing to say" apart from
"this approach's judge never ran a sidecar-writing pass at all", and loses
the distinction entirely once an approach is archived without one.
`state.record_approach_verdict()` fixes this by persisting the ENGINE's own
copy of each judge channel's verdict -- explicit `blocking`/`residual`
arrays, `findingsChannel: bool`, `recordedAt` -- into `approach-metadata.json`
at the run root the MOMENT that judge's verdict is decided
(`LoopSupervisor._review_verdict`/`_verdict_gate_verdict`, never a mid-run
adjudication episode, which scores no approach verdict), independent of
approach archiving. `run_record.build_approach_history()` reads that
metadata FIRST for every approach it lists (live or archived) and only falls
back to a findings sidecar for an approach this engine-owned file has no
entry for at all -- a run entirely on this side of task 035.

### Model resolution

Each hat resolves its model independently: per-hat override (`models:`/
`model_overrides:`, keyed by `planning`, `worker`, `verify`, `review`,
`reflect`, `steer`, `plan_review`, `verdict_gate`, `router`) → job default (`model`, `None` =
pi's own default). An unrecognised hat name degrades to `planning`'s own
configured entry (`UNRECOGNISED_HAT_FALLBACK`), never to some cheaper tier —
a typo must never quietly downgrade a phase. `steer` (task 009, PRD
requirement G), `plan_review` (task 018, PRD requirement K, the opt-in
peer plan-review gate — off by default via `peer_plan_review: false`) and
`verdict_gate` (task 020, PRD requirement L, the opt-in, genuinely BLIND
peer verdict gate — off by default via `peer_verdict_gate: false`) are
all real, first-class entries here, resolved through this same table like
every other known hat — never through the unrecognised-name fallback, which
is reserved for a typo, not a real role. `router` additionally has its own
dedicated `router_model` key, checked before `model_overrides["router"]`'s
sibling rung.
Each hat's `(model, thinking)` PAIR — never just the model — is what
`JobConfig.bundle_for(hat)` (task 007) actually returns: `thinking_overrides:`
is the same per-hat table for `pi --thinking`/reasoning settings, kept as a
separate field from `model_overrides` rather than folded into it, because a
model ref and its thinking setting are two independent axes that can be set
independently.

Task 006 retired the earlier `model_strategy`/`fast_model` phase→tier
preset table (`quality-first`/`cost-optimized`/`balanced`) entirely — there
is no implicit "fast" tier any more without an explicit `models`/`model_overrides` entry naming it. A job.yaml still carrying either
retired key is translated deterministically into an equivalent `models:`
table at load time (`engine.config._migrate_legacy_model_config`), so an old
run keeps resolving to the same models it always did; `ralphctl config set
model_strategy`/`fast_model` and `ralphctl start --model-strategy`/
`--fast-model` are refused outright, naming the `models:` replacement.

Every model ref is just a pi "provider/model-id" string in job config — any
model pi can reach is valid in any hat's slot. The engine sets the model
per-iteration via pi's model selection flag/env on the subprocess.

### Tiered provisioning (task 012, #68 half 2)

The per-hat bundle above is one CELL of a wider matrix: task 012 gives every
hat its own ladder of exactly three ordinal tiers, `trivial < normal < hard`
(`engine/tiers.py`) — not a configurable count, and each hat's ladder is its
own (no relationship assumed between one hat's `hard` rung and another's).
`tiers.default_host_map(cfg)` widens `JobConfig.bundle_for`'s existing
per-hat table into the `{hat: {tier: HatBundle}}` matrix by handing every
rung of a hat's ladder that hat's single already-resolved bundle, so a
job.yaml naming only `model`/`model_overrides` collapses cleanly: tiering is
a no-op, never an error, with no special-case branch.

**The resolver is a pure function applied at dispatch, never a transition-
table entry.** `tiers.resolve_bundle(hat, effective_tier, host_map) ->
HatBundle` does no I/O, touches no clock, and never reads `JobConfig` itself
(`default_host_map` is the one place that happens, once, by delegating to
`bundle_for`, so there is exactly one model-resolution pathway, not two).
`transitions.TABLE` stays untouched by any of this — the table is about
*what happens next*; provisioning is *how*, resolved at the single dispatch
call site in `loop.py`'s `run_iteration()` (task 016), which passes in
whichever tier applies for that call: a task-scoped call (worker, per-task
verify) reads the in-hand task's effective tier; every hat with no task in
hand (planning, reflect, the router's own call) keeps its static half-1 role
at `tiers.DEFAULT_TIER`; the run-ending verification that decides the run's
own verdict always provisions at `tiers.TOP_TIER`, unconditionally — the exit
gate is never economised.

**A task's tier moves only up, and only on validation-failed.** Task 013
lets the planner annotate an optional `tier` per task at ingestion time
(defaulted to `normal` when missing/unrecognised, with a warning — never
rejecting the plan); `tasks.json`'s engine-written `effectiveTier` is claimed
equal to that baseline on first claim and escalated exactly one rung
(`tiers.escalate_tier`, capped at `hard`) per `validation-failed` re-claim
(task 014) — there is no de-escalation path, true by construction: no
function that touches `effectiveTier` ever moves it down. A task already at
`hard` that fails validation again is marked `tierLadderExhausted` instead,
feeding the stagnation guard. Per-task verify then provisions one rung BELOW
the work it is checking (`tiers.step_down_tier`, floored at `trivial`, task
015) — checking is cheaper than doing, never cheaper than the ladder's own
floor. Spend and iteration counts are broken down by tier the same way they
already are by phase and approach (`usage.byTier`, task 017), and the
highest `effectiveTier` any task currently carries is published on the hub/
`ralphctl runs` row (task 019) so escalation is visible without opening a
task's own detail.

### Self-reflection phase (PRD req 24)

Optional (`reflect: true` in job config, `ralphctl start --reflect`). After the
job's normal loop (`LoopSupervisor._run_job_core()`) reaches a terminal state
(`succeeded`/`failed`/`aborted`), the engine runs exactly one extra `reflect`
iteration (`LoopSupervisor._run_reflection()`), using its own builtin prompt
(`src/ralphd/prompts/reflect.md`, phase name `reflect`, model resolved per the
job's per-hat model table exactly like any other hat). This iteration is *not* part
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

Task 121 replaced the phase's bare `<promise>REFLECTED</promise>` sentinel
(never scored by the engine) with a `reflect` response contract
(`{"report_written": ..., "suggestions_written": ..., "notes": ...}`,
`transitions.HAT_SCHEMAS["reflect"]`): `LoopSupervisor._reflect_verdict()`
parses the reply's last non-blank line as JSON and validates it
(`contracts.validate_payload` plus `transitions.reflect_report_errors`, which
requires a non-empty `notes` on a `report_written: false` claim), and
`_reflect_failure()` treats a missing/malformed contract or a
`report_written: false` claim as a reflect failure ON TOP OF, never instead
of, the pre-existing check that `artifacts/reflection/report.md` actually
exists on disk.

With `reflect` absent/`false` (the default), no extra iteration runs at all —
`run_job()` is a no-op wrapper around the same core loop as before this feature.

### A self-dev wave runs the *installed* engine, not the workspace's (v0.20.1 requirement E.6)

A **self-dev wave** is a job whose workspace is ralphd's own source checkout —
the PRD asks an iteration to change the very code that is executing it, exactly
as this run does. That workspace copy is inert prose and modules until some
process imports it; the process actually doing the importing, for `ralphd-engine`
PID 1 in this container, is whatever `ralphd` build was `pip install`ed (or
baked into the job image) *before the container started* — never the tree
under `src/ralphd/` that the current iteration is editing on disk. Editing
`src/ralphd/engine/loop.py` in the workspace does not change which `loop.py`
is already loaded in this process's memory; the new code only takes effect
once a *later* wave's image is built from that edited tree and a fresh
container starts from it. Any report or test claiming a behavior "changed
this run" because a file changed on disk, without a fresh process import to
back it, is asserting something about the workspace, not about this engine.

This is why a self-dev wave's own PRD cannot add a newly *required* field to
a hat contract or a findings schema and have that requirement enforced in the
same wave that adds it: the engine validating every iteration's payload
against `transitions.HAT_SCHEMAS`/`contracts.HAT_SCHEMAS` for the rest of
*this* run is the installed build from before the edit, so it still enforces
the *old* shape. The rule this forces is **permissive-then-required**: a newly
added contract or findings field lands **permissive** (optional,
`additionalProperties` still accepting payloads that omit it) in the wave that
introduces it — wave N — and only becomes **required** starting in wave
N+1, once a build carrying the change is what a later wave's container
actually installs and runs. A schema edit that skips straight to required in
wave N is not a stricter contract; every iteration of the *current* run still
executes the pre-edit validator, so the change is invisible until the next
wave's image, and the workspace edit was validated against nothing this run
ever exercised.

## 3. State model

All run state lives in `/run/ralphd` inside the container (the engine's
`RALPHD_RUN_DIR`, `engine/config.py: RUN_DIR`), which is **always a host
bind-mount** of `~/.ralphd/runs/<run-id>/`. This is what makes history survive the
container and lets the CLI read state even when the container is dead.

```
~/.ralphd/runs/<run-id>/          # mounted at /run/ralphd in the container
├── status.json         # engine-maintained: phase, iteration, approach, verdict
├── host.json           # host-side facts ralphctl recorded: apiUrl, container,
│                       #   workspace path(s), network, resolved image
├── prd.md              # original PRD
├── composite-prd.md    # approach ≥2 only
├── tasks.json          # task state (source of truth)
├── .tasks-last-good.json # fallback copy, written ONLY when a read of tasks.json fails
├── notes.md            # agent handoff notes, ≤50 lines enforced by prompt
├── standing-rules.md   # harness-pinned copy of notes.md's "## Standing rules" block (task 107)
├── review-findings.md  # written by failed reviews
├── review-findings.json # the same findings, structured (severity/curable/clause/evidence, task 001, #110)
├── verdict-gate-findings.md # the peer verdict gate's own free-markdown findings, when it rejects (task 019, requirement D.4, #125)
├── verdict-gate-findings.json # the same findings, structured, the sidecar `verdict_gate_findings_json_file` points at
├── petition-adjudications.json # run-scope CONFIRMED impossibility-petition adjudications, survives _archive_approach (task 011, #112)
├── petitions.md        # RUN-scope impossibility-petition filings/adjudications, never the workspace tree (task 065, requirement N.6, #150)
├── approach-metadata.json # engine-owned per-approach judge verdicts (review/verdictGate, explicit blocking+residual arrays), written when the judge runs, survives _archive_approach (task 035, requirement B.1)
├── vigilant-verified.json # task ids that have passed a verify iteration
├── debt.json           # per-run debt ledger: verify/petition notes recorded in form but not substance (task 006, requirement A.3, #155)
├── debt.md             # debt.json rendered for a human, served by `ralphctl docs`/the hub document panel via state.RUN_DOCUMENTS
├── operator-termination.json # what the operator asked for (abort/interrupt)
├── .lock               # the engine's exclusive flock (self-protection, below)
├── steering/           # steering inbox: 001-*.md, 002-*.md …
│   └── .consumed.json  # which of them an iteration has already been given
├── iterations/         # per-iteration record
│   └── 0007/
│       ├── meta.json   # phase, model, start/end, exit code, signal seen, usage
│       ├── prompt.md   # the exact prompt this iteration was given
│       ├── output.jsonl# full agent transcript (pi session log)
│       └── attempts/01/# an earlier, crashed attempt at this same number (#44)
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
      "failureKind": "validation-exhausted|requirement-unmet, engine-written when failed",
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
`failed`/`skipped` dependency (directly or transitively, through another
merely-blocked task) is never silently ground against, but the worker does
not have to notice or record it either -- the harness derives it itself
(`engine.state.task_blocked_by`, task 021, requirement F) and marks the
dependent `blocked` on every surface (`ralphctl tasks`, the hub, and
`GET /tasks`'/`GET /status`'s own `taskBlockedBy` map / `blocked` count)
the moment the dependency reaches `failed`/`skipped`. With no
`dependsOn`/`priority` fields anywhere in the plan this is identical to
plain sequential list-order picking. This pick rule lives entirely in the
phase prompts (the engine does not parse or enforce it in code) — it is the
agent reading `tasks.json` and the prompt's instructions, same as task
selection always has been; the `blocked` DERIVATION, unlike the pick rule
itself, is engine code (`engine/state.py`), not prompt text.

A task may also carry `orderingOnly` (GH #209 defect 3), a planner-declared
subset of its own `dependsOn` ids that are pure ORDERING edges -- sequencing
alone, producing none of the evidence that task's `successCriteria` reports
on. It changes no scheduling whatsoever, but unlike the pick rule above it
IS engine-read, at both ends: refused at ingestion when it is not an array
or when an entry names an id the task does not depend on
(`plan_ingestion.validate_proposal_shape`/`lint_proposal` — a bad entry
refuses the whole proposal), and then read by the cure pass's evidence walk
below (`plan_ingestion.ordering_only_dependencies`, its one consumer), which
without it drags such a dependency from `validated` back to `pending` merely
for sitting in a re-staled record's `dependsOn`. An absent field means
"every edge produces evidence", so a plan that never names it behaves
exactly as one written before it existed.

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

The **workspace** (`/workspace`) is separate from `/run/ralphd`: it is the repo checkout the
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
guidance -- include the full steering text and can mark it consumed (recorded in
`meta.json`'s `steeringDelivered`/`steeringConsumed` and in
`steering/.consumed.json`). The
`review` and `verify` phases are pure verification roles ("Do NOT fix
anything yourself"; nothing in their prompts tells the agent to act on
steering), so if a steering file arrives while a worker iteration is in
flight and the very next iteration boundary happens to be a review or verify
iteration, that iteration only sees a passive notice (file names, no content,
not marked consumed) and leaves it pending -- it is picked up and consumed by
the next planning/worker iteration instead. This prevents steering from being
silently discarded (recorded as "consumed" yet never actually acted on) when
it happens to land just before a non-worker-bound phase.

**Every hat's prompt also carries a read-only context echo of recent operator
messages** (`## Recent operator messages (read-only context)`,
`LoopSupervisor._recent_steering_echo()`) -- the successor of the passive notice
above, which reached only the non-actionable phases and only while a message was
still pending, so a message vanished from every prompt the moment it was applied.
The echo lists the run's most recent messages (capped in count and per-message
length, with the file path for the full text) with each one's state, whatever the
phase. It carries **no consumption semantics**: building a prompt neither
consumes nor settles anything (`RunDir.consume_steering` remains the only
writer), `applied (settled)` says only that some hat processed the message and
never that the tasks it produced are `done` -- `tasks.json` is the single source
of truth for that -- and being listed is not permission to act, which is what the
actionable/passive block above still decides.

**Delivery is at-least-once; application is at-most-once (issue #34).** Being
handed to an actionable phase is not enough either: the marker append happens
only after that iteration has *finished cleanly* (`faultClass is None`), so the
ordering is build the prompt -> run the agent -> record the outcome -> append to
`.consumed.json`. An iteration that fails, is interrupted, times out or dies with
the engine therefore leaves its notes pending and the next actionable iteration
is handed them again -- the alternative (marking them at iteration start, which
is what v0.6 did) turns any dead iteration into a silently discarded operator
instruction that the run nevertheless reports as `applied`. Re-delivery cannot
apply a note twice: `RunDir.consume_steering` skips names already in the marker,
so neither the `applied` state nor the `steering.consumed` event can be doubled.
The per-iteration record keeps the two apart, so an operator can see a note was
handed to iteration 12 and only earned by iteration 13.

**A `VERIFIED` verdict is refused while steering is unsettled or a
steering-origin task is unfinished.** Passive notice at review time is not
enough on its own: if a steering file arrives while the worker is in flight
and the very next iteration boundary is the review that would otherwise end
the job, the engine must not let the run go terminal-succeeded with that
steering permanently stranded (a terminal run never reads pending steering
again). Both exit-gate sites -- `_step_review`'s `VERIFIED` check and the
off-budget grace review -- therefore ask one shared predicate,
`LoopSupervisor._terminal_success_blockers()`, which names three things: a
steering file nothing has settled yet (`Run.pending_steering()`), a
steering-origin task that is not terminal (`state.steering_origin_
outstanding`, task 013 of v0.14 -- `settled` is not `done`, a message can be
`consumed` with most of the work it asked for still undone), and a
non-terminal `review-cure`-origin task (`state.review_cure_origin_
outstanding`, v0.23 requirement D.1 -- engine-inserted cure/remedy work,
including the REFUTED petition remedy a run-ending review inserts under
requirement D.2, gets the same "settled is not done" protection one level
over). When it names
anything, the verdict is discarded and the run defers exactly once back to
`worker` through whichever of the two declared deferral edges names the
blocker being acted on -- `steering_pending_at_verified` while anything on
the steering side is outstanding, `review_cure_pending_at_verified` when a
non-terminal `review-cure`-origin task is the whole of what is left
(`LoopSupervisor._pending_at_verified_guard` is the single choice site, so
the event log never records one blocker under the other's name); the
ordinary dispatch loop then routes an unsettled message to the `steer` hat
at absolute top priority (which consumes it only on a clean exit) or leaves
the outstanding task to the worker's own scheduler, and the approach is
re-reviewed the ordinary way. Only a `VERIFIED` verdict observed with that
predicate empty is allowed to end the job successfully. (Before v0.14 this
was an in-place loop inside `_step_review` that dispatched a no-task
`worker` iteration itself and re-reviewed until the FILE was consumed --
same guarantee, but keyed on the message rather than on the work it asked
for.)

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

### The uid boundary: an iteration cannot signal its own supervisor (task 020, #48)

The engine is the container's PID 1 with the argv `ralphd-engine`, and until
v0.7 `container/Dockerfile` ended in `USER agent` — the same uid iterations run
as. `pkill -f ralphd-engine` typed inside a task iteration was therefore both a
match and *permitted*, and it ended a 9h36m run. This is the containment model's
floor, so it is enforced by the kernel rather than by a prompt rule:

- **The image no longer sets `USER`.** The entrypoint and the engine start as
  root; `engine/privsep.py:separate_engine_identity()` then runs
  `setresgid(0, agent_gid, 0)` and `setresuid(0, agent_uid, 0)` before anything
  else in the process — before the first file is written and before the API
  listens.
- **Signals are decided by the *real or saved* uid of the target.** Both stay
  `0`, so nothing running as `agent` can signal the engine: not by pid, not by
  process group, not by any `pkill`/`killall` pattern (`ptrace` follows the same
  rule). The attempt returns `EPERM` and the iteration goes on being an ordinary
  failed-or-successful iteration.
- **File ownership is decided by the *effective* uid**, which stays the agent's.
  Every file the engine writes into the bind-mounted run dir, the workspace and
  `$HOME` is owned by uid 1000 exactly as in v0.6 — which is what makes this
  route cheap: the `$HOME` config overlay, the `~/.creds` files pi must read,
  the git identity used for commits and the host user's ability to read and
  remove its own run dir are all untouched.
- **Each iteration is dropped all the way down.** `runner.py` passes
  `user=`/`group=` (from `privsep.agent_child_kwargs()`) to
  `create_subprocess_exec`, which `setreuid(uid, uid)`s the forked child, so the
  iteration's *saved* uid moves too and it cannot climb back to the engine's
  real uid. `on_complete_cmd` — operator-supplied shell — runs under the same
  drop. The engine can still signal the iteration (`interrupt()`'s `killpg`):
  permission flows downward, its effective uid being the child's real uid.
- **No boundary is claimed when there is none.** The engine is also run
  unprivileged (the whole test suite, `docker run --user 1000`, a derived image
  that ends in `USER 1000`); every function in `privsep.py` is then a no-op that
  logs one warning, and `separated()` reads the live process credentials rather
  than a startup flag, so no surface can report a boundary the kernel is not
  enforcing.

PID-namespace isolation per iteration (the other route, still deferred) remains
the general fix: it would additionally let the engine distinguish "stop this one
iteration" from "the container is being torn down", which the uid boundary does
not address. It is not needed to stop the signal.

### The task record, root-owned under the same boundary (task 105, #60 requirement D)

`RunDir.write_tasks()` (`engine/state.py`, the sole authorized writer, task
013) routes its atomic write through `privsep.write_root_owned_json()` rather
than calling `atomic_write_json` itself. Under the uid boundary above
(`privsep.separated()` true) the engine's real and saved uid are already `0`;
`privsep.root_for_write()` raises the *effective* uid back to `0` for exactly
the duration of that one write (`setresuid(0, 0, 0)` ... write ... `setresuid(0,
agent_uid, 0)`, the restore in a `finally` so a write that raises cannot leave
the engine's effective uid at root), then `chmod`s the file `0644`. The result:
`tasks.json` is owned by root, mode `0644` -- readable by the agent uid (so
`GET /tasks`/`ralphctl tasks` and every iteration's own read of the plan are
unaffected) but not writable by it. **Hand-editing a live run's task record
from inside a job container is no longer available**; `ralphctl steer` is the
channel an operator uses instead, and every other file the engine writes stays
owned by the agent uid exactly as the section above describes -- this is the
one deliberate exception, not a second ownership model.

Without the boundary (`separated()` false -- the whole test suite, `docker run
--user 1000`, an unprivileged engine) `root_for_write()` is a no-op, the write
lands owned by the agent uid exactly as every other file does, and the engine
logs exactly one warning per process the first time it happens -- the same
degrade-don't-fail discipline every other function in `privsep.py` already
follows, not a second code path. `tests/test_engine_uid_docker.py`'s
`test_a_worker_iteration_cannot_kill_the_engine` proves the root-owned shape in
a real container (ownership, mode, and a `docker exec --user 1000:1000` write
attempt refused with `Permission denied`); `tests/test_engine_uid_boundary.py`
covers the degrade and the elevate/restore bracketing at the fast-lane tier.

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

**Model-route validation.** Before iteration 1, every `MODEL_HATS` entry's
resolved ref is checked against the profile's own declared providers (task
009, PRD v0.11 requirement D): `engine/llm.py:check_hat_routes` reads
`~/.pi/agent/models.json` locally (never a live gateway call) and, on the
first hat whose ref names an undeclared provider, prints a diagnostic naming
the hat, the offending ref, and the provider keys that ARE available, then
exits **`5`** (`EXIT_UNROUTABLE_HAT_MODEL` in `engine/main.py`) before
`LoopSupervisor.run_job()` is ever scheduled — a typo in one hat's ref
surfaces at start, not three hours in on that hat's first dispatch. An
empty/absent `models.json` skips the check entirely ("nothing declared yet",
not "nothing is routable").
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
- **The crashed attempt survives the reuse (#44).** Reusing the *number* must
  not mean overwriting the *record*: `RunDir.begin_iteration_dir()` moves
  everything the dead attempt left in that slot into
  `iterations/<NNNN>/attempts/<NN>/` (oldest first, mirroring
  `approaches/<NN>/`) before the resumed attempt writes, emits one
  `iteration.attempt_archived` event, and surfaces the count as
  `archivedAttempts` on the iteration-detail views. The archive is never read
  as an iteration of its own -- not by the numbering seed, not by the budget,
  not by the transcript merge -- so a resume that preserves evidence cannot
  inflate what the run is charged for.
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
- **Resuming a run paused by `rejected_incurable` (task 007, requirement B,
  #111).** The `review -> review` `rejected_incurable` edge (task 006) parks
  the run on `self._pause` awaiting an operator ruling, and that pause is an
  in-memory `asyncio.Event`: a container restart while it is held loses it
  entirely. `_resume_point()` therefore has a fifth case,
  `LoopSupervisor._paused_awaiting_incurable_ruling(approach)` -- the most
  recent `operator_escalation` event with `reason == "rejected_incurable"`
  for this approach, with no later `iteration.start` on record -- and routes
  straight back to **`review`**, never to `replan` (a fresh approach over
  findings a fresh approach was already shown incapable of curing) and never
  through a throwaway `worker` pass first.
  The rulings the operator recorded while it sat paused reach that review
  iteration through both ruling channels, so nothing is left silently unruled:
  `mode: status` rulings are applied by `_apply_operator_status_rulings()` (->
  `state.apply_amendment_status_rulings`), which runs unconditionally **once
  per engine episode** at the top of `_run_job_core()`, ahead of the resume
  decision, **and at every worker and review step boundary** (`_step_worker`'s
  `while` head, `_step_review` just after its `phase` emit -- GH #209 defect
  6). The episode call is what covers a container restart; the boundary calls
  are what cover the in-process `POST /resume` path, which before #209 defect
  6 re-applied nothing at all. The criteria-ruling channel,
  `_operator_amendment_review_context()`, does re-read `CONFIG_DIR` fresh on
  every `_step_review()` call, restart or in-process resume alike -- but it
  was never the fallback for a status ruling that this paragraph once claimed
  it was: it filters `mode: status` entries out by design (requirement E -- a
  ruling must not be able to look like a claim the criteria were met), so it
  only ever carried the `waive`/`replace` kinds.
  Tests: `tests/test_resume_after_incurable_rejection.py` --
  `test_resume_point_routes_straight_to_review_not_worker_not_replan` pins
  the routing, `test_resume_lands_on_review_and_sees_the_ruling_recorded_while_paused`
  pins the visibility (the amendment text written host-side while paused
  appears in the resumed review iteration's own prompt), and
  `test_resume_without_any_ruling_still_lands_on_review_and_rejects_again`
  is its no-ruling control. All of them reach the paused state through an
  already-paused fixture and never await an unscheduled operator.
- **The cure pass: one chance to fix curable findings before spending a
  fresh approach (v0.18 requirement C.1/C.2, #141, tasks 009/010).** A
  `rejected` review whose blocking findings are ALL `curable: true`
  (`findings.all_blocking_findings_curable`) does not automatically take the
  ordinary `rejected_approach_left` edge into a brand-new `planning` pass --
  it takes `rejected_curable_pass_available` back into `worker` instead,
  **exactly once per approach**: `LoopSupervisor._step_review` checks
  `status.json`'s `curePasses["<approach>"]` (absent reads as `0`) AFTER the
  `rejected_incurable` check above, so an incurable finding keeps its
  precedence even when curable findings are also present. Taking the edge
  appends one cure task per blocking finding to the SAME approach's
  `tasks.json` (`state.insert_cure_tasks`, `origin: "review-cure"`, so a
  cure task is visibly distinct from the planner's own), increments
  `curePasses[approach]` to `1`, and emits `approach.cure_pass` -- a second
  `rejected` verdict in that same approach, curable or not, falls through to
  the ordinary `rejected_approach_left`/`rejected_incurable`/
  `rejected_last_approach` edges regardless, because the counter is already
  spent. `curePasses` is disk-backed (survives a resume) and is never reset
  by anything short of a fresh approach (it is keyed by approach number, and
  archiving one leaves the next approach's own entry, if any, absent). This
  is what makes `rejected_last_approach` below reachable on the LAST
  approach specifically: a final-approach rejection that is all-curable
  takes the cure pass FIRST (same as any other approach) and only pauses on
  a SECOND all-curable rejection, once that approach's one cure pass is
  already spent. Tests: `tests/test_cure_pass.py` (task 009's own section:
  the edge and the appended tasks; task 010's own section: the
  one-per-approach bound, incurable precedence, and that the counter
  survives a resume). A criteria-tamper rejection is the ONE exception to
  "all-curable takes the cure pass": the engine's own tamper finding
  (`findings.flagged_criteria_findings`, #116 section 2) is flagged
  `curable: true` because a *fresh* approach that never rewrites its
  criteria again does not repeat the violation -- but curing is not a fresh
  approach, it hands the approach that already rewrote its own
  `successCriteria` another worker pass over exactly those tasks. So
  `_review_cure_pass_available` refuses the edge whenever
  `findings.any_criteria_tamper_finding` recognises such a finding in the
  channel (severity-blind on purpose, so a downgrade cannot buy the cure
  pass back), and a tampered approach is archived and replanned exactly as
  it was before this edge existed (v0.18 task 039, operator steer 002).
  Tests: `tests/test_cure_pass.py` (task 039's own section) in process and
  `tests/test_e2e.py::test_criteria_edited_after_validation_failure_is_refused`
  end to end.
- **A cure is not a replan: a progress-based cure budget past the original
  one-per-approach cap (v0.20 requirement E.1, #167, task 030).** The
  one-per-approach bound task 010 pinned above is no longer absolute:
  `_review_cure_pass_available` grants a SECOND (or third) cure pass in the
  same approach when the run's own cure-pass budget is not yet spent (v0.21
  requirement A.2, #192, task 002: `_cure_passes_for_run()`, the total over
  `curePasses` and `gateCurePasses` for every approach, below the configured
  `JobConfig.cure_pass_ceiling`, job.yaml key `cure_pass_ceiling`, default
  3 — a per-approach comparison against the module-level
  `CURE_PASS_CEILING` literal before that task) AND this rejection's own
  blocking clause set
  (`findings.blocking_finding_clauses` -- the `clause` values of its
  blocking findings, residuals excluded) is NOT a superset of the clause
  set the immediately preceding GRANTED pass recorded. Every granted pass
  persists its own blocking clause set to `status.json`'s `cureBlocking
  [approach]` (`_take_cure_pass`, alongside the existing `curePasses`
  increment) so the comparison survives a resume with no shared Python
  state, the same disk-not-memory discipline `curePasses` itself already
  uses; `_cure_pass_blocking_clauses` reads it back. An unchanged or GROWN
  blocking set -- review pointing at the identical wall, or a bigger one --
  is a superset of itself and is refused, falling through to the ordinary
  replan/pause edges exactly as task 010's original one-pass bound did; a
  genuinely narrower or different set (real progress, even if incomplete)
  earns another pass. `approach.cure_pass` gains `blockingBefore`
  (the previous granted pass's set, `[]` on the first pass), `blockingAfter`
  (this pass's own set) and `passNumber` (the new count) alongside its
  existing fields, so the whole progression is legible off `events.jsonl`
  alone. This closes the v0.19.1 approach-1 incident this task's PRD
  section names directly: a second rejection that repeated the SAME three
  clauses a first cure pass had already targeted (a stale sweep, never
  re-taken) spent that approach's entire one-cure-pass budget with no
  ordering rule in the plan able to reach it -- under this rule the repeat
  is still refused (same clauses, a superset), but a rejection that instead
  narrows or shifts its own blocking set is no longer treated identically
  to one that doesn't. Tests: `tests/test_cure_pass.py` (task 030's own
  section: the grant on a non-superset second rejection, the refusal on a
  superset one, and the ceiling at a third).
- **A cure is a specification, not a free-text repeat of the finding
  (v0.20 requirement E.2, #171, task 031).** Structured findings gain two
  fields (`engine/findings.py`'s `FINDING_SCHEMA`): `cured_when` (a
  string; required, as a business rule rather than a schema constraint,
  whenever `curable` is `true`) and `invalidates` (an optional list of
  in-checkout paths, wired by a later task). A curable blocking finding
  missing `cured_when` -- or carrying one whose own text overruns the
  plan's bounded-checkable-clause lint, `plan_ingestion.clause_count_
  overrun` (the same public function `lint_proposal` itself now calls,
  exposed for exactly this cross-module reuse) **and whose overrun has no
  lossless, lint-clean fold** (#209 defect 1) -- is a review contract
  violation on the SAME ladder a `verified` claim contradicting its own
  findings already rides (`transitions.review_cured_when_errors`, wired
  into `_review_verdict` and `_review_contract_ladder` beside `review_
  verdict_findings_errors`); this reaches every engine-authored curable
  finding too (`findings.flagged_criteria_findings`'s criteria-tamper
  finding, `petitions.duplicate_petition_findings`'s duplicate-filing
  finding), each of which now carries its own `cured_when`. The bound
  itself is unchanged: an overrun `cured_when` whose clauses fold
  losslessly and lint-clean is REPAIRED at `state.insert_cure_tasks`
  (`plan_ingestion.repair_criteria`, event `task.criteria_repaired` with
  `source: "cure-insertion"`) rather than refused, because the refusal
  spent a review contract attempt re-asking a hat for advice the harness
  had already computed and then used the refused text verbatim anyway
  (`_step_review` discards those contract errors before granting the cure
  pass). `state.cure_task_from_finding`/`insert_cure_tasks` use
  `cured_when` as the cure task's `successCriteria` whenever a finding
  carries one -- VERBATIM unless that lexical repair applies -- and always
  set a new `description` field from `clause` + `evidence`
  (the specification and its human-readable context, kept separate); a
  finding with no `cured_when` (a fixture predating this task, or a direct
  `insert_cure_tasks` caller that supplies none) falls back to the
  pre-existing clause+evidence derivation unchanged. Tests:
  `tests/test_cured_when.py` (task 031's own module: the schema/business-
  rule layer, the ladder re-ask and its recovery, and the criteria
  substitution itself).
- **A cure invalidates evidence: re-take the paths it stales, re-open the
  record it stales (v0.20 requirement E.3, #167, task 032; steer 001
  retired the local-sweep "launch"/"record" shape this requirement's PRD
  text originally named, in favour of the check-run-record regime tasks
  057/058 build).** A finding's own optional `invalidates` (`engine/
  findings.py`'s `FINDING_SCHEMA`, task 031) is read by `insert_cure_
  tasks`, one path at a time, in `invalidates` order, right after that
  finding's own cure task is appended: for an ordinary path -- one no
  existing task's `successCriteria` already names -- a brand-new re-take
  task is appended (`_retake_task_from_path`, `origin: "review-cure"`,
  `dependsOn` the cure task, `id` deterministic from `(approach, index,
  path_index)`), whose `successCriteria` states the path is re-taken at
  the tree the cure leaves. But a path that IS the plan's own final
  lane-evidence record -- engine-generically recognised
  (`_lane_evidence_record_chain`) as a task whose `successCriteria` names
  the path AND that itself `dependsOn`s the check-run verification
  step(s) it records, never a hardcoded task id -- instead RE-OPENS that
  record task and every one of its verification tasks (`status` back to
  `"pending"`) and adds the cure task to each verification task's own
  `dependsOn`, completing the chain cure -> verification -> record (the
  record task already depends on its verification task). No new task is
  appended for that path; `insert_cure_tasks`'s return value -- which
  `_take_cure_pass` still emits verbatim as `approach.cure_pass`'s
  `taskIds` -- names every id this call appended OR reopened, so the
  whole move is legible off `events.jsonl` alone. A finding with no
  `invalidates` (every fixture predating this task) leaves this new step
  a no-op, unchanged from before. #209 defect 3 narrows both branches
  without touching the invariant: a task the ENGINE itself authored
  (`origin: "review-cure"`) is invisible to that recognition, BOTH as a
  candidate record and as a verification step, because pass N's own
  `cure -> verification` edge otherwise made pass N+1 re-open pass N's
  LANDED cure task (measured: `deck-phase4` reset `cure-03-01`/
  `cure-03-02` from `validated` to `pending` and re-worked them;
  `selfdev-v022` left `cure-01-01` `pending` behind three later cure
  tasks); and a path that already has this approach's re-take task
  re-uses that ONE row -- `status` back to `"pending"`, the new cure id
  added to its `dependsOn` -- instead of appending a second one with the
  same title. The re-open of a GENUINE planner record is unchanged:
  still every pass, still from any terminal status, because "re-record
  only after the LAST cure" is unknowable at insertion time and
  deferring it would mean reviewing on stale evidence. What #209 defect
  3's residual narrows is which of that record's DEPENDENCIES come back
  with it: a dependency the plan declares to be a pure ordering edge
  (`tasks[].orderingOnly`, read through `plan_ingestion.ordering_only_
  dependencies`) is not an evidence step, keeps its terminal status and is
  absent from `taskIds` -- measured in `selfdev-v022`, where the already
  landed, nightly-green task `012` was re-worked purely for sitting in the
  record `013`'s `dependsOn`. An absent declaration means "every edge
  produces evidence", the conservative reading every plan written without
  the field already gets, and the distinction is PLANNER-DECLARED because
  no reading of criteria text can tell an evidence edge from an ordering
  one. When that leaves a record with no re-opened verification step at
  all, the cure task joins the RECORD's own `dependsOn` instead, so the
  re-record still cannot be picked ahead of the cure and requirement E.3
  holds either way. Tests:
  `tests/test_cure_invalidates.py` (task 032's own module: the per-path
  insertion, the lane re-open chain, the cure-origin exclusion and
  per-path re-take reuse, the planner-declared ordering edge, and the
  emitted event's own `taskIds`).
- **The `invalidates` re-open is a prediction, and it is now reconciled
  against the cure's real diff afterwards (v0.23 requirement B, #221,
  tasks 005-008).** The bullet above still runs first and unconditionally,
  from any prior terminal status, on the finding's own `invalidates`
  prediction alone -- that part is unchanged. What is new is a walk-back
  in three pieces, extracted and landed as four separate tasks because the
  whole requirement is the wave's largest and touches a transition table:
  (1) **B.1 (task 005)** pulled the per-finding walk itself out of
  `insert_cure_tasks` as a pure predicate, `state.invalidates_reopen_walk`
  -- one `(record_task, verification_tasks)` pair per `invalidates` entry,
  `None` where nothing would be re-opened -- with no behaviour change; the
  write side (status flips, dependency edges) stays inlined in
  `insert_cure_tasks`, which now just calls the predicate once per finding.
  (2) **B.2 (task 006)** persists the workspace HEAD sha the cure pass
  started at -- `insert_cure_tasks`'s own new optional `pre_cure_head`
  parameter, read once by the caller via `_workspace_head_sha()` -- into
  `status.json`'s `preCureHeads` map, keyed by the RECORD task's own id
  (never a verification/retake task's), in the same shape
  `approachStartHeads` already uses, and emits it on `task.pre_cure_head_
  recorded`; a later re-open of the same record id overwrites the entry
  rather than accumulating it. (3) **B.3 (task 007)** declares the move
  back OUT of `pending` as a real edge in `transitions.TASK_STATUS_TABLE`
  (`pending -> completed`/`pending -> validated` under the vigilant
  delta -- both replaced by cure-01-05, PRD requirement B.3/B.4, F05,
  #221: all five restore targets (`completed`, `validated`, `failed`,
  `skipped`, `superseded`) are declared once, in the BASE table, and the
  vigilant delta carries no `pending` entry at all, because the restore
  target is the status the engine persisted at reopen time and not a
  function of the verification mode in force when it is read back --
  F05 measured the split leaving a genuinely `validated` record `pending`
  in a non-vigilant run, with no event at all, while a hat's own
  vocabulary is held where it was by `task_status_report_errors`), named
  variant-agnostically by
  `transitions.is_un_reopen_
  transition`, and routes it through `state.apply_task_status_change`,
  which emits `task.un_reopened` instead of the generic amendment event
  for exactly this transition. This does not relax the standing rule that
  there is no edge INTO `pending` -- it is the one declared way OUT of a
  `pending` the walk above put a record into on its own prediction. No
  caller invokes the edge yet at this point. (4) **B.4 (task 008)** is the
  completion-path hook that actually calls it: `state.records_pending_
  reconciliation` reads back every `task.pre_cure_head_recorded` entry and
  returns a record only when its live `preCureHeads` baseline still matches
  the sha a cure re-opened it at, the record is still `pending`, AND every
  cure task that named that same live baseline has itself reached
  `completed`/`validated` -- the LAST such owner, not the first, is the one
  whose completion triggers the check, because one cure pass can insert
  several cure tasks sharing one baseline and the first owner to land used
  to restore the record out from under the second owner's still-pending
  work. `LoopSupervisor._reconcile_pre_cure_heads`, called from both
  completion-path writers, then diffs that sha against HEAD over every
  owner's commits: an empty-of-cited-paths diff takes the declared edge
  (`source: "cure-reconciliation"`), to the record's own ACTUAL pre-reopen
  status (`records_pending_reconciliation`'s own `priorStatus` field,
  fed from `status.json`'s `preCureStatuses` map -- cure-01-05, PRD
  requirement B.3/B.4, F05, #221 -- never a value inferred from `cfg.
  vigilant`, not even as a fallback: a candidate carrying no persisted
  prior status at all is left `pending` like any other doubt. The
  production hook inferred it from the variant until this task, which
  silently PROMOTED a `failed`/`skipped`/`superseded` record to
  `completed`/`validated` and left a `validated` record in a non-vigilant
  run unrestored), and fires a follow-up
  notification
  naming the owning cure tasks, the status each record was restored TO,
  the diffed range and the paths checked; a
  diff that touches a cited path, an owner that is not yet terminal, an
  owner that ended `failed`/`skipped`, or a diff that cannot be computed at
  all (non-git workspace, missing sha) all leave the record exactly where
  it is -- when in doubt the record stays open, since a false un-reopen
  ships unverified work and a missed one only costs one extra re-record.
  This reconciliation does not make A.3's pre-spend trigger any quieter --
  that gate fires before a cure commit exists at all, so it has only the
  same prediction to act on, and on the measured reference incidents this
  walk-back would have suppressed none of them, because every one of those
  predictions turned out correct. One consequence worth naming explicitly:
  `approach.cure_pass`'s own `taskIds` (above) is a snapshot taken at
  insertion time, and is therefore no longer a permanent record of
  "everything this pass put into motion" -- a record/verification pair it
  names can be un-reopened later by this same mechanism, visible only as a
  separate, later `task.un_reopened` event. Tests: `tests/test_cure_
  invalidates.py` (the same module as above -- the pure predicate's own
  pins, the persisted/emitted pre-cure head across a fresh OS process, and
  `records_pending_reconciliation`/`_reconcile_pre_cure_heads`'s full
  matrix: single-owner, multi-owner, a dead sibling, a stale or already-
  restored baseline, a diff that does and does not touch the cited path,
  and the vigilant variant's `validated` target).
- **Cure tasks land before the frozen tail, re-take tasks after it (v0.20
  requirement E.4, #167, task 033).** `insert_cure_tasks` no longer only
  ever `tasks.append`s: `state.last_code_touching_task_id(run, tasks)`
  finds the plan's own named last-code-touching task -- an explicit
  override in the harness's pinned standing rules (`RunDir.read_standing_
  rules()`, searched for the phrase "last code-touching task" with a task
  id nearby), checked first, then a task's own text self-declaring the
  role (the same phrase, in its `title`/`successCriteria`/`description`)
  -- and `None` when neither source names one. Every freshly-appended cure
  task is spliced immediately BEFORE that task (`_insert_before_id`);
  every freshly-appended re-take task immediately AFTER it
  (`_insert_after_id`); several of either kind from one cure pass land in
  finding order, never reversed, because each call re-finds its anchor's
  CURRENT position rather than a cached one. A REOPENED record/
  verification task's own list position is untouched -- only `status`/
  `dependsOn` change for those, since they already sit at the plan's own
  final-lane-evidence tail. When the plan names no such task at all, both
  kinds fall back to `tasks.append`, exactly as every approach before this
  task. Tests: `tests/test_cure_freeze_ordering.py` (task 033's own
  module: the standing-rules override, the task-self-declaring fallback,
  several cure/re-take tasks staying in finding order, and the
  append-at-the-end ordering used when the plan names no such task).
- **Resuming a run paused by `rejected_last_approach`, and seeing that pause
  from the host (v0.18 requirement D.1/D.2, #142, tasks 011 and 036).** The
  final approach's all-curable rejection takes `rejected_last_approach` into
  the dedicated `paused` step state (v0.18 task 004, finding 4 --
  previously a `review -> review` self-loop) and parks the run on the same
  `self._pause` event via `LoopSupervisor._step_paused`; `_resume_point()`'s
  `_paused_awaiting_last_approach_ruling(approach)` case routes a restarted
  episode straight back to **`review`** over the SAME (never archived)
  approach, for the same reasons as the `rejected_incurable` case above plus
  one more: there is no fresh approach left to route to at all.
  Task 036 adds the half that makes the hold *legible off the run dir*: an
  engine-initiated pause passes its edge name to `pause(reason=...)`, which
  persists `state: "paused"` plus `pauseReason: "rejected_last_approach"` to
  `status.json` at the escalation site itself, and `resume()` clears exactly
  that pair (back to `running`/`null`) while leaving `approach` and `phase`
  untouched. Nobody typed that pause, so unlike an operator `POST /pause` --
  which still records no status field at all -- there would otherwise be
  nothing on disk telling a host the run is waiting for a ruling rather than
  working. `paused` is therefore part of `state.NONTERMINAL_STATES`: a live
  engine still owns the run, no destructive action may open on it, and an
  engine killed while parked leaves it dangling like any other non-terminal
  run. Tests: `tests/test_last_approach.py` (the edge, the persisted pause,
  the resume that clears it over the same approach, and the operator pause
  that still records nothing -- absorbed by task 022, requirement D.1, from
  the former `test_last_approach_pause_task011.py` and
  `test_last_approach_pause_persisted_task036.py`).
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
  `tests/flows/test_flow_crash_resume_refunds.py::test_sigkill_after_task_completed_before_verify_finishes_then_resume_verifies`
  (SIGKILL a specific PID after a task is durably `completed` but its
  verify iteration's `meta.json` has `startedAt` and no `endedAt`; resume
  proves a `taskVerified` signal and a passing verify iteration for that
  task eventually appear; formerly `test_vigilant_crash_resume.py`,
  folded by task 021, requirement C.10).

## 5. Configuration & injection

Everything the job needs is mapped in by the CLI. Three mount points:

| Mount | Contents | Writable at runtime |
|-------|----------|--------------------|
| `/run/ralphd` | run state (always host-mounted) | engine-owned |
| `/workspace` | code | agent-owned |
| `/config` | prompts overrides, skills, creds, pi settings | via API (`/config/*`) |

`/config` layout:

```
/config/
├── job.yaml            # job config (PRD ref, budgets, mode flags, model strategy)
├── llm-wiring.json     # `--llm`-derived env/mounts, so `resume` reproduces them (§9)
├── env-wiring.json     # resolved --forward-env/--llm-env/--env pairs (§9)
├── prompts/            # optional phase-prompt overrides
├── skills/             # agent skills, exposed to pi as workspace skills
├── creds/              # operator-prepared credential env files + extras
│   ├── github.env      # KEY=value lines → placed at ~/.creds/github.env (0600)
│   ├── jenkins.env     # …one file per credential set, any names
│   └── setup.sh        # optional: run once before first iteration
└── pi/                 # pi settings fragments (models.json, provider config)
```

Both `*-wiring.json` files are mode `0600` and never served by any HTTP route;
the rest of `/config` is the operator's own input.

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
  values never reach `/run/ralphd`, `events.jsonl`, stdout, or the job config
  (only file
  *names* are ever logged).
  **The agent knows where to look**: every phase prompt lists the available cred
  file names and the usage rule — *source the file you need, in the shell where
  you need it* (`set -a; . ~/.creds/github.env; set +a`). Values are **not**
  auto-exported into the engine or agent process environment; a credential is
  visible only to commands that explicitly source its file. Recognized
  non-env extras keep their conventional placement (`gitconfig`,
  `git-credentials`, `netrc`, `ssh/`), and an executable `setup.sh` still runs
  once at container start as the escape hatch. Nothing credential-shaped is
  ever written to `/run/ralphd` (which is host-visible history) or logged.
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
(`/run/ralphd`) must never carry credential-shaped content. But the runtime
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
[roadmap.md](roadmap.md)): PID-namespace isolation of agent iterations (v0.7
closed the in-container *signal* hazard with the uid boundary above instead —
see §4's "The uid boundary"; what a namespace would still add is telling "stop
this iteration" from "the container is being torn down"), and a `ralphctl
repair` command for hand-fixing corrupted run state.

## 6. API security

- Default: container port published to `127.0.0.1` only, **no auth**.
- Optional: `--api-token <t>` (or auto-generate with `--api-token auto`) makes the
  engine require `Authorization: Bearer <t>` on every route; combine with
  `--api-bind 0.0.0.0` for LAN/remote access. The CLI stores the token in the
  registry's host-only control-creds directory (`~/.ralphd/control/<id>/.api-token`,
  mode 0600, never bind-mounted into any container -- task 003, requirement A,
  #201) and sends it automatically.
- The job config itself (`<config-dir>/job.yaml`) is written **verbatim**, not
  redacted at rest: it is a private host-side file (like `llm-wiring.json` /
  `env-wiring.json` next to it) that only ever reaches the container as a
  read-only mount, and no HTTP route serves it. Redaction happens where it is
  *read out* — `ralphctl docs <run> job` and the hub's document dialog mask by
  key name and scrub by value through `engine/redact.py: redact_job_yaml()`, so
  a secret an operator put in e.g. `on_complete_cmd` is not printed or
  screenshotted. The creds API, however, is
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
├── config.yaml          # registry-wide defaults (`ralphctl config`): image,
│                        #   on_complete, default_llm_profile, network, …
├── llm-profiles/        # named LLM profiles (see llm-profiles.md)
├── templates/<name>/    # optional job templates (`start --template`)
├── configs/<run-id>/    # per-job config dir, bind-mounted read-only as /config:
│                        #   job.yaml, prompts/, skills/, creds/, pi/,
│                        #   llm-wiring.json, env-wiring.json (§5, §9)
└── runs/<run-id>/       # one dir per job, bind-mounted as /run/ralphd (above)
```

Run IDs are generated (`adjective-animal-HHMM` style) or supplied with `--run-id`.
`ralphctl runs` lists history by scanning `runs/*/status.json`; live status merges
in the container API when the container is up. `ralphctl watch` streams that
run's events as they arrive (one line per event, NDJSON with `--json`) — not a
TUI: the host side ships no curses/TUI framework at all.
`ralphctl ui [--port N]` (PRD reqs 21-22) serves the same registry over a
stdlib-only HTTP server (`http.server`; no `fastapi`/`uvicorn` on this path).
Its JSON surface is a family of `GET /api/runs[/<id>[/…]]` reads — run list,
run detail, log tail, PRD, steering history, state documents, artifacts, one
iteration, the fault explanation, the cost breakdown — plus two proxies
(`POST .../steer`, `POST .../retry`) and one mutation (`DELETE /api/runs/<id>`,
terminal runs only). Every endpoint is enumerated with its exact payload in
[cli.md](cli.md#ralphctl-ui---port-n---bind-addr); the rule they share is that
the four live-answer reads (run detail, log tail, PRD, steering history) proxy
each run's container API when reachable and fall back to the on-disk
`status.json`/`tasks.json`/transcript snapshot otherwise, so a dead run degrades
gracefully instead of erroring (each of those payloads carries a `live` flag
saying which side answered), while the rest — the run list, one iteration, the
state documents, the artifacts, the fault explanation and the cost breakdown —
are on-disk by design (the files they read are the engine's own atomic writes,
so there is nothing better a live container could say; the run list only ever
*probes* liveness, with a loopback TCP connect, never an HTTP round trip).
The static bundle served at non-`/api` paths
(`src/ralphd/cli/web/`: `index.html`, `app.js`, `style.css`, shipped in the
wheel, no build step) landed in v0.3 task 034.

**Run-detail `ETag`, its two derivation caches, and the client's back-off
(v0.19.1 tasks 003-005, requirement A).** `GET /api/runs/<id>` answers with
an `ETag` derived from the run's own on-disk timestamps -- the max `mtime`
across `status.json`, `tasks.json`, `events.jsonl` and the newest iteration's
`meta.json` (`run_detail_etag` in `ui_server.py`) -- so the tag changes
exactly when the payload it would compute would change. A request that
sends that tag back as `If-None-Match` gets a `304` with the same `ETag` and
no body, rather than the server recomputing and resending a payload the
client already has. Two per-run, in-memory caches sit behind that route so a
miss is cheap too: an iteration-row cache keyed on `(meta.json mtime,
transcript size)` per iteration number, and a `tasks.json`-shaping cache and
a `status.json`-shaping cache each keyed on their own file's `mtime`
(`_ITERATION_ROW_CACHE`, `_TASKS_BLOCK_CACHE`, `_STATUS_BLOCK_CACHE`); all
three are dropped for a run the moment it is deleted, and a cache entry
never answers for any run but the one it was keyed on. `app.js`'s run-detail
poll sends the last `ETag` back as `If-None-Match` on every tick; two
consecutive `304`s, or the run leaving `state: "running"`, widen its polling
interval from 4 s (`REFRESH_MS`) to a fixed 20 s (`BACKOFF_POLL_MS`, inside
the PRD's 15-30s band), snapping back to 4 s the moment a poll's payload
actually differs again. Every view arms its refresh timer through one
shared `scheduleRefresh()`, which will not arm one at all while
`document.hidden`; a single page-level `visibilitychange` listener stops
whichever timer is running the moment the tab is hidden and restarts it, at
whatever interval was already in force, the moment the tab is shown again --
so a backgrounded hub tab neither polls nor advances its own back-off state.
See [SPEC.md §11.1](../SPEC.md#111-serving-it) for the full walkthrough.

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

**The log tail's work is bounded by the tail, not by the run's history
(tasks 043-048, PRD H).** The paragraph above describes this route's
ORIGINAL shape (v0.3-era, task 014): fetch everything, render everything,
trim afterwards. PRD H's own measurement (2026-09-12, a 176 MB run) found
that a repeated identical request to this route cost MORE the second time
(2.259 s then 4.506 s, 1.72 s and 2.49 s of CPU, RSS ranging 352 868 kB to
1 153 316 kB over one minute) -- proportional to the run's whole history,
not to the 200-line tail an operator actually sees. Three layers were each
bounded in turn: `log_render.render_tail_lines` (task 043/H.1) renders
backwards from the newest `ralphd.iteration` boundary in growing chunks
(1, 2, 4, 8... boundaries back) instead of rendering the full blob and
slicing, byte-identical to the old output across a many-delta thinking
flood (task 044/H.2, `tests/test_log_render_bounded_tail.py`); the route
itself is `ETag`-conditional and cache-keyed on the newest iteration's
number and transcript size (task 045/H.3); a LIVE run's fetch from the
container asks `GET /logs?tail=N` for a bound derived from the requested
rendered-line count and re-asks with a doubled bound when the render comes
up short (task 046/H.4, `ui_server.rendered_log_lines`'s nested
`_fetch_live_tail`); and `_ITERATION_ROW_CACHE`/`_TASKS_BLOCK_CACHE`/
`_STATUS_BLOCK_CACHE` are capped at 200 distinct run ids with LRU eviction
(task 047/H.5). The one layer those four left unbounded was the DISK READ
itself: `log_merge.merge_entries` is a generator over every iteration dir
the run has ever recorded, oldest first, and every caller wanting a tail
had to force it to a full list (`list(merge_entries(...))`) before
`apply_tail` could trim from the end -- so a 200-line tail on a run with
thousands of iterations still opened and read every `output.jsonl` on
disk, live or dead. Task 048 (H.6) closes that: `log_merge.merge_entries_
tail` walks the SAME iteration dirs in REVERSE (newest first) and stops
the moment it has collected enough content lines, so `merged_lines(...,
tail=N)` -- and, through it, `engine.api`'s `GET /logs?tail=N` and the
hub's dead-run fallback, `ui_server`'s new nested `_fetch_dead_tail` -- read
only the iterations a tail actually needs.
`tests/test_log_merge_bounded_reads.py::
test_merged_lines_reads_a_bounded_number_of_iteration_transcripts_regardless_of_run_length`
pins this as work, not just output: a run of 300 iterations and a run of
3000, one content line each, both cost exactly 200 `output.jsonl` reads to
serve a 200-line tail -- the SAME number either way, never 300 or 3000 of
them.

**Measured budget (task 011 of the 0.21.1 wave, PRD J.3, correcting task
048's own PRD H.6 figure), a urllib poller (`ralphctl ui`'s own hub
process, stdlib-only) simulating one hub tab against a TERMINAL cloned-
engine job's reachable API, holding 102 recorded iterations, polling
`GET /api/runs/<id>` and `GET /api/runs/<id>/logs?tail=200` every 4 s
(`REFRESH_MS`) for 60 s (15 polls of each): the hub process used 1.53 s of
CPU across that 60 s sample and its peak resident set size (`/proc/<pid>/
status`'s `VmHWM`) was 35 364 kB.** Measured against a real `ralphctl ui`
subprocess pointed at a real directly-spawned `ralphd-engine` from this
checkout (`tests.conftest.LiveRun`, `registry=True`, `on_complete: idle`
so the process -- and its API -- stays up once the job itself reaches a
terminal state, rather than exiting; `job.iterations: 120` and the stub
`pi`'s `STUB_TASKS=100`, giving 1 planning + 100 worker + 1 review = 102
iterations, `state: succeeded` throughout the sample). `GET /api/runs/
<id>` reported `live: true` at both the start and the end of the 60 s
sample -- that field names container/API reachability, not an in-progress
job, and the job HAD already reached its terminal state before the sample
started -- so every poll in it took the live-container path (`_fetch_live_
tail`, task 046/H.4), never the dead-run on-disk fallback this task's own
predecessor (048/H.6) bounded and which used to stand in as this budget's
sample. Read from `/proc/<pid>/stat` and `/proc/<pid>/status` rather than
against a wall clock, per this PRD's own ban on wall-clock assertions as a
test's *pass condition* -- the numbers themselves are a disclosed
measurement, not a test, and this paragraph adds no pass/fail threshold on
either one.

**The hub's render rule (task 018, #27).** The hub renders decisions Python
made and never applies a response it did not ask for. `cli/web/app.js` is a
client in the strict sense: every screen it shows is data `ui_server.py`
already decided (the JSON payload), and `app.js` never lets a stale answer
land on a screen the user has since navigated away from. The mechanism is
`renderGeneration` (task 016, #27): `router()` mints a new generation on
every navigation, each render closure (`renderRunList`/`renderRunDetail`)
captures the generation it was born with as `myGen`, and both the DOM write
and the refresh-timer install are gated on `myGen === renderGeneration`
still holding once the awaited `fetch` resolves — so a second, faster
navigation that supersedes a slow first one lets the first one's response
arrive, decode, and then be silently discarded rather than painted over the
newer screen. The same guard, checked a second time against a per-render
`loadSeq` counter, catches a slow *periodic refresh* tick response landing
after a newer tick of the *same* render superseded it. A test in
`tests/test_docs_semantics.py` derives this paragraph's claim from the code
(the `myGen !== renderGeneration` guard is present in both renderers) and
fails, by mutation, if the guard is removed.

## 8. Docker image

The default job image is **built locally from this checkout** and tagged
`ralphd:<hash>` (`cli/image.py: IMAGE_REPO`, below) — v0.6 publishes no image to
any registry, so there is nothing to pull; see [roadmap.md](roadmap.md) for the
deferred publishing work. `container/Dockerfile` builds it, containing: Python
3.12 (engine), Node 22 +
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
of the other two namespaces, and an install with nothing to hash. A
reference that cannot be compared to a source hash is never reported as up to
date (see [cli.md](cli.md#job-image-staleness-20-h4)). The same verdict is
applied per live run against the image its own `host.json` records, which is
the case the whole mechanism exists for: a running job executing an engine that
predates the fix it is watching for.

### Where the image inputs come from in a `pipx` install (v0.6, #20 H4)

Content-hashing the image assumes the inputs are on disk, and a
`pipx install ralphd` has no checkout next to it. Requirement H4 asked for one
of two answers, chosen and written down. **Decision: the wheel ships the image
inputs as package data**, and a build from such an install stages them into a
context:

| | |
|---|---|
| shipped | `ralphd/_image/` = `container/`, `pyproject.toml`, `README.md`, `LICENSE` (`cli/image.py: PACKAGED_FILES`, mapped into the wheel by `pyproject.toml`'s `force-include` -- no second copy in the repo) |
| staged at build time | that package data, plus the **installed** `ralphd` package copied to `src/ralphd`, into a temporary context laid out exactly like a checkout |
| result | the same `ralphd:<hash>` a checkout of that version builds |

Why not the other option, a pinned *published* tag? v0.6 publishes no image (an
explicit non-goal), so the fallback would name a reference that cannot be
pulled -- trading "cannot tell which engine you are running" for "cannot start
at all", and re-introducing a hand-maintained tag as the thing that decides
which engine runs. Shipping the inputs keeps the property the whole requirement
exists for: whatever way ralphd was installed, the tag is a function of the
engine that is actually installed.

Why the inputs and not just the Dockerfile: the default image's build context
*is* the install recipe (`COPY . /opt/ralphd` then `pip install /opt/ralphd`), so
a wheel carrying only `container/Dockerfile` would build an image with no engine
in it -- and `pip install` also reads the `readme`/license `pyproject.toml`
declares, hence those two files. The engine source is not shipped twice: the
installed package is the source, and staging it means the image contains exactly
the engine the host CLI is running.

The layout is a copy, not a symlink farm, and the copy walks by the same rules
the hash uses (`_collect`: caches pruned, symlinks recreated, the executable bit
preserved), which is what makes the tag come out identical rather than merely
similar; `_image/` itself is left out of the staged `src/ralphd`, since a
checkout has no such directory. The staged context is deleted with the build
that needed it -- derived data, never litter the next build has to distrust.

None of this is silent, which was the other half of the requirement: `start`
prints where the inputs came from on every packaged build, `doctor` reports
`imageStaleness.inputs` (`checkout` / `packaged` / `none`) plus a line naming the
package-data directory, and an install with *neither* a checkout nor package
data still warns and falls back to `ralphd:dev` -- the one case where staleness
remains `unknowable` because there is genuinely nothing to hash.

## 9. Failure containment

- Agent process crash / nonzero exit → iteration recorded as failed, loop continues
  (budget permitting). Repeated immediate failures (3 consecutive iterations with no
  task-state change) → job fails fast with a diagnostic event.
- Engine crash → container exits; run dir remains consistent (atomic writes);
  `ralphctl resume <run-id>` starts a fresh container against the same run dir and
  the loop continues from `tasks.json` (v0.2).
- Host reboot ⇒ same as engine crash; nothing lives only in container memory.

### The iteration containment boundary (tasks 010/011, #28)

The bullets above are the *coarse* containment; this is the one inside the
engine. What one iteration can do to its run is bounded at a single place:
the `await self.runner.run(...)` in `LoopSupervisor._run_iteration_once` is
wrapped in an `except BaseException` guard, which replaces whatever came out
with a failed `IterationResult` (`error_message` starts
`engine iteration failure: ` and carries the exception's `repr`) and lets the
rest of the iteration's bookkeeping run normally --
`meta.json`'s `endedAt`/`faultClass`, the `iteration.end` event, usage
accounting, the infra refund. So a timed-out iteration, an OS error in the
event pump or a stray cancellation each cost **one iteration, never the run**.

The boundary is deliberately at that `await` rather than around the whole run:
the code that makes a failed attempt legible is *below* it, and a guard placed
further out (`_run_job_core`'s, which stays `except Exception` on purpose) can
only end the job with an "engine error" -- exactly the v0.6 failure this closed,
where a worker iteration that blew `iteration_timeout_s` took the engine down.
That leak was an `asyncio.CancelledError`: `asyncio.wait_for` cancels the future
it timed out on, so the runner's old `await asyncio.wait_for(pump_task, ...)`
re-awaited an already-cancelled task and re-raised, and `CancelledError` is a
`BaseException` no `except Exception` in between could see. The runner's timeout
path now uses `asyncio.wait({pump_task}, timeout=...)` (the task is left alone,
so `pi` can be `SIGINT`ed and its last output drained inside
`runner.SHUTDOWN_GRACE_S`); catching `BaseException` here is the structural half
of the fix, so nothing has to trust that no cancellation ever leaks again.

Two escapes are *not* contained, because each one means "stop, do not start
another iteration": `KeyboardInterrupt`/`SystemExit`, and a cancellation
genuinely requested on this task from outside -- distinguished by
`asyncio.Task.cancelling() > 0`, which is `0` for a `CancelledError` that merely
leaked out of inner plumbing.

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
`<config-dir>/pi/models.json` (task 013/023-026's resolved `apiKey`,
mounted read-only into the container like everything else under
`<config-dir>`) and `<control-creds-dir>/.api-token`
(`~/.ralphd/control/<id>/.api-token`, task 003 -- host-only, never mounted
into any container at all): a private file, never served by any HTTP
route -- no new secret-storage mechanism was
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
  `faultClass` for *that branch* stays deliberately coarse (its job is "never
  retry this as an outage"), which is issue #49.
- **A signal is its own class** (task 013, #49). An iteration that reached the
  model and was then ended by a signal, with **no** abort recorded for the run
  (an OOM kill, a stray `pkill`, a `docker stop` of the agent's process group),
  is `faultClass: "signal"` — not `"work"`, which is the class that burns
  approach and task-failure bookkeeping and would charge the agent for a failure
  a signal committed, and not `"infra"` either, so the retry wrapper (which acts
  on `"infra"` alone) never relaunches into whatever wanted the run stopped.
  Requirement I's own subject: an iteration that `pkill`s something is exactly
  how this shape arises.
- **A bare `aborted` with a clean exit is the provider hanging up, at any
  duration** (task 014, #49 part 2; the duration bound retired by task 117 /
  steering 005). With traffic already observed, a clean exit status
  (`exit_code == 0`) and no abort recorded for the run, a one-word `aborted` is
  `faultClass: "infra"` — retried and refunded — rather than `"work"`. No
  iteration that did real work exits 0 with nothing but that one word as its
  error text. This used to be bounded by `faults.ABORTED_STREAM_MAX_DURATION_S`
  (120s), derived from a single 39s sample (`selfdev-v06-release` iteration
  145); `selfdev-v09-fsm-loop` then produced 15 more real samples of this exact
  shape, 14 of them above 120s, and failed an approach on three of them in a
  row before the bound was retired in favour of deciding the shape alone
  (SPEC §17 entry 1 records the whole history).

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
  (150s) **and is not consuming CPU** (the re-arm described under "Infra-fault
  fail-fast + retry with backoff" above: a starved child on a contended host
  still advances its own CPU counter, a blocked one does not),
  and sets `no_traffic_timeout` — always `"infra"`. Killing early is
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
  `validationAttempts` are untouched. All refund counters (`infra`, `grace`,
  and `steer` — task 014, #98) are written to `status.json` as
  `iterationsRefunded` as they are earned and seeded back by the next engine
  process, so a resume cannot re-charge them (issue #32).
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
`"infra"`, so the wrapper used to distinguish them by *shape over time*, not
by text (task 010, #5):

- A transient fault **varies and takes time** — a gateway 502 arrives after
  a connect, a DNS glitch resolves itself, the error text moves around.
- A broken environment **fails identically in 0.6s, every time**.

Task 017 (v0.19 requirement D, #151) sharpened this: shape-over-time alone
still let a *genuinely* infra-shaped outage (an error text matching a known
`INFRA_SIGNATURES` row — DNS, TCP, TLS, a gateway 5xx, back-pressure …)
repeat identically for a few attempts in a row and get mistaken for the
broken-credential shape, ending a recoverable outage in seconds instead of
letting the outage budget govern it. So an instant
(sub-`INSTANT_FAILURE_MAX_DURATION_S` = 5s), zero-work failure *is* retried
like any other infra fault, and it is ALSO scored by
`_check_instant_failure()` against `_instant_failure_signature()` — exit
code plus error text with digits normalised away (timestamps, ports, request
ids) — UNLESS its error text matches a genuine `INFRA_SIGNATURES` row, in
which case `_check_instant_failure()` ignores it outright (touches neither
the streak nor its signature) and the infra outage budget/backoff ladder is
left to govern it alone. An *unclassified* no-traffic failure (still
`"infra"` by `classify_fault()`'s own catch-all, but with no signature
match — the missing/broken-credential shape) keeps counting: once
`MAX_CONSECUTIVE_INSTANT_FAILURES` = 3 such attempts have failed instantly,
with no traffic and the **same** signature, the wrapper stops with §2's
broken-environment diagnosis ("… likely missing or broken LLM credentials,
or an agent-startup fault …") instead of waiting out the outage budget.
"No work" means no assistant text and no *tokens*: pi zero-fills a usage
block on every `message_end`, so an in-band error's `usage` dict is
non-empty while nothing was ever billed. An instant failure that **did**
reach the model (a 0.3s Bedrock 502 with tokens billed) is handed straight
back to the phase's own bounded error retry, and any iteration that reaches
the model resets the streak — so the fail-fast path can only fire on a run
of genuinely identical, work-free, *unclassified* failures.

`reflect` (§10.2) runs this same wrapper over its own short outage budget
(`REFLECT_OUTAGE_BUDGET_S`) after the job has already ended — possibly
because this very carve-out just tripped. `_begin_reflect_retry_window()`
resets `_instant_failure_streak`/`_instant_failure_sig` (task 017) exactly
as it already reset the episode clock and parked the abort reason: without
that reset, a job that had just aborted at the threshold left the counter
sitting AT it, so reflect's own first attempt — failing the same way, since
the underlying fault is still there — pushed it one past the threshold and
re-tripped the breaker on that ONE attempt, never reaching reflect's own
bounded ladder at all.

## 11. Test environments that model a real developer host (v0.12, #84)

Every suite run this project has ever recorded green ran either in this job
image or in a container. Two scripts under `scripts/` deliberately construct
an environment able to *fail* the way a plain developer checkout can, so
"the suite is green" means the same thing everywhere it is asserted.

### `scripts/host_like_pytest.sh` — environment shape

Models a developer host's **environment variables and import-path
shadowing**, from inside this same job container: every `RALPHD_*`
environment variable unset except `RALPHD_RUN_ID` (preserved, or minted
fresh — see the script's own header, F1/#73), a regular-package `tests`
decoy planted ahead of this repository on `PYTHONPATH` (F2/#74, #64, #50),
and a fresh, empty `$HOME`. The docker tier is filtered out (`-m "not
docker"`) rather than run nested, since this container always has a real,
working docker socket bind-mounted in — there is no clean self-skip to fall
into. `tests/test_host_like_pytest_script.py` proves the mechanism by
running a throwaway probe module through it and reading the properties back
from inside the subprocess.

This script's `$HOME` being fresh and *empty* is itself a blind spot:
`check_hat_routes` (§5, `engine/llm.py`) documents an empty provider list as
"this profile hasn't declared anything yet" and skips its whole check, so a
host_like_pytest.sh run is silent about the ambient-`$HOME`-read class §5
and `tests/test_ambient_home_allowlist_guard.py` close (#83, #85).

### `scripts/host_like_dev_layout.sh` — install layout and dotfiles

A **sibling** script, not a mode of the one above (see its own header for
the full reasoning: different lifecycle, different failure surface — a
mutating `pip install --user -e .` step every host_like_pytest.sh invocation
should not have to pay for). It models the two pieces host_like_pytest.sh's
environment-only shape cannot reach:

- **A user-base editable install.** `PYTHONUSERBASE` is computed from the
  new, scratch `$HOME` once (`site.getuserbase()` evaluated under that
  `$HOME`, portable across platforms whose default derivation differs) and
  threaded explicitly through both the `pip install --user -e ".[dev]"` call
  and the pytest invocation that follows — so `ralphd`'s import path
  genuinely moves when `$HOME` moves, the way it does on a real `--user -e
  .` developer machine and never does in this container's own system-wide
  install (`container/Dockerfile:42`). Requires a non-venv interpreter (a
  virtualenv disables user-site packages outright); the script refuses to
  run under one rather than silently proving nothing.
- **A populated, hostile `$HOME`.** Before the install, a
  `~/.pi/agent/models.json` declaring a provider no test fixture in this
  repository uses, and a skill placed under `~/.pi/agent/skills/<name>/` the
  way `ralphctl start --skills` mounts one. After the pytest run, the script
  re-reads that skill's `SKILL.md` and reports on stdout whether it
  survived — the #83 class is exactly a suite run silently deleting it.

`tests/test_host_like_dev_layout_script.py` proves the mechanism the same
way `tests/test_host_like_pytest_script.py` does for the environment-only
script: a probe module executed through the script, its assertions read
back from inside that subprocess, plus a check that the post-run survival
report actually appears.

## 12. Lexical-only pins (v0.17, G.6, #130; first cut)

Most of this project's test suite is behavioral: it drives real code paths
(the engine loop, the scheduler, a subprocess-driven stub-`pi`) and asserts
on the outcome. A smaller set of tests instead asserts on the literal text
of a prompt or prose file — they pin that specific words appear in a
specific section, nothing about what any agent reading those words then
does. Both kinds of test are real and both fail the build when broken, but
they are not interchangeable evidence: a lexical pin proves the sentence is
still there, not that the behavior it describes still works. This section
names the pins that are lexical by construction, so a reader of a green
suite never mistakes one for behavioral coverage of the thing it describes.

- **Steering durability** — `worker.md`'s `## Operator steering` section
  must state that a steering note is delivered to exactly one iteration,
  that its durable part is copied into the notes file before acting on it,
  and that the copy rule holds under either consumption semantics; the
  ledger the engine renders into later prompts must be described as
  informing, never waiving. Pinned by
  `tests/test_prompt_steering_durability.py`, which regex-matches these
  phrases inside `worker.md`'s own section text. No part of this module
  drives a real steering delivery — that is `tests/test_e2e.py`'s and
  `tests/test_steer.py`'s job.
- **Dependency-doom prose** — `worker.md`'s `## Completion signal` section
  must say that neither failed-task resolution (carve a residual task;
  relabel `skipped`) ever moves the original failed task into
  `TASK_DEPENDENCY_SATISFIED_STATUSES`, and must tell the worker to report
  the doomed dependent set and stop rather than declare victory. Pinned by
  `tests/test_prompt_dependency_doom.py`, which also cross-checks the
  `state.py` constant's actual value so a change there fails the same
  module — but the scheduler's real blocking behavior for a dependent
  naming a `failed`/`skipped` id is proven separately, by
  `tests/test_scheduler.py` and the engine-subprocess tier, not by this
  module.
- **Peer-gate prompt shapes** — `review.md`, `plan_review.md` and
  `verdict_gate.md` must each name the structured findings fields
  (`severity`, `curable`, `clause`, `evidence`) and the blocking/residual
  severities, and must not describe `{"verdict", "reason",
  "findings_path"}` as the whole of a judging hat's payload shape. Pinned
  by `tests/test_review_findings_prompt_shape.py`, asserting substring
  presence in the three prompt files' raw text; the engine-side computation
  those prompts describe (verdict derived from blocking findings only) is
  pinned separately by `tests/test_structured_findings.py` and
  `tests/test_review_verdict_findings.py`.
- **Revision-history-free prompts** — no prompt file under
  `src/ralphd/prompts/` may contain phrasing that only makes sense relative
  to an earlier revision of itself (`previously`, `no longer`, `used to
  be`, …), since a fresh-context agent has no notion of "before" the
  current text. Pinned by `tests/test_prompt_lint.py`'s banned-pattern scan
  over every prompt file; it does not run any prompt through an agent, so
  it cannot detect a prompt that is confusing for reasons other than these
  named phrases.

A lexical pin is not behavioral coverage. It proves a sentence survived an
edit; it does not exercise the loop, the scheduler, or an agent's response
to the sentence. Treat a green lexical-pin module as "the prose is still
there," and look to the named behavioral modules above for evidence that
the thing the prose describes still works.
