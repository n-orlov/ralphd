"""D.1 -- the last-approach pause, resume and escalation, folded into one
module (task 022, requirement D.1). Absorbs, whole, the five `test_*_task*.py`
modules whose subject was this edge family:

* `tests/test_last_approach_pause_task011.py` (task 011, D.1/D.2, #142): the
  declared `review -> review` self-loop (`rejected_last_approach`), the pause
  outcome itself (`self.pause()`, `operator_escalation`, approach left
  unarchived), the cure-pass-still-available and incurable/mixed-curability
  cases that must NOT take the pause edge, and both the in-process and
  fresh-process resume routing back into `review`.
* `tests/test_last_approach_pause_persisted_task036.py` (task 036, cure of
  011's one residual gap): the pause reaching `status.json` (`state:
  "paused"`, `pauseReason: "rejected_last_approach"`) so a HOST reader (no
  supervisor) can see it, `resume()` clearing exactly that pair, an operator
  pause (`pause()` with no reason) recording no state at all, and `paused`
  being part of the state vocabulary every surface shares.
* `tests/test_resume_last_approach_pause_task024.py` (task 024, D.4): the
  in-process half `036` did not cover -- the SAME `LoopSupervisor` actually
  asking `review` again over the SAME approach after `resume()`, and that
  second review reaching a terminal verdict, waiver-aware.
* `tests/test_stagnation_last_approach_pause_task012.py` (task 012, B.3,
  #153): the same generalisation reached from `worker` instead of `review` --
  a stagnant LAST approach pauses with `reason: "stagnation"` instead of
  falling through to `failed` silently, and resumes back into `worker`.
* `tests/test_notifier_wiring_task007.py` (task 007, D.1 finding 5): the
  escalation reaching the CONFIGURED notifier through `hat_tools.
  send_notification`, with a broken/failing poster never costing the pause
  or the `operator_escalation` event.

Two node names collided across the absorbed modules (`review`'s and
`worker`'s escalation each landed a node named
`test_the_escalation_is_emitted_to_events_jsonl_with_no_notifier`) -- both
survive here, renamed `_review`/`_worker` respectively; see
`docs/test-suite-audit.md` § "Subsumed" for the two rows that record the
rename.

Everything below drives a real `LoopSupervisor` (`_step_review()`,
`_step_worker()`, `_step_paused()`, `_resume_point()`, `resume()`, `pause()`)
over a real run dir -- never a mock of the guard itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from ralphd.engine import findings, hat_tools, run_record, scheduler, state as state_mod, transitions
from ralphd.engine import loop as loop_mod
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import PAUSED_STATE, RunDir, atomic_write_json, write_amendment
from tests.conftest import make_supervisor, supervisor_factory, supervisor_with_runner

AMENDMENT_MARKER = "Operator rulings (task 028, #26)"
TASK_ID = "001"


def _finding(severity: str, curable: bool, clause: str, evidence: str,
             cured_when: str | None = None) -> dict:
    payload = {"severity": severity, "curable": curable, "clause": clause,
               "evidence": evidence}
    # Task 011 (PRD requirement C.2, #219): a BLOCKING, curable finding
    # with no `cured_when` is now one of the three shapes `_review_
    # contract_ladder` pauses the run over (`review_findings_unusable`)
    # instead of taking whatever edge this module's own fixtures exist to
    # drive -- default one in, the same fix `tests/test_cure_pass.py`'s
    # own `_finding` helper carries.
    if cured_when is None and severity == "blocking" and curable is True:
        cured_when = f"{clause} is discharged"
    if cured_when is not None:
        payload["cured_when"] = cured_when
    return payload


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


class _StubNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, text: str) -> None:
        self.messages.append(text)


def _host_status(root: Path) -> dict:
    """What a HOST reader sees: status.json parsed off disk, no supervisor."""
    return json.loads((root / "status.json").read_text())


# ---------------------------------------------------------------------------
# Supervisor factories, one per absorbed module's own fixture shape.
# ---------------------------------------------------------------------------

# task 011 + task 007's shape: a bare review-only run, `notifier_url`
# overridable per call (task 007's own notifier-wiring tests).
# Task 014 (PRD requirement A.1, #224): `cfg_defaults=False` means the bare
# `JobConfig` default applies, and that default is now `True` for
# `operator_input_needed_pause` -- explicitly off below, so this module's
# own subject (the ladder park on the last approach) needs the park ON.
_supervisor_review = supervisor_factory(
    run_id="task011-last-approach-pause", cfg_defaults=False,
    prd="# task 011 fixture PRD\n\nDo the thing.\n",
    status={"state": "running"}, max_approaches=1,
    operator_input_needed_pause=True)

# task 036's shape: `seed_status=False` is the crashed-episode form -- a
# second supervisor over the SAME run dir must read whatever the previous
# episode left on status.json, paused marker included, so it writes no
# status of its own.
_supervisor_persisted = lambda root, seed_status=True, **cfg: make_supervisor(
    root, **{"run_id": "task036-persisted-pause", "cfg_defaults": False,
             "prd": "# task 036 fixture PRD\n\nDo the thing.\n",
             "status": ({"state": "running", "phase": "review", "approach": 1}
                        if seed_status else None),
             "max_approaches": 1, **cfg})

# task 024's shape: a run with one already-completed task, so `resume()`'s
# second `_step_review()` call has a real plan behind it.
_supervisor_resume = supervisor_factory(
    run_id="task024-resume-same-approach", cfg_defaults=False, workspace="ws",
    prd="# task 024 fixture PRD\n\nDo the thing.\n",
    status={"state": "running", "phase": "review", "approach": 1},
    tasks=[{"id": "001", "title": "do the thing", "status": "completed",
            "successCriteria": "the thing is done"}],
    max_approaches=1)


def _task(**over) -> dict:
    task = {"id": TASK_ID, "title": "make the thing work",
            "status": "pending", "successCriteria": "the thing works"}
    task.update(over)
    return task


class _NoopRunner:
    """Every iteration is an ordinary successful one: the stagnation tests
    are about the counter, not about what any hat says."""

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw):
        return _result("looked at the task and made no change")


# task 012's shape: the worker/stagnation path, `_NoopRunner` throughout.
_supervisor_worker = supervisor_with_runner(
    lambda sup: _NoopRunner(),
    run_id="task012-stagnation-last-approach", cfg_defaults=False,
    prd="# task 012 fixture PRD\n\nDo the thing.\n", status={"state": "running"},
    tasks=[_task()], max_approaches=1, vigilant=False, iterations=20)


# ===========================================================================
# 1. `review`'s edge itself (task 011).
# ===========================================================================


def test_the_retired_last_approach_edge_is_declared_nowhere():
    """v0.36 requirement B: the last-approach pause is a trigger
    (`cure_rounds_spent`) of the one ladder park, `review -> paused` on
    `operator_input_needed`."""
    for vigilant in (True, False):
        guards = transitions.guard_names("review", vigilant)
        assert "rejected_last_approach" not in guards
        by_guard = {e.guard: e.target for e in
                   transitions.successors("review", vigilant)}
        assert by_guard["operator_input_needed"] == "paused"


def test_the_retired_last_approach_edge_is_also_absent_under_the_peer_verdict_gate_delta():
    edges = transitions.successors("review", vigilant=False,
                                   peer_verdict_gate=True)
    by_guard = {e.guard: e.target for e in edges}
    assert "rejected_last_approach" not in by_guard
    assert by_guard["operator_input_needed"] == "paused"


# ===========================================================================
# 2. `review`'s pause outcome (task 011): last approach, all curable, cure
#    pass already spent -> pause and escalate, never `failed`, unarchived.
# ===========================================================================


def test_last_approach_all_curable_with_cure_spent_pauses_and_escalates(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor_review(root, max_approaches=1)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier
    # simulate the one cure pass this approach already spent (task 009).
    sup.cfg.cure_pass_ceiling = 1  # v0.36 C.2: the approach's one round is spent
    sup.run.update_status(curePasses={"1": 1})

    still_curable = [
        _finding("blocking", True, "requirement C.1 (#141)",
                "the notify tool still doesn't record the event"),
    ]
    findings.write_findings_json(sup.run.findings_json_file, still_curable)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected",
            "reason": "cure did not fully resolve the finding",
            "findings_path": "review-findings.md",
        }))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    assert sup._pause.is_set() is True  # not paused yet

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "paused", "operator_input_needed")
    # paused through the EXISTING pause machinery -- the same asyncio.Event
    # POST /pause's loop.pause() clears.
    assert sup._pause.is_set() is False
    status = sup.run.read_status()
    assert status.get("state") != "succeeded"
    assert status.get("state") != "failed"  # never failed on paperwork

    # never archived, never a composite PRD for a fresh (nonexistent)
    # approach -- resume asks review again over the SAME approach.
    assert not (root / "approaches" / "01").exists()
    assert not sup.run.composite_prd_file.exists()
    assert sup._approach == 1

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "operator_input_needed"
    assert escalations[0]["approach"] == 1
    assert escalations[0]["findings"][0]["clause"] == "requirement C.1 (#141)"
    # the event's own `escalation` field keeps the FULL, un-digested text --
    # evidence and the exact `ralphctl amend` invocation both present.
    full_text = escalations[0]["escalation"]
    assert "requirement C.1 (#141)" in full_text
    assert "the notify tool still doesn't record the event" in full_text
    assert "resolve with: ralphctl amend" in full_text

    # Cure task cure-01-02 (PRD requirement A.1/A.2, #269): the notifier
    # itself gets the bounded, EVIDENCE-FREE digest -- the finding's
    # clause, never its evidence, and neither a `resolve with:` line (the
    # digest never carries one) -- plus both live-run levers, since this
    # pause waits on an operator ruling.
    assert len(notifier.messages) == 1
    text = notifier.messages[0]
    assert "requirement C.1 (#141)" in text
    assert "the notify tool still doesn't record the event" not in text
    assert "resolve with: ralphctl amend" not in text
    assert "ralphctl answer task011-last-approach-pause" in text
    assert "ralphctl unpause task011-last-approach-pause" not in text


def test_the_review_escalation_is_emitted_to_events_jsonl_with_no_notifier(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor_review(root, max_approaches=1)
    sup._approach = 1
    assert sup.notifier is None
    sup.cfg.cure_pass_ceiling = 1  # v0.36 C.2: the approach's one round is spent
    sup.run.update_status(curePasses={"1": 1})

    still_curable = [_finding("blocking", True, "requirement C.1 (#141)",
                              "still not fixed")]
    findings.write_findings_json(sup.run.findings_json_file, still_curable)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())
    assert outcome == ("advance", "paused", "operator_input_needed")
    assert sup._pause.is_set() is False

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "operator_input_needed"


# ===========================================================================
# 3. `review`'s D.1 other half (task 011): a cure pass still available on
#    the last approach is taken instead of the pause edge.
# ===========================================================================


def test_last_approach_first_rejection_still_takes_the_cure_pass(tmp_path, monkeypatch):
    # Task 020 (D.11, #196): `_step_review()` reads real operator rulings
    # under `CONFIG_DIR` -- this container's own `/config/amendments/`
    # (see tests/conftest's own gotcha) -- and, with an empty tasks.json
    # below, every task-scoped ruling there reads as ORPHANED and would
    # notify `sup.notifier` unrelated to this test's own escalation
    # assertion; isolate it with an empty amendments dir, same pattern as
    # every other node here that cares about ruling context.
    root = tmp_path / "run"
    cdir = tmp_path / "config"
    cdir.mkdir(parents=True)
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", cdir)
    sup = _supervisor_review(root, max_approaches=1)
    sup._approach = 1
    sup.notifier = _StubNotifier()
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})

    curable = [_finding("blocking", True, "requirement C.1 (#141)",
                        "the notify tool doesn't exist yet")]
    findings.write_findings_json(sup.run.findings_json_file, curable)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "cure", "rejected_curable_pass_available")
    assert sup._pause.is_set() is True  # never paused -- cured instead
    assert sup.notifier.messages == []  # never escalated
    assert sup.run.read_status().get("curePasses") == {"1": 1}


# ===========================================================================
# 4. `review`'s D.2 (task 011): any incurable blocking finding on the last
#    approach still takes `rejected_no_approach_left` -> `failed`, never the
#    pause edge, whether or not a cure pass has already been spent.
# ===========================================================================


def test_last_approach_with_an_incurable_finding_parks_instead_of_failing(tmp_path):
    """v0.36 requirement B (was: `rejected_no_approach_left` -> `failed`): an
    incurable blocking finding on the last approach parks on the ladder with
    trigger `incurable`; the run is never failed on the spot."""
    root = tmp_path / "run"
    sup = _supervisor_review(root, max_approaches=1)
    sup._approach = 1
    sup.notifier = _StubNotifier()

    all_incurable = [_finding("blocking", False, "requirement B (#111)",
                              "an environment fact a fresh approach cannot "
                              "change")]
    findings.write_findings_json(sup.run.findings_json_file, all_incurable)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "paused", "operator_input_needed")
    assert sup._pause.is_set() is False
    status = sup.run.read_status()
    assert status["state"] == "awaiting_input"
    assert status["inputQuestion"]["trigger"] == "incurable"
    assert status["inputQuestion"]["onTimeout"] == state_mod.INPUT_ON_TIMEOUT_EXHAUSTED
    assert not (root / "approaches" / "01").exists()


def test_last_approach_with_mixed_curability_parks_instead_of_failing(tmp_path):
    """A blocking set that is neither all-curable nor all-incurable (mixed)
    parks as `incurable` on the last approach (v0.36 requirement B)."""
    root = tmp_path / "run"
    sup = _supervisor_review(root, max_approaches=1)
    sup._approach = 1
    sup.notifier = _StubNotifier()

    mixed = [
        _finding("blocking", False, "requirement B (#111)", "incurable one"),
        _finding("blocking", True, "requirement D (#99)", "curable one"),
    ]
    findings.write_findings_json(sup.run.findings_json_file, mixed)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "mixed"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "paused", "operator_input_needed")
    status = sup.run.read_status()
    assert status["state"] == "awaiting_input"
    assert status["inputQuestion"]["trigger"] == "incurable"


# ===========================================================================
# 5. `review`'s resume routing (task 011): a fresh `LoopSupervisor` over
#    the SAME on-disk run dir detects the pause and routes straight back
#    into `review`, over the SAME approach -- never `worker`, never `replan`.
# ===========================================================================


def test_resume_detects_the_last_approach_pause_and_re_enters_review(
        tmp_path):
    root = tmp_path / "run"
    sup1 = _supervisor_review(root, max_approaches=1)
    sup1._approach = 1
    sup1.cfg.cure_pass_ceiling = 1  # v0.36 C.2: the approach's one round is spent
    sup1.run.update_status(curePasses={"1": 1})
    atomic_write_json(sup1.run.tasks_file, {"version": 1, "tasks": [
        {"id": "001", "title": "do the thing", "status": "completed",
         "successCriteria": "the thing is done"},
    ]})

    still_curable = [_finding("blocking", True, "requirement C.1 (#141)",
                              "still not fixed")]
    findings.write_findings_json(sup1.run.findings_json_file, still_curable)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))

    sup1.runner.run = _review_rejects  # type: ignore[method-assign]
    outcome = asyncio.run(sup1._step_review())
    assert outcome == ("advance", "paused", "operator_input_needed")
    assert sup1._pause.is_set() is False

    # a brand-new process, no shared Python state at all -- reads
    # everything fresh off disk (`events.jsonl`, `status.json`).
    sup2 = _supervisor_review(root, max_approaches=1)
    assert sup2._paused_awaiting_operator_input_needed_ruling(1) is True
    # the legacy 0.35 predicates (`rejected_incurable`) do not also fire.
    assert sup2._paused_awaiting_incurable_ruling(1) is False

    start_approach, skip_planning, start_step = sup2._resume_point()
    assert start_approach == 1
    assert skip_planning is False
    assert start_step == "review"


def test_paused_awaiting_last_approach_ruling_is_false_once_a_new_iteration_started(
        tmp_path):
    """Once a fresh iteration has actually started since the escalation
    (an in-process `resume()` that got all the way to running another
    review attempt), the predicate must not fire again for the now-stale
    escalation -- the same shape
    `tests/test_resume_after_incurable_rejection.py` pins for its own
    sibling predicate."""
    root = tmp_path / "run"
    sup = _supervisor_review(root, max_approaches=1)
    sup._approach = 1
    sup.cfg.cure_pass_ceiling = 1  # v0.36 C.2: the approach's one round is spent
    sup.run.update_status(curePasses={"1": 1})

    still_curable = [_finding("blocking", True, "requirement C.1 (#141)",
                              "still not fixed")]
    findings.write_findings_json(sup.run.findings_json_file, still_curable)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]
    outcome = asyncio.run(sup._step_review())
    assert outcome == ("advance", "paused", "operator_input_needed")
    assert sup._paused_awaiting_operator_input_needed_ruling(1) is True

    sup.run.emit("iteration.start", phase="review", approach=1)
    assert sup._paused_awaiting_operator_input_needed_ruling(1) is False


# ===========================================================================
# 6. The pause reaches `status.json`, host-observable (task 036).
# ===========================================================================


def _drive_the_persisted_pause_edge(root: Path) -> LoopSupervisor:
    """Run one real `_step_review()` that lands on `rejected_last_approach`:
    last approach, every blocking finding curable, the approach's one cure
    pass already spent."""
    sup = _supervisor_persisted(root, max_approaches=1)
    sup._approach = 1
    sup.cfg.cure_pass_ceiling = 1  # v0.36 C.2: the approach's one round is spent
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
    assert outcome == ("advance", "paused", "operator_input_needed")
    return sup


def test_the_edge_persists_the_pause_to_status_json_immediately(tmp_path):
    root = tmp_path / "run"
    sup = _drive_the_persisted_pause_edge(root)

    # in-process pause machinery: unchanged (task 011's own assertion)
    assert sup._pause.is_set() is False

    # the new half: the HOST can see it, off disk, with no supervisor at all.
    host = _host_status(root)
    # v0.36: the park lands in `awaiting_input` (covering node: tests/test_awaiting_input_question.py)
    assert host["state"] == "awaiting_input"
    assert host["pauseReason"] == "operator_input_needed"
    # ... and it is the recorded state a second reader of the run dir sees too
    # v0.36: the park lands in `awaiting_input` (covering node: tests/test_awaiting_input_question.py)
    assert RunDir(root=root).read_status()["state"] == "awaiting_input"

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

    sup = _supervisor_persisted(root, max_approaches=1)
    sup._approach = 1
    sup.notifier = lambda text: seen.append(_host_status(root))
    sup.cfg.cure_pass_ceiling = 1  # v0.36 C.2: the approach's one round is spent
    sup.run.update_status(curePasses={"1": 1})
    findings.write_findings_json(sup.run.findings_json_file, [
        _finding("blocking", True, "requirement D.1 (#142)", "still open")])

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]
    assert asyncio.run(sup._step_review()) == (
        "advance", "paused", "operator_input_needed")

    assert len(seen) == 1
    # v0.36: the park lands in `awaiting_input` (covering node: tests/test_awaiting_input_question.py)
    assert seen[0]["state"] == "awaiting_input"
    assert seen[0]["pauseReason"] == "operator_input_needed"


# ===========================================================================
# 7. `resume` clears the persisted pause, same approach, back into review
#    (task 036).
# ===========================================================================


def test_resume_clears_the_persisted_pause_and_keeps_approach_and_phase(
        tmp_path):
    root = tmp_path / "run"
    sup = _drive_the_persisted_pause_edge(root)
    # v0.36: the park lands in `awaiting_input` (covering node: tests/test_awaiting_input_question.py)
    assert _host_status(root)["state"] == "awaiting_input"

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
    sup1 = _drive_the_persisted_pause_edge(root)
    assert _host_status(root)["pauseReason"] == "operator_input_needed"
    # the plan the paused approach worked (what makes this a *resume* of an
    # existing run dir rather than a fresh run: `_resuming_existing_run_dir`)
    state_mod.atomic_write_json(sup1.run.tasks_file, {"version": 1, "tasks": [
        {"id": "001", "title": "do the thing", "status": "completed",
         "successCriteria": "the thing is done"},
    ]})

    sup2 = _supervisor_persisted(root, seed_status=False, max_approaches=1)
    # the crashed episode's paused marker is still on disk -- the host can
    # still read WHY the run is parked, not just that it stopped
    # v0.36: the park lands in `awaiting_input` (covering node: tests/test_awaiting_input_question.py)
    assert _host_status(root)["state"] == "awaiting_input"
    assert sup2._paused_awaiting_operator_input_needed_ruling(1) is True
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
    sup = _drive_the_persisted_pause_edge(root)
    sup.resume()
    messages = [ev.get("message", "") for ev in _events(root, "log")]
    assert any("operator_input_needed" in m and "cleared" in m
               for m in messages), messages
    assert "resumed" in messages


# ===========================================================================
# 8. An operator pause is still not a state (task 036).
# ===========================================================================


def test_an_operator_pause_records_no_state_and_resume_does_not_rewrite_it(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor_persisted(root, max_approaches=1)
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
    sup = _supervisor_persisted(root, max_approaches=1)
    for settled in ("finishing", "succeeded", "failed", "aborted"):
        sup.run.update_status(state=settled)
        sup.pause()
        assert _host_status(root)["state"] == settled
        assert _host_status(root).get("pauseReason") is None
        assert _host_status(root).get("pausedBy") is None
        sup.resume()


# ===========================================================================
# 9. `paused` is in the shared state vocabulary (task 036).
# ===========================================================================


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


# ===========================================================================
# 10. `resume` actually re-asks `review`, in-process, over the SAME
#     approach, to a terminal verdict (task 024).
# ===========================================================================


def _drive_the_resume_pause_edge(sup: LoopSupervisor) -> None:
    """The exact `rejected_last_approach` fixture section 6 above pins in
    isolation: last (only) approach, one blocking-but-curable finding, this
    approach's own cure pass already spent."""
    sup._approach = 1
    sup.cfg.cure_pass_ceiling = 1  # v0.36 C.2: the approach's one round is spent
    sup.run.update_status(curePasses={"1": 1})
    findings.write_findings_json(sup.run.findings_json_file, [
        _finding("blocking", True, "requirement D.1/D.2 (#142)",
                 "the cure did not land before the last cure pass ran out",
                 cured_when="the cure lands before the cure-pass ceiling is spent"),
    ])

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected",
            "reason": "the cure did not land",
            "findings_path": "review-findings.md",
        }))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]
    outcome = asyncio.run(sup._step_review())
    assert outcome == ("advance", "paused", "operator_input_needed")


def test_resume_asks_review_again_over_the_same_approach_and_reaches_terminal(
        tmp_path, monkeypatch):
    root = tmp_path / "run"
    sup = _supervisor_resume(root)

    # --- episode: the last-approach pause fires, in-process -------------
    _drive_the_resume_pause_edge(sup)
    assert sup._pause.is_set() is False
    status = sup.run.read_status()
    # v0.36: the park lands in `awaiting_input` (covering node: tests/test_awaiting_input_question.py)
    assert status["state"] == "awaiting_input"
    assert status["pauseReason"] == "operator_input_needed"
    assert status["approach"] == 1
    assert not (root / "approaches" / "01").exists()
    assert not sup.run.composite_prd_file.exists()

    # --- the operator's ruling, recorded WHILE the run sits paused ------
    cdir = tmp_path / "config"
    write_amendment(cdir, "001",
                    "Waived: the residual gap is cured; re-review the "
                    "same approach.", waive=True)
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", cdir)

    # --- `ralphctl resume` -----------------------------------------------
    sup.resume()
    status = sup.run.read_status()
    assert status["state"] == "running"
    assert status["pauseReason"] is None
    assert status["approach"] == 1          # never bumped
    assert status["phase"] == "review"      # still the phase that paused
    assert sup._pause.is_set() is True       # in-process gate released
    assert asyncio.run(sup._gate()) == 0.0   # the next boundary does not park

    # --- review is asked again, over the SAME approach, SAME process ----
    prompts: list[str] = []

    async def _review_sees_the_ruling_and_verifies(prompt, transcript, **kw):
        prompts.append(prompt)
        assert AMENDMENT_MARKER in prompt
        assert "Waived: the residual gap is cured" in prompt
        # a real review hat re-runs curability on this pass and, seeing the
        # waiver, writes a fresh sidecar with no blocking findings left --
        # `_review_verdict` recomputes the verdict from findings.json on
        # disk, so leaving the stale finding in place would reject again.
        findings.write_findings_json(sup.run.findings_json_file, [])
        return _result(json.dumps({"verdict": "verified"}))

    sup.runner.run = _review_sees_the_ruling_and_verifies  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("done", "succeeded")
    assert len(prompts) == 1
    assert AMENDMENT_MARKER in prompts[0]

    # terminal verdict reached, same approach throughout, never archived
    assert sup._approach == 1
    final_status = sup.run.read_status()
    assert final_status["approach"] == 1
    assert not (root / "approaches" / "01").exists()


def test_resume_without_a_ruling_asks_review_again_and_parks_again(
        tmp_path, monkeypatch):
    """Same in-process resume routing, but with no ruling ever recorded --
    proving the terminal `succeeded` above is caused by the ruling reaching
    the second review call, not by anything else in the fixture (the same
    pairing `tests/test_resume_after_incurable_rejection.py` uses for the
    `rejected_incurable` edge)."""
    root = tmp_path / "run"
    sup = _supervisor_resume(root)
    _drive_the_resume_pause_edge(sup)

    cdir = tmp_path / "config"
    cdir.mkdir(parents=True)
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", cdir)

    sup.resume()
    assert sup.run.read_status()["approach"] == 1

    # the stale finding is still on disk -- nothing cured it -- so review
    # rejects the same finding again, on the SAME approach.
    calls = []

    async def _review_rejects_again(prompt, transcript, **kw):
        calls.append(prompt)
        assert AMENDMENT_MARKER not in prompt
        return _result(json.dumps({
            "verdict": "rejected", "reason": "still no operator ruling",
            "findings_path": "review-findings.md"}))

    sup.runner.run = _review_rejects_again  # type: ignore[method-assign]
    outcome = asyncio.run(sup._step_review())

    # v0.36 D.2: a bare unpause grants no one-shot proceed (`noRulingProceed`
    # is gone), so the same rejection parks again; only `answer` (or the
    # input timeout) leaves the park -- never `failed`, never a fresh
    # approach (covering node: tests/test_answer_outcomes.py).
    assert outcome == ("advance", "paused", "operator_input_needed")
    assert len(calls) == 1
    assert sup._approaches_exhausted_tail is False
    status = sup.run.read_status()
    assert status["approach"] == 1
    assert status["state"] == "awaiting_input"
    assert "noRulingProceed" not in status


# ===========================================================================
# 11. `worker`'s side of the SAME generalisation (task 012): stagnation on
#     the last approach pauses/escalates instead of ending `failed` silently.
# ===========================================================================


def test_the_worker_to_paused_stagnation_edge_is_retired_in_both_variants():
    """v0.36 requirement F.1: the stagnation guard replans on every
    approach, so `worker` has no `paused` successor for it any more."""
    for vigilant in (True, False):
        by_guard = {e.guard: e.target for e in
                   transitions.successors("worker", vigilant)}
        assert by_guard["stagnation_exhausted"] == "replan"
        assert "stagnation_last_approach" not in by_guard
        assert "paused" not in by_guard.values()


def test_the_paused_to_worker_resume_edge_is_declared_in_both_variants():
    # A 0.35 run directory can still hold a `stagnation` park; resuming it
    # re-enters `worker` over the same approach.
    for vigilant in (True, False):
        by_guard = {e.guard: e.target for e in
                   transitions.successors("paused", vigilant)}
        assert by_guard["paused_resumed_stagnation"] == "worker"
        # the pre-existing rejected_last_approach resume target is unchanged.
        assert by_guard["paused_resumed"] == "review"


def test_the_generated_mermaid_carries_the_replan_edge_not_the_retired_pause():
    import sys
    scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import render_transition_diagram as rtd
    rendered = rtd.render()
    assert "worker --> replan : stagnation_exhausted" in rendered
    assert "worker --> paused : stagnation_last_approach" not in rendered
    assert "paused --> worker : paused_resumed_stagnation" in rendered


def test_stagnant_last_approach_archives_and_holds_exhausted_instead_of_pausing(
        tmp_path):
    """v0.36 requirement F.1, last approach: the approach is archived, the
    replan has no approach left, and the run holds `exhausted` with
    `exhaustedBudget: approaches` -- no pause, no escalation."""
    root = tmp_path / "run"
    sup = _supervisor_worker(root, max_approaches=1)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    outcome = asyncio.run(sup._step_worker())

    assert outcome == ("advance", "replan", "stagnation_exhausted")
    assert sup._pause.is_set() is True       # never paused
    assert (root / "approaches" / "01").is_dir()   # archived
    assert notifier.messages == []
    assert _events(root, "operator_escalation") == []

    assert sup._step_replan() == ("tail",)
    assert sup._approaches_exhausted_tail is True
    assert sup._finish_tail_unverified() == "exhausted"
    status = sup.run.read_status()
    assert status.get("state") == "exhausted"
    assert status.get("exhaustedBudget") == "approaches"
    assert status.get("pauseReason") is None
    assert status.get("verdict") is None


def test_a_stagnant_approach_never_emits_an_escalation_with_no_notifier(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor_worker(root, max_approaches=1)
    sup._approach = 1
    assert sup.notifier is None

    outcome = asyncio.run(sup._step_worker())
    assert outcome == ("advance", "replan", "stagnation_exhausted")
    assert sup._pause.is_set() is True
    assert _events(root, "operator_escalation") == []


def _park_as_0_35_did(sup: LoopSupervisor, approach: int = 1) -> None:
    """What a 0.35 engine left on disk when its stagnation guard parked: an
    `operator_escalation` event with `reason: stagnation` and a persisted
    `paused` status. The 0.36 engine never writes this itself."""
    sup.run.emit("iteration.start", phase="worker", approach=approach)
    done = sup.run.root / "iterations" / "0001"
    done.mkdir(parents=True, exist_ok=True)
    (done / "meta.json").write_text(json.dumps({"endedAt": "2026-01-01T00:00:00Z"}))
    sup.run.emit("operator_escalation", approach=approach, reason="stagnation",
                 outstandingTasks=[{"id": TASK_ID, "status": "pending"}],
                 approachProgressed=False, awaitingValidation=[],
                 escalation="0.35 stagnation park")
    # What a 0.35 engine left on disk: the engine no longer pauses itself, so
    # the record is written the way that engine wrote it.
    sup._pause.clear()
    sup.run.update_status(state="paused", pauseReason="stagnation",
                          pauseDetail=None)


def test_in_process_resume_of_a_0_35_stagnation_park_re_enters_worker(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor_worker(root, max_approaches=1)
    sup._approach = 1
    _park_as_0_35_did(sup)

    async def _resume_flow():
        # `_step_paused` reads `pauseReason` BEFORE it awaits `_gate()`.
        task = asyncio.ensure_future(sup._step_paused())
        await asyncio.sleep(0)
        sup.resume()
        return await task

    outcome2 = asyncio.run(_resume_flow())
    assert outcome2 == ("advance", "worker", "paused_resumed_stagnation")
    status = sup.run.read_status()
    assert status.get("state") == "running"
    assert status.get("pauseReason") is None
    assert sup._approach == 1


def test_fresh_process_resume_detects_a_0_35_stagnation_park_and_re_enters_worker(
        tmp_path):
    root = tmp_path / "run"
    sup1 = _supervisor_worker(root, max_approaches=1)
    sup1._approach = 1
    _park_as_0_35_did(sup1)
    assert sup1._pause.is_set() is False

    # a brand-new process, no shared Python state at all -- reads
    # everything fresh off disk (`events.jsonl`, `status.json`).
    sup2 = _supervisor_worker(root, max_approaches=1)
    assert sup2._paused_awaiting_stagnation_ruling(1) is True
    # the OTHER predicate (rejected_last_approach) must not also fire.
    assert sup2._paused_awaiting_operator_input_needed_ruling(1) is False

    start_approach, skip_planning, start_step = sup2._resume_point()
    assert start_approach == 1
    assert skip_planning is False
    assert start_step == "worker"


def test_paused_awaiting_stagnation_ruling_is_false_once_a_new_iteration_started(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor_worker(root, max_approaches=1)
    sup._approach = 1
    _park_as_0_35_did(sup)
    assert sup._paused_awaiting_stagnation_ruling(1) is True

    sup.run.emit("iteration.start", phase="worker", approach=1)
    assert sup._paused_awaiting_stagnation_ruling(1) is False


def test_a_stagnant_approach_with_approaches_left_archives_and_replans(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor_worker(root, max_approaches=2)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    outcome = asyncio.run(sup._step_worker())

    assert outcome == ("advance", "replan", "stagnation_exhausted")
    # the approach WAS archived and the run was never paused or escalated.
    assert (root / "approaches" / "01").is_dir()
    assert sup._pause.is_set() is True
    assert notifier.messages == []
    assert _events(root, "operator_escalation") == []
    status = sup.run.read_status()
    assert status.get("state") != "paused"
    assert status.get("pauseReason") is None
    # and the replan opens approach 2 (decision 5), uncharged by nothing else.
    assert sup._step_replan() == ("advance", "planning", "budget_and_approach_left")
    assert sup._approach == 2


# ===========================================================================
# 11b. Task 018 (PRD requirement B.3, #156): a PROGRESSING approach --
#      neither the last, nor genuinely silent -- pauses too, not just the
#      last approach.
# ===========================================================================


def _git(cwd: Path, *args: str) -> None:
    import subprocess
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_git_workspace(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "t@example.com")
    _git(workspace, "config", "user.name", "t")
    (workspace / "seed.txt").write_text("seed")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "seed")


class _CommitsThenGoesQuietRunner:
    """The FIRST call commits a real file into `workspace` (the approach
    gains a commit); every call after that leaves the tree untouched and
    reports plain text with no structured claim -- so the per-iteration
    `commits_gained` witness (task 010) is true ONLY on call 1, and the
    three-strike counter still trips on calls 2-4, exactly as it would for a
    genuinely silent approach. The only difference from silence is that
    THIS approach's own HEAD, read once before call 1, no longer equals the
    HEAD read at the stagnation-exhaustion decision point."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.calls = 0

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.calls += 1
        if self.calls == 1:
            (self.workspace / "progress.txt").write_text("real work")
            _git(self.workspace, "add", "-A")
            _git(self.workspace, "commit", "-q", "-m", "real progress")
        return _result(f"iteration {self.calls}: nothing further to report")


def _progressing_approach_supervisor(tmp_path: Path, **cfg_kw) -> LoopSupervisor:
    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)
    root = tmp_path / "run"
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [_task()]})
    kw = {"run_id": "task018-progressing-approach", "vigilant": False,
          "on_complete": "exit", "iterations": 20,
          "infra_retry_backoff_s": [0.0], "infra_retry_backoff_max_s": 0.0,
          "infra_outage_budget_s": 1000.0, "max_approaches": 3, **cfg_kw}
    cfg = loop_mod.JobConfig(**kw)
    sup = LoopSupervisor(cfg, run, workspace)
    sup._approach = 1
    sup.runner = _CommitsThenGoesQuietRunner(workspace)  # type: ignore[assignment]
    return sup


def test_a_progressing_approach_1_of_3_with_commits_present_archives_and_replans(
        tmp_path):
    sup = _progressing_approach_supervisor(tmp_path)
    notifier = _StubNotifier()
    sup.notifier = notifier

    outcome = asyncio.run(sup._step_worker())

    # replanned, not paused -- stagnation self-heals on a progressing
    # approach too (v0.36 requirement F.1); the 0.35 park is gone.
    assert outcome == ("advance", "replan", "stagnation_exhausted")
    status = sup.run.read_status()
    assert status.get("state") != "paused"
    assert status.get("pauseReason") is None
    assert (sup.run.root / "approaches" / "01").is_dir()
    assert _events(sup.run.root, "operator_escalation") == []
    assert notifier.messages == []
    # the workspace tree (the commits the approach made) is kept.
    assert sup._workspace_head_sha() is not None
    # the replan opens approach 2 of 3 over the same tree.
    assert sup._step_replan() == ("advance", "planning", "budget_and_approach_left")
    assert sup._approach == 2


def test_a_superseded_task_present_replans_a_stagnant_non_last_approach(
        tmp_path):
    """The PRD's OTHER B.3 witness (#156): "a superseded task's children
    exhaust their budgets too" -- no git commit anywhere, but a forced
    split (task 016) already happened THIS approach. v0.36 requirement
    F.1: that approach is archived and replanned like any other."""
    root = tmp_path / "run"
    sup = _supervisor_worker(
        root, max_approaches=3,
        tasks=[
            {"id": "parent", "title": "the split parent", "status": "superseded",
             "successCriteria": "n/a"},
            _task(id="child"),
        ])
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    outcome = asyncio.run(sup._step_worker())

    assert outcome == ("advance", "replan", "stagnation_exhausted")
    status = sup.run.read_status()
    assert status.get("state") != "paused"
    assert status.get("pauseReason") is None
    assert (root / "approaches" / "01").is_dir()
    assert _events(root, "operator_escalation") == []
    assert notifier.messages == []


# ===========================================================================
# 11c. Task 018 (PRD requirement B.3, #156): the SECOND exhaustion -- a
#      `superseded` parent's own split child spending ITS budget pauses
#      instead of forcing yet another split.
# ===========================================================================


class _SplitProposingRunner:
    """Every dispatch returns a well-formed forced-split report. If the
    engine dispatches at all when it should have paused, the split lands and
    the assertions below see the grandchild -- so `calls == 0` is a real
    observation, not an absence of evidence."""

    def __init__(self, child_ids=("018c-a", "018c-b")) -> None:
        self.calls = 0
        self.child_ids = list(child_ids)

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw):
        self.calls += 1
        return _result(json.dumps({
            "verdict": "no_verdict",
            "proposed_tasks": [
                {"id": cid, "title": f"part {cid}",
                 "successCriteria": f"{cid} works on its own end to end"}
                for cid in self.child_ids],
        }))


_PARENT = {"id": "018p", "title": "the split parent",
           "status": "superseded", "successCriteria": "the whole thing works"}


def _second_exhaustion_supervisor(tmp_path: Path, parent_status="superseded",
                                  **cfg) -> LoopSupervisor:
    """A plan in exactly the shape a forced split leaves behind: parent
    `superseded` (task 016's own write), one child stamped `splitFrom` the
    parent, and `progressBudget` on status.json spent against THAT CHILD."""
    child = {"id": "018c", "title": "the split child", "status": "pending",
             "successCriteria": "the child's own slice works",
             "origin": "worker", "splitFrom": _PARENT["id"]}
    return make_supervisor(
        tmp_path / "run", run_id="task018-second-exhaustion",
        cfg_defaults=False, prd="# task 018 fixture PRD\n\nDo the thing.\n",
        vigilant=False, iterations=4,
        contract_retry_max=1,
        tasks=[{**_PARENT, "status": parent_status}, child],
        status={"state": "running",
                "progressBudget": {"taskId": "018c", "used": 3, "budget": 3}},
        runner_factory=lambda sup: _SplitProposingRunner(),
        **{"max_approaches": 3, **cfg})


def test_a_superseded_parents_child_exhausting_its_budget_archives_and_replans(
        tmp_path):
    sup = _second_exhaustion_supervisor(tmp_path)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    outcome = asyncio.run(sup._step_worker())

    # approach 1 of 3 is archived and replanned -- never another forced
    # split, never a park.
    assert outcome == ("advance", "replan", "stagnation_exhausted")
    assert sup.runner.calls == 0, "a forced-split iteration was dispatched"
    status = sup.run.read_status()
    assert status.get("state") != PAUSED_STATE
    assert status.get("pauseReason") is None
    assert (sup.run.root / "approaches" / "01").is_dir()

    # no recursion: no grandchild inserted, the child not superseded.
    tasks = {t["id"]: t for t in sup.run.read_tasks()["tasks"]}
    assert set(tasks) == {"018p", "018c"}
    assert tasks["018c"]["status"] != "superseded"

    assert _events(sup.run.root, "operator_escalation") == []
    assert notifier.messages == []
    assert sup._step_replan() == ("advance", "planning", "budget_and_approach_left")
    assert sup._approach == 2


def test_a_second_exhaustion_with_no_approach_left_holds_exhausted(tmp_path):
    """v0.36 requirement F.1: the exhausted-split-child case on the LAST
    approach has nowhere to replan to, so the run holds `exhausted` on
    `approaches` instead of parking."""
    sup = _second_exhaustion_supervisor(tmp_path, max_approaches=1)
    sup._approach = 1

    assert asyncio.run(sup._step_worker()) == (
        "advance", "replan", "stagnation_exhausted")
    assert sup._step_replan() == ("tail",)
    assert sup._finish_tail_unverified() == "exhausted"
    status = sup.run.read_status()
    assert status.get("state") == "exhausted"
    assert status.get("exhaustedBudget") == "approaches"
    assert sup.runner.calls == 0


def test_a_child_of_a_still_live_parent_still_forces_its_first_split(tmp_path):
    """The negative: `splitFrom` alone is not the second exhaustion. A
    voluntary `proposed_tasks` split leaves its parent schedulable, so its
    child's FIRST budget exhaustion must still force a split, unchanged."""
    sup = _second_exhaustion_supervisor(tmp_path, parent_status="completed")
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    asyncio.run(sup._step_worker())

    assert sup.runner.calls >= 1
    status = sup.run.read_status()
    assert status.get("state") != PAUSED_STATE
    assert status.get("pauseReason") is None
    assert _events(sup.run.root, "operator_escalation") == []
    assert notifier.messages == []
    tasks = {t["id"]: t for t in sup.run.read_tasks()["tasks"]}
    assert "018c-a" in tasks and tasks["018c"]["status"] == "superseded"


# ===========================================================================
# 11d. Task 020 (#156): the three stages chained for real, in ONE continuous
#      `_step_worker()` call, instead of each pre-seeded on its own -- 016's
#      forced split, 018's second-exhaustion pause, and the schedulable
#      picked child in between, all driven from a fresh budget of zero.
# ===========================================================================

FLOW_PARENT = "020p"
FLOW_CHILD = "020c"


class _FlowStepRunner:
    """Same shape as `tests/test_progress_pin_task015.py`'s own
    `_ScriptedWorker`: every dispatch calls `step(sup, call_index)`, which
    performs whatever real workspace side effect this pass's own claim
    needs and returns the reply text."""

    def __init__(self, sup: LoopSupervisor, step) -> None:
        self.sup = sup
        self.step = step
        self.calls = 0

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw):
        text = self.step(self.sup, self.calls)
        self.calls += 1
        return _result(text)


# Task 020's own supervisor -- `workspace="ws"` deliberately separates the
# fingerprinted tree from the run dir's own tasks.json/events.jsonl/
# status.json writes (`tests/test_progress_pin_task015.py`'s own
# `_worker_sup` docstring: without the split, every pass's harness-owned
# bookkeeping write would itself look like a real tree delta and every
# claim below would be corroborated for the wrong reason).
_supervisor_split_flow = supervisor_with_runner(
    lambda sup, step: _FlowStepRunner(sup, step),
    run_id="task020-split-flow", cfg_defaults=False, workspace="ws",
    prd="# task 020 fixture PRD\n\nDo the thing.\n",
    status={"state": "running"}, max_approaches=3, vigilant=False,
    iterations=12)


def _flow_step(sup, i):
    if i < 3:
        (sup.workspace / f"p{i}.txt").write_text(f"parent progress {i}")
        return json.dumps({"verdict": "no_verdict", "progress": {
            "summary": f"still working on {FLOW_PARENT}, pass {i}",
            "evidence": [{"kind": "file", "ref": f"p{i}.txt"}]}})
    if i == 3:
        return json.dumps({
            "verdict": "no_verdict",
            "proposed_tasks": [
                {"id": FLOW_CHILD, "title": "the split child",
                 "successCriteria": f"{FLOW_CHILD} works on its own end to end"}]})
    j = i - 4
    (sup.workspace / f"c{j}.txt").write_text(f"child progress {j}")
    return json.dumps({"verdict": "no_verdict", "progress": {
        "summary": f"still working on {FLOW_CHILD}, pass {j}",
        "evidence": [{"kind": "file", "ref": f"c{j}.txt"}]}})


def test_budget_exhaustion_drives_a_forced_split_a_superseded_parent_a_picked_child_and_a_second_stall_replan(
        tmp_path):
    """Three corroborated `file`-evidence progress claims spend
    `020p`'s own default budget (`estimatedIterations` absent, 1+2==3) to
    3/3; the fourth dispatch carries the \"Progress budget exhausted\"
    notice and reports a `proposed_tasks` split -- accepted, `020p`
    becomes `superseded` and `020c` is inserted, worker-origin,
    `splitFrom: \"020p\"`; the scheduler picks `020c` next with a FRESH
    budget (the stale record still names `020p`, so `_record_progress_
    budget_usage` resets rather than carries the parent's spent 3/3
    forward); three more corroborated claims spend `020c`'s own budget to
    3/3; the eighth pass -- `020c`'s own `splitFrom` parent already
    `superseded` -- is the SECOND exhaustion (task 018, B.3): the run
    archives and replans BEFORE dispatching a doomed second split, never
    recursing into a grandchild."""
    root = tmp_path / "run"
    sup = _supervisor_split_flow(
        root, _flow_step,
        tasks=[_task(id=FLOW_PARENT, status="in-progress")])
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    outcome = asyncio.run(sup._step_worker())

    assert outcome == ("advance", "replan", "stagnation_exhausted")
    assert sup.runner.calls == 7, (
        f"flow: expected 7 dispatched iterations (3 on the parent, the "
        f"split itself, 3 on the child), got {sup.runner.calls}")

    tasks = {t["id"]: t for t in sup.run.read_tasks()["tasks"]}
    assert set(tasks) == {FLOW_PARENT, FLOW_CHILD}
    assert tasks[FLOW_PARENT]["status"] == "superseded"
    assert tasks[FLOW_CHILD]["status"] == "in-progress"
    assert tasks[FLOW_CHILD]["splitFrom"] == FLOW_PARENT
    assert state_mod.is_worker_origin(tasks[FLOW_CHILD])
    # nothing PENDING left to pick: the only actionable candidate is the
    # child's own resumed `in-progress` claim -- its superseded parent is
    # not a candidate `select_next_task` ever returns again.
    picked = scheduler.select_next_task(list(tasks.values()))
    assert picked is not None and picked["id"] == FLOW_CHILD

    status = sup.run.read_status()
    assert status.get("state") != PAUSED_STATE
    assert status.get("pauseReason") is None
    assert (root / "approaches" / "01").is_dir()
    budget_record = status.get("progressBudget")
    assert budget_record == {
        "taskId": FLOW_CHILD, "used": 3, "budget": 3}, budget_record

    assert _events(root, "operator_escalation") == []
    logged = [e for e in
              (json.loads(x) for x in
               (root / "events.jsonl").read_text().splitlines() if x.strip())
              if e.get("exhaustedSplitChild")]
    assert [e["exhaustedSplitChild"] for e in logged] == [FLOW_CHILD]

    claims = [e for e in
              (json.loads(x) for x in
               (root / "events.jsonl").read_text().splitlines() if x.strip())
              if e.get("type") == "progress_claim"]
    assert len(claims) == 6
    assert all(c["corroborated"] for c in claims), claims
    assert [c["taskId"] for c in claims] == (
        [FLOW_PARENT] * 3 + [FLOW_CHILD] * 3)

    assert notifier.messages == []


# ===========================================================================
# 12. The escalation reaching the CONFIGURED notifier (task 007).
# ===========================================================================


def _trigger_notifier_wiring_pause(sup: LoopSupervisor):
    sup._approach = 1
    sup.cfg.cure_pass_ceiling = 1  # v0.36 C.2: the approach's one round is spent
    sup.run.update_status(curePasses={"1": 1})
    still_curable = [_finding("blocking", True, "requirement D.1 (#142)",
                              "the notifier is still not wired in")]
    findings.write_findings_json(sup.run.findings_json_file, still_curable)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]
    return asyncio.run(sup._step_review())


def test_notifier_url_set_reaches_send_notification_and_is_recorded(
        tmp_path, monkeypatch):
    calls = []

    def stub_poster(url, text, timeout_s):
        calls.append((url, text, timeout_s))
        return 200, None

    monkeypatch.setattr(hat_tools, "_http_post_text", stub_poster)

    root = tmp_path / "run"
    sup = _supervisor_review(root, max_approaches=1,
                             notifier_url="http://notifier.example/notify")
    assert sup.notifier is not None

    outcome = _trigger_notifier_wiring_pause(sup)
    assert outcome == ("advance", "paused", "operator_input_needed")

    # reached through B.3's own helper, not a second HTTP client.
    assert len(calls) == 1
    posted_url, posted_text, _ = calls[0]
    assert posted_url == "http://notifier.example/notify"
    expected_sha = hashlib.sha256(posted_text.encode()).hexdigest()

    events = _events(root, hat_tools.NOTIFICATION_SENT_EVENT)
    assert len(events) == 1
    assert events[0]["textSha256"] == expected_sha
    assert events[0]["endpoint"] == "http://notifier.example/notify"
    assert events[0]["httpStatus"] == 200

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "operator_input_needed"

    section = run_record.build_notifications_section(sup.run)
    assert len(section) == 1
    assert section[0]["textSha256"] == expected_sha


def test_notifier_url_unset_still_escalates_but_writes_no_event(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor_review(root, max_approaches=1)
    assert sup.cfg.notifier_url is None
    assert sup.notifier is None

    outcome = _trigger_notifier_wiring_pause(sup)
    assert outcome == ("advance", "paused", "operator_input_needed")
    assert sup._pause.is_set() is False  # still pauses

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "operator_input_needed"

    events = _events(root, hat_tools.NOTIFICATION_SENT_EVENT)
    assert events == []
    assert run_record.build_notifications_section(sup.run) == []


def test_a_raising_poster_still_leaves_the_pause_outcome_unchanged(
        tmp_path, monkeypatch):
    def raising_poster(url, text, timeout_s):
        raise RuntimeError("boom -- unexpected poster failure")

    monkeypatch.setattr(hat_tools, "_http_post_text", raising_poster)

    root = tmp_path / "run"
    sup = _supervisor_review(root, max_approaches=1,
                             notifier_url="http://notifier.example/notify")

    outcome = _trigger_notifier_wiring_pause(sup)

    assert outcome == ("advance", "paused", "operator_input_needed")
    assert sup._pause.is_set() is False

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "operator_input_needed"

    status = sup.run.read_status()
    assert status.get("state") != "succeeded"
    assert status.get("state") != "failed"


def test_a_non_2xx_status_still_leaves_the_pause_outcome_unchanged(
        tmp_path, monkeypatch):
    def failing_poster(url, text, timeout_s):
        return 500, None

    monkeypatch.setattr(hat_tools, "_http_post_text", failing_poster)

    root = tmp_path / "run"
    sup = _supervisor_review(root, max_approaches=1,
                             notifier_url="http://notifier.example/notify")

    outcome = _trigger_notifier_wiring_pause(sup)

    assert outcome == ("advance", "paused", "operator_input_needed")
    assert sup._pause.is_set() is False

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "operator_input_needed"

    # the failed send is still recorded, distinguishably, on
    # events.jsonl -- a non-2xx is a recorded outcome, never a reason to
    # silently lose the escalation. Task 009 (requirement B.1) means this
    # is the distinct `notification.failed` event, never `notification.
    # sent` -- the send never actually succeeded.
    #
    # Task 004 (PRD requirement A.4, #269) rewrites this pin's own COUNT:
    # a permanently-failing notifier is now retried up to 3 times with
    # bounded backoff (`LoopSupervisor._notify_escalation_delivery`) before
    # the run gives up on this escalation, so 3 attempts each honestly
    # record their own `notification.failed` event (#197's sent/failed
    # split is unchanged PER ATTEMPT) -- never 1 -- and the exhausted
    # retries additionally leave exactly one `notification.
    # escalation_undelivered` event, covered in full by `tests/
    # test_escalation_delivery.py::test_always_failing_notifier_is_
    # recorded_undelivered_after_three_attempts`.
    events = _events(root, run_record.NOTIFICATION_FAILED_EVENT)
    assert len(events) == 3
    assert all(e["httpStatus"] == 500 for e in events)
    assert _events(root, hat_tools.NOTIFICATION_SENT_EVENT) == []
    undelivered = _events(root, run_record.NOTIFICATION_ESCALATION_UNDELIVERED_EVENT)
    assert len(undelivered) == 1
    assert undelivered[0]["reason"] == "operator_input_needed"
