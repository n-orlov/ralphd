"""#300 (PRD v0.32.3 requirement B.1/B.2, the peer verdict gate half): a
judging hat whose reply was cut off by the provider, or came back empty after
the contract ladder, is re-dispatched as a fresh, uncharged iteration that
differs from the failed one (bounded-budget wording, one thinking step lower),
capped by `JobConfig.judge_retry_max`.

Black-box through the scripted stand-in for `pi` (`tests/conftest.py`'s
`ScriptedRunner`), in the style of `tests/test_peer_verdict_gate.py`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine import state, tiers
from ralphd.engine.config import HatBundle, JobConfig
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import scripted_supervisor

REPO_ROOT = Path(__file__).resolve().parents[1]
RETRY_PROMPT = REPO_ROOT / "src" / "ralphd" / "prompts" / "judge_retry.md"
# The two phrases the retry's wording must carry (and a first attempt must not).
BUDGET_WORDING = "ran out of output budget"
BOUNDED_WORDING = "bounded investigation"

TASK_1 = "001"


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


def _gate_rejects(sup, prompt=""):
    return _ok(json.dumps({"verdict": "rejected", "reason": "does not meet it"}))


def _gate_truncated(sup, prompt=""):
    """A reply cut off by the provider: `stopReason: length`, no text."""
    r = IterationResult(exit_code=0)
    r.final_text = ""
    r.output_truncated = True
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 4096, "totalTokens": 4106}
    return r


def _gate_empty(sup, prompt=""):
    return _ok("")


_supervisor = scripted_supervisor(run_id="judge-retry", iterations=20,
                                  max_approaches=1, vigilant=False,
                                  on_complete="exit",
                                  infra_outage_budget_s=1000.0)


def _run(root: Path, gate_steps: list, **cfg):
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(
        root,
        {"planning": [_plan], "worker": [_worker], "review": [_review],
         "verdict_gate": gate_steps},
        peer_verdict_gate=True, **cfg)
    final = asyncio.run(asyncio.wait_for(sup._run_job_core(), 30.0))
    return sup, final


def _retry_events(sup) -> list[dict]:
    return state.read_events(sup.run.root, types=["verdict_gate.retry"])


def _gate_prompts(sup) -> list[str]:
    agent = sup.runner
    return [p for ph, p in zip(agent.phases_seen, agent.prompts)
            if ph == "verdict_gate"]


def _gate_meta(sup) -> list[dict]:
    metas = []
    for d in sorted((sup.run.root / "iterations").iterdir()):
        mp = d / "meta.json"
        if mp.exists():
            m = json.loads(mp.read_text())
            if m.get("phase") == "verdict_gate":
                metas.append(m)
    return metas


# ---------------------------------------------------------------------------
# (1) the knob
# ---------------------------------------------------------------------------


def test_judge_retry_max_defaults_to_two():
    assert JobConfig().judge_retry_max == 2


def test_judge_retry_max_is_settable_from_job_yaml(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text("judge_retry_max: 5\n")
    assert JobConfig.load(path).judge_retry_max == 5
    path.write_text("judge_retry_max: 0\n")
    assert JobConfig.load(path).judge_retry_max == 0


@pytest.mark.parametrize("bad", [-1, -5, "2", 1.5, True, None])
def test_judge_retry_max_refuses_an_invalid_value(bad):
    with pytest.raises(ValueError, match="judge_retry_max"):
        JobConfig(judge_retry_max=bad)


def test_judge_retry_max_negative_in_job_yaml_is_refused(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text("judge_retry_max: -1\n")
    with pytest.raises(ValueError, match="judge_retry_max"):
        JobConfig.load(path)


def test_judge_retry_max_env_override_is_validated(tmp_path, monkeypatch):
    path = tmp_path / "job.yaml"
    path.write_text("run_id: x\n")
    monkeypatch.setenv("RALPHD_JUDGE_RETRY_MAX", "4")
    assert JobConfig.load(path).judge_retry_max == 4
    monkeypatch.setenv("RALPHD_JUDGE_RETRY_MAX", "-3")
    with pytest.raises(ValueError, match="judge_retry_max"):
        JobConfig.load(path)


# ---------------------------------------------------------------------------
# (2) + (3) the retry, black-box
# ---------------------------------------------------------------------------


def test_a_truncated_gate_reply_is_retried_and_the_run_ends_verified(tmp_path):
    sup, final = _run(tmp_path / "run", [_gate_truncated, _gate_approves])
    status = sup.run.read_status()
    assert final == "succeeded", status
    assert status["verdict"] == "verified", status
    assert sup._approach == 1
    assert sup.runner.phases_seen.count("verdict_gate") == 2
    events = _retry_events(sup)
    assert len(events) == 1, events
    ev = events[0]
    assert ev["hat"] == "verdict_gate"
    assert ev["attempt"] == 1
    assert ev["cap"] == 2
    assert ev["reason"] == "output_truncated"
    # No approach was archived and the gate's rejection path never ran.
    assert not (sup.run.root / "approaches").exists() or not any(
        (sup.run.root / "approaches").iterdir())


def test_an_empty_reply_that_exhausts_the_ladder_is_retried(tmp_path):
    # first attempt + two ladder re-asks all empty, then the retry approves.
    sup, final = _run(
        tmp_path / "run",
        [_gate_empty, _gate_empty, _gate_empty, _gate_approves])
    status = sup.run.read_status()
    assert final == "succeeded", status
    assert status["verdict"] == "verified", status
    events = _retry_events(sup)
    assert len(events) == 1, events
    assert events[0]["reason"] == "empty_reply"
    assert events[0]["attempt"] == 1 and events[0]["cap"] == 2
    assert events[0]["hat"] == "verdict_gate"


def test_the_retry_iteration_is_not_charged_against_the_budget(tmp_path):
    base, base_final = _run(tmp_path / "base", [_gate_approves])
    trunc, trunc_final = _run(tmp_path / "trunc",
                              [_gate_truncated, _gate_approves])
    assert base_final == trunc_final == "succeeded"
    assert base.run.read_status()["iterationsUsed"] == 4
    assert (trunc.run.read_status()["iterationsUsed"]
            == base.run.read_status()["iterationsUsed"])
    assert trunc.iterations_used_charged == base.iterations_used_charged


def _run_unparking(root: Path, gate_steps: list, **cfg):
    """`_run`, with the no-verdict park (#300) released at once, as an
    immediate `ralphctl unpause` would: returns the supervisor, the final
    state and how many times the run parked."""
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(
        root,
        {"planning": [_plan], "worker": [_worker], "review": [_review],
         "verdict_gate": gate_steps},
        peer_verdict_gate=True, **cfg)
    parks = []

    async def park():
        parks.append(sup.run.read_status().get("pauseDetail"))
        sup.resume()
        return 0.0

    sup._park_until_resumed = park                       # type: ignore[method-assign]
    final = asyncio.run(asyncio.wait_for(sup._run_job_core(), 30.0))
    return sup, final, parks


def test_the_retry_is_capped_by_judge_retry_max(tmp_path):
    # Covering node for the park this wave adds: the cap used to end the run
    # on a rejection; it now ends the retries and parks, and an unpause asks
    # the gate again.
    sup, final, parks = _run_unparking(
        tmp_path / "run", [_gate_truncated, _gate_truncated, _gate_approves],
        judge_retry_max=1)
    assert sup.runner.phases_seen.count("verdict_gate") == 3
    assert len(_retry_events(sup)) == 1
    assert len(parks) == 1 and parks[0]["reason"] == "judge_no_verdict"
    assert final == "succeeded"


def test_judge_retry_max_zero_never_retries(tmp_path):
    sup, final, parks = _run_unparking(
        tmp_path / "run", [_gate_truncated, _gate_approves],
        judge_retry_max=0)
    # one attempt, a park with no retry, then the unpause's fresh dispatch
    assert sup.runner.phases_seen.count("verdict_gate") == 2
    assert _retry_events(sup) == []
    assert len(parks) == 1
    assert final == "succeeded"


def test_a_well_formed_rejection_is_not_retried(tmp_path):
    sup, final = _run(tmp_path / "run", [_gate_rejects, _gate_approves])
    assert _retry_events(sup) == []
    assert sup.runner.phases_seen.count("verdict_gate") == 1
    assert final != "succeeded"


def test_a_non_empty_malformed_reply_is_not_retried(tmp_path):
    sup, final = _run(tmp_path / "run",
                      [lambda s, p="": _ok("not json at all")])
    assert _retry_events(sup) == []
    assert final == "failed"


# ---------------------------------------------------------------------------
# (4) the retry differs from the failed attempt
# ---------------------------------------------------------------------------


def test_the_retry_prompt_carries_the_bounded_budget_wording(tmp_path):
    wording = RETRY_PROMPT.read_text()
    assert BUDGET_WORDING in wording and BOUNDED_WORDING in wording
    sup, final = _run(tmp_path / "run", [_gate_truncated, _gate_approves])
    assert final == "succeeded"
    first, retry = _gate_prompts(sup)
    assert BUDGET_WORDING not in first and BOUNDED_WORDING not in first
    assert wording.strip() in retry
    assert BUDGET_WORDING in retry and BOUNDED_WORDING in retry


def test_the_retry_runs_one_thinking_step_lower(tmp_path):
    sup, final = _run(tmp_path / "run", [_gate_truncated, _gate_approves],
                      thinking="high")
    assert final == "succeeded"
    first, retry = _gate_meta(sup)
    assert first["thinking"] == "high"
    assert retry["thinking"] == "medium"
    # ... and only the retry: the next dispatch of the same hat is back at
    # the configured level (checked on the pure helper below).


def test_a_hat_at_the_lowest_level_keeps_it(tmp_path):
    sup, final = _run(tmp_path / "run", [_gate_truncated, _gate_approves],
                      thinking="off")
    assert final == "succeeded"
    first, retry = _gate_meta(sup)
    assert first["thinking"] == "off" and retry["thinking"] == "off"


def test_a_hat_with_no_thinking_level_keeps_none(tmp_path):
    sup, _final = _run(tmp_path / "run", [_gate_truncated, _gate_approves],
                       thinking=None)
    first, retry = _gate_meta(sup)
    assert "thinking" not in first and "thinking" not in retry


@pytest.mark.parametrize("level, lower", [
    ("max", "xhigh"), ("xhigh", "high"), ("high", "medium"),
    ("medium", "low"), ("low", "minimal"), ("minimal", "off"),
    ("off", "off"), (None, None), ("unheard-of", "unheard-of")])
def test_lower_thinking_level(level, lower):
    assert tiers.lower_thinking_level(level) == lower


def test_lower_bundle_lowers_thinking_and_effort_and_nothing_else():
    b = HatBundle(model="m", thinking="high", effort=None, harness="pi")
    low = tiers.lower_bundle(b)
    assert (low.model, low.thinking, low.effort, low.harness) == (
        "m", "medium", None, "pi")
    e = tiers.lower_bundle(HatBundle(model="m", effort="medium"))
    assert e.effort == "low" and e.thinking is None


def test_the_step_down_does_not_leak_past_the_retry(tmp_path):
    sup, _final = _run(tmp_path / "run", [_gate_truncated, _gate_approves],
                       thinking="high")
    assert sup._judge_retry_step_down is False
