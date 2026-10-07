"""#300 (PRD v0.32.3 requirement B.3/B.5, the peer verdict gate half): a gate
that has no verdict after every retry does not reject. The run is parked
`operator_input_needed` (`pauseDetail.reason == "judge_no_verdict"`), nothing is
archived, review's recorded verdict is untouched, and `ralphctl status` says
`gate: no verdict (output truncated)`. An `unpause` re-dispatches the gate. With
no budget or wall clock left at the park the run ends `unreviewed`, never
`rejected`.

Black-box through the scripted stand-in for `pi` (`tests/conftest.py`'s
`ScriptedRunner`), in the style of `tests/test_judge_retry.py`. The park is the
real one: the run sits in `_gate()` until the test calls `resume()`.
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


async def _drive(sup, *, unpause: bool) -> tuple[str, dict | None, Path | None]:
    """Run the job; when it parks, snapshot status.json, then either unpause
    (as `ralphctl unpause` would) or abort. Returns the final state, the
    parked status (None if it never parked) and the snapshot path."""
    task = asyncio.ensure_future(sup._run_job_core())
    parked = None
    snap = None
    for _ in range(1500):
        if task.done():
            break
        if sup.run.read_status().get("state") == "paused":
            snap = sup.run.root.parent / "parked-status.json"
            snap.write_text((sup.run.root / "status.json").read_text())
            parked = json.loads(snap.read_text())
            break
        await asyncio.sleep(0.01)
    if parked is not None:
        # Still parked a moment later: nothing proceeds on its own.
        await asyncio.sleep(0.05)
        assert not task.done(), "a no-verdict gate must wait for the operator"
        if not unpause:
            sup.abort("test done")
        sup.resume()
    final = await asyncio.wait_for(task, 30.0)
    return final, parked, snap


def _run_parked(root: Path, gate_steps: list, *, unpause: bool, **cfg):
    sup = _build(root, gate_steps, **cfg)
    final, parked, snap = asyncio.run(_drive(sup, unpause=unpause))
    return sup, final, parked, snap


def _events(sup, *types) -> list[dict]:
    return state.read_events(sup.run.root, types=list(types))


def _recorded_verdicts(sup) -> list[tuple[str, str]]:
    return [(e["channel"], e["verdict"])
            for e in _events(sup, "approach.verdict_recorded")]


# ---------------------------------------------------------------------------
# (1) the park
# ---------------------------------------------------------------------------


def test_a_gate_that_truncates_every_attempt_parks_the_run(tmp_path):
    sup, final, parked, _snap = _run_parked(
        tmp_path / "run", [_gate_truncated], unpause=False)
    assert parked is not None, "the run never parked"
    assert parked["state"] == "paused"
    assert parked["pauseReason"] == "operator_input_needed"
    detail = parked["pauseDetail"]
    assert detail["reason"] == "judge_no_verdict"
    assert detail["hat"] == "verdict_gate"
    assert NO_VERDICT_WORDING in detail["message"]
    assert "verdict_gate" in detail["message"]
    # the retries ran first (default cap 2), all uncharged
    assert len(_events(sup, "verdict_gate.retry")) == 2
    assert sup.runner.phases_seen.count("verdict_gate") == 3
    # not a rejection: nothing archived, no composite PRD, no rejected record
    approaches = sup.run.root / "approaches"
    assert not approaches.exists() or not any(approaches.iterdir())
    assert not (approaches / "01").exists()
    assert not list(sup.run.root.glob("**/composite*"))
    verdicts = _recorded_verdicts(sup)
    assert not any(v == "rejected" for _c, v in verdicts), verdicts
    # review's recorded verdict is unchanged, and the gate recorded none
    assert verdicts == [("review", "verified")], verdicts
    assert _events(sup, "verdict_gate.no_verdict")[0]["parked"] is True
    # the park is not the run's ending
    assert final == "aborted"


def test_the_park_sets_the_fourth_gate_ending(tmp_path):
    assert state.GATE_REJECTION_ENDING_NO_VERDICT in state.GATE_REJECTION_ENDINGS
    assert len(state.GATE_REJECTION_ENDINGS) == 4
    _sup, _final, parked, _snap = _run_parked(
        tmp_path / "run", [_gate_truncated], unpause=False)
    assert parked["gateRejectionEnding"] == state.GATE_REJECTION_ENDING_NO_VERDICT


# ---------------------------------------------------------------------------
# (2) ralphctl status
# ---------------------------------------------------------------------------


class _StatusArgs:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.json = False


def test_ralphctl_status_says_no_verdict_not_rejected(tmp_path, monkeypatch,
                                                      capsys):
    _sup, _final, parked, snap = _run_parked(
        tmp_path / "run", [_gate_truncated], unpause=False)
    registry = tmp_path / "registry"
    monkeypatch.setenv("RALPHD_REGISTRY", str(registry))
    monkeypatch.setattr(cli_main, "_dangling_run_entry", lambda run_id: None)
    root = registry / "runs" / "judge-park"
    root.mkdir(parents=True)
    (root / "status.json").write_text(snap.read_text())
    cli_main.cmd_status(_StatusArgs("judge-park"))
    out = capsys.readouterr().out
    assert "gate:      no verdict (output truncated)" in out, out
    assert "rejected on the merits" not in out, out
    paused = [ln for ln in out.splitlines() if ln.startswith("paused:")]
    assert len(paused) == 1 and NO_VERDICT_WORDING in paused[0], out


# ---------------------------------------------------------------------------
# (3) unpause re-dispatches; judge_retry_max 0 parks with no retry
# ---------------------------------------------------------------------------


def test_unpause_redispatches_the_gate_and_a_verified_reply_ends_the_run(
        tmp_path):
    # first attempt + two retries truncated, then the post-unpause dispatch.
    sup, final, parked, _snap = _run_parked(
        tmp_path / "run",
        [_gate_truncated, _gate_truncated, _gate_truncated, _gate_approves],
        unpause=True)
    assert parked is not None
    status = sup.run.read_status()
    assert final == "succeeded", status
    assert status["verdict"] == "verified", status
    assert sup.runner.phases_seen.count("verdict_gate") == 4
    assert len(_events(sup, "verdict_gate.retry")) == 2
    # the ending is cleared once the gate is asked again
    assert status.get("gateRejectionEnding") is None
    assert status["state"] == "succeeded"
    assert _recorded_verdicts(sup) == [("review", "verified"),
                                       ("verdictGate", "verified")]


def test_judge_retry_max_zero_parks_without_a_retry(tmp_path):
    sup, final, parked, _snap = _run_parked(
        tmp_path / "run", [_gate_truncated, _gate_approves],
        unpause=True, judge_retry_max=0)
    assert parked is not None
    assert parked["pauseDetail"]["reason"] == "judge_no_verdict"
    assert _events(sup, "verdict_gate.retry") == []
    assert final == "succeeded"
    assert sup.runner.phases_seen.count("verdict_gate") == 2


# ---------------------------------------------------------------------------
# (4) no budget or wall clock left at the park
# ---------------------------------------------------------------------------


def _assert_unreviewed_not_rejected(sup, final, parked):
    status = sup.run.read_status()
    assert parked is None, "the run parked with nothing left to resume into"
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
    sup, final, parked, _snap = _run_parked(
        tmp_path / "run", [_gate_truncated], unpause=True, iterations=4)
    _assert_unreviewed_not_rejected(sup, final, parked)


def test_a_spent_deadline_ends_unreviewed_not_rejected(tmp_path):
    sup, final, parked, _snap = _run_parked(
        tmp_path / "run", [_gate_truncated_past_the_deadline], unpause=True)
    _assert_unreviewed_not_rejected(sup, final, parked)
