"""Task 010 (#51): the effective iteration budget at startup is
max(status.json's recorded `iterationsBudget`, job.yaml's `iterations`),
never job.yaml's alone.

Without this, a live top-up (`PATCH /config/budget` / `ralphctl budget`)
that already bumped status.json's `iterationsBudget` above job.yaml's
`iterations` is silently undone the instant the container restarts: the
engine reloads job.yaml from scratch (a read-only mount an operator cannot
edit in place) and forgets the larger number status.json already recorded.
Conversely job.yaml is allowed to raise the floor -- an operator who
recreated the container with a bigger `iterations:` in job.yaml must see
that number win over a smaller (or absent) status.json figure.

Three cases, all black-box against a real `amain()` with a stubbed
`run_job` (the same pattern as
tests/test_status_max_approaches.py::test_the_first_status_write_already_carries_the_denominator):
status.json higher wins, job.yaml higher wins, and no status.json at all
(a fresh run dir) leaves job.yaml's number untouched. Each case also pins
the one log/event pair `_seed_iteration_budget` emits, naming both source
numbers and which one won -- so the decision is auditable, not just
effective.
"""

from __future__ import annotations

import json
import socket

import pytest
import uvicorn

from ralphd.engine.loop import LoopSupervisor


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _events(run_dir):
    f = run_dir / "events.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text().splitlines() if line]


async def _run_amain_capturing_cfg(tmp_path, monkeypatch, *, job_iterations,
                                   status_iterations_budget):
    from ralphd.engine import config as config_mod
    from ralphd.engine import main as main_mod
    from ralphd.engine import skills as skills_mod

    cfg_dir, run_dir, ws = tmp_path / "cfg", tmp_path / "run", tmp_path / "ws"
    for d in (cfg_dir, run_dir, ws):
        d.mkdir()
    (cfg_dir / "prd.md").write_text("# PRD\n")
    (cfg_dir / "job.yaml").write_text(
        f"run_id: budget-seed\niterations: {job_iterations}\non_complete: exit\n")
    if status_iterations_budget is not None:
        (run_dir / "status.json").write_text(json.dumps(
            {"runId": "budget-seed", "state": "running",
             "iterationsBudget": status_iterations_budget}))

    monkeypatch.setattr(config_mod, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(main_mod, "CONFIG_DIR", cfg_dir)
    # Task 026: see test_status_max_approaches.py's identical comment --
    # `skills.py` binds its own `CONFIG_DIR` at import time, separate from
    # `config_mod.CONFIG_DIR`; task 002's `place_skills` refuses any other
    # `config_dir`, so this needs patching too.
    monkeypatch.setattr(skills_mod, "CONFIG_DIR", cfg_dir)
    monkeypatch.setenv("RALPHD_RUN_DIR", str(run_dir))
    monkeypatch.setenv("RALPHD_WORKSPACE_DIR", str(ws))
    monkeypatch.setenv("RALPHD_PORT", str(_free_port()))

    seen: list[int] = []

    async def fake_serve(self):          # no socket, no port bound
        return None

    async def fake_run_job(self):
        seen.append(self.cfg.iterations)
        return "aborted"

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    monkeypatch.setattr(LoopSupervisor, "run_job", fake_run_job)

    assert await main_mod.amain() == 1    # not succeeded -> 1, as ever
    assert len(seen) == 1
    return seen[0], run_dir


@pytest.mark.asyncio
async def test_status_json_higher_budget_wins(tmp_path, monkeypatch):
    effective, run_dir = await _run_amain_capturing_cfg(
        tmp_path, monkeypatch, job_iterations=10, status_iterations_budget=40)
    assert effective == 40

    seeded = [e for e in _events(run_dir) if e["type"] == "budget_seeded"]
    assert len(seeded) == 1, seeded
    assert seeded[0]["statusJsonIterations"] == 40
    assert seeded[0]["jobYamlIterations"] == 10
    assert seeded[0]["effectiveIterations"] == 40
    assert seeded[0]["source"] == "status.json"


@pytest.mark.asyncio
async def test_job_yaml_higher_budget_wins(tmp_path, monkeypatch):
    effective, run_dir = await _run_amain_capturing_cfg(
        tmp_path, monkeypatch, job_iterations=50, status_iterations_budget=10)
    assert effective == 50

    seeded = [e for e in _events(run_dir) if e["type"] == "budget_seeded"]
    assert len(seeded) == 1, seeded
    assert seeded[0]["statusJsonIterations"] == 10
    assert seeded[0]["jobYamlIterations"] == 50
    assert seeded[0]["effectiveIterations"] == 50
    assert seeded[0]["source"] == "job.yaml"


@pytest.mark.asyncio
async def test_no_status_file_leaves_job_yaml_untouched(tmp_path, monkeypatch):
    effective, run_dir = await _run_amain_capturing_cfg(
        tmp_path, monkeypatch, job_iterations=30, status_iterations_budget=None)
    assert effective == 30
    assert not (run_dir / "status.json").exists() or json.loads(
        (run_dir / "status.json").read_text()).get("iterationsBudget") is None

    seeded = [e for e in _events(run_dir) if e["type"] == "budget_seeded"]
    assert len(seeded) == 1, seeded
    assert seeded[0]["statusJsonIterations"] is None
    assert seeded[0]["jobYamlIterations"] == 30
    assert seeded[0]["effectiveIterations"] == 30
    assert seeded[0]["source"] == "job.yaml"


@pytest.mark.asyncio
async def test_equal_budgets_prefer_job_yaml_source_label(tmp_path, monkeypatch):
    """Equal numbers are not a win for status.json -- job.yaml is the
    ordinary source of truth and only loses when status.json is strictly
    bigger."""
    effective, run_dir = await _run_amain_capturing_cfg(
        tmp_path, monkeypatch, job_iterations=25, status_iterations_budget=25)
    assert effective == 25

    seeded = [e for e in _events(run_dir) if e["type"] == "budget_seeded"]
    assert seeded[0]["source"] == "job.yaml"


# -- v0.30 F.3: the rate-limit wait ceiling survives a resume -------------------

def _seed_run(tmp_path, status: dict | None):
    from ralphd.engine.state import RunDir

    root = tmp_path / "run"
    root.mkdir()
    run = RunDir(root=root)
    if status is not None:
        run.update_status(**status)
    return run


@pytest.mark.parametrize("status,job,expected", [
    ({"rateLimitMaxWaitS": 172800}, 86400, 172800),   # the live raise wins
    ({"rateLimitMaxWaitS": 3600}, 86400, 86400),      # job.yaml raises the floor
    (None, 7200, 7200),                               # a fresh run dir
    ({"rateLimitMaxWaitS": True}, 86400, 86400),      # not a number of seconds
])
def test_rate_limit_max_wait_seed(tmp_path, status, job, expected):
    from ralphd.engine.config import JobConfig
    from ralphd.engine.main import _seed_rate_limit_max_wait

    run = _seed_run(tmp_path, status)
    cfg = JobConfig(run_id="rlw-seed", rate_limit_max_wait_s=job)
    _seed_rate_limit_max_wait(cfg, run)
    assert cfg.rate_limit_max_wait_s == expected


def test_a_live_rate_limit_ceiling_raise_survives_a_fresh_engine(tmp_path):
    """`set_rate_limit_max_wait` (PATCH /config/budget) writes status.json;
    a fresh process over the same run dir seeds its ceiling from it."""
    from ralphd.engine.config import JobConfig
    from ralphd.engine.main import _seed_rate_limit_max_wait

    run = _seed_run(tmp_path, {"state": "running"})
    sup = LoopSupervisor(JobConfig(run_id="rlw-seed"), run, tmp_path)
    sup.set_rate_limit_max_wait(100000)
    fresh = JobConfig(run_id="rlw-seed")
    assert fresh.rate_limit_max_wait_s == 86400
    _seed_rate_limit_max_wait(fresh, run)
    assert fresh.rate_limit_max_wait_s == 100000
