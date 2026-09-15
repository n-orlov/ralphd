"""Task 004 (requirement A.1, #114): the worker contract's structured
impossibility declaration -- a worker names the obligation (a requirement
letter, a DoD item, or its own task's criterion clause), the petition TYPE
(`petitions.PETITION_TYPES`) and the EVIDENCE, and the ENGINE -- never the
worker -- writes/validates the filing into the wave's own
`artifacts/reports/<wave>-petitions.md`, in `docs/petition-format.md`'s
field shape, refusing a second declaration against an obligation already
filed instead of filing it.

Task 005 PART 1 (requirement A.1's missing wiring, #114) closes task 004's
own residual gap, disclosed here per operator steer 001: 004 shipped the
declaration schema, `transitions.worker_petition_report_errors` and the
write path itself (`petitions.file_worker_petition`, refuses duplicates),
but `LoopSupervisor` never called any of them on the real worker path --
this module's OWN three behaviour tests used to drive only a test-local
`_extra_validate` helper (never `LoopSupervisor._step_worker` itself),
exactly the gap 004's own `validationNotes` names. **This task also closes
task 004's residual gap (see its validationNotes).** The three tests below
now drive the REAL production call site
(`LoopSupervisor._apply_worker_petition_declaration`, wired into
`_step_worker`, `src/ralphd/engine/loop.py`) instead of the retired
`_extra_validate` shim -- `rg _apply_worker_petition_declaration
src/ralphd/engine/loop.py` finds it outside `tests/` at the shipped sha.

Three behaviours, pinned as separate tests, each driven through
`LoopSupervisor._step_worker` itself -- a stubbed `sup.runner`, a real
`RunDir` on a real `tmp_path`, never a test-local validation shim -- all
carrying `verdict: "no_verdict"` alongside the petition (the routing is
independent of the verdict value):

- a valid declaration is filed exactly once, through
  `petitions.file_worker_petition`, and parses back through
  `petitions.parse_petitions` with the declared type;
- a malformed declaration (a blank `obligation`) is refused through the
  SAME `contract_enforcement.enforce_contract` re-ask ladder every other
  worker semantic channel in this tree already uses (`transitions.
  worker_unsatisfiable_report_errors`'s own sibling, `transitions.
  worker_petition_report_errors`) -- re-asked, never filed;
- a duplicate declaration against an obligation that already has one is
  refused the same way, and the original filing is left untouched.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import petitions
from ralphd.engine.config import JobConfig
from ralphd.engine import contracts
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _make_petitions_file(workspace: Path, wave: str = "v0.16") -> Path:
    reports = workspace / "artifacts" / "reports"
    reports.mkdir(parents=True)
    path = reports / f"{wave}-petitions.md"
    path.write_text(
        f"# {wave} petitions\n\nNo petition filed yet in this wave.\n")
    return path


class _ScriptedWorker:
    """The same shape `tests/test_contract_events.py`'s own
    `_ScriptedWorker` uses to drive a real `_step_worker()` pass: every
    call -- the one charged `run_iteration("worker", ...)` call AND every
    uncharged `_ask_contract_fix` re-ask (both reach `self.runner.run`
    directly) -- advances through `script`, the last entry repeating
    forever."""

    def __init__(self, script: list[IterationResult]):
        self.script = script
        self.calls = 0

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return step


def _worker_sup(tmp_path: Path, name: str, **cfg_kw) -> tuple[LoopSupervisor, RunDir, Path]:
    root = tmp_path / name
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [
        {"id": "1", "title": "the task this worker iteration was assigned",
         "status": "in-progress", "successCriteria": "n/a"},
    ]})
    kw = {"iterations": 1, "max_approaches": 3, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    cfg = JobConfig(run_id=f"task004-005-{name}", **kw)
    sup = LoopSupervisor(cfg, run, root)
    sup._approach = 1
    return sup, run, root


def _petition_payload(obligation: str, ptype: str, evidence: str) -> str:
    return json.dumps({
        "verdict": "no_verdict",
        "petition": {"obligation": obligation, "type": ptype,
                     "evidence": evidence},
    })


# ---------------------------------------------------------------------------
# `self-referential` joins the type table (requirement A.1's own text)
# ---------------------------------------------------------------------------


def test_self_referential_is_a_member_of_petition_types():
    assert "self-referential" in petitions.PETITION_TYPES
    assert len(petitions.PETITION_TYPES) == 5


def test_worker_schema_accepts_a_self_referential_petition():
    payload = {
        "verdict": "no_verdict",
        "petition": {
            "obligation": "Task 900's own criterion clause",
            "type": "self-referential",
            "evidence": "the criterion names its own not-yet-existing sha",
        },
    }
    assert contracts.validate_payload("worker", payload) == []


def test_petition_format_doc_names_five_types_including_self_referential():
    doc = Path("docs/petition-format.md").read_text()
    assert "## The five types" in doc
    assert "## The four types" not in doc
    assert "self-referential" in doc


# ---------------------------------------------------------------------------
# A valid declaration: filed exactly once via the REAL _step_worker path
# ---------------------------------------------------------------------------


def test_a_valid_declaration_is_filed_exactly_once_via_real_step_worker(
        tmp_path, monkeypatch):
    """Task 065 (requirement N.6, #150): the production write path now
    passes `run=self.run`, so the filing lands in `run.petitions_file` --
    RUN scope -- and the workspace's own `<wave>-petitions.md` (still
    tracked here purely to prove it is left ALONE) never gains a second
    section for it. #150 stays CLOSED: this is not its glob-discovery bug
    fixed again, it is the tree-side file that bug reads, removed for a
    run built on this code.
    """
    sup, run, workspace = _worker_sup(tmp_path, "valid")
    petitions_file = _make_petitions_file(workspace)
    before = petitions_file.read_text()
    obligation = "Requirement Z: an obligation this test invents for coverage"
    payload_text = _petition_payload(
        obligation, "environmental", "no such capability in this sandbox")
    sup.runner = _ScriptedWorker([_ok(payload_text)])  # type: ignore[assignment]

    calls: list[tuple] = []
    original = petitions.file_worker_petition

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(petitions, "file_worker_petition", spy)

    asyncio.run(sup._step_worker())

    # Invoked through the REAL production path -- LoopSupervisor itself,
    # never a test-local validation shim -- and exactly once, with this
    # run's own handle.
    assert len(calls) == 1, "file_worker_petition must be invoked exactly once"
    assert calls[0][1].get("run") is run

    # The workspace's tracked wave file is UNTOUCHED (requirement N.6) --
    # the filing landed in the run dir instead.
    assert petitions_file.read_text() == before

    parsed = petitions.parse_petitions(run.petitions_file.read_text())
    assert len(parsed) == 1
    assert parsed[0]["obligation"] == obligation
    assert parsed[0]["type"] == "environmental"
    assert parsed[0]["adjudication"] is None

    # Only ONE attempt was needed -- a valid declaration is never re-asked.
    assert sup.runner.calls == 1  # type: ignore[attr-defined]

    # The task this worker iteration was assigned is untouched by the
    # petition side channel -- filing a petition is not a task-status move.
    task = run.read_tasks()["tasks"][0]
    assert task["status"] == "in-progress"


# ---------------------------------------------------------------------------
# A malformed declaration: re-asked through the ladder, never filed
# ---------------------------------------------------------------------------


def test_a_malformed_declaration_is_reasked_not_filed_via_real_step_worker(
        tmp_path, monkeypatch):
    sup, run, workspace = _worker_sup(tmp_path, "malformed")
    petitions_file = _make_petitions_file(workspace)
    # A blank obligation passes the schema's own "type": "string" check
    # (the empty string is still a string) but is exactly what
    # `transitions.worker_petition_report_errors` -- the semantic half --
    # catches.
    bad_text = _petition_payload("   ", "environmental", "some evidence")
    sup.runner = _ScriptedWorker([_ok(bad_text)])  # type: ignore[assignment]

    calls: list[tuple] = []
    original = petitions.file_worker_petition

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(petitions, "file_worker_petition", spy)

    asyncio.run(sup._step_worker())

    # The ladder actually re-asked -- contract_retry_max + 1 attempts.
    assert sup.runner.calls == sup.cfg.contract_retry_max + 1  # type: ignore[attr-defined]
    # The write path was NEVER reached: the malformed declaration failed
    # the semantic check before `file_worker_petition` is ever called.
    assert calls == []
    assert petitions.parse_petitions(petitions_file.read_text()) == []

    events = [json.loads(x) for x in (run.root / "events.jsonl").read_text().splitlines()
             if x.strip()]
    violation_events = [e for e in events if e.get("type") == "contract.violation"]
    fault_events = [e for e in events if e.get("type") == "contract.agent_fault"]
    assert len(violation_events) == sup.cfg.contract_retry_max + 1
    assert len(fault_events) == 1
    assert any("obligation" in e["errors"][0] for e in violation_events)


# ---------------------------------------------------------------------------
# A duplicate declaration: refused, never filed, original untouched
# ---------------------------------------------------------------------------


def test_a_duplicate_declaration_is_refused_not_filed_via_real_step_worker(
        tmp_path, monkeypatch):
    sup, run, workspace = _worker_sup(tmp_path, "duplicate")
    petitions_file = _make_petitions_file(workspace)
    obligation = "Requirement Y: exact same obligation text every time"

    first_errors = petitions.file_worker_petition(
        workspace, obligation, "environmental", "the first filing's evidence")
    assert first_errors == []

    dup_text = _petition_payload(obligation, "temporal",
                                 "a second attempt at the same obligation")
    sup.runner = _ScriptedWorker([_ok(dup_text)])  # type: ignore[assignment]

    calls: list[tuple] = []
    original = petitions.file_worker_petition

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(petitions, "file_worker_petition", spy)

    asyncio.run(sup._step_worker())

    # The ladder re-asked over the refusal -- every attempt reached the
    # write path (which refused, every time) and the ladder still ran out.
    assert sup.runner.calls == sup.cfg.contract_retry_max + 1  # type: ignore[attr-defined]
    assert len(calls) == sup.cfg.contract_retry_max + 1

    parsed = petitions.parse_petitions(petitions_file.read_text())
    # Still exactly the one, original filing -- the duplicate never landed.
    assert len(parsed) == 1
    assert parsed[0]["type"] == "environmental"
    assert parsed[0]["evidence"] == "the first filing's evidence"
