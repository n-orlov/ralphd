"""ralphd.engine.contract_enforcement — the re-prompt loop around
`contracts.validate_payload` (task 010, requirement C, #60 pillar 1).

Fact 4 is why this exists at all: `pi` has no `--response-format`, no
`--schema`, no tool-forcing, so a hat's final output not matching its
declared response contract is an ORDINARY case, not an exception. A
malformed payload, an absent one (the hat produced no parseable output at
all) and syntactically valid JSON of the wrong shape are all the same
failure from here: the harness re-prompts with the *specific* validation
error, up to `JobConfig.contract_retry_max` extra times, and if the hat
never recovers, scores one **agent-fault** iteration against the budget
and the caller continues per the table (`transitions.py`) exactly as it
would for any other outcome in that state — this module does not decide
what "continuing per the table" means, only whether the contract was
satisfied.

Fact 19's hazard (a shared re-derivation of the same check going stale in
one of many copies) applies here too: every violation is `emit()`ed to
`events.jsonl` at the moment it is discovered — inside the retry loop, not
after the fact from the final outcome — so a run that dies mid-retry still
leaves the violation on disk.

Wired in: `_run_job_core`'s dispatch loop (in `loop.py`) reaches
`enforce_contract` at four call sites, one per hat's own step method --
`_step_generic_hat`'s single schema-and-task_status call site
(`loop.py:2561`, landed by v0.13 tasks 003 `2be8b86` and 004 `edbcb79`,
which share one `enforce_contract` call) and `_step_worker`'s three
verdict-refusal sites (`loop.py:3320` unsatisfiable, task 005 `1311548`;
`loop.py:3378` complete, task 006 `8599b7b`; `loop.py:3445`
schema-error, task 007 `aa0a7b1`). Every retry attempt after the first is
answered by `LoopSupervisor._ask_contract_fix`'s uncharged re-ask channel
(task 002), never a second retry loop of its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ralphd.engine.contracts import validate_against, validate_payload

# Event types this module emits -- named once so a caller (or a test) can
# recognise them without restating the literal strings.
EVENT_VIOLATION = "contract.violation"
EVENT_AGENT_FAULT = "contract.agent_fault"


class EventSink(Protocol):
    """The one method this module needs from `state.RunDir` -- a Protocol
    so a test can pass a bare stub instead of a real run directory."""

    def emit(self, type_: str, **data: Any) -> dict: ...


def parse_response(
    hat: str, raw: str | None,
    *, extra_validate: "Callable[[dict], list[str]] | None" = None,
    schema: dict | None = None,
) -> tuple[dict | None, list[str]]:
    """Turn one attempt's raw hat output into `(payload, errors)`.

    `errors` is empty iff `payload` is a validated dict. Three ordinary
    failure shapes, all handled here rather than left for a caller to
    rediscover independently (fact 19):

    - absent: `raw` is `None` or blank -- the hat produced no parseable
      final output at all.
    - malformed: `raw` is not valid JSON.
    - wrong-shape: `raw` is valid JSON but fails `contracts.validate_payload`
      against `hat`'s declared schema (wrong type, missing required field,
      bad enum value, unexpected field, ...).

    `extra_validate` (task 019, requirement F, #60 pillar 3) is an optional
    SEMANTIC check layered on top of the schema pass above -- never a
    second schema validator (fact 16/#65 stays satisfied: `validate_payload`
    is still the only call site that reads a hat's `HAT_SCHEMAS` entry).
    It runs only once the payload is already schema-valid, and its errors
    are folded in exactly like a schema violation: same shape, same
    re-prompt treatment by `enforce_contract` below. A hat reporting a
    `task_status` move the declared status table
    (`transitions.is_legal_task_status_transition`) forbids is the concrete
    case this exists for, but the hook itself knows nothing about task
    statuses -- the caller supplies the check.

    `schema` (task 014, requirement F.1, #144) is an optional OVERRIDE of
    the schema the wrong-shape check above validates against: when given,
    `contracts.validate_against(schema, payload)` runs instead of
    `contracts.validate_payload(hat, payload)` -- the same generic entry
    point `engine.router`'s own answer shape already uses for a shape with
    no `HATS` entry, still routed through `engine.contracts` and nothing
    else (fact 16/#65 holds: no second validator is introduced, only a
    second SCHEMA the one validator may be pointed at). This is what lets
    a `midRunAdjudication: true` review episode's re-ask ladder validate
    every attempt -- not merely a compliant first one -- against
    `transitions.MIDRUN_REVIEW_SCHEMA` rather than the stricter
    `HAT_SCHEMAS["review"]` the SAME `hat="review"` string would otherwise
    select. `hat` is still used for `extra_validate`'s own callers and
    every other caller that passes no `schema` at all, whose behaviour is
    byte-for-byte unchanged.
    """
    if raw is None or not raw.strip():
        return None, ["response: no payload produced (absent output)"]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, [f"response: malformed JSON ({exc.msg} at line {exc.lineno} column {exc.colno})"]
    errors = (validate_against(schema, payload) if schema is not None
              else validate_payload(hat, payload))
    if errors:
        return None, errors
    if extra_validate is not None:
        extra_errors = extra_validate(payload)
        if extra_errors:
            return None, list(extra_errors)
    return payload, []


def violation_message(hat: str, errors: list[str]) -> str:
    """The ONE human sentence for a hat that broke its response contract,
    quoting the validation error(s) verbatim -- v0.13 task 012 (#92,
    requirement G's second half): a hat that never recovers ends the run
    with this exact text as `loop.py`'s `self._abort_reason` (and therefore
    `status.json`'s `reason` field, `ralphctl status`'s `reason:` line), and
    the SAME function builds the text `state.contract_fault_for_iteration`
    reconstructs from `EVENT_VIOLATION`/`EVENT_AGENT_FAULT` events for
    `ralphctl iteration N` -- one shared derivation, never two hand-kept
    strings that happen to agree today and drift tomorrow.

    Deliberately keeps the exact wording this repository already shipped
    (`'worker' contract violation: $.task_id: expected string, got int`,
    the PRD's own `deck-phase3g` example) rather than inventing new prose:
    `tests/test_interrupt_not_substantive_outcome_task004.py` pins the
    substring `"contract violation"` in `_abort_reason` and must keep
    passing unmodified.
    """
    return f"{hat!r} contract violation: " + "; ".join(errors)


@dataclass
class ContractResult:
    """The outcome of `enforce_contract` for one hat invocation."""

    ok: bool
    payload: dict | None
    attempts: int
    agent_fault: bool
    errors: list[str] = field(default_factory=list)


# A `respond` callable models "ask the hat again": given the 0-based attempt
# index and the validation errors from the previous attempt (`None` on the
# first call), it returns that attempt's raw output. A real caller (task
# 014) re-prompts `pi` with `prior_errors` folded into the next turn; a test
# stub can just replay a fixed sequence and assert it *received* the
# specific error the harness is supposed to carry back.
RespondFn = Callable[[int, "list[str] | None"], "str | None"]


def enforce_contract(
    hat: str,
    respond: RespondFn,
    *,
    max_retries: int,
    state: EventSink | None = None,
    extra_validate: "Callable[[dict], list[str]] | None" = None,
    schema: dict | None = None,
) -> ContractResult:
    """Validate a hat's response, re-prompting on failure.

    Calls `respond` up to `max_retries + 1` times (the first attempt plus
    `max_retries` re-prompts). The first call is `respond(0, None)`; every
    subsequent call carries the previous attempt's validation errors as
    `prior_errors`, which is what a real re-prompt states verbatim to the
    hat. Every failed attempt is `emit()`ed as `EVENT_VIOLATION` to `state`
    (when given) at the moment it is discovered -- not batched, not
    deferred to the end. If every attempt fails, `EVENT_AGENT_FAULT` is
    emitted once and the result comes back with `agent_fault=True`: the
    caller scores this as one iteration against the budget and continues
    per the table, exactly as any other worker/verify/review outcome would.

    `extra_validate` (task 019) is forwarded to `parse_response` unchanged
    -- a payload that is schema-valid but fails this SEMANTIC check (e.g. an
    illegal task-status move) is re-prompted through this exact same loop,
    with the same events, the same retry budget and the same eventual
    agent-fault outcome as a malformed/absent/wrong-shape payload. "Handled
    as task 010 handles any other" is true by construction: there is no
    second retry loop, only a second reason `parse_response` can hand back
    a non-empty `errors` list.

    `schema` (task 014, requirement F.1, #144) is forwarded to
    `parse_response` unchanged too -- an optional override of the schema
    every attempt (not merely a compliant first one) is validated against,
    letting a caller keep the SAME `hat` string (for `respond`'s own
    re-prompt text and this function's own event bookkeeping) while
    checking every attempt against a different, caller-supplied schema.
    `None` (every pre-existing caller) keeps this function's behaviour
    byte-for-byte unchanged.
    """
    if max_retries < 0:
        raise ValueError("max_retries must be >= 0")

    prior_errors: list[str] | None = None
    errors: list[str] = []
    for attempt in range(max_retries + 1):
        raw = respond(attempt, prior_errors)
        payload, errors = parse_response(hat, raw, extra_validate=extra_validate,
                                          schema=schema)
        if not errors:
            return ContractResult(ok=True, payload=payload, attempts=attempt + 1,
                                   agent_fault=False, errors=[])
        if state is not None:
            state.emit(EVENT_VIOLATION, hat=hat, attempt=attempt + 1,
                        maxAttempts=max_retries + 1, errors=list(errors))
        prior_errors = errors

    if state is not None:
        state.emit(EVENT_AGENT_FAULT, hat=hat, attempts=max_retries + 1,
                    errors=list(errors))
    return ContractResult(ok=False, payload=None, attempts=max_retries + 1,
                           agent_fault=True, errors=errors)
