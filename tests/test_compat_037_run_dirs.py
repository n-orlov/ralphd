"""v0.38 K.2: a run directory written by 0.37 loads and continues on 0.38.

Three committed fixtures (`tests/fixtures/compat_037/<case>/`, each a run
directory exactly as a 0.37 engine left it: `status.json` with no
`reviewStateKeys`, `tasks.json`, the iteration records, and for the third
case the operator's `amendments/` entry) are copied out of the tree and
driven through the real `LoopSupervisor._run_job_core` with fake hats:

1. `mid_worker`: killed in the worker phase; the worker loop continues over
   the same plan and the first VERIFIED ends the run, the review dispatched
   once;
2. `awaiting_input`: parked on a findings question; the park is kept as
   persisted (same question, same `expiresAt`) and an answer releases it;
3. `petitioned_waived`: a `petitioned` task holding an operator waiver that
   0.37 recorded (`amend --waive`). 0.37 left its dependent blocked forever;
   0.38 reads the waiver as SETTLING the petitioned task, so the dependent is
   scheduled, the run reaches `succeeded`, and the petitioned task keeps its
   status (no status lies).

Nothing sleeps: the clock is a fake and every hat is scripted.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from ralphd.engine import loop as loop_mod
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir
from tests.conftest import ScriptedRunner, attach_runner

FIXTURES = Path(__file__).parent / "fixtures" / "compat_037"
NOW = 1_795_000_000.0          # 2026-11-18 .. before the fixtures' expiry


def _ok(payload: dict) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = json.dumps(payload)
    r.duration_s = 5.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _load(case: str, tmp_path: Path, monkeypatch, **cfg):
    """Copy the 0.37 run dir out of the tree and build the supervisor a
    fresh engine process would build over it."""
    root = tmp_path / case
    shutil.copytree(FIXTURES / case, root)
    config_dir = root / "config"
    config_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", config_dir)
    ws = tmp_path / "ws"
    ws.mkdir()
    run = RunDir(root=root)
    sup = LoopSupervisor(
        JobConfig(run_id="legacy-037", iterations=30, max_approaches=2,
                  vigilant=False, on_complete="exit", peer_verdict_gate=False,
                  infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
                  infra_outage_budget_s=0.0, **cfg),
        run, ws)
    sup._now_epoch = lambda: NOW                     # the fake clock
    sup._wait_out_backoff = lambda *a, **k: asyncio.sleep(0)  # type: ignore
    return sup


def _tasks(sup) -> dict:
    return {t["id"]: t["status"] for t in sup.run.read_tasks()["tasks"]}


def _bounded(coro, timeout: float = 30.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _worker_hat(done: list):
    def hat(sup, prompt):
        assigned = [t["id"] for t in sup.run.read_tasks()["tasks"]
                    if t["status"] == "in-progress"]
        assert len(assigned) == 1, assigned
        done.append(assigned[0])
        return _ok({"verdict": "complete", "task_id": assigned[0]})
    return hat


def _scripted(sup, worker, reviews: list):
    def review(sup_, prompt):
        reviews.append(1)
        return _ok({"verdict": "verified"})
    return attach_runner(sup, ScriptedRunner({
        "worker": [worker], "review": [review]}))


def test_the_fixtures_are_0_37_run_dirs_with_no_0_38_state():
    for case in ("mid_worker", "awaiting_input", "petitioned_waived"):
        status = json.loads((FIXTURES / case / "status.json").read_text())
        assert status["engineVersion"] == "0.37.0"
        assert "reviewStateKeys" not in status


def test_a_run_killed_mid_worker_continues_its_plan_to_one_review(
        tmp_path, monkeypatch):
    sup = _load("mid_worker", tmp_path, monkeypatch)
    assert sup.iterations_used == 2           # numbering continues from disk
    done: list[str] = []
    reviews: list[int] = []
    _scripted(sup, _worker_hat(done), reviews)

    final = _bounded(sup._run_job_core())

    assert final == "succeeded"
    assert done == ["002", "003"]             # 001 was already completed
    assert len(reviews) == 1
    assert _tasks(sup) == {"001": "completed", "002": "completed",
                           "003": "completed"}
    status = sup.run.read_status()
    assert status["verdict"] == "verified"
    assert status["iterationsUsed"] == 2 + 2 + 1
    assert status.get("inputQuestion") is None


def test_an_awaiting_input_run_keeps_its_park_and_an_answer_releases_it(
        tmp_path, monkeypatch):
    sup = _load("awaiting_input", tmp_path, monkeypatch)
    saved = json.loads(
        (FIXTURES / "awaiting_input" / "status.json").read_text())
    seen: dict = {}

    async def wait(timeout_s: float) -> bool:
        if not seen:
            status = sup.run.read_status()
            seen.update(state=status["state"],
                        question=dict(status["inputQuestion"]),
                        reason=status["pauseReason"])
            sup.answer(waive=["F1"])                 # the operator answers
        return sup._pause.is_set()
    sup._wait_pause_event = wait                     # type: ignore[method-assign]
    reviews: list[int] = []
    _scripted(sup, _worker_hat([]), reviews)

    final = _bounded(sup._run_job_core())

    assert seen["state"] == "awaiting_input"
    assert seen["question"] == saved["inputQuestion"]
    assert seen["reason"] == "operator_input_needed"
    assert final == "succeeded"
    assert sup.run.read_status()["verdict"] == "verified"
    assert not [e for e in _events(sup, "input.timed_out")]


def _events(sup, kind: str) -> list[dict]:
    log = sup.run.root / "events.jsonl"
    return [e for e in (json.loads(x) for x in log.read_text().splitlines()
                        if x.strip()) if e.get("type") == kind]


def test_a_waiver_0_37_recorded_now_settles_the_petitioned_task_for_its_dependent(
        tmp_path, monkeypatch):
    sup = _load("petitioned_waived", tmp_path, monkeypatch)
    doc = sup.run.read_tasks()
    assert sup._settled_petitioned_ids(doc) == {"001"}
    done: list[str] = []
    reviews: list[int] = []
    _scripted(sup, _worker_hat(done), reviews)

    final = _bounded(sup._run_job_core())

    assert final == "succeeded"
    assert done == ["002"]                    # the dependent of the waived task
    assert len(reviews) == 1
    # the petitioned task keeps its status; only the dependent completed
    assert _tasks(sup) == {"001": "petitioned", "002": "completed"}
    assert sup.run.read_status()["verdict"] == "verified"


def test_without_the_waiver_the_petitioned_task_still_holds_its_dependent(
        tmp_path, monkeypatch):
    """Control: the same run dir minus the operator's waiver is not settled."""
    sup = _load("petitioned_waived", tmp_path, monkeypatch)
    shutil.rmtree(sup.run.root / "config" / "amendments")
    assert sup._settled_petitioned_ids(sup.run.read_tasks()) == frozenset()
