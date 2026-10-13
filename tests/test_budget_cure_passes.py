"""Task 010 (v0.27 requirement C.3): `ralphctl budget <run> --cure-passes
+N|N` -- the run-wide cure-pass ceiling (`JobConfig.cure_pass_ceiling`)
joins the other four `PATCH /config/budget` knobs, live via the SAME
`curePassCeiling`-style key, engine-applied through
`LoopSupervisor.set_cure_pass_ceiling`, and -- unlike `maxApproaches`/
`iterationTimeoutS`/`jobDeadlineEpochS` (task 004/#94 (d), process-only) --
PERSISTS across a resume the same way `iterations` does
(`main._seed_cure_pass_ceiling`, mirroring `main._seed_iteration_budget`).

Four things asserted below, each its own section:

1. the grant lands in status.json and a fresh engine seeds its ceiling
   from it (the resume-persistence contract, engine-level, no HTTP);
2. the full operator story end to end: a gate rejection parks on a spent
   ceiling (`verdict_gate_operator_input_needed`, task 008's own C.1
   edge), a live `PATCH /config/budget {"curePassCeiling": "+1"}` raises
   it, `resume()` unpauses, and the SAME rejection now takes the cure-pass
   edge instead of parking again -- driven over the real HTTP route
   (`create_app`) and a real `LoopSupervisor`/`RunDir`, exactly the
   fixture shape tests/test_gate_pause_before_archive.py already uses;
3. `ralphctl budget <run> --cure-passes +N|N` round-trips the same request
   shape the other budget flags use, and is refused on a run with no live
   API with the identical "unreachable" wording `test_cli_budget.py`'s own
   `test_budget_unreachable_api_exits_4` pins for every other flag (same
   `api()` helper, same exit code 4 -- proven here so this flag is never
   the one exception);
4. docs/cli.md, docs/api.md and SPEC.md each name the new flag/field.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from ralphd.engine import findings
from ralphd.engine.api import create_app
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from test_cli_budget import _seed as _cli_seed
from test_cli_budget import engine
from test_cli_docker import Ctl, ctl, unix_sock  # noqa: F401 (fixtures)

__all__ = ["ctl", "unix_sock", "engine"]


def _bounded(coro, timeout: float = 30.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _client(sup: LoopSupervisor) -> httpx.AsyncClient:
    app = create_app(sup.cfg, sup.run, sup)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://engine")


# -- 1. resume-persistence contract, engine-level --------------------------

def test_cure_pass_ceiling_grant_lands_in_status_json(tmp_path):
    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running", approach=2)
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": []})
    cfg = JobConfig(run_id="task010-cure-passes", iterations=5,
                    cure_pass_ceiling=3)
    sup = LoopSupervisor(cfg, run, root)

    sup.set_cure_pass_ceiling(4)

    assert sup.cfg.cure_pass_ceiling == 3, "the base is not rewritten"
    assert run.read_status().get("cureRoundTopUps") == {"2": 1}
    assert sup.cure_pass_allowance == 4


def test_cure_pass_ceiling_grant_survives_a_resumed_engine(tmp_path):
    """The top-up is disk-backed: a fresh supervisor over the same run
    directory (job.yaml's own default base, `3`) reads the same allowance
    with no startup seeding."""
    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running", approach=1, cureRoundTopUps={"1": 2})
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": []})

    fresh = LoopSupervisor(JobConfig(run_id="task010-cure-passes"), run, root)
    assert fresh.cfg.cure_pass_ceiling == 3  # engine/config.py's own default

    assert fresh.cure_pass_allowance == 5, "the live grant must survive"


# -- 2. park (ceiling spent) -> budget --cure-passes +1 -> unpause -> cure --

def _all_curable_gate_findings() -> list[dict]:
    return [
        {"severity": "blocking", "curable": True,
         "clause": "requirement C.3 (v0.27)",
         "evidence": "the peer gate found an unmet requirement",
         "cured_when": "a cure task adds the missing behaviour"},
    ]


def _gate_rejects(reason: str):
    async def handler(self, phase, extra="", *a, **kw):
        r = IterationResult(exit_code=0)
        r.final_text = json.dumps({"verdict": "rejected", "reason": reason})
        return r
    return handler


def _fixture(root: Path, *, cure_pass_ceiling: int) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": []})
    cfg = JobConfig(run_id="task010-cure-passes-flow", iterations=5,
                    max_approaches=2, vigilant=False,
                    peer_verdict_gate=True, cure_pass_ceiling=cure_pass_ceiling,
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0)
    sup = LoopSupervisor(cfg, run, root)
    sup._approach = 1
    findings.write_findings_json(run.verdict_gate_findings_json_file,
                                 _all_curable_gate_findings())
    return sup


def test_park_then_budget_cure_passes_then_unpause_takes_the_cure_pass(
        tmp_path):
    root = tmp_path / "run"
    sup = _fixture(root, cure_pass_ceiling=0)

    orig = LoopSupervisor.run_iteration
    LoopSupervisor.run_iteration = _gate_rejects(
        "the one blocking finding is curable")
    try:
        outcome = _bounded(sup._step_verdict_gate())
    finally:
        LoopSupervisor.run_iteration = orig

    # parked -- ceiling was spent (0).
    assert outcome == ("advance", "paused", "verdict_gate_operator_input_needed")
    # v0.36: the park lands in `awaiting_input` (covering node: tests/test_awaiting_input_question.py)
    assert sup.run.read_status().get("state") == "awaiting_input"
    assert sup.run.read_status().get("gateCurePasses") is None

    # `ralphctl budget <run> --cure-passes +1` -> PATCH /config/budget
    # {"curePassCeiling": "+1"} against the LIVE app, same route every
    # other budget knob uses.
    async def _patch():
        async with _client(sup) as client:
            return await client.patch("/config/budget",
                                      json={"curePassCeiling": "+1"})
    resp = _bounded(_patch())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"curePassCeiling": 1, "previousCurePassCeiling": 0}
    assert sup.cfg.cure_pass_ceiling == 0, "the base is not rewritten"
    assert sup.run.read_status().get("cureRoundTopUps") == {"1": 1}

    # `ralphctl unpause <run>` -> POST /resume.
    sup.resume()
    assert sup.run.read_status().get("state") == "running"

    # the SAME rejection, re-asked: with the ceiling raised the gate now
    # takes the cure-pass edge instead of parking a second time.
    orig = LoopSupervisor.run_iteration
    LoopSupervisor.run_iteration = _gate_rejects(
        "the one blocking finding is curable")
    try:
        outcome2 = _bounded(sup._step_verdict_gate())
    finally:
        LoopSupervisor.run_iteration = orig

    assert outcome2 == ("advance", "cure",
                        "verdict_gate_rejected_curable_pass_available")
    status = sup.run.read_status()
    assert status.get("gateRejectionEnding") == "rejected_cure_taken"
    assert status.get("gateCurePasses") == {"1": 1}
    assert status.get("state") != "paused"


def test_cure_pass_ceiling_below_cure_passes_used_is_refused_with_409(
        tmp_path):
    root = tmp_path / "run"
    sup = _fixture(root, cure_pass_ceiling=3)
    sup.run.update_status(gateCurePasses={"1": 2})

    async def _patch():
        async with _client(sup) as client:
            return await client.patch("/config/budget",
                                      json={"curePassCeiling": 1})
    resp = _bounded(_patch())
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["title"] == "cure pass ceiling below cure passes used"
    assert "2" in detail["detail"]
    assert sup.cfg.cure_pass_ceiling == 3, "a rejected request changes nothing"


# -- 3. CLI contract, exactly test_cli_budget.py's own shape ---------------

def test_cli_budget_cure_passes_round_trips_curepassceiling(ctl: Ctl, engine):
    eng = engine(status=200, body={"curePassCeiling": 4,
                                   "previousCurePassCeiling": 3})
    _cli_seed(ctl, "tst-budget-cure-passes", eng)
    res = ctl.run("budget", "tst-budget-cure-passes", "--cure-passes", "+1")
    assert res.returncode == 0, res.stderr
    assert eng.requests[-1]["body"] == {"curePassCeiling": "+1"}
    assert "cure pass ceiling" in res.stdout
    assert "3" in res.stdout and "4" in res.stdout


def test_cli_budget_cure_passes_unreachable_api_exits_4_same_wording(
        ctl: Ctl):
    _cli_seed(ctl, "tst-budget-cure-passes-dead")  # apiUrl points at a closed port
    res = ctl.run("budget", "tst-budget-cure-passes-dead", "--cure-passes", "+1")
    assert res.returncode == 4, (res.returncode, res.stderr)
    assert "unreachable" in res.stderr


def test_cli_budget_cure_passes_malformed_spec_exits_2(ctl: Ctl, engine):
    eng = engine()
    _cli_seed(ctl, "tst-budget-cure-passes-bad", eng)
    res = ctl.run("budget", "tst-budget-cure-passes-bad", "--cure-passes", "abc")
    assert res.returncode == 2, res.stderr
    assert "invalid value" in res.stderr
    assert eng.requests == [], "malformed spec must not reach the engine"


# -- 4. spec in step ---------------------------------------------------------

def test_cure_passes_flag_documented_in_cli_docs():
    doc = (Path(__file__).resolve().parents[1] / "docs" / "cli.md").read_text()
    heading = "### `ralphctl budget <run-id> <+N|N>`"
    assert heading in doc
    section = doc.split(heading, 1)[1].split("\n### ", 1)[0]
    assert "--cure-passes" in section
    assert "curePassCeiling" in section


def test_cure_pass_ceiling_field_documented_in_api_docs():
    doc = (Path(__file__).resolve().parents[1] / "docs" / "api.md").read_text()
    assert "curePassCeiling" in doc


def test_cure_pass_ceiling_behaviour_documented_in_spec():
    doc = (Path(__file__).resolve().parents[1] / "SPEC.md").read_text()
    assert "curePassCeiling" in doc
