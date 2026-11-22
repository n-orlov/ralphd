"""v0.38 C: an operator status ruling the engine would refuse is refused by
`ralphctl amend --status` when it is recorded, and one the engine refuses
later (the task moved after recording) stays visible.

C.1 (the door): `amend <run> <task> --status S` loads the authoritative task
record (`<config dir>/record/tasks.json`; never the run dir's mirror, not even when the record is unreadable) and asks the engine's own legality
check (`state.task_status_change_refusal`: the transition table, the operator
exits of A.2 and the cure-reopened guard of A.3). An illegal ruling exits 2
with the engine's reason and writes no ruling file; every legal case is
recorded. The legal-ruling tests are the controls: they pass with or without
the check.

C.2 (a later refusal): the engine's apply-time refusal emits
`amendment.refused` (ruling file, task, reason); `amend --list` shows that
ruling as `refused: <reason>`; `ralphctl status` prints one line naming it
until a later ruling for that task is applied.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from test_cli_docker import ctl  # noqa: F401  (fixture)

from ralphd.engine import loop as loop_mod
from ralphd.engine.state import (AMENDMENT_REFUSED_EVENT, atomic_write_json,
                                 outstanding_refused_rulings, write_amendment)
from tests.conftest import make_supervisor

RUN = "tst-door"
ILLEGAL_REASON = "illegal transition"


def _seed(ctl, tasks, *, vigilant=False, pre_cure=None, mirror=None):
    """A run on the host: authoritative record under the config dir, the run
    dir's `tasks.json` mirror (a different status when `mirror` is given),
    `job.yaml` for the variant, and `status.json`."""
    cdir = ctl.registry / "configs" / RUN
    rdir = ctl.registry / "runs" / RUN
    (cdir / "record").mkdir(parents=True)
    rdir.mkdir(parents=True)
    atomic_write_json(cdir / "record" / "tasks.json",
                      {"version": 1, "tasks": tasks})
    atomic_write_json(rdir / "tasks.json",
                      {"version": 1, "tasks": mirror or tasks})
    (cdir / "job.yaml").write_text(
        f"vigilant: {'true' if vigilant else 'false'}\n")
    status = {"runId": RUN, "state": "paused"}
    if pre_cure is not None:
        status["preCureStatuses"] = pre_cure
    atomic_write_json(rdir / "status.json", status)
    return cdir, rdir


def _task(status, tid="001"):
    return {"id": tid, "title": "t", "status": status,
            "successCriteria": "c", "validationAttempts": 0}


def _ruling_files(cdir: Path) -> list[str]:
    adir = cdir / "amendments"
    return sorted(p.name for p in adir.glob("*")) if adir.is_dir() else []


# -- C.1: refused at the door -------------------------------------------------

@pytest.mark.parametrize("vigilant", [False, True])
@pytest.mark.parametrize("current,target", [("completed", "pending"),
                                            ("in-progress", "pending"),
                                            ("petitioned", "completed")])
def test_an_illegal_ruling_is_refused_and_no_file_is_written(
        ctl, current, target, vigilant):
    cdir, _ = _seed(ctl, [_task(current)], vigilant=vigilant)
    res = ctl.run("amend", RUN, "001", "--status", target, "because")
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert ILLEGAL_REASON in res.stderr
    assert current in res.stderr and target in res.stderr
    assert _ruling_files(cdir) == []


def test_a_pending_to_validated_ruling_without_a_cure_reopen_is_refused_with_the_engines_reason(
        ctl):
    cdir, _ = _seed(ctl, [_task("pending")], vigilant=True)
    res = ctl.run("amend", RUN, "001", "--status", "validated", "done")
    assert res.returncode == 2
    assert "cure-reconciliation" in res.stderr
    assert _ruling_files(cdir) == []


def test_a_failed_to_skipped_ruling_with_no_justification_is_refused(ctl):
    cdir, _ = _seed(ctl, [_task("failed")])
    res = ctl.run("amend", RUN, "001", "--status", "skipped")
    assert res.returncode == 2
    assert "justification" in res.stderr
    assert _ruling_files(cdir) == []


def test_the_door_reads_the_authoritative_record_not_the_run_dir_mirror(ctl):
    # record: completed (completed -> pending is illegal); mirror: failed.
    cdir, _ = _seed(ctl, [_task("completed")], mirror=[_task("in-progress")])
    res = ctl.run("amend", RUN, "001", "--status", "pending", "again")
    assert res.returncode == 2
    assert "'completed'" in res.stderr
    assert _ruling_files(cdir) == []


def _deny_record(cdir: Path, how: str):
    """Make the authoritative record unreadable to this (host) user the way
    a live run does (`record/` root-owned 0700: here, mode 000 on our own
    directory), or by damaging it. Returns the undo."""
    rec_dir = cdir / "record"
    if how == "denied":
        if os.geteuid() == 0:
            pytest.skip("root reads through mode 000")
        rec_dir.chmod(0)
        return lambda: rec_dir.chmod(0o700)
    (rec_dir / "tasks.json").write_text("{not json")
    return lambda: None


@pytest.mark.parametrize("how", ["denied", "damaged"])
def test_an_unreadable_record_never_lets_the_mirror_refuse_a_legal_ruling(
        ctl, how):
    # record: petitioned (petitioned -> skipped is a legal operator exit);
    # mirror: pending (pending -> skipped would be refused). The mirror is
    # agent-writable and may be stale, so it must not decide the ruling.
    cdir, _ = _seed(ctl, [_task("petitioned")], mirror=[_task("pending")])
    undo = _deny_record(cdir, how)
    try:
        res = ctl.run("amend", RUN, "001", "--status", "skipped", "moot")
    finally:
        undo()
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "UNCHECKED" in res.stderr
    assert len(_ruling_files(cdir)) == 1


@pytest.mark.parametrize("how", ["denied", "damaged"])
def test_an_unreadable_record_records_the_ruling_unchecked_and_says_so(
        ctl, how):
    # Mirror and record agree on a status the ruling is illegal from: with
    # the record unreadable there is no authoritative status to refuse on,
    # so the ruling is recorded and the engine's apply-time check (C.2's
    # `amendment.refused`) is named as what decides it.
    cdir, _ = _seed(ctl, [_task("completed")])
    undo = _deny_record(cdir, how)
    try:
        res = ctl.run("amend", RUN, "001", "--status", "pending", "again")
    finally:
        undo()
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "UNCHECKED" in res.stderr and "--list" in res.stderr
    assert len(_ruling_files(cdir)) == 1


def test_without_a_record_mount_the_door_reads_the_run_dirs_protected_record(
        ctl):
    # No `record/` mount: the engine keeps the record in the run dir's
    # `.tasks-protected/` (privsep's degrade), which beats the mirror.
    cdir, rdir = _seed(ctl, [_task("completed")], mirror=[_task("failed")])
    (cdir / "record" / "tasks.json").unlink()
    (cdir / "record").rmdir()
    (rdir / ".tasks-protected").mkdir()
    atomic_write_json(rdir / ".tasks-protected" / "tasks.json",
                      {"version": 1, "tasks": [_task("completed")]})
    res = ctl.run("amend", RUN, "001", "--status", "pending", "again")
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert "'completed'" in res.stderr
    assert _ruling_files(cdir) == []


@pytest.mark.parametrize("vigilant", [False, True])
@pytest.mark.parametrize("target", ["validated", "skipped"])
def test_control_a_petitioned_task_ruling_exit_is_recorded(
        ctl, target, vigilant):
    """A.2: `petitioned -> validated|skipped` by an operator ruling."""
    cdir, _ = _seed(ctl, [_task("petitioned")], vigilant=vigilant)
    res = ctl.run("amend", RUN, "001", "--status", target, "operator ruling")
    assert res.returncode == 0, res.stderr
    assert len(_ruling_files(cdir)) == 1


@pytest.mark.parametrize("vigilant", [False, True])
def test_control_a_ruling_on_a_cure_reopened_task_is_recorded(ctl, vigilant):
    """A.3: `pending -> validated` on a task a cure pass re-opened."""
    cdir, _ = _seed(ctl, [_task("pending")], vigilant=vigilant,
                    pre_cure={"001": "validated"})
    res = ctl.run("amend", RUN, "001", "--status", "validated", "restore it")
    assert res.returncode == 0, res.stderr
    assert len(_ruling_files(cdir)) == 1


def test_control_a_ruling_to_the_status_the_task_holds_is_recorded(ctl):
    cdir, _ = _seed(ctl, [_task("skipped")])
    res = ctl.run("amend", RUN, "001", "--status", "skipped", "already so")
    assert res.returncode == 0, res.stderr
    assert len(_ruling_files(cdir)) == 1


# -- C.2: a refusal the engine makes later is visible --------------------------

def _engine_with_refused_ruling(ctl, monkeypatch, *, task_status="pending"):
    """A ruling recorded while it was legal (`petitioned -> skipped`), after
    which the task moved: the engine refuses it when it applies it."""
    cdir, rdir = _seed(ctl, [_task("petitioned")])
    fname = write_amendment(cdir, "001", "stand it down", status="skipped")
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", cdir)
    sup = make_supervisor(ctl.tmp, run_id=RUN, run_root=rdir, tasks=[
        _task(task_status)], vigilant=False,
        notifier_url="http://notifier.example/notify")
    sup._apply_operator_status_rulings()
    return sup, cdir, rdir, fname


def _events(rdir: Path, kind: str) -> list[dict]:
    path = rdir / "events.jsonl"
    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    return [e for e in rows if e.get("type") == kind]


def test_a_ruling_the_engine_refuses_at_apply_time_emits_amendment_refused(
        ctl, monkeypatch):
    sup, _cdir, rdir, fname = _engine_with_refused_ruling(ctl, monkeypatch)
    refused = _events(rdir, AMENDMENT_REFUSED_EVENT)
    assert AMENDMENT_REFUSED_EVENT == "amendment.refused"
    assert len(refused) == 1, refused
    assert refused[0]["file"] == fname
    assert refused[0]["taskId"] == "001"
    assert refused[0]["reason"]
    assert refused[0]["previous"] == "pending"
    # reported once, not once per boundary
    sup._apply_operator_status_rulings()
    assert len(_events(rdir, AMENDMENT_REFUSED_EVENT)) == 1
    assert sup.run.read_tasks()["tasks"][0]["status"] == "pending"


def test_control_an_applied_ruling_emits_no_amendment_refused(ctl, monkeypatch):
    _sup, _cdir, rdir, _f = _engine_with_refused_ruling(
        ctl, monkeypatch, task_status="petitioned")
    assert _events(rdir, AMENDMENT_REFUSED_EVENT) == []
    assert len(_events(rdir, "amendment_status_applied")) == 1


def test_amend_list_shows_the_refused_ruling_with_the_engines_reason(
        ctl, monkeypatch):
    _sup, _cdir, rdir, fname = _engine_with_refused_ruling(ctl, monkeypatch)
    reason = _events(rdir, AMENDMENT_REFUSED_EVENT)[0]["reason"]
    res = ctl.run("amend", RUN, "--list")
    assert res.returncode == 0, res.stderr
    assert f"refused: {reason}" in res.stdout
    doc = json.loads(ctl.run("--json", "amend", RUN, "--list").stdout)
    assert doc["entries"][0]["refused"] == reason


def test_control_amend_list_shows_no_refusal_for_an_applied_ruling(
        ctl, monkeypatch):
    _engine_with_refused_ruling(ctl, monkeypatch, task_status="petitioned")
    res = ctl.run("amend", RUN, "--list")
    assert res.returncode == 0, res.stderr
    assert "refused:" not in res.stdout


def test_status_prints_one_line_naming_the_refused_ruling(ctl, monkeypatch):
    _sup, _cdir, rdir, fname = _engine_with_refused_ruling(ctl, monkeypatch)
    reason = _events(rdir, AMENDMENT_REFUSED_EVENT)[0]["reason"]
    res = ctl.run("status", RUN)
    assert res.returncode == 0, res.stderr
    lines = [ln for ln in res.stdout.splitlines() if fname in ln]
    assert len(lines) == 1, res.stdout
    assert "001" in lines[0] and reason in lines[0]
    doc = json.loads(ctl.run("--json", "status", RUN).stdout)
    assert [r["file"] for r in doc["refusedRulings"]] == [fname]


def test_status_names_the_refusal_until_a_later_ruling_for_the_task_is_applied(
        ctl, monkeypatch):
    sup, cdir, rdir, fname = _engine_with_refused_ruling(ctl, monkeypatch)
    assert [r["file"] for r in outstanding_refused_rulings(rdir)] == [fname]
    # the operator rules again, legally from where the task now stands
    write_amendment(cdir, "001", "hand it back", status="in-progress")
    sup._apply_operator_status_rulings()
    assert sup.run.read_tasks()["tasks"][0]["status"] == "in-progress"
    assert outstanding_refused_rulings(rdir) == []
    res = ctl.run("status", RUN)
    assert fname not in res.stdout
    assert json.loads(ctl.run("--json", "status", RUN).stdout)[
        "refusedRulings"] == []


def _engine_with_pending_task(ctl, monkeypatch, *, pre_cure=None):
    cdir, rdir = _seed(ctl, [_task("pending")], pre_cure=pre_cure)
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", cdir)
    status = {"preCureStatuses": pre_cure} if pre_cure else None
    sup = make_supervisor(ctl.tmp, run_id=RUN, run_root=rdir, tasks=[
        _task("pending")], vigilant=False, status=status,
        notifier_url="http://notifier.example/notify")
    return sup, cdir, rdir


def _status_clear(ctl, fname):
    res = ctl.run("status", RUN)
    assert res.returncode == 0, res.stderr
    assert fname not in res.stdout, res.stdout
    assert json.loads(ctl.run("--json", "status", RUN).stdout)[
        "refusedRulings"] == []


def test_status_stays_clear_when_the_engine_retries_a_superseded_ruling(
        ctl, monkeypatch):
    """The later ruling ends the warning for good: the engine re-attempts the
    older ruling at every boundary, and refusing it again from the task's new
    status does not bring the warning back."""
    sup, cdir, rdir = _engine_with_pending_task(ctl, monkeypatch)
    old = write_amendment(cdir, "001", "call it done", status="validated")
    sup._apply_operator_status_rulings()
    assert [r["file"] for r in outstanding_refused_rulings(rdir)] == [old]
    write_amendment(cdir, "001", "hand it back", status="in-progress")
    sup._apply_operator_status_rulings()
    assert sup.run.read_tasks()["tasks"][0]["status"] == "in-progress"
    # the next boundary retries `old` from in-progress, still illegal
    sup._apply_operator_status_rulings()
    assert sup.run.read_tasks()["tasks"][0]["status"] == "in-progress"
    assert outstanding_refused_rulings(rdir) == []
    assert len(_events(rdir, AMENDMENT_REFUSED_EVENT)) == 1
    _status_clear(ctl, old)


def test_a_refusal_logged_again_after_a_later_ruling_applied_stays_cleared(ctl):
    """The reader alone: a refusal of an older ruling file written after a
    later ruling for that task applied is still superseded."""
    _cdir, rdir = _seed(ctl, [_task("in-progress")])
    rows = [
        {"type": AMENDMENT_REFUSED_EVENT, "file": "001-001.md",
         "taskId": "001", "previous": "pending", "status": "validated",
         "reason": ILLEGAL_REASON},
        {"type": "amendment_status_applied", "file": "002-001.md",
         "taskId": "001", "previous": "pending", "status": "in-progress",
         "source": "operator"},
        {"type": AMENDMENT_REFUSED_EVENT, "file": "001-001.md",
         "taskId": "001", "previous": "in-progress", "status": "validated",
         "reason": ILLEGAL_REASON},
    ]
    (rdir / "events.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows))
    assert outstanding_refused_rulings(rdir) == []
    _status_clear(ctl, "001-001.md")


def test_an_earlier_ruling_applied_after_a_later_refusal_does_not_clear_it(ctl):
    """An earlier ruling re-applied after a later one was refused does not
    clear the later one: it is not a later ruling."""
    _cdir, rdir = _seed(ctl, [_task("in-progress")])
    rows = [
        {"type": AMENDMENT_REFUSED_EVENT, "file": "002-001.md",
         "taskId": "001", "previous": "pending", "status": "validated",
         "reason": ILLEGAL_REASON},
        {"type": "amendment_status_applied", "file": "001-001.md",
         "taskId": "001", "previous": "pending", "status": "in-progress",
         "source": "operator"},
    ]
    (rdir / "events.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows))
    assert [r["file"] for r in outstanding_refused_rulings(rdir)] == [
        "002-001.md"]


def test_a_later_cure_reopen_ruling_clears_the_refusal(ctl, monkeypatch):
    """A later ruling that restores a cure-reopened task (A.3) takes effect as
    `task.un_reopened` (source operator, naming its ruling file) and ends the
    warning about the older refused ruling."""
    sup, cdir, rdir = _engine_with_pending_task(
        ctl, monkeypatch, pre_cure={"001": "validated"})
    old = write_amendment(cdir, "001", "send it to verify",
                          status="awaiting-validation")
    sup._apply_operator_status_rulings()
    assert [r["file"] for r in outstanding_refused_rulings(rdir)] == [old]
    later = write_amendment(cdir, "001", "restore it", status="validated")
    sup._apply_operator_status_rulings()
    sup._apply_operator_status_rulings()
    assert sup.run.read_tasks()["tasks"][0]["status"] == "validated"
    restored = _events(rdir, "task.un_reopened")
    assert [(e["source"], e["file"]) for e in restored] == [
        ("operator", later)]
    assert outstanding_refused_rulings(rdir) == []
    _status_clear(ctl, old)


def test_control_status_prints_no_refusal_line_for_a_run_without_one(ctl):
    _seed(ctl, [_task("petitioned")])
    res = ctl.run("status", RUN)
    assert res.returncode == 0, res.stderr
    assert "was refused" not in res.stdout
