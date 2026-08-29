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


def validate_answer(raw: str | None,
                     state: str,
                     vigilant: bool,
                     peer_plan_review: bool = False) -> tuple[dict | None, list[str]]:
    """Parse and validate one router answer: JSON object, `ROUTER_SCHEMA`,
    and `next_step` must be one of `state`'s own legal targets for this
    variant. Returns `(payload, errors)`; `errors` is empty iff `payload`
    is not None -- the same shape `contract_enforcement.parse_response`
    uses, deliberately, so both re-prompt loops read the same way.

    `peer_plan_review` defaults to False so every pre-existing caller (the
    production dispatch loop passes only `vigilant`, since `plan_review`
    has its own dedicated `_step_plan_review` handler and never reaches
    this function today) keeps validating against exactly the same legal-
    target set as before. Task 019 (requirement M) adds the parameter so
    the FIRST real multi-edge state (`plan_review`, only reachable on this
    axis) can be validated against its true edges by a live test, without
    a second copy of `transitions.successors`'s own variant-selection
    logic living here.
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
# `validate_answer` above) defaults to False -- the production dispatch
# loop (`LoopSupervisor._step_generic_hat`) passes only `vigilant`, and
# stays byte-identical in behaviour, because `plan_review` still resolves
# through its own dedicated `_step_plan_review` handler rather than this
# generic path. The parameter exists so a caller that DOES want
# `plan_review`'s real (peer-gate-enabled) edges -- currently only
# `tests/test_router.py`'s live-path tests -- can ask this module to
# resolve them without a second, hand-duplicated copy of
# `transitions.successors`'s own variant-selection logic.
async def decide(
    state: str,
    vigilant: bool,
    *,
    ask: AskFn,
    fallback_guard: str | None,
    retry_max: int,
    timeout_s: float,
    peer_plan_review: bool = False,
) -> RouterOutcome:
    """Resolve `state`'s next edge, calling the model only if a genuine
    choice exists.

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
    for attempt_index in range(retry_max + 1):
        attempts = attempt_index + 1
        prompt = _router_prompt(state, edges, error)
        try:
            raw, usage = await asyncio.wait_for(ask(prompt), timeout=timeout_s)
        except asyncio.TimeoutError:
            error = f"router: no answer within {timeout_s}s timeout"
            continue
        last_usage = usage
        payload, errors = validate_answer(raw, state, vigilant, peer_plan_review)
        if payload is not None:
            guard = next(e.guard for e in edges if e.target == payload["next_step"])
            return RouterOutcome(target=payload["next_step"], guard=guard,
                                 used_router=True, used_fallback=False,
                                 attempts=attempts, usage=last_usage)
        error = "; ".join(errors)

    return RouterOutcome(target=fallback_edge.target, guard=fallback_edge.guard,
                         used_router=True, used_fallback=True,
                         attempts=attempts, usage=last_usage)


def _router_prompt(state: str, edges: tuple, error: str | None) -> str:
    options = sorted({e.target for e in edges})
    lines = [
        f"Loop state {state!r} has more than one legal next step.",
        f"Legal options: {options!r}.",
        'Answer with exactly one JSON object and nothing else: '
        '{"next_step": "<one of the options above>"}',
    ]
    if error:
        lines.append(f"Your previous answer was rejected: {error}")
    return "\n".join(lines)
