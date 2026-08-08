"""Black-box tests for engine resume-from-existing-state (PRD req 16, engine
side / task 028).

Reuses `test_e2e.py`'s `engine_factory` fixture: calling it twice within one
test launches a second real `ralphd-engine` process over the *same* run dir
(the fixture's `tmp_path` is shared for the whole test, and `EngineProc`
always resolves `run`/`config`/`workspace` under it) -- exactly what a real
`ralphctl resume` does (fresh container, same mounted run dir), without
needing Docker.
"""

from __future__ import annotations

import json

from test_e2e import engine_factory

__all__ = ["engine_factory"]


def test_resume_stale_running_status_skips_planning_and_completes(engine_factory):
    """A terminal (budget-exhausted) run dir, restarted with a bumped
    iteration budget, must: skip planning, continue iteration numbering
    from N+1, and finish the job (verdict verified)."""
    # First process: deliberately exhaust the iteration budget so the job
    # ends in a terminal "failed" state with tasks.json partially done and
    # left on disk (mirrors a stale/killed prior engine).
    e1 = engine_factory(job={"on_complete": "exit", "iterations": 2,
                             "max_approaches": 1},
                        stub_env={"STUB_TASKS": "5"})
    assert e1.proc.wait(timeout=60) == 1
    status1 = json.loads((e1.run_dir / "status.json").read_text())
    assert status1["state"] == "failed"
    assert status1["iterationsUsed"] == 2

    iters_before = sorted((e1.run_dir / "iterations").iterdir())
    assert len(iters_before) == 2
    phases_before = [json.loads((d / "meta.json").read_text())["phase"]
                     for d in iters_before]
    assert phases_before == ["planning", "worker"]

    tasks_before = json.loads((e1.run_dir / "tasks.json").read_text())["tasks"]
    assert len(tasks_before) == 5
    completed_before = sum(1 for t in tasks_before if t["status"] == "completed")
    assert completed_before == 1

    # Second process over the SAME run dir (same tmp_path -> same run/
    # config/workspace dirs), with a bumped iteration budget, simulating
    # `ralphctl resume --iterations +N`.
    e2 = engine_factory(job={"on_complete": "exit", "iterations": 20,
                             "max_approaches": 1},
                        stub_env={"STUB_TASKS": "5"})
    assert e2.run_dir == e1.run_dir  # sanity: genuinely the same run dir
    assert e2.proc.wait(timeout=60) == 0

    status2 = json.loads((e2.run_dir / "status.json").read_text())
    assert status2["state"] == "succeeded"
    assert status2["verdict"] == "verified"
    # budget accounting reflects prior usage: iterationsUsed keeps growing
    # from where the first process left off, it does not restart at 1
    assert status2["iterationsUsed"] > 2

    iters_after = sorted((e2.run_dir / "iterations").iterdir())
    numbers = [int(d.name) for d in iters_after]
    assert numbers == sorted(set(numbers)), "iteration numbers must be monotonic, no dupes"
    assert numbers[:2] == [1, 2], "the original two iterations are untouched"

    metas_after = [json.loads((d / "meta.json").read_text()) for d in iters_after]
    phases_after = [m["phase"] for m in metas_after]
    # planning ran exactly once total (in the first process) -- resume must
    # not re-plan
    assert phases_after.count("planning") == 1
    assert phases_after[0] == "planning"
    assert phases_after[2] == "worker", "iteration 3 resumes straight into work"
    # every meta.json from iteration 1 onward has an endedAt (nothing
    # left half-written) and no duplicate iteration directories were created
    assert all(m.get("endedAt") for m in metas_after)

    tasks_after = json.loads((e2.run_dir / "tasks.json").read_text())["tasks"]
    assert len(tasks_after) == 5
    assert all(t["status"] == "completed" for t in tasks_after)

    # a log event named the resume explicitly (operator-visible)
    events = [json.loads(line) for line in
              (e2.run_dir / "events.jsonl").read_text().splitlines()]
    resume_logs = [ev for ev in events if ev.get("type") == "log"
                   and "resuming existing run-dir state" in ev.get("message", "")]
    assert resume_logs, "expected a log event announcing the resume"


def test_fresh_run_dir_still_plans_normally(engine_factory):
    """Negative control: a genuinely fresh run dir (no prior tasks.json /
    iterations) is completely unaffected -- planning still runs first, and
    iteration numbering still starts at 1."""
    e = engine_factory(job={"on_complete": "exit"})
    assert e.proc.wait(timeout=60) == 0
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"
    iters = sorted((e.run_dir / "iterations").iterdir())
    assert iters[0].name == "0001"
    meta1 = json.loads((iters[0] / "meta.json").read_text())
    assert meta1["phase"] == "planning"
