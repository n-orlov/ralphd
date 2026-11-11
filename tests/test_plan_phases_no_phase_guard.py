"""PRD v0.37 requirement K.1 (decision 6): no phases, no change.

Each bullet of K.1 is tested on a plan with no `phases`: the run directory a
full planning -> worker -> review -> peer gate loop leaves behind carries no
phase key, no `phase_gate.*` event and no `planPhase`; the review, verdict-gate,
worker and verify prompts carry neither phase heading and equal the prompts
built with the phase readers taken out of the loop's inputs entirely (a
differential within 0.37, not a pinned 0.36 text); and the scheduler returns
one sequence with or without the `phases` argument.

The loop runs with scripted hats; nothing sleeps.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time

from ralphd.engine import loop as loop_mod
from ralphd.engine import scheduler
from ralphd.engine.state import RunDir, atomic_write_json, read_events

from tests.conftest import scripted_supervisor
from tests.test_phase_review_dispatch import (
    _init_workspace, _ok, _worker)

HEADINGS = ("## Phase review", "## Earlier phase reviews")

_supervisor = scripted_supervisor(
    run_id="no-phase-guard", run_root="run", workspace="workspace",
    iterations=30, max_approaches=1, vigilant=False, on_complete="exit",
    infra_outage_budget_s=1000.0, operator_input_needed_pause=False,
    peer_verdict_gate=True)


def _plan(sup, prompt=""):
    atomic_write_json(sup.run.root / "plan-proposal.json", {
        "version": 1, "tasks": [
            {"id": "001", "title": "t1", "successCriteria": "criterion one"},
            {"id": "002", "title": "t2", "successCriteria": "criterion two",
             "dependsOn": ["001"]}]})
    return _ok(json.dumps({"proposal_path": "plan-proposal.json",
                           "summary": "planned"}))


class _Seen:
    def __init__(self):
        self.prompts: dict[str, list[str]] = {}

    def hat(self, name, reply):
        def run(sup, prompt=""):
            self.prompts.setdefault(name, []).append(prompt)
            return _ok(json.dumps(reply))
        return run

    def worker(self, sup, prompt=""):
        self.prompts.setdefault("worker", []).append(prompt)
        return _worker(sup, prompt)


def _run_no_phase_loop(tmp_path):
    root = tmp_path / "run"
    ws = tmp_path / "workspace"
    _init_workspace(ws)
    RunDir(root=root).update_status(state="starting")
    seen = _Seen()
    sup = _supervisor(tmp_path, {
        "planning": [_plan], "worker": [seen.worker, seen.worker],
        "review": [seen.hat("review", {"verdict": "verified",
                                       "reason": "fine"})],
        "verdict_gate": [seen.hat("verdict_gate", {"verdict": "verified",
                                                   "reason": "agreed"})]})
    final = asyncio.run(asyncio.wait_for(sup._run_job_core(), 60))
    assert final == "succeeded", sup.run.read_status()
    return sup, seen


# -- K.1 bullets 1-3: the run directory ---------------------------------------

def test_a_no_phase_run_leaves_no_phase_key_in_tasks_json(tmp_path):
    sup, _seen = _run_no_phase_loop(tmp_path)
    for data in (sup.run.read_tasks(),
                 json.loads(sup.run.tasks_file.read_text())):
        assert "phases" not in data
        assert data["tasks"] and all("phase" not in t for t in data["tasks"])


def test_a_no_phase_run_writes_no_phase_gate_event(tmp_path):
    sup, _seen = _run_no_phase_loop(tmp_path)
    lines = [l for l in (sup.run.root / "events.jsonl").read_text().splitlines()
             if l.strip()]
    assert lines
    for line in lines:
        assert "phase_gate." not in line
    names = {str(e.get("type") or e.get("event")) for e in read_events(
        sup.run.root)}
    assert not [n for n in names if n.startswith("phase_gate.")]
    assert not (sup.run.artifacts_dir / "phase-reviews").exists()


def test_a_no_phase_run_has_a_null_plan_phase_and_no_plan_phase_in_iteration_meta(tmp_path):
    sup, _seen = _run_no_phase_loop(tmp_path)
    assert sup.run.read_status().get("planPhase") is None
    metas = sorted(sup.run.root.glob("iterations/*/meta.json"))
    assert len(metas) >= 4      # planning, two workers, review, peer gate
    for meta in metas:
        data = json.loads(meta.read_text())
        assert "planPhase" not in data
        assert data.get("scope") != "phase"


def test_a_no_phase_loops_prompts_carry_neither_phase_heading(tmp_path):
    _sup, seen = _run_no_phase_loop(tmp_path)
    assert set(seen.prompts) == {"worker", "review", "verdict_gate"}
    for name, prompts in seen.prompts.items():
        assert prompts, name
        for prompt in prompts:
            for heading in HEADINGS:
                assert heading not in prompt, (name, heading)


# -- K.1 bullet 4: the prompt differential -----------------------------------

_MID_PLAN = {"version": 1, "tasks": [
    {"id": "001", "title": "first", "status": "validated",
     "successCriteria": "criterion one"},
    {"id": "002", "title": "second", "status": "in-progress",
     "successCriteria": "criterion two", "validationAttempts": 1,
     "validationNotes": "an earlier note"},
    {"id": "003", "title": "third", "status": "pending",
     "successCriteria": "criterion three", "dependsOn": ["002"]}]}


def _prompts(sup) -> dict[str, str]:
    data = sup.run.read_tasks()
    task = next(t for t in data["tasks"] if t["id"] == "002")
    return {
        "review": sup.build_prompt("review", extra=sup._review_extra_context()),
        "verdict_gate": sup.build_prompt(
            "verdict_gate",
            extra=sup._operator_amendment_review_context(
                audience="verdict_gate")),
        "worker": sup.build_prompt(
            "worker", extra=sup._worker_assignment_extra(
                task, tasks_doc=copy.deepcopy(data))),
        "verify": sup.build_prompt(
            "verify", extra=sup._verify_task_extra(task),
            prompt_name="task-verify"),
    }


def _built(tmp_path, name):
    sup = _supervisor(tmp_path / name, approach=1)
    _init_workspace(sup.workspace)
    atomic_write_json(sup.run.tasks_file, copy.deepcopy(_MID_PLAN))
    assert "phases" not in sup.run.read_tasks()
    return sup


class _FrozenTime:
    """`time` as the loop module sees it, with the wall clock held still."""

    def __init__(self, now):
        self._now = now

    def time(self):
        return self._now

    def __getattr__(self, name):
        return getattr(time, name)


def test_no_phase_prompts_equal_the_prompts_built_with_phases_out_of_the_inputs(
        tmp_path, monkeypatch):
    # The job context prints the deadline and the minutes left to it. Two
    # supervisors built a second apart disagree on both, so hold the clock
    # still and give both the same deadline: only the phase readers may differ.
    sup = _built(tmp_path, "a")
    monkeypatch.setattr(loop_mod, "time", _FrozenTime(sup._deadline_epoch - 5400))
    with_hooks = _prompts(sup)
    for name, prompt in with_hooks.items():
        for heading in HEADINGS:
            assert heading not in prompt, (name, heading)

    # `phases` taken out of the loop's inputs: neither phase reader may
    # contribute a byte, and nothing may read the plan's `phases`.
    other = _built(tmp_path, "b")
    other._deadline_epoch = sup._deadline_epoch
    monkeypatch.setattr(type(other), "_phase_review_context",
                        lambda self: "")
    monkeypatch.setattr(type(other), "_earlier_phase_reviews_context",
                        lambda self: "")
    without_hooks = _prompts(other)

    assert with_hooks.keys() == without_hooks.keys() == {
        "review", "verdict_gate", "worker", "verify"}
    for name in with_hooks:
        a = with_hooks[name].replace(str(sup.run.root), "<run>").replace(
            str(sup.workspace), "<ws>")
        b = without_hooks[name].replace(str(other.run.root), "<run>").replace(
            str(other.workspace), "<ws>")
        assert a == b, name


# -- K.1 bullet 5: the scheduler ---------------------------------------------

def _drain(tasks: list, **kw) -> list:
    """The sequence `select_next_task` yields while every pick completes."""
    tasks = copy.deepcopy(tasks)
    order = []
    for _ in range(len(tasks) + 2):
        pick = scheduler.select_next_task(tasks, **kw)
        if pick is None:
            break
        order.append(pick["id"])
        pick["status"] = "completed"
    return order


def test_select_next_task_is_the_same_with_and_without_phases_none():
    tasks = [
        {"id": "a", "status": "pending", "priority": 1},
        {"id": "b", "status": "pending", "priority": 5},
        {"id": "c", "status": "pending", "dependsOn": ["b"]},
        {"id": "d", "status": "validation-failed"},
        {"id": "e", "status": "in-progress"},
        {"id": "f", "status": "pending", "dependsOn": ["zz"]},
        {"id": "g", "status": "failed"},
        {"id": "h", "status": "pending", "dependsOn": ["g"]},
        {"id": "i"}]
    plain = _drain(tasks)
    assert plain == _drain(tasks, phases=None)
    assert plain[:2] == ["d", "e"]       # the two non-pending steps first
    assert "h" not in plain and "f" not in plain
    # and one call over the unchanged fixture
    assert scheduler.select_next_task(copy.deepcopy(tasks)) == \
        scheduler.select_next_task(copy.deepcopy(tasks), phases=None)
    assert scheduler.pending_candidates(copy.deepcopy(tasks)) == \
        scheduler.pending_candidates(copy.deepcopy(tasks), phases=None)
