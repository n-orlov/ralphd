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
            "verdict": {"type": "string", "enum": ["complete", "no_verdict"]},
            "task_id": {"type": "string"},
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
