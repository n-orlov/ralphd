"""v0.39 K.2: a run directory written by 0.38 loads and continues on 0.39.

Three committed fixtures (`tests/fixtures/compat_038/<case>/`, each a run
directory exactly as a 0.38 engine left it: `status.json` with no `spend`
counters, `tasks.json` and the iteration records) are copied out of the tree
and driven through the real `LoopSupervisor._run_job_core` with fake hats:

1. `mid_worker`: killed in the worker phase; the worker loop continues over
   the same plan, the first VERIFIED ends the run, and the spend counters
   are rebuilt from the iteration records (nothing is capped, so no
   `spend.*` event and no `spend` line is written for the operator);
2. `awaiting_input`: parked on a findings question; the park is kept as
   persisted (same question, same `expiresAt`) and an answer releases it;
3. `exhausted_iterations`: held on the iteration budget; a bare `resume` is
   refused naming `--iterations`, `resume --iterations +N` is accepted, and
   the engine then continues at the held worker step.

Nothing sleeps: the clock is a fake and every hat is scripted.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from test_cli_docker import ctl, unix_sock          # noqa: F401
from test_cli_resume import _seed_run

from ralphd.engine import loop as loop_mod
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir
from tests.conftest import ScriptedRunner, attach_runner

__all__ = ["ctl", "unix_sock"]

FIXTURES = Path(__file__).parent / "fixtures" / "compat_038"
NOW = 1_795_000_000.0          # before the fixtures' expiry
CASES = ("mid_worker", "awaiting_input", "exhausted_iterations")


def _ok(payload: dict) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = json.dumps(payload)
    r.duration_s = 5.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _load(case: str, tmp_path: Path, monkeypatch, *, iterations: int = 30):
    """Copy the 0.38 run dir out of the tree and build the supervisor a
    fresh engine process would build over it."""
    root = tmp_path / case
    shutil.copytree(FIXTURES / case, root)
    config_dir = root / "config"
    config_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", config_dir)
    ws = tmp_path / "ws"
    ws.mkdir()
    sup = LoopSupervisor(
        JobConfig(run_id="legacy-038", iterations=iterations,
                  max_approaches=2, vigilant=False, on_complete="exit",
                  peer_verdict_gate=False, infra_retry_backoff_s=[0.0],
                  infra_retry_backoff_max_s=0.0, infra_outage_budget_s=0.0),
        RunDir(root=root), ws)
    sup._now_epoch = lambda: NOW                     # the fake clock
    sup._wait_out_backoff = lambda *a, **k: asyncio.sleep(0)  # type: ignore
    return sup


def _tasks(sup) -> dict:
    return {t["id"]: t["status"] for t in sup.run.read_tasks()["tasks"]}


def _bounded(coro, timeout: float = 30.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _events(sup, prefix: str) -> list[dict]:
    log = sup.run.root / "events.jsonl"
    if not log.is_file():
        return []
    return [e for e in (json.loads(x) for x in log.read_text().splitlines()
                        if x.strip()) if str(e.get("type", "")).startswith(prefix)]


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


def test_the_fixtures_are_0_38_run_dirs_with_no_spend_state():
    for case in CASES:
        status = json.loads((FIXTURES / case / "status.json").read_text())
        assert status["engineVersion"] == "0.38.0"
        assert "spend" not in status
        assert not (FIXTURES / case / "config").exists()


def test_a_run_killed_mid_worker_continues_its_plan_to_one_review(
        tmp_path, monkeypatch):
    sup = _load("mid_worker", tmp_path, monkeypatch)
    assert sup.iterations_used == 2           # numbering continues from disk
    # the engine process seeds counters rebuilt from the 0.38 records
    assert "spend" not in sup.run.read_status()
    assert sup.run.seed_spend() is True
    assert sup.run.read_spend()["overall"] == {
        "planning": 0.5, "worker": 0.25, "total": 0.75}
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
    # the continued iterations add to the rebuilt counters; nothing is
    # capped, so nothing is announced
    assert sup.run.read_spend()["overall"]["total"] == 0.75
    assert _events(sup, "spend.") == []
    assert not sup.cfg.spend_caps


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
    assert _events(sup, "input.timed_out") == []
    assert _events(sup, "spend.") == []


def test_an_iterations_exhausted_run_holds_on_the_same_budget_and_resumes_with_iterations(
        tmp_path, monkeypatch, ctl):
    # engine side: the unchanged budget holds the run again, on `iterations`
    sup0 = _load("exhausted_iterations", tmp_path, monkeypatch, iterations=2)
    assert _bounded(sup0._run_job_core()) == "exhausted"
    held = sup0.run.read_status()
    assert held["state"] == "exhausted" and held["exhaustedBudget"] == "iterations"

    # CLI side: a bare resume is refused, `--iterations +4` is accepted
    rdir, cdir = _seed_run(ctl, "legacy-038", iterations=2)
    (rdir / "status.json").write_text(
        (FIXTURES / "exhausted_iterations" / "status.json").read_text())
    env = {"STUB_DOCKER_CONTAINERS": "ralphd-legacy-038",
           "STUB_DOCKER_RUNNING": ""}
    bare = ctl.run("resume", "legacy-038", env=env)
    assert bare.returncode == 2, bare.stderr
    assert "exhausted: iterations 2/2 — ralphctl resume legacy-038 --iterations +10" \
        in bare.stderr
    ok = ctl.run("resume", "legacy-038", "--iterations", "+4", env=env)
    assert ok.returncode == 0, ok.stderr
    assert "iterations: 6" in (cdir / "job.yaml").read_text()
    assert "spend_caps" not in (cdir / "job.yaml").read_text()

    # engine side again, with the topped-up budget: the held worker step carries on
    (tmp_path / "again").mkdir()
    sup = _load("exhausted_iterations", tmp_path / "again", monkeypatch,
                iterations=6)
    done: list[str] = []
    reviews: list[int] = []
    _scripted(sup, _worker_hat(done), reviews)

    final = _bounded(sup._run_job_core())

    assert final == "succeeded"
    assert done == ["002", "003"]
    assert len(reviews) == 1
    status = sup.run.read_status()
    assert status["verdict"] == "verified"
    assert "exhaustedBudget" not in status
    assert _events(sup, "spend.") == []
