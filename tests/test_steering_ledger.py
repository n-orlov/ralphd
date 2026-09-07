"""Task 034 (operator-inserted): the STEERING LEDGER, and its surfacing to
every following iteration of every hat.

The gap this closes was observed in this very run: an operator steer is
consumed once by the steer hat, and afterwards its content survives only as
standing-rules/notes prose. The verify and review hats had no first-class
view of what was steered, what the steer hat applied and what is still
pending -- so a validator could fail work for conforming to a steer it never
saw, or call a deliverable stale for not reflecting a message nobody had
actioned yet.

Pinned here, each as its own named test:

1. A consumed-and-settled steer renders as `applied` WITH the steer hat's
   own settlement notes -- driven through the REAL production path
   (`LoopSupervisor._step_generic_hat("steer", ...)`, the only place the
   `settled` payload is read), never by calling the recorder directly.
2. A steering file dropped into `steering/` renders as `pending` in the very
   next iteration's context, with no steer iteration and no record written.
3. The worker, verify and review prompts each carry the ledger section --
   `build_prompt` is the one join point, so no hat can be forgotten.
4. The semantics are in the rendered text: the ledger INFORMS (do not fail
   work for conforming to an applied steer, weigh a pending one when judging
   staleness) but a steer is NOT a waiver -- only `/config/amendments`
   waives. The same sentence is asserted present in `prompts/task-verify.md`
   and `prompts/review.md` by `tests/test_prompt_steering_durability.py`,
   the prompt-asserting module for steering prompt rules.

The `_supervisor`/`_ok` scaffolding is `tests/test_steering_priority_routing_
task012.py`'s own, the same import `tests/test_worker_hat.py`
makes -- one scripted-agent harness for every steer-hat production-path test.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import state, transitions
from ralphd.engine.loop import (
    STEERING_LEDGER_HEADING,
    STEERING_LEDGER_NOT_A_WAIVER,
)
from ralphd.engine.state import RunDir, atomic_write_json

from test_steer import _ok, _supervisor

STEER_EDGES = transitions.successors("steer", vigilant=False)

PROMPTS_DIR = Path(state.__file__).resolve().parents[1] / "prompts"


def _seed(root: Path, handlers: dict):
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": []})
    sup = _supervisor(root, handlers)
    return sup


def _entry(sup, file_name: str) -> dict:
    entries = {e["file"]: e for e in sup.run.steering_ledger()}
    assert file_name in entries, f"{file_name} missing from the ledger"
    return entries[file_name]


# ---------------------------------------------------------------------------
# 1. a consumed-and-settled steer renders as `applied` with its notes
# ---------------------------------------------------------------------------

def test_a_consumed_and_settled_steer_renders_as_applied_with_its_notes(tmp_path):
    root = tmp_path / "run"
    notes = "inserted task 042 and pinned the standing rule about pushes"

    def _steer_handler(sup, prompt=""):
        return _ok(json.dumps({"settled": True, "notes": notes}))

    sup = _seed(root, {"steer": [_steer_handler]})
    name = sup.run.add_steering("stop pushing to main", "op-stop-pushing")

    outcome = asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))
    assert outcome == ("advance", "worker", "steer_settled")

    entry = _entry(sup, name)
    assert entry["application"] == state.STEER_APPLICATION_APPLIED
    assert entry["settled"] is True
    assert entry["notes"] == notes
    assert entry["received"], "the ledger records when the message arrived"
    assert entry["consumed"], "a cleanly finished steer iteration consumes it"
    assert entry["state"] == state.STEERING_APPLIED

    # ... and that is what the NEXT iteration of any hat actually reads.
    prompt = sup.build_prompt("verify", prompt_name="task-verify")
    assert STEERING_LEDGER_HEADING in prompt
    assert name in prompt
    assert "application: applied" in prompt
    assert notes in prompt


def test_a_steer_that_did_not_claim_settled_renders_as_partially_applied(tmp_path):
    root = tmp_path / "run"

    def _steer_handler(sup, prompt=""):
        return _ok(json.dumps({"settled": False,
                               "notes": "half of it needs the operator"}))

    sup = _seed(root, {"steer": [_steer_handler]})
    name = sup.run.add_steering("three separate asks", "op-three-asks")

    asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))

    entry = _entry(sup, name)
    assert entry["application"] == state.STEER_APPLICATION_PARTIAL
    assert entry["settled"] is False
    assert "half of it needs the operator" in entry["notes"]
    assert "application: partially-applied" in sup.build_prompt("review")


def test_the_hats_own_application_claim_is_what_the_ledger_records(tmp_path):
    """`HAT_SCHEMAS["steer"]`'s optional `application` field: a hat that
    settled the message but applied only part of it says so directly."""
    root = tmp_path / "run"

    def _steer_handler(sup, prompt=""):
        return _ok(json.dumps({"settled": True,
                               "application": "partially-applied",
                               "notes": "one ask needs a host-side edit"}))

    sup = _seed(root, {"steer": [_steer_handler]})
    name = sup.run.add_steering("two asks, one impossible", "op-two-asks")

    asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))

    entry = _entry(sup, name)
    assert entry["settled"] is True
    assert entry["application"] == state.STEER_APPLICATION_PARTIAL


# ---------------------------------------------------------------------------
# 2. an unconsumed steering file is `pending` in the very next iteration
# ---------------------------------------------------------------------------

def test_a_dropped_steering_file_renders_as_pending_in_the_next_iteration(tmp_path):
    """No steer iteration, no ledger record -- the file alone is enough."""
    root = tmp_path / "run"
    sup = _seed(root, {"steer": []})
    name = sup.run.add_steering("size the remaining tasks smaller", "op-sizing")

    assert not state.steering_ledger_path(root).exists(), (
        "the pending state must be derived from the file, not from a record")

    entry = _entry(sup, name)
    assert entry["application"] == state.STEER_APPLICATION_PENDING
    assert entry["settled"] is False
    assert entry["consumed"] is None
    assert entry["state"] == state.STEERING_PENDING

    prompt = sup.build_prompt("worker")
    assert STEERING_LEDGER_HEADING in prompt
    assert name in prompt
    assert "application: pending" in prompt
    assert "not yet consumed" in prompt


def test_a_run_with_no_steering_renders_no_ledger_section(tmp_path):
    sup = _seed(tmp_path / "run", {"steer": []})
    assert sup.run.steering_ledger() == []
    assert sup._steering_ledger_context() == ""
    assert STEERING_LEDGER_HEADING not in sup.build_prompt("worker")


# ---------------------------------------------------------------------------
# 3. every hat's context carries the section
# ---------------------------------------------------------------------------

def test_the_worker_verify_and_review_contexts_each_contain_the_ledger(tmp_path):
    root = tmp_path / "run"

    def _steer_handler(sup, prompt=""):
        return _ok(json.dumps({"settled": True, "notes": "applied it all"}))

    sup = _seed(root, {"steer": [_steer_handler]})
    settled_name = sup.run.add_steering("the first ask", "op-first")
    asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))
    pending_name = sup.run.add_steering("the second ask", "op-second")

    for phase, prompt_name in (("worker", "worker"),
                               ("verify", "task-verify"),
                               ("review", "review"),
                               ("planning", "planning"),
                               ("reflect", "reflect")):
        prompt = sup.build_prompt(phase, prompt_name=prompt_name)
        assert STEERING_LEDGER_HEADING in prompt, phase
        assert settled_name in prompt, phase
        assert pending_name in prompt, phase
        assert "application: applied" in prompt, phase
        assert "application: pending" in prompt, phase


# ---------------------------------------------------------------------------
# 4. semantics stated, not changed
# ---------------------------------------------------------------------------

def test_the_rendered_ledger_states_that_it_informs_and_does_not_waive(tmp_path):
    root = tmp_path / "run"
    sup = _seed(root, {"steer": []})
    sup.run.add_steering("a standing instruction", "op-standing")

    context = sup._steering_ledger_context()
    assert STEERING_LEDGER_NOT_A_WAIVER in context
    assert "/config/amendments" in context
    assert "do not fail work for conforming to" in context.lower()
    assert "weigh a pending steer" in context.lower()
    assert "consumes and settles nothing" in context.lower()


def test_the_not_a_waiver_sentence_is_in_both_verify_and_review_prompts():
    """The same sentence the engine renders, stated in the two validating
    hats' own prompts -- asserted here and, per this task's criteria, by
    `tests/test_prompt_steering_durability.py` as well."""
    for name in ("task-verify", "review"):
        text = (PROMPTS_DIR / f"{name}.md").read_text()
        assert "A steer is NOT a waiver" in text, name
        assert "/config/amendments" in text, name
        assert "Steering ledger" in text, name


def test_pending_is_never_something_a_steer_hat_may_claim():
    """`pending` is the ABSENCE of a settlement record, so the hat's own
    optional `application` enum must not offer it."""
    enum = transitions.HAT_SCHEMAS["steer"]["properties"]["application"]["enum"]
    assert state.STEER_APPLICATION_PENDING not in enum
    assert set(enum) == {state.STEER_APPLICATION_APPLIED,
                         state.STEER_APPLICATION_PARTIAL}
    assert set(state.STEER_APPLICATION_STATUSES) == {
        state.STEER_APPLICATION_APPLIED,
        state.STEER_APPLICATION_PARTIAL,
        state.STEER_APPLICATION_PENDING,
    }


def test_the_ledger_survives_a_malformed_record_file(tmp_path):
    """A lost or corrupt `.ledger.json` degrades to 'nothing applied yet',
    never to a crash in the prompt builder every hat goes through."""
    root = tmp_path / "run"
    sup = _seed(root, {"steer": []})
    name = sup.run.add_steering("an ask", "op-ask")
    state.steering_ledger_path(root).write_text("{not json")

    entry = _entry(sup, name)
    assert entry["application"] == state.STEER_APPLICATION_PENDING
    assert STEERING_LEDGER_HEADING in sup.build_prompt(
        "verify", prompt_name="task-verify")
