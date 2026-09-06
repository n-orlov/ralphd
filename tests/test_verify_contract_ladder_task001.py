"""Task 001 (#119, requirement A.1): `_verify_task` parses its reply through
`contract_enforcement.enforce_contract` -- with `extra_validate` covering
`transitions.verify_report_errors` and the `task_id == tid` cross-check --
instead of reading a not-yet-valid `verify` payload once and immediately
burning a validation attempt on it.

Before this task, a verify reply whose last line was not a well-formed
`verify` contract payload (a prose line, a `task_id` naming the wrong task,
...) went straight to `_verify_task`'s own manual parse-and-record block:
`validationAttempts` incremented and `status` forced to
`validation-failed` on the FIRST such reply, with no chance for the hat to
correct it -- unlike every other hat's own payload, which already gets
`contract_enforcement.enforce_contract`'s uncharged re-ask ladder
(`_step_generic_hat`, `_step_worker`, `_review_contract_ladder`).

Three things pinned here:

1. a prose final line, repaired by the ladder's uncharged re-ask, leaves
   `validationAttempts` unchanged and the task reaching `validated` (never
   burning an attempt on a reply the hat could -- and, once asked, did --
   correct);
2. the re-asks are recorded under `usage.byPhase["contract"]` and charge no
   iteration -- `iterations_used` advances by exactly one across the whole
   episode, same invariant `test_worker_contract_reask_end_to_end_task008.py`
   pins for the worker path;
3. ladder exhaustion (the hat never produces a valid payload across every
   attempt) still produces exactly today's verdict-miss/validation-failed
   outcome -- this task changes WHEN a broken payload gets a chance to be
   repaired, never what happens once that chance is spent.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import supervisor_with_runner

TASK = {
    "id": "007",
    "title": "the task under verification",
    "status": "completed",
    "successCriteria": "the thing works",
    "validationAttempts": 1,
    "validationNotes": "first round: the CLI flag was missing (worker's note)",
}


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0, duration_s=5.0)
    r.final_text = text
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


class _SequencedRunner:
    """Replays one `IterationResult` per call, in order (the last one
    repeats forever once the sequence runs out) -- distinct from
    `test_verify_no_verdict.py`'s `_FixedRunner`, which replays the SAME
    result for every call, because these tests need the charged verify
    call and the uncharged re-ask(s) that follow it to answer
    differently."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0
        self.prompts: list[str] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.prompts.append(prompt)
        idx = min(self.calls, len(self.results) - 1)
        self.calls += 1
        return self.results[idx]


_supervisor = supervisor_with_runner(
    lambda sup, results: _SequencedRunner(results),
    run_id="task001-verify-ladder", iterations=8, max_approaches=1, vigilant=True,
    on_complete="exit", infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
    infra_outage_budget_s=0.0, tasks=[dict(TASK)])


def _events(run_dir: Path) -> list[dict]:
    p = run_dir / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


# -- 1. a prose final line is repaired, no attempt spent -------------------


def test_a_prose_final_line_is_repaired_on_reask_leaving_attempts_unchanged(
        tmp_path):
    prose = "let me check the criteria... yes, this looks complete to me"
    corrected = json.dumps({"task_id": TASK["id"], "verdict": "verified"})

    sup = _supervisor(tmp_path, [_ok(prose), _ok(corrected)])

    verified = asyncio.run(sup._verify_task(dict(TASK)))

    assert verified is True
    t = json.loads(sup.run.tasks_file.read_text())["tasks"][0]
    assert t["status"] == "validated"
    # never touched -- the re-ask fixed the FIRST attempt's own reply, so
    # no validation-failed round was ever recorded against this task.
    assert t["validationAttempts"] == TASK["validationAttempts"]
    assert t["validationNotes"] == TASK["validationNotes"]

    # the ladder's re-ask prompt states the specific parse failure, not a
    # generic complaint -- mirrors the worker path's own re-ask pinning.
    assert sup.runner.calls == 2
    reask_prompt = sup.runner.prompts[1]
    assert "malformed JSON" in reask_prompt


def test_a_task_id_naming_the_wrong_task_is_repaired_on_reask(tmp_path):
    wrong_task = json.dumps({"task_id": "999", "verdict": "verified"})
    corrected = json.dumps({"task_id": TASK["id"], "verdict": "verified"})

    sup = _supervisor(tmp_path, [_ok(wrong_task), _ok(corrected)])

    verified = asyncio.run(sup._verify_task(dict(TASK)))

    assert verified is True
    t = json.loads(sup.run.tasks_file.read_text())["tasks"][0]
    assert t["status"] == "validated"
    assert t["validationAttempts"] == TASK["validationAttempts"]

    reask_prompt = sup.runner.prompts[1]
    assert "999" in reask_prompt
    assert "the one under verification" in reask_prompt


# -- 2. the re-ask is uncharged and lands under byPhase.contract -----------


def test_the_reask_charges_no_iteration_and_is_recorded_under_contract_phase(
        tmp_path):
    prose = "still reading the diff"
    corrected = json.dumps({"task_id": TASK["id"], "verdict": "verified"})

    sup = _supervisor(tmp_path, [_ok(prose), _ok(corrected)])

    iterations_before = sup.iterations_used
    verified = asyncio.run(sup._verify_task(dict(TASK)))
    assert verified is True

    # exactly one CHARGED iteration for the whole episode -- the re-ask
    # never opens a second iteration slot.
    assert sup.iterations_used == iterations_before + 1
    iteration_dirs = sorted(p.name for p in (sup.run.root / "iterations").iterdir())
    assert len(iteration_dirs) == 1

    status = sup.run.read_status()
    by_phase = status.get("usage", {}).get("byPhase", {})
    assert "contract" in by_phase, by_phase
    assert by_phase["contract"].get("totalTokens", 0) > 0

    # exactly one contract-violation event recorded for the repaired attempt
    events = _events(sup.run.root)
    violations = [e for e in events if e.get("type") == "contract.violation"]
    faults = [e for e in events if e.get("type") == "contract.agent_fault"]
    assert len(violations) == 1
    assert faults == []


# -- 3. ladder exhaustion still produces today's outcome --------------------


def test_ladder_exhaustion_still_produces_the_verdict_miss_validation_failed_outcome(
        tmp_path):
    """The hat never produces a valid payload, across every attempt the
    ladder grants (`contract_retry_max + 1`, default 3) -- exactly the
    pre-task-001 outcome: one validation-failed round recorded, with the
    generic "no valid contract payload" note."""
    prose = "still not sure, let me look again"

    sup = _supervisor(tmp_path, [_ok(prose)])   # every call replays `prose`

    task = dict(TASK)
    task["validationNotes"] = ""
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": [task]})

    verified = asyncio.run(sup._verify_task(dict(task)))

    assert verified is False
    t = json.loads(sup.run.tasks_file.read_text())["tasks"][0]
    assert t["status"] == "validation-failed"
    assert t["validationAttempts"] == TASK["validationAttempts"] + 1
    notes = t["validationNotes"]
    assert "contract payload" in notes
    assert "verdict" in notes.lower()

    # the ladder made its full 1 + contract_retry_max attempts (1 reused +
    # `contract_retry_max` uncharged re-asks) before giving up -- still only
    # ONE charged iteration.
    assert sup.runner.calls == 1 + sup.cfg.contract_retry_max
    assert sup.iterations_used == 1

    events = _events(sup.run.root)
    faults = [e for e in events if e.get("type") == "contract.agent_fault"]
    assert len(faults) == 1
    assert faults[0]["hat"] == "verify"
