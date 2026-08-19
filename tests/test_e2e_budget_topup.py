"""Task 047 (#3): end-to-end proof that a mid-flight iteration-budget top-up
actually buys a live job more work.

Tasks 045/046 proved the two halves separately: `PATCH /config/budget` against
a real app (tests/test_budget_patch.py) and the `ralphctl budget` wire contract
against a stub engine (tests/test_cli_budget.py). Neither shows the thing the
operator cares about -- that a job which is *about to die of budget
exhaustion* survives a top-up issued from the outside while it runs, without a
container restart.

So this is strictly black-box: a real `ralphd-engine` with the stub `pi` (the
`live` fixture from tests/conftest.py, wired so the real `ralphctl` executable
can address it through a temp registry), a plan deliberately larger than the
budget, and a top-up fired by `ralphctl budget` from another process while the
engine is mid-iteration.

The pair of tests is the whole argument:

- `test_run_without_topup_dies_of_budget_exhaustion` pins the counterfactual:
  with `iterations: 3` and a 4-task plan the run CANNOT finish -- it ends
  `failed`/`unverified` with tasks still pending. Without this, the top-up
  test below could pass for a run that was always going to succeed.
- `test_topup_midflight_lets_the_job_reach_a_terminal_state_it_could_not_have`
  runs the same job, tops it up in its last budget slot, and asserts it now
  performs further iterations and reaches `succeeded`/`verified` -- the
  terminal state the control run proves is unreachable on the original
  budget -- with the `budget_changed` audit event in events.jsonl.

`STUB_SLEEP` is what makes the top-up window real rather than racy: every
stub iteration takes ~2s, so once `iterationsUsed` reaches the last budget
slot there is a comfortable window in which to spawn `ralphctl`.
"""

from __future__ import annotations

import json
import time

# 4 tasks -> planning + 4 worker iterations + 1 review = 6 iterations needed.
PLAN_TASKS = 4
NEEDED_ITERATIONS = 6
# Deliberately short: enough for planning + 2 workers, never enough to finish.
STARTING_BUDGET = 3
# Per-iteration stub work time; keeps a wide window for the CLI top-up.
STUB_SLEEP_S = "2"


def _status(run) -> dict:
    f = run.run_dir / "status.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except json.JSONDecodeError:  # mid-write
        return {}


def _wait_for_last_budget_slot(run, timeout: float = 60.0) -> dict:
    """Block until the run has *started* its final affordable iteration
    (`iterationsUsed == STARTING_BUDGET`, written at iteration start), i.e. it
    is one boundary away from exhausting its budget. Fails loudly if the run
    went terminal first -- that would mean the window was missed and any
    later assertion would be meaningless."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = _status(run)
        if st.get("state") in ("succeeded", "failed", "aborted"):
            raise AssertionError(
                f"run went terminal before the top-up window: {st}")
        if st.get("iterationsUsed", 0) >= STARTING_BUDGET:
            return st
        time.sleep(0.1)
    raise AssertionError(f"never reached the last budget slot; last: {_status(run)}")


def _events(run) -> list[dict]:
    lines = (run.run_dir / "events.jsonl").read_text().splitlines()
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


# --------------------------------------------------------------------------
def test_run_without_topup_dies_of_budget_exhaustion(live):
    """Counterfactual for the test below: this job cannot finish on the
    budget it started with."""
    run = live(run_id="budget-control",
               job={"iterations": STARTING_BUDGET},
               stub_env={"STUB_TASKS": str(PLAN_TASKS)})
    status = run.wait_terminal()

    assert status["state"] == "failed"
    assert status["verdict"] == "unverified"
    assert status["iterationsUsed"] == STARTING_BUDGET
    tasks = json.loads((run.run_dir / "tasks.json").read_text())["tasks"]
    assert len(tasks) == PLAN_TASKS
    assert any(t["status"] != "completed" for t in tasks), tasks


def test_topup_midflight_lets_the_job_reach_a_terminal_state_it_could_not_have(live):
    run = live(run_id="budget-topup",
               job={"iterations": STARTING_BUDGET},
               stub_env={"STUB_TASKS": str(PLAN_TASKS),
                         "STUB_SLEEP": STUB_SLEEP_S})
    run.wait_api()
    _wait_for_last_budget_slot(run)

    # ...still running, one boundary from exhaustion: top up from outside.
    res = run.ralphctl("budget", run.run_id, "+10")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert f"iteration budget: {STARTING_BUDGET} -> {STARTING_BUDGET + 10}" in res.stdout
    assert _status(run)["state"] == "running", "top-up must apply to a live run"

    # The run keeps going past the original budget and finishes.
    status = run.wait_terminal(timeout=120)
    assert status["state"] == "succeeded", status
    assert status["verdict"] == "verified"
    assert status["iterationsBudget"] == STARTING_BUDGET + 10
    # Further iterations really happened, and the job needed them all.
    assert status["iterationsUsed"] > STARTING_BUDGET
    assert status["iterationsUsed"] == NEEDED_ITERATIONS, status
    phases = [json.loads((d / "meta.json").read_text())["phase"]
              for d in sorted((run.run_dir / "iterations").iterdir())]
    assert phases == ["planning"] + ["worker"] * PLAN_TASKS + ["review"]
    tasks = json.loads((run.run_dir / "tasks.json").read_text())["tasks"]
    assert all(t["status"] == "completed" for t in tasks)

    # The audit trail records who changed what, mid-run.
    changes = [e for e in _events(run) if e.get("type") == "budget_changed"]
    assert len(changes) == 1, changes
    change = changes[0]
    assert change["field"] == "iterations"
    assert change["previous"] == STARTING_BUDGET
    assert change["iterations"] == STARTING_BUDGET + 10
    assert change["delta"] == 10
    assert change["source"] == "api"
    assert change["iterationsUsed"] == STARTING_BUDGET
