"""ralphd.engine.transitions — the declared FSM table for the v0.9 loop.

Task 008 (requirement B, #60 pillar 2): states, legal transitions, guard
predicate *names* and per-hat entries as ONE data structure, with vigilant
and non-vigilant expressed as two variants of that one structure rather than
two implicit paths through `_run_job_core`. Nothing in this module executes
anything and nothing in `_run_job_core` reads it yet — that cutover is
task 014. This module is the design; task 009 adds each hat's response
schema beside its entry, task 013 gives task-state writes one path, task 014
makes the engine actually walk this table instead of the hand-written
branches in `loop.py`.

Every transition and guard name below is traceable to a mechanism cited in
`artifacts/reports/conformance-ledger.md` (task 006/007) or to the PRD's
acceptance text for requirement B; see the `# ledger guard N` / `# fact N`
comments next to each entry.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ralphd.engine import petitions

# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------
# STEP_STATES are states an iteration or a deterministic harness decision
# occupies while an approach is running. `finishing` and the three outcomes
# are RUN states — status.json's top-level `state` field — not step states;
# `finishing` is explicitly non-terminal (fact 11, #56): it is the window
# that covers the reflect unwind before the run promotes to its real
# outcome. `verify` only exists as a *reachable* state in the vigilant
# variant (see VIGILANT_DELTA below) but is always present in the state
# inventory so both variants can be rendered from the same enumeration.

STEP_STATES: tuple[str, ...] = (
    "planning",
    "worker",
    "verify",
    "review",
    "replan",   # the approach boundary, as an explicit state (requirement B)
    "reflect",  # the post-finishing unwind iteration (fact 11, #56)
    # Task 009 (PRD requirement G, #98): the steer hat's own step state --
    # reshapes state (tasks.json inserts/updates/deletes, standing rules),
    # never does the work itself. Reachable only through the declared edge
    # below (worker -> steer), exactly like every other state -- an
    # undeclared target is refused by `is_legal`/`TransitionExecutor.advance`
    # the same way it is for any other state. Task 012 is what makes the
    # harness actually TAKE that edge (the absolute-top-priority routing
    # rule, requirement I); this table entry alone declares that the edge
    # is legal, nothing more.
    "steer",
    # Task 018 (PRD requirement K, #101): the peer plan-review hat's own
    # step state -- reviews the plan `planning` just produced and either
    # approves it (-> worker) or objects, routing back to `planning` for a
    # bounded number of revisions (`JobConfig.peer_plan_review_max_
    # revisions`, an explicit NUMBER, never "until it agrees" --
    # `artifacts/reports/task-018-requirement-k-budget-decision.md`).
    # Reachable only through the declared `planning -> plan_review` edge,
    # gated on `self.cfg.peer_plan_review` -- the opt-in flag is OFF by
    # default, and `table_for`'s third parameter (below) is what keeps
    # the composed table byte-identical to today's whenever it stays off
    # (`tests/test_peer_plan_review_gate.py::
    # test_gate_off_by_default_table_is_byte_identical_to_today`). Present
    # in the state inventory unconditionally anyway, exactly like `verify`
    # is for the vigilant/non-vigilant axis: the hat exists either way,
    # only its *reachability* differs.
    "plan_review",
    # Task 020 (PRD requirement L, #101): the peer VERDICT gate's own step
    # state -- reachable only when `JobConfig.peer_verdict_gate` (opt-in,
    # OFF by default) is on, on its OWN axis exactly like `plan_review`'s
    # (`JobConfig.peer_plan_review`) -- the two never interact, one gates
    # `planning -> worker`, this one gates `review`'s own `verified` edge.
    # Present in the state inventory unconditionally anyway, exactly like
    # `plan_review`/`verify` are, so both variants render from the same
    # enumeration whether or not the gate is on.
    "verdict_gate",
    # Task 004 (PRD requirement C.1, finding 4): the cure-pass edge's own
    # step state -- a pure formalisation of "review took the cure pass"
    # (task 009's `rejected_curable_pass_available`, previously a direct
    # `review -> worker` edge) as a NAMED position in the FSM rather than
    # an unrecorded side effect that happened to land back on `worker`.
    # Deterministic, no hat of its own (like `replan`/`finishing`): the
    # actual append/counter/event work still happens inside `_step_review`
    # (`_take_cure_pass`) before this state is even entered -- `cure`'s own
    # handler (`LoopSupervisor._step_cure`) does nothing but take its one
    # declared edge straight back to `worker`, in the same dispatch pass,
    # so "the worker runs the appended cure tasks" is unchanged end to
    # end. Reachable ONLY through `review`'s `rejected_curable_pass_
    # available` edge, in both `TABLE` and `PEER_VERDICT_GATE_DELTA`.
    "cure",
    # Task 004 (PRD requirement D.1, finding 4): the last-approach pause's
    # own step state -- the same formalisation for task 011/036's
    # `rejected_last_approach` edge, previously a `review -> review`
    # self-loop that relied entirely on `status.json`'s `state: "paused"`
    # (a DIFFERENT namespace, `state.PAUSED_STATE`) to make the parked run
    # observable. `paused`'s own handler (`LoopSupervisor._step_paused`)
    # does the actual parking (`await self._gate()`, the same existing
    # `asyncio.Event` `self.pause()`/`resume()` already drive) and, once
    # released, takes its one declared edge straight back to `review` --
    # never `worker`, never a fresh `replan` -- so a `resume` still
    # re-reviews the SAME approach exactly as before. Reachable through
    # `review`'s `rejected_last_approach` edge, in both `TABLE` and
    # `PEER_VERDICT_GATE_DELTA`.
    #
    # Task 012 (PRD requirement B.3, #153): a SECOND way into this same
    # state -- `worker`'s own `stagnation_last_approach` edge below,
    # taken when the stagnation guard would otherwise fail the run's
    # LAST approach silently. `_step_paused` (loop.py) now branches on
    # which escalation reason parked the run: a `rejected_last_approach`
    # pause still resumes into `review` over the same approach; a
    # `stagnation_last_approach` pause resumes straight back into
    # `worker` over the same approach instead (there is no verdict
    # pending for review to judge, only unfinished worker tasks the
    # approach never archived). Deterministic, no hat of its own.
    "paused",
)

RUN_TERMINAL_STATES: tuple[str, ...] = ("succeeded", "failed", "aborted")

# Every state the table can name, step states plus the run-level ones.
ALL_STATES: tuple[str, ...] = STEP_STATES + ("finishing",) + RUN_TERMINAL_STATES

# The table's single entry point (requirement B: "entry at `planning`").
ENTRY_STATE: str = "planning"


class Transition:
    """One legal edge: a target state, gated by a named predicate.

    The predicate itself is not implemented here — only its *name* is
    declared, so the table stays a pure data structure. Task 014's
    `TransitionExecutor` and `LoopSupervisor`'s `_step_*` handlers compute
    each guard's truth value at runtime and pass the *name* through to
    `is_legal`/`TransitionExecutor.advance` for the refusal message and the
    audit trail — the table itself holds no callable.
    """

    __slots__ = ("guard", "target")

    def __init__(self, target: str, guard: str) -> None:
        self.target = target
        self.guard = guard

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"Transition({self.target!r}, guard={self.guard!r})"

    def __eq__(self, other: object) -> bool:
        return (isinstance(other, Transition)
                and self.target == other.target
                and self.guard == other.guard)

    def __hash__(self) -> int:
        return hash((self.target, self.guard))


def T(target: str, guard: str) -> Transition:
    """Short constructor so the table below reads as data, not code."""
    return Transition(target, guard)


# ---------------------------------------------------------------------------
# The table shared by both variants
# ---------------------------------------------------------------------------
# One entry per state -> tuple of legal Transitions, in the order the
# current engine tries them (first-match-wins is NOT implied here — the
# executor's guard evaluation order is task 014's business; this is the
# inventory of what is legal, not a priority list). Terminal states have an
# empty tuple: there is no transition out of a terminal state, by
# construction, and a test asserts that emptiness holds for exactly the
# three RUN_TERMINAL_STATES and nothing else.

TABLE: dict[str, tuple[Transition, ...]] = {
    "planning": (
        # ledger guard 1: an instant/infra failure retries planning in
        # place without costing the approach (loop.py:1978-1985).
        T("planning", "instant_failure_streak"),
        # ledger guard 2: an empty plan advances the approach rather than
        # entering the worker loop (loop.py:1989).
        T("replan", "empty_plan"),
        # requirement B: "planning -> worker".
        T("worker", "planning_produced_tasks"),
    ),
    "worker": (
        # Task 009 (requirement G/I, #98): unsettled operator steering takes
        # absolute top priority over every other worker edge below --
        # listed first so the table's own order mirrors that priority, even
        # though the executor's guard evaluation order is task 014's
        # business, not this table's (see `Transition`'s docstring). Task
        # 012 is what actually computes `steering_pending`'s truth value
        # and takes this edge deterministically, without consulting the
        # router.
        T("steer", "steering_pending"),
        # the common case: no verdict yet, same state, next task or retry.
        T("worker", "no_verdict_yet"),
        # honoured COMPLETE (ledger guard 8) — verdict ready for review.
        T("review", "verdict_ready"),
        # ledger guard 9: nothing actionable left and something `failed`
        # routes to review for a verdict instead of stagnating.
        T("review", "blocked_with_failures"),
        # ledger guard 10 (task 014 correction, verified against
        # loop.py's actual runtime shape at the cutover: `stagnant >= 3`
        # leaves `verdict_ready` False, so the run goes straight to the
        # approach boundary -- `_archive_approach` then the next approach
        # -- WITHOUT a review verdict ever being requested. The ledger's
        # own guard-10 citation says "fail the approach", not "route to
        # review"; task 008's first draft of this row said `review`, which
        # this task's cutover proved wrong against the code it now
        # executes rather than merely declares.
        T("replan", "stagnation_exhausted"),
        # Task 007 (requirement D, #99): `state.task_blocked_by`'s doomed
        # set covers the ENTIRE actionable remainder -- no worker iteration
        # could EVER move this plan again, not just "not yet" the way an
        # ordinary `blocked_with_failures`/`stagnation_exhausted` pass would
        # read it. Routed to `review` for a verdict, reusing the #33
        # `blocked_with_failures` mechanism (guard 9) rather than starving
        # through three no-progress iterations into `stagnation_exhausted`
        # (guard 10), which would misattribute an UNREACHABLE plan as a
        # merely-stuck approach -- exactly the PRD's complaint. See
        # `LoopSupervisor._doomed_plan_fault`'s own docstring in `loop.py`
        # for the fault payload shape and why this consequence (review, not
        # a dependency-graph replan or a terminal degraded state) was
        # chosen.
        T("review", "doomed_plan_unverifiable"),
        # Task 011 (PRD v0.21 requirement C.4, #187): the entire actionable
        # remainder has been held on the SAME petitioned ancestor for
        # `LoopSupervisor.PETITION_HELD_MAX_NO_TASK_ITERATIONS` no-task
        # passes with no new adjudication outcome -- routed to `review`
        # for a verdict, reusing the exact same edge `doomed_plan_
        # unverifiable`/`blocked_with_failures` already take, rather than
        # letting the held branch's own stagnation exemption (see
        # `petition_held_this_pass` in loop.py) turn into an unbounded
        # spend of the run's own iteration budget.
        T("review", "petition_held_no_task_bound"),
        # Task 025 (requirement H, #63): a worker's `verdict: "unsatisfiable"`
        # report is a state change (the named task moves to `skipped`,
        # `unsatisfiableReason` recorded on its record) the moment the
        # harness applies it -- so it is progress, not the silence the
        # stagnation guard exists to catch. Two declared outcomes, chosen
        # by whether anything else is still actionable once that one task is
        # off the table: other work left keeps the SAME approach and the
        # worker loop simply continues (a targeted replan of that one task
        # -- worker.md's own carve-a-new-task/skip escape hatch handles the
        # follow-up, on a LATER iteration, same as it already does for a
        # `failed` task); nothing left routes to review for a verdict, same
        # reasoning as `blocked_with_failures` just above.
        T("worker", "unsatisfiable_targeted_replan"),
        T("review", "unsatisfiable_and_nothing_left"),
        # v0.20 task 021 (PRD requirement C.1, #156): a worker's `verdict:
        # "deferred"` report -- the task moves BEHIND every runnable task
        # (back to `pending`, with `deferredUntil` recorded) rather than
        # absorbing further iterations on it right now. This is a real
        # `tasks.json` write the moment the harness applies it, so it is
        # progress, not the silence the stagnation guard exists to catch
        # (`stagnant` resets to 0) -- but, unlike `unsatisfiable_targeted_
        # replan` just above, it never spends the deferred task's own
        # `progressBudget` either: deferring is a reorder, not an attempt.
        # A worker self-loop, same shape as the unsatisfiable route just
        # above: the SAME approach continues, the scheduler picks whatever
        # independent task is next in line.
        T("worker", "worker_deferred_reordered"),
        # Task 012 (PRD requirement B.3, #153): a stagnant LAST approach
        # (`self._approach == self.cfg.max_approaches`) must not end
        # `failed` silently. Task 018 (PRD requirement B.3, #156)
        # generalises this SAME edge to any approach that PROGRESSED --
        # gained a workspace commit somewhere during the approach, or left
        # a `superseded` task behind from a forced split (task 016) --
        # before its own three-strike counter stalled: a `superseded`
        # task's children exhausting THEIR budgets too, or a stagnant
        # counter tripping over a branch that gained commits this
        # approach, is not the "genuinely silent" case the ordinary
        # `stagnation_exhausted` edge above is for -- that edge still
        # fires for exactly that case, on every approach that has a
        # successor left ("a genuinely silent approach with approaches
        # left still fails as today"). Either way -- last approach or a
        # progressing earlier one -- the run instead takes THIS edge:
        # `_pause_and_escalate_stagnation_last_approach` (loop.py) pauses
        # the run through the same existing pause machinery `rejected_
        # last_approach` already uses, emits an `operator_escalation`
        # event with `reason: "stagnation"`, and notifies -- the approach
        # is left unarchived, exactly like `rejected_last_approach`, so
        # `resume` re-enters the SAME approach (`_step_paused` routes a
        # `stagnation_last_approach` pause back to `worker`, never
        # `review` -- there is no verdict pending, only unfinished tasks).
        T("paused", "stagnation_last_approach"),
        # Task 002 (PRD requirement E, #206): a `dependsOn` CYCLE (a
        # self-loop is the length-one case) among the plan's own tasks
        # makes the touched tasks permanently unschedulable BY
        # CONSTRUCTION -- structurally distinct from the doomed-set case
        # `doomed_plan_unverifiable` above handles (that one follows
        # `failed`/`skipped`/`petitioned` ancestors; a cycle among
        # `pending` tasks has none of those and is invisible to it).
        # Routed to `paused`, not `review`: there is no worker verdict to
        # adjudicate, only a `tasks.json` edit an operator has to make by
        # hand to break the cycle. See
        # `LoopSupervisor._unschedulable_cycle_fault`'s own docstring in
        # `loop.py` for the fault payload shape and the separating
        # condition from a merely in-flight dependency (E.6).
        T("paused", "unschedulable_dependency_cycle"),
        # Review cure cure-01-03 (PRD requirement E.1/E.2, #206's second
        # defect): the exact sibling of the `unschedulable_dependency_
        # cycle` edge just above, for a DIFFERENT unreachable shape -- an
        # actionable task's `dependsOn` names an id that is not a node in
        # the plan graph at all (no ancestor to follow, no cycle to close,
        # just an edge pointing at nothing). See `LoopSupervisor._
        # unresolvable_edge_fault`'s own docstring in `loop.py` for the
        # fault payload shape and the separating condition from a merely
        # in-flight dependency (E.6).
        T("paused", "unresolvable_dependency_edge"),
    ),
    "review": (
        # ledger guard 12: a VERIFIED verdict is discarded while operator
        # steering is pending, and the run is routed back to worker so it
        # gets consumed (fact 10, STEERING_ACTIONABLE_PHASES).
        T("worker", "steering_pending_at_verified"),
        # the run's one success edge.
        T("finishing", "verified"),
        # rejected, and an approach is still available — replan.
        T("replan", "rejected_approach_left"),
        # Task 006 (PRD requirement B.2, #111): a rejection whose BLOCKING
        # findings are ALL incurable (`findings.compute_verdict` says
        # "rejected" AND `findings.all_blocking_findings_incurable` says
        # `True`) takes THIS edge instead of `rejected_approach_left` --
        # spending a fresh approach on findings a fresh approach cannot
        # change is certain rejection, the same "route rather than starve"
        # precedent `doomed_plan_unverifiable` (worker, task 007/#99)
        # already set for an unwinnable plan. Self-loops back to `review`
        # itself -- `_step_review` calls `self.pause()` (the EXISTING
        # pause machinery, `_gate()`'s `self._pause` event, no new pause
        # implementation) and produces an operator escalation (each
        # finding plus the `ralphctl amend` invocation that would resolve
        # it) before returning this edge; the run parks at the very next
        # `_gate()` call inside the re-entered `review` state -- i.e. on
        # resume it asks review again over the SAME approach (`_approach`
        # untouched, `_archive_approach`/`_write_composite_prd` never
        # called for this branch) rather than planning a fresh one. Task
        # 007 owns making a freshly-recorded ruling visible to that
        # resumed review pass; task 008 pins that a CURABLE blocking
        # finding still takes `rejected_approach_left` above, and that
        # `rejected_no_approach_left` below still wins once the approach
        # budget is spent (checked first in `_step_review`, ahead of this
        # edge, so an exhausted budget never pauses instead of ending).
        T("review", "rejected_incurable"),
        # Task 009 (PRD requirement C.1, #141): a rejection whose blocking
        # findings are ALL curable (`findings.compute_verdict` says
        # "rejected" AND `findings.all_blocking_findings_curable` says
        # `True`) takes THIS edge -- back to `worker`, never `replan` --
        # on the first rejection per approach (`curePasses[approach] ==
        # 0`, checked by `LoopSupervisor._step_review` AFTER the incurable
        # check above so `rejected_incurable` keeps its precedence when
        # every blocking finding is incurable, requirement C.2). The
        # engine appends one cure task per blocking finding to THIS
        # approach's `tasks.json` (`state.insert_cure_tasks`), increments
        # `curePasses` on status.json and emits `approach.cure_pass`
        # before taking this edge; task 010 bounds a SECOND rejection in
        # the same approach back onto the ordinary `rejected_approach_
        # left` edge below UNLESS task 030 (PRD requirement E.1, #167)
        # grants another pass -- up to a configured ceiling on the RUN's
        # own total cure passes over both cure channels
        # (`JobConfig.cure_pass_ceiling`, default 3, task 002 PRD
        # requirement A.2: `LoopSupervisor._cure_passes_for_run`, so a
        # replanned approach does not reset the budget), and only when the
        # new rejection's
        # own blocking clause set is not a SUPERSET of the immediately
        # preceding granted pass's own set (`LoopSupervisor._review_cure_pass_
        # available`).
        #
        # Task 004 (PRD requirement C.1, finding 4): the target is now the
        # dedicated `cure` step state, not `worker` directly -- `cure`'s
        # own edge (below) takes the run the rest of the way to `worker`,
        # in the same dispatch pass, so this is a formalisation of what
        # already happened, not a behaviour change.
        T("cure", "rejected_curable_pass_available"),
        # Task 011 (PRD requirement D.1, #142): on the FINAL approach
        # (`approach == max_approaches`), a rejection whose blocking
        # findings are ALL curable but whose one cure pass for this
        # approach is already spent (`rejected_curable_pass_available`
        # above already took it when it was still available -- this edge
        # only fires once that is no longer true) must not spend the
        # nonexistent next approach on paperwork: it self-loops back to
        # `review`, exactly like `rejected_incurable` does, generalising
        # `_pause_and_escalate_incurable_rejection` into `_pause_and_
        # escalate_last_approach_rejection` -- `operator_escalation` with
        # `reason: rejected_last_approach`, the EXISTING pause machinery
        # (`self.pause()`), and the approach left unarchived (`resume`
        # after an `amend`/`--waive` re-runs review over the SAME
        # approach, never a fresh one). A rejection with any incurable
        # blocking finding on the last approach still falls through to
        # `rejected_no_approach_left` below (requirement D.2).
        #
        # Task 004 (PRD requirement D.1, finding 4): the target is now the
        # dedicated `paused` step state, not a `review -> review` self-
        # loop -- `paused`'s own edge (below) parks the run
        # (`LoopSupervisor._step_paused`, the same `self._pause`
        # `asyncio.Event` this self-loop always relied on) and, once
        # resumed, lands back on `review` over the SAME approach, which is
        # exactly what the self-loop already did; this is a
        # formalisation, not a behaviour change.
        T("paused", "rejected_last_approach"),
        # Task 016 (PRD requirement E.5, #190): a rejection whose own
        # blocking-clause signature repeats the IMMEDIATELY PRECEDING
        # approach's own rejection signature (`LoopSupervisor.
        # _repeated_rejection_signature`) self-loops back to `review`,
        # exactly like `rejected_incurable`/`rejected_last_approach` --
        # `_pause_and_escalate_repeated_rejection_signature` pauses the run
        # (existing pause machinery, `operator_escalation` with `reason:
        # rejected_repeated_signature`) rather than spending a fresh
        # approach on findings a fresh approach would almost certainly
        # repeat identically. Checked AFTER every other rejection edge
        # above keeps their own precedence exactly as documented at each
        # of their own call sites in `LoopSupervisor._step_review`; the
        # approach is left unarchived, so `resume` re-reviews the SAME
        # approach, never a fresh one.
        T("paused", "rejected_repeated_signature"),
        # GH #209 defect 2: review's OWN contract ladder exhausted
        # (`contract_enforcement.enforce_contract` gave up after
        # `contract_retry_max + 1` attempts and emitted `contract.
        # agent_fault` for hat `review`) with the hat's OWN VERDICT CLAIM
        # still unreadable -- no parseable object at all, or a `verdict`
        # absent or outside the enum below; an exhaustion over the review's
        # findings sidecar alone, with a legible `rejected` verdict in the
        # payload, deliberately keeps the ordinary rejection edges above
        # (`LoopSupervisor._review_contract_ladder`'s own
        # `verdict_claim_unreadable` is where that line is drawn). A hat
        # that never returned a verdict
        # has not returned one, so this holds for an operator on the
        # EXISTING pause machinery (`LoopSupervisor._pause_and_escalate_
        # review_contract_unrepaired`) instead of scoring a substantive
        # rejection off a payload nobody could read: before this the
        # exhausted ladder handed the still-broken reply straight to
        # `_review_verdict`, whose "not verified" default archived the
        # approach and wrote a composite PRD for a successor that need not
        # exist -- one of the reference run's three review faults was the
        # pure schema pedantry `$.verdict: 'fail' is not one of
        # ['verified', 'rejected']`, and it cost an approach. Checked
        # BEFORE every rejection edge above in `_step_review`, because
        # there is no rejection to classify. Deliberately unconditional
        # (no `approach < max_approaches` guard), exactly as cure task
        # cure-01-03 made `rejected_repeated_signature` unconditional; the
        # approach is left unarchived, so `resume` re-enters `review` over
        # the SAME approach through `_step_paused`'s default branch.
        T("paused", "review_contract_unrepaired"),
        # rejected, and this was the last approach / budget is gone.
        T("finishing", "rejected_no_approach_left"),
        # requirement A, #72: the review iteration itself got cut short
        # (IterationResult.interrupted, fact 9) rather than replying with
        # a scored verdict -- that is not a rejection, so this edge stays in
        # `review` and asks again, the same shape as `planning`'s
        # `instant_failure_streak` and `worker`'s `no_verdict_yet`
        # self-loops: it must not archive the approach, must not emit a
        # `review rejected` event, and must not advance the approach
        # counter.
        T("review", "review_interrupted"),
        # requirement A, #72: the interrupted-review case above, but there
        # is no wall clock or retry budget left to ask again. This still
        # must not read as a rejection -- no approach archive, no `review
        # rejected` event, no composite PRD for a successor approach that
        # will never run -- it only ends the run via the ordinary
        # non-terminal `finishing` gate, distinct from `rejected_no_
        # approach_left`'s target even though both land on `finishing`.
        T("finishing", "review_unjudged_at_budget_end"),
    ),
    "replan": (
        # ledger guard NEW (requirement I, #61): a repeated STRUCTURAL
        # blocker -- this approach's stagnation signature equals its
        # predecessor's -- ends the run right here instead of consuming
        # (and losing) another approach on the same wall
        # (loop.py:_step_replan / _blocker_signature / _record_
        # stagnation_signature). A DIFFERENT blocker signature never
        # matches, so a distinct wall still gets its retry.
        T("finishing", "repeated_blocker_signature"),
        # requirement B: "the approach boundary as a transition with a
        # guard". Two outcomes only: another approach, or the run is out of
        # budget/approaches and must finish.
        T("planning", "budget_and_approach_left"),
        T("finishing", "budget_or_approach_exhausted"),
    ),
    "finishing": (
        # fact 11 / #56: `finishing` is non-terminal — it always has a
        # successor, either the reflect unwind or straight to the decided
        # outcome when reflect is off.
        T("reflect", "reflect_enabled"),
        T("succeeded", "reflect_disabled_outcome_succeeded"),
        T("failed", "reflect_disabled_outcome_failed"),
        T("aborted", "reflect_disabled_outcome_aborted"),
    ),
    "reflect": (
        # the reflect unwind always ends by promoting to the outcome that
        # was decided before `finishing` was entered (`_promote_finishing_state`).
        T("succeeded", "reflect_done_outcome_succeeded"),
        T("failed", "reflect_done_outcome_failed"),
        T("aborted", "reflect_done_outcome_aborted"),
    ),
    # Task 009 (requirement G/I, #98): once the steer hat has settled every
    # pending steering message for this iteration ("all pending steers in
    # one iteration, in arrival order"), control returns to `worker` -- the
    # ordinary lifecycle then does and verifies whatever the hat just
    # inserted/updated, same as any planner-proposed task.
    "steer": (
        T("worker", "steer_settled"),
    ),
    # Task 004 (PRD requirement C.1, finding 4): `cure` -- reached only via
    # `review`'s `rejected_curable_pass_available` edge above (and its
    # `PEER_VERDICT_GATE_DELTA` twin below) -- has exactly one declared
    # successor, straight back to `worker`, taken unconditionally and
    # immediately by `LoopSupervisor._step_cure` (the append/counter/event
    # side effects already ran inside `_step_review` before this state was
    # entered; nothing here is contingent on anything).
    "cure": (
        T("worker", "cure_pass_appended"),
    ),
    # Task 004 (PRD requirement D.1, finding 4): `paused` -- reached via
    # `review`'s `rejected_last_approach` edge above (and its
    # `PEER_VERDICT_GATE_DELTA` twin below) -- has this successor, back
    # to `review` over the SAME approach, taken by
    # `LoopSupervisor._step_paused` only once the existing `self._pause`
    # `asyncio.Event` is released again (`ralphctl resume`/`POST /resume`).
    #
    # Task 012 (PRD requirement B.3, #153): a SECOND successor, back to
    # `worker` instead, taken when the pause that parked the run was
    # `worker`'s own `stagnation_last_approach` edge above rather than
    # `review`'s `rejected_last_approach` one -- `_step_paused` decides
    # which of the two by reading the pause it is actually resuming from
    # (`pauseReason` captured before the gate releases), never by
    # guessing from the approach number alone.
    "paused": (
        T("review", "paused_resumed"),
        T("worker", "paused_resumed_stagnation"),
        # Task 002 (PRD requirement E.3, #206): the third successor,
        # taken when the pause that parked the run was `worker`'s own
        # `unschedulable_dependency_cycle` edge above -- same reasoning as
        # `paused_resumed_stagnation`: there is no verdict pending, only
        # unfinished tasks (here, an operator's `tasks.json` edit to break
        # the cycle), so this resumes straight back into `worker`.
        T("worker", "paused_resumed_unschedulable_cycle"),
        # Review cure cure-01-03 (PRD requirement E.3, #206): a FOURTH
        # successor, taken when the pause that parked the run was
        # `worker`'s own `unresolvable_dependency_edge` edge above --
        # same reasoning as `paused_resumed_unschedulable_cycle`: there
        # is no verdict pending, only an operator's `tasks.json` edit to
        # repair the absent-id edge, so this resumes straight back into
        # `worker` too.
        T("worker", "paused_resumed_unresolvable_edge"),
    ),
    # Task 018 (requirement K, #101): `plan_review` has NO base-table entry
    # -- unlike `verify` (which is always a key, forced to `()` when
    # non-vigilant), this state's edges exist ONLY inside
    # `PEER_PLAN_REVIEW_DELTA` below, so `table_for(vigilant)` (no third
    # argument, or `peer_plan_review=False`) never gains a `"plan_review"`
    # key at all -- `.get(state, ())` already answers `()` for an absent
    # key, which is exactly what keeps the off-by-default composed table
    # byte-identical to today's (a dict with an extra `"plan_review": ()`
    # entry would NOT be byte-identical to one missing that key).
    "succeeded": (),
    "failed": (),
    "aborted": (),
}


# ---------------------------------------------------------------------------
# The vigilant delta — the ONLY difference between the two variants
# ---------------------------------------------------------------------------
# Requirement B: "the two modes differ only by which table variant is
# selected". `verify` is reachable only under vigilant (ledger guard 4: the
# disk-persisted verified record gates it, not a before/after diff), and a
# vigilant worker also transitions to `verify` per completed task pending
# verification. Non-vigilant leaves `TABLE` untouched and simply never
# enters `verify` (`verify`'s successors are empty in that variant, and
# nothing in `worker`'s non-vigilant successors names it as a target).

VIGILANT_DELTA: dict[str, tuple[Transition, ...]] = {
    "worker": TABLE["worker"] + (
        # ledger guard 4: a completed task not yet in the verified record
        # (this process's or a resumed one's) must be verified.
        T("verify", "completed_task_pending_verification"),
    ),
    "verify": (
        # requirement B: "verify -> worker | worker(next)". Both targets are
        # `worker`; the guard name is what distinguishes "retry the same
        # task" from "move on to the next pending task" — the table records
        # both edges rather than collapsing them into one, so a test can
        # name each guard separately even though they share a target.
        T("worker", "verify_failed_retry_same_task"),
        T("worker", "verify_passed_next_task"),
    ),
}


# ---------------------------------------------------------------------------
# The peer-plan-review delta — task 018, requirement K, #101
# ---------------------------------------------------------------------------
# A SECOND, INDEPENDENT delta beside `VIGILANT_DELTA`, on its own axis
# (`JobConfig.peer_plan_review`, opt-in, OFF by default) -- the two never
# interact: this delta only ever touches `planning`/`plan_review`, and
# `VIGILANT_DELTA` only ever touches `worker`/`verify`, so composing both at
# once (a vigilant run with the gate on) is unambiguous regardless of
# application order.
#
# `planning`'s WHOLE tuple is replaced (not appended to, the same shape
# `VIGILANT_DELTA["verify"]` already uses for a full replacement) so that
# its one changed edge -- `planning_produced_tasks` targeting `worker` --
# is retargeted at `plan_review` under a DELIBERATELY DIFFERENT guard name
# (`planning_produced_tasks_peer_review_enabled`) rather than reusing the
# same guard name for two different targets: the fact being guarded ("a
# plan proposal was ingested") is the same, but naming the two edges
# distinctly keeps `scripts/render_transition_table.py`'s rendered doc
# unambiguous (no guard name that maps to two different targets depending
# on config nobody reading a single row can see).
#
# `plan_review`'s three edges: `plan_review_approved` (-> worker, the plan
# proceeds), `plan_review_objected_revision_available` (-> planning, one
# more revision), `plan_review_revision_bound_reached` (-> worker, the
# SAME target as approval -- the plan proceeds anyway once
# `JobConfig.peer_plan_review_max_revisions`, an explicit NUMBER, is spent,
# because "until it agrees" is exactly the unbounded loop this requirement
# forbids). `LoopSupervisor._step_plan_review` (loop.py) is what computes
# which of these three fires, deterministically from the hat's own
# `verdict` field -- never through `engine.router` (that widening to a
# genuinely router-mediated multi-edge hat is task 019's business,
# requirement M, the FIRST hat to exercise it in production; this hat
# picks its own edge exactly like `_step_worker`'s `_apply_reported_task_
# status` and `_review_verdict` already do for theirs).
#
# Two more edges since #161 (a resume re-entered `plan_review` over an
# approved plan; the interrupted review spent a "revision" whose ingestion
# would have reset every validated task to `pending`):
# `planning_skipped_plan_already_reviewed` (planning -> worker) is the
# resume edge -- `_step_planning(skip_planning=True)` takes it instead of
# the peer-review edge when this approach's plan was already approved
# (`status.json` `planReviewApproved`) or already has recorded work (any
# task past `pending`), so a resume never re-asks the peer about a plan
# that is being worked. `plan_review_revision_refused_work_recorded`
# (plan_review -> worker) is the safety net behind it: whatever brought
# the gate back over a worked plan, a revision is refused -- a fresh
# `plan-proposal.json` is ingested with every status `pending`, and that
# is a status wipe, not a revision, once work exists. Neither edge is legal
# with the gate off (they live only in this delta, like the other four).
PEER_PLAN_REVIEW_DELTA: dict[str, tuple[Transition, ...]] = {
    "planning": (
        T("planning", "instant_failure_streak"),
        T("replan", "empty_plan"),
        T("plan_review", "planning_produced_tasks_peer_review_enabled"),
        T("worker", "planning_skipped_plan_already_reviewed"),
    ),
    "plan_review": (
        T("worker", "plan_review_approved"),
        T("planning", "plan_review_objected_revision_available"),
        T("worker", "plan_review_revision_bound_reached"),
        T("worker", "plan_review_revision_refused_work_recorded"),
    ),
}


# ---------------------------------------------------------------------------
# The peer-VERDICT-gate delta — task 020, requirement L, #101
# ---------------------------------------------------------------------------
# A THIRD, INDEPENDENT delta beside `VIGILANT_DELTA` and
# `PEER_PLAN_REVIEW_DELTA`, on its own axis (`JobConfig.peer_verdict_gate`,
# opt-in, OFF by default) -- it only ever touches `review`/`verdict_gate`,
# so composing it with either (or both) of the other two deltas at once is
# unambiguous regardless of application order: `VIGILANT_DELTA` only ever
# touches `worker`/`verify`, `PEER_PLAN_REVIEW_DELTA` only ever touches
# `planning`/`plan_review`.
#
# `review`'s WHOLE tuple is replaced (the same full-replacement shape
# `PEER_PLAN_REVIEW_DELTA["planning"]` already uses) so that its one
# changed edge -- the run's ONE success edge, `verified` -> `finishing` --
# is retargeted at `verdict_gate` under a DELIBERATELY DIFFERENT guard name
# (`verified_peer_verdict_gate_enabled`) rather than reusing `verified` for
# two different targets, same reasoning `PEER_PLAN_REVIEW_DELTA`'s own
# comment gives for `planning_produced_tasks_peer_review_enabled`. Every
# other `review` edge (steering-deferred, both rejections, both
# interrupted-review edges, requirement A/#72) is carried over byte-for-
# byte unchanged.
#
# `verdict_gate`'s three edges, deliberately mirroring `review`'s own
# verified/rejected shape one level up rather than `plan_review`'s
# approve/object vocabulary (this hat judges a FINISHED APPROACH, the same
# kind of judgement `review` itself makes, not a PLAN):
# `verdict_gate_approved` (-> finishing, i.e. the run's real success edge
# now lives HERE), `verdict_gate_rejected_approach_left` (-> replan, an
# approach is still available for a fresh worker/review pass -- see this
# task's commit message and
# `artifacts/reports/task-020-requirement-l-rejection-semantics.md` for why
# a NEW APPROACH, not a targeted worker fix-up, is the decided rejection
# semantics), `verdict_gate_rejected_no_approach_left` (-> finishing, the
# same fail-open-on-budget-exhaustion shape every other bounded gate in
# this table already uses -- a peer gate must never be able to hold a run
# hostage once its own approach budget is spent). `LoopSupervisor.
# _step_verdict_gate` (loop.py) computes which of these three fires,
# deterministically from the hat's own `verdict` field -- an absent/
# malformed reply, or a `rejected` verdict with no `reason`
# (`transitions.verdict_gate_report_errors`), defaults to the SAFE choice
# ("not verified", i.e. rejected) exactly the way `_review_verdict`'s own
# malformed-reply default already works for `review` -- never through
# `engine.router`: this hat's two live edges (approved / rejected) are
# always decidable from the hat's own contract payload alone, so there is
# no genuine choice left for the router to resolve, unlike `plan_review`'s
# undecidable-reply case (task 019, requirement M).
PEER_VERDICT_GATE_DELTA: dict[str, tuple[Transition, ...]] = {
    "review": (
        T("worker", "steering_pending_at_verified"),
        T("verdict_gate", "verified_peer_verdict_gate_enabled"),
        T("replan", "rejected_approach_left"),
        # Task 006 (PRD requirement B.2, #111): carried over byte-for-byte
        # from `TABLE["review"]` above -- the incurable-rejection check
        # runs in `_step_review` on the REJECTED branch, entirely before
        # the peer verdict gate (which only ever sits on the VERIFIED
        # branch) is consulted, so this edge's meaning is identical
        # whether or not the gate is on.
        T("review", "rejected_incurable"),
        # Task 009 (PRD requirement C.1, #141): the cure-pass edge, also
        # carried over byte-for-byte -- same reasoning as
        # `rejected_incurable` just above: the cure-pass check runs on the
        # REJECTED branch too, entirely before the (VERIFIED-only) peer
        # verdict gate is ever consulted. Task 004 (finding 4) retargeted
        # this at `cure` instead of `worker` directly, in `TABLE` above --
        # carried over identically here for the same reason every other
        # entry in this delta is.
        T("cure", "rejected_curable_pass_available"),
        # Task 011 (PRD requirement D.1, #142): carried over byte-for-byte
        # from `TABLE["review"]` above -- same reasoning as `rejected_
        # incurable`/`rejected_curable_pass_available` just above: this
        # check runs on the REJECTED branch too, entirely before the
        # (VERIFIED-only) peer verdict gate is ever consulted. Task 004
        # (finding 4) retargeted this at `paused` instead of `review`, in
        # `TABLE` above -- carried over identically here.
        T("paused", "rejected_last_approach"),
        # Task 016 (PRD requirement E.5, #190): carried over byte-for-byte
        # from `TABLE["review"]` above -- same reasoning as `rejected_
        # incurable`/`rejected_last_approach` just above: the repeated-
        # signature check runs on the REJECTED branch too, entirely
        # before the (VERIFIED-only) peer verdict gate is ever consulted.
        T("paused", "rejected_repeated_signature"),
        # GH #209 defect 2: carried over byte-for-byte from
        # `TABLE["review"]` above -- same reasoning as `rejected_
        # incurable`/`rejected_repeated_signature` just above, only more
        # so: this check runs entirely before there is any verdict to
        # classify at all, so it is reached long before the (VERIFIED-only)
        # peer verdict gate is ever consulted.
        T("paused", "review_contract_unrepaired"),
        T("finishing", "rejected_no_approach_left"),
        T("review", "review_interrupted"),
        T("finishing", "review_unjudged_at_budget_end"),
    ),
    "verdict_gate": (
        T("finishing", "verdict_gate_approved"),
        # Task 001 (PRD requirement A.1, #192): the peer gate's own
        # cure-pass edge -- a rejection whose blocking findings are ALL
        # curable takes this edge, straight to the same `cure` state
        # `review`'s own `rejected_curable_pass_available` edge already
        # targets, rather than spending this approach's replan (or ending
        # the run once the budget is spent). Declared BEFORE the ordinary
        # replan/no-approach-left edges below, mirroring `TABLE["review"]`'s
        # own ordering, because `LoopSupervisor._step_verdict_gate` checks
        # `_gate_cure_pass_available` before falling through to either.
        T("cure", "verdict_gate_rejected_curable_pass_available"),
        T("replan", "verdict_gate_rejected_approach_left"),
        T("finishing", "verdict_gate_rejected_no_approach_left"),
    ),
}


def table_for(vigilant: bool, peer_plan_review: bool = False,
              peer_verdict_gate: bool = False) -> dict[str, tuple[Transition, ...]]:
    """Compose `TABLE` with `VIGILANT_DELTA` (vigilant) or leave `verify`
    unreachable (non-vigilant), THEN layer `PEER_PLAN_REVIEW_DELTA` and/or
    `PEER_VERDICT_GATE_DELTA` on top when their respective flags are True.
    This is the ONLY function that produces a variant's table — there must
    never be a second, hand-written table sitting beside this one.

    `peer_plan_review`/`peer_verdict_gate` both default to `False` so every
    pre-existing caller that only ever passed `vigilant` (or `vigilant` +
    `peer_plan_review`) keeps getting EXACTLY the same table it always did,
    unchanged -- this is what requirement K's and requirement L's "off by
    default, byte-identical to today's" acceptance both rest on
    (`tests/test_peer_plan_review_gate.py`, `tests/test_peer_verdict_gate.py`).
    """
    table = dict(TABLE)
    if vigilant:
        table.update(VIGILANT_DELTA)
    else:
        table["verify"] = ()
    if peer_plan_review:
        table.update(PEER_PLAN_REVIEW_DELTA)
    if peer_verdict_gate:
        table.update(PEER_VERDICT_GATE_DELTA)
    return table


def successors(state: str, vigilant: bool, peer_plan_review: bool = False,
               peer_verdict_gate: bool = False) -> tuple[Transition, ...]:
    """Every legal transition out of `state` for the given variant. Always
    returns a tuple (possibly empty) — never raises for a state the table
    knows about, so successors are enumerable for every declared state."""
    return table_for(vigilant, peer_plan_review, peer_verdict_gate).get(state, ())


def is_legal(state: str, target: str, vigilant: bool, peer_plan_review: bool = False,
             peer_verdict_gate: bool = False) -> bool:
    """True iff `target` is a legal successor of `state` in this variant."""
    return any(edge.target == target
               for edge in successors(state, vigilant, peer_plan_review, peer_verdict_gate))


def guard_names(state: str, vigilant: bool, peer_plan_review: bool = False,
                peer_verdict_gate: bool = False) -> tuple[str, ...]:
    """The declared guard names for `state`'s transitions, in table order."""
    return tuple(edge.guard for edge in
                 successors(state, vigilant, peer_plan_review, peer_verdict_gate))


# ---------------------------------------------------------------------------
# Per-hat entries
# ---------------------------------------------------------------------------
# Which hat (prompt-driven role) occupies each step state. `replan` and
# `finishing` are deterministic harness decisions with no hat of their own:
# `replan` is the approach-boundary bookkeeping between review and the next
# planning entry, and `finishing` is the reflect-unwind gate, not a role an
# agent plays. Task 009 hangs each hat's response-contract schema beside
# this same mapping.

STATE_HAT: dict[str, str | None] = {
    "planning": "planning",
    "worker": "worker",
    "verify": "verify",
    "review": "review",
    "replan": None,
    "finishing": None,
    "reflect": "reflect",
    # Task 004 (PRD requirement C.1/D.1, finding 4): `cure`/`paused` are
    # deterministic harness decisions, exactly like `replan`/`finishing`
    # above -- no hat of their own, no prompt, no schema.
    "cure": None,
    "paused": None,
    # Task 009 (requirement G, #98): the steer hat occupies its own state.
    "steer": "steer",
    # Task 018 (requirement K, #101): the peer plan-review hat occupies its
    # own state too, exactly like `steer` -- reachable only when
    # `JobConfig.peer_plan_review` is on, unreachable (but still a real
    # hat with a schema and a prompt, see `HATS` just below) otherwise.
    "plan_review": "plan_review",
    # Task 020 (requirement L, #101): the peer verdict-gate hat occupies
    # its own state too, on the SAME axis as `plan_review` -- reachable
    # only when `JobConfig.peer_verdict_gate` is on, unreachable (but
    # still a real hat with a schema and a prompt, see `HATS` just below)
    # otherwise.
    "verdict_gate": "verdict_gate",
    "succeeded": None,
    "failed": None,
    "aborted": None,
}

# The hats that need a response-contract schema (task 009) and a prompt.
# `verify` is included even though it is unreachable in the non-vigilant
# variant: the hat exists in both, the *transition* into it does not.
# `steer` (task 009, requirement G, #98) joins the same way, and
# `plan_review` (task 018, requirement K, #101) joins on the SAME axis --
# unreachable when `JobConfig.peer_plan_review` is off, but a real hat with
# a real schema, prompt and `MODEL_HATS` entry either way. `verdict_gate`
# (task 020, requirement L, #101) is the fourth hat on this same shape,
# gated by `JobConfig.peer_verdict_gate` instead.
HATS: tuple[str, ...] = ("planning", "worker", "verify", "review", "reflect",
                        "steer", "plan_review", "verdict_gate")


# ---------------------------------------------------------------------------
# The router's declared fallback -- task 018, requirement E, #60 pillar 2
# ---------------------------------------------------------------------------
# "The harness decides wherever the table makes it unambiguous -- one legal
# transition, or an obvious next pending task. Only a genuine choice reaches
# a model." Every hand-written `_step_*` method (planning/worker/review/
# replan) computes its own guard directly from disk state and NEVER reaches
# the router, whatever this dict says -- `ROUTER_FALLBACK` matters to
# `engine.loop._route_with_router`, the ONE production router call site
# (reached from `_step_generic_hat`, task 015's shipped fallback for any
# OTHER state the table names, and from `_step_plan_review`, task 019's
# first real one), and then only when that state has more than one legal
# edge (task 015 shipped only the single-edge case; task 018 is what makes
# a multi-edge one work, via `engine.router`).
#
# "Say in the table what that fallback is for each ambiguous state"
# (requirement E's acceptance): a state that can reach the router MUST name,
# right here, which of its own declared edges (by GUARD name) the harness
# takes when the router's answer is invalid/unparseable even after the
# retry budget, or when the call times out. This is not a second table --
# it is a value alongside the one that already exists, exactly like
# STATE_HAT/HAT_SCHEMAS beside TABLE.
#
# Task 019 (requirement M, #60 pillar 2): the first REAL entry --
# `plan_review` (task 018, requirement K, #101) is the first genuinely
# multi-edge hat this table declares (three edges in
# `PEER_PLAN_REVIEW_DELTA` above: approved, objected-revision, revision-
# bound-reached), and the first state to reach `engine.router.decide()` in
# PRODUCTION. `_step_plan_review` (loop.py) still decides its own guard
# deterministically from the hat's own `verdict` field whenever that
# verdict is decidable -- exactly like `_step_worker`/`_step_review` do for
# theirs -- but an UNDECIDABLE reply (absent/malformed/schema-invalid, or
# `objected` with no reason) with a revision still available is a genuine
# choice between two different targets, and that case now asks the router
# instead of inventing a verdict the peer never gave. When the router's
# own budget is spent, THIS entry is what the run takes.
# `tests/test_router.py`'s live-path tests exercise
# `engine.router.decide()` against these exact edges (via
# `transitions.successors("plan_review", ..., peer_plan_review=True)`),
# never the task-015/018 synthetic fixture states, and
# `tests/test_peer_plan_review_gate.py` drives the same edges through a
# real `LoopSupervisor` run.
#
# The guard chosen, `plan_review_revision_bound_reached`, targets `worker`
# -- the SAME target `plan_review_approved` already names. Rationale: if
# the router's retry budget is exhausted without ever recovering a valid
# answer from the peer, that is indistinguishable in effect from "this
# gate has now asked all it is allowed to ask" -- the revision-bound
# guard's own condition -- so proceeding with the plan (never silently
# discarding it, never blocking the run indefinitely on a hat that will
# not answer) is the one fallback consistent with every other exhaustion
# guard already in this table (`stagnation_exhausted`,
# `repeated_blocker_signature`): spend the budget, then move on, rather
# than hang.
#
# Empty otherwise: no other *shipped* state is router-ambiguous today
# (every other real hat still has a hand-written `_step_*` method that
# decides its own guard deterministically) -- this dict exists so a FUTURE
# multi-edge hat can declare its fallback beside its table entry, the same
# way this one now does.
ROUTER_FALLBACK: dict[str, str] = {
    "plan_review": "plan_review_revision_bound_reached",
}


def fallback_guard(state: str) -> str | None:
    """The declared deterministic fallback guard for `state`, or None when
    none is declared (a state with 0 or 1 legal edges never needs one --
    it is resolved without ever reaching the router)."""
    return ROUTER_FALLBACK.get(state)


# ---------------------------------------------------------------------------
# Declared conditions for guards whose NAME is narrower than what they fire
# on (v0.20 requirement B.3, #156, task 019)
# ---------------------------------------------------------------------------
# A guard name is an identifier, not a specification, and one of them has
# outgrown its name: `stagnation_last_approach` was task 012's LAST-approach
# pause and task 018 widened the condition it fires on to EVERY approach
# that progressed, without renaming the guard or adding a row (the edge, the
# target and the resume path are all unchanged -- see the entry's own
# comment above). A reader of the generated table would otherwise have to
# infer that from the name, so the condition is declared HERE, as data, and
# rendered into the generated table block by
# `scripts/render_transition_table.py` -- the same discipline the table
# itself follows: one source, no hand-kept copy in the docs.
#
# Only guards whose name genuinely under-states their condition belong here;
# a guard whose name already says what it fires on needs no entry, and the
# renderer emits nothing for it.
GUARD_CONDITION_NOTES: dict[str, str] = {
    "stagnation_last_approach": (
        "fires on EVERY approach that progressed, not only the last one "
        "(v0.20 requirement B.3, #156, task 018): the run's last approach "
        "stalling three iterations running, OR any earlier approach that "
        "gained a workspace commit or left a `superseded` task behind from "
        "a forced split and then stalled, OR a `superseded` parent's own "
        "split child spending its own `progressBudget` (escalated once per "
        "child, naming it on the `operator_escalation` event's "
        "`exhaustedSplitChild` field) instead of forcing a second split. A "
        "genuinely silent approach with a successor left still takes "
        "`stagnation_exhausted` to `replan` exactly as before, and either "
        "pause resumes into `worker` over the SAME approach."),
}


def guard_condition_note(guard: str) -> str | None:
    """The declared condition note for `guard`, or None when its name
    already states what it fires on."""
    return GUARD_CONDITION_NOTES.get(guard)


# ---------------------------------------------------------------------------
# The task-status mini-FSM the harness advances at transitions (task 019,
# requirement F, #60 pillar 3)
# ---------------------------------------------------------------------------
# A SEPARATE declared table from the step-state TABLE above: this one
# governs one task record's `status` field, not the run's own FSM position.
# Same reading rule as the table above -- "the declared table is what a
# caller checks against, not a second hand-derived copy of the same fact" --
# kept here, beside the per-hat schemas, because a proposed task-status move
# is exactly the kind of hat OUTPUT this module already governs: worker/
# verify report through `HAT_SCHEMAS`, and a `task_status` field on a report
# is the same kind of claim a `verdict` field already is.
#
# The operator's ruling is the spec (PRD section F): "task state changes
# only at step transitions, and only the harness makes them", and target
# lifecycle, from #60:
#
#   vigilant:     pending -> in-progress -> awaiting-validation -> validated
#                                ^               | verify
#                                +-- validation-failed <--+
#   non-vigilant: pending -> in-progress -> completed
#   either:       failed / skipped (terminal dead)
#
# `awaiting-validation` REPLACES `completed` as the worker's claim state in
# vigilant mode (the delta below overwrites `in-progress`'s whole successor
# tuple, it does not add to it) -- `validated` is the verifier-granted
# terminal, and `completed` stays the non-vigilant terminal.
#
# `blocked` (task 021's harness-derived dead-dependency label,
# `engine.state.task_blocked_by()`) is deliberately NOT a status in this
# table: it is never the TARGET of a transition a hat proposes, only a
# read-time annotation the harness computes from a dependency's own status,
# so there is nothing to validate here for it.
#
# `superseded` (v0.20 requirement B.1, #156, tasks 016/019) IS in this
# table, and the distinction from `blocked` is exactly why: it is a real
# TARGET of a real transition the harness takes -- `in-progress ->
# superseded`, written by `engine.state.supersede_task()` the instant a
# task's `progressBudget` is spent and the forced split's children are
# inserted -- so the declared table would be lying by omission without it,
# and every reader that checks a move against the table (`ralphctl doctor`'s
# tasks.json diagnosis, the rendered table/diagram in
# `docs/architecture.md`) would read the engine's own write as an unknown
# status. It is terminal, with no successors in either variant, and it is
# the one declared move NO hat may claim through the `task_status` contract
# channel (`task_status_report_errors` refuses it below) and NO operator may
# rule by hand (`OPERATOR_RULEABLE_TASK_STATUSES` excludes it): the engine
# forces this move itself, at the one moment a progress budget is spent with
# nothing to relieve it.

TASK_STATUSES: tuple[str, ...] = (
    "pending",
    "in-progress",
    "awaiting-validation",
    "validated",
    "validation-failed",
    "completed",
    "failed",
    "skipped",
    "petitioned",
    "superseded",
)

# The statuses `ralphctl amend --status` accepts as an operator ruling:
# every declared status EXCEPT `superseded`, which the engine forces itself
# at a spent progress budget and an operator therefore never rules by hand
# (see the paragraph above and `docs/architecture.md`'s `superseded`
# section). Derived from `TASK_STATUSES` rather than re-typed, so a status
# added to the vocabulary is operator-ruleable by default and only an
# explicit exclusion here narrows it.
ENGINE_FORCED_TASK_STATUSES: tuple[str, ...] = ("superseded",)
OPERATOR_RULEABLE_TASK_STATUSES: tuple[str, ...] = tuple(
    s for s in TASK_STATUSES if s not in ENGINE_FORCED_TASK_STATUSES)

TASK_STATUS_TABLE: dict[str, tuple[str, ...]] = {
    "pending": ("in-progress",),
    # non-vigilant's claim path -- vigilant overrides this entry entirely
    # (see TASK_STATUS_VIGILANT_DELTA below).
    # `superseded` (v0.20 requirement B.1, #156, tasks 016/019) is declared
    # in the BASE table, like `failed -> skipped` below and for the same
    # reason: the engine takes this edge identically in both variants (a
    # spent `progressBudget` is not a verification concern), so the
    # vigilant delta below re-states it rather than dropping it when it
    # overwrites this whole tuple.
    "in-progress": ("completed", "failed", "skipped", "superseded"),
    # a task a worker iteration can still act on (state.TASK_ACTIONABLE_
    # STATUSES) may be judged unmeetable and relabelled `skipped`, or may
    # simply be picked back up -- same rule as `pending`/`in-progress`
    # holds for `validation-failed`.
    "validation-failed": ("in-progress", "failed", "skipped"),
    # Unreachable in the non-vigilant variant: nothing in the base table
    # ever targets these two, they exist only so `TASK_STATUSES`/successors
    # are enumerable for both variants from the one inventory.
    "awaiting-validation": (),
    "validated": (),
    "completed": (),
    # Task 009 (requirement C.1, #123, option a): the sanctioned escape from
    # a permanently `failed` task -- exhausted validation attempts, judged
    # unmeetable, relabelled rather than left as a dead end that blocks
    # `plan_complete` forever. Declared once, here, in the BASE table --
    # the vigilant delta below never overrides this key, so both variants
    # (`task_status_table_for(True)` and `task_status_table_for(False)`)
    # carry it identically, matching the PRD's "both variants" wording.
    # Carried by the SAME `task_status` contract channel every other move
    # in this table already uses -- the harness applies it
    # (`LoopSupervisor._apply_reported_task_status`), the worker never
    # writes `tasks.json` directly. `task_status_report_errors` below adds
    # the one extra semantic condition this specific move carries that no
    # other declared move does: a non-empty `justification` in the same
    # payload, which the harness then writes into the task's own `notes`
    # field (never into `validationNotes`/`unsatisfiableReason` -- those
    # are separate, pre-existing fields with their own writers). The
    # caveat stands and stays documented, unchanged by this move existing
    # at all: `skipped` is not in `TASK_DEPENDENCY_SATISFIED_STATUSES`, so
    # relabelling a `failed` task never un-dooms whatever depends on it.
    "failed": ("skipped",),
    "skipped": (),
    # Task 009 (requirement C.1, #114): `petitioned` is likewise reachable
    # only via the vigilant delta below (`_verify_task`'s exhaustion branch
    # -- the only writer -- runs exclusively inside the vigilant verify
    # sub-cycle); no move OUT of it is declared here yet (that is task
    # 010's `plan_complete`/REFUTED-routing scope), so it is enumerable
    # from the base table with no successors, exactly like the other
    # terminal-shaped entries above.
    "petitioned": (),
    # v0.20 requirement B.1 (#156, tasks 016/019): terminal, with no
    # successors in either variant -- the work a `superseded` parent named
    # moved to the split children `state.insert_worker_split_tasks` created
    # in the same write, so nothing ever moves the parent again.
    # `state.TASK_TERMINAL_STATUSES` and `TASK_DEPENDENCY_SATISFIED_
    # STATUSES` say the same thing from the scheduler's side.
    "superseded": (),
}

TASK_STATUS_VIGILANT_DELTA: dict[str, tuple[str, ...]] = {
    # replaces the non-vigilant claim path: a vigilant worker's completed
    # claim is not trusted on its own word, it waits for verify. The
    # engine-forced `superseded` edge is RE-STATED here, unchanged, because
    # this entry overwrites the base table's whole tuple: a spent
    # `progressBudget` is not a verification concern and forces the split
    # identically in both variants (v0.20 requirement B.1, #156).
    "in-progress": ("awaiting-validation", "failed", "skipped",
                    "superseded"),
    # verify's own verdict: `validated` is the vigilant terminal, a
    # negative verdict loops to `validation-failed` for a retry, and an
    # exhausted validation budget (`state.VALIDATION_ATTEMPT_LIMIT`) may
    # force straight to `failed` -- or, when the contract carries a
    # petition protocol (task 009, requirement C.1, #114), to `petitioned`
    # instead -- from here in one disk write (`_verify_task`'s own
    # sequencing: `validation-failed` is set in memory first, then
    # overwritten to `failed`/`petitioned` before the single write lands).
    "awaiting-validation": ("validated", "validation-failed", "failed",
                            "petitioned"),
}


def task_status_table_for(vigilant: bool) -> dict[str, tuple[str, ...]]:
    """Compose `TASK_STATUS_TABLE` with `TASK_STATUS_VIGILANT_DELTA` -- the
    ONLY function that produces a variant's task-status table, mirroring
    `table_for` above."""
    table = dict(TASK_STATUS_TABLE)
    if vigilant:
        table.update(TASK_STATUS_VIGILANT_DELTA)
    return table


def legal_next_task_statuses(status: str, vigilant: bool) -> tuple[str, ...]:
    """Every legal next status for a task currently at `status`, for the
    given variant. Always returns a tuple (possibly empty), mirroring
    `successors()` above."""
    return task_status_table_for(vigilant).get(status, ())


def is_legal_task_status_transition(current: str, target: str, vigilant: bool) -> bool:
    """True iff moving a task from `current` to `target` is legal in this
    variant. A no-op (`current == target`) is always legal -- it is not a
    move at all, and a hat re-stating the status it already holds is not a
    violation of anything."""
    if current == target:
        return True
    return target in legal_next_task_statuses(current, vigilant)


def task_status_report_errors(payload: Any, current_status: str, vigilant: bool) -> list[str]:
    """The semantic check layered ON TOP OF a hat's schema validation
    (`contracts.validate_payload`, the only SCHEMA validator, fact 16/#65 --
    this is not a second one of those: it is a business-rule check of the
    same KIND as `plan_ingestion`'s semantic lint over an already
    schema-valid plan proposal).

    A payload carrying no `task_status` field proposes no task-state change
    at all and is never in violation here -- exactly the in-step case the
    PRD's operator ruling describes ("in-step there is no task-state change
    at all"). Only once a hat's OUTPUT actually proposes a move is there
    anything for the declared table above to accept or refuse.
    """
    if not isinstance(payload, dict):
        return []
    target = payload.get("task_status")
    if target is None:
        return []
    if not is_legal_task_status_transition(current_status, target, vigilant):
        variant = "vigilant" if vigilant else "non-vigilant"
        return [f"task_status: {current_status!r} -> {target!r} is not a "
                f"legal transition in the declared {variant} task-status "
                "table"]
    # Task 009 (requirement C.1, #123): the one declared move that carries
    # an extra condition beyond "is this edge in the table" -- relabelling
    # a permanently `failed` task `skipped` must explain itself. A real
    # no-op report (`current_status == target`) is caught by `is_legal_
    # task_status_transition`'s own equality bypass before this function
    # is even reached from most call sites, but this check is written
    # against `current_status == "failed" and target == "skipped"` (not
    # `!=`) so it also fires correctly for the one caller
    # (`_apply_reported_task_status`) that calls this on every report
    # regardless of whether it turns out to be a no-op.
    # v0.20 requirement B.1 (#156, tasks 016/019): `in-progress ->
    # superseded` is a declared edge above because the ENGINE really takes
    # it (`state.supersede_task`, the one writer, at a spent
    # `progressBudget`) -- and it is the one declared move no HAT may claim
    # through this channel. Refused here, in the semantic layer, so the
    # declared table stays an honest inventory of what the engine does
    # without widening the vocabulary a report may carry.
    if target == "superseded" and current_status != target:
        return ["task_status: 'superseded' is applied by the harness itself "
                "at a forced split (a spent progressBudget); a hat never "
                "reports it -- carry the split in `proposed_tasks` instead"]
    if current_status == "failed" and target == "skipped":
        justification = payload.get("justification")
        if not isinstance(justification, str) or not justification.strip():
            return ["task_status: 'failed' -> 'skipped' requires a "
                    "non-empty justification"]
    return []


# ---------------------------------------------------------------------------
# The runtime primitive that walks this table (task 014, requirement B)
# ---------------------------------------------------------------------------
# "One module holds the table and nothing else holds transition logic"
# (requirement B's acceptance): the executor that refuses an edge the table
# above does not list lives HERE, beside the table it enforces, rather than
# in a second module. `engine.loop`'s step handlers (`_step_planning`,
# `_step_worker`, `_step_review`, `_step_replan`) compute the guard that
# fired and call `LoopSupervisor._advance`, which delegates to exactly this
# class -- nothing in `loop.py` re-implements `is_legal`'s check.


class IllegalTransition(RuntimeError):
    """Raised when a caller asks the executor to move to a target state the
    declared table does not list as a legal successor of the current state,
    for the variant in force. The refusal happens HERE -- inside the one
    runtime primitive every state move must pass through -- not by a test
    reading `TABLE`/`VIGILANT_DELTA` back and asserting a property about
    them (requirement B's acceptance: "by driving an illegal one and seeing
    it refused, not by reading the table back").
    """

    def __init__(self, state: str, target: str, guard: str, vigilant: bool) -> None:
        self.state = state
        self.target = target
        self.guard = guard
        self.vigilant = vigilant
        variant = "vigilant" if vigilant else "non-vigilant"
        super().__init__(
            f"illegal transition: {state!r} -> {target!r} (guard={guard!r}, "
            f"{variant} variant) is not in the declared table"
        )


class TransitionExecutor:
    """Advances a run's FSM position by exactly one table-declared edge at a
    time. `vigilant` selects the table variant (`table_for`) -- the ONLY
    difference between the two modes' behaviour here (requirement B: "the
    two modes differ only by which table variant is selected"); there is no
    second, hand-written non-vigilant executor anywhere.

    `peer_plan_review` (task 018, requirement K, #101) is a SECOND,
    independent selector on its own axis, defaulting to `False` so every
    pre-existing construction site (`TransitionExecutor(cfg.vigilant)`)
    keeps behaving exactly as before.

    `peer_verdict_gate` (task 020, requirement L, #101) is a THIRD,
    independent selector on its own axis, same shape and same default.
    """

    __slots__ = ("vigilant", "peer_plan_review", "peer_verdict_gate")

    def __init__(self, vigilant: bool, peer_plan_review: bool = False,
                 peer_verdict_gate: bool = False) -> None:
        self.vigilant = vigilant
        self.peer_plan_review = peer_plan_review
        self.peer_verdict_gate = peer_verdict_gate

    def advance(self, state: str, target: str, guard: str) -> str:
        """Move from `state` to `target` via the named `guard`'s edge, or
        raise `IllegalTransition`. Returns `target` on success so a caller
        can write `state = executor.advance(state, target, guard)`."""
        if not is_legal(state, target, self.vigilant, self.peer_plan_review,
                        self.peer_verdict_gate):
            raise IllegalTransition(state, target, guard, self.vigilant)
        return target


# ---------------------------------------------------------------------------
# Response-contract schemas — one per hat, declared beside the mapping above
# ---------------------------------------------------------------------------
# Task 009 (requirement C, #60 pillar 1). Fact 4: `pi` has no
# `--response-format`/`--schema`/tool-forcing, so nothing enforces a hat's
# final output shape except the harness reading it back — this dict is what
# `ralphd.engine.contracts.validate_payload` (the *only* validator code path,
# see that module's docstring and fact 16/#65's lesson) validates against.
# These are a subset of JSON Schema: "type", "properties", "required",
# "additionalProperties" and per-property "enum" — exactly what
# `contracts.validate_payload` implements, nothing more.
#
# Per #60's decided channel split: worker/verify report small verdicts
# inline in the final assistant text (COMPLETE/VERIFIED sentinels become
# schema fields here); planning (and, per the PRD, plausibly review) is
# file-borne — the hat drafts `plan-proposal.json` in its scratch area and
# the *schema below* covers only the pointer-plus-summary its final output
# carries, not the proposal file's own shape (task 011's business, and its
# own schema — the plan schema explicitly has no `status` field, per
# requirement C).
HAT_SCHEMAS: dict[str, dict] = {
    "planning": {
        "type": "object",
        "required": ["proposal_path", "summary"],
        "properties": {
            # Pointer to the file-borne plan-proposal.json (task 011 reads
            # and lints it); never the plan content itself.
            "proposal_path": {"type": "string"},
            "summary": {"type": "string"},
            # Task 107 (#60 pillar 3): the notes' `## Standing rules` block
            # content, supplied ONCE through this contract instead of
            # trusting worker.md's "preserve verbatim" convention.
            # Deliberately OPTIONAL, not required: an older stub/test that
            # never supplies it keeps validating exactly as before (no
            # `standing_rules` -> `_step_planning` pins nothing ->
            # `_reinject_standing_rules` is a no-op every iteration,
            # unchanged behaviour), while a planner that DOES supply it
            # gets it pinned and re-injected after every iteration
            # (`RunDir.write_standing_rules` / `state.reinject_standing_rules`).
            "standing_rules": {"type": "string"},
        },
        "additionalProperties": False,
    },
    "worker": {
        "type": "object",
        "required": ["verdict"],
        "properties": {
            # Replaces the bare COMPLETE sentinel (ledger guard 8) with a
            # field the harness reads instead of scanning free text.
            # Task 025 (requirement H, #63) adds `"unsatisfiable"`: a worker
            # that finds a task's criteria cannot be satisfied AS WRITTEN
            # reports it here, with `task_id` and `reason` (semantic
            # check -- both non-empty -- is `worker_unsatisfiable_report_
            # errors` below, the same two-layer split `task_status_report_
            # errors` already uses for task 019's plain status reports).
            # Task 015 (requirement O, #100) adds `"plan_complete"`: the
            # LAST untyped claim `<promise>COMPLETE</promise>` used to make
            # -- "nothing was assigned to me and every task is already
            # terminal" -- is now a contract value too, deliberately NOT
            # named `"complete"` (already taken by the PER-TASK claim just
            # above) so a payload can never be misread as one or the other.
            # Valid only when the harness assigned no task this iteration
            # (`LoopSupervisor._apply_worker_plan_complete_report`, the
            # structural half) and cross-checked against `_outstanding_
            # for_completion()` exactly as `honoured_complete` cross-checked
            # the retired marker (the semantic half) -- a false claim is
            # refused on the same evidence, and a malformed attempt reaches
            # `contract_enforcement.enforce_contract`'s re-ask ladder
            # instead of reading as a silent no-progress iteration.
            # v0.20 task 021 (PRD requirement C.1, #156) adds
            # `"deferred"`: a task that must wait (a gated time, a
            # dependency that is not yet ripe in practice even though the
            # scheduler's own `dependsOn` graph does not say so) moves
            # behind every runnable task instead of absorbing further
            # iterations or tripping the stagnation guard. Carries `until`
            # (an ISO-8601 timestamp, checked below by `worker_deferred_
            # report_errors` -- `contracts.py`'s restricted schema engine
            # has no date-format keyword) and reuses the existing `reason`
            # property just below rather than adding a second one. Applies
            # to the task ASSIGNED this iteration -- there is no separate
            # `task_id` field for it, unlike `"unsatisfiable"`, because a
            # worker can only defer the one task the harness handed it.
            "verdict": {"type": "string",
                        "enum": ["complete", "no_verdict", "unsatisfiable",
                                 "plan_complete", "deferred"]},
            "task_id": {"type": "string"},
            "reason": {"type": "string"},
            "until": {"type": "string"},
            "notes": {"type": "string"},
            # Task 021 (requirement N, #102): an OPTIONAL split proposal --
            # "the assigned task is too big; here is how I'd break it up" --
            # scoped deliberately narrower than the planner's own proposal
            # channel (`HAT_SCHEMAS["planning"]`'s `proposal_path`, a
            # pointer to a FILE): this one travels inline, in the same
            # payload as `verdict`, because it is always small (a split of
            # ONE task, never a whole plan) and always accompanies an
            # ordinary verdict rather than replacing one. Only the TYPE is
            # checked here (`contracts.py`'s schema engine has no `items`
            # sub-schema for array elements, by design -- see that
            # module's docstring); the actual shape/semantic lint is
            # `plan_ingestion.lint_worker_split` below, deliberately the
            # SAME three functions (`validate_proposal_shape`,
            # `lint_proposal`, `lint_own_terminal_state`) `_parse_proposal`
            # already runs for the planner's file-borne proposal, reused
            # rather than re-derived (fact 16/#65's lesson) --
            # `LoopSupervisor._apply_worker_proposed_tasks` (`engine/
            # loop.py`) is the one call site. Restricted to a SPLIT of the
            # task actually assigned this iteration (never unbounded new
            # scope -- that is what the PRD and the peer plan gate exist to
            # constrain instead), budgeted per approach
            # (`JobConfig.worker_split_budget_per_approach`), and every
            # accepted task is stamped `origin: "worker"` -- see
            # `state.TASK_ORIGIN_WORKER`/`state.insert_worker_split_tasks`,
            # the same "always stamp, never trust the caller" discipline
            # `state.steer_task_insert` already applies for `origin:
            # "steer"` (requirement I). A worker whose ONLY activity across
            # three iterations is proposing splits still fails the
            # 3-iteration stagnation guard -- proposing is deliberately
            # excluded from what counts as "progress" there
            # (`state.tasks_doc_excluding_new_entries`,
            # `tests/test_worker_split_proposal.py`'s stagnation pin) --
            # there is no escape hatch through this field, the same hazard
            # v0.9 requirement H had to pin for "unsatisfiable" and pinned
            # the same way.
            "proposed_tasks": {"type": "array"},
            # Task 011 (requirement C.1, #123): the two fields worker.md's
            # failed-task escape actually instructs -- `task_status` (the
            # target status for the task named by `task_id`) and
            # `justification` (required by `task_status_report_errors`
            # above for the one declared `failed -> skipped` move). Task
            # 010 rewrote worker.md to route that escape through THIS
            # contract channel instead of a direct `tasks.json` write, but
            # this schema still had `additionalProperties: False` and no
            # such properties -- so the very payload the shipped prompt
            # asks for was refused as `unexpected field(s)
            # ['justification', 'task_status']` and the escape was
            # unreachable in production. Declared OPTIONAL and
            # deliberately independent of `verdict` (a status report rides
            # ALONGSIDE the ordinary verdict, exactly like `petition` and
            # `proposed_tasks` above -- worker.md: "carry ... alongside
            # your ordinary verdict in the same JSON reply"). `type`
            # `string` with no enum: WHICH moves are legal is the declared
            # task-status table's business (`task_status_table_for`,
            # applied per-task against its CURRENT status by
            # `LoopSupervisor._apply_reported_task_status`), never a
            # second, drifting copy of that table pinned here.
            "task_status": {"type": "string"},
            "justification": {"type": "string"},
            # Task 007 (requirement A.3 prompt half, #155): an OPTIONAL
            # side channel, independent of `verdict` exactly like
            # `proposed_tasks`/`task_status`/`petition` above -- a worker
            # MAY, while on its own assigned task, resolve a debt ledger
            # item (`state.DEBT_JSON_FILE`, task 006) when doing so costs
            # minutes and stays inside its own task's files, and names
            # which one(s) here by `id` (`"<taskId>/<n>"`, the shape
            # `record_debt_entries` mints). Only the outer TYPE is checked
            # here -- `contracts.py`'s restricted schema engine has no
            # `items` sub-schema for array elements, by design, the same
            # as every other optional array field in this schema. Applied
            # by `LoopSupervisor._apply_worker_debt_resolution`
            # (`state.resolve_debt_entries` -> `RunDir.resolve_debt`),
            # stamping `resolvedBy` with the iteration's own commit sha --
            # an id this run's ledger does not recognise, or one already
            # resolved, is silently ignored rather than rejected, since
            # this report is advisory, never authoritative.
            "debt_resolved": {"type": "array"},
            # Task 004 (requirement A.1, #114): an OPTIONAL structured
            # impossibility declaration -- a worker names the obligation
            # (a requirement letter, a DoD item, or its own task's
            # criterion clause), the petition TYPE (one of `petitions.
            # PETITION_TYPES`, `self-referential` joining the original
            # four this same task adds) and the EVIDENCE, and the ENGINE
            # -- never the worker -- writes/validates the filing
            # (`petitions.file_worker_petition`, the schema/semantic split
            # every sibling channel in this module already uses; the
            # semantic half is `worker_petition_report_errors` below).
            # Deliberately independent of `verdict`: a worker may declare
            # a petition alongside ANY verdict value (including
            # "no_verdict" -- still working on the rest of the task while
            # flagging one obligation as unachievable), the same way
            # `proposed_tasks` above is a side channel rather than a
            # verdict value of its own.
            "petition": {
                "type": "object",
                "required": ["obligation", "type", "evidence"],
                "properties": {
                    "obligation": {"type": "string"},
                    "type": {"type": "string",
                              "enum": list(petitions.PETITION_TYPES)},
                    "evidence": {"type": "string"},
                },
                "additionalProperties": False,
            },
            # Task 011 (PRD requirement A.1, #156): the worker contract's
            # OPTIONAL "still working" claim -- "the worker claims it, the
            # engine corroborates it" (PRD requirement A's own framing) --
            # rides alongside `verdict: "no_verdict"` ONLY (PRD: "a
            # progress block on any other verdict is ignored and logged",
            # `LoopSupervisor._step_worker`'s dedicated site is what does
            # the ignoring/logging -- this schema entry only fixes the
            # SHAPE, not which verdict it may accompany). `evidence` is a
            # bare `{"type": "array"}` for the same reason
            # `proposed_tasks`/`debt_resolved` above are -- this restricted
            # schema engine has no `items` sub-schema for array elements,
            # by design (`contracts.py`'s own docstring) -- so each
            # element's `kind` (one of "commit"/"file"/"external", PRD
            # requirement A.1) is checked by the semantic half instead,
            # `worker_progress_report_errors` below, the same two-layer
            # split every sibling optional channel in this schema already
            # uses. Nothing here yet reads an ACCEPTED claim for anything
            # beyond a log record -- fingerprint corroboration (task 012)
            # and the per-task budget (task 013) are what will consume it.
            "progress": {
                "type": "object",
                "required": ["summary", "evidence"],
                "properties": {
                    "summary": {"type": "string"},
                    "evidence": {"type": "array"},
                },
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    },
    "verify": {
        "type": "object",
        "required": ["task_id", "verdict"],
        "properties": {
            "task_id": {"type": "string"},
            "verdict": {"type": "string", "enum": ["verified", "rejected"]},
            "reason": {"type": "string"},
            # Task 005 (requirement A.2, #154): a `verified` verdict may
            # carry structured notes instead of (or beside) a plain
            # `reason` string -- form/wording/provenance/process clauses
            # task-verify.md now tells the verifier to "verify with a
            # note" rather than reject over. Each element is expected to
            # be a `{clause, observed, suggested}` object, but (like
            # `HAT_SCHEMAS["review"]`'s `petition_adjudications` and
            # `HAT_SCHEMAS["worker"]`'s `proposed_tasks`) only the outer
            # TYPE is checked here -- `contracts.py`'s restricted schema
            # engine has no `items` sub-schema for array elements, by
            # design. `_verify_task` (engine/loop.py) persists this list
            # verbatim onto the task record as `verifyNotes`, in the same
            # write that sets `validated`; task 006 is what turns each
            # entry into a `debt.json` ledger row.
            "notes": {"type": "array"},
        },
        "additionalProperties": False,
    },
    "review": {
        "type": "object",
        "required": ["verdict"],
        "properties": {
            # Replaces the bare VERIFIED sentinel (review.md) with a field.
            "verdict": {"type": "string", "enum": ["verified", "rejected"]},
            "reason": {"type": "string"},
            # Optional file-borne findings, same channel shape as planning.
            "findings_path": {"type": "string"},
            # Task 010 (requirement C, #112): review's ruling on every OPEN
            # impossibility petition `engine.petitions.petitions_review_
            # context` surfaced this pass -- one entry per petition, each
            # naming the obligation and CONFIRMED/REFUTED (a REFUTED entry
            # also carries the remedy, per review.md's own instructions).
            # Only the TYPE is checked here (`contracts.py`'s restricted
            # schema engine has no `items` sub-schema for array elements,
            # by design -- see that module's docstring, and
            # `HAT_SCHEMAS["worker"]`'s `proposed_tasks` for the same
            # shape of optional array field). Deliberately OPTIONAL: a
            # review pass with nothing open (the common case) supplies
            # none, and validates exactly as it always did.
            "petition_adjudications": {"type": "array"},
        },
        "additionalProperties": False,
    },
    "reflect": {
        "type": "object",
        # Task 121 (#60 pillar 3, fact 9): replaces the bare
        # `<promise>REFLECTED</promise>` sentinel (never scored by the
        # engine, `_reflect_failure()` inferred outcome from disk alone)
        # with a claim the harness reads. `report_written`/
        # `suggestions_written` are the two facts the PRD's reflect phase
        # already promises on disk (`artifacts/reflection/report.md`,
        # optionally `suggestions.diff`) -- this schema just makes the
        # agent SAY which of those it actually did, so the harness has a
        # claim to validate against the files instead of only the files
        # themselves.
        "required": ["report_written", "suggestions_written"],
        "properties": {
            "report_written": {"type": "boolean"},
            "suggestions_written": {"type": "boolean"},
            "notes": {"type": "string"},
        },
        "additionalProperties": False,
    },
    "steer": {
        "type": "object",
        # Task 009 (requirement G/I, #98): `settled` is the harness-
        # observable fact the PRD's own vocabulary distinguishes from
        # `done` -- "settled is a harness-observable fact: the hat
        # processed the message and applied its changes. done is the
        # ordinary lifecycle's verdict on the tasks it created." The hat
        # claims only the former here; the latter is judged later, by the
        # ordinary worker/verify/review path over whatever it inserted.
        "required": ["settled"],
        "properties": {
            "settled": {"type": "boolean"},
            "notes": {"type": "string"},
            # Task 034 (operator-inserted): the hat's OPTIONAL self-report
            # of how much of this iteration's steering it actually applied,
            # recorded per steering file in the run's steering ledger
            # (`state.record_steering_settlement`, rendered into every
            # following iteration's prompt by
            # `LoopSupervisor._steering_ledger_context`). Optional on
            # purpose: a hat that says nothing here keeps validating
            # exactly as before, and the ledger derives `applied` from a
            # `settled: true` claim and `partially-applied` from a
            # `settled: false` one. `pending` is deliberately absent from
            # the enum -- "pending" is the ABSENCE of a settlement record,
            # never something a hat that just processed a message may claim
            # about it.
            "application": {"type": "string",
                            "enum": ["applied", "partially-applied"]},
        },
        "additionalProperties": False,
    },
    # Task 018 (requirement K, #101): the peer plan-review hat's own
    # contract -- `verdict: "approved"` (the plan proceeds to `worker`) or
    # `verdict: "objected"` with a mandatory `reason` (checked by
    # `plan_review_report_errors` below, the same split every sibling
    # semantic check in this module already uses) routed back to
    # `planning` for a bounded revision. Deliberately the SAME two-value
    # shape `review`'s `verdict` enum uses (`verified`/`rejected`) rather
    # than reusing those exact strings -- `plan_review` is a judgement
    # about a PLAN, `review` is a judgement about a completed APPROACH,
    # and the two must never be confusable by a payload that named the
    # wrong hat's enum value by mistake.
    "plan_review": {
        "type": "object",
        "required": ["verdict"],
        "properties": {
            "verdict": {"type": "string", "enum": ["approved", "objected"]},
            "reason": {"type": "string"},
            "notes": {"type": "string"},
        },
        "additionalProperties": False,
    },
    # Task 020 (requirement L, #101): the peer VERDICT gate's own contract
    # -- `verdict: "verified"` (this second, independent, BLIND judgement
    # agrees with `review`'s own -- the run may reach terminal success) or
    # `verdict: "rejected"` with a mandatory `reason` (checked by
    # `verdict_gate_report_errors` below, the same split every sibling
    # semantic check in this module already uses) -- routed to a fresh
    # approach (`replan`), never straight back to `worker`: see this
    # task's commit message and `artifacts/reports/task-020-requirement-l-
    # rejection-semantics.md` for the decided rejection semantics (a NEW
    # approach, not a targeted worker fix-up) and who re-runs review (the
    # new approach's own ordinary worker -> review pass, automatically --
    # nothing bespoke). Deliberately REUSES `review`'s own `verified`/
    # `rejected` vocabulary, unlike `plan_review`'s `approved`/`objected`
    # -- this hat judges the SAME kind of thing `review` does (a completed
    # approach), just blind and a second time, so the same two words are
    # the honest ones to reuse.
    "verdict_gate": {
        "type": "object",
        "required": ["verdict"],
        "properties": {
            "verdict": {"type": "string", "enum": ["verified", "rejected"]},
            "reason": {"type": "string"},
            # Same optional file-borne findings channel review's own
            # schema already uses (findings_path) -- a rejected verdict's
            # OWN findings, written to the gate's OWN fixed path
            # (RunDir.verdict_gate_findings_file, verdict-gate-
            # findings.md, task 019 requirement D.4 #125 -- deliberately
            # NOT review's own review-findings.md, so the two
            # independent judges' findings can never overwrite one
            # another) so the existing _archive_approach/_write_
            # composite_prd machinery folds them into the next approach's
            # composite PRD, with authorship attributed, exactly the way
            # review's own rejection findings already are -- no second
            # mechanism, no engine-side change needed beyond calling the
            # same two methods.
            "findings_path": {"type": "string"},
            "notes": {"type": "string"},
        },
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# GH #209 defect 2 (Half B): the one tolerance layered on top of the
# `["verified", "rejected"]` enum above -- placed here, beside the enum it
# is a tolerance for, so the two can never drift apart.
# ---------------------------------------------------------------------------

NEGATIVE_VERDICT_SYNONYMS: dict[str, str] = {"fail": "rejected",
                                             "failed": "rejected"}


def normalize_negative_verdict(payload: dict) -> str | None:
    """Rewrite `payload["verdict"]` IN PLACE from one of the closed set of
    words a judging hat has been observed to say when it plainly meant this
    contract's own NEGATIVE verdict, and return the literal word that was
    replaced -- `None` when nothing was rewritten (every compliant payload,
    and every non-`str`/absent `verdict`).

    GH #209 defect 2 exists because one of the reference run's three review
    agent-faults was nothing but this: `$.verdict: 'fail' is not one of
    ['verified', 'rejected']` refused three times over, then scored as a
    substantive rejection that archived the approach. A reviewer that
    plainly meant "fail" cost an approach.

    Three decisions worth stating here rather than re-deriving:

    * ONE-DIRECTIONAL by construction. There is deliberately no `pass`/
      `passed` -> `verified` entry: no word in this map may be a route
      STRAIGHT to a terminal SUCCESS (`verified` is the one word
      `_step_review` turns into `_finish("succeeded", ...)`), so a wrong
      guess here can only ever land on the negative word.
      Precisely, and worth stating because the shorter claim is not true:
      what the rewritten payload then receives is exactly the treatment a
      LITERAL `rejected` receives, which includes `_review_verdict`'s own
      recorded correction of an over-cautious refusal -- a `rejected` claim
      whose structured findings are ALL `severity: "residual"` is
      recomputed to `verified` (`review.verdict_recomputed`, task 002's
      DoD predicate 3). So a hat that writes `fail` while filing only
      residual findings can still reach `succeeded`, by that route, on its
      own findings, exactly as it would have with the compliant word. That
      is the point of the map rather than a hole in it: making `fail`
      BINDING where `rejected` is correctable would give the contract a
      second, undeclared verdict vocabulary in which the illegal word
      outranks the legal one.
    * This does NOT re-open #92's requirement F2 decision (`ff869a4`,
      recorded again at docs/roadmap.md's v0.13 block), which refused TYPE
      coercion of an arbitrary scalar inside `contracts.validate_payload`/
      `contract_enforcement.parse_response` on the grounds that
      "normalising one scalar field repairs one line, and the next
      violation would be a different field". Both of those functions are
      untouched -- `validate_payload` still rejects `{"verdict": "fail"}`
      outright -- and this runs strictly outside them, on a closed
      two-entry map, over ONE field whose legal values are a two-word enum
      the hat's own prompt (`prompts/review.md`) already states verbatim.
      The shape copied is `plan_ingestion.normalize_task_tiers` (normalise
      in place, report what changed, never reject), not a schema coercion.
    * `hat`-agnostic on purpose: it touches only the `verdict` key, so
      `_verify_contract_ladder` and `_verdict_gate_verdict` (same
      `["verified", "rejected"]` enum) can adopt it with no second copy.
      `plan_review`'s deliberately non-interchangeable `approved`/
      `objected` vocabulary is out of its reach for the same reason.
      Exactly one caller today: `LoopSupervisor._review_contract_ladder`.
    """
    verdict = payload.get("verdict")
    if not isinstance(verdict, str):
        return None
    said = verdict.strip().lower()
    replacement = NEGATIVE_VERDICT_SYNONYMS.get(said)
    if replacement is None or verdict == replacement:
        return None
    payload["verdict"] = replacement
    return verdict


# ---------------------------------------------------------------------------
# Task 014 (requirement F.1, #144): a `midRunAdjudication: true` review
# episode's own answer shape -- deliberately NOT an entry in `HAT_SCHEMAS`
# (it names no hat of its own; the episode still runs `run_iteration(
# "review", ...)`, the review hat's own model/prompt) and NOT wired through
# `contracts.validate_payload`, which only ever resolves a *hat* name to
# `HAT_SCHEMAS[hat]` (fact 16/#65's one schema validator, keyed by hat).
# Validated instead through `contracts.validate_against(MIDRUN_REVIEW_
# SCHEMA, payload)` -- the same generic entry point `engine.router`'s own
# answer shape already uses for a shape with no `HATS` entry.
#
# The ordinary `review` schema above requires `verdict` and restricts it to
# `"verified"`/`"rejected"` -- exactly right for a run-ending review, and
# left byte-for-byte unchanged here (the safe default at ladder exhaustion
# is the PRD's own explicit "unchanged" for this requirement). A bounded,
# off-budget adjudication episode is judging PETITIONS, not the approach --
# its `verdict` field is required by the ordinary contract but "has NO
# terminal effect" (`LoopSupervisor._midrun_adjudication_extra`'s own
# prompt text) -- so this schema makes `verdict` OPTIONAL and widens its
# enum with a third, legal value, `"adjudicated"`, naming exactly what the
# episode's reply is answering when it has nothing terminal to claim. A
# reply that omits `verdict` entirely, or spells it `"adjudicated"`, is
# schema-valid here and therefore never reaches `contract_enforcement.
# enforce_contract`'s retry ladder at all -- no attempt is burned, no
# `contract.agent_fault` is emitted -- while the exact same reply against
# the ordinary `review` schema above still fails it, unchanged.
MIDRUN_REVIEW_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string",
                    "enum": ["verified", "rejected", "adjudicated"]},
        "reason": {"type": "string"},
        "findings_path": {"type": "string"},
        "petition_adjudications": {"type": "array"},
    },
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Task 025 (requirement H, #63): the worker's "criteria cannot be satisfied
# as written" report -- the semantic half layered ON TOP OF a hat's schema
# validation (`contracts.validate_payload`, the only SCHEMA validator, fact
# 16/#65), same split `task_status_report_errors` above already uses. A
# `verdict: "unsatisfiable"` claim with no `task_id` or no `reason` names
# nothing a reviewer or a later worker iteration could act on -- it is not a
# report at all, and `_step_worker` treats a non-empty return here exactly
# like a schema violation (a contract violation, ending the run loudly
# rather than silently taking the claim anyway).
# ---------------------------------------------------------------------------


def worker_unsatisfiable_report_errors(payload: Any) -> list[str]:
    """Errors in a worker's `verdict: "unsatisfiable"` report, or an empty
    list when the report is well-formed. A payload that does not claim
    `verdict == "unsatisfiable"` at all is never in violation here -- exactly
    the same "nothing proposed, nothing to check" shape
    `task_status_report_errors` uses for a payload with no `task_status`.
    """
    if not isinstance(payload, dict) or payload.get("verdict") != "unsatisfiable":
        return []
    errors = []
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        errors.append(
            "verdict 'unsatisfiable' requires a non-empty task_id")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        errors.append(
            "verdict 'unsatisfiable' requires a non-empty reason")
    return errors


# ---------------------------------------------------------------------------
# v0.20 task 021 (PRD requirement C.1, #156): the semantic half of a
# worker's `verdict: "deferred"` report -- same split as `worker_
# unsatisfiable_report_errors` just above (schema shape -- both fields are
# strings -- is `contracts.validate_payload`'s job; this is the business
# rule). `until` must be a non-empty, PARSEABLE ISO-8601 timestamp --
# `contracts.py`'s restricted schema engine has no date-format keyword, so
# a malformed timestamp string still passes schema validation and must be
# caught here instead -- and `reason` (the shared top-level property) must
# be non-empty, same rule `worker_unsatisfiable_report_errors` already
# applies to its own `reason`.
# ---------------------------------------------------------------------------


def worker_deferred_report_errors(payload: Any) -> list[str]:
    """Errors in a worker's `verdict: "deferred"` report, or an empty list
    when the report is well-formed. A payload that does not claim
    `verdict == "deferred"` at all is never in violation here -- the same
    "nothing proposed, nothing to check" shape every sibling check in this
    module uses.
    """
    if not isinstance(payload, dict) or payload.get("verdict") != "deferred":
        return []
    errors = []
    until = payload.get("until")
    if not isinstance(until, str) or not until.strip():
        errors.append("verdict 'deferred' requires a non-empty ISO-8601 'until'")
    else:
        try:
            datetime.fromisoformat(until.strip().replace("Z", "+00:00"))
        except ValueError:
            errors.append(
                "verdict 'deferred' requires 'until' to be a valid "
                f"ISO-8601 timestamp, got {until!r}")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        errors.append("verdict 'deferred' requires a non-empty reason")
    return errors


# ---------------------------------------------------------------------------
# Task 004 (requirement A.1, #114): the semantic half of the worker's
# optional structured impossibility declaration -- the sibling split every
# other channel in this module already uses. `HAT_SCHEMAS["worker"]
# ["properties"]["petition"]` already enforces the object SHAPE (all three
# fields present, all strings, `type` one of `petitions.PETITION_TYPES`);
# this catches what that restricted schema engine cannot -- a field
# present but blank/whitespace-only. Writing/validating the filing itself
# (duplicate detection, appending to `<wave>-petitions.md`) is
# `petitions.file_worker_petition`'s job, the ENGINE's own write path,
# never this module's (this module owns the contract, not the write).
# ---------------------------------------------------------------------------


def worker_petition_report_errors(payload: Any) -> list[str]:
    """Errors in a worker's optional `payload["petition"]` declaration, or
    an empty list when there is none, or when the one present is
    well-formed. A payload carrying no `petition` key at all is never in
    violation here -- the same "nothing declared, nothing to check" shape
    `worker_unsatisfiable_report_errors` above uses for a payload with no
    `verdict: "unsatisfiable"`.
    """
    if not isinstance(payload, dict):
        return []
    petition = payload.get("petition")
    if not isinstance(petition, dict):
        return []
    errors = []
    obligation = petition.get("obligation")
    if not isinstance(obligation, str) or not obligation.strip():
        errors.append("petition.obligation must be a non-empty string")
    evidence = petition.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        errors.append("petition.evidence must be a non-empty string")
    return errors


# ---------------------------------------------------------------------------
# Task 011 (PRD requirement A.1, #156): the semantic half of the worker's
# optional "still working" progress claim -- the same schema/semantic split
# every sibling optional channel in this module already uses.
# `HAT_SCHEMAS["worker"]["properties"]["progress"]` already enforces the
# OUTER shape (`summary`/`evidence` present, `summary` a string, `evidence`
# an array); this catches what that restricted schema engine cannot -- a
# blank `summary`, an empty `evidence` list, and each evidence element's own
# shape (`kind` one of "commit"/"file"/"external", a non-empty `ref`) --
# `contracts.py` has no `items` sub-schema for array elements, by design.
# Deliberately independent of `verdict`: this function only checks the
# `progress` block's own SHAPE, never whether the accompanying `verdict` is
# `"no_verdict"` -- that gate ("ignored and logged" for any other verdict,
# PRD requirement A.1) is `LoopSupervisor._step_worker`'s own site, not a
# shape violation this function should ever raise over.
# ---------------------------------------------------------------------------


def worker_progress_report_errors(payload: Any) -> list[str]:
    """Errors in a worker's optional `payload["progress"]` claim, or an
    empty list when there is none, or when the one present is well-formed.
    A payload carrying no `progress` key at all is never in violation here
    -- the same "nothing declared, nothing to check" shape
    `worker_petition_report_errors` above uses for a payload with no
    `petition`.
    """
    if not isinstance(payload, dict):
        return []
    progress = payload.get("progress")
    if not isinstance(progress, dict):
        return []
    errors = []
    summary = progress.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("progress.summary must be a non-empty string")
    evidence = progress.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        errors.append("progress.evidence must be a non-empty array")
        return errors
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            errors.append(f"progress.evidence[{index}] must be an object")
            continue
        kind = item.get("kind")
        if kind not in ("commit", "file", "external"):
            errors.append(
                f"progress.evidence[{index}].kind must be one of "
                "'commit', 'file', 'external', got " + repr(kind))
        ref = item.get("ref")
        if not isinstance(ref, str) or not ref.strip():
            errors.append(f"progress.evidence[{index}].ref must be a non-empty string")
    return errors


# ---------------------------------------------------------------------------
# Task 116 (#60 pillar 3, fact 9): the sibling semantic check for the
# worker's ORDINARY per-task completion report -- the harness-owned
# replacement for "the agent edits tasks.json directly". Same split as
# `worker_unsatisfiable_report_errors` just above: schema validation is
# `contracts.validate_payload`'s job, this is the business rule (a
# `task_id` is required so the harness's own contract-violation message
# can name exactly which assignment a malformed report was for -- the move
# it authorises is always over the task THIS iteration was assigned,
# `LoopSupervisor._apply_worker_completion_report`, never a task_id the
# payload could invent unilaterally).
# ---------------------------------------------------------------------------


def worker_completion_report_errors(payload: Any) -> list[str]:
    """Errors in a worker's `verdict: "complete"` report, or an empty list
    when the report is well-formed. A payload that does not claim
    `verdict == "complete"` at all is never in violation here -- the same
    "nothing proposed, nothing to check" shape every sibling check in this
    module uses.
    """
    if not isinstance(payload, dict) or payload.get("verdict") != "complete":
        return []
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        return ["verdict 'complete' requires a non-empty task_id"]
    return []


# ---------------------------------------------------------------------------
# Task 118 (#60 pillar 1, fact 9): the sibling semantic check for the
# verifier's own `verify` contract report -- same split every other check in
# this module uses (schema validation is `contracts.validate_payload`'s job;
# this is the business rule). `task_id` is required by the schema itself
# (HAT_SCHEMAS["verify"]["required"]) for every verdict, so nothing here
# repeats that check; what only a REJECTED verdict adds is a mandatory
# `reason` -- the harness no longer accepts an agent's own free-text
# `validationNotes` write (fact 9), so a rejection with nothing behind it
# would leave no record at all of why. A verifier that names a task other
# than the one it was actually asked about is not this function's business
# either -- `LoopSupervisor._verify_task` checks that against the task under
# verification directly, the same way `_apply_worker_completion_report`
# checks a worker's task_id against the one the scheduler assigned.
# ---------------------------------------------------------------------------


def verify_report_errors(payload: Any) -> list[str]:
    """Errors in a verifier's `verdict: "rejected"` report, or an empty list
    when the report is well-formed. A payload that does not claim
    `verdict == "rejected"` at all (including a `verified` verdict, which
    carries no such requirement) is never in violation here -- the same
    "nothing proposed beyond what the schema already checked, nothing more
    to check" shape every sibling check in this module uses.
    """
    if not isinstance(payload, dict) or payload.get("verdict") != "rejected":
        return []
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return ["verdict 'rejected' requires a non-empty reason"]
    return []


# ---------------------------------------------------------------------------
# Task 119 (#60 pillar 3, fact 9): review's sibling to `verify_report_errors`
# just above -- review.md no longer ends its reply with the bare
# `<promise>VERIFIED</promise>` sentinel; `LoopSupervisor._review_verdict`
# reads its `{"verdict": "verified"|"rejected", "reason": ..., 
# "findings_path": ...}` contract payload instead (`HAT_SCHEMAS["review"]`).
# Same split as every sibling check in this module: schema validation is
# `contracts.validate_payload`'s job, this is the business rule a `rejected`
# verdict with nothing behind it would otherwise dodge -- a review that
# rejects an approach without saying why leaves no record at all of what to
# fix on the next attempt, exactly the gap a mandatory `reason` on a
# rejected `verify` verdict already closes.
# ---------------------------------------------------------------------------


def review_report_errors(payload: Any) -> list[str]:
    """Errors in a reviewer's `verdict: "rejected"` report, or an empty list
    when the report is well-formed. A payload that does not claim
    `verdict == "rejected"` at all (including a `verified` verdict, which
    carries no such requirement) is never in violation here -- same
    "nothing proposed, nothing to check" shape `verify_report_errors` uses.
    """
    if not isinstance(payload, dict) or payload.get("verdict") != "rejected":
        return []
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return ["verdict 'rejected' requires a non-empty reason"]
    return []


# ---------------------------------------------------------------------------
# Task 002 (requirement A.2, #110): the ONE contradiction a structured
# findings channel (`engine/findings.py`, task 001) can catch that
# `review_report_errors` above cannot -- a `verdict: "verified"` claim
# alongside the SAME review's own findings, when at least one of them is
# `severity: "blocking"`. `engine/loop.py`'s `_review_contract_ladder` feeds
# this to `contract_enforcement.enforce_contract` as an `extra_validate`
# hook, the same re-ask ladder every other semantic check in this module
# already rides (`task_status_extra_validate`, `worker_unsatisfiable_
# report_errors`, ...) -- the payload is refused and re-asked, NEVER
# silently rewritten to `"rejected"` by this function or its caller.
#
# Deliberately one-directional: a `verdict: "rejected"` payload whose
# findings are ALL residual is not flagged here at all -- that is not a
# hat mistake to refuse, it is the engine's own honest recomputation
# (`findings.compute_verdict`, DoD predicate 3), applied by the caller on
# top of an already-accepted payload, never routed back through this
# re-ask ladder.
# ---------------------------------------------------------------------------


def review_verdict_findings_errors(
    payload: Any, findings: "list[dict] | None"
) -> list[str]:
    """Errors when `payload` claims `verdict == "verified"` while `findings`
    (the review's own structured findings list, already schema-validated by
    the caller -- see `engine.findings.read_findings_json`) contains at
    least one `severity: "blocking"` entry. Returns `[]` whenever `findings`
    is `None` (no structured findings channel exists for this review at
    all -- nothing to check the claim against, so the payload's own claimed
    verdict is trusted exactly as it always was) or the payload does not
    claim `verified` in the first place (a `rejected` verdict carries no
    such requirement, same "nothing proposed, nothing to check" shape every
    sibling check in this module uses).
    """
    if findings is None or not isinstance(payload, dict):
        return []
    if payload.get("verdict") != "verified":
        return []
    from ralphd.engine import findings as findings_mod
    if findings_mod.compute_verdict(findings) == "rejected":
        return ["verdict 'verified' contradicts its own findings: at least "
                "one finding has severity 'blocking'"]
    return []


def review_cured_when_errors(findings: "list[dict] | None") -> list[str]:
    """Task 031 (PRD requirement E.2, #171): errors when `findings` (the
    same already-schema-validated structured findings list `review_
    verdict_findings_errors` above reads) carries a curable blocking
    finding with no `cured_when`, or one whose `cured_when` text itself
    overruns the plan's own bounded-checkable-clause lint. `cured_when`
    BECOMES a cure task's `successCriteria` once a cure pass is granted
    (`state.cure_task_from_finding`), so it is held to review's contract
    ladder at the point it is asserted, exactly the way `review_verdict_
    findings_errors` holds a contradicted `verified` claim to it -- never
    left for the worker to discover only once the malformed cure task is
    already in `tasks.json`.

    Returns `[]` whenever `findings` is `None` (no structured findings
    channel at all -- nothing to check) -- the same "nothing to check"
    shape `review_verdict_findings_errors` uses for the identical case.
    Deliberately does not restrict itself to a `rejected` payload the way
    `review_verdict_findings_errors` restricts itself to a `verified`
    one: a curable blocking finding with no `cured_when` is malformed on
    its own terms regardless of what verdict the payload claims (a
    `verified` claim over such a finding is ALREADY refused by `review_
    verdict_findings_errors`'s own contradiction check; this closes the
    remaining case, a `rejected` claim whose curable blocking findings
    are individually incomplete).

    #209 defect 1: the overrun half of this refusal now fires only where no
    legal rewrite exists. The identical bound still applies, but a
    `cured_when` whose overrun folds losslessly and lint-clean under
    `plan_ingestion.repair_criteria` is REPAIRED at the one point the text
    becomes a criterion (`state.insert_cure_tasks`) instead of being
    refused here and the review hat re-asked over advice the harness could
    already act on. The scope handed to `repair_criteria` --
    `lints=("clause-count-overrun",)` -- is deliberately the SAME scope
    that call site uses: a guard that forgave more than the repair applies
    would let an oversized `cured_when` land verbatim with no refusal
    anywhere, which is strictly worse than the waste it was meant to
    remove. The missing-`cured_when` refusal above is untouched -- there is
    no text there to rewrite.
    """
    if not findings:
        return []
    from ralphd.engine import findings as findings_mod
    from ralphd.engine import plan_ingestion
    errors: list[str] = []
    for finding in findings_mod.curable_findings_missing_cured_when(findings):
        errors.append(
            f"finding {finding.get('clause')!r}: a curable blocking "
            "finding requires a non-empty 'cured_when' -- the "
            "specification a fresh cure task's successCriteria is built "
            "from (PRD requirement E.2, #171)")
    for finding, overrun in findings_mod.curable_findings_with_overrun_cured_when(
            findings):
        # #209 defect 1: an overrun `cured_when` for which `plan_ingestion`
        # can compute a LOSSLESS, lint-clean fold -- with its own
        # clause-count rewrite, the one that computes the `suggested
        # rewrite` the plan lint has always quoted as advice -- is no
        # longer refused. `state.insert_cure_tasks` applies that
        # same fold when it turns the text into the cure task's
        # `successCriteria` (`plan_ingestion.repair_criteria`, event
        # `task.criteria_repaired`, `source="cure-insertion"`), so there is
        # nothing left for review to fix and re-asking a review hat that
        # already said everything correct wastes a whole contract attempt.
        # `selfdev-v022` (#209 §1) spent its planning ladder on the twin of
        # this refusal. The bound itself is UNCHANGED and the refusal below
        # survives verbatim for the residue: a `cured_when` whose every
        # fold is still refused by another clause lint, i.e. one where no
        # legal rewrite exists at all -- including the case where the fold
        # would be `_OVERRUN_REWRITE_FALLBACK`, which deletes the criterion
        # and is never applied.
        if plan_ingestion.repair_criteria(
                finding.get("cured_when"),
                lints=("clause-count-overrun",)) is not None:
            continue
        clause_count, overflow_clause = overrun
        errors.append(
            f"finding {finding.get('clause')!r}: cured_when carries "
            f"{clause_count} checkable clauses, past the "
            f"~{plan_ingestion.MAX_CHECKABLE_CLAUSES}-clause bound (first "
            f"clause past the bound: {overflow_clause!r})")
    return errors


# ---------------------------------------------------------------------------
# Task 018 (requirement K, #101): the peer plan-review hat's sibling to
# `review_report_errors` just above -- a `verdict: "objected"` claim with
# no `reason` names nothing a planning revision could act on, exactly the
# gap a mandatory `reason` on a `rejected` review/verify verdict already
# closes. Same split every sibling semantic check in this module uses:
# schema validation is `contracts.validate_payload`'s job, this is the
# business rule.
# ---------------------------------------------------------------------------


def plan_review_report_errors(payload: Any) -> list[str]:
    """Errors in a peer reviewer's `verdict: "objected"` report, or an
    empty list when the report is well-formed. A payload that does not
    claim `verdict == "objected"` at all (including an `approved` verdict,
    which carries no such requirement) is never in violation here -- same
    "nothing proposed, nothing to check" shape `review_report_errors` uses.
    """
    if not isinstance(payload, dict) or payload.get("verdict") != "objected":
        return []
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return ["verdict 'objected' requires a non-empty reason"]
    return []


# ---------------------------------------------------------------------------
# Task 020 (requirement L, #101): the peer VERDICT gate's sibling to
# `review_report_errors`/`plan_review_report_errors` above -- a `verdict:
# "rejected"` claim with no `reason` names nothing for a fresh approach to
# act on, exactly the gap a mandatory `reason` on a `rejected` review/
# verify verdict already closes. Same split every sibling semantic check
# in this module uses: schema validation is `contracts.validate_payload`'s
# job, this is the business rule.
# ---------------------------------------------------------------------------


def verdict_gate_report_errors(payload: Any) -> list[str]:
    """Errors in a peer verdict-gate hat's `verdict: "rejected"` report, or
    an empty list when the report is well-formed. A payload that does not
    claim `verdict == "rejected"` at all (including a `verified` verdict,
    which carries no such requirement) is never in violation here -- same
    "nothing proposed, nothing to check" shape `review_report_errors` uses.
    """
    if not isinstance(payload, dict) or payload.get("verdict") != "rejected":
        return []
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return ["verdict 'rejected' requires a non-empty reason"]
    return []


# ---------------------------------------------------------------------------
# Task 121 (#60 pillar 3, fact 9): reflect's sibling to `review_report_
# errors`/`verify_report_errors` just above -- reflect.md no longer leaves
# its outcome to be inferred from `artifacts/reflection/report.md`'s mere
# existence on disk; `LoopSupervisor._reflect_verdict` reads its
# `{"report_written": ..., "suggestions_written": ..., "notes": ...}`
# contract payload instead (`HAT_SCHEMAS["reflect"]`). Same split as every
# sibling check in this module: schema validation is `contracts.
# validate_payload`'s job, this is the business rule -- a claim of
# `report_written: false` with no `notes` explaining why leaves no record
# at all of what went wrong, exactly the gap a mandatory `reason` on a
# rejected `verify`/`review` verdict already closes for those two hats.
# ---------------------------------------------------------------------------


def reflect_report_errors(payload: Any) -> list[str]:
    """Errors in a reflector's `report_written: false` report, or an empty
    list when the report is well-formed. A payload that does not claim
    `report_written == False` at all (including a `True` claim, which
    carries no such requirement) is never in violation here -- same
    "nothing proposed, nothing to check" shape every sibling check in this
    module uses.
    """
    if not isinstance(payload, dict) or payload.get("report_written") is not False:
        return []
    notes = payload.get("notes")
    if not isinstance(notes, str) or not notes.strip():
        return ["report_written 'false' requires a non-empty notes explaining why"]
    return []
