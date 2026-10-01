"""ralphd.engine.router — the harness's single constrained question for a
state whose next legal edge is not decidable from disk state alone (task
018, requirement E, #60 pillar 2).

"The harness decides wherever the table makes it unambiguous -- one legal
transition, or an obvious next pending task. Only a genuine choice reaches
a model, and then it is an extremely constrained step: one question, one
JSON object (`{"next_step": "worker", "task_id": "123"}`), validated
against both the table's legal transitions and the schema."

Deterministic-first is a property of THIS module, not a promise a caller
has to keep separately: `decide()` below never calls its injected `ask`
callable at all when the state has zero or one legal edge -- the router's
*absence* is what a caller-side test measures, by counting calls into
`ask`, not by inspecting a prompt.

An invalid, unparseable or illegal answer re-prompts with the specific
error (mirroring `contract_enforcement`'s N-then-give-up shape, task 010),
and a hanging call cannot stall the loop -- `asyncio.wait_for` bounds every
attempt. Either way, once the retry budget or the state's own declared
`transitions.ROUTER_FALLBACK` guard is exhausted, `decide()` falls back to
that declared deterministic choice rather than proceeding on a guess: the
table -- not the model -- has the last word on which edge is legal.

This module holds no subprocess/pi knowledge and writes nothing to disk:
`ask` is injected by the caller (`engine.loop.LoopSupervisor._ask_router`
in the real engine; a plain stub in a test), so the decision logic here is
exercised without a model, a run directory or an event loop wired to
anything real.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Awaitable, Callable

from ralphd.engine import contracts
from ralphd.engine import transitions

# One JSON object, one question. Declared once, here -- `next_step` is
# checked against the STATE's OWN legal edges (transitions.successors),
# never accepted as an isolated string; `task_id` is optional context a
# hat-aware caller may want back but the router itself never inspects.
ROUTER_SCHEMA: dict = {
    "type": "object",
    "required": ["next_step"],
    "properties": {
        "next_step": {"type": "string"},
        "task_id": {"type": "string"},
    },
    "additionalProperties": False,
}

# Injected by the caller: given the router's prompt text, return the raw
# answer text and a usage dict (empty/None when the caller has nothing to
# report -- a test stub, for instance). Async so the real engine's
# implementation can await a subprocess exactly like any other model call.
AskFn = Callable[[str], Awaitable[tuple[str, dict]]]


class RouterTimeoutError(RuntimeError):
    """A single router attempt did not answer within its timeout budget.
    Caught by `decide()` itself -- a caller never sees this class; it is
    the mechanism, not the outcome (the outcome is always a `RouterOutcome`
    with `used_fallback=True` once the budget is spent)."""


@dataclass
class RouterOutcome:
    target: str
    guard: str
    used_router: bool     # False iff the state had <=1 edge: no `ask` call.
    used_fallback: bool   # True iff every attempt failed/timed out and the
                           # declared fallback edge was taken instead.
    attempts: int          # number of `ask` calls actually made (0 when
                           # used_router is False).
    usage: dict | None = None  # last attempt's usage, or None if none ran.
    # Task 002 (#278 B.7): a one-word classification of WHY the fallback
    # was taken -- "timeout" (the last attempt never answered within
    # `timeout_s`), "unparseable" (absent/malformed JSON, or a schema
    # violation) or "illegal_target" (a well-formed answer naming a real
    # state that is simply not one of THIS state's own legal edges).
    # `None` whenever `used_fallback` is False -- there is no cause to
    # report for an answer that was actually taken. Feeds
    # `engine.loop.LoopSupervisor._emit_router_decision_event`'s
    # `fallbackCause` field alongside the task-selection path's own
    # `TaskSelectionOutcome.fallback_cause` below, so both routed states
    # emit the SAME `router.decision` shape.
    fallback_cause: str | None = None


def validate_answer(raw: str | None,
                     state: str,
                     vigilant: bool,
                     peer_plan_review: bool = False) -> tuple[dict | None, list[str]]:
    """Parse and validate one router answer: JSON object, `ROUTER_SCHEMA`,
    and `next_step` must be one of `state`'s own legal targets for this
    variant. Returns `(payload, errors)`; `errors` is empty iff `payload`
    is not None -- the same shape `contract_enforcement.parse_response`
    uses, deliberately, so both re-prompt loops read the same way.

    `peer_plan_review` defaults to False so every pre-existing caller keeps
    validating against exactly the same legal-target set as before. Task
    019 (requirement M) added the parameter so the FIRST real multi-edge
    state (`plan_review`, only reachable on this axis) is validated
    against its true edges -- exercised by the live tests
    (`tests/test_router.py`) and, until task 016 (requirement D.2, #117
    half B) replaced that call with a deterministic policy, by
    `engine.loop.LoopSupervisor._step_plan_review`'s undecidable-reply
    branch. `plan_review` stays the first *declared* router-eligible
    state -- its table entry is still validated with
    `peer_plan_review=True` here and in the fixture tests -- without a
    second copy of `transitions.successors`'s own variant-selection logic
    living here.
    """
    if raw is None or not raw.strip():
        return None, ["router: no payload produced (absent output)"]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, [f"router: malformed JSON ({exc.msg} at line {exc.lineno} "
                       f"column {exc.colno})"]
    errors = contracts.validate_against(ROUTER_SCHEMA, payload)
    if errors:
        return None, errors
    legal_targets = sorted({e.target for e in
                            transitions.successors(state, vigilant, peer_plan_review)})
    if payload["next_step"] not in legal_targets:
        return None, [
            f"router: next_step {payload['next_step']!r} is not a legal "
            f"successor of {state!r} (legal: {legal_targets!r})"
        ]
    return payload, []


# Task 019 (requirement M): `peer_plan_review` (both here and on
# `validate_answer` above) defaults to False -- `_step_generic_hat`'s route
# through `engine.loop.LoopSupervisor._route_with_router` passes only
# `vigilant`, and stays byte-identical in behaviour. The parameter exists
# for `plan_review`'s real (peer-gate-enabled) edges: `tests/test_router.py`'s
# live-path tests exercise this module with `peer_plan_review=True` directly
# -- `plan_review` is the first *declared* router-eligible state, a table
# entry with a genuine multi-edge choice on a real reply, even though task
# 016 (requirement D.2, #117 half B) replaced `_step_plan_review`'s own call
# with a deterministic policy, so no shipped run reaches this module for
# `plan_review` any more. Either way the edges come from
# `transitions.successors` itself, never from a second, hand-duplicated
# copy of its variant-selection logic.
async def decide(
    state: str,
    vigilant: bool,
    *,
    ask: AskFn,
    fallback_guard: str | None,
    retry_max: int,
    timeout_s: float,
    peer_plan_review: bool = False,
    context: str | None = None,
) -> RouterOutcome:
    """Resolve `state`'s next edge, calling the model only if a genuine
    choice exists.

    `context` (task 015, requirement D.1, #117 half A) is an opaque text
    block a caller may supply -- the router never parses or inspects it,
    only folds it verbatim into the prompt via `_router_prompt` so the
    model sees whatever evidence made the state genuinely ambiguous (a
    peer's raw reply, its parse errors, etc.) instead of the bare table.
    `None` (the default) reproduces the exact prompt every pre-existing
    caller and fixture test already asserts on -- this parameter changes
    nothing about the deterministic-first behaviour above, the validation
    in `validate_answer`, or the retry/fallback shape below.

    `ask` is called up to `retry_max + 1` times (the same N-then-give-up
    shape as `contract_enforcement.enforce_contract`, task 010) -- never
    zero times once a genuine choice is reached, and never at all when it
    is not. Each call is bounded by `timeout_s`; a call that does not
    return in time counts as one failed attempt (its own validation error
    names the timeout) rather than a raised exception the caller has to
    catch. If nothing valid ever comes back, `fallback_guard` -- which
    MUST name one of `state`'s own legal edges when this path is ever
    reached, exactly as `transitions.ROUTER_FALLBACK` declares it -- is
    taken instead.
    """
    edges = transitions.successors(state, vigilant, peer_plan_review)
    if len(edges) <= 1:
        # Deterministic-first: nothing to decide, so `ask` is never called.
        if not edges:
            raise ValueError(f"router.decide() called for terminal state {state!r}")
        edge = edges[0]
        return RouterOutcome(target=edge.target, guard=edge.guard,
                             used_router=False, used_fallback=False,
                             attempts=0, usage=None)

    if fallback_guard is None:
        raise AssertionError(
            f"state {state!r} has {len(edges)} legal edges and no "
            "transitions.ROUTER_FALLBACK entry -- an ambiguous state must "
            "declare its deterministic fallback beside its table entry "
            "before the router can ever be reached for it")
    fallback_edges = [e for e in edges if e.guard == fallback_guard]
    if not fallback_edges:
        raise AssertionError(
            f"state {state!r}'s declared ROUTER_FALLBACK guard "
            f"{fallback_guard!r} is not one of its own legal edges "
            f"({[e.guard for e in edges]!r})")
    fallback_edge = fallback_edges[0]

    error: str | None = None
    last_usage: dict | None = None
    attempts = 0
    cause: str | None = None
    for attempt_index in range(retry_max + 1):
        attempts = attempt_index + 1
        prompt = _router_prompt(state, edges, error, context)
        try:
            raw, usage = await asyncio.wait_for(ask(prompt), timeout=timeout_s)
        except asyncio.TimeoutError:
            error = f"router: no answer within {timeout_s}s timeout"
            cause = "timeout"
            continue
        last_usage = usage
        payload, errors = validate_answer(raw, state, vigilant, peer_plan_review)
        if payload is not None:
            guard = next(e.guard for e in edges if e.target == payload["next_step"])
            return RouterOutcome(target=payload["next_step"], guard=guard,
                                 used_router=True, used_fallback=False,
                                 attempts=attempts, usage=last_usage)
        error = "; ".join(errors)
        cause = ("illegal_target" if any("is not a legal successor" in e for e in errors)
                 else "unparseable")

    return RouterOutcome(target=fallback_edge.target, guard=fallback_edge.guard,
                         used_router=True, used_fallback=True,
                         attempts=attempts, usage=last_usage,
                         fallback_cause=cause)


def _router_prompt(state: str, edges: tuple, error: str | None,
                    context: str | None = None) -> str:
    options = sorted({e.target for e in edges})
    lines = [
        f"Loop state {state!r} has more than one legal next step.",
        f"Legal options: {options!r}.",
        'Answer with exactly one JSON object and nothing else: '
        '{"next_step": "<one of the options above>"}',
    ]
    if error:
        lines.append(f"Your previous answer was rejected: {error}")
    # Task 015 (D.1, #117 half A): whatever evidence the caller supplied
    # (a peer's raw reply, its parse errors, ...) is folded in verbatim,
    # never parsed or reshaped -- `decide()` treats it as opaque text.
    if context:
        lines.append(f"Context:\n{context}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Task 002 (#278 B.1/B.4/B.7): task-selection routing -- a SECOND, narrower
# question from the same module, asked only when `engine.scheduler`'s
# pending step (`scheduler.pending_candidates`) has genuinely more than one
# runnable candidate. Deliberately its own function rather than a second
# caller of `decide()` above: `decide()` answers "which of this STATE's
# legal EDGES" (`next_step`, validated against `transitions.successors`)
# and retries up to `retry_max` extra times before falling back; a task
# pick answers "which of these TASK ids" and is asked exactly ONCE -- one
# `ask` call, no contract retry -- so an unparseable answer, an answer
# naming a task outside the candidate set, and a call that outruns
# `timeout_s` each fall back to the caller-supplied deterministic pick
# immediately, distinguished only by `fallback_cause` ("unparseable",
# "non_candidate_id", "timeout") for the `router.decision` event
# (`engine.loop.LoopSupervisor._emit_router_decision_event`) to report.

# One JSON object, one question: a candidate id plus a one-line reason.
# Task 004 (#278 B.2/B.3) widens the PROMPT this schema's answer is asked
# for (every candidate's id/title/description start/origin/priority/
# dependsOn, the deterministic pick, the last worker iteration's outcome,
# and B.3's "whose result the others depend on in fact" rule) -- see
# `_task_selection_prompt`/`TASK_SELECTION_RULE` below -- without touching
# this schema or the single-call/no-retry/fallback shape below.
TASK_SELECTION_SCHEMA: dict = {
    "type": "object",
    "required": ["candidate_id", "reason"],
    "properties": {
        "candidate_id": {"type": "string"},
        "reason": {"type": "string"},
    },
    "additionalProperties": False,
}


@dataclass
class TaskSelectionOutcome:
    chosen_id: str
    reason: str
    used_router: bool
    used_fallback: bool
    fallback_cause: str | None = None
    usage: dict | None = None


def validate_task_selection_answer(
        raw: str | None, candidate_ids: list) -> tuple[dict | None, str | None]:
    """Parse and validate one task-selection answer. Returns `(payload,
    cause)`; `cause` is `None` iff `payload` is not `None`, and is one of
    the two non-timeout fallback causes otherwise: `"unparseable"` for an
    absent/malformed answer or one that does not match
    `TASK_SELECTION_SCHEMA`, `"non_candidate_id"` for an otherwise-valid
    answer naming a task id that is not in `candidate_ids`."""
    if raw is None or not raw.strip():
        return None, "unparseable"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, "unparseable"
    if contracts.validate_against(TASK_SELECTION_SCHEMA, payload):
        return None, "unparseable"
    if payload["candidate_id"] not in candidate_ids:
        return None, "non_candidate_id"
    return payload, None


# Task 004 (#278 B.2/B.3): how far into a candidate's `description` (or,
# absent that field -- the shape every plan in this codebase actually
# uses -- its `successCriteria`) the prompt quotes, verbatim, before
# truncating with an ellipsis. Mirrors the `[:160]` convention already
# used for other operator/model-facing text previews in this module's
# sibling `engine.loop` (e.g. its fault-line previews).
_CANDIDATE_DESCRIPTION_PREVIEW_CHARS = 160

# Task 004 (#278 B.3): the router's ordering rule, quoted into the prompt
# verbatim -- this is the ONLY place that decides task order from the
# model's point of view; nothing in `_task_selection_prompt` below ever
# tells the router to prefer a candidate because of its `origin` or
# `priority` field, on top of this text.
TASK_SELECTION_RULE = (
    "Pick the task whose result the others depend on in fact, not only by "
    "dependsOn. If a candidate changes the code that another candidate "
    "measures (a fix before a sweep of the same code), the change goes "
    "first. origin and priority are inputs, not a ranking. When no "
    "candidate's text gives a reason to reorder, take the deterministic "
    "pick."
)


def _candidate_description_start(candidate: dict) -> str:
    """The start of a candidate's descriptive text -- its `description`
    field if present, else `successCriteria` (every plan this engine ships
    with uses the latter; no plan task carries both), truncated to
    `_CANDIDATE_DESCRIPTION_PREVIEW_CHARS` with a trailing ellipsis when
    cut."""
    text = candidate.get("description") or candidate.get("successCriteria") or ""
    text = str(text).strip()
    if len(text) > _CANDIDATE_DESCRIPTION_PREVIEW_CHARS:
        return text[:_CANDIDATE_DESCRIPTION_PREVIEW_CHARS] + "..."
    return text


async def decide_task_selection(
        candidates: list,
        deterministic_id: str,
        *,
        ask: AskFn,
        timeout_s: float,
        last_worker_iteration: dict | None = None,
        context: str | None = None,
) -> TaskSelectionOutcome:
    """Ask the router to name ONE of `candidates` (plus a one-line
    reason), exactly once -- never called at all by the only production
    caller (`engine.loop.LoopSupervisor._route_task_selection`) unless
    `scheduler.pending_candidates` already found 2+ runnable tasks, so
    `used_router` is always True here (there is no deterministic-first
    short-circuit the way `decide()` has for a 0/1-edge state -- the
    caller is the one that never reaches this function for a single
    candidate).

    `candidates` is the FULL task dict for each runnable candidate (task
    004, #278 B.2) -- never a bare id list -- so the prompt built below can
    quote each one's id, title, description start, `origin`, `priority`
    and `dependsOn`. `last_worker_iteration`, if given, is a plain dict
    with `task_id`/`verdict`/`proposed_tasks` describing the outcome of
    the worker iteration immediately before this call (B.2's "outcome of
    the last worker iteration"); `None` when none has run yet this
    approach.

    Falls back to `deterministic_id` -- which MUST be a member of
    `candidates`' ids -- on any of three causes, each its own
    `fallback_cause`: `"timeout"` (no answer within `timeout_s`),
    `"unparseable"` (absent/malformed JSON, or a schema violation) and
    `"non_candidate_id"` (a well-formed answer naming a task id outside
    the candidate set). No re-prompt, on any of the three -- one `ask` call
    total, whatever the failure.
    """
    candidate_ids = [str(c.get("id")) for c in candidates]
    if len(candidate_ids) < 2:
        raise ValueError(
            "decide_task_selection() called with fewer than 2 candidates "
            f"({candidate_ids!r}) -- the caller must only reach this "
            "function when scheduler.pending_candidates() found 2 or more")
    if deterministic_id not in candidate_ids:
        raise ValueError(
            f"deterministic_id {deterministic_id!r} is not one of "
            f"candidate_ids {candidate_ids!r}")
    prompt = _task_selection_prompt(candidates, deterministic_id,
                                     last_worker_iteration, context)
    try:
        raw, usage = await asyncio.wait_for(ask(prompt), timeout=timeout_s)
    except asyncio.TimeoutError:
        return TaskSelectionOutcome(chosen_id=deterministic_id, reason="",
                                    used_router=True, used_fallback=True,
                                    fallback_cause="timeout", usage=None)
    payload, cause = validate_task_selection_answer(raw, candidate_ids)
    if payload is None:
        return TaskSelectionOutcome(chosen_id=deterministic_id, reason="",
                                    used_router=True, used_fallback=True,
                                    fallback_cause=cause, usage=usage)
    return TaskSelectionOutcome(chosen_id=payload["candidate_id"],
                                reason=payload.get("reason", ""),
                                used_router=True, used_fallback=False,
                                fallback_cause=None, usage=usage)


def _task_selection_prompt(
        candidates: list, deterministic_id: str,
        last_worker_iteration: dict | None = None,
        context: str | None = None) -> str:
    """Task 004 (#278 B.2/B.3): the prompt a task-selection router call is
    asked. Every candidate's id/title/description-start/origin/priority/
    dependsOn (B.2), the deterministic pick, the last worker iteration's
    own outcome (B.2), and the ordering rule (B.3, `TASK_SELECTION_RULE`)
    -- `origin`/`priority` are quoted as plain candidate FIELDS here, never
    folded into any reordering instruction of this function's own; the
    only reordering instruction in the whole prompt is `TASK_SELECTION_RULE`
    itself."""
    lines = [
        "Selecting the next task for a worker iteration has more than one "
        "runnable candidate.",
        "Candidates:",
    ]
    for c in candidates:
        depends_on = c.get("dependsOn") if isinstance(c.get("dependsOn"), list) else []
        lines.append(
            f"- id: {c.get('id')!r}\n"
            f"  title: {c.get('title')!r}\n"
            f"  description: {_candidate_description_start(c)!r}\n"
            f"  origin: {c.get('origin')!r}\n"
            f"  priority: {c.get('priority', 0)!r}\n"
            f"  dependsOn: {list(depends_on)!r}")
    lines.append(
        "The deterministic pick (highest priority, then plan order) is "
        f"{deterministic_id!r}.")
    if last_worker_iteration:
        lines.append(
            "The last worker iteration worked on task "
            f"{last_worker_iteration.get('task_id')!r}, reported verdict "
            f"{last_worker_iteration.get('verdict')!r}, and proposed these "
            f"new tasks: {list(last_worker_iteration.get('proposed_tasks') or [])!r}.")
    else:
        lines.append("No worker iteration has run yet this approach.")
    lines.append(TASK_SELECTION_RULE)
    lines.append(
        'Answer with exactly one JSON object and nothing else: '
        '{"candidate_id": "<one of the candidate ids above>", '
        '"reason": "<one-line reason>"}')
    if context:
        lines.append(f"Context:\n{context}")
    return "\n".join(lines)
