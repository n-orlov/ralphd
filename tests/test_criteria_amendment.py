"""Task 006 (requirement B.1, #114): a judge-authored criteria amendment on
a CONFIRMED petition whose subject is the affected task's own
`successCriteria` (types `contradictory-clauses` or `self-referential`).

Drives the REAL production path -- `LoopSupervisor._step_worker`, which
calls `_maybe_run_midrun_adjudication_episode` (task 005 PART 2), which
calls `_review_verdict` (the SAME method an ordinary run-ending review
settles through), which now also calls `LoopSupervisor.
_apply_criteria_amendments` -- via a phase-dispatching scripted agent, the
same shape `tests/test_midrun_adjudication_episode.py`'s own
`_ScriptedAgent` uses. No test-local `_extra_validate`-style shim, no
monkeypatch of the behaviour under test: the review payload's own
`petition_adjudications` field, with a judge-authored `task_id` +
`replacement_criteria` pair, is what triggers every effect pinned below.

Four effects, pinned separately, plus one negative control:

1. a `task.criteria_amended` event recording the old text, the new text
   and the authorizing (CONFIRMED) adjudication;
2. `criteriaFingerprint` recomputed with `LoopSupervisor.
   _criteria_fingerprint` over the NEW text;
3. the task's `validationAttempts` counter reset to 0 for the re-scoped
   criteria;
4. `successCriteria` itself replaced with the judge's text;
5. (negative control) every OTHER field on the task record -- `id`,
   `title`, `status`, `dependsOn`, `tier` -- is untouched by the
   amendment.

Effect 5 is pinned at the moment the amendment is applied, not at the end
of `_step_worker`: the snapshot is taken from inside the VERIFY hat's own
turn, which the production path enters strictly after
`_apply_criteria_amendments` has already written `tasks.json` and strictly
before `_verify_task` records any verdict. Reading the record there is an
observation of real production state at a real production instant (the
scripted agent is the test's stand-in for the hat's model, never for the
behaviour under test) -- an attempt-1 gap the validator demonstrated by
mutating `task["status"]` inside `_apply_criteria_amendments` and watching
the end-of-call assertion still pass, because the ordinary verify pass
overwrites `status` anyway.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import petitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

OLD_CRITERIA = (
    "This task's own successCriteria can only be verified using a state "
    "its own satisfaction would have to create first -- self-referential "
    "and unmeetable as written.")
NEW_CRITERIA = (
    "This task ships a fresh, achievable criterion that no longer refers "
    "to its own not-yet-existing state.")
OBLIGATION = OLD_CRITERIA  # the petition's subject IS the task's own text


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _make_petitions_file(workspace: Path, obligation: str, ptype: str,
                         wave: str = "v0.16") -> Path:
    reports = workspace / "artifacts" / "reports"
    reports.mkdir(parents=True)
    path = reports / f"{wave}-petitions.md"
    path.write_text(
        f"# {wave} petitions\n\n"
        f"## Petition: task 1's own criteria\n\n"
        f"**Obligation**: {obligation}\n"
        f"**Type**: {ptype}\n"
        "**Evidence**: the criterion's own text names a state its "
        "satisfaction would have to create\n")
    return path


class _ScriptedAgent:
    """Phase-dispatching scripted agent (same shape
    `tests/test_midrun_adjudication_episode.py`'s own `_ScriptedAgent`
    uses): one script per phase, keyed off the CURRENT `status.json`
    phase, last step of each repeating forever."""

    def __init__(self, handlers: dict):
        self.handlers = handlers
        self.calls: dict[str, int] = {}
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        phase = self.sup.run.read_status().get("phase")
        n = self.calls.get(phase, 0)
        self.calls[phase] = n + 1
        steps = self.handlers[phase]
        self.running = True
        try:
            return steps[min(n, len(steps) - 1)](self.sup, prompt)
        finally:
            self.running = False


def _task_record(**overrides) -> dict:
    base = {
        "id": "1", "title": "a task whose own criteria are unmeetable",
        "status": "awaiting-validation", "successCriteria": OLD_CRITERIA,
        "validationAttempts": 2, "dependsOn": [], "tier": "hard",
    }
    base.update(overrides)
    return base


def _worker_supervisor(tmp_path: Path, task: dict) -> tuple[LoopSupervisor, RunDir, Path]:
    root = tmp_path / "run"
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [task]})
    cfg = JobConfig(
        run_id="task006-criteria-amendment", iterations=10, max_approaches=3,
        vigilant=True, on_complete="exit", infra_retry_backoff_s=[0.0],
        infra_retry_backoff_max_s=0.0, infra_outage_budget_s=1000.0,
        model_overrides={"worker": "prov/worker-model-x",
                         "review": "prov/review-model-y"})
    sup = LoopSupervisor(cfg, run, root)
    sup._approach = 1
    return sup, run, root


def _confirming_amendment_review(sup, prompt=""):
    return _ok("adjudicating and re-scoping the task\n" + json.dumps({
        "verdict": "rejected",
        "petition_adjudications": [{
            "obligation": OBLIGATION,
            "adjudication": "CONFIRMED",
            "task_id": "1",
            "replacement_criteria": NEW_CRITERIA,
        }],
    }))


def _verified(sup, prompt=""):
    return _ok(json.dumps({"task_id": "1", "verdict": "verified"}))


def _verified_snapshotting(store: list):
    """The verify hat's own turn, which the production `_step_worker`
    reaches strictly AFTER `_apply_criteria_amendments` wrote `tasks.json`
    and strictly BEFORE `_verify_task` applies any verdict: record what
    the engine's own amendment left on disk at that instant."""

    def _handler(sup, prompt=""):
        store.append(sup.run.read_tasks()["tasks"][0])
        return _verified(sup, prompt)

    return _handler


def _run_amendment_episode(
        tmp_path: Path, task: dict,
        snapshots: list | None = None) -> tuple[LoopSupervisor, RunDir]:
    sup, run, workspace = _worker_supervisor(tmp_path, task)
    _make_petitions_file(workspace, OBLIGATION, "self-referential")

    verify = _verified if snapshots is None else _verified_snapshotting(snapshots)
    agent = _ScriptedAgent({
        "worker": [lambda s, p: _ok(json.dumps({"verdict": "no_verdict"}))],
        "review": [_confirming_amendment_review],
        "verify": [verify],
    })
    agent.sup = sup  # type: ignore[attr-defined]
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())
    return sup, run


def _events(run: RunDir) -> list[dict]:
    return [json.loads(x) for x in (run.root / "events.jsonl").read_text().splitlines()
           if x.strip()]


def test_criteria_amended_event_records_old_new_and_adjudication(tmp_path):
    old_fp = LoopSupervisor._criteria_fingerprint(OLD_CRITERIA)
    task = _task_record(criteriaFingerprint=old_fp)
    sup, run = _run_amendment_episode(tmp_path, task)

    amended = [e for e in _events(run) if e.get("type") == "task.criteria_amended"]
    assert len(amended) == 1
    ev = amended[0]
    assert ev.get("taskId") == "1"
    assert ev.get("oldCriteria") == OLD_CRITERIA
    assert ev.get("newCriteria") == NEW_CRITERIA
    adjudication = ev.get("adjudication")
    assert isinstance(adjudication, dict)
    assert adjudication.get("adjudication") == "CONFIRMED"
    assert adjudication.get("obligation") == OBLIGATION
    assert adjudication.get("task_id") == "1"
    assert adjudication.get("replacement_criteria") == NEW_CRITERIA


def test_criteria_fingerprint_recomputed_with_criteria_fingerprint(tmp_path):
    old_fp = LoopSupervisor._criteria_fingerprint(OLD_CRITERIA)
    task = _task_record(criteriaFingerprint=old_fp)
    sup, run = _run_amendment_episode(tmp_path, task)

    on_disk = run.read_tasks()["tasks"][0]
    assert on_disk["criteriaFingerprint"] == LoopSupervisor._criteria_fingerprint(NEW_CRITERIA)
    assert on_disk["criteriaFingerprint"] != old_fp


def test_validation_attempt_counter_reset_for_the_rescoped_criteria(tmp_path):
    task = _task_record(validationAttempts=2)
    sup, run = _run_amendment_episode(tmp_path, task)

    # The episode itself resets validationAttempts to 0 the moment the
    # amendment is applied -- read straight after the mid-run episode, but
    # since the same _step_worker call also runs verify (which passed and
    # left validationAttempts alone), the on-disk 0 survives to the end.
    on_disk = run.read_tasks()["tasks"][0]
    assert on_disk["validationAttempts"] == 0


def test_success_criteria_replaced_with_the_judges_text(tmp_path):
    task = _task_record()
    sup, run = _run_amendment_episode(tmp_path, task)

    on_disk = run.read_tasks()["tasks"][0]
    assert on_disk["successCriteria"] == NEW_CRITERIA
    assert on_disk["successCriteria"] != OLD_CRITERIA


def test_status_is_not_touched_by_the_amendment_itself(tmp_path):
    """Pinned separately (attempt-1 validator's point): the amendment must
    not write `status` AT ALL. Observed at the production instant after
    `_apply_criteria_amendments` and before `_verify_task`, so the verify
    pass's own legitimate status move cannot mask an amendment-side one."""
    task = _task_record(status="awaiting-validation")
    snapshots: list = []
    sup, run = _run_amendment_episode(tmp_path, task, snapshots)

    assert len(snapshots) == 1, "the verify hat's turn never ran"
    amended = snapshots[0]
    # The amendment demonstrably happened by this instant...
    assert amended["successCriteria"] == NEW_CRITERIA
    assert amended["validationAttempts"] == 0
    # ...and it left the status exactly as the engine's own status machine
    # had it before the adjudication.
    assert amended["status"] == "awaiting-validation"


def test_other_task_fields_are_otherwise_untouched(tmp_path):
    task = _task_record(dependsOn=["0"], tier="hard")
    before = dict(task)
    snapshots: list = []
    sup, run = _run_amendment_episode(tmp_path, task, snapshots)

    # The record as the engine's OWN amendment left it, read at the
    # production instant between `_apply_criteria_amendments` and
    # `_verify_task` (the verify hat's turn) -- so an amendment-side
    # mutation of any other field, `status` included, is caught here even
    # though the verify pass that follows would overwrite `status` anyway.
    assert len(snapshots) == 1, "the verify hat's turn never ran"
    amended = snapshots[0]
    assert amended["successCriteria"] == NEW_CRITERIA, "amendment not applied yet"
    assert amended["status"] == before["status"] == "awaiting-validation"
    assert amended["id"] == "1"
    assert amended["title"] == "a task whose own criteria are unmeetable"
    assert amended["dependsOn"] == ["0"]
    assert amended["tier"] == "hard"
    # Exhaustive: the ONLY keys the amendment may differ on are the four
    # it is authorized to write. Every other key -- present, absent, or
    # newly invented -- must match the pre-amendment record exactly.
    # `criteriaOfRecord` joined that set in task 024 (requirement E.3,
    # #116 section 2): the engine now keeps the authoritative criteria text
    # verbatim beside its hash so an UNauthorized rewrite can be restored,
    # and a legitimate judge-authored amendment must move that copy forward
    # in the same atomic step as the hash -- otherwise the next
    # `_check_criteria_edits` pass would revert the amendment itself.
    amendable = {"successCriteria", "criteriaFingerprint",
                 "validationAttempts", "criteriaOfRecord"}
    assert set(amended) - amendable == set(before) - amendable
    for key in set(before) - amendable:
        assert amended[key] == before[key], key

    on_disk = run.read_tasks()["tasks"][0]
    assert on_disk["id"] == "1"
    assert on_disk["title"] == "a task whose own criteria are unmeetable"
    assert on_disk["dependsOn"] == ["0"]
    assert on_disk["tier"] == "hard"
    # The task genuinely reached verified afterwards -- the amendment did
    # not itself force a status; the ordinary verify pass that followed
    # (in the SAME _step_worker call) is what moved it there.
    assert on_disk["status"] == "validated"


def test_a_petition_about_a_prd_obligation_is_never_amended(tmp_path):
    """Negative control: a CONFIRMED petition whose obligation is NOT the
    task's own successCriteria text (a PRD requirement, in this case)
    carries no `task_id`/`replacement_criteria` at all in a real judge's
    payload, and the engine leaves the task's criteria untouched even if
    review's payload matched a real task id under a DIFFERENT
    obligation."""
    task = _task_record()
    sup, run, workspace = _worker_supervisor(tmp_path, task)
    prd_obligation = "Requirement Z: an obligation about the PRD, not task 1"
    _make_petitions_file(workspace, prd_obligation, "contradictory-clauses")

    def _prd_review(s, p):
        return _ok(json.dumps({
            "verdict": "rejected",
            "petition_adjudications": [{
                "obligation": prd_obligation,
                "adjudication": "CONFIRMED",
            }],
        }))

    agent = _ScriptedAgent({
        "worker": [lambda s, p: _ok(json.dumps({"verdict": "no_verdict"}))],
        "review": [_prd_review],
        "verify": [_verified],
    })
    agent.sup = sup  # type: ignore[attr-defined]
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    on_disk = run.read_tasks()["tasks"][0]
    assert on_disk["successCriteria"] == OLD_CRITERIA
    assert not any(e.get("type") == "task.criteria_amended" for e in _events(run))
    # But the obligation still converted to a disclosed residual at run
    # scope, exactly as B.3 says -- no text rewritten anywhere.
    confirmed, tampered = petitions.confirmed_obligations(sup.run)
    assert not tampered
    assert any(prd_obligation.lower() in c for c in confirmed)
