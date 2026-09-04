"""Task 024 (cure/completion of D.4, PRD requirement D.1/D.2, #142) -- the
one gap task 011/036 left open: `resume` clearing the `rejected_last_approach`
pause must not just flip `status.json` back to `running` (036's own scope) --
it must actually let the SAME, already-running engine process ask `review`
again over the SAME approach, and that second review must be able to reach a
terminal verdict, all IN-PROCESS (one `LoopSupervisor`, no restart, no second
`RunDir` reader standing in for a killed container).

Shape: a single `LoopSupervisor` drives a real `_step_review()` call that
lands on `rejected_last_approach` (the exact fixture task 036's own module
pins in isolation) -- pausing the run and leaving the approach unarchived.
While still paused, the operator records a ruling the way `ralphctl amend
--waive` does host-side (`state.write_amendment(..., waive=True)`), then
`ralphctl resume` calls `sup.resume()`. The SAME supervisor instance then
calls `_step_review()` a second time -- this IS "asking review again": the
identical call site the dispatch loop (`_run_job_core`) itself would make
next, now that the in-process gate (`self._pause`) is set again -- and the
stub reviewer, seeing the ruling in its own prompt, verifies. The run reaches
`("done", "succeeded")` with the approach counter never bumped and no
approach ever archived to disk.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import findings, loop as loop_mod
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import PAUSED_STATE, RunDir, write_amendment

AMENDMENT_MARKER = "Operator rulings (task 028, #26)"


def _finding(severity: str, curable: bool, clause: str, evidence: str) -> dict:
    return {"severity": severity, "curable": curable, "clause": clause,
            "evidence": evidence}


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _write_tasks(run: RunDir) -> None:
    from ralphd.engine.state import atomic_write_json
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [
        {"id": "001", "title": "do the thing", "status": "completed",
         "successCriteria": "the thing is done"},
    ]})


def _supervisor(root: Path, **cfg_kw) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    (root / "prd.md").write_text("# task 024 fixture PRD\n\nDo the thing.\n")
    run = RunDir(root=root)
    run.update_status(state="running", phase="review", approach=1)
    _write_tasks(run)
    kw = dict(max_approaches=1)
    kw.update(cfg_kw)
    cfg = JobConfig(run_id="task024-resume-same-approach", **kw)
    return LoopSupervisor(cfg, run, root / "ws")


def _drive_the_pause_edge(sup: LoopSupervisor) -> None:
    """The exact `rejected_last_approach` fixture `tests/
    test_last_approach_pause_persisted_task036.py` pins in isolation: last
    (only) approach, one blocking-but-curable finding, this approach's own
    cure pass already spent."""
    sup._approach = 1
    sup.run.update_status(curePasses={"1": 1})
    findings.write_findings_json(sup.run.findings_json_file, [
        _finding("blocking", True, "requirement D.1/D.2 (#142)",
                 "the cure did not land before the last cure pass ran out"),
    ])

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected",
            "reason": "the cure did not land",
            "findings_path": "review-findings.md",
        }))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]
    outcome = asyncio.run(sup._step_review())
    assert outcome == ("advance", "review", "rejected_last_approach")


def test_resume_asks_review_again_over_the_same_approach_and_reaches_terminal(
        tmp_path, monkeypatch):
    root = tmp_path / "run"
    sup = _supervisor(root)

    # --- episode: the last-approach pause fires, in-process -------------
    _drive_the_pause_edge(sup)
    assert sup._pause.is_set() is False
    status = sup.run.read_status()
    assert status["state"] == PAUSED_STATE == "paused"
    assert status["pauseReason"] == "rejected_last_approach"
    assert status["approach"] == 1
    assert not (root / "approaches" / "01").exists()
    assert not sup.run.composite_prd_file.exists()

    # --- the operator's ruling, recorded WHILE the run sits paused ------
    cdir = tmp_path / "config"
    write_amendment(cdir, "001",
                    "Waived: the residual gap is cured; re-review the "
                    "same approach.", waive=True)
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", cdir)

    # --- `ralphctl resume` -----------------------------------------------
    sup.resume()
    status = sup.run.read_status()
    assert status["state"] == "running"
    assert status["pauseReason"] is None
    assert status["approach"] == 1          # never bumped
    assert status["phase"] == "review"      # still the phase that paused
    assert sup._pause.is_set() is True       # in-process gate released
    assert asyncio.run(sup._gate()) == 0.0   # the next boundary does not park

    # --- review is asked again, over the SAME approach, SAME process ----
    prompts: list[str] = []

    async def _review_sees_the_ruling_and_verifies(prompt, transcript, **kw):
        prompts.append(prompt)
        assert AMENDMENT_MARKER in prompt
        assert "Waived: the residual gap is cured" in prompt
        # a real review hat re-runs curability on this pass and, seeing the
        # waiver, writes a fresh sidecar with no blocking findings left --
        # `_review_verdict` recomputes the verdict from findings.json on
        # disk, so leaving the stale finding in place would reject again.
        findings.write_findings_json(sup.run.findings_json_file, [])
        return _result(json.dumps({"verdict": "verified"}))

    sup.runner.run = _review_sees_the_ruling_and_verifies  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("done", "succeeded")
    assert len(prompts) == 1
    assert AMENDMENT_MARKER in prompts[0]

    # terminal verdict reached, same approach throughout, never archived
    assert sup._approach == 1
    final_status = sup.run.read_status()
    assert final_status["approach"] == 1
    assert not (root / "approaches" / "01").exists()


def test_resume_without_a_ruling_asks_review_again_and_can_still_reject(
        tmp_path, monkeypatch):
    """Same in-process resume routing, but with no ruling ever recorded --
    proving the terminal `succeeded` above is caused by the ruling reaching
    the second review call, not by anything else in the fixture (the same
    pairing `tests/test_resume_after_incurable_rejection.py` uses for the
    `rejected_incurable` edge)."""
    root = tmp_path / "run"
    sup = _supervisor(root)
    _drive_the_pause_edge(sup)

    cdir = tmp_path / "config"
    cdir.mkdir(parents=True)
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", cdir)

    sup.resume()
    assert sup.run.read_status()["approach"] == 1

    # the stale finding is still on disk -- nothing cured it -- so review
    # self-loops right back onto `rejected_last_approach` again, on the
    # SAME approach, never spending a fresh one that does not exist.
    calls = []

    async def _review_rejects_again(prompt, transcript, **kw):
        calls.append(prompt)
        assert AMENDMENT_MARKER not in prompt
        return _result(json.dumps({
            "verdict": "rejected", "reason": "still no operator ruling",
            "findings_path": "review-findings.md"}))

    sup.runner.run = _review_rejects_again  # type: ignore[method-assign]
    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "review", "rejected_last_approach")
    assert len(calls) == 1
    status = sup.run.read_status()
    assert status["approach"] == 1
    assert status["state"] == "paused"
    assert not (root / "approaches" / "01").exists()
