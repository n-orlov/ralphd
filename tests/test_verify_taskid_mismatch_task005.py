"""Task 005 (#120, requirement A.5): the task_id-mismatch guard inside
`_verify_task`/`_verify_contract_ladder` -- a schema-valid `verify` payload
whose `task_id` names some OTHER task than the one under verification is a
contract violation (`transitions`-style semantic check layered on top of
`contracts.validate_payload`, task 001/#119), not a silent misattribution
of a verdict meant for one task onto a different one.

Task 001's own module (`test_verify_contract_ladder_task001.py`) already
pins the REPAIRED case -- a wrong-`task_id` reply corrected on the ladder's
uncharged re-ask reaches `validated`. This module pins the tier one level
up (#120, A.5): a verifier that answers with the WRONG task_id on every
single ladder attempt, never once naming the task actually under
verification -- the ladder exhausts, and the mismatch is what lands on the
task record and on `events.jsonl`, at the full in-process `LoopSupervisor`
tier (a real `tasks.json` with TWO tasks on disk, a real `_verify_task`
call, no mocking below `sup.runner`).

Three things pinned:

1. verifying task "001" against a verifier that always reports
   `{"task_id": "002", "verdict": "verified"}` exhausts the ladder and
   leaves task "001" `validation-failed`, with the mismatch text in its
   `validationNotes`;
2. task "002" (the task the verifier kept naming, but which was never
   under verification) is untouched -- byte-identical to its pre-run
   record;
3. a `contract.violation` event fires carrying the guard's message; and a
   negative twin -- a verifier that answers with the CORRECT task_id --
   reaches `validated` (the guard does not misfire on a genuine match).
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import supervisor_with_runner

TASK1 = {
    "id": "001",
    "title": "the task under verification",
    "status": "completed",
    "successCriteria": "the thing works",
    "validationAttempts": 0,
    "validationNotes": "",
}

# A second, unrelated task -- the one the verifier keeps (wrongly) naming.
# Deliberately carries fields TASK1 does not, so a test that mutated the
# wrong record would be caught by more than just `status`.
TASK2 = {
    "id": "002",
    "title": "an unrelated task the verifier keeps misnaming",
    "status": "in-progress",
    "successCriteria": "something else entirely",
    "validationAttempts": 1,
    "validationNotes": "an earlier, unrelated validation-failed round",
    "priority": 42,
}


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0, duration_s=5.0)
    r.final_text = text
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


class _SequencedRunner:
    """Replays one `IterationResult` per call, in order (the last one
    repeats forever once the sequence runs out) -- same shape as
    `test_verify_contract_ladder_task001.py`'s helper."""

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
    run_id="task005-verify-taskid-mismatch", iterations=8, max_approaches=1, vigilant=True,
    on_complete="exit", infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
    infra_outage_budget_s=0.0, tasks=[dict(TASK1), dict(TASK2)])


def _events(run_dir: Path) -> list[dict]:
    p = run_dir / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _task(tasks_file: Path, tid: str) -> dict:
    data = json.loads(tasks_file.read_text())
    for t in data["tasks"]:
        if t["id"] == tid:
            return t
    raise AssertionError(f"task {tid!r} not found in {tasks_file}")


def test_a_persistent_wrong_task_id_exhausts_the_ladder_and_is_recorded_as_a_mismatch(
        tmp_path):
    wrong_task = json.dumps({"task_id": "002", "verdict": "verified"})

    sup = _supervisor(tmp_path, [_ok(wrong_task)])   # every call replays it

    verified = asyncio.run(sup._verify_task(dict(TASK1)))

    assert verified is False

    t1 = _task(sup.run.tasks_file, "001")
    assert t1["status"] == "validation-failed"
    assert t1["validationAttempts"] == TASK1["validationAttempts"] + 1
    notes = t1["validationNotes"]
    assert "task_id" in notes
    assert "'002'" in notes
    assert "is the one under verification" in notes

    # task "002" -- the task the verifier kept (wrongly) naming, but which
    # was never under verification -- is byte-identical to its pre-run
    # record.
    t2 = _task(sup.run.tasks_file, "002")
    assert t2 == TASK2

    # the ladder made its full 1 + contract_retry_max attempts before
    # giving up, all charged to no more than one iteration.
    assert sup.runner.calls == 1 + sup.cfg.contract_retry_max
    assert sup.iterations_used == 1

    events = _events(sup.run.root)
    violations = [e for e in events if e.get("type") == "contract.violation"]
    assert violations, "expected at least one contract.violation event"
    assert any(
        any("is the one under verification" in err for err in e.get("errors", []))
        for e in violations
    ), violations
    assert any(
        any("'002'" in err for err in e.get("errors", []))
        for e in violations
    ), violations


def test_negative_twin_the_correct_task_id_reaches_validated(tmp_path):
    correct = json.dumps({"task_id": "001", "verdict": "verified"})

    sup = _supervisor(tmp_path, [_ok(correct)])

    verified = asyncio.run(sup._verify_task(dict(TASK1)))

    assert verified is True
    t1 = _task(sup.run.tasks_file, "001")
    assert t1["status"] == "validated"

    # task "002" is untouched here too -- nothing about this run should
    # ever have written to it.
    t2 = _task(sup.run.tasks_file, "002")
    assert t2 == TASK2

    events = _events(sup.run.root)
    violations = [e for e in events if e.get("type") == "contract.violation"]
    assert violations == []
