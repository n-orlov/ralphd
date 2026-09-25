"""The verify hat's own subject matter, folded into one module (D.7 sweep,
task 028). Absorbs, whole, the five `test_*_task*.py` modules whose subject
is `_verify_task` itself -- its injected context, the resolved materiality
rubric, the task_id-mismatch guard -- and the `vigilant_verified` sidecar
migration that same verify machinery depends on:

* `tests/test_verify_attempts_context_task004.py` (task 004, #121,
  requirement A.4): the injected `extra` context always names
  `validationAttempts` against `VALIDATION_ATTEMPT_LIMIT`, carries a final-
  attempt marker only one attempt below the limit, and the lexical pin that
  `task-verify.md` step 4 refers to the same literal field name the engine
  injects.
* `tests/test_verify_materiality_rubric_task004.py` (task 004, requirement
  A.1, #154): a built verify prompt carries the same resolved materiality
  rubric review receives (or the engine default absent one), and the
  rewritten "Be strict" paragraph is lexically pinned, with the retired
  "Reject on substance, disclose on tails" bullet gone as its own bullet.
* `tests/test_verify_standing_rules_context_task025.py` (task 025, E.4,
  #115 second cut): `task-verify.md` names the notes file's harness-pinned
  `## Standing rules` heading, calls it harness-pinned, states that
  enforcing it wave-wide is review's job (not per-task `successCriteria`),
  and tells the verifier not to reject a task for an unnamed standing-rule
  drift.
* `tests/test_verify_taskid_mismatch_task005.py` (task 005, #120,
  requirement A.5): a schema-valid `verify` payload whose `task_id` names
  some OTHER task than the one under verification is a contract violation
  at the full in-process `LoopSupervisor` tier -- the ladder exhausts, the
  mismatch lands on the task record and `events.jsonl`, the wrongly-named
  task is untouched, and the negative twin (correct `task_id`) still
  reaches `validated`.
* `tests/test_vigilant_verified_migration_task005.py` (task 005,
  requirement J, #75): `RunDir.migrate_legacy_vigilant_verified()` falls
  back to another approach's namespaced sidecar entries for a task id only
  when that approach's own archived snapshot shows the identical task dict
  already `completed` -- the `selfdev-v09-fsm-loop` reproduction (approach
  advanced past the sidecar) no longer loses the verifications, the
  pre-fix current-approach-only matching is replayed as a mutation twin to
  prove the fix is load-bearing, and the ordinary same-approach fold still
  validates and cleans up.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from ralphd.engine import findings
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import (
    RunDir,
    VALIDATION_ATTEMPT_LIMIT,
    archived_approach_dir,
    atomic_write_json,
    read_events,
)
from tests.conftest import make_supervisor, supervisor_with_runner

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts"
LOOP_PY = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "engine" / "loop.py"


def _verify_text() -> str:
    path = PROMPTS_DIR / "task-verify.md"
    assert path.is_file(), f"missing prompt file {path}"
    return path.read_text()


def _verified(tid: str) -> IterationResult:
    r = IterationResult(exit_code=0, duration_s=1.0)
    r.final_text = json.dumps({"task_id": tid, "verdict": "verified"})
    r.usage = {"input": 5, "output": 2, "totalTokens": 7}
    return r


class _FixedRunner:
    def __init__(self, result: IterationResult):
        self.result = result
        self.calls = 0
        self.prompts: list[str] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.prompts.append(prompt)
        self.calls += 1
        return self.result


# ===========================================================================
# 1. `_verify_task`'s injected `validationAttempts`/final-attempt context
#    (task 004, #121, requirement A.4).
# ===========================================================================

TASK_ATTEMPTS = {
    "id": "004",
    "title": "the task under verification",
    "status": "completed",
    "successCriteria": "the thing works",
}


def _supervisor_attempts(tmp_path, task, **cfg):
    return make_supervisor(
        tmp_path, run_id="task004-verify-attempts", iterations=8, max_approaches=1,
        vigilant=True, on_complete="exit", infra_retry_backoff_s=[0.0],
        infra_retry_backoff_max_s=0.0, infra_outage_budget_s=0.0, tasks=[dict(task)],
        runner_factory=lambda sup: _FixedRunner(_verified(task['id'])), **cfg)


def _extra_block(prompt: str) -> str:
    """Only the injected `extra` block, not the whole rendered prompt --
    task-verify.md's own step-4 prose also mentions `validationAttempts`
    (by name, describing the last-available-attempt marker by role rather
    than quoting its literal text -- task 074), so asserting against the
    whole prompt would pass for the wrong reason."""
    idx = prompt.index("## Task under verification")
    return prompt[idx:]


def test_the_extra_block_names_the_count_and_the_limit(tmp_path):
    task = dict(TASK_ATTEMPTS, validationAttempts=1)
    sup = _supervisor_attempts(tmp_path, task)

    verified = asyncio.run(sup._verify_task(dict(task)))
    assert verified is True

    prompt = _extra_block(sup.runner.prompts[0])
    assert re.search(
        rf"validationAttempts\*\*:\s*1\s+of\s+{VALIDATION_ATTEMPT_LIMIT}",
        prompt), prompt


def test_the_final_attempt_marker_is_absent_at_zero_attempts(tmp_path):
    task = dict(TASK_ATTEMPTS, validationAttempts=0)
    sup = _supervisor_attempts(tmp_path, task)

    verified = asyncio.run(sup._verify_task(dict(task)))
    assert verified is True

    prompt = _extra_block(sup.runner.prompts[0])
    assert "validationAttempts" in prompt
    assert "FINAL ATTEMPT" not in prompt


def test_the_final_attempt_marker_appears_one_below_the_limit(tmp_path):
    task = dict(TASK_ATTEMPTS, validationAttempts=VALIDATION_ATTEMPT_LIMIT - 1)
    sup = _supervisor_attempts(tmp_path, task)

    verified = asyncio.run(sup._verify_task(dict(task)))
    assert verified is True

    prompt = _extra_block(sup.runner.prompts[0])
    assert "FINAL ATTEMPT" in prompt
    assert re.search(
        rf"validationAttempts\*\*:\s*{VALIDATION_ATTEMPT_LIMIT - 1}\s+of\s+"
        rf"{VALIDATION_ATTEMPT_LIMIT}", prompt), prompt


def test_the_final_attempt_marker_is_absent_at_every_other_non_final_count(
        tmp_path):
    # Anything strictly below "one below the limit" (and not itself the
    # skipped-already-exhausted case) must NOT carry the marker either --
    # only the single count one below VALIDATION_ATTEMPT_LIMIT does.
    for attempts in range(0, VALIDATION_ATTEMPT_LIMIT - 1):
        task = dict(TASK_ATTEMPTS, validationAttempts=attempts)
        sup = _supervisor_attempts(tmp_path / str(attempts), task)
        asyncio.run(sup._verify_task(dict(task)))
        prompt = _extra_block(sup.runner.prompts[0])
        assert "FINAL ATTEMPT" not in prompt, (attempts, prompt)


def _injected_field_name() -> str:
    """Extract the literal field name `_verify_task` labels its
    validationAttempts line with, straight from loop.py's own source --
    never hand-typed, so a rename on this side is what the test actually
    detects."""
    src = LOOP_PY.read_text()
    match = re.search(
        r'f"- \*\*(\w+)\*\*:\s*\{attempts\}\s*of\s*\{VALIDATION_ATTEMPT_LIMIT\}',
        src)
    assert match, (
        "could not find _verify_task's validationAttempts extra-block line "
        "in loop.py -- update this test's extraction regex to match "
        "whatever replaced it")
    return match.group(1)


def _verify_step_four_text() -> str:
    text = _verify_text()
    match = re.search(
        r"^4\.\s(.*?)(?=^\d+[a-z]?\.\s|^#)", text, re.DOTALL | re.MULTILINE)
    assert match, "task-verify.md has no `4.` step"
    return match.group(1)


def test_the_prompt_names_the_same_field_the_engine_injects():
    field_name = _injected_field_name()
    assert field_name == "validationAttempts", (
        f"unexpected extraction {field_name!r} -- fix the regex, not this "
        "assertion")
    step = _verify_step_four_text()
    assert re.search(rf"`{re.escape(field_name)}`", step), (
        f"task-verify.md step 4 never refers to the injected field "
        f"`{field_name}` by its literal name: {step!r}")


def test_the_lexical_pin_fails_if_the_two_names_diverge():
    """A direct demonstration that the pin above is load-bearing: renaming
    ONE side's literal token (simulated here, not written to disk) breaks
    the cross-check the same way a real drift would."""
    field_name = _injected_field_name()
    step = _verify_step_four_text()
    renamed = field_name + "Renamed"
    assert not re.search(rf"`{re.escape(renamed)}`", step), (
        "sanity check itself is broken: the renamed token unexpectedly "
        "matched task-verify.md's step 4")


# ===========================================================================
# 2. The resolved materiality rubric reaching a built verify prompt, and
#    the rewritten "Be strict" paragraph (task 004, requirement A.1, #154).
# ===========================================================================

TASK_RUBRIC = {
    "id": "004",
    "title": "the task under verification",
    "status": "completed",
    "successCriteria": "the thing works",
    "validationAttempts": 0,
}

RUBRIC_BEARING_CONTRACT = """# Some PRD

## Goal

Ship the thing.

## Materiality rubric

**Blocking** -- a red test at the shipped sha, task004-marker-blocking.

**Residual** -- a stale snapshot label, task004-marker-residual.
"""


def _supervisor_rubric(tmp_path, task, **cfg):
    return make_supervisor(
        tmp_path, run_id="task004-verify-rubric", iterations=8, max_approaches=1,
        vigilant=True, on_complete="exit", infra_retry_backoff_s=[0.0],
        infra_retry_backoff_max_s=0.0, infra_outage_budget_s=0.0, tasks=[dict(task)],
        runner_factory=lambda sup: _FixedRunner(_verified(task['id'])), **cfg)


def test_a_built_verify_prompt_carries_the_resolved_rubric_text(tmp_path):
    task = dict(TASK_RUBRIC)
    sup = _supervisor_rubric(tmp_path, task)
    sup.run.prd_file.write_text(RUBRIC_BEARING_CONTRACT)

    verified = asyncio.run(sup._verify_task(dict(task)))
    assert verified is True

    prompt = sup.runner.prompts[0]
    # The literal marker text from the PRD's own rubric section, so this
    # is unambiguously THE resolved rubric -- not some other rubric-shaped
    # text -- and not read off a helper in isolation but off the actual
    # rendered prompt handed to the agent.
    assert "task004-marker-blocking" in prompt
    assert "task004-marker-residual" in prompt
    assert "Materiality rubric" in prompt


def test_a_built_verify_prompt_falls_back_to_the_engine_default_rubric(
        tmp_path):
    # No PRD file written at all -- the "no rubric supplied anywhere"
    # case: verify still receives a real blocking/residual boundary (the
    # engine default), same as review does, never silence.
    task = dict(TASK_RUBRIC)
    sup = _supervisor_rubric(tmp_path, task)

    verified = asyncio.run(sup._verify_task(dict(task)))
    assert verified is True

    prompt = sup.runner.prompts[0]
    assert findings.NO_RUBRIC_NOTICE in prompt
    assert findings.DEFAULT_RUBRIC in prompt


def _be_strict_paragraph(text: str) -> str:
    match = re.search(
        r"- \*\*Be strict, but reject only on substance.*?"
        r"(?=\n- \*\*Budget your test runs)", text, re.DOTALL)
    assert match, (
        "task-verify.md has no 'Be strict, but reject only on substance' "
        "bullet immediately before the 'Budget your test runs' bullet")
    return match.group(0)


def test_the_rewritten_be_strict_paragraph_is_lexically_pinned():
    text = _verify_text()
    para = _be_strict_paragraph(text)

    # The five reject-worthy substance grounds, named explicitly.
    assert "deliverable" in para
    assert re.search(r"test.{0,30}is red", para, re.DOTALL)
    assert re.search(r"spec \(`SPEC\.md` or the user docs\) contradicts\s+the live code",
                     para)
    # Prose outside the spec is never a rejection ground.
    assert re.search(r"Prose outside the spec.{0,120}is never a rejection\s+ground",
                     para, re.DOTALL)
    assert "command the criterion names was never run" in para
    assert "secret is exposed" in para

    # Form/wording/provenance/process clauses verify with a note.
    assert "form" in para.lower()
    assert "wording" in para.lower()
    assert "provenance" in para.lower()
    assert "process" in para.lower()
    assert "note" in para.lower()

    # "Disclose on tails" folded in as a special case of this same rule.
    assert "disclose on tails" in para.lower()
    assert "special case" in para.lower()
    assert "open-set" in para.lower()


def test_the_old_standalone_reject_on_substance_bullet_is_gone():
    text = _verify_text()
    # It must not survive as its OWN separate bullet any more -- folded
    # into "Be strict" instead. A bullet starts a line with "- **".
    assert not re.search(
        r"^- \*\*Reject on substance, disclose on tails", text,
        re.MULTILINE)


# ===========================================================================
# 3. The verifier's injected standing-rules context (task 025, E.4, #115
#    second cut) -- prompt text only, no engine-code assertions.
# ===========================================================================

# the same literal heading `test_prompt_standing_rules.py` pins for
# planning.md/worker.md -- verify must refer to it by the same literal text
# or a reader has no way to tell which block it means.
STANDING_RULES_HEADING = "## Standing rules"


def test_verify_names_the_literal_standing_rules_heading():
    text = _verify_text()
    assert STANDING_RULES_HEADING in text, (
        f"task-verify.md never refers to the literal "
        f"{STANDING_RULES_HEADING!r} heading the notes file's pinned block "
        "uses -- the verifier has no way to recognise what 'the standing "
        "rules' means without it")


def test_verify_names_it_as_harness_pinned():
    text = _verify_text()
    assert re.search(r"harness-pinned", text, re.IGNORECASE), (
        "task-verify.md must call the block harness-pinned (task 107), the "
        "same wording the worker/planning prompts use for it")


def test_verify_states_review_owns_wave_wide_enforcement():
    text = _verify_text()
    assert re.search(
        r"review.{0,40}job", text, re.IGNORECASE | re.DOTALL), (
        "task-verify.md must state that enforcing the standing rules "
        "wave-wide is review's job")
    assert re.search(r"wave-wide", text, re.IGNORECASE), (
        "task-verify.md must say the enforcement is wave-wide (over the "
        "whole approach), not per task")


def test_verify_states_standing_rules_are_not_per_task_criteria():
    text = _verify_text()
    assert re.search(
        r"not\s+per-task\s+criteria|not\s+a\s+per-task\s+success",
        text, re.IGNORECASE) or re.search(
        r"successCriteria.{0,80}does not name", text, re.IGNORECASE), (
        "task-verify.md must say a standing rule is not itself a per-task "
        "success criterion, so the verifier does not reject a task for "
        "drifting from a standing rule its own successCriteria never named")


def test_verify_does_not_instruct_rejecting_for_unnamed_standing_rules():
    text = _verify_text()
    assert re.search(
        r"[Dd]o not reject a task for.{0,80}standing rule", text,
        re.DOTALL), (
        "task-verify.md must explicitly tell the verifier not to reject a "
        "task for a standing-rule drift its own criteria does not name")


# ===========================================================================
# 4. The task_id-mismatch guard inside `_verify_task`/
#    `_verify_contract_ladder` (task 005, #120, requirement A.5).
# ===========================================================================

TASK1_MISMATCH = {
    "id": "001",
    "title": "the task under verification",
    "status": "completed",
    "successCriteria": "the thing works",
    "validationAttempts": 0,
    "validationNotes": "",
}

# A second, unrelated task -- the one the verifier keeps (wrongly) naming.
# Deliberately carries fields TASK1_MISMATCH does not, so a test that
# mutated the wrong record would be caught by more than just `status`.
TASK2_MISMATCH = {
    "id": "002",
    "title": "an unrelated task the verifier keeps misnaming",
    "status": "in-progress",
    "successCriteria": "something else entirely",
    "validationAttempts": 1,
    "validationNotes": "an earlier, unrelated validation-failed round",
    "priority": 42,
}


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0, duration_s=5.0)
    r.final_text = text
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


class _SequencedRunner:
    """Replays one `IterationResult` per call, in order (the last one
    repeats forever once the sequence runs out) -- same shape as
    `test_verify_contract_ladder_task001.py`'s helper."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0
        self.prompts: list[str] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.prompts.append(prompt)
        idx = min(self.calls, len(self.results) - 1)
        self.calls += 1
        return self.results[idx]


_supervisor_taskid_mismatch = supervisor_with_runner(
    lambda sup, results: _SequencedRunner(results),
    run_id="task005-verify-taskid-mismatch", iterations=8, max_approaches=1, vigilant=True,
    on_complete="exit", infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
    infra_outage_budget_s=0.0, tasks=[dict(TASK1_MISMATCH), dict(TASK2_MISMATCH)])


def _mismatch_events(run_dir: Path) -> list[dict]:
    p = run_dir / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _mismatch_task(tasks_file: Path, tid: str) -> dict:
    data = json.loads(tasks_file.read_text())
    for t in data["tasks"]:
        if t["id"] == tid:
            return t
    raise AssertionError(f"task {tid!r} not found in {tasks_file}")


def test_a_persistent_wrong_task_id_exhausts_the_ladder_and_is_recorded_as_a_mismatch(
        tmp_path):
    wrong_task = json.dumps({"task_id": "002", "verdict": "verified"})

    sup = _supervisor_taskid_mismatch(tmp_path, [_ok(wrong_task)])   # every call replays it

    verified = asyncio.run(sup._verify_task(dict(TASK1_MISMATCH)))

    assert verified is False

    t1 = _mismatch_task(sup.run.tasks_file, "001")
    assert t1["status"] == "validation-failed"
    assert t1["validationAttempts"] == TASK1_MISMATCH["validationAttempts"] + 1
    notes = t1["validationNotes"]
    assert "task_id" in notes
    assert "'002'" in notes
    assert "is the one under verification" in notes

    # task "002" -- the task the verifier kept (wrongly) naming, but which
    # was never under verification -- is byte-identical to its pre-run
    # record.
    t2 = _mismatch_task(sup.run.tasks_file, "002")
    assert t2 == TASK2_MISMATCH

    # the ladder made its full 1 + contract_retry_max attempts before
    # giving up, all charged to no more than one iteration.
    assert sup.runner.calls == 1 + sup.cfg.contract_retry_max
    assert sup.iterations_used == 1

    events = _mismatch_events(sup.run.root)
    violations = [e for e in events if e.get("type") == "contract.violation"]
    assert violations, "expected at least one contract.violation event"
    assert any(
        any("is the one under verification" in err for err in e.get("errors", []))
        for e in violations
    ), violations
    assert any(
        any("'002'" in err for err in e.get("errors", []))
        for e in violations
    ), violations


def test_negative_twin_the_correct_task_id_reaches_validated(tmp_path):
    correct = json.dumps({"task_id": "001", "verdict": "verified"})

    sup = _supervisor_taskid_mismatch(tmp_path, [_ok(correct)])

    verified = asyncio.run(sup._verify_task(dict(TASK1_MISMATCH)))

    assert verified is True
    t1 = _mismatch_task(sup.run.tasks_file, "001")
    assert t1["status"] == "validated"

    # task "002" is untouched here too -- nothing about this run should
    # ever have written to it.
    t2 = _mismatch_task(sup.run.tasks_file, "002")
    assert t2 == TASK2_MISMATCH

    events = _mismatch_events(sup.run.root)
    violations = [e for e in events if e.get("type") == "contract.violation"]
    assert violations == []


# ===========================================================================
# 5. `RunDir.migrate_legacy_vigilant_verified()` -- the cross-approach
#    fallback that keeps a fold from losing verifications (task 005,
#    requirement J, #75).
# ===========================================================================

def _migration_plan(*ids_statuses: tuple[str, str]) -> dict:
    return {"version": 1,
            "tasks": [{"id": i, "title": f"task {i}", "status": s,
                       "successCriteria": f"{i} works"}
                      for i, s in ids_statuses]}


def _migration_archive(run: RunDir, approach: int, plan: dict) -> None:
    """Mimic the loop supervisor's `_archive_approach()` effect for this
    test's purposes: a frozen, byte-identical copy of `plan` under
    `approaches/NN/tasks.json` -- the artifact requirement J's fix shape
    names ("the archived per-approach records already carry that
    structure")."""
    adir = archived_approach_dir(run.root, approach)
    adir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(adir / "tasks.json", plan)


def _twenty_task_ids() -> list[str]:
    return [f"{i:03d}" for i in range(1, 21)]


def _migration_reproduction_plan() -> dict:
    return _migration_plan(*[(i, "completed") for i in _twenty_task_ids()])


def test_approach_advanced_past_sidecar_does_not_lose_the_verifications(tmp_path):
    """20 tasks `completed`, verified under approach 3
    (`"3:001"`..`"3:020"`), `status.json.approach` sitting at 4 -- exactly
    `selfdev-v09-fsm-loop`'s shape. None of the 20 must be demoted to
    `awaiting-validation`, and the sidecar must not be destroyed: the fold
    only got there via the cross-approach fallback (a heuristic), so the
    migration withholds the delete rather than throw away the one durable
    record of what it just had to infer."""
    run = RunDir(tmp_path)
    plan = _migration_reproduction_plan()
    # tasks.json was never rewritten for approach 4 (the #72-shaped bug):
    # it is still, byte-for-byte, what approach 3 archived.
    _migration_archive(run, 3, plan)
    run.write_tasks(plan)
    run.update_status(approach=4)
    atomic_write_json(run.vigilant_verified_file,
                       [f"3:{i}" for i in _twenty_task_ids()])

    run.migrate_legacy_vigilant_verified()

    statuses = {t["id"]: t["status"] for t in run.read_tasks()["tasks"]}
    assert set(statuses.values()) == {"validated"}, (
        "every one of the 20 tasks verified under approach 3 must read "
        "validated, not demoted to awaiting-validation, once the approach "
        "counter has moved on to 4")
    assert run.vigilant_verified_file.exists(), (
        "the fold here only succeeded through the cross-approach fallback "
        "(a heuristic), so the sidecar -- the one durable record of what "
        "was actually verified -- must not be destroyed")

    events = read_events(tmp_path, types=["vigilant_verified_migration"])
    assert len(events) == 1
    assert events[0]["usedFallback"] is True
    assert events[0]["sidecarKept"] is True
    assert events[0]["matchedCount"] == 20
    assert events[0]["completedTaskCount"] == 20


def test_approach_advanced_but_archive_missing_does_not_fabricate_a_match(tmp_path):
    """The other half of #29's guard, replayed against the NEW fallback
    path: if approach 3 was never actually archived (so there is no
    trustworthy evidence the live tasks.json is still approach 3's
    unrenumbered plan), the fallback must not fire just because the
    sidecar happens to name the same ids -- the tasks stay
    `awaiting-validation`, same as the pre-fix behaviour, and the sidecar
    is kept precisely because the fold matched nothing while there was
    something (20 completed tasks) it could have matched."""
    run = RunDir(tmp_path)
    plan = _migration_reproduction_plan()
    run.write_tasks(plan)
    run.update_status(approach=4)
    atomic_write_json(run.vigilant_verified_file,
                       [f"3:{i}" for i in _twenty_task_ids()])
    # No `_migration_archive(run, 3, plan)` call -- approach 3 was never
    # archived.

    run.migrate_legacy_vigilant_verified()

    statuses = {t["id"]: t["status"] for t in run.read_tasks()["tasks"]}
    assert set(statuses.values()) == {"awaiting-validation"}
    assert run.vigilant_verified_file.exists()

    events = read_events(tmp_path, types=["vigilant_verified_migration"])
    assert events[-1]["usedFallback"] is False
    assert events[-1]["matchedCount"] == 0
    assert events[-1]["sidecarKept"] is True


def test_ordinary_same_approach_fold_still_validates_and_cleans_up(tmp_path):
    run = RunDir(tmp_path)
    run.update_status(approach=1)
    atomic_write_json(run.vigilant_verified_file, ["1:001", "1:002"])
    run.write_tasks(_migration_plan(("001", "completed"), ("002", "completed")))

    run.migrate_legacy_vigilant_verified()

    tasks = {t["id"]: t["status"] for t in run.read_tasks()["tasks"]}
    assert tasks == {"001": "validated", "002": "validated"}
    assert not run.vigilant_verified_file.exists(), (
        "an ordinary current-approach-only fold needed no fallback and "
        "matched everything the sidecar named -- safe to delete, exactly "
        "as before this fix")

    events = read_events(tmp_path, types=["vigilant_verified_migration"])
    assert events[-1]["usedFallback"] is False
    assert events[-1]["matchedCount"] == 2
    assert events[-1]["sidecarKept"] is False


def test_migration_with_no_completed_tasks_is_legitimately_empty_and_cleans_up(tmp_path):
    """No completed tasks at all to classify -- distinct in the record from
    "matched nothing while there was something to match" (the prior two
    tests): `completedTaskCount == 0` names it explicitly, and the sidecar
    is safe to delete since nothing needed preserving."""
    run = RunDir(tmp_path)
    run.update_status(approach=1)
    atomic_write_json(run.vigilant_verified_file, ["1:001"])
    run.write_tasks(_migration_plan(("001", "pending")))

    run.migrate_legacy_vigilant_verified()

    assert run.read_tasks()["tasks"][0]["status"] == "pending"
    assert not run.vigilant_verified_file.exists()

    events = read_events(tmp_path, types=["vigilant_verified_migration"])
    assert events[-1]["completedTaskCount"] == 0
    assert events[-1]["sidecarKept"] is False


def test_mutation_current_approach_only_matching_loses_the_verifications(
        tmp_path, monkeypatch):
    """Patch both seams `migrate_legacy_vigilant_verified()` uses back to
    the pre-fix shape -- `_resolve_legacy_vigilant_ids` to direct
    current-approach matches only (no cross-approach fallback), and
    `_legacy_vigilant_fold_is_uncertain` to the old unconditional-unlink
    behaviour (never withholds the delete) -- and show test 1's exact
    fixture now DOES lose everything: all 20 tasks demoted to
    `awaiting-validation` AND the sidecar deleted anyway. Proves test 1
    actually exercises this task's fix rather than passing by
    construction."""
    def _current_approach_only(self, by_approach, approach_now, completed_tasks):
        return set(by_approach.get(approach_now, set())), False

    monkeypatch.setattr(RunDir, "_resolve_legacy_vigilant_ids", _current_approach_only)
    monkeypatch.setattr(RunDir, "_legacy_vigilant_fold_is_uncertain",
                         staticmethod(lambda *a, **k: False))

    run = RunDir(tmp_path)
    plan = _migration_reproduction_plan()
    _migration_archive(run, 3, plan)
    run.write_tasks(plan)
    run.update_status(approach=4)
    atomic_write_json(run.vigilant_verified_file,
                       [f"3:{i}" for i in _twenty_task_ids()])

    run.migrate_legacy_vigilant_verified()

    statuses = {t["id"]: t["status"] for t in run.read_tasks()["tasks"]}
    assert set(statuses.values()) == {"awaiting-validation"}, (
        "the old current-approach-only matching finds nothing under "
        "approach 4's empty namespace and demotes every one of the 20 "
        "tasks -- the exact selfdev-v09-fsm-loop incident")
    assert not run.vigilant_verified_file.exists(), (
        "the old code unlinked unconditionally, with no notion of an "
        "uncertain fold -- the second half of the incident, alongside the "
        "lost verifications asserted above")
