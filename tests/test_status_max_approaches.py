"""Task 006 (#16): `maxApproaches` is part of the status contract.

`status.json` already grew an `approach` numerator; without its denominator
every surface that wants to say "approach 2 of 3" has to make a second call to
`GET /config` (which the hub's on-disk snapshot path cannot make at all). So
the engine writes `maxApproaches` with the *first* status write -- not only on
the move to `running` -- and `GET /status` reports it as an explicit `null` for
a run dir written by a pre-v0.6 engine, so "unknown denominator" is a value a
consumer can render (a bare `2`) rather than a missing key it has to guess
about.

Three layers: the endpoint contract over real ASGI, the startup write proven
to land before the loop runs at all, and a real engine writing the number from
`starting` through the terminal snapshot (and serving it live).
"""

from __future__ import annotations

import asyncio
import json
import socket
import time

import httpx
import pytest
import uvicorn

from ralphd.engine.api import create_app
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def client(tmp_path):
    """ASGI client factory over the engine app on a caller-written run dir."""
    run = RunDir(root=tmp_path)
    sup = LoopSupervisor(JobConfig(run_id="unit", max_approaches=3), run,
                         tmp_path)
    app = create_app(sup.cfg, run, sup)

    def open_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://engine")

    return open_client


def _status(client) -> dict:
    async def go():
        async with client() as c:
            r = await c.get("/status")
            assert r.status_code == 200, r.text
            return r.json()
    return asyncio.run(go())


# -- the endpoint contract --------------------------------------------------

def test_status_serves_the_denominator_written_on_disk(client, tmp_path):
    RunDir(root=tmp_path).update_status(state="running", approach=2,
                                        maxApproaches=3)
    s = _status(client)
    assert (s["approach"], s["maxApproaches"]) == (2, 3)


def test_pre_v06_status_json_yields_an_explicit_null_denominator(client, tmp_path):
    """A run dir written before this field existed: read without crashing,
    numerator intact, denominator explicitly unknown -- never a missing key,
    and never the live config's value quietly guessed in (the run may have
    been started with a different budget)."""
    (tmp_path / "status.json").write_text(json.dumps({
        "runId": "old", "state": "running", "approach": 2,
        "iterationsBudget": 25, "schemaVersion": 1,
    }))
    s = _status(client)
    assert s["approach"] == 2
    assert "maxApproaches" in s, "absence must never be a third case"
    assert s["maxApproaches"] is None, "no denominator is known for this run"


def test_no_approach_at_all_is_still_readable(client, tmp_path):
    """`starting` on a pre-v0.6 run dir: nothing to render, nothing crashes."""
    (tmp_path / "status.json").write_text(json.dumps(
        {"runId": "old", "state": "starting"}))
    s = _status(client)
    assert s.get("approach") is None
    assert s["maxApproaches"] is None


# -- the startup write lands before the loop ever runs ---------------------

@pytest.mark.asyncio
async def test_the_first_status_write_already_carries_the_denominator(
        tmp_path, monkeypatch):
    """The denominator must not wait for the move to `running`: a job that
    dies during startup, or is inspected inside the `starting` window, still
    has to carry its budget. Snapshot status.json from inside a stubbed
    `run_job` -- i.e. the moment before the loop's own status write."""
    from ralphd.engine import config as config_mod
    from ralphd.engine import main as main_mod

    cfg_dir, run_dir, ws = tmp_path / "cfg", tmp_path / "run", tmp_path / "ws"
    for d in (cfg_dir, run_dir, ws):
        d.mkdir()
    (cfg_dir / "prd.md").write_text("# PRD\n")
    (cfg_dir / "job.yaml").write_text(
        "run_id: startup-write\nmax_approaches: 5\non_complete: exit\n")
    monkeypatch.setattr(config_mod, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(main_mod, "CONFIG_DIR", cfg_dir)
    monkeypatch.setenv("RALPHD_RUN_DIR", str(run_dir))
    monkeypatch.setenv("RALPHD_WORKSPACE_DIR", str(ws))
    monkeypatch.setenv("RALPHD_PORT", str(_free_port()))

    seen: list[dict] = []

    async def fake_serve(self):          # no socket, no port bound
        return None

    async def fake_run_job(self):
        seen.append(json.loads((run_dir / "status.json").read_text()))
        return "aborted"

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    monkeypatch.setattr(LoopSupervisor, "run_job", fake_run_job)

    assert await main_mod.amain() == 1    # not succeeded -> 1, as ever
    assert len(seen) == 1
    assert seen[0]["state"] == "starting", "the loop had not started yet"
    assert seen[0]["maxApproaches"] == 5, seen[0]


# -- black box: a real engine, first write through terminal snapshot -------

def test_engine_writes_max_approaches_from_starting_to_terminal(live):
    run = live(run_id="max-approaches",
               job={"iterations": 6, "max_approaches": 2},
               stub_env={"STUB_TASKS": "1"})
    run.wait_api()

    # Every status.json snapshot an operator could ever poll carries the
    # denominator -- including any pre-`running` one this loop catches.
    seen_states: set[str] = set()
    deadline = time.time() + 30
    while time.time() < deadline:
        sf = run.run_dir / "status.json"
        if sf.exists():
            try:
                snap = json.loads(sf.read_text())
            except json.JSONDecodeError:
                snap = {}
            if snap.get("state"):
                seen_states.add(snap["state"])
                assert snap["maxApproaches"] == 2, snap
                if snap["state"] in ("succeeded", "failed", "aborted"):
                    break
        time.sleep(0.05)

    live_status = json.loads(run.ralphctl("--json", "status",
                                          run.run_id).stdout or "{}")
    assert live_status.get("maxApproaches") == 2, live_status

    final = run.wait_terminal(timeout=60)
    assert final["state"] == "succeeded"
    assert final["maxApproaches"] == 2, "survives to the terminal snapshot"
    assert final["approach"] == 1
    assert seen_states, "never observed a status.json snapshot"
