"""Task 029 (#26): pin both review directions for an amended criterion.

Task 028 proved the amendment channel is unforgeable and that its text
reaches the rendered review prompt (`tests/test_operator_ruling.py`), but
none of those tests ever produced an actual review VERDICT from it -- they
only inspected prompt strings. This module drives a full, otherwise-
deadlocked job through `LoopSupervisor._run_job_core()` with a stub runner
that plays the reviewer's own role: it reads the SAME rendered prompt text
the real review phase would see and answers `VERIFIED` only when the
operator's ruling text is present in it, exactly mirroring what
`_operator_amendment_review_context()` promises review will be told to do
(review.md's "Operator rulings" section, task 028).

Two assertions, both fast-tier:

* with a `ralphctl amend --waive` ruling recorded under `CONFIG_DIR`, the
  run's terminal state is `succeeded` -- the amendment flips an otherwise
  dead-ended run;
* the SAME job, same tasks.json, same worker behaviour, but with an EMPTY
  `CONFIG_DIR` (no ruling ever written) still ends `failed` -- proving the
  flip is caused by the amendment reaching the prompt, not by anything else
  in the fixture.

The deadlock shape reused here is task 024's (#33) own route-to-review path
(`LoopSupervisor._unresolved_failures`): a worker that marks its one task
`failed` and then makes no further progress is routed straight to review
for a verdict instead of stagnating -- so review's answer is the ONLY thing
that decides the run's fate, which is exactly the property this task is
pinning.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine import loop as loop_mod
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json, write_amendment

AMENDMENT_MARKER = "Operator rulings (task 028, #26)"


class _PromptAwareRunner:
    """Stands in for PiRunner at the same seam `_ScriptedRunner` (task 011's
    test module) does, except each step also sees the ACTUAL rendered
    prompt text -- the review step needs it to decide its own answer the
    way a real reviewer following review.md's instructions would."""

    def __init__(self, run: RunDir, steps: list):
        self.rundir = run
        self.steps = steps
        self.calls = 0
        self.running = False
        self.prompts: list[str] = []

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.calls += 1
        self.running = True
        self.prompts.append(prompt)
        try:
            step = self.steps[min(self.calls - 1, len(self.steps) - 1)]
            return step(self.rundir, prompt)
        finally:
            self.running = False


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 30.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _plan(run: RunDir, _prompt: str) -> IterationResult:
    atomic_write_json(run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "001", "title": "impossible without a waiver",
         "successCriteria": "do something the worker cannot do alone"},
    ]})
    return _ok("planned\n" +
              json.dumps({"proposal_path": "plan-proposal.json", "summary": "planned"}))


def _worker_marks_failed(run: RunDir, _prompt: str) -> IterationResult:
    doc = run.read_tasks()
    doc["tasks"][0]["status"] = "failed"
    doc["tasks"][0]["failureKind"] = "requirement-unmet"
    atomic_write_json(run.tasks_file, doc)
    return _ok("cannot satisfy 001 without operator sign-off; marking failed")


def _worker_stuck(run: RunDir, _prompt: str) -> IterationResult:
    # No change at all -- the stagnation+blocked combination that routes
    # straight to review (task 024, #33) rather than looping forever.
    return _ok("still stuck on 001, nothing else to do")


def _review_honours_the_ruling(_run: RunDir, prompt: str) -> IterationResult:
    if AMENDMENT_MARKER in prompt:
        return _ok("operator ruling found for task 001; honouring it\n"
                   + json.dumps({"verdict": "verified"}))
    return _ok("task 001 failed and no operator ruling was found; rejecting\n"
               + json.dumps({"verdict": "rejected",
                             "reason": "no operator ruling found for task 001"}))


def _supervisor(tmp_path: Path) -> LoopSupervisor:
    run = RunDir(root=tmp_path / "run")
    cfg = JobConfig(run_id="unit", iterations=8, max_approaches=1,
                    vigilant=False, on_complete="exit",
                    infra_retry_backoff_s=[0.0],
                    infra_retry_backoff_max_s=0.0,
                    infra_outage_budget_s=1000.0)
    sup = LoopSupervisor(cfg, run, tmp_path / "ws")
    sup.runner = _PromptAwareRunner(run, [
        _plan,
        _worker_marks_failed,
        _worker_stuck,
        _review_honours_the_ruling,
    ])  # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff  # type: ignore[method-assign]
    return sup


def test_an_amended_criterion_flips_an_otherwise_deadlocked_run_to_succeeded(
        tmp_path, monkeypatch):
    cdir = tmp_path / "config"
    write_amendment(cdir, "001", "Waived: this cannot be done without an "
                    "operator sign-off.", waive=True)
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", cdir)

    sup = _supervisor(tmp_path)
    state = asyncio.run(sup._run_job_core())

    assert state == "succeeded"
    status = sup.run.read_status()
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"
    # the review prompt genuinely carried the ruling -- not a fluke pass
    review_prompt = sup.runner.prompts[-1]
    assert AMENDMENT_MARKER in review_prompt
    assert "task 001" in review_prompt
    assert "Waived: this cannot be done without an operator sign-off." in review_prompt


def test_the_same_run_without_the_amendment_still_fails(tmp_path, monkeypatch):
    cdir = tmp_path / "config"  # created but left empty: no ruling ever written
    cdir.mkdir(parents=True)
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", cdir)

    sup = _supervisor(tmp_path)
    state = asyncio.run(sup._run_job_core())

    assert state == "failed"
    status = sup.run.read_status()
    assert status["state"] == "failed"
    assert status["verdict"] == "unverified"
    review_prompt = sup.runner.prompts[-1]
    assert AMENDMENT_MARKER not in review_prompt


def test_config_dir_never_even_created_is_the_same_as_empty(tmp_path, monkeypatch):
    """`amendment_entries()` on a CONFIG_DIR that does not exist at all must
    behave identically to an existing-but-empty one -- the common case for
    any run nobody has ever amended."""
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", tmp_path / "never-created")

    sup = _supervisor(tmp_path)
    state = asyncio.run(sup._run_job_core())

    assert state == "failed"
