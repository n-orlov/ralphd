"""v0.36 requirement G -- `exhausted` is the one budget hold.

Spending the iteration budget, the job deadline or the approach budget moves
a run to the non-terminal `exhausted` state: `verdict: null`, `exhaustedBudget`
naming which budget ran out (`iterations`, `time`, `approaches`). `ralphctl
resume` of a held run is refused unless it tops up that budget (any other
top-up passed alongside is still applied), and the resumed engine continues
where the run held: the held step for `iterations`/`time`, the replan on the
next approach number for `approaches`. Every surface (`ralphctl status`,
`ralphctl runs`, the hub run list and run page) shows `exhausted (<budget>)`.

Engine behaviour is driven in-process with scripted hats and a fake clock
(no test sleeps out a real timeout); the CLI refusal runs `ralphctl` against
the stub docker the other resume tests use.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ralphd.cli import main as cli_main
from ralphd.cli import ui_server
from ralphd.engine import loop as loop_mod
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import atomic_write_json, read_events
from tests.conftest import make_supervisor

from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run

__all__ = ["ctl", "unix_sock"]

APP_JS = Path(ui_server.__file__).parent / "web" / "app.js"


# ---------------------------------------------------------------------------
# scripted hats and a fake clock
# ---------------------------------------------------------------------------

class _FakeClock:
    """A monotonic clock only the test advances."""

    def __init__(self) -> None:
        self.now = 1_000.0


class _TimeProxy:
    """Stands in for `time` inside the engine loop: `monotonic()` and
    `time()` read the fake clock, everything else is the real module."""

    def __init__(self, clock: _FakeClock) -> None:
        import time as real_time
        self._real = real_time
        self._clock = clock
        self._epoch0 = real_time.time()

    def monotonic(self) -> float:
        return self._clock.now

    def time(self) -> float:
        return self._epoch0 + (self._clock.now - 1_000.0)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _ok(text: str = "working") -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 30.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


class _ScriptExhausted(BaseException):
    """Raised by a hat the script has no entry for. A BaseException on
    purpose: the engine's infra retry swallows ordinary exceptions and would
    spin on an assertion."""


class _Hats:
    """Scripted hats keyed by the phase the engine recorded."""

    def __init__(self, sup, handlers: dict) -> None:
        self.sup = sup
        self.handlers = handlers
        self.calls = 0
        self.phases: list[str] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.calls += 1
        phase = self.sup.run.read_status().get("phase")
        self.phases.append(phase)
        self.running = True
        try:
            queue = self.handlers.get(phase)
            if not queue:
                raise _ScriptExhausted(f"no scripted hat left for phase {phase!r}")
            step = queue.pop(0) if isinstance(queue, list) else queue
            return step(self.sup)
        finally:
            self.running = False


def _plan(sup) -> IterationResult:
    atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "001", "title": "do the first thing",
         "successCriteria": "the first thing is done"},
        {"id": "002", "title": "do the second thing",
         "successCriteria": "the second thing is done"}]})
    return _ok("planned\n" + json.dumps(
        {"proposal_path": "plan-proposal.json", "summary": "planned"}))


def _replan(sup) -> IterationResult:
    """The second approach's plan: new tasks on top of the current tree."""
    atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "003", "title": "do the replanned thing",
         "successCriteria": "the replanned thing is done"}]})
    return _ok("planned\n" + json.dumps(
        {"proposal_path": "plan-proposal.json", "summary": "replanned"}))


def _complete(sup) -> IterationResult:
    task_id = next(t["id"] for t in sup.run.read_tasks()["tasks"]
                   if t["status"] in ("pending", "in-progress"))
    return _ok("done\n" + json.dumps({"verdict": "complete", "task_id": task_id}))


def _verified(sup) -> IterationResult:
    return _ok("fine\n" + json.dumps({"verdict": "verified"}))


def _supervisor(root: Path, handlers: dict, resume: bool = False, **cfg):
    """A supervisor over `root`; `resume=True` leaves the status.json the
    held episode wrote untouched (what `ralphctl resume` leaves behind)."""
    return make_supervisor(
        root, run_id="exhausted-hold",
        status=None if resume else {"state": "starting"},
        prd="# PRD\n\nDo the thing.\n", vigilant=False, on_complete="exit",
        infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
        infra_outage_budget_s=1000.0,
        runner_factory=lambda sup: _Hats(sup, handlers),
        **{"max_approaches": 2, **cfg})


def _held(sup) -> dict:
    status = sup.run.read_status()
    assert status["state"] == "exhausted", status
    return status


# ---------------------------------------------------------------------------
# (1) the three triggers
# ---------------------------------------------------------------------------

def test_a_spent_iteration_budget_holds_exhausted_iterations(tmp_path):
    # plan + the worker's COMPLETE use both budgeted iterations; review is
    # never entered.
    sup = _supervisor(tmp_path / "run", {"planning": [_plan], "worker": [_complete]},
                      iterations=2)

    assert asyncio.run(sup._run_job_core()) == "exhausted"

    status = _held(sup)
    assert status["verdict"] is None
    assert status["exhaustedBudget"] == "iterations"
    assert not status.get("endedAt")
    assert "iterations 2/2" in status["reason"]
    # the state event names the budget too
    held = [e for e in read_events(sup.run.root, types=("state",))
            if e["state"] == "exhausted"]
    assert [e["exhaustedBudget"] for e in held] == ["iterations"]


def test_a_passed_deadline_holds_exhausted_time(tmp_path, monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(loop_mod, "time", _TimeProxy(clock))

    def slow_worker(sup):
        clock.now += 3 * 3600  # the worker "takes" three hours of a two-hour job
        return _complete(sup)

    sup = _supervisor(tmp_path / "run",
                      {"planning": [_plan], "worker": [slow_worker]},
                      iterations=25, job_timeout_s=2 * 3600)

    assert asyncio.run(sup._run_job_core()) == "exhausted"

    status = _held(sup)
    assert status["verdict"] is None
    assert status["exhaustedBudget"] == "time"
    assert not status.get("endedAt")
    # iterations were NOT the budget that ran out
    assert status["iterationsUsed"] < 25


def test_a_replan_with_no_approach_left_holds_exhausted_approaches(tmp_path, monkeypatch):
    sup = _supervisor(tmp_path / "run", {}, iterations=25, max_approaches=2,
                      approach=2)
    # the position a rejected last approach leaves: the replan step itself
    sup.run.update_status(approach=2)
    monkeypatch.setattr(sup, "_resume_point", lambda: (2, False, "replan"))

    assert asyncio.run(sup._run_job_core()) == "exhausted"

    status = _held(sup)
    assert status["verdict"] is None
    assert status["exhaustedBudget"] == "approaches"
    assert status["approach"] == 2 and status["maxApproaches"] == 2
    assert not status.get("endedAt")
    assert "approaches 2/2" in status["reason"]
    assert sup.runner.calls == 0


# ---------------------------------------------------------------------------
# (2) resume is refused without a top-up of the named budget
# ---------------------------------------------------------------------------

def _hold(c: Ctl, run_id: str, **status) -> tuple:
    rdir, cdir = _seed_run(c, run_id)
    (rdir / "status.json").write_text(json.dumps({"state": "exhausted",
                                                  "verdict": None, **status}))
    return rdir, cdir


_STUB_ENV = lambda run_id: {"STUB_DOCKER_CONTAINERS": f"ralphd-{run_id}",
                            "STUB_DOCKER_RUNNING": ""}


def test_a_bare_resume_of_an_exhausted_run_is_refused_naming_budget_and_command(ctl):
    _hold(ctl, "tst-apx", exhaustedBudget="approaches", approach=3,
          maxApproaches=3)

    res = ctl.run("resume", "tst-apx", env=_STUB_ENV("tst-apx"))

    assert res.returncode == 2, res.stderr
    assert "exhausted: approaches 3/3 — ralphctl resume tst-apx --approaches +1" \
        in res.stderr
    assert not any(a[:2] == ["run", "-d"] for a in ctl.recorded())


def test_the_refusal_names_each_budgets_own_flag(ctl):
    _hold(ctl, "tst-it", exhaustedBudget="iterations", iterationsUsed=5,
          iterationsBudget=5)
    _hold(ctl, "tst-tm", exhaustedBudget="time")

    it = ctl.run("resume", "tst-it", env=_STUB_ENV("tst-it"))
    tm = ctl.run("resume", "tst-tm", env=_STUB_ENV("tst-tm"))

    assert it.returncode == 2 and tm.returncode == 2
    assert "exhausted: iterations 5/5 — ralphctl resume tst-it --iterations +10" \
        in it.stderr
    assert "ralphctl resume tst-tm --timeout +3600" in tm.stderr
    assert "exhausted: time" in tm.stderr


def test_another_topup_alongside_is_applied_but_does_not_satisfy_the_hold(ctl):
    rdir, cdir = _hold(ctl, "tst-mix", exhaustedBudget="approaches",
                       approach=1, maxApproaches=1)

    res = ctl.run("resume", "tst-mix", "--iterations", "+7",
                  env=_STUB_ENV("tst-mix"))

    assert res.returncode == 2, res.stderr
    assert "ralphctl resume tst-mix --approaches +1" in res.stderr
    # refused -- but the OTHER top-up was applied: iterations 5 -> 12
    assert "iterations: 12" in (cdir / "job.yaml").read_text()
    assert not any(a[:2] == ["run", "-d"] for a in ctl.recorded())

    # the named top-up now resumes, and lands in job.yaml
    ok = ctl.run("resume", "tst-mix", "--approaches", "+1",
                 env=_STUB_ENV("tst-mix"))
    assert ok.returncode == 0, ok.stderr
    assert "max_approaches: 2" in (cdir / "job.yaml").read_text()
    assert any(a[:2] == ["run", "-d"] for a in ctl.recorded())


def test_a_0_35_timed_out_run_reads_as_exhausted_and_is_refused_the_same(ctl):
    _hold(ctl, "tst-legacy", exhaustedBudget=None)
    rdir = ctl.registry / "runs" / "tst-legacy"
    (rdir / "status.json").write_text(json.dumps({
        "state": "timed_out", "verdict": None,
        "reason": "the run holds: no iteration budget or wall clock remained"}))

    res = ctl.run("resume", "tst-legacy", env=_STUB_ENV("tst-legacy"))

    assert res.returncode == 2, res.stderr
    assert "exhausted: time" in res.stderr


def test_a_run_that_is_not_exhausted_resumes_without_a_topup(ctl):
    _seed_run(ctl, "tst-plain")

    res = ctl.run("resume", "tst-plain", env=_STUB_ENV("tst-plain"))

    assert res.returncode == 0, res.stderr


# ---------------------------------------------------------------------------
# (3) resuming with the top-up continues where the run held
# ---------------------------------------------------------------------------

def test_iterations_resume_continues_at_the_held_step(tmp_path):
    root = tmp_path / "run"
    sup1 = _supervisor(root, {"planning": [_plan], "worker": [_complete]},
                       iterations=2)
    assert asyncio.run(sup1._run_job_core()) == "exhausted"
    assert _held(sup1)["exhaustedBudget"] == "iterations"

    # the topped-up episode: planning is NOT redone, the held worker step
    # carries on with the second task, then review judges the finished plan
    sup2 = _supervisor(root, {"worker": [_complete], "review": [_verified]},
                       resume=True, iterations=6)
    assert sup2._resume_point()[1:] == (True, "planning")

    assert asyncio.run(sup2._run_job_core()) == "succeeded"

    assert sup2.runner.phases == ["worker", "review"]
    status = sup2.run.read_status()
    assert status["verdict"] == "verified"
    assert status["approach"] == 1
    assert "exhaustedBudget" not in status


def test_time_resume_continues_at_the_held_step_on_a_fresh_window(tmp_path, monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(loop_mod, "time", _TimeProxy(clock))
    root = tmp_path / "run"

    def slow_worker(sup):
        clock.now += 3 * 3600
        return _complete(sup)

    sup1 = _supervisor(root, {"planning": [_plan], "worker": [slow_worker]},
                       iterations=25, job_timeout_s=2 * 3600)
    assert asyncio.run(sup1._run_job_core()) == "exhausted"
    assert _held(sup1)["exhaustedBudget"] == "time"

    # `resume --timeout +N`: a longer window, opened at the (fake) now
    sup2 = _supervisor(root, {"worker": [_complete], "review": [_verified]},
                       resume=True, iterations=25, job_timeout_s=5 * 3600)

    assert asyncio.run(sup2._run_job_core()) == "succeeded"
    assert sup2.runner.phases == ["worker", "review"]
    assert sup2.run.read_status()["approach"] == 1


def test_approaches_resume_continues_at_the_replan_on_the_next_approach(tmp_path):
    root = tmp_path / "run"
    # approach 1 planned and worked on, then the approach budget (1) ran out:
    # the position `exhausted (approaches)` leaves on disk
    sup1 = _supervisor(root, {"planning": [_plan],
                              "worker": [_complete, _complete]},
                       iterations=3, max_approaches=1)
    assert asyncio.run(sup1._run_job_core()) == "exhausted"
    sup1.run.update_status(exhaustedBudget="approaches", approach=1)

    # `resume --approaches +1`: approach 2 exists now. The engine lands on the
    # replan step -- not the held worker, not review -- and plans approach 2.
    sup2 = _supervisor(root, {"planning": [_replan], "worker": [_complete],
                              "review": [_verified]},
                       resume=True, iterations=25, max_approaches=2)

    assert asyncio.run(sup2._run_job_core()) == "succeeded"

    assert sup2.runner.phases[0] == "planning"
    status = sup2.run.read_status()
    assert status["approach"] == 2
    assert "exhaustedBudget" not in status
    assert any("continuing at the replan on approach 2" in (e.get("message") or "")
               for e in read_events(root, types=("log",)))


# ---------------------------------------------------------------------------
# (4) every surface shows `exhausted (<budget>)`
# ---------------------------------------------------------------------------

def _seed_registry(reg: Path) -> None:
    for run_id, extra in (("held-it", {"exhaustedBudget": "iterations"}),
                          ("held-tm", {"exhaustedBudget": "time"}),
                          ("held-ap", {"exhaustedBudget": "approaches"}),
                          ("held-0-35", {"state": "timed_out",
                                         "reason": "no iteration budget remained"})):
        run_dir = reg / "runs" / run_id
        run_dir.mkdir(parents=True)
        status = {"runId": run_id, "state": "exhausted", "verdict": None,
                  "phase": None, "approach": 1, "maxApproaches": 1,
                  "iterationsUsed": 3, "iterationsBudget": 3,
                  "startedAt": "2026-01-01T00:00:00Z", **extra}
        (run_dir / "status.json").write_text(json.dumps(status))


def test_ralphctl_runs_shows_the_budget(tmp_path, monkeypatch, capsys):
    reg = tmp_path / "reg"
    _seed_registry(reg)
    monkeypatch.setenv("RALPHD_REGISTRY", str(reg))

    cli_main.cmd_runs(argparse.Namespace(state=None, json=True, sort=None,
                                         reverse=False))
    rows = {r["runId"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["held-it"]["stateDisplay"] == "exhausted (iterations)"
    assert rows["held-ap"]["exhaustedBudget"] == "approaches"
    assert rows["held-0-35"]["stateDisplay"] == "exhausted (iterations)"

    cli_main.cmd_runs(argparse.Namespace(state=None, json=False, sort=None,
                                         reverse=False))
    table = capsys.readouterr().out
    assert "exhausted (iterations)" in table
    assert "exhausted (time)" in table
    assert "exhausted (approaches)" in table


def test_ralphctl_status_shows_the_budget(tmp_path, monkeypatch, capsys):
    reg = tmp_path / "reg"
    _seed_registry(reg)
    monkeypatch.setenv("RALPHD_REGISTRY", str(reg))
    monkeypatch.setattr(cli_main, "_dangling_run_entry", lambda run_id: None)

    cli_main.cmd_status(argparse.Namespace(run_id="held-tm", json=False))

    out = capsys.readouterr().out
    assert any(line.startswith("state:     exhausted (time)")
               for line in out.splitlines()), out


def test_the_hub_run_list_carries_the_budget(tmp_path):
    reg = tmp_path / "reg"
    _seed_registry(reg)

    rows = {r["runId"]: r for r in ui_server.run_list(reg)}

    assert rows["held-ap"]["stateDisplay"] == "exhausted (approaches)"
    assert rows["held-ap"]["exhaustedBudget"] == "approaches"
    assert rows["held-0-35"]["stateDisplay"] == "exhausted (iterations)"


_NODE = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
function grab(name) {
  const start = src.indexOf('function ' + name + '(');
  if (start < 0) throw Error('missing function ' + name);
  return src.slice(start, src.indexOf('\n}\n', start) + 3);
}
function text(node) {
  if (typeof node === 'string') return node;
  return (node.children || []).map(text).join('');
}
const h = (tag, attrs, children) => ({ tag, attrs: attrs || {}, children: children || [] });
const FAULT_BADGE_STATES = new Set(["failed", "aborted"]);
const faultBadge = () => null;
eval(grab('pill') + grab('stateMarkerSpans') + grab('runRowStateChildren') + grab('stateCell'));
const doc = JSON.parse(process.argv[2]);
console.log(JSON.stringify({
  row: text(h('td', {}, runRowStateChildren(doc))),
  cell: text(stateCell({ runId: 'x' }, doc)),
}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_hub_row_and_run_page_render_the_budget_label():
    out = subprocess.run(
        ["node", "-e", _NODE, str(APP_JS), json.dumps({
            "state": "exhausted", "exhaustedBudget": "approaches",
            "stateDisplay": "exhausted (approaches)", "pauseMarker": "",
            "escalationUndeliveredMarker": ""})],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    rendered = json.loads(out.stdout)
    assert rendered["row"] == "exhausted (approaches)"
    assert rendered["cell"] == "exhausted (approaches)"


def test_the_hub_label_held_at_the_deadline_is_gone():
    source = APP_JS.read_text()
    assert "held at the deadline" not in source
    assert '"timed_out"' in source  # only the 0.35 spelling kept readable
    assert 'RUN_STATE_ORDER = ["starting", "running", "paused", "finishing", ' \
        '"exhausted"' in source
