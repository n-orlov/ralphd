"""#291 (PRD B.3): `run.paused` / `run.unpaused` on the engine's event log.

Four emissions, each proven through production code: an operator
`POST /pause` taking effect, an engine `_persist_pause`, and the two
`run.unpaused` that leave those paused states (`POST /resume` for the
operator pause, `resume()` for the engine pause).
"""

from __future__ import annotations

import json

import httpx
import pytest

from ralphd.engine.api import create_app
from ralphd.engine.state import RunDir
from tests.conftest import make_supervisor


def _supervisor(tmp_path):
    return make_supervisor(tmp_path, run_id="unit", cfg_defaults=False,
                           status={"state": "running"})


def _events(run: RunDir, type_: str) -> list[dict]:
    log = run.root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


def _client(sup) -> httpx.AsyncClient:
    app = create_app(sup.cfg, sup.run, sup)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://engine")


@pytest.mark.asyncio
async def test_operator_pause_emits_run_paused_and_resume_emits_run_unpaused(tmp_path):
    sup = _supervisor(tmp_path)
    async with _client(sup) as client:
        assert (await client.post("/pause")).status_code == 200
        paused = _events(sup.run, "run.paused")
        assert [e["reason"] for e in paused] == ["operator"]
        assert _events(sup.run, "run.unpaused") == []

        # A second POST /pause on a run that is already held takes no new effect.
        assert (await client.post("/pause")).status_code == 200
        assert len(_events(sup.run, "run.paused")) == 1

        assert (await client.post("/resume")).status_code == 200
    unpaused = _events(sup.run, "run.unpaused")
    assert len(unpaused) == 1
    assert isinstance(unpaused[0]["pausedS"], (int, float))
    assert unpaused[0]["pausedS"] >= 0


def test_resume_of_a_run_that_was_never_paused_emits_nothing(tmp_path):
    sup = _supervisor(tmp_path)
    sup.resume()
    assert _events(sup.run, "run.unpaused") == []
    assert _events(sup.run, "run.paused") == []


def test_a_persisted_operator_pause_emits_run_paused_operator_and_unpause_follows(tmp_path):
    sup = _supervisor(tmp_path)
    sup.pause("lunch")
    paused = _events(sup.run, "run.paused")
    assert [e["reason"] for e in paused] == ["operator"]
    status = sup.run.read_status()
    assert status["state"] == "paused"
    assert status["pausedBy"] == "operator"
    assert status["pauseNote"] == "lunch"

    sup.resume()
    unpaused = _events(sup.run, "run.unpaused")
    assert len(unpaused) == 1
    assert isinstance(unpaused[0]["pausedS"], (int, float))
    cleared = sup.run.read_status()
    assert cleared["state"] == "running"
    assert cleared["pausedBy"] is None and cleared["pauseNote"] is None


def test_a_second_pause_while_paused_does_not_emit_a_second_run_paused(tmp_path):
    sup = _supervisor(tmp_path)
    sup.pause()
    sup.pause("now with a note")
    assert [e["reason"] for e in _events(sup.run, "run.paused")] == ["operator"]
    assert sup.run.read_status()["pauseNote"] == "now with a note"
    sup.resume()
    assert len(_events(sup.run, "run.unpaused")) == 1


def test_pausedS_measures_the_held_stretch(tmp_path, monkeypatch):
    sup = _supervisor(tmp_path)
    clock = {"t": 1000.0}
    monkeypatch.setattr("ralphd.engine.loop.time.monotonic", lambda: clock["t"])
    sup.pause()
    clock["t"] += 42.5
    sup.resume()
    assert _events(sup.run, "run.unpaused")[0]["pausedS"] == pytest.approx(42.5)
