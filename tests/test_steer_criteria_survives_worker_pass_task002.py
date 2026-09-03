"""Task 002 (#132, following on from task 001): task 001 fixed
`state.steer_task_update` so a steer-tool `successCriteria` edit moves
`criteriaFingerprint`/`criteriaOfRecord` forward atomically with the new
text. That is proven directly against `state.py`'s own write path in
`tests/test_steer_criteria_fingerprint_132.py`, but steer 003's acceptance
#2 wants the fix proven through the actual consumer: the very next
`LoopSupervisor` worker pass, which calls `_check_criteria_edits`
(loop.py:5533, inside `_step_worker`) against whatever is on disk. Before
task 001, that pass is exactly what undid a legitimate steer edit -- it
saw the mismatch between the (unmoved) stored fingerprint and the new
text, RESTORED the old text, emitted `task.criteria_edit_refused`, and set
the sticky `criteriaEditedAfterValidationFailure` marker (whose
engine-forced blocking finding, `findings.flagged_criteria_findings`,
recurs on every later review pass).

This module drives the full sequence at `LoopSupervisor` tier, no stubbing
of `_check_criteria_edits` itself:

1. A task starts with a criteria baseline already established (as it
   would be after any earlier pass through `_ensure_criteria_baseline`).
2. `state.steer_task_update` edits its `successCriteria` -- the exact tool
   surface a `steer` iteration's `task-update` call goes through
   (`hat_tools.py:108-113`).
3. `LoopSupervisor._step_worker()` runs for real, with a fake agent
   reporting `verdict: "complete"` for that task -- the next worker pass
   the steer edit has to survive. `_step_worker` calls `_check_criteria_
   edits` on this exact task list (loop.py:5533) before the pass ends.

`tests/test_criteria_tamper_refusal_task024.py` is not modified by this
task and stays green unmodified in the same bounded run -- this module
never touches `_check_criteria_edits`'s own refusal behaviour, only proves
that a fingerprint-forward steer edit never trips it in the first place.
"""

from __future__ import annotations

import asyncio
import json

from ralphd.engine import state
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json, read_events
from ralphd.engine import findings as findings_mod


def _make_sup(tmp_path, tasks, vigilant=False):
    root = tmp_path / "run"
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": tasks})
    cfg = JobConfig(run_id="task002-steer-survives-worker-pass", iterations=5,
                    max_approaches=1, vigilant=vigilant, on_complete="exit",
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
                    infra_outage_budget_s=1000.0)
    sup = LoopSupervisor(cfg, run, root)
    return sup, run


def _criteria_edit_refused_events(run: RunDir) -> list[dict]:
    return [e for e in read_events(run.root)
            if e.get("type") == "task.criteria_edit_refused"]


def test_steer_criteria_edit_survives_the_next_worker_pass(tmp_path):
    original = "ship the widget exactly as originally scoped"
    fp = LoopSupervisor._criteria_fingerprint(original)
    sup, run = _make_sup(tmp_path, [
        {"id": "001", "title": "ship the widget", "status": "pending",
         "successCriteria": original,
         "criteriaFingerprint": fp, "criteriaOfRecord": original},
    ])

    # The steer-tool edit -- the exact call `steer_task_update` makes for a
    # `task-update` tool invocation carrying `successCriteria`.
    new_criteria = "ship the widget, relaxed per operator steer 003"
    result = state.steer_task_update(
        run, "001", {"successCriteria": new_criteria}, sup.cfg.vigilant)
    assert result["updated"] is True

    class _Agent:
        def interrupt(self):
            return False

        async def run(self, prompt, transcript, **kw):
            r = IterationResult(exit_code=0)
            r.final_text = json.dumps({"verdict": "complete", "task_id": "001"})
            r.duration_s = 0.01
            r.usage = {"input": 1, "output": 1, "totalTokens": 2}
            return r

    sup.runner = _Agent()  # type: ignore[assignment]
    asyncio.run(sup._step_worker())

    # 1) no refusal event fired on this pass.
    assert _criteria_edit_refused_events(run) == []

    tasks_after = run.read_tasks()["tasks"]
    task = tasks_after[0]

    # 2) the steer's text still holds, no tamper marker, no flagged finding.
    assert task["successCriteria"] == new_criteria
    assert not task.get("criteriaEditedAfterValidationFailure")
    assert findings_mod.flagged_criteria_findings(tasks_after) == []

    # the pass actually completed the task -- the edit did not just
    # silently fail to apply.
    assert task["status"] == "completed"
    assert task["criteriaFingerprint"] == LoopSupervisor._criteria_fingerprint(
        new_criteria)
    assert task["criteriaOfRecord"] == new_criteria
