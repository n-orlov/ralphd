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

from typing import Any

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
        # rejected, and this was the last approach / budget is gone.
        T("finishing", "rejected_no_approach_left"),
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


def table_for(vigilant: bool) -> dict[str, tuple[Transition, ...]]:
    """Compose `TABLE` with `VIGILANT_DELTA` (vigilant) or leave `verify`
    unreachable (non-vigilant). This is the ONLY function that produces a
    variant's table — there must never be a second, hand-written
    non-vigilant table sitting beside this one.
    """
    table = dict(TABLE)
    if vigilant:
        table.update(VIGILANT_DELTA)
    else:
        table["verify"] = ()
    return table


def successors(state: str, vigilant: bool) -> tuple[Transition, ...]:
    """Every legal transition out of `state` for the given variant. Always
    returns a tuple (possibly empty) — never raises for a state the table
    knows about, so successors are enumerable for every declared state."""
    return table_for(vigilant).get(state, ())


def is_legal(state: str, target: str, vigilant: bool) -> bool:
    """True iff `target` is a legal successor of `state` in this variant."""
    return any(edge.target == target for edge in successors(state, vigilant))


def guard_names(state: str, vigilant: bool) -> tuple[str, ...]:
    """The declared guard names for `state`'s transitions, in table order."""
    return tuple(edge.guard for edge in successors(state, vigilant))


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
    "succeeded": None,
    "failed": None,
    "aborted": None,
}

# The hats that need a response-contract schema (task 009) and a prompt.
# `verify` is included even though it is unreachable in the non-vigilant
# variant: the hat exists in both, the *transition* into it does not.
HATS: tuple[str, ...] = ("planning", "worker", "verify", "review", "reflect")


# ---------------------------------------------------------------------------
# The router's declared fallback -- task 018, requirement E, #60 pillar 2
# ---------------------------------------------------------------------------
# "The harness decides wherever the table makes it unambiguous -- one legal
# transition, or an obvious next pending task. Only a genuine choice reaches
# a model." Every hand-written `_step_*` method (planning/worker/review/
# replan) computes its own guard directly from disk state and NEVER reaches
# the router, whatever this dict says -- `ROUTER_FALLBACK` only matters to
# `engine.loop._step_generic_hat` (task 015's one shipped fallback for any
# OTHER state the table names), and then only when that state has more than
# one legal edge (task 015 shipped only the single-edge case; task 018 is
# what makes a multi-edge one work, via `engine.router`).
#
# "Say in the table what that fallback is for each ambiguous state"
# (requirement E's acceptance): a state that can reach the router MUST name,
# right here, which of its own declared edges (by GUARD name) the harness
# takes when the router's answer is invalid/unparseable even after the
# retry budget, or when the call times out. This is not a second table --
# it is a value alongside the one that already exists, exactly like
# STATE_HAT/HAT_SCHEMAS beside TABLE.
#
# Empty by default: no *shipped* state is router-ambiguous today (every real
# hat still has a hand-written `_step_*` method that decides its own guard
# deterministically) -- this dict exists so a FUTURE multi-edge hat can
# declare its fallback beside its table entry, proven end to end only by a
# test that registers one (mirroring task 015's no-op hat fixture).
ROUTER_FALLBACK: dict[str, str] = {}


def fallback_guard(state: str) -> str | None:
    """The declared deterministic fallback guard for `state`, or None when
    none is declared (a state with 0 or 1 legal edges never needs one --
    it is resolved without ever reaching the router)."""
    return ROUTER_FALLBACK.get(state)


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

TASK_STATUSES: tuple[str, ...] = (
    "pending",
    "in-progress",
    "awaiting-validation",
    "validated",
    "validation-failed",
    "completed",
    "failed",
    "skipped",
)

TASK_STATUS_TABLE: dict[str, tuple[str, ...]] = {
    "pending": ("in-progress",),
    # non-vigilant's claim path -- vigilant overrides this entry entirely
    # (see TASK_STATUS_VIGILANT_DELTA below).
    "in-progress": ("completed", "failed", "skipped"),
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
    "failed": (),
    "skipped": (),
}

TASK_STATUS_VIGILANT_DELTA: dict[str, tuple[str, ...]] = {
    # replaces the non-vigilant claim path: a vigilant worker's completed
    # claim is not trusted on its own word, it waits for verify.
    "in-progress": ("awaiting-validation", "failed", "skipped"),
    # verify's own verdict: `validated` is the vigilant terminal, a
    # negative verdict loops to `validation-failed` for a retry, and an
    # exhausted validation budget (`state.VALIDATION_ATTEMPT_LIMIT`) may
    # force straight to `failed` from here in one disk write (`_verify_
    # task`'s own sequencing: `validation-failed` is set in memory first,
    # then overwritten to `failed` before the single write lands).
    "awaiting-validation": ("validated", "validation-failed", "failed"),
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
    """

    __slots__ = ("vigilant",)

    def __init__(self, vigilant: bool) -> None:
        self.vigilant = vigilant

    def advance(self, state: str, target: str, guard: str) -> str:
        """Move from `state` to `target` via the named `guard`'s edge, or
        raise `IllegalTransition`. Returns `target` on success so a caller
        can write `state = executor.advance(state, target, guard)`."""
        if not is_legal(state, target, self.vigilant):
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
            "verdict": {"type": "string",
                        "enum": ["complete", "no_verdict", "unsatisfiable"]},
            "task_id": {"type": "string"},
            "reason": {"type": "string"},
            "notes": {"type": "string"},
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
        },
        "additionalProperties": False,
    },
    "reflect": {
        "type": "object",
        "required": ["outcome"],
        "properties": {
            "outcome": {
                "type": "string",
                "enum": ["succeeded", "failed", "aborted"],
            },
            "notes": {"type": "string"},
        },
        "additionalProperties": False,
    },
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
