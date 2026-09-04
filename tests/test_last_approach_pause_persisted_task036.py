"""Task 036 (PRD requirement D.1/D.2, #142) -- the cure for task 011's one
residual gap: the `rejected_last_approach` pause has to be a *resumable*,
*externally observable* pause, not only an in-process `asyncio.Event` plus a
line in `events.jsonl`.

Task 011 landed the edge itself (`tests/test_last_approach_pause_task011.py`:
the declared `review -> review` self-loop, `operator_escalation` with
`reason: rejected_last_approach`, `self.pause()`, the approach left
unarchived, the incurable/mixed cases still failing, and a fresh episode's
`_resume_point()` landing back on `review`). Verification named exactly one
gap: nothing about the pause reached `status.json`, so a host reading run
state (`ralphctl status`, the hub, a plain read of the file) could not tell a
run parked for an operator ruling from one working normally, and `resume` had
no persisted pause to clear.

This module pins that gap closed, end to end:

1. the moment the edge fires, `status.json` carries `state: "paused"` and
   `pauseReason: "rejected_last_approach"` -- read back off disk by a
   *separate* `RunDir` (the host's view, not the supervisor's memory) -- with
   the approach still unarchived;
2. `resume()` clears exactly that pair (`state: "running"`,
   `pauseReason: None`) while leaving `approach` and `phase` untouched, so the
   run re-enters `review` over the SAME approach -- both in-process (the
   `_gate()` the loop parks on lets the next iteration through) and across a
   process restart (`_resume_point()` -> `review`, same approach number);
3. an OPERATOR pause (`pause()` with no reason, what `POST /pause` calls)
   still records nothing at all, and `resume()` on such a run does not rewrite
   `state`;
4. `paused` is part of the state vocabulary every surface shares
   (`state.NONTERMINAL_STATES`, `cli.main._STATUS_STATES`) -- a paused run is a
   run a live engine still owns, never a corrupt one and never deletable.

Everything here drives a real `LoopSupervisor` over a real run dir, the same
shape task 011's own module uses -- never a mock of the guard.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import findings, state as state_mod
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import PAUSED_STATE, RunDir


def _finding(severity: str, curable: bool, clause: str, evidence: str) -> dict:
    return {"severity": severity, "curable": curable, "clause": clause,
            "evidence": evidence}


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _supervisor(root: Path, seed_status: bool = True, **cfg_kw) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    (root / "prd.md").write_text("# task 036 fixture PRD\n\nDo the thing.\n")
    run = RunDir(root=root)
    if seed_status:
        # `seed_status=False` is the crashed-episode shape: a second
        # supervisor over the SAME run dir must read whatever the previous
        # episode left on status.json, paused marker included.
        run.update_status(state="running", phase="review", approach=1)
    kw = {"max_approaches": 1}
    kw.update(cfg_kw)
    cfg = JobConfig(run_id="task036-persisted-pause", **kw)
    return LoopSupervisor(cfg, run, root)


def _host_status(root: Path) -> dict:
    """What a HOST reader sees: status.json parsed off disk, no supervisor."""
    return json.loads((root / "status.json").read_text())


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


def _drive_the_pause_edge(root: Path) -> LoopSupervisor:
    """Run one real `_step_review()` that lands on `rejected_last_approach`:
    last approach, every blocking finding curable, the approach's one cure
    pass already spent."""
    sup = _supervisor(root, max_approaches=1)
    sup._approach = 1
    sup.run.update_status(curePasses={"1": 1})
    findings.write_findings_json(sup.run.findings_json_file, [
        _finding("blocking", True, "requirement D.1 (#142)",
                 "the pause is still invisible to the host"),
    ])

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected",
                                   "reason": "the cure did not land"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]
    outcome = asyncio.run(sup._step_review())
    assert outcome == ("advance", "review", "rejected_last_approach")
    return sup


# ---------------------------------------------------------------------------
# 1. The persisted pause, observable from the host, approach unarchived.
# ---------------------------------------------------------------------------


def test_the_edge_persists_the_pause_to_status_json_immediately(tmp_path):
    root = tmp_path / "run"
    sup = _drive_the_pause_edge(root)

    # in-process pause machinery: unchanged (task 011's own assertion)
    assert sup._pause.is_set() is False

    # the new half: the HOST can see it, off disk, with no supervisor at all.
    host = _host_status(root)
    assert host["state"] == PAUSED_STATE == "paused"
    assert host["pauseReason"] == "rejected_last_approach"
    # ... and it is the recorded state a second reader of the run dir sees too
    assert RunDir(root=root).read_status()["state"] == "paused"

    # never failed on paperwork, never finished
    assert host["state"] not in ("failed", "succeeded", "finishing")
    # the approach is left UNARCHIVED (requirement D.1) and no composite PRD
    # for a fresh, nonexistent approach was written
    assert not (root / "approaches" / "01").exists()
    assert not sup.run.composite_prd_file.exists()
    assert host["approach"] == 1
    # no `phase: "paused"` -- the phase keeps naming what ran last
    assert host["phase"] == "review"

    # the escalation the operator is meant to act on is still recorded, and
    # its reason is the SAME string the status field carries -- one vocabulary.
    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == host["pauseReason"]


def test_the_persisted_pause_is_recorded_before_the_escalation_is_notified(
        tmp_path):
    """"Immediately" means at the escalation site, not at the boundary the
    loop later parks on: by the time the notifier is called (still inside
    `_step_review`), status.json already says paused -- so a notification that
    tells an operator to go look never beats the state they would look at."""
    root = tmp_path / "run"
    seen: list[dict] = []

    sup = _supervisor(root, max_approaches=1)
    sup._approach = 1
    sup.notifier = lambda text: seen.append(_host_status(root))
    sup.run.update_status(curePasses={"1": 1})
    findings.write_findings_json(sup.run.findings_json_file, [
        _finding("blocking", True, "requirement D.1 (#142)", "still open")])

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]
    assert asyncio.run(sup._step_review()) == (
        "advance", "review", "rejected_last_approach")

    assert len(seen) == 1
    assert seen[0]["state"] == "paused"
    assert seen[0]["pauseReason"] == "rejected_last_approach"


# ---------------------------------------------------------------------------
# 2. `resume` clears the persisted pause, same approach, back into review.
# ---------------------------------------------------------------------------


def test_resume_clears_the_persisted_pause_and_keeps_approach_and_phase(
        tmp_path):
    root = tmp_path / "run"
    sup = _drive_the_pause_edge(root)
    assert _host_status(root)["state"] == "paused"

    sup.resume()

    host = _host_status(root)
    assert host["state"] == "running"
    assert host["pauseReason"] is None
    # the same approach, still unarchived, still in the review phase --
    # `resume` re-reviews what was rejected, it does not spend a fresh
    # (nonexistent) approach.
    assert host["approach"] == 1
    assert host["phase"] == "review"
    assert not (root / "approaches" / "01").exists()
    assert sup._approach == 1

    # the in-process gate is released too: the next iteration boundary does
    # not park, and nothing is credited as paused wait for a run that was
    # never actually parked at a boundary.
    assert sup._pause.is_set() is True
    assert asyncio.run(sup._gate()) == 0.0


def test_a_fresh_episode_re_enters_review_over_the_same_approach(tmp_path):
    """The cross-process half of "resume re-enters the review phase": a brand
    new `LoopSupervisor` over the same run dir (the `ralphctl resume` shape,
    no shared Python state) reads the pause off disk and routes straight back
    into `review` on approach 1 -- never `worker`, never `replan`."""
    root = tmp_path / "run"
    sup1 = _drive_the_pause_edge(root)
    assert _host_status(root)["pauseReason"] == "rejected_last_approach"
    # the plan the paused approach worked (what makes this a *resume* of an
    # existing run dir rather than a fresh run: `_resuming_existing_run_dir`)
    state_mod.atomic_write_json(sup1.run.tasks_file, {"version": 1, "tasks": [
        {"id": "001", "title": "do the thing", "status": "completed",
         "successCriteria": "the thing is done"},
    ]})

    sup2 = _supervisor(root, seed_status=False, max_approaches=1)
    # the crashed episode's paused marker is still on disk -- the host can
    # still read WHY the run is parked, not just that it stopped
    assert _host_status(root)["state"] == "paused"
    assert sup2._paused_awaiting_last_approach_ruling(1) is True
    start_approach, skip_planning, start_step = sup2._resume_point()
    assert (start_approach, skip_planning, start_step) == (1, False, "review")


def test_a_fresh_episodes_move_to_running_clears_the_stale_pause_reason():
    """The startup write that moves a resumed episode to `running`
    (`_run_job_core`'s first `update_status`) also nulls `pauseReason`: the
    engine-recorded pause belongs to the process that held it, and leaving its
    reason beside `state: "running"` would strand a ruling-wanted marker on a
    run that is working again. Read off the engine's own call site rather than
    retyped, so it cannot drift."""
    import ast
    import inspect

    loop_src = Path(inspect.getsourcefile(LoopSupervisor)).read_text()
    module = ast.parse(loop_src)
    calls = []
    for node in ast.walk(module):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_run_job_core"):
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "update_status"):
                    kws = {kw.arg: kw.value for kw in inner.keywords}
                    state_kw = kws.get("state")
                    if (isinstance(state_kw, ast.Constant)
                            and state_kw.value == "running"):
                        calls.append(kws)
    assert len(calls) == 1, "expected exactly one move-to-running status write"
    assert "pauseReason" in calls[0]
    assert isinstance(calls[0]["pauseReason"], ast.Constant)
    assert calls[0]["pauseReason"].value is None


def test_resume_logs_the_cleared_pause_reason(tmp_path):
    root = tmp_path / "run"
    sup = _drive_the_pause_edge(root)
    sup.resume()
    messages = [ev.get("message", "") for ev in _events(root, "log")]
    assert any("rejected_last_approach" in m and "cleared" in m
               for m in messages), messages
    assert "resumed" in messages


# ---------------------------------------------------------------------------
# 3. An operator pause is still not a state.
# ---------------------------------------------------------------------------


def test_an_operator_pause_records_no_state_and_resume_does_not_rewrite_it(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=1)
    sup.run.update_status(state="finishing")  # an outcome already settled

    sup.pause()  # what POST /pause calls: no reason
    assert sup._pause.is_set() is False
    host = _host_status(root)
    assert host["state"] == "finishing"      # untouched
    assert host.get("pauseReason") is None

    sup.resume()
    # `resume` only clears the ENGINE's recorded pause; a run whose state is
    # anything else (here: `finishing`) is never resurrected to `running`.
    assert _host_status(root)["state"] == "finishing"
    assert sup._pause.is_set() is True


def test_the_engine_never_records_a_pause_over_a_settled_outcome(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=1)
    for settled in ("finishing", "succeeded", "failed", "aborted"):
        sup.run.update_status(state=settled)
        sup.pause(reason="rejected_last_approach")
        assert _host_status(root)["state"] == settled
        assert _host_status(root).get("pauseReason") is None
        sup.resume()


# ---------------------------------------------------------------------------
# 4. `paused` is in the shared state vocabulary.
# ---------------------------------------------------------------------------


def test_paused_is_nonterminal_and_recognized_by_every_surface():
    from ralphd.cli import main as cli_main

    assert PAUSED_STATE == "paused"
    assert PAUSED_STATE in state_mod.NONTERMINAL_STATES
    assert PAUSED_STATE not in state_mod.TERMINAL_STATES
    # a live engine still owns a paused run: `ralphctl repair`/`doctor` must
    # treat it as a recognized state (never "unrecognized state"), and the
    # sort orders every surface shares must know where it goes.
    assert PAUSED_STATE in cli_main._STATUS_STATES
    assert PAUSED_STATE in cli_main.RUN_STATE_ORDER
    assert (cli_main.RUN_STATE_ORDER.index("running")
            < cli_main.RUN_STATE_ORDER.index(PAUSED_STATE)
            < cli_main.RUN_STATE_ORDER.index("finishing"))
    app_js = (Path(cli_main.__file__).resolve().parent / "web" / "app.js").read_text()
    assert '"paused"' in app_js.split("RUN_STATE_ORDER")[1].split("]")[0]
