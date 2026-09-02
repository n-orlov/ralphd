"""Task 032 (requirement G.4, #130; first cut): run-level black-box coverage
for two COMPOSED gate variants that tasks 029/030/031 never drove together
-- each of those covered exactly one opt-in mechanism at a time (a single
peer gate, or the steer spool), never two mechanisms interacting inside the
same run. `transitions.table_for` (and its unit-level regression,
`tests/test_peer_verdict_gate.py::test_the_two_peer_gates_compose_without_
interaction`) already proves the STATE TABLE composes without either axis
touching the other's states; this module is the run-level counterpart --
proving the real subprocess engine, the real stub `pi`, and the real
per-iteration bookkeeping (`meta.json` phases, `status.json`) actually walk
the composed table end to end, for two scenarios:

1. `vigilant` (task 019/020's per-task verify mini-FSM) together with
   `peer_verdict_gate` (task 020's second, independent opinion on `review`'s
   own verdict) in the SAME run -- both mechanisms fire on every task the
   plan carries and the run still reaches its real, gate-gated success edge.

2. BOTH peer gates -- `peer_plan_review` (task 018) and `peer_verdict_gate`
   (task 020) -- enabled in the same run, non-vigilant, each with a real
   objection/rejection round trip before its own approval, so both gates are
   observably exercised rather than merely configured.

Uses the `live` fixture (tests/conftest.py) at the same tier as tasks
029/031, and the same stub-`pi` knobs those tasks already added
(`STUB_PLAN_REVIEW_OBJECTIONS`, `STUB_VERDICT_GATE_REJECT_COUNT`) -- no new
stub surface needed for this first cut.
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


def test_verdict_gate_with_vigilant_mode_both_observably_exercised(live):
    """`vigilant: true` + `peer_verdict_gate: true`, no rejection knob set:
    every one of the two stub tasks gets its own vigilant `verify`
    iteration (task 019/020's per-task mini-FSM), `review` then verifies
    the whole plan, and the SECOND, independent `verdict_gate` invocation
    also agrees -- the run's real success edge sits at `verdict_gate`
    exactly as task 029's non-vigilant scenario asserts, but now with both
    per-task verification AND the peer gate recorded in the SAME phase
    sequence, and every task's TERMINAL status is vigilant's own
    `validated` (task 020), never the non-vigilant `completed`."""
    run = live(run_id="g4-verdict-gate-plus-vigilant",
               job={"vigilant": True, "peer_verdict_gate": True,
                    "on_complete": "exit"})
    assert run.proc.wait(timeout=60) == 0
    status = _status(run)
    assert status.get("state") == "succeeded", status
    assert status.get("verdict") == "verified", status

    metas = _iteration_metas(run)
    phases = [m["phase"] for m in metas]
    # 2 stub tasks (the `live` fixture's own default STUB_TASKS count is
    # unset, so the stub's own default of 2 applies): one (worker, verify)
    # pair per task, then review, then the peer gate.
    assert phases == [
        "planning", "worker", "verify", "worker", "verify",
        "review", "verdict_gate",
    ], phases

    # vigilant's own per-task mechanism actually fired: two verify
    # iterations, each carrying its own verified task id and a passing
    # outcome -- not merely present in the phase list.
    verify_metas = [m for m in metas if m["phase"] == "verify"]
    assert len(verify_metas) == 2
    for vm in verify_metas:
        assert "verifiedTask" in vm
        assert vm["verifyOutcome"] == "pass"

    # the peer gate's own mechanism actually fired too: exactly one
    # verdict_gate iteration, after review, with no findings artifact
    # (this scenario never rejects).
    vg_metas = [m for m in metas if m["phase"] == "verdict_gate"]
    assert len(vg_metas) == 1
    assert not (run.run_dir / "verdict-gate-findings.md").exists()

    # both mechanisms' own terminal bookkeeping holds together: every task
    # is vigilant-`validated` (never the bare `completed` a non-vigilant
    # run would leave), which is the terminal state_gate/verdict_gate had
    # to see agree with in order to approve.
    tasks = json.loads((run.run_dir / "tasks.json").read_text())["tasks"]
    assert tasks and all(t["status"] == "validated" for t in tasks)


def test_both_peer_gates_enabled_in_the_same_run(live):
    """`peer_plan_review: true` + `peer_verdict_gate: true`, non-vigilant:
    the first `plan_review` invocation OBJECTS (spending a real revision
    and routing back to a second real `planning` iteration), the second
    `plan_review` invocation approves and the plan proceeds to `worker`;
    then, after `review` verifies, the FIRST `verdict_gate` invocation
    REJECTS (spending the run's only other approach), a full second
    approach runs its own `planning`/`plan_review`/`worker`/`review`, and
    that approach's own `verdict_gate` invocation approves -- both gates
    fired for real, on both ends of the run, not merely configured."""
    run = live(run_id="g4-both-peer-gates",
               job={"peer_plan_review": True, "peer_verdict_gate": True,
                    "on_complete": "exit", "max_approaches": 2},
               stub_env={"STUB_PLAN_REVIEW_OBJECTIONS": "1",
                        "STUB_VERDICT_GATE_REJECT_COUNT": "1"})
    assert run.proc.wait(timeout=60) == 0
    status = _status(run)
    assert status.get("state") == "succeeded", status
    assert status.get("verdict") == "verified", status

    metas = _iteration_metas(run)
    phase_approach = [(m["phase"], m["approach"]) for m in metas]
    assert phase_approach == [
        # approach 1: plan_review objects once, then approves; the
        # verdict gate then rejects once, spending the approach.
        ("planning", 1), ("plan_review", 1),
        ("planning", 1), ("plan_review", 1),
        ("worker", 1), ("worker", 1), ("review", 1), ("verdict_gate", 1),
        # approach 2: a fresh planning/plan_review round (approving on
        # its own first try -- the objection knob is spent, not reset),
        # then the verdict gate's second invocation approves for real.
        ("planning", 2), ("plan_review", 2),
        ("worker", 2), ("worker", 2), ("review", 2), ("verdict_gate", 2),
    ], phase_approach

    # plan_review's own mechanism fired for real: one revision recorded
    # against approach 1, none against approach 2 (its own first pass
    # approved outright).
    assert status.get("planReviewRevisions") == {"1": 1}, status
    assert status.get("approach") == 2, status

    # verdict_gate's own mechanism fired for real too: approach 1 was
    # rejected-with-budget-left and archived (task 020's
    # `_archive_approach`), carrying the gate's own findings channel;
    # approach 2, the run's real terminal success, is never archived.
    approaches_dir = run.run_dir / "approaches"
    approach_one = approaches_dir / "01"
    assert approach_one.is_dir(), sorted(p.name for p in approaches_dir.iterdir())
    assert not (approaches_dir / "02").exists()
    assert (approach_one / "verdict-gate-findings.md").exists()
    findings = json.loads((approach_one / "verdict-gate-findings.json").read_text())
    assert findings and all(f.get("author") == "verdict_gate" for f in findings)

    tasks = json.loads((run.run_dir / "tasks.json").read_text())["tasks"]
    assert tasks and all(t["status"] == "completed" for t in tasks)
