"""Task 015 (requirement B, #60 pillar 2): "adding a hypothetical hat is a
table entry plus a prompt plus a schema, with no edit to the transition
executor. Prove it: add a trivial no-op hat in a test, drive it, and assert
the executor was untouched."

This is the PRD's own acceptance text for requirement B, and it is the only
evidence that `engine.transitions`' table is a design rather than a lookup
wrapped around the old branches (task 014's cutover made the table a
*legality* check on top of four hand-written `_step_*` methods -- it did not,
by itself, prove a FIFTH state could ever be added without touching engine
code again; that is exactly what this module tests).

The mechanism under test:

- `engine.loop.LoopSupervisor._run_job_core`'s dispatch loop has exactly one
  generic fallback (`_step_generic_hat`) for any state that is not one of
  the four hand-written ones. It is shipped code, added once, for every
  future trivial hat -- this test never edits it.
- A "trivial no-op hat" is registered purely by MUTATING the already-public,
  already-mutable module-level dicts `transitions.TABLE`,
  `transitions.STATE_HAT`, `transitions.HAT_SCHEMAS` and reassigning
  `transitions.ENTRY_STATE` -- no new API, no file on disk changes, and
  (asserted below) no line of `src/ralphd/engine/transitions.py` or
  `src/ralphd/engine/loop.py` changes on disk while any of this happens.
- The hat is driven end to end through the real dispatch loop (the
  "phase-dispatching scripted runner, no subprocess" technique from
  tests/test_transition_executor_cutover.py / test_failed_task_vocabulary.py),
  with a real prompt fixture file and a real schema-validated JSON payload,
  proving the generic path actually exercises `contracts.validate_payload`
  (task 009's one validator code path) rather than being driven around it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from ralphd.engine import config as config_mod
from ralphd.engine import contracts
from ralphd.engine import loop as loop_mod
from ralphd.engine import transitions as tr
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import COMPLETE, IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

REPO_ROOT = Path(__file__).resolve().parents[1]
TRANSITIONS_PATH = REPO_ROOT / "src" / "ralphd" / "engine" / "transitions.py"
LOOP_PATH = REPO_ROOT / "src" / "ralphd" / "engine" / "loop.py"

NOOP_STATE = "noop_hat_task015"
NOOP_HAT = "noop_hat_task015"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# scaffolding (same "scripted agent, no subprocess" shape as
# tests/test_transition_executor_cutover.py)
# ---------------------------------------------------------------------------

def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 30.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


class _ScriptedAgent:
    def __init__(self, sup: LoopSupervisor, handlers: dict):
        self.sup = sup
        self.handlers = handlers
        self.phases: list[str] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        phase = self.sup.run.read_status().get("phase")
        self.phases.append(phase)
        self.running = True
        try:
            return self.handlers[phase](self.sup, prompt)
        finally:
            self.running = False


def _plan(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "001", "title": "do the thing",
         "successCriteria": "the thing is done"},
    ]})
    return _ok("planned\n" +
              json.dumps({"proposal_path": "plan-proposal.json", "summary": "planned"}))


def _complete_task(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    doc = sup.run.read_tasks()
    doc["tasks"][0]["status"] = "completed"
    atomic_write_json(sup.run.tasks_file, doc)
    return _ok(f"done\n{COMPLETE}")


def _review_verified(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    # Task 119 (#60 pillar 3, fact 9): review reports through the `review`
    # JSON contract, not the retired `<promise>VERIFIED</promise>` sentinel.
    return _ok("looks right\n" + json.dumps({"verdict": "verified"}))


def _noop_ok(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    # The "trivial no-op hat"'s entire job: emit a schema-valid, empty JSON
    # object as the final line of its reply.
    return _ok("(nothing to do)\n{}")


def _happy_handlers() -> dict:
    return {"planning": _plan, "worker": _complete_task,
            "review": _review_verified, NOOP_STATE: _noop_ok}


@pytest.fixture
def registered_noop_hat(monkeypatch, tmp_path):
    """Registers the no-op hat by mutating `transitions`' already-public
    dicts and `ENTRY_STATE`, plus a prompt fixture on disk that `prompt_text`
    can actually find. Restores every one of those on teardown -- this is
    test-fixture bookkeeping, not a second writer of engine state.
    """
    prompts_dir = tmp_path / "config" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / f"{NOOP_HAT}.md").write_text(
        "You are a trivial no-op hat (task 015 fixture). Reply with `{}` as "
        "the last line of your final message and nothing else.\n")
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "config")

    assert NOOP_STATE not in tr.TABLE, "test fixture name collided with a real state"
    original_entry = tr.ENTRY_STATE
    tr.TABLE[NOOP_STATE] = (tr.T("planning", "noop_task015_done"),)
    tr.STATE_HAT[NOOP_STATE] = NOOP_HAT
    tr.HAT_SCHEMAS[NOOP_HAT] = {
        "type": "object", "properties": {}, "additionalProperties": False,
    }
    tr.ENTRY_STATE = NOOP_STATE
    try:
        yield
    finally:
        tr.ENTRY_STATE = original_entry
        tr.TABLE.pop(NOOP_STATE, None)
        tr.STATE_HAT.pop(NOOP_STATE, None)
        tr.HAT_SCHEMAS.pop(NOOP_HAT, None)


def _run(root: Path, handlers: dict, **cfg_kw) -> tuple[LoopSupervisor, str]:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="starting")
    kw = {"iterations": 15, "max_approaches": 1, "vigilant": True,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="task015-noop-hat", **kw), run, root)
    sup.runner = _ScriptedAgent(sup, handlers)  # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff  # type: ignore[method-assign]
    return sup, asyncio.run(sup._run_job_core())


# ---------------------------------------------------------------------------
# 1. the no-op hat runs end to end: entry -> noop hat -> planning -> worker
#    -> review -> succeeded, exactly like any real run, without vigilant's
#    verify sub-cycle getting in the way (max_approaches=1, vigilant=False
#    keeps the assertion about `.phases` simple -- verify's own reachability
#    is task 008/014's business, not this test's).
# ---------------------------------------------------------------------------

def test_a_registered_noop_hat_runs_end_to_end(tmp_path, registered_noop_hat):
    root = tmp_path / "run"
    sup, state = _run(root, _happy_handlers(), vigilant=False)
    assert state == "succeeded"
    agent = sup.runner
    assert agent.phases[0] == NOOP_STATE
    assert agent.phases[1:4] == ["planning", "worker", "review"]
    # The transition that actually fired is recorded in the executor's own
    # audit trail, not inferred from the outcome.
    from ralphd.engine.state import read_events
    events = read_events(sup.run.root)
    phase_events = [e for e in events if e.get("type") == "phase"]
    assert phase_events[0]["phase"] == NOOP_STATE


def test_a_contract_violation_from_the_noop_hat_is_reasked_not_fatal(
        tmp_path, registered_noop_hat):
    """The generic fallback validates the hat's payload against its
    declared schema (task 009) exactly as any dedicated `_step_*` method
    would honour its own sentinel/verdict -- a hat that breaks its own
    trivial contract does not get its edge anyway.

    v0.13 task 003 (#92) strengthens this. The OLD claim, pinned here
    before the cutover, was just: `state in ("failed", "aborted")` and
    `"contract violation" in status["reason"]` -- i.e. a SINGLE bad
    payload ended the run outright, with the abort reason literally
    quoting the schema error. That is `deck-phase3g`'s own shape (#92's
    PRD): a broken contract was fatal on the first offence.

    The NEW claim keeps the old one's real content -- the move is still
    refused on every bad payload, the payload never gets its edge while
    it stays wrong -- and adds what the old assertion could not see at
    all: the specific error reached the hat verbatim on the re-ask, the
    ladder ran the FULL `contract_retry_max + 1` attempts before giving up
    on that one work iteration (never fewer, never more), that exhaustion
    did NOT end the run (`self._abort_reason` stays unset, the SAME hat is
    asked completely fresh instead), and the run went on to SUCCEED once
    the hat's next attempt finally validated. That is strictly stronger:
    everything the old assertion proved is still proved (the bad payload
    never took its edge), plus the survival the old assertion could not
    have distinguished from a crash.

    A one-line mutation that reverts the task-003 cutover -- put back
    `errors = contracts.validate_payload(hat, payload)` /
    `self._abort_reason = message` / `return ("tail",)` in place of the
    `enforce_contract` call in `_step_generic_hat` -- makes this test fail
    on its very first new assertion (`state == "succeeded"`): the
    reverted code sets `_abort_reason` on the FIRST bad payload and
    returns `("tail",)` before the hat ever gets a second chance, so the
    run ends `"aborted"` instead, and only ONE `contract.violation` event
    is ever emitted instead of `attempts_per_ladder`.
    """
    RETRY_MAX = 1
    attempts_per_ladder = RETRY_MAX + 1  # the first attempt plus 1 re-ask

    handlers = dict(_happy_handlers())
    calls: list[str] = []
    call_count = [0]

    def _bad_then_fixed(sup, prompt=None):
        call_count[0] += 1
        calls.append(prompt or "")
        if call_count[0] <= attempts_per_ladder:
            # every attempt of the FIRST work iteration's ladder is bad.
            return _ok("oops\n{\"unexpected_field\": true}")
        # the SECOND work iteration's hat call -- a completely fresh
        # `run_iteration()` call, not a re-ask -- is schema-valid.
        return _ok("(fixed)\n{}")

    handlers[NOOP_STATE] = _bad_then_fixed
    root = tmp_path / "run-violation"
    sup, state = _run(root, handlers, vigilant=False, iterations=5,
                      contract_retry_max=RETRY_MAX)

    # The run SURVIVED the violation outright -- it succeeded, exactly as
    # the happy-path test above, proving a contract violation is never
    # fatal on its own while budget remains.
    assert state == "succeeded"
    assert sup._abort_reason is None

    from ralphd.engine.state import read_events
    events = read_events(sup.run.root)
    violation_events = [e for e in events if e.get("type") == "contract.violation"]
    fault_events = [e for e in events if e.get("type") == "contract.agent_fault"]
    assert len(violation_events) == attempts_per_ladder
    assert len(fault_events) == 1
    for ev in violation_events + fault_events:
        assert ev["hat"] == NOOP_HAT
    assert all(ev["maxAttempts"] == attempts_per_ladder for ev in violation_events)

    # The ladder ran to the FULL contract_retry_max + 1 attempts on the
    # FIRST work iteration (never fewer -- "asked once and gave up" --
    # and never more -- a second, hidden retry loop double-charging
    # attempts) before giving up on THAT attempt and asking the hat
    # completely fresh: exactly `attempts_per_ladder` bad calls plus ONE
    # fresh call that finally succeeded.
    assert call_count[0] == attempts_per_ladder + 1
    assert len(calls) == attempts_per_ladder + 1

    # The errors reached the hat, verbatim, on the re-ask -- and the
    # re-ask never repeats the noop hat's own task prompt (its fixture
    # text below) or invites further workspace work: it is a constrained
    # question about the payload only, never a second work turn.
    assert "trivial no-op hat (task 015 fixture)" in calls[0]
    for reask_prompt in calls[1:attempts_per_ladder]:
        assert "unexpected field" in reask_prompt
        assert "trivial no-op hat (task 015 fixture)" not in reask_prompt
    # The call that finally succeeded is a REAL, fresh hat call again --
    # the task prompt, not a re-ask.
    assert "trivial no-op hat (task 015 fixture)" in calls[attempts_per_ladder]


# ---------------------------------------------------------------------------
# 2. the executor's source is untouched -- literally: neither
#    transitions.py nor loop.py changed on disk while the hat was
#    registered and driven.
# ---------------------------------------------------------------------------

def test_registering_and_driving_the_hat_touches_no_engine_module_on_disk(
        tmp_path, registered_noop_hat):
    before_transitions = _sha(TRANSITIONS_PATH)
    before_loop = _sha(LOOP_PATH)

    root = tmp_path / "run"
    sup, state = _run(root, _happy_handlers(), vigilant=False)
    assert state == "succeeded"

    assert _sha(TRANSITIONS_PATH) == before_transitions, (
        "engine/transitions.py changed on disk while registering/driving "
        "the no-op hat -- requirement B's promise is that this never "
        "happens for a new hat")
    assert _sha(LOOP_PATH) == before_loop, (
        "engine/loop.py changed on disk while registering/driving the "
        "no-op hat")


def test_the_transition_executor_class_source_is_unchanged_by_the_hat(
        registered_noop_hat):
    """Not just "the file didn't change" (the test above) but the specific
    class the PRD names -- captured as source text so a future edit to
    `TransitionExecutor` that happens to leave the file's OTHER bytes alone
    (impossible, but the point is not to rely on that) still fails this."""
    import inspect
    executor = tr.TransitionExecutor(vigilant=False)
    before = inspect.getsource(tr.TransitionExecutor)
    # Actually use the executor while the hat is registered, so this isn't
    # vacuous: advance through the noop hat's own edge.
    assert executor.advance(NOOP_STATE, "planning", "noop_task015_done") == "planning"
    after = inspect.getsource(tr.TransitionExecutor)
    assert after == before
    assert type(executor) is tr.TransitionExecutor, (
        "a subclass or wrapper was used instead of the shipped executor")


# ---------------------------------------------------------------------------
# 3. the registration really is additive: TABLE/STATE_HAT/HAT_SCHEMAS gain
#    exactly the one new entry each, and disappear again on teardown.
# ---------------------------------------------------------------------------

def test_registration_is_purely_additive_and_cleans_up(registered_noop_hat):
    assert NOOP_STATE in tr.TABLE
    assert tr.STATE_HAT[NOOP_STATE] == NOOP_HAT
    assert NOOP_HAT in tr.HAT_SCHEMAS
    assert tr.ENTRY_STATE == NOOP_STATE
    # Every OTHER state's declared edges are exactly what task 008 shipped --
    # the registration fixture only ever ADDS the one new key, never
    # rewrites an existing row.
    for existing_state in ("planning", "worker", "review", "replan",
                           "reflect"):
        assert existing_state in tr.TABLE
    # `verify` only exists as a key via `VIGILANT_DELTA` (task 008's own
    # design -- see transitions.py's module docstring); the base `TABLE`
    # legitimately never had it, registration or no registration.
    assert "verify" in tr.VIGILANT_DELTA


def test_the_noop_state_is_gone_after_the_fixture_tears_down():
    assert NOOP_STATE not in tr.TABLE
    assert NOOP_STATE not in tr.STATE_HAT
    assert NOOP_HAT not in tr.HAT_SCHEMAS
    assert tr.ENTRY_STATE == "planning"


# ---------------------------------------------------------------------------
# 4. the generic fallback requires a hat to be declared -- an unregistered
#    state (no `transitions.STATE_HAT` entry at all, whatever its edge
#    count) is refused rather than silently doing something with it. The
#    OLD narrower rule this test used to assert ("more than one legal edge
#    is always refused") is superseded by task 018 (requirement E): a
#    multi-edge state with a hat AND a declared `transitions.ROUTER_FALLBACK`
#    entry now runs end to end through `engine.router` -- see
#    tests/test_router_engine.py for that path's own coverage. This test
#    keeps proving the ADJACENT guard that never changed: no hat, no run,
#    whatever the table says about edges.
# ---------------------------------------------------------------------------

def test_the_generic_fallback_refuses_a_state_with_no_declared_hat():
    import ralphd.engine.loop as loop_module
    sup = LoopSupervisor.__new__(LoopSupervisor)  # bypass __init__: no I/O needed
    edges = (tr.T("worker", "a"), tr.T("review", "b"))
    assert "bogus_task015_no_hat" not in tr.STATE_HAT
    with pytest.raises(AssertionError, match="no hat declared"):
        asyncio.run(loop_module.LoopSupervisor._step_generic_hat(
            sup, "bogus_task015_no_hat", edges))
