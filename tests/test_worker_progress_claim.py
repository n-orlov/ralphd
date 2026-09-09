"""Task 011 (PRD requirement A.1, #156): "the claim" -- the first of the
five #156 stages. The worker contract accepts an OPTIONAL `progress` block
-- `{"summary": <str>, "evidence": [{"kind": "commit"|"file"|"external",
"ref": <str>}, ...]}` -- alongside `verdict: "no_verdict"`; `worker.md`
documents it (inline, never inside a fenced contract object -- #170's own
future scan is not this task's business, but there is no reason to create
the very thing it will have to remove); and a `progress` block riding
alongside any OTHER verdict is ignored and logged rather than validated or
applied (nothing left to charge a "still working" claim against once the
task is already decided).

Deliberately narrow scope, matching the PRD's own five-stage split for
requirement A: fingerprint corroboration (stage 2, task 012), the per-task
progress budget (stage 3, task 013) and the `status.json`/surfaces
(stage 4, task 014) do not exist yet -- this task only has to accept the
shape, validate each evidence item's `kind`, and route a misplaced block to
the ignore-and-log path instead of the validation ladder.

Three behaviours, each pinned twice: once as a direct schema/semantic
check, and once driven through the REAL production call site
(`LoopSupervisor._step_worker`, a stubbed `sup.runner` only), the same
style `tests/test_impossibility_declaration.py` (task 004/005's own
sibling side channel) already uses for `petition`:

- acceptance: a well-formed `progress` block alongside `"no_verdict"` is
  applied in exactly one attempt, logged in `events.jsonl`;
- ignore-and-log: a `progress` block alongside `"complete"` never reaches
  the validation ladder at all -- the ordinary `"complete"` report is
  applied unchanged, and a `log` event records the block as ignored;
- an invalid `kind`: rejected by `transitions.worker_progress_report_errors`
  and, via the real path, re-asked through `contract_enforcement.
  enforce_contract`'s ladder exactly like any other worker semantic
  violation -- never silently accepted, never silently dropped.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import contracts, transitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

REPO = Path(__file__).resolve().parents[1]
WORKER_MD = (REPO / "src" / "ralphd" / "prompts" / "worker.md").read_text()


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


class _ScriptedWorker:
    """Same shape `test_impossibility_declaration.py`'s own
    `_ScriptedWorker` uses: every call -- the charged `run_iteration`
    call AND every uncharged `_ask_contract_fix` re-ask -- advances
    through `script`, the last entry repeating forever."""

    def __init__(self, script: list[IterationResult]):
        self.script = script
        self.calls = 0

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return step


def _worker_sup(tmp_path: Path, name: str, **cfg_kw) -> tuple[LoopSupervisor, RunDir]:
    root = tmp_path / name
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [
        {"id": "011t", "title": "the task this worker iteration was assigned",
         "status": "in-progress", "successCriteria": "n/a"},
    ]})
    kw = {"iterations": 1, "max_approaches": 3, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    cfg = JobConfig(run_id=f"task011-{name}", **kw)
    sup = LoopSupervisor(cfg, run, root)
    sup._approach = 1
    return sup, run


def _log_messages(run: RunDir) -> list[str]:
    events = [json.loads(x) for x in
              (run.root / "events.jsonl").read_text().splitlines() if x.strip()]
    return [e.get("message") or "" for e in events if e.get("type") == "log"]


def _events_of_type(run: RunDir, type_: str) -> list[dict]:
    events = [json.loads(x) for x in
              (run.root / "events.jsonl").read_text().splitlines() if x.strip()]
    return [e for e in events if e.get("type") == type_]


GOOD_PROGRESS = {
    "summary": "read the failing node, added a targeted fix",
    "evidence": [{"kind": "commit", "ref": "abc1234"},
                 {"kind": "external", "ref": "re-ran the suite manually"}],
}


# ---------------------------------------------------------------------------
# The schema field itself
# ---------------------------------------------------------------------------


def test_hat_schemas_worker_declares_an_optional_progress_object():
    schema = transitions.HAT_SCHEMAS["worker"]
    assert "progress" in schema["properties"]
    assert "progress" not in schema["required"]
    prog = schema["properties"]["progress"]
    assert prog["type"] == "object"
    assert set(prog["required"]) == {"summary", "evidence"}


def test_a_worker_payload_with_a_well_formed_progress_block_validates_clean():
    errors = contracts.validate_payload("worker", {
        "verdict": "no_verdict", "progress": GOOD_PROGRESS})
    assert errors == []


def test_a_progress_block_with_wrong_outer_types_is_a_schema_violation():
    errors = contracts.validate_payload("worker", {
        "verdict": "no_verdict",
        "progress": {"summary": 5, "evidence": "not a list"}})
    assert errors


def test_a_progress_block_with_an_unexpected_field_is_a_schema_violation():
    errors = contracts.validate_payload("worker", {
        "verdict": "no_verdict",
        "progress": {**GOOD_PROGRESS, "extra": "nope"}})
    assert errors


# ---------------------------------------------------------------------------
# The semantic check: transitions.worker_progress_report_errors
# ---------------------------------------------------------------------------


def test_no_progress_key_at_all_is_never_in_violation():
    assert transitions.worker_progress_report_errors({"verdict": "complete"}) == []
    assert transitions.worker_progress_report_errors("not a dict") == []


def test_a_well_formed_progress_block_has_no_errors():
    assert transitions.worker_progress_report_errors(
        {"verdict": "no_verdict", "progress": GOOD_PROGRESS}) == []


def test_a_blank_summary_is_an_error():
    errors = transitions.worker_progress_report_errors({
        "verdict": "no_verdict",
        "progress": {"summary": "   ", "evidence": GOOD_PROGRESS["evidence"]}})
    assert any("summary" in e for e in errors)


def test_an_empty_evidence_list_is_an_error():
    errors = transitions.worker_progress_report_errors({
        "verdict": "no_verdict",
        "progress": {"summary": "did work", "evidence": []}})
    assert any("evidence" in e for e in errors)


def test_an_invalid_kind_is_an_error_naming_the_three_valid_values():
    errors = transitions.worker_progress_report_errors({
        "verdict": "no_verdict",
        "progress": {"summary": "did work",
                     "evidence": [{"kind": "guess", "ref": "x"}]}})
    assert errors
    assert any("kind" in e and "commit" in e and "file" in e and "external" in e
                for e in errors)


def test_each_of_the_three_valid_kinds_is_accepted():
    for kind in ("commit", "file", "external"):
        errors = transitions.worker_progress_report_errors({
            "verdict": "no_verdict",
            "progress": {"summary": "did work",
                         "evidence": [{"kind": kind, "ref": "x"}]}})
        assert errors == [], (kind, errors)


def test_a_blank_ref_is_an_error():
    errors = transitions.worker_progress_report_errors({
        "verdict": "no_verdict",
        "progress": {"summary": "did work",
                     "evidence": [{"kind": "commit", "ref": ""}]}})
    assert any("ref" in e for e in errors)


# ---------------------------------------------------------------------------
# worker.md documents the block, with no contract object inside a fence
# ---------------------------------------------------------------------------


def test_worker_md_documents_the_progress_block():
    assert "## Reporting progress while still working" in WORKER_MD
    section = WORKER_MD.split("## Reporting progress while still working", 1)[1]
    section = section.split("## Sandbox safety", 1)[0]
    assert '"no_verdict"' in section
    assert "summary" in section
    assert "evidence" in section
    assert "commit" in section and "file" in section and "external" in section
    assert "ignored and logged" in section


def test_worker_md_progress_section_has_no_fenced_code_block():
    section = WORKER_MD.split("## Reporting progress while still working", 1)[1]
    section = section.split("## Sandbox safety", 1)[0]
    assert "```" not in section


# ---------------------------------------------------------------------------
# In-process nodes: acceptance, ignore-and-log, invalid kind -- all driven
# through the REAL LoopSupervisor._step_worker path.
# ---------------------------------------------------------------------------


def test_acceptance_a_well_formed_progress_block_is_applied_in_one_attempt(tmp_path):
    sup, run = _worker_sup(tmp_path, "accept")
    payload_text = json.dumps({"verdict": "no_verdict", "progress": GOOD_PROGRESS})
    sup.runner = _ScriptedWorker([_ok(payload_text)])  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    assert sup.runner.calls == 1  # type: ignore[attr-defined]
    messages = _log_messages(run)
    assert any("progress" in m and GOOD_PROGRESS["summary"] in m for m in messages)
    assert _events_of_type(run, "contract.violation") == []
    # The claim rode alongside "no_verdict" -- the assigned task itself is
    # untouched; this task's own scope is the claim, not the budget/verify
    # replacement (tasks 012/013).
    task = run.read_tasks()["tasks"][0]
    assert task["status"] == "in-progress"


def test_ignore_and_log_a_progress_block_alongside_complete_is_ignored(tmp_path):
    sup, run = _worker_sup(tmp_path, "ignore")
    payload_text = json.dumps({
        "verdict": "complete", "task_id": "011t", "progress": GOOD_PROGRESS})
    sup.runner = _ScriptedWorker([_ok(payload_text)])  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    # The ordinary "complete" report was applied unchanged -- the progress
    # block never reached the validation ladder for the WORKER hat at all
    # (no `contract.violation`/`contract.agent_fault` naming `"worker"`;
    # a fully-completed one-task run then triggers this harness's own
    # unrelated off-budget grace-review grant, whose own review-phase
    # contract chatter -- the scripted stub replaying the same payload for
    # a hat it was never shaped for -- is not this test's business) and is
    # only a log record.
    worker_violations = [e for e in _events_of_type(run, "contract.violation")
                         if e.get("hat") == "worker"]
    worker_faults = [e for e in _events_of_type(run, "contract.agent_fault")
                     if e.get("hat") == "worker"]
    assert worker_violations == []
    assert worker_faults == []
    messages = _log_messages(run)
    assert any("progress block" in m and "ignored" in m and "'complete'" in m
               for m in messages)
    task = run.read_tasks()["tasks"][0]
    assert task["status"] in ("completed", "awaiting-validation")


def test_an_invalid_kind_is_reasked_through_the_ladder_not_silently_accepted(tmp_path):
    sup, run = _worker_sup(tmp_path, "badkind")
    bad_text = json.dumps({
        "verdict": "no_verdict",
        "progress": {"summary": "did work",
                     "evidence": [{"kind": "guess", "ref": "x"}]}})
    sup.runner = _ScriptedWorker([_ok(bad_text)])  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    # The ladder actually re-asked -- contract_retry_max + 1 attempts, all
    # of which see the same malformed `kind` (the script repeats its last
    # entry forever) and none of which is silently accepted.
    assert sup.runner.calls == sup.cfg.contract_retry_max + 1  # type: ignore[attr-defined]
    violations = _events_of_type(run, "contract.violation")
    faults = _events_of_type(run, "contract.agent_fault")
    assert len(violations) == sup.cfg.contract_retry_max + 1
    assert len(faults) == 1
    assert any("kind" in e for ev in violations for e in ev.get("errors", []))
    # Never applied: nothing beyond the ladder's own violation/fault events
    # was emitted for this claim.
    messages = _log_messages(run)
    assert not any("worker reported progress on the assigned task" in m
                   for m in messages)
