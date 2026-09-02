"""Task 033 (requirement G.5, #130; first cut): ladder exhaustion, with
budget remaining, driven at the run-level ("e2e") tier for the three
judging sites tasks 001/002/003 already proved at the unit tier --
`verify` (`_verify_contract_ladder`), `review` (`_review_contract_ladder`)
and `verdict_gate` (`_verdict_gate_verdict`). Same `live` fixture / real
subprocess engine + stub-`pi` tier tasks 029/031/032 already use, never
`LoopSupervisor` internals.

Each unit-tier test drove exhaustion by handing `sup.runner` a fixed
sequence of `IterationResult`s and reading the ladder's own return value.
Here there is no `sup.runner` to script: the engine spawns a REAL `pi`
subprocess for the charged call AND for every uncharged re-ask the ladder
issues (`_ask_contract_fix` -> `self.runner.run` -> another `tests/stub-pi/
pi` invocation). Three new stub-`pi` knobs, added alongside this module
(`STUB_VERIFY_MALFORMED_COUNT`, `STUB_REVIEW_MALFORMED_COUNT`,
`STUB_VERDICT_GATE_MALFORMED_COUNT`), make the FIRST invocation of the
named site reply with no parseable contract payload at all (prose, no
JSON); the re-ask prompt `_ask_contract_fix` builds carries no `Role: X`
marker for any site, so it falls to the stub's own `else` branch
("unrecognized phase prompt") and is *also* unparseable -- the ladder
therefore exhausts naturally after `1 + contract_retry_max` total stub
invocations, with no extra stub logic needed to answer a re-ask.

"Budget remaining" is checked concretely at each site, never merely
asserted:

  - `verify`: the SAME task is picked up by another `worker` iteration
    after its validation-failed round (today's ordinary retry path) and
    the run reaches its real terminal success, never ending at the
    ladder's own exhaustion.
  - `review`/`verdict_gate`: `max_approaches: 2` gives the run a second
    approach to fall back on after the first one's ladder-exhausted
    invocation archives it (today's `rejected_approach_left` safe
    default) -- the SECOND approach's own invocation of the same site
    answers normally and the run reaches its real terminal success there.

A fourth test drives task 032's fenced-payload rescue
(`_extract_validated_fenced_payload`) above the unit tier, at the `verify`
site: the malformed reply is prose followed by a fenced ```json block
that already carries a valid payload, so the very first contract
violation is rescued with ZERO additional stub-`pi` invocations (the
`.stub-verify-call-count` marker never advances past 1) and no
`usage.byPhase.contract` at all -- contrasted directly against the
exhaustion test's own non-zero contract-phase usage and its `call-count`
marker advancing to 2.
"""

from __future__ import annotations

import json


def _status(run) -> dict:
    f = run.run_dir / "status.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except json.JSONDecodeError:  # mid-write
        return {}


def _iteration_metas(run) -> list[dict]:
    iters = sorted((run.run_dir / "iterations").iterdir())
    return [json.loads((d / "meta.json").read_text()) for d in iters]


def _events(run) -> list[dict]:
    p = run.run_dir / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _marker(run, name: str) -> str | None:
    p = run.run_dir / f".stub-{name}"
    return p.read_text().strip() if p.exists() else None


# ---------------------------------------------------------------------------
# 1. verify: ladder exhaustion with budget remaining -- the SAME task is
#    retried by another worker iteration and the run still finishes.
# ---------------------------------------------------------------------------


def test_verify_ladder_exhaustion_with_budget_remaining_does_not_end_the_run(live):
    run = live(run_id="g5-verify-ladder-exhaustion",
               job={"vigilant": True, "on_complete": "exit"},
               stub_env={"STUB_TASKS": "1", "STUB_VERIFY_MALFORMED_COUNT": "1"})
    assert run.proc.wait(timeout=60) == 0
    status = _status(run)
    assert status.get("state") == "succeeded", status
    assert status.get("verdict") == "verified", status

    # the run did NOT end at the exhausted verify -- another worker
    # iteration picked the same task back up (its validation-failed
    # retry), a second verify invocation passed, and review still ran.
    metas = _iteration_metas(run)
    phases = [m["phase"] for m in metas]
    assert phases == ["planning", "worker", "verify", "worker", "verify",
                       "review"], phases

    # the ladder genuinely exhausted (never merely "got lucky and passed
    # first try"): two real verify invocations were made -- the exhausted
    # one and the successful retry.
    assert _marker(run, "verify-call-count") == "2"

    # today's ordinary safe default fired exactly once: one validation-
    # failed round recorded, then the task reached vigilant's own
    # terminal `validated` -- never stuck at `validation-failed`.
    tasks = json.loads((run.run_dir / "tasks.json").read_text())["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["status"] == "validated", tasks[0]
    assert tasks[0]["validationAttempts"] == 1, tasks[0]

    # the exhausted attempt's uncharged re-asks are recorded under
    # usage.byPhase.contract and the recorded contract.agent_fault --
    # mirrors task 001's unit-tier invariant, now observed through the
    # engine's own recorded run state rather than a scripted runner.
    by_phase = status.get("usage", {}).get("byPhase", {})
    assert by_phase.get("contract", {}).get("totalTokens", 0) > 0, by_phase
    events = _events(run)
    faults = [e for e in events if e.get("type") == "contract.agent_fault"]
    assert len(faults) == 1, events
    assert faults[0].get("hat") == "verify", faults[0]

    # there was genuine iteration budget left over -- the run did not
    # merely limp to its own cap.
    assert status.get("iterationsUsed", 0) < status.get("iterationsBudget", 0), status


# ---------------------------------------------------------------------------
# 2. review: ladder exhaustion with budget remaining -- approach 1 is
#    archived (today's `rejected_approach_left`) and approach 2's own
#    review invocation answers normally.
# ---------------------------------------------------------------------------


def test_review_ladder_exhaustion_with_budget_remaining_does_not_end_the_run(live):
    run = live(run_id="g5-review-ladder-exhaustion",
               job={"on_complete": "exit", "max_approaches": 2},
               stub_env={"STUB_REVIEW_MALFORMED_COUNT": "1"})
    assert run.proc.wait(timeout=60) == 0
    status = _status(run)
    assert status.get("state") == "succeeded", status
    assert status.get("verdict") == "verified", status
    assert status.get("approach") == 2, status

    metas = _iteration_metas(run)
    phase_approach = [(m["phase"], m["approach"]) for m in metas]
    assert phase_approach == [
        ("planning", 1), ("worker", 1), ("worker", 1), ("review", 1),
        ("planning", 2), ("worker", 2), ("worker", 2), ("review", 2),
    ], phase_approach

    # only ONE real review invocation happened per approach: the ladder's
    # own uncharged re-asks never advance this marker (they carry no
    # `Role: Reviewer` text), so it reads exactly 2 -- one exhausted call
    # for approach 1, one passing call for approach 2.
    assert _marker(run, "review-count") == "2"

    # today's ordinary archive-with-safe-default consequences of an
    # exhausted (unparseable) review reply, unchanged: approach 1 is
    # archived, approach 2 -- the run's real terminal success -- never is.
    approaches_dir = run.run_dir / "approaches"
    assert (approaches_dir / "01").is_dir(), sorted(
        p.name for p in approaches_dir.iterdir())
    assert not (approaches_dir / "02").exists()
    assert (approaches_dir / "01" / "tasks.json").exists()

    tasks = json.loads((run.run_dir / "tasks.json").read_text())["tasks"]
    assert tasks and all(t["status"] == "completed" for t in tasks)

    by_phase = status.get("usage", {}).get("byPhase", {})
    assert by_phase.get("contract", {}).get("totalTokens", 0) > 0, by_phase
    assert status.get("iterationsUsed", 0) < status.get("iterationsBudget", 0), status


# ---------------------------------------------------------------------------
# 3. verdict_gate: ladder exhaustion with budget remaining -- mirrors task
#    031's rejection-then-second-approach shape, but the first approach's
#    gate invocation never produces a parseable payload at all rather
#    than a well-formed rejection.
# ---------------------------------------------------------------------------


def test_verdict_gate_ladder_exhaustion_with_budget_remaining_does_not_end_the_run(live):
    run = live(run_id="g5-verdict-gate-ladder-exhaustion",
               job={"peer_verdict_gate": True, "on_complete": "exit",
                    "max_approaches": 2},
               stub_env={"STUB_VERDICT_GATE_MALFORMED_COUNT": "1"})
    assert run.proc.wait(timeout=60) == 0
    status = _status(run)
    assert status.get("state") == "succeeded", status
    assert status.get("verdict") == "verified", status
    assert status.get("approach") == 2, status

    metas = _iteration_metas(run)
    phase_approach = [(m["phase"], m["approach"]) for m in metas]
    assert phase_approach == [
        ("planning", 1), ("worker", 1), ("worker", 1), ("review", 1),
        ("verdict_gate", 1),
        ("planning", 2), ("worker", 2), ("worker", 2), ("review", 2),
        ("verdict_gate", 2),
    ], phase_approach

    # the marker counts EVERY `Role: Peer verdict gate` invocation seen
    # (its own `_vg_mseen` counter keeps advancing once armed): 2 total --
    # the exhausted approach-1 call plus approach 2's own, later, normal
    # invocation (which falls through once `_vg_mseen >= wanted`).
    assert _marker(run, "verdict-gate-malformed-count") == "2"

    # the exhausted gate invocation still reaches today's ordinary
    # archive-with-safe-default path: approach 1 is archived, approach 2
    # -- the run's real terminal success -- never is.
    approaches_dir = run.run_dir / "approaches"
    assert (approaches_dir / "01").is_dir(), sorted(
        p.name for p in approaches_dir.iterdir())
    assert not (approaches_dir / "02").exists()

    tasks = json.loads((run.run_dir / "tasks.json").read_text())["tasks"]
    assert tasks and all(t["status"] == "completed" for t in tasks)

    by_phase = status.get("usage", {}).get("byPhase", {})
    assert by_phase.get("contract", {}).get("totalTokens", 0) > 0, by_phase
    assert status.get("iterationsUsed", 0) < status.get("iterationsBudget", 0), status


# ---------------------------------------------------------------------------
# 4. the fenced-payload rescue, above the unit tier, at the verify site:
#    a prose+fenced-valid-payload reply is rescued on the FIRST attempt
#    with zero additional stub-pi invocations -- never reaching the
#    ladder's exhaustion path at all.
# ---------------------------------------------------------------------------


def test_verify_fenced_payload_rescue_settles_with_no_extra_stub_invocation(live):
    run = live(run_id="g5-verify-fenced-rescue",
               job={"vigilant": True, "on_complete": "exit"},
               stub_env={"STUB_TASKS": "1", "STUB_VERIFY_MALFORMED_COUNT": "1",
                         "STUB_VERIFY_MALFORMED_FENCED": "1"})
    assert run.proc.wait(timeout=60) == 0
    status = _status(run)
    assert status.get("state") == "succeeded", status
    assert status.get("verdict") == "verified", status

    # the ordinary success shape -- never a validation-failed round, and
    # never a second worker retry -- because the FIRST verify reply was
    # rescued in place.
    metas = _iteration_metas(run)
    phases = [m["phase"] for m in metas]
    assert phases == ["planning", "worker", "verify", "review"], phases

    tasks = json.loads((run.run_dir / "tasks.json").read_text())["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["status"] == "validated", tasks[0]
    # never touched -- the rescue fixed the FIRST attempt's own reply, so
    # no validation-failed round was ever recorded against this task.
    assert tasks[0].get("validationAttempts", 0) == 0, tasks[0]

    # exactly ONE real stub-pi invocation for the whole verify episode --
    # the rescue never asked the model again, unlike the exhaustion test
    # above (whose own marker reads "2").
    assert _marker(run, "verify-call-count") == "1"

    # no contract-phase usage at all -- `_ask_contract_fix`'s
    # fenced-payload branch returns before ever calling `self.runner.run`.
    by_phase = status.get("usage", {}).get("byPhase", {})
    assert not by_phase.get("contract"), by_phase
    events = _events(run)
    faults = [e for e in events if e.get("type") == "contract.agent_fault"]
    assert faults == [], events
