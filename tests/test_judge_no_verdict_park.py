"""#300 / v0.36 F.3: a peer verdict gate that has no verdict after every retry
does not reject, and (v0.36) does not park the run either. It is an infra fault
under the unchanged outage budget: nothing is archived, review's recorded
verdict is untouched, `gateRejectionEnding` is the fourth value (`ralphctl
status` says `gate: no verdict (output truncated)`), the engine waits out the
backoff and asks the gate again. When the outage budget is spent, or no budget
or wall clock is left, the run ends `failed` with verdict `unreviewed`, never
`rejected` -- resumable with `ralphctl resume`. (The file keeps its 0.35 name;
tests/test_stop_routing_faults.py covers the same routing across every judge
fault.)

Black-box through the scripted stand-in for `pi` (`tests/conftest.py`'s
`ScriptedRunner`), in the style of `tests/test_judge_retry.py`.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from ralphd.cli import main as cli_main
from ralphd.engine import state
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import scripted_supervisor

TASK_1 = "001"
NO_VERDICT_WORDING = "no verdict: output truncated"


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _plan(sup, prompt=""):
    atomic_write_json(sup.run.root / "plan-proposal.json", {
        "version": 1,
        "tasks": [{"id": TASK_1, "title": "t", "successCriteria": "c"}],
    })
    return _ok(json.dumps(
        {"proposal_path": "plan-proposal.json", "summary": "planned"}))


def _worker(sup, prompt=""):
    return _ok(json.dumps({"verdict": "complete", "task_id": TASK_1}))


def _review(sup, prompt=""):
    return _ok(json.dumps({"verdict": "verified"}))


def _gate_approves(sup, prompt=""):
    return _ok(json.dumps({"verdict": "verified"}))


def _gate_truncated(sup, prompt=""):
    r = IterationResult(exit_code=0)
    r.final_text = ""
    r.output_truncated = True
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 4096, "totalTokens": 4106}
    return r


def _gate_truncated_past_the_deadline(sup, prompt=""):
    """A truncated reply that lands after the run's wall clock is spent."""
    sup.deadline = time.monotonic() - 1.0
    return _gate_truncated(sup, prompt)


_supervisor = scripted_supervisor(run_id="judge-park", iterations=20,
                                  max_approaches=1, vigilant=False,
                                  on_complete="exit",
                                  infra_outage_budget_s=1000.0)


def _build(root: Path, gate_steps: list, **cfg):
    RunDir(root=root).update_status(state="starting")
    return _supervisor(
        root,
        {"planning": [_plan], "worker": [_worker], "review": [_review],
         "verdict_gate": gate_steps},
        peer_verdict_gate=True, **cfg)


async def _drive(sup) -> tuple[str, list[dict]]:
    """Run the job to its end. The engine never parks on a judge fault, so a
    `paused`/`awaiting_input` state seen while it runs is returned for the
    test to assert on (it must be empty)."""
    task = asyncio.ensure_future(sup._run_job_core())
    seen: list[dict] = []
    while not task.done():
        status = sup.run.read_status()
        if status.get("state") in ("paused", "awaiting_input"):
            seen.append(status)
        await asyncio.sleep(0.005)
    return await asyncio.wait_for(task, 30.0), seen


def _run(root: Path, gate_steps: list, **cfg):
    sup = _build(root, gate_steps, **cfg)
    final, parks = asyncio.run(_drive(sup))
    return sup, final, parks


def _events(sup, *types) -> list[dict]:
    return state.read_events(sup.run.root, types=list(types))


def _recorded_verdicts(sup) -> list[tuple[str, str]]:
    return [(e["channel"], e["verdict"])
            for e in _events(sup, "approach.verdict_recorded")]


# ---------------------------------------------------------------------------
# (1) the outage budget is spent: failed / unreviewed, no park
# ---------------------------------------------------------------------------


def test_a_gate_that_truncates_every_attempt_ends_the_run_failed_unreviewed(
        tmp_path):
    sup, final, parks = _run(tmp_path / "run", [_gate_truncated],
                             infra_outage_budget_s=0.0)
    assert parks == [], "v0.36 F.3: the engine never parks on a judge fault"
    status = sup.run.read_status()
    assert final == "failed" and status["state"] == "failed", status
    assert status["verdict"] == "unreviewed" and status["verdict"] != "rejected"
    assert status["gateRejectionEnding"] == state.GATE_REJECTION_ENDING_NO_VERDICT
    assert NO_VERDICT_WORDING in (status.get("reason") or ""), status
    # nothing archived, no rejection recorded; review's verdict is unchanged
    approaches = sup.run.root / "approaches"
    assert not approaches.exists() or not any(approaches.iterdir())
    assert _recorded_verdicts(sup) == [("review", "verified")]
    (ev,) = _events(sup, "verdict_gate.no_verdict")
    assert ev["parked"] is False and ev["outcome"] == "spent"


def test_the_fault_sets_the_fourth_gate_ending(tmp_path):
    assert state.GATE_REJECTION_ENDING_NO_VERDICT in state.GATE_REJECTION_ENDINGS
    assert len(state.GATE_REJECTION_ENDINGS) == 4
    sup, _final, _parks = _run(tmp_path / "run", [_gate_truncated],
                               infra_outage_budget_s=0.0)
    assert (sup.run.read_status()["gateRejectionEnding"]
            == state.GATE_REJECTION_ENDING_NO_VERDICT)


# ---------------------------------------------------------------------------
# (2) ralphctl status
# ---------------------------------------------------------------------------


class _StatusArgs:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.json = False


def test_ralphctl_status_says_no_verdict_not_rejected(tmp_path, monkeypatch,
                                                      capsys):
    sup, _final, _parks = _run(tmp_path / "run", [_gate_truncated],
                               infra_outage_budget_s=0.0)
    registry = tmp_path / "registry"
    monkeypatch.setenv("RALPHD_REGISTRY", str(registry))
    monkeypatch.setattr(cli_main, "_dangling_run_entry", lambda run_id: None)
    root = registry / "runs" / "judge-park"
    root.mkdir(parents=True)
    (root / "status.json").write_text(
        (sup.run.root / "status.json").read_text())
    cli_main.cmd_status(_StatusArgs("judge-park"))
    out = capsys.readouterr().out
    assert "gate:      no verdict (output truncated)" in out, out
    assert "rejected on the merits" not in out, out


# ---------------------------------------------------------------------------
# (3) inside the outage budget the gate is asked again; judge_retry_max 0
# ---------------------------------------------------------------------------


def test_a_fault_inside_the_budget_asks_the_gate_again_and_a_verified_reply_ends_the_run(
        tmp_path):
    # first attempt + two retries truncated, then the post-backoff dispatch.
    sup, final, parks = _run(
        tmp_path / "run",
        [_gate_truncated, _gate_truncated, _gate_truncated, _gate_approves])
    assert parks == []
    status = sup.run.read_status()
    assert final == "succeeded", status
    assert status["verdict"] == "verified", status
    assert sup.runner.phases_seen.count("verdict_gate") == 4
    assert len(_events(sup, "verdict_gate.retry")) == 2
    # the ending is cleared once the gate is asked again
    assert status.get("gateRejectionEnding") is None
    assert _recorded_verdicts(sup) == [("review", "verified"),
                                       ("verdictGate", "verified")]
    (ev,) = _events(sup, "verdict_gate.no_verdict")
    assert ev["outcome"] == "retry"


def test_judge_retry_max_zero_waits_without_a_retry(tmp_path):
    sup, final, parks = _run(tmp_path / "run",
                             [_gate_truncated, _gate_approves],
                             judge_retry_max=0)
    assert parks == []
    assert _events(sup, "verdict_gate.retry") == []
    assert final == "succeeded"
    assert sup.runner.phases_seen.count("verdict_gate") == 2


# ---------------------------------------------------------------------------
# (4) no budget or wall clock left
# ---------------------------------------------------------------------------


def _assert_unreviewed_not_rejected(sup, final, parks):
    status = sup.run.read_status()
    assert parks == [], "the run waited with nothing left to ask again"
    assert final == "failed", status
    assert status["verdict"] == "unreviewed", status
    assert status["verdict"] != "rejected"
    assert status["gateRejectionEnding"] == state.GATE_REJECTION_ENDING_NO_VERDICT
    assert "no verdict" in (status.get("reason") or "")
    verdicts = _recorded_verdicts(sup)
    assert verdicts == [("review", "verified")], verdicts
    approaches = sup.run.root / "approaches"
    assert not approaches.exists() or not any(approaches.iterdir())
    assert _events(sup, "verdict_gate.no_verdict")[0]["parked"] is False


def test_a_spent_iteration_budget_ends_unreviewed_not_rejected(tmp_path):
    # plan, worker, review and the gate's last (charged) attempt are 4.
    sup, final, parks = _run(tmp_path / "run", [_gate_truncated], iterations=4)
    _assert_unreviewed_not_rejected(sup, final, parks)


def test_a_spent_deadline_ends_unreviewed_not_rejected(tmp_path):
    sup, final, parks = _run(tmp_path / "run",
                             [_gate_truncated_past_the_deadline])
    _assert_unreviewed_not_rejected(sup, final, parks)
