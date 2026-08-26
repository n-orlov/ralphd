"""ralphd.engine.plan_ingestion — ingest the planner's file-borne plan
proposal (task 011, requirement C, #60 pillar 1).

Per #60's decided channel split (fact 4, `transitions.HAT_SCHEMAS["planning"]`):
the planner's final output is only a pointer-plus-summary
(`{"proposal_path": ..., "summary": ...}`), validated exactly like any other
hat's response (`contracts.validate_payload`, via
`contract_enforcement.parse_response` — reused here, not re-derived). The
plan itself is file-borne: the planner drafts `plan-proposal.json` in its
writable scratch area, and the harness treats *that file* as untrusted
input, in three ordered steps:

  1. schema validation of the pointer payload (task 010's mechanism, as-is)
  2. structural shape validation of the proposal file's own JSON  — ad hoc
     `isinstance` checks below, deliberately NOT written through a second
     schema-shaped validator. `tests/test_response_contracts.py` scans the
     whole tree for any function that treats "required"/"properties"/"type"
     as string constants together and fails the suite if it finds one
     outside `engine/contracts.py` (fact 16/#65's lesson) — this module's
     shape check earns that by construction, not by exemption.
  3. semantic lint: `dependsOn` refs resolve, no cycles, no duplicate ids,
     no empty/boilerplate `successCriteria`, and — the enforcement point for
     requirement C's rule that a proposal "carrying a status field is
     rejected, not honoured" — no task may smuggle a `status` of its own.
     Requirement H's own-terminal-state rule (task 012, #63's incident) is
     also enforced at this step, by `lint_own_terminal_state` alongside
     `lint_proposal` — kept as a separate, separately-tested function rather
     than folded into `lint_proposal` itself.

Only after all three steps pass does `ingest_proposal` build the harness's
own `tasks.json` document, initialising every task to `pending` regardless
of anything the proposal said (belt-and-suspenders: step 3 already refuses
a proposal that tries).

A lint failure — like any other contract violation — re-prompts the planner
with the *specific* error, under the same N-then-agent-fault policy task 010
built (`JobConfig.contract_retry_max`, `EVENT_VIOLATION`/`EVENT_AGENT_FAULT`
emitted to `events.jsonl` at the moment each attempt fails, not batched).
`enforce_plan_ingestion` below is that loop, reusing
`contract_enforcement.parse_response` for step 1 rather than re-implementing
it.

The exact same function serves an approach-boundary replan: nothing here is
first-plan-only, and nothing keys off approach number (approach-scoped
namespacing of *validated/verified* task state is task 020's concern, not
ingestion's).

Nothing in `_run_job_core` calls this yet — task 014's executor cutover
decides where in the real loop a planning iteration's proposal reaches this
module. This module only has to be correct and observable in isolation,
exactly as `contracts.py` and `contract_enforcement.py` were for tasks 009
and 010.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ralphd.engine.contract_enforcement import (
    EVENT_AGENT_FAULT,
    EVENT_VIOLATION,
    EventSink,
    RespondFn,
    parse_response,
)

# A task field carrying exactly this text is the unfilled placeholder from
# `prompts/planning.md`'s own schema example -- copy-pasted rather than
# written, which makes it "boilerplate" rather than a real criterion.
BOILERPLATE_SUCCESS_CRITERIA = (
    "<natural language, independently checkable by someone who did not do "
    "the work>"
)

# Fields a proposal's task is allowed to carry through to the harness's own
# tasks.json verbatim. Deliberately excludes "status" -- see the module
# docstring's step 3.
_TASK_PASSTHROUGH_FIELDS: tuple[str, ...] = (
    "id",
    "title",
    "successCriteria",
    "dependsOn",
    "priority",
)


# ---------------------------------------------------------------------------
# Step 2: structural shape validation (deliberately not schema-shaped)
# ---------------------------------------------------------------------------

def validate_proposal_shape(payload: Any) -> list[str]:
    """Ad hoc structural check on the parsed `plan-proposal.json` document:
    an object with a `tasks` array of objects, each carrying non-blank
    `id`/`title`/`successCriteria` strings and, if present, a `dependsOn`
    that is itself a list. Nothing here treats a schema as data -- see the
    module docstring for why that distinction matters to the test suite."""
    if not isinstance(payload, dict):
        return [f"$: expected an object, got {type(payload).__name__}"]
    errors: list[str] = []
    tasks = payload.get("tasks")
    if tasks is None:
        return ["$.tasks: missing"]
    if not isinstance(tasks, list):
        return [f"$.tasks: expected an array, got {type(tasks).__name__}"]
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            errors.append(f"$.tasks[{i}]: expected an object, got {type(t).__name__}")
            continue
        tid = t.get("id")
        for field_name in ("id", "title", "successCriteria"):
            value = t.get(field_name)
            if not isinstance(value, str) or not value.strip():
                errors.append(
                    f"$.tasks[{i}] (id={tid!r}): missing or blank {field_name!r}"
                )
        if "dependsOn" in t and not isinstance(t["dependsOn"], list):
            errors.append(f"$.tasks[{i}] (id={tid!r}): 'dependsOn' must be an array")
    return errors


# ---------------------------------------------------------------------------
# Step 3: semantic lint -- structural checks only, plan *quality* is not
# the lint's business (requirement C).
# ---------------------------------------------------------------------------

def _is_boilerplate(criteria: str) -> bool:
    stripped = criteria.strip()
    if stripped == BOILERPLATE_SUCCESS_CRITERIA:
        return True
    # Any leftover "<...>" placeholder shape is boilerplate whether or not
    # it is exactly the example text above -- a worker who half-filled the
    # template still left a template.
    return stripped.startswith("<") and stripped.endswith(">")


def _detect_cycles(tasks: list[dict]) -> list[str]:
    graph: dict[Any, list[Any]] = {t.get("id"): list(t.get("dependsOn") or []) for t in tasks}
    color: dict[Any, int] = {tid: 0 for tid in graph}  # 0 white, 1 gray, 2 black
    errors: list[str] = []
    reported: set[frozenset] = set()

    def visit(node: Any, path: list[Any]) -> None:
        color[node] = 1
        for dep in graph.get(node, []):
            if dep not in graph:
                continue  # unresolved dep is reported separately
            if color.get(dep) == 1:
                cycle = path[path.index(dep):] + [dep]
                key = frozenset(cycle)
                if key not in reported:
                    reported.add(key)
                    errors.append(
                        f"task {cycle[0]!r}: dependsOn cycle: "
                        + " -> ".join(repr(c) for c in cycle)
                    )
            elif color.get(dep) == 0:
                visit(dep, path + [dep])
        color[node] = 2

    for tid in graph:
        if color[tid] == 0:
            visit(tid, [tid])
    return errors


def lint_proposal(payload: dict) -> list[str]:
    """Semantic lint over an already shape-valid proposal. Every error
    names the offending task's id. This function does NOT re-check
    requirement H's own-terminal-state rule -- that lint is task 012's."""
    errors: list[str] = []
    tasks = payload.get("tasks", [])
    seen: dict[Any, int] = {}
    for i, t in enumerate(tasks):
        tid = t.get("id")
        if tid in seen:
            errors.append(f"task {tid!r}: duplicate id (also at position {seen[tid]})")
        else:
            seen[tid] = i
        if "status" in t:
            errors.append(
                f"task {tid!r}: carries a 'status' field -- the harness "
                "initialises every ingested task to pending; a proposal "
                "must not smuggle one in"
            )
        criteria = t.get("successCriteria", "")
        if not isinstance(criteria, str) or not criteria.strip() or _is_boilerplate(criteria):
            errors.append(f"task {tid!r}: successCriteria is empty or boilerplate")

    known_ids = set(seen)
    for t in tasks:
        tid = t.get("id")
        for dep in (t.get("dependsOn") or []):
            if dep not in known_ids:
                errors.append(f"task {tid!r}: dependsOn references unknown task {dep!r}")

    errors.extend(_detect_cycles(tasks))
    return errors


# ---------------------------------------------------------------------------
# Requirement H's ingestion-time half (#63's incident, fact 13, task 012):
# reject a task whose successCriteria makes THIS run's own terminal state a
# precondition of the task's own completion. v0.8's task 042 is the incident
# this exists for: approach 1's 43rd (and last) task made its closing report
# conditional on `status.json` having already left `state: running`, which
# no iteration executing *inside* that same run can ever observe -- three
# worker iterations correctly refused to fabricate it and the approach
# starved on "no task progress in 3 iterations" (docs/prds/README.md's
# settled row for `selfdev-v08-auditable`, and this repo's own commit
# `5f6b6d9` before that row was rewritten, are where that account survives).
#
# The literal task 042 JSON no longer exists anywhere reachable from this
# repo: it lived only inside that run's own run directory, which this job's
# harness tore down once the run finished, and it was never committed here.
# `TASK_042_SUCCESS_CRITERIA` below is therefore reconstructed, not lifted
# byte-for-byte from a surviving file. What IS verifiable is narrower than
# earlier commit messages here claimed (see the fix-up in task 012's second
# pass): commit `5f6b6d9^`'s copy of `docs/prds/README.md` (the parent of
# the commit that rewrote that row -- `git show 5f6b6d9^:docs/prds/README.md`)
# contains, as prose reflowed across a hard line wrap, the clause
#     required `status.json` to leave `state: running` before its closing
#     report could send
# This clause is embedded byte-for-byte (after collapsing the source's own
# whitespace-only line wrap, which changes no word) as a literal substring
# of `TASK_042_SUCCESS_CRITERIA` below -- not merely quoted in this comment.
# `tests/test_plan_ingestion.py` asserts that substring relationship directly
# against the git blob, so the claim is checked, not just written down; it
# also names the limitation that the fixture as a whole is a reconstruction,
# not a recovered original file.
TASK_042_SUCCESS_CRITERIA = (
    "Task 042's own success criteria required `status.json` to leave "
    "`state: running` before its closing report could send, which makes "
    "this run's own terminal state a precondition of this task's own "
    "success criteria being observed to hold."
)

# Cue phrases naming THIS run's own terminal state (not terminal states in
# general -- `finishing` as a non-terminal *run state* in some other task's
# criteria, e.g. task 008's, must not trip this). Matched literally against
# lowercased text, deliberately not as one giant regex, so each cue stays
# readable and auditable on its own.
_OWN_TERMINAL_STATE_CUES: tuple[str, ...] = (
    "terminal state",
    "run is terminal",
    "run has terminated",
    "run's own terminal state",
    "own terminal state",
    "run being terminal",
    "run becomes terminal",
)

# A leave/leaves/left/leaving `state: running` clause is on its own already
# the own-terminal-state trap (v0.8 task 042's exact shape), independent of
# whether the literal words "terminal state" also appear.
_LEAVES_RUNNING_RE = re.compile(
    r"le(?:ave|aves|ft|aving)\s+`?state:\s*running`?", re.IGNORECASE,
)

# Words that turn a mere mention of terminal-state vocabulary into a
# precondition on it -- "before the run is terminal" is the trap; "the run
# terminating cleanly is described in docs/architecture.md" is not.
_PRECONDITION_CUES: tuple[str, ...] = (
    "before", "until", "once", "depends on", "depend on", "requires",
    "require", "cannot", "can not", "can only", "only once", "after",
)

# #76: the two cue sets above used to be matched independently over the
# *whole* criteria string, so any criterion whose subject is terminal-state
# behaviour tripped the guard as soon as one of twelve extremely common
# precondition words appeared *anywhere else* in it -- a criterion long
# enough to be useful (~1000-1400 chars) essentially guarantees one. That is
# what rejected `selfdev-v010-not-a-verdict`'s satisfiable task 006 attempt 2
# twice: it named terminal-state vocabulary as its *subject*, not as a
# precondition of its own completion. Scope the match to one clause -- a
# terminal cue and a precondition cue only trip the guard when they occur in
# the same sentence/clause, not merely the same criterion.
_CLAUSE_SPLIT_RE = re.compile(r"[.!?;]+")


def _depends_on_own_terminal_state(criteria: str) -> bool:
    """True if `criteria` gates the task's own completion on this run
    having already reached a terminal state -- the #63 trap. A criterion
    that merely mentions "status" or a terminal state as a *fact about the
    system* (not a precondition of this task's own success) is not flagged;
    both directions have their own test in `tests/test_plan_ingestion.py`.
    The `_OWN_TERMINAL_STATE_CUES`/`_PRECONDITION_CUES` pair only trips the
    guard when both occur inside the SAME clause (#76) -- a criterion that
    merely discusses terminal-state behaviour in one sentence and uses an
    unrelated precondition word in another is not flagged."""
    lowered = criteria.lower()
    if _LEAVES_RUNNING_RE.search(lowered):
        return True
    for clause in _CLAUSE_SPLIT_RE.split(lowered):
        has_terminal_cue = any(cue in clause for cue in _OWN_TERMINAL_STATE_CUES)
        if not has_terminal_cue:
            continue
        if any(cue in clause for cue in _PRECONDITION_CUES):
            return True
    return False


def lint_own_terminal_state(payload: dict) -> list[str]:
    """Requirement H's plan-time lint (task 012): reject any task whose
    `successCriteria` depends on this run's own terminal state, naming the
    offending task and quoting the criterion. Kept separate from
    `lint_proposal` (see that function's own docstring) -- the two are
    always run together by `_parse_proposal`, but each is independently
    testable and each names its offending task the same way."""
    errors: list[str] = []
    for t in payload.get("tasks", []):
        criteria = t.get("successCriteria", "")
        if isinstance(criteria, str) and _depends_on_own_terminal_state(criteria):
            errors.append(
                f"task {t.get('id')!r}: successCriteria depends on this run's "
                f"own terminal state, which no iteration executing inside the "
                f"run can ever observe (#63's incident, v0.8 task 042): "
                f"{criteria!r}"
            )
    return errors


def _parse_proposal(text: str | None) -> tuple[dict | None, list[str]]:
    """Turn the raw text of `plan-proposal.json` into `(payload, errors)`,
    running steps 2 and 3 in order. Mirrors
    `contract_enforcement.parse_response`'s three-shapes shape, for the
    file channel instead of the inline one."""
    if text is None or not text.strip():
        return None, ["proposal: no plan-proposal.json produced (absent)"]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, [f"proposal: malformed JSON ({exc.msg} at line {exc.lineno} column {exc.colno})"]
    shape_errors = validate_proposal_shape(payload)
    if shape_errors:
        return None, shape_errors
    lint_errors = lint_proposal(payload) + lint_own_terminal_state(payload)
    if lint_errors:
        return None, lint_errors
    return payload, []


# ---------------------------------------------------------------------------
# Ingestion proper: the ONLY place a proposal becomes the harness's own plan
# ---------------------------------------------------------------------------

def ingest_proposal(payload: dict) -> dict:
    """Build the harness-owned `tasks.json` document from a validated,
    linted proposal. Ids/titles/successCriteria/dependsOn/priority survive
    verbatim -- nothing is renumbered or invented. Every task's `status` is
    set to `pending` unconditionally; lint (step 3) already refuses a
    proposal that tries to carry one of its own, so this is
    belt-and-suspenders, not the enforcement point."""
    tasks = []
    for t in payload.get("tasks", []):
        new_task = {k: t[k] for k in _TASK_PASSTHROUGH_FIELDS if k in t}
        new_task["status"] = "pending"
        tasks.append(new_task)
    return {
        "version": payload.get("version", 1),
        "goal": payload.get("goal", ""),
        "scope": payload.get("scope", {}),
        "repositories": payload.get("repositories", []),
        "tasks": tasks,
        "discovered": payload.get("discovered", {}),
    }


@dataclass
class PlanIngestionResult:
    """The outcome of `enforce_plan_ingestion` for one planning (or replan)
    invocation."""

    ok: bool
    tasks_doc: dict | None
    attempts: int
    agent_fault: bool
    errors: list[str] = field(default_factory=list)
    # Task 107 (#60 pillar 3): the planning contract's optional
    # `standing_rules` field, carried through so `LoopSupervisor._step_planning`
    # can pin it (`RunDir.write_standing_rules`) without re-parsing the raw
    # response itself. `""` (never `None`) when the pointer omitted the
    # field or ingestion failed -- the same "absent means nothing to pin"
    # reading `RunDir.read_standing_rules()` already uses.
    standing_rules: str = ""


def file_reader(base_dir: Path | str) -> Callable[[str], str | None]:
    """A convenience `read_proposal` for the ordinary case: the proposal
    lives at `<base_dir>/<proposal_path>`. Returns `None` (never raises) for
    anything unreadable, so a missing/garbled file is the same ordinary
    "absent" case `_parse_proposal` already handles."""
    base = Path(base_dir)

    def _read(proposal_path: str) -> str | None:
        try:
            return (base / proposal_path).read_text()
        except OSError:
            return None

    return _read


def enforce_plan_ingestion(
    respond: RespondFn,
    *,
    max_retries: int,
    read_proposal: Callable[[str], str | None],
    state: EventSink | None = None,
) -> PlanIngestionResult:
    """Validate a planning hat's response end to end -- pointer schema,
    then the proposal file's shape and lint -- re-prompting on any failure.

    Same shape as `contract_enforcement.enforce_contract`: up to
    `max_retries + 1` calls to `respond(attempt, prior_errors)`, the
    specific error carried into the next call, every failed attempt
    `emit()`ed as `EVENT_VIOLATION` the moment it is discovered, and one
    `EVENT_AGENT_FAULT` if every attempt fails. The same function serves an
    approach-boundary replan -- nothing here is first-plan-only."""
    if max_retries < 0:
        raise ValueError("max_retries must be >= 0")

    prior_errors: list[str] | None = None
    errors: list[str] = []
    for attempt in range(max_retries + 1):
        raw = respond(attempt, prior_errors)
        pointer, pointer_errors = parse_response("planning", raw)
        if pointer_errors:
            errors = pointer_errors
        else:
            proposal_text = read_proposal(pointer["proposal_path"])
            proposal, errors = _parse_proposal(proposal_text)
            if not errors:
                return PlanIngestionResult(
                    ok=True, tasks_doc=ingest_proposal(proposal),
                    attempts=attempt + 1, agent_fault=False, errors=[],
                    standing_rules=pointer.get("standing_rules") or "",
                )
        if state is not None:
            state.emit(EVENT_VIOLATION, hat="planning", attempt=attempt + 1,
                        maxAttempts=max_retries + 1, errors=list(errors))
        prior_errors = errors

    if state is not None:
        state.emit(EVENT_AGENT_FAULT, hat="planning", attempts=max_retries + 1,
                    errors=list(errors))
    return PlanIngestionResult(ok=False, tasks_doc=None, attempts=max_retries + 1,
                                agent_fault=True, errors=errors)
