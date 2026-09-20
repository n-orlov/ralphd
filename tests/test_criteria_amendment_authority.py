"""Task 008 (requirement B.2, #114): the criteria-amendment AUTHORITY
BOUNDARY -- only a judge-authored amendment on a CONFIRMED petition,
routed through the existing petition ladder (`petitions.
criteria_amendment_candidates` / `LoopSupervisor._apply_criteria_
amendments`, task 006), may ever rewrite a task's own `successCriteria`.

Every test here drives a REAL `LoopSupervisor` against a real `RunDir`
(constructed exactly like `tests/test_criteria_amendment.py`'s own
`_worker_supervisor`/`_ScriptedAgent`, `tests/test_rejected_incurable_
edge.py`'s own `_step_review()`-driving `_supervisor` helper, and
`tests/test_review_criteria_reverification.py`'s direct call onto
`_flagged_criteria_review_context`) through the SAME production entry
points those already-validated modules use: `LoopSupervisor._step_worker`,
`LoopSupervisor._step_review`, and the review-context/candidate-decision
methods those two dispatch through (`_check_criteria_edits`,
`_flagged_criteria_review_context`, `_review_verdict` ->
`_apply_criteria_amendments` -> `petitions.criteria_amendment_candidates`
/`petitions.criteria_amendment_refusals`). No test-local `_extra_validate`
shim, no monkeypatch of any of those methods.

Four boundaries, pinned separately:

1. A worker/planner-authored `successCriteria` rewrite (bypassing the
   petition ladder entirely) is detected by the production
   `_check_criteria_edits` call `_step_worker` already makes on every
   pass, and then forces a MANDATORY `severity: "blocking"` finding
   instruction into the REAL review prompt `_step_review` ->
   `_run_review_iteration` builds via `_review_extra_context()` ->
   `_flagged_criteria_review_context()` -- the SAME tamper-evidence
   pattern `petitions.tamper_review_context` already uses for a tampered
   adjudication record (task 011, requirement C). Honouring that mandate
   (a `rejected` verdict) is what "refused" means here: the review edge
   taken is a rejection, never `verified`.
2. A judge amendment whose `task_id` names a DIFFERENT real task (not the
   one whose own `successCriteria` text the petition's obligation is
   actually about) is silently refused: `criteria_amendment_candidates`'s
   own fuzzy-match gate (item 5 of its docstring) never applies a
   replacement to a task whose current criteria don't match the
   obligation, however real that task's id is.
3. A judge amendment whose `task_id` names something that is NOT a real
   task at all in the CURRENT plan -- the shape an attempted amendment of
   the PRD text, a Definition-of-done item, or any path under `/config`
   would take, since none of those is ever a key in the task table -- is
   refused by `petitions.criteria_amendment_refusals` (task 008's own new
   function), which names the OFFENDING TARGET verbatim in a
   `task.criteria_amendment_refused` event `_apply_criteria_amendments`
   emits for every such entry.
4. A genuinely VALID amendment (real task, real CONFIRMED+correctly-typed
   petition, matching obligation) is recorded in a form citable
   afterwards: the `task.criteria_amended` event in this run's own
   `events.jsonl`, carrying the old text, the new text, and a full COPY of
   the authorizing adjudication entry -- exactly the shape
   `tests/test_criteria_amendment.py::
   test_criteria_amended_event_records_old_new_and_adjudication` already
   pins for task 006; this module re-derives it here as the FOURTH leg of
   the authority boundary, not merely restates it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine import findings
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import make_supervisor


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _events(run: RunDir) -> list[dict]:
    path = run.root / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _make_petitions_file(workspace: Path, obligation: str, ptype: str,
                          wave: str = "v0.16") -> Path:
    reports = workspace / "artifacts" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"{wave}-petitions.md"
    path.write_text(
        f"# {wave} petitions\n\n"
        f"## Petition: authority-boundary fixture\n\n"
        f"**Obligation**: {obligation}\n"
        f"**Type**: {ptype}\n"
        "**Evidence**: fixture evidence for task 008\n")
    return path


def _job_config(run_id: str) -> JobConfig:
    return JobConfig(
        run_id=run_id, iterations=10, max_approaches=3, vigilant=True,
        on_complete="exit", infra_retry_backoff_s=[0.0],
        infra_retry_backoff_max_s=0.0, infra_outage_budget_s=1000.0,
        model_overrides={"worker": "prov/worker-model-x",
                          "review": "prov/review-model-y"})


_supervisor = lambda root, run_id, **cfg: make_supervisor(
    root, **{"run_id": run_id, "cfg_defaults": False, "status": {"state": "running"},
             "prd": "# task 008 fixture PRD\n\nDo the thing.\n",
             "iterations": 10, "max_approaches": 3, "vigilant": True,
             "on_complete": "exit", "infra_retry_backoff_s": [0.0],
             "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
             "model_overrides": {"worker": "prov/worker-model-x",
                                 "review": "prov/review-model-y"},
             # Task 014 (PRD requirement A.1, #224): `cfg_defaults=False`
             # means the bare `JobConfig` default applies, and that
             # default is now `True` for `operator_input_needed_pause` --
             # explicitly off below, so this module's own subject (the
             # tamper-forced rejection archiving/replanning) still reaches
             # its own `rejected_approach_left` outcome; task 014's own
             # edge has its own module
             # (`tests/test_operator_input_needed_pause.py`).
             "operator_input_needed_pause": False, **cfg})


# ---------------------------------------------------------------------------
# 1. A worker/planner-authored rewrite (no petition, no judge involved at
#    all) is detected on the real `_step_worker` path and forces a
#    mandatory BLOCKING-finding instruction into the real review prompt;
#    honouring that instruction takes the real `rejected_approach_left`
#    edge -- never `verified`.
# ---------------------------------------------------------------------------

def test_worker_authored_edit_forces_a_blocking_finding_and_is_refused(tmp_path):
    root = tmp_path / "run"
    root.mkdir(parents=True)
    sup = _supervisor(root, "task008-unauthorized-worker-edit")
    sup._approach = 1

    old_text = "the original, judge-untouched criteria text"
    new_text = "the criteria text a worker rewrote on its own authority"
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": [{
        "id": "1", "title": "a task whose bar a worker quietly moved",
        # already past its own next validation attempt, with the REWRITTEN
        # text already in place and validationAttempts >= 1 -- exactly the
        # shape a worker re-marking a failed task "awaiting-validation"
        # after silently editing successCriteria would leave on disk.
        "status": "awaiting-validation", "successCriteria": new_text,
        "validationAttempts": 1, "dependsOn": [], "tier": "normal",
        "criteriaFingerprint": LoopSupervisor._criteria_fingerprint(old_text),
    }]})

    class _WorkerVerifyAgent:
        def __init__(self):
            self.running = False

        def interrupt(self) -> bool:
            return self.running

        async def run(self, prompt, transcript, **kw) -> IterationResult:
            phase = sup.run.read_status().get("phase")
            self.running = True
            try:
                if phase == "worker":
                    return _ok(json.dumps({"verdict": "no_verdict"}))
                assert phase == "verify"
                return _ok(json.dumps({"task_id": "1", "verdict": "verified"}))
            finally:
                self.running = False

    sup.runner = _WorkerVerifyAgent()  # type: ignore[assignment]

    # No petitions file exists at all -- this is deliberately NOT the
    # judge-adjudication path; nothing here ever goes through
    # `criteria_amendment_candidates`.
    asyncio.run(sup._step_worker())

    # `_check_criteria_edits` (already existing production bookkeeping,
    # invoked unconditionally at the end of every `_step_worker` pass) has
    # detected the mismatch between the stored baseline and the current
    # text, on a task whose validationAttempts was already >= 1.
    on_disk = sup.run.read_tasks()["tasks"][0]
    assert on_disk["criteriaEditedAfterValidationFailure"] is True
    assert on_disk["successCriteria"] == new_text
    assert on_disk["criteriaFingerprint"] == LoopSupervisor._criteria_fingerprint(new_text)

    captured_prompts: list[str] = []

    async def _review_honours_the_mandate(prompt, transcript, **kw):
        captured_prompts.append(prompt)
        return _ok(json.dumps({
            "verdict": "rejected",
            "reason": "task 1's successCriteria was rewritten by a worker "
                      "with no amendment authority; refusing per task 008",
        }))

    sup.runner.run = _review_honours_the_mandate  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())

    # The real review prompt, built by the real production
    # `_review_extra_context()` -> `_flagged_criteria_review_context()`
    # call chain, carried the mandatory-blocking-finding instruction --
    # this is the SAME tamper-evidence pattern
    # `petitions.tamper_review_context` already uses for a tampered
    # adjudication record.
    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]
    assert "1" in prompt
    assert new_text in prompt
    assert "BLOCKING" in prompt
    assert "no authority" in prompt.lower() or "no  authority" in prompt.lower()

    # Honouring the mandate is what "refused" means: a real rejection edge,
    # never `verified` -- the unauthorized rewrite bought the task nothing.
    # Task 009 (PRD requirement C.1, #141) added a cure-pass edge for a
    # rejection whose blocking findings are all `curable: True`, and the
    # engine-forced tamper finding IS flagged curable
    # (`findings.flagged_criteria_findings`), so between task 009 and task
    # 039 this first rejection took `rejected_curable_pass_available` and
    # appended a cure task. Task 039 (operator steer 002) reverted that for
    # this case specifically: a rejection carrying a criteria-tamper finding
    # is never curable (`findings.any_criteria_tamper_finding` gating
    # `_review_cure_pass_available`), because curing hands the SAME approach
    # another worker pass over the very criteria in dispute. So the edge is
    # the ordinary archive-and-replan one, exactly as before the cure-pass
    # edge existed, and no cure task is appended -- the same fact
    # `tests/test_e2e.py::test_criteria_edited_after_validation_failure_is_
    # refused` pins end to end and `tests/test_cure_pass_not_on_tamper_
    # task039.py` pins in process.
    assert outcome == ("advance", "replan", "rejected_approach_left")
    assert on_disk["successCriteria"] == new_text  # nothing silently reverted
    cure_tasks = [t for t in sup.run.read_tasks()["tasks"]
                 if t.get("origin") == "review-cure"]
    assert cure_tasks == []


# ---------------------------------------------------------------------------
# 2. A judge amendment naming another (real) task's id is refused --
#    driven through the real mid-run adjudication episode's own
#    `_review_verdict` -> `_apply_criteria_amendments` call, exactly the
#    seam `tests/test_criteria_amendment.py` exercises for the positive
#    case.
# ---------------------------------------------------------------------------

def test_judge_amendment_naming_another_tasks_id_is_refused(tmp_path):
    root = tmp_path / "run"
    root.mkdir(parents=True)
    sup = _supervisor(root, "task008-wrong-task-id")
    sup._approach = 1

    subject_text = (
        "this task's own successCriteria can only be verified using a "
        "state its own satisfaction would have to create first")
    other_text = "an unrelated task with its own, unrelated bar"
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": [
        {"id": "1", "title": "subject task", "status": "awaiting-validation",
         "successCriteria": subject_text, "validationAttempts": 2,
         "dependsOn": [], "tier": "hard",
         "criteriaFingerprint": LoopSupervisor._criteria_fingerprint(subject_text)},
        {"id": "2", "title": "an unrelated real task", "status": "pending",
         "successCriteria": other_text, "validationAttempts": 0,
         "dependsOn": [], "tier": "normal",
         "criteriaFingerprint": LoopSupervisor._criteria_fingerprint(other_text)},
    ]})
    _make_petitions_file(root, subject_text, "self-referential")

    async def _review_names_the_wrong_task(prompt, transcript, **kw):
        return _ok(json.dumps({
            "verdict": "rejected",
            "petition_adjudications": [{
                "obligation": subject_text,
                "adjudication": "CONFIRMED",
                # A real task id, but NOT the task whose own criteria the
                # obligation is actually about.
                "task_id": "2",
                "replacement_criteria": "a brand-new bar for task 2",
            }],
        }))

    sup.runner.run = _review_names_the_wrong_task  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())
    assert outcome[0] in ("advance", "tail")

    on_disk = {t["id"]: t for t in sup.run.read_tasks()["tasks"]}
    # Neither task's criteria moved: task 2's own text doesn't fuzzy-match
    # the obligation (which is really about task 1), so
    # `criteria_amendment_candidates` never applies the replacement -- and
    # task 1, never named at all, is untouched too.
    assert on_disk["1"]["successCriteria"] == subject_text
    assert on_disk["2"]["successCriteria"] == other_text
    assert not any(e.get("type") == "task.criteria_amended" for e in _events(sup.run))
    # task 2's id IS a real task, so this is not the "offending target
    # named" refusal (that is boundary 3, below) -- it is the silent,
    # fuzzy-match-gated drop `criteria_amendment_candidates` already
    # applies, which boundary 3's new `criteria_amendment_refusals`
    # deliberately leaves alone.
    assert not any(e.get("type") == "task.criteria_amendment_refused"
                   for e in _events(sup.run))


# ---------------------------------------------------------------------------
# 3. An attempted amendment of the PRD text, a DoD item, or a `/config`
#    path is refused WITH THE OFFENDING TARGET NAMED.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("offending_target", [
    "PRD requirement B.2",
    "Definition of done predicate 6",
    "/config/amendments/001-012.md",
])
def test_amendment_of_a_non_task_target_is_refused_and_named(
        tmp_path, offending_target):
    root = tmp_path / "run"
    root.mkdir(parents=True)
    sup = _supervisor(root, "task008-non-task-target")
    sup._approach = 1

    subject_text = "task 1's own, perfectly ordinary successCriteria text"
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": [
        {"id": "1", "title": "the only real task in this plan",
         "status": "awaiting-validation", "successCriteria": subject_text,
         "validationAttempts": 1, "dependsOn": [], "tier": "normal",
         "criteriaFingerprint": LoopSupervisor._criteria_fingerprint(subject_text)},
    ]})
    _make_petitions_file(root, subject_text, "contradictory-clauses")

    async def _review_targets_a_non_task(prompt, transcript, **kw):
        return _ok(json.dumps({
            "verdict": "rejected",
            "petition_adjudications": [{
                "obligation": subject_text,
                "adjudication": "CONFIRMED",
                "task_id": offending_target,
                "replacement_criteria": "a rewritten PRD/DoD/config text",
            }],
        }))

    sup.runner.run = _review_targets_a_non_task  # type: ignore[method-assign]

    asyncio.run(sup._step_review())

    on_disk = sup.run.read_tasks()["tasks"][0]
    assert on_disk["successCriteria"] == subject_text
    assert not any(e.get("type") == "task.criteria_amended" for e in _events(sup.run))

    refused = [e for e in _events(sup.run)
              if e.get("type") == "task.criteria_amendment_refused"]
    assert len(refused) == 1
    assert refused[0]["target"] == offending_target
    assert refused[0]["obligation"] == subject_text
    adjudication = refused[0]["adjudication"]
    assert isinstance(adjudication, dict)
    assert adjudication["task_id"] == offending_target
    assert adjudication["adjudication"] == "CONFIRMED"


# ---------------------------------------------------------------------------
# 4. A VALID amendment is recorded in a form citable afterwards: a
#    `task.criteria_amended` event in this run's own `events.jsonl`
#    carrying the old text, the new text, and the full authorizing
#    adjudication.
# ---------------------------------------------------------------------------

def test_the_applied_amendment_and_its_adjudication_are_recorded_and_citable(
        tmp_path):
    root = tmp_path / "run"
    root.mkdir(parents=True)
    sup = _supervisor(root, "task008-citable-record")
    sup._approach = 1

    old_text = ("this task's own successCriteria can only be verified using "
                "a state its own satisfaction would have to create first")
    new_text = "a fresh, achievable criterion that names no unmet state"
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": [
        {"id": "1", "title": "the task the judge legitimately re-scopes",
         "status": "awaiting-validation", "successCriteria": old_text,
         "validationAttempts": 2, "dependsOn": [], "tier": "hard",
         "criteriaFingerprint": LoopSupervisor._criteria_fingerprint(old_text)},
    ]})
    _make_petitions_file(root, old_text, "self-referential")

    async def _review_amends_legitimately(prompt, transcript, **kw):
        return _ok(json.dumps({
            "verdict": "rejected",
            "petition_adjudications": [{
                "obligation": old_text,
                "adjudication": "CONFIRMED",
                "task_id": "1",
                "replacement_criteria": new_text,
            }],
        }))

    sup.runner.run = _review_amends_legitimately  # type: ignore[method-assign]

    asyncio.run(sup._step_review())

    amended = [e for e in _events(sup.run) if e.get("type") == "task.criteria_amended"]
    assert len(amended) == 1
    ev = amended[0]
    assert ev["taskId"] == "1"
    assert ev["oldCriteria"] == old_text
    assert ev["newCriteria"] == new_text
    adjudication = ev["adjudication"]
    assert isinstance(adjudication, dict)
    assert adjudication["adjudication"] == "CONFIRMED"
    assert adjudication["obligation"] == old_text
    assert adjudication["task_id"] == "1"
    assert adjudication["replacement_criteria"] == new_text

    on_disk = sup.run.read_tasks()["tasks"][0]
    assert on_disk["successCriteria"] == new_text
    assert on_disk["criteriaFingerprint"] == LoopSupervisor._criteria_fingerprint(new_text)
    assert on_disk["validationAttempts"] == 0

    # Citable afterwards: the event is on-disk, in this run's OWN
    # events.jsonl, at a stable, engine-emitted event type -- exactly the
    # form `artifacts/reports/v0.16-task-008-record.md` points a reader at
    # rather than restating its contents.
    events_path = sup.run.root / "events.jsonl"
    assert events_path.exists()
    raw_lines = [l for l in events_path.read_text().splitlines()
                if '"task.criteria_amended"' in l]
    assert len(raw_lines) == 1
