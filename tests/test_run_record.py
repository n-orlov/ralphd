"""Task 006 (PRD requirement B.1, #140): `run-record.md`/`.json`.

Two layers, both in-process (no docker, no real `pi`):

  * `engine.run_record` unit tests -- build the record straight off a hand
    built run dir (archived approaches with findings sidecars, a confirmed
    petition adjudication, a consumed steer, an operator ruling under a
    monkeypatched `CONFIG_DIR`, and `status.json` usage/iteration figures),
    then assert on the JSON shape and that the markdown renders the same
    facts.
  * `LoopSupervisor` tests -- `_finish()` writes both files at entry to
    `finishing` (reflect on and off), and a later terminal transition
    (`_promote_finishing_state()`, `_promote_recorded_finishing_state()`)
    rewrites them, exactly as PRD requirement B.1 asks.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine import loop as loop_mod
from ralphd.engine import run_record
from ralphd.engine.config import JobConfig
from ralphd.engine.findings import write_findings_json
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.petitions import record_confirmed_adjudications
from ralphd.engine.state import (
    RunDir,
    VERDICT_GATE_FINDINGS_JSON_FILE,
    amendment_entries,
    atomic_write_json,
    write_amendment,
)
from tests.conftest import make_supervisor


def _run(tmp_path: Path) -> RunDir:
    return RunDir(root=tmp_path / "run")


_supervisor = lambda tmp_path, run=None, **cfg: make_supervisor(
    tmp_path, **{"run_id": "unit-run", "cfg_defaults": False,
                 "run_root": (run or _run(tmp_path)).root, "workspace": "ws",
                 "max_approaches": 3, **cfg})


# ---------------------------------------------------------------------------
# engine.run_record: approach history
# ---------------------------------------------------------------------------

def test_approach_history_reads_archived_approach_with_blocking_and_residual(tmp_path):
    run = _run(tmp_path)
    adir = run.root / "approaches" / "01"
    adir.mkdir(parents=True)
    atomic_write_json(adir / "tasks.json", {"tasks": [
        {"id": "001", "status": "completed"},
        {"id": "002", "status": "pending"},
    ]})
    write_findings_json(adir / "review-findings.json", [
        {"severity": "blocking", "curable": True, "clause": "req A",
         "evidence": "missing X"},
        {"severity": "residual", "curable": False, "clause": "req B",
         "evidence": "cosmetic gap"},
    ])
    run.update_status(approach=2)  # approach 1 archived, 2 is the live one
    history = run_record.build_approach_history(run)
    assert [h["approach"] for h in history] == [1, 2]
    a1 = history[0]
    assert a1["archived"] is True
    assert a1["archivedDir"] == "approaches/01"
    assert a1["taskTally"]["total"] == 2
    assert a1["review"]["verdict"] == "rejected"
    assert [f["clause"] for f in a1["review"]["blocking"]] == ["req A"]
    assert [f["clause"] for f in a1["review"]["residual"]] == ["req B"]
    assert a1["verdictGate"] is None  # never written for this approach
    a2 = history[1]
    assert a2["archived"] is False
    assert a2["archivedDir"] is None
    assert a2["review"] is None  # nothing written at the run root either


def test_approach_history_reads_live_channel_off_the_run_root_when_never_archived(tmp_path):
    run = _run(tmp_path)
    write_findings_json(run.findings_json_file, [
        {"severity": "residual", "curable": True, "clause": "req C",
         "evidence": "noted, not blocking"},
    ])
    run.write_tasks({"tasks": [{"id": "001", "status": "validated"}]})
    history = run_record.build_approach_history(run)
    assert len(history) == 1
    live = history[0]
    assert live["approach"] == 1
    assert live["archived"] is False
    assert live["taskTally"] == {"total": 1, "validated": 1}
    assert live["review"]["verdict"] == "verified"


def test_approach_history_never_duplicates_an_archived_approach_as_live(tmp_path):
    """The current approach in status.json IS the one just archived (the
    ordinary rejection path archives before advancing `approach`) -- must
    not appear twice."""
    run = _run(tmp_path)
    adir = run.root / "approaches" / "01"
    adir.mkdir(parents=True)
    atomic_write_json(adir / "tasks.json", {"tasks": []})
    run.update_status(approach=1)
    history = run_record.build_approach_history(run)
    assert len(history) == 1
    assert history[0]["archived"] is True


def test_approach_history_verdict_gate_channel_attributed_separately(tmp_path):
    run = _run(tmp_path)
    adir = run.root / "approaches" / "01"
    adir.mkdir(parents=True)
    atomic_write_json(adir / "tasks.json", {"tasks": []})
    write_findings_json(adir / VERDICT_GATE_FINDINGS_JSON_FILE, [
        {"severity": "blocking", "curable": False, "clause": "gate req",
         "evidence": "peer disagreed"},
    ])
    run.update_status(approach=2)
    history = run_record.build_approach_history(run)
    a1 = history[0]
    assert a1["review"] is None
    assert a1["verdictGate"]["verdict"] == "rejected"
    assert a1["verdictGate"]["blocking"][0]["clause"] == "gate req"


# ---------------------------------------------------------------------------
# engine.run_record: petitions / steers / operator rulings / notifications
# ---------------------------------------------------------------------------

def test_petitions_section_reads_confirmed_adjudications_file(tmp_path):
    run = _run(tmp_path)
    record_confirmed_adjudications(
        run, [{"obligation": "Cannot reach the network here.",
              "adjudication": "CONFIRMED"}])
    section = run_record.build_petitions_section(run)
    assert section["tampered"] is False
    assert section["entries"] == [
        {"obligation": "Cannot reach the network here.",
         "adjudication": "CONFIRMED"}]


def test_petitions_section_empty_when_nothing_ever_confirmed(tmp_path):
    run = _run(tmp_path)
    section = run_record.build_petitions_section(run)
    assert section == {"entries": [], "tampered": False}


def test_steers_section_only_lists_consumed_files(tmp_path):
    run = _run(tmp_path)
    f1 = run.add_steering("do the thing", name="one")
    run.add_steering("do the other thing", name="two")
    run.consume_steering([run.steering_dir / f1], iteration=1)
    steers = run_record.build_steers_section(run)
    assert [s["file"] for s in steers] == [f1]
    assert steers[0]["consumed"] is True


def test_operator_rulings_section_reads_config_root_amendments(tmp_path):
    cdir = tmp_path / "config"
    write_amendment(cdir, "003", "waived for the run.", waive=True)
    rulings = run_record.build_operator_rulings_section(cdir)
    assert [r["taskId"] for r in rulings] == ["003"]
    assert rulings == amendment_entries(cdir)


def test_notifications_section_empty_until_a_notification_sent_event_exists(tmp_path):
    run = _run(tmp_path)
    assert run_record.build_notifications_section(run) == []
    run.emit(run_record.NOTIFICATION_SENT_EVENT, endpoint="http://x",
             textSha256="abc", httpStatus=200)
    notifications = run_record.build_notifications_section(run)
    assert len(notifications) == 1
    assert notifications[0]["httpStatus"] == 200


# ---------------------------------------------------------------------------
# engine.run_record: the whole record + markdown rendering
# ---------------------------------------------------------------------------

def test_build_run_record_carries_usage_and_iterations_from_status_json(tmp_path):
    run = _run(tmp_path)
    run.update_status(state="succeeded", verdict="verified", approach=1,
                      iterationsUsed=12,
                      usage={"totalTokens": 500, "costUSD": 0.5})
    record = run_record.build_run_record(
        run, run_id="unit-run", max_approaches=3, config_root=tmp_path / "cfg")
    assert record["runId"] == "unit-run"
    assert record["state"] == "succeeded"
    assert record["verdict"] == "verified"
    assert record["iterationsUsed"] == 12
    assert record["usage"] == {"totalTokens": 500, "costUSD": 0.5}
    assert record["maxApproaches"] == 3
    assert record["notifications"] == []
    assert record["petitions"] == {"entries": [], "tampered": False}
    assert record["steersConsumed"] == []
    assert record["operatorRulings"] == []
    assert isinstance(record["approaches"], list) and record["approaches"]


def test_markdown_renders_the_same_record_for_a_human(tmp_path):
    run = _run(tmp_path)
    write_findings_json(run.findings_json_file, [
        {"severity": "blocking", "curable": True, "clause": "req D",
         "evidence": "not done yet"},
    ])
    run.update_status(state="failed", verdict="unverified", approach=1,
                      iterationsUsed=3, usage={"totalTokens": 10})
    record_confirmed_adjudications(
        run, [{"obligation": "no network route here",
              "adjudication": "CONFIRMED"}])
    f1 = run.add_steering("focus on X", name="focus")
    run.consume_steering([run.steering_dir / f1], iteration=1)
    record = run_record.build_run_record(
        run, run_id="unit-run", max_approaches=1, config_root=tmp_path / "cfg")
    md = run_record.render_run_record_markdown(record)
    assert "unit-run" in md
    assert "`failed`" in md
    assert "req D" in md and "not done yet" in md
    assert "no network route here" in md
    assert f1 in md
    assert "## Petitions" in md and "## Steers consumed" in md
    assert "## Notifications" in md and "## Approach history" in md


# ---------------------------------------------------------------------------
# LoopSupervisor: write at entry to finishing, rewrite on later promotion
# ---------------------------------------------------------------------------

def _json_record(run: RunDir) -> dict:
    return json.loads((run.artifacts_dir / "run-record.json").read_text())


def test_finish_writes_both_files_with_reflect_false(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", tmp_path / "no-config")
    run = _run(tmp_path)
    sup = _supervisor(tmp_path, run, reflect=False)
    sup._finish("succeeded", "verified")
    assert (run.artifacts_dir / "run-record.json").is_file()
    assert (run.artifacts_dir / "run-record.md").is_file()
    record = _json_record(run)
    assert record["state"] == "succeeded"
    assert record["verdict"] == "verified"
    assert record["runId"] == "unit-run"


def test_finish_writes_both_files_with_reflect_true_state_still_finishing(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", tmp_path / "no-config")
    run = _run(tmp_path)
    sup = _supervisor(tmp_path, run, reflect=True)
    sup._finish("succeeded", "verified")
    record = _json_record(run)
    # `_finish()` with reflect:true leaves status.json's own `state` at the
    # non-terminal `finishing` -- the run record reads that same status.json,
    # so it must say the same thing until `_promote_finishing_state()` runs.
    assert run.read_status()["state"] == "finishing"
    assert record["state"] == "finishing"
    assert record["verdict"] == "verified"


def test_promote_finishing_state_rewrites_the_record_to_the_terminal_state(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", tmp_path / "no-config")
    run = _run(tmp_path)
    sup = _supervisor(tmp_path, run, reflect=True)
    sup._finish("succeeded", "verified")
    assert _json_record(run)["state"] == "finishing"
    # Something happens in the reflect window that the rewritten record must
    # pick up (a steer consumed just before the run ended).
    f1 = run.add_steering("late note", name="late")
    run.consume_steering([run.steering_dir / f1], iteration=99)
    sup._promote_finishing_state()
    record = _json_record(run)
    assert record["state"] == "succeeded"
    assert [s["file"] for s in record["steersConsumed"]] == [f1]


def test_promote_recorded_finishing_state_rewrites_the_record(tmp_path, monkeypatch):
    """The resumed-process counterpart: a fresh `LoopSupervisor` that never
    called `_finish()` at all, reading a `finishing` status back off disk
    (task 012, #56) -- the record must still be (re)written at that point,
    not only when the same process called `_finish()` itself."""
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", tmp_path / "no-config")
    run = _run(tmp_path)
    run.update_status(state="finishing", verdict="verified",
                      pendingTerminalState="succeeded", approach=1)
    sup = _supervisor(tmp_path, run, reflect=True)
    state = sup._promote_recorded_finishing_state(run.read_status())
    assert state == "succeeded"
    record = _json_record(run)
    assert record["state"] == "succeeded"
    assert record["verdict"] == "verified"


def test_promote_recorded_finishing_state_still_writes_record_on_corrupt_status(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", tmp_path / "no-config")
    run = _run(tmp_path)
    run.update_status(state="finishing", pendingTerminalState="not-a-real-state")
    sup = _supervisor(tmp_path, run, reflect=True)
    state = sup._promote_recorded_finishing_state(run.read_status())
    assert state == "failed"
    record = _json_record(run)
    assert record["state"] == "failed"


def test_operator_rulings_flow_through_to_the_written_record(tmp_path, monkeypatch):
    cdir = tmp_path / "config"
    write_amendment(cdir, "007", "waived.", waive=True)
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", cdir)
    run = _run(tmp_path)
    sup = _supervisor(tmp_path, run, reflect=False)
    sup._finish("failed", "unverified")
    record = _json_record(run)
    assert [r["taskId"] for r in record["operatorRulings"]] == ["007"]
