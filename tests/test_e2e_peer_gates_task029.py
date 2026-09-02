"""Task 029 (requirement G.1, #130): black-box e2e battery for BOTH opt-in
peer gates -- `plan_review` (task 018) and `verdict_gate` (task 020) -- at
the same tier `tests/test_e2e.py` already uses for the rest of the engine:
a real `ralphd-engine` subprocess, a stub `pi` on PATH, the gates configured
from a real `job.yaml` (the `live` fixture in tests/conftest.py, wired for
`ralphctl` too, but only its plain HTTP-free run/status surface is needed
here). No engine internals are imported; every assertion below reads the
run's OWN recorded state -- `status.json`'s `state`/`verdict`/`reason` and
each iteration's `meta.json` `phase` -- never only the stub's own input.

The unit-level coverage for these two gates
(`tests/test_peer_plan_review_gate.py`, `tests/test_peer_verdict_gate.py`)
drives a real `LoopSupervisor` with a hand-scripted `sup.runner` standing in
for the model call; this module instead drives the real subprocess boundary
those tests never touch -- job.yaml parsing, the real `runner.py` spawn of a
`pi`-shaped process, and the real NDJSON parse of that process's own stdout
-- for exactly the four scenarios the PRD's requirement G.1 names:

1. `plan_review` approval (the plan proceeds to `worker` untouched).
2. `plan_review` objection followed by a revision (a real second `planning`
   iteration, then a second `plan_review` that approves).
3. `verdict_gate` approval (the run's real success edge, per task 020).
4. `verdict_gate` rejection (fails OPEN to `finishing`/`failed` once the
   approach budget is spent -- task 020's decided rejection semantics,
   `artifacts/reports/task-020-requirement-l-rejection-semantics.md`).

New stub-`pi` knobs added alongside this module (`tests/stub-pi/pi`), mirror
`STUB_REVIEW_FAILS`'s own shape:

  STUB_PLAN_REVIEW_OBJECTIONS  how many `plan_review` invocations object
                      before the next one approves (default 0).
  STUB_VERDICT_GATE_REJECT     if set, every `verdict_gate` invocation
                      rejects instead of approving.
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


def _phases(run) -> list[str]:
    iters = sorted((run.run_dir / "iterations").iterdir())
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    return [m["phase"] for m in metas]


def test_plan_review_approval_lets_worker_proceed(live):
    """`peer_plan_review: true`, no objection knob set: the very first
    `plan_review` invocation approves, and the run reaches its ordinary
    terminal success exactly as an off run would -- the gate's own phase
    shows up in the recorded iteration sequence between `planning` and the
    first `worker`."""
    run = live(run_id="g1-plan-review-approve",
               job={"peer_plan_review": True, "on_complete": "exit"})
    assert run.proc.wait(timeout=60) == 0
    status = _status(run)
    assert status.get("state") == "succeeded", status
    assert status.get("verdict") == "verified", status
    phases = _phases(run)
    assert phases == ["planning", "plan_review", "worker", "worker", "review"], phases
    tasks = json.loads((run.run_dir / "tasks.json").read_text())["tasks"]
    assert tasks and all(t["status"] == "completed" for t in tasks)


def test_plan_review_objection_then_revision(live):
    """`STUB_PLAN_REVIEW_OBJECTIONS=1`: the first `plan_review` invocation
    objects with a reason, spending one revision (persisted on disk, task
    018's `planReviewRevisions[str(approach)]`) and routing back to a real
    second `planning` iteration; the SECOND `plan_review` invocation then
    approves and the run proceeds and finishes exactly as scenario 1 does,
    just with one extra `planning`/`plan_review` round trip recorded."""
    run = live(run_id="g1-plan-review-objection",
               job={"peer_plan_review": True, "on_complete": "exit"},
               stub_env={"STUB_PLAN_REVIEW_OBJECTIONS": "1"})
    assert run.proc.wait(timeout=60) == 0
    status = _status(run)
    assert status.get("state") == "succeeded", status
    assert status.get("verdict") == "verified", status
    phases = _phases(run)
    assert phases == [
        "planning", "plan_review", "planning", "plan_review",
        "worker", "worker", "review",
    ], phases
    # the revision was actually spent and recorded against approach 1 --
    # the run's own disk state, not merely the stub having been asked to
    # object once.
    assert status.get("planReviewRevisions") == {"1": 1}, status
    tasks = json.loads((run.run_dir / "tasks.json").read_text())["tasks"]
    assert tasks and all(t["status"] == "completed" for t in tasks)


def test_verdict_gate_approval_is_the_runs_real_success_edge(live):
    """`peer_verdict_gate: true`, no rejection knob set: `review` verifies,
    the SECOND, independent `verdict_gate` invocation also agrees, and the
    run's real success edge now sits at `verdict_gate` (task 020) --
    `finishing`/`succeeded` is reached only after both, with the gate's own
    phase recorded after `review`."""
    run = live(run_id="g1-verdict-gate-approve",
               job={"peer_verdict_gate": True, "on_complete": "exit"})
    assert run.proc.wait(timeout=60) == 0
    status = _status(run)
    assert status.get("state") == "succeeded", status
    assert status.get("verdict") == "verified", status
    phases = _phases(run)
    assert phases == ["planning", "worker", "worker", "review", "verdict_gate"], phases
    tasks = json.loads((run.run_dir / "tasks.json").read_text())["tasks"]
    assert tasks and all(t["status"] == "completed" for t in tasks)


def test_verdict_gate_rejection_fails_open_when_no_approach_is_left(live):
    """`STUB_VERDICT_GATE_REJECT` set, `max_approaches: 1`: `review` itself
    still verifies, but the SECOND, independent `verdict_gate` invocation
    disagrees. Task 020's decided rejection semantics with the approach
    budget already spent: fail OPEN to `finishing` rather than deadlock --
    the run's own recorded terminal state is `failed`/`unverified` (a peer
    rejection is never a terminal success, budget or not), never a hang
    and never a crash."""
    run = live(run_id="g1-verdict-gate-reject",
               job={"peer_verdict_gate": True, "on_complete": "exit",
                    "max_approaches": 1},
               stub_env={"STUB_VERDICT_GATE_REJECT": "1"})
    assert run.proc.wait(timeout=60) == 1
    status = _status(run)
    assert status.get("state") == "failed", status
    assert status.get("verdict") == "unverified", status
    phases = _phases(run)
    assert phases == ["planning", "worker", "worker", "review", "verdict_gate"], phases
