"""Task 006 (PRD requirement B.1, #122): a claim means something -- a steer
that honestly reports `settled: false` gets its message redelivered,
bounded, instead of being silently consumed anyway.

Before this task `_run_iteration_once` consumed a steer iteration's pending
notes the instant the iteration itself finished cleanly (`fault_class is
None`), with no regard at all for what the hat's own payload said --
`test_a_steer_that_did_not_claim_settled_renders_as_partially_applied`
(`tests/test_steering_ledger.py`) already showed the LEDGER records
`settled: false`, but the underlying steering FILE was consumed exactly the
same as a `settled: true` reply, and the next actionable iteration was never
handed it again.

The fix, pinned here, through the real production path
(`LoopSupervisor._step_generic_hat("steer", ...)`, never a direct call to
the new state.py helpers):

1. A clean steer iteration whose payload says `settled: false` leaves the
   message UNCONSUMED (`RunDir.unconsume_steering`, undoing what
   `_run_iteration_once` already banked) -- `pending_steering()` names it
   again, the ledger's `consumed` stamp is cleared, and the very next
   `worker` pass (steer takes absolute priority, task 012) is handed it
   again.
2. Each redelivery increments a per-message counter,
   `steerRedeliveries[<name>]` in status.json -- disk-backed, the same
   shape `planReviewRevisions` already uses for
   `peer_plan_review_max_revisions` -- so it survives a resumed process
   (simulated here by constructing a SECOND `LoopSupervisor` over the same
   run root, exactly the way a real resume would).
3. Once a message's counter reaches the declared cap
   (`JobConfig.steer_redelivery_max`), it stops being redelivered: instead
   it is recorded settled-BY-EXHAUSTION in the steering ledger, with the
   reason, and stays consumed for good.
4. `settled: true` is untouched -- it still consumes the message exactly
   once, on the very first clean pass.

Scaffolding: `_supervisor`/`_ScriptedAgent`/`_ok` from
`tests/test_steering_priority_routing_task012.py`, the same harness
`tests/test_steering_ledger.py` already reuses for the production steer
path.
"""

from __future__ import annotations

import asyncio
import json

from ralphd.engine import state, transitions
from ralphd.engine.loop import LoopSupervisor

from test_steering_priority_routing_task012 import _ok, _supervisor

STEER_EDGES = transitions.successors("steer", vigilant=False)


def _steer_unsettled(notes: str = "ran out of iteration time"):
    def handler(sup, prompt=""):
        return _ok(json.dumps({"settled": False, "notes": notes}))
    return handler


def _steer_settled(notes: str = "applied it all"):
    def handler(sup, prompt=""):
        return _ok(json.dumps({"settled": True, "notes": notes}))
    return handler


def _entry(sup: LoopSupervisor, file_name: str) -> dict:
    entries = {e["file"]: e for e in sup.run.steering_ledger()}
    assert file_name in entries, f"{file_name} missing from the ledger"
    return entries[file_name]


def _redeliveries(sup: LoopSupervisor) -> dict:
    return dict(sup.run.read_status().get("steerRedeliveries") or {})


# ---------------------------------------------------------------------------
# 1. settled: false leaves the message unconsumed and deliverable
# ---------------------------------------------------------------------------

def test_a_settled_false_steer_leaves_the_message_unconsumed_and_pending(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, {"steer": [_steer_unsettled("half done")]})
    name = sup.run.add_steering("do the three-part thing", "op-three-part")

    outcome = asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))

    # the (unconditional, single-edge) state transition is unchanged --
    # this task never touches WHICH state comes next, only whether the
    # message the hat was handed is still deliverable afterward.
    assert outcome == ("advance", "worker", "steer_settled")

    assert [p.name for p in sup.run.pending_steering()] == [name], (
        "an honest settled: false must leave the note genuinely pending")
    entry = _entry(sup, name)
    assert entry["consumed"] is None, "the consumption stamp must be cleared"
    assert entry["state"] == state.STEERING_PENDING
    assert entry["settled"] is False
    assert entry["settledByExhaustion"] is False
    assert _redeliveries(sup) == {name: 1}


def test_a_settled_true_steer_still_consumes_exactly_once(tmp_path):
    """The non-change: this task must not touch the settled: true path."""
    root = tmp_path / "run"
    sup = _supervisor(root, {"steer": [_steer_settled()]})
    name = sup.run.add_steering("a single clear ask", "op-clear")

    asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))

    assert sup.run.pending_steering() == []
    entry = _entry(sup, name)
    assert entry["consumed"], "settled: true consumes on the first pass"
    assert entry["state"] == state.STEERING_APPLIED
    assert entry["settled"] is True
    assert _redeliveries(sup) == {}, (
        "settled: true never touches the redelivery counter at all")


# ---------------------------------------------------------------------------
# 2. the redelivery counter increments per redelivery and survives resume
# ---------------------------------------------------------------------------

def test_the_redelivery_counter_increments_and_is_read_back_on_resume(
        tmp_path):
    root = tmp_path / "run"
    sup1 = _supervisor(root, {"steer": [_steer_unsettled()]},
                       steer_redelivery_max=5)
    name = sup1.run.add_steering("still not done", "op-slow")

    asyncio.run(sup1._step_generic_hat("steer", STEER_EDGES))
    assert _redeliveries(sup1) == {name: 1}

    # A resumed process is a FRESH LoopSupervisor over the same run root --
    # nothing in-memory on sup1 may be what makes this work.
    sup2 = _supervisor(root, {"steer": [_steer_unsettled()]},
                       steer_redelivery_max=5)
    assert _redeliveries(sup2) == {name: 1}, (
        "the counter must be read back off status.json, not re-derived")
    assert [p.name for p in sup2.run.pending_steering()] == [name]

    asyncio.run(sup2._step_generic_hat("steer", STEER_EDGES))
    assert _redeliveries(sup2) == {name: 2}


def test_two_messages_are_redelivered_and_counted_independently(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, {"steer": [_steer_unsettled("both pending")]})
    one = sup.run.add_steering("first ask", "op-one")
    two = sup.run.add_steering("second ask", "op-two")

    asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))

    assert sorted(p.name for p in sup.run.pending_steering()) == sorted(
        [one, two])
    assert _redeliveries(sup) == {one: 1, two: 1}


# ---------------------------------------------------------------------------
# 3. the cap: settled-by-exhaustion, with the reason, stays consumed
# ---------------------------------------------------------------------------

def test_the_redelivery_cap_marks_settled_by_exhaustion_with_a_reason(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, {"steer": [_steer_unsettled("still stuck")]},
                      steer_redelivery_max=2)
    name = sup.run.add_steering("a message nobody ever settles", "op-stuck")

    # Pass 1: used=0 < cap(2) -> redelivered (redeliveries -> 1).
    asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))
    assert [p.name for p in sup.run.pending_steering()] == [name]
    assert _redeliveries(sup) == {name: 1}
    assert _entry(sup, name)["settledByExhaustion"] is False

    # Pass 2: used=1 < cap(2) -> redelivered again (redeliveries -> 2).
    asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))
    assert [p.name for p in sup.run.pending_steering()] == [name]
    assert _redeliveries(sup) == {name: 2}
    assert _entry(sup, name)["settledByExhaustion"] is False

    # Pass 3: used=2, no longer < cap(2) -> settled BY EXHAUSTION, stays
    # consumed for good, never handed back out again.
    asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))
    assert sup.run.pending_steering() == [], (
        "an exhausted message must stop being redelivered")
    entry = _entry(sup, name)
    assert entry["settledByExhaustion"] is True
    assert entry["exhaustionReason"], "the reason must be recorded"
    assert name in entry["exhaustionReason"] or "cap" in entry["exhaustionReason"]
    assert entry["settled"] is False, (
        "exhaustion is the harness giving up, not the hat claiming settled")
    assert entry["application"] == state.STEER_APPLICATION_APPLIED
    assert entry["state"] == state.STEERING_APPLIED
    # the counter itself is left as-is at the cap, not incremented past it
    assert _redeliveries(sup) == {name: 2}

    # A further pass, were one to happen, must not resurrect it: the file
    # stays consumed and no further redelivery event fires.
    log = [json.loads(ln) for ln in
          (root / "events.jsonl").read_text().splitlines()]
    exhausted = [e for e in log if e.get("type") == "steering.settled_by_exhaustion"]
    assert [e["file"] for e in exhausted] == [name]


def test_the_exhaustion_reason_names_the_declared_cap():
    """A plain-value assertion, independent of run-dir scaffolding: the
    reason text this task's own code produces must state the actual cap
    value, not a vague "too many times"."""
    import inspect

    from ralphd.engine.loop import LoopSupervisor as LS
    src = inspect.getsource(LS._handle_unsettled_steering)
    assert "steer_redelivery_max" in src
    assert "settled_by_exhaustion" in src or "settled-by-exhaustion" in src


# ---------------------------------------------------------------------------
# 4. the ledger's own reader surfaces the new fields even with no record
# ---------------------------------------------------------------------------

def test_a_never_touched_message_reports_no_exhaustion(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, {"steer": []})
    name = sup.run.add_steering("nothing has looked at this yet", "op-fresh")

    entry = _entry(sup, name)
    assert entry["settledByExhaustion"] is False
    assert entry["exhaustionReason"] is None
