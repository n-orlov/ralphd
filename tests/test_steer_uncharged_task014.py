"""Task 014 (PRD requirement J, #98): the steer hat's own iteration is
**uncharged** -- like the router and the v0.13 contract re-ask, an operator
message must not cost the run a work iteration -- while everything else
about it stays exactly as tasks 009-013 shipped it:

1. **Uncharged.** A steer iteration still gets a real, fully observable
   `iterations/NNNN` slot (`iterations_used` keeps rising, same as any
   other hat) but the run is refunded the instant it returns
   (`LoopSupervisor._refund_iteration("steer")`, called from
   `_step_generic_hat` right after `run_iteration("steer", ...)`), so
   `status.json`'s published `iterationsUsed` and `budget_left()`'s charged
   count never see it -- full parity with `_ask_router`/`_ask_contract_fix`,
   which never touch `iterations_used` at all, just reached by a different
   mechanism because a steer iteration (unlike those two) DOES occupy a
   numbered directory other tooling (task 013's hub/API surfaces) depends
   on being able to list.

2. **Laddered.** A malformed/absent/wrong-shape steer payload rides
   `_step_generic_hat`'s ONE existing `contract_enforcement.enforce_contract`
   call site -- the exact same ladder every other generic hat's payload
   rides -- and an exhausted ladder is recorded as `EVENT_AGENT_FAULT`
   exactly like any other hat's exhaustion. There is no second,
   steer-specific ladder anywhere in this codebase.

3. **Pre-plan steering is left exactly alone.** There is no state to
   manipulate before a plan exists, so `planning`'s prompt still gets the
   operator's steering body injected verbatim (`STEERING_ACTIONABLE_
   PHASES`), unaffected by any of the above.

4. **The read-only context echo of recent operator messages is in EVERY
   hat's prompt.** `build_prompt` appends `_recent_steering_echo()`
   unconditionally -- phase-independent by construction -- so all six hats
   (`planning`/`worker`/`review`/`verify`/`reflect`/`steer`) see the
   operator's recent messages, including ones already settled, and the
   section carries no consumption semantics whatsoever: building a prompt
   never consumes/settles anything, `applied (settled)` is never a claim
   that the tasks a message produced are `done`, and presence in the list
   is not permission to act. The pre-existing filename-only "pending, not
   for this phase" notice (the echo's predecessor, which reached only the
   non-actionable hats and only while a message was still pending) is kept
   alongside it -- it is the thing that tells a non-actionable hat it may
   not act.

Driven the same way `tests/test_steering_priority_routing_task012.py`
already does: a real `LoopSupervisor` with a stubbed `sup.runner`
(`_ScriptedAgent`), never `_run_iteration_once`'s subprocess seam itself.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine import transitions
from ralphd.engine.contract_enforcement import EVENT_AGENT_FAULT, EVENT_VIOLATION
from ralphd.engine.loop import (
    STEERING_ECHO_HEADING, STEERING_ECHO_MAX_BODY_CHARS,
    STEERING_ECHO_MAX_MESSAGES, LoopSupervisor,
)
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

from test_steering_priority_routing_task012 import (
    TASK_1, _ok, _review_verifies, _run_resumed, _steer_settles, _supervisor,
    _worker_completes,
)

STEER_EDGES = transitions.successors("steer", vigilant=False)


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


def _garbage(sup, prompt=""):
    """A steer attempt that satisfies no schema at all -- not even valid
    JSON -- the plainest "malformed" case `contract_enforcement.
    parse_response` names."""
    return _ok("this is not JSON, and steer requires a `settled` boolean")


# ---------------------------------------------------------------------------
# 1. Uncharged: a clean steer pass never reaches iterationsUsed/budget.
# ---------------------------------------------------------------------------


def test_steer_hat_state_is_refunded_like_grace_and_infra():
    assert LoopSupervisor.REFUND_KINDS == ("infra", "grace", "steer")


def test_a_clean_steer_iteration_gets_a_slot_but_is_refunded(tmp_path):
    root = tmp_path / "run"
    sup, final_state = _run_resumed(
        root,
        {"steer": [_steer_settles], "worker": [_worker_completes(TASK_1)],
         "review": [_review_verifies]},
        tasks=[{"id": TASK_1, "status": "pending", "title": "do stuff"}],
        steering=[("do X before anything else", "opX")],
    )
    assert final_state == "succeeded"

    # raw attempts: seeded at 1 (planning), then steer, worker, review = 4
    assert sup.iterations_used == 4
    # exactly one of those four was the steer hat, and it is refunded
    assert sup._steer_refunded == 1
    assert sup._infra_refunded == 0 and sup._grace_refunded == 0

    status = json.loads(sup.run.status_file.read_text())
    assert status["iterationsRefunded"] == {"infra": 0, "grace": 0, "steer": 1}
    # the published, budget-relevant figure excludes the steer slot entirely
    assert status["iterationsUsed"] == 3 == sup.iterations_used_charged
    # ... yet the steer iteration still has its own real, numbered slot --
    # tooling that lists iterations/NNNN must still find it there.
    assert sorted(p.name for p in (root / "iterations").iterdir()) == \
        ["0002", "0003", "0004"]


def test_a_steer_iteration_never_moves_budget_left(tmp_path):
    """The literal comparison the PRD draws: a job with a 3-iteration
    budget that needs one planning-equivalent seed iteration plus a
    worker and a review to finish must still finish -- one operator
    message and its steer pass along the way must not eat any of those
    3 charged iterations."""
    root = tmp_path / "run"
    sup, final_state = _run_resumed(
        root,
        {"steer": [_steer_settles], "worker": [_worker_completes(TASK_1)],
         "review": [_review_verifies]},
        tasks=[{"id": TASK_1, "status": "pending", "title": "do stuff"}],
        steering=[("hurry up", "opHurry")],
        iterations=3,
    )
    assert final_state == "succeeded"
    status = json.loads(sup.run.status_file.read_text())
    assert status["iterationsUsed"] == 3
    assert sup.iterations_used == 4, "the steer pass still got its own slot"


def test_repeated_steer_passes_are_each_individually_refunded(tmp_path):
    """Three separate operator messages that each land while `worker` is
    in flight (three separate `worker -> steer -> worker` round trips)
    refund THREE times, one per steer pass -- not once for "the steer
    phase" as a whole."""
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, {"steer": [_steer_settles, _steer_settles,
                                        _steer_settles]})
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    sup.iterations_used = 1

    edges = STEER_EDGES
    for name in ("alpha", "beta", "gamma"):
        sup.run.add_steering(f"message {name}", name)
        outcome = asyncio.run(sup._step_generic_hat("steer", edges))
        assert outcome == ("advance", "worker", "steer_settled")

    assert sup._steer_refunded == 3
    assert sup.iterations_used == 4  # seeded 1 + three steer passes
    assert sup.iterations_used_charged == 1


# ---------------------------------------------------------------------------
# 2. Laddered: malformed steer output rides the EXISTING ladder, no second
#    ladder, and the exhaustion is an ordinary agent fault.
# ---------------------------------------------------------------------------


def test_a_malformed_steer_attempt_is_repaired_through_ask_contract_fix(
        tmp_path):
    """The first attempt is garbage; `enforce_contract`'s own re-prompt
    loop (never a bespoke steer-specific one) repairs it through the
    UNCHARGED `_ask_contract_fix` channel, and the steer hat still only
    costs ONE (refunded) iteration overall."""
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, {"steer": [_garbage, _steer_settles]},
                      contract_retry_max=1)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    sup.run.add_steering("please settle this", "opFix")
    sup.iterations_used = 1

    outcome = asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))
    assert outcome == ("advance", "worker", "steer_settled")

    violations = _events(root, EVENT_VIOLATION)
    assert len(violations) == 1 and violations[0]["hat"] == "steer"
    assert _events(root, EVENT_AGENT_FAULT) == [], (
        "the ladder recovered on the re-ask -- this must not also be "
        "recorded as an exhausted (agent-fault) ladder")

    # exactly one steer ITERATION (not one per ladder attempt) was charged
    # and then refunded -- the repair rode inside the same attempt.
    assert sup.iterations_used == 2
    assert sup._steer_refunded == 1
    agent = sup.runner
    assert agent.calls["steer"] == 2, (
        "one run_iteration() call plus one _ask_contract_fix repair, both "
        "counted the same way this stub counts every runner.run() call")


def test_an_exhausted_steer_ladder_scores_one_agent_fault_and_retries_in_place(
        tmp_path):
    """`contract_retry_max=1` means 2 total attempts per ladder call. Both
    fail here, so the ladder is exhausted and `EVENT_AGENT_FAULT` fires --
    exactly the outcome `contract_enforcement.enforce_contract` documents
    for ANY hat, never a special steer-only path. Budget still remains
    (steer's own attempts are refunded), so `_step_generic_hat` retries
    the SAME steer call in place, and the SECOND full pass succeeds."""
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(
        root, {"steer": [_garbage, _garbage, _steer_settles]},
        contract_retry_max=1)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    sup.run.add_steering("please settle this too", "opRetry")
    sup.iterations_used = 1

    outcome = asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))
    assert outcome == ("advance", "worker", "steer_settled")

    violations = _events(root, EVENT_VIOLATION)
    assert len(violations) == 2 and {v["hat"] for v in violations} == {"steer"}
    agent_faults = _events(root, EVENT_AGENT_FAULT)
    assert len(agent_faults) == 1 and agent_faults[0]["hat"] == "steer"

    # two full steer ITERATIONS happened (the exhausted one, then the
    # retry-in-place that succeeded) -- both refunded, neither charged.
    assert sup.iterations_used == 3
    assert sup._steer_refunded == 2
    status = json.loads(sup.run.status_file.read_text())
    assert status["iterationsUsed"] == 1 == sup.iterations_used_charged


def test_no_second_ladder_module_is_involved_for_steer():
    """There is exactly one call site in `_step_generic_hat` that reaches
    `contract_enforcement.enforce_contract`, and it is shared by every
    generic hat including `steer` -- proven at the source level, not
    merely by absence of a failure above."""
    import inspect

    from ralphd.engine import loop as loop_module

    src = inspect.getsource(loop_module.LoopSupervisor._step_generic_hat)
    assert src.count("contract_enforcement.enforce_contract,") == 1, (
        "steer must ride the ONE existing ladder call site, never a "
        "second copy of it")


# ---------------------------------------------------------------------------
# 3. Pre-plan steering is untouched: planning still gets the full body.
# ---------------------------------------------------------------------------


def test_planning_still_gets_the_full_steering_body_injected(tmp_path):
    root = tmp_path / "run"
    run = RunDir(root=root)
    run.update_status(state="starting")
    run.add_steering("prefer approach B over approach A", "opPlanPref")

    sup = _supervisor(root, {})
    sup.run = run
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})

    prompt = sup.build_prompt("planning")
    assert "prefer approach B over approach A" in prompt
    assert "MUST take priority" in prompt


# ---------------------------------------------------------------------------
# 4. The read-only context echo of recent operator messages, in EVERY hat's
#    prompt, carrying no consumption semantics.
# ---------------------------------------------------------------------------

# Every hat there is (`transitions.HAT_SCHEMAS`) and the prompt file each one
# is built from -- `verify` is the only hat whose prompt name differs from its
# phase name (`prompts/task-verify.md`, see `_step_verify`'s
# `prompt_name="task-verify"`). Derived from the shipped hat table itself
# (not hand-listed) so a new hat (e.g. task 018/020's `plan_review`/
# `verdict_gate`) is picked up automatically instead of silently passing --
# only the `verify` exception is hand-carried, because nothing in the table
# names a hat's prompt file.
HAT_PROMPT_NAMES = {hat: ("task-verify" if hat == "verify" else None)
                    for hat in transitions.HAT_SCHEMAS}


def test_the_hat_list_this_module_checks_is_the_whole_hat_table():
    """So "every hat's prompt" cannot quietly become "every hat we happened
    to list here" when a later task adds a seventh hat."""
    assert set(HAT_PROMPT_NAMES) == set(transitions.HAT_SCHEMAS)


def _echo(prompt: str) -> str:
    """The read-only echo section of a prompt (from its heading to the end)."""
    assert STEERING_ECHO_HEADING in prompt, "no read-only echo in this prompt"
    return prompt[prompt.index(STEERING_ECHO_HEADING):]


def _prompt_supervisor(root: Path) -> LoopSupervisor:
    sup = _supervisor(root, {})
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    return sup


def test_every_hat_prompt_echoes_a_message_that_is_already_settled(tmp_path):
    """The property the requirement names: RECENT operator messages, in
    EVERY hat's prompt. A message that has been consumed/settled is exactly
    the case the old filename-only notice dropped on the floor -- it
    vanished from every prompt the moment it was applied, so `review`,
    `verify` and `reflect` could never see what the operator had just
    asked for."""
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _prompt_supervisor(root)
    sup.run.add_steering("do not touch the migration yet", "opEcho")
    sup.run.consume_steering(sup.run.pending_steering(), 2)
    assert sup.run.pending_steering() == [], "precondition: already settled"

    for phase, prompt_name in HAT_PROMPT_NAMES.items():
        prompt = sup.build_prompt(phase, prompt_name=prompt_name)
        echo = _echo(prompt)
        assert "001-opEcho.md" in echo, phase          # the file name
        assert "do not touch the migration yet" in echo, phase   # the body
        assert "applied (settled)" in echo, phase      # its harness state
        # ... and nothing anywhere claims this phase may act on it: there is
        # nothing pending at all, so no actionable section exists.
        assert "MUST take priority" not in prompt, phase
        assert "pending, not for this phase" not in prompt, phase


def test_the_echo_carries_no_consumption_semantics(tmp_path):
    """Reading (or building) the echo must never consume, settle, or
    re-present anything, and it must not read as permission to act:
    consumption is exclusively `RunDir.consume_steering`, called only after
    a real actionable-phase iteration finishes cleanly, and `settled` is
    never `done` (requirement I -- `tasks.json` is the single source of
    truth for that)."""
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _prompt_supervisor(root)
    sup.run.add_steering("do not touch the migration yet", "opEcho")
    sup.run.add_steering("and land the fix today", "opToday")
    before = [p.name for p in sup.run.pending_steering()]

    for phase, prompt_name in HAT_PROMPT_NAMES.items():
        echo = _echo(sup.build_prompt(phase, prompt_name=prompt_name))
        assert "read-only" in echo and "no consumption semantics" in echo
        assert "not permission to act" in echo
        assert "single source of truth" in echo
        assert str(sup.run.tasks_file) in echo
        assert "pending" in echo  # each message's state, plainly labelled

    # building all six prompts changed nothing about what is pending, and
    # wrote no consumption record at all.
    assert [p.name for p in sup.run.pending_steering()] == before == [
        "001-opEcho.md", "002-opToday.md"]
    assert not (root / "steering" / ".consumed.json").exists()


def test_a_non_actionable_phase_still_gets_the_pending_not_for_this_phase_notice(
        tmp_path):
    """The echo's predecessor is KEPT, because it says the one thing the echo
    deliberately does not: whether THIS hat may act.

    Assertion changed by task 014's rework: this test used to assert the
    body of a pending message appears NOWHERE in a `review` prompt. That
    claim contradicted requirement J's echo (recent operator messages in
    every hat's prompt), so it is replaced by a stricter pair -- the body
    appears ONLY inside the read-only echo section and never in the
    act-on-this part of the prompt, and the filename notice is still there.
    """
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _prompt_supervisor(root)
    sup.run.add_steering("do not touch the migration yet", "opEcho")

    prompt = sup.build_prompt("review")
    head, echo = prompt.split(STEERING_ECHO_HEADING, 1)
    assert "pending, not for this phase" in head
    assert "opEcho" in head                                  # named
    assert "do not touch the migration yet" not in head, (
        "a non-actionable hat must not be handed the body as something to "
        "act on -- only as read-only context, below the echo heading")
    assert "do not touch the migration yet" in echo
    assert "MUST take priority" not in prompt

    # read-only: nothing about building this prompt may mark anything
    # consumed -- consumption is exclusively `RunDir.consume_steering`,
    # called only after a real actionable-phase iteration finishes cleanly.
    assert sup.run.pending_steering() != []


def test_the_echo_is_capped_at_the_most_recent_messages_and_says_so(tmp_path):
    """A job that collects dozens of steers must not have its prompt crowded
    out by them: the echo keeps the most recent
    `STEERING_ECHO_MAX_MESSAGES` and states the cap instead of applying it
    silently."""
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _prompt_supervisor(root)
    total = STEERING_ECHO_MAX_MESSAGES + 2
    for i in range(1, total + 1):
        sup.run.add_steering(f"message body number {i}", f"m{i}")

    echo = _echo(sup.build_prompt("reflect"))
    assert f"most recent of {total}" in echo
    for i in (1, 2):        # the two oldest are dropped, by name and body
        assert f"00{i}-m{i}.md" not in echo
        assert f"message body number {i}\n" not in echo
    kept = [i for i in range(3, total + 1)]
    positions = [echo.index(f"message body number {i}") for i in kept]
    assert positions == sorted(positions), "oldest first, arrival order"


def test_a_very_long_message_is_truncated_with_a_pointer_to_the_file(tmp_path):
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _prompt_supervisor(root)
    sup.run.add_steering("x" * (STEERING_ECHO_MAX_BODY_CHARS + 500), "opLong")

    echo = _echo(sup.build_prompt("worker"))
    assert f"truncated at {STEERING_ECHO_MAX_BODY_CHARS} characters" in echo
    assert str(root / "steering" / "001-opLong.md") in echo


# ---------------------------------------------------------------------------
# 5. Unchargedness holds across a LATER infra fault, in the published figure.
# ---------------------------------------------------------------------------


def _infra_fault(sup, prompt=""):
    """What the infra classifier reads as an outage, not an agent fault:
    a connection error after a non-instant duration."""
    r = IterationResult(exit_code=0)
    r.error_message = "Connection error."
    r.duration_s = 30.0
    return r


@pytest.mark.asyncio
async def test_an_infra_fault_after_a_steer_pass_never_recharges_it(tmp_path):
    """The published `iterationsUsed` -- the figure the operator reads and
    the one a budget change is validated against -- must never re-charge a
    refunded steer pass. The infra-retry path used to publish
    `iterations_used - _infra_refunded` by hand, silently putting the steer
    slot back on the bill; it now publishes the single charged-count
    expression, `iterations_used_charged`."""
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    seen: list[int] = []

    def _capture_then_complete(sup, prompt=""):
        return _worker_completes(TASK_1)(sup, prompt)

    sup = _supervisor(root, {"steer": [_steer_settles],
                             "worker": [_infra_fault, _capture_then_complete]})

    async def capture_backoff(seconds):
        # runs between the infra classification's own status write and the
        # retry attempt -- i.e. exactly on the figure that write published.
        seen.append(json.loads(sup.run.status_file.read_text())[
            "iterationsUsed"])
        return seconds, False

    sup._wait_out_backoff = capture_backoff  # type: ignore[method-assign]
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    sup.run.add_steering("one small thing", "opCharge")
    sup.iterations_used = 1

    outcome = await sup._step_generic_hat("steer", STEER_EDGES)
    assert outcome == ("advance", "worker", "steer_settled")
    assert sup._steer_refunded == 1

    result = await sup._run_iteration_with_infra_retry("worker", "", None)
    assert result.final_text, "the wrapper rode the outage out"
    assert sup._infra_refunded == 1

    # raw attempts: the seeded 1 + steer + 2 worker attempts = 4. Charged: the
    # seeded one plus the one clean worker pass = 2 (the steer pass and the
    # infra-faulted attempt are both refunded). The mid-outage write must
    # already agree with that arithmetic.
    assert sup.iterations_used == 4
    assert sup.iterations_used_charged == 2
    assert seen == [1], (
        "the figure published between the infra fault and its retry "
        "re-charged the refunded steer pass (seeded 1 + nothing chargeable "
        "yet = 1)")
    status = json.loads(sup.run.status_file.read_text())
    assert status["iterationsUsed"] == 2
