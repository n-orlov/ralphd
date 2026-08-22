"""Black-box tests for the auto-resume crash-loop guard (task 028, issue #8,
PRD req F).

Self-recovery that never gives up is a crash loop: a run whose container dies
seconds after every resume would be resurrected by every `doctor --fix` tick
forever. The guard records `{attempts, lastAt, maxAttempts}` in the run dir,
spaces consecutive attempts by an escalating backoff, and after
`AUTO_RESUME_MAX_ATTEMPTS` attempts that never made progress stops touching
the run and leaves a reason readable from `ralphctl status`.

Uses the recording stub docker (test_cli_docker.Ctl) -- no real container, no
real engine, and no real sleeping: the backoff is exercised by back-dating the
recorded `lastAt`, never by waiting it out.
"""

from __future__ import annotations

import json

import pytest
from test_cli_docker import Ctl

from ralphd.cli.main import (
    AUTO_RESUME_BACKOFF_S,
    AUTO_RESUME_MAX_ATTEMPTS,
    _auto_resume_backoff_s,
)
from ralphd.engine.state import utc_from_epoch


@pytest.fixture
def ctl(tmp_path):
    return Ctl(tmp_path)


def _base_env(**extra) -> dict:
    return {"STUB_DOCKER_INSPECT_OK": "1", **extra}


def _start(ctl: Ctl, run_id: str, *extra: str) -> None:
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", run_id, *extra)
    assert res.returncode == 0, res.stderr


def _docker_runs(ctl: Ctl) -> list[list[str]]:
    return [a for a in ctl.recorded() if a[:2] == ["run", "-d"]]


def _kill_container(ctl: Ctl, run_id: str, iterations_used: int = 0) -> None:
    """The container vanished: status.json still records a non-terminal state
    and no container by that name exists (the stub never lists it)."""
    (ctl.registry / "runs" / run_id / "status.json").write_text(
        json.dumps({"state": "running", "schemaVersion": 1,
                    "iterationsUsed": iterations_used}))


def _doctor_fix(ctl: Ctl, *extra: str) -> dict:
    res = ctl.run("--json", "doctor", *extra, env=_base_env(
        STUB_DOCKER_CONTAINERS="some-unrelated-container"))
    assert res.stdout, res.stderr
    return json.loads(res.stdout)


def _guard_path(ctl: Ctl, run_id: str):
    return ctl.registry / "runs" / run_id / "auto-resume.json"


def _guard(ctl: Ctl, run_id: str) -> dict:
    return json.loads(_guard_path(ctl, run_id).read_text())


def _backdate(ctl: Ctl, run_id: str, seconds: int = 24 * 3600) -> None:
    """Pretend the recorded attempt happened `seconds` ago, so the crash-loop
    backoff has elapsed -- the tests never really sleep."""
    import time
    state = _guard(ctl, run_id)
    state["lastAt"] = utc_from_epoch(time.time() - seconds)
    _guard_path(ctl, run_id).write_text(json.dumps(state))


# ---------------------------------------------------- the record itself
def test_first_auto_resume_records_the_attempt(ctl):
    _start(ctl, "tst-guard-rec", "--auto-resume")
    _kill_container(ctl, "tst-guard-rec", iterations_used=3)

    doc = _doctor_fix(ctl, "--fix")
    assert doc["autoResume"]["resumed"] == ["tst-guard-rec"]

    state = _guard(ctl, "tst-guard-rec")
    assert state["attempts"] == 1
    assert state["maxAttempts"] == AUTO_RESUME_MAX_ATTEMPTS
    assert state["lastAt"], state
    assert state["gaveUp"] is False
    # the run's progress as of the attempt, so later progress can reset it
    assert state["iterationsUsed"] == 3


# ------------------------------------------------------------- backoff
def test_backoff_prevents_an_immediate_second_attempt(ctl):
    """Two `doctor --fix` sweeps back to back: the second one must not start a
    second container, and must say when it will try again."""
    _start(ctl, "tst-guard-bo", "--auto-resume")
    _kill_container(ctl, "tst-guard-bo")
    assert _doctor_fix(ctl, "--fix")["autoResume"]["resumed"] == ["tst-guard-bo"]
    runs_after_first = len(_docker_runs(ctl))

    _kill_container(ctl, "tst-guard-bo")        # died again, instantly
    doc = _doctor_fix(ctl, "--fix")
    assert doc["autoResume"]["resumed"] == []
    assert doc["autoResume"]["waiting"] == [
        {"runId": "tst-guard-bo", "attempts": 1,
         "nextAttemptAt": doc["autoResume"]["waiting"][0]["nextAttemptAt"]}]
    assert doc["autoResume"]["waiting"][0]["nextAttemptAt"]
    assert len(_docker_runs(ctl)) == runs_after_first, "no second container"
    assert _guard(ctl, "tst-guard-bo")["attempts"] == 1

    res = ctl.run("doctor", "--fix", env=_base_env(STUB_DOCKER_CONTAINERS="x"))
    assert "crash-loop backoff" in res.stdout, res.stdout

    # ... and once the backoff has elapsed it is retried
    _backdate(ctl, "tst-guard-bo")
    assert _doctor_fix(ctl, "--fix")["autoResume"]["resumed"] == ["tst-guard-bo"]
    assert len(_docker_runs(ctl)) == runs_after_first + 1


def test_backoff_schedule_escalates():
    assert _auto_resume_backoff_s(0) == 0
    schedule = [_auto_resume_backoff_s(n)
                for n in range(1, len(AUTO_RESUME_BACKOFF_S) + 1)]
    assert schedule == AUTO_RESUME_BACKOFF_S
    assert schedule == sorted(schedule) and schedule[0] < schedule[-1]
    # past the end the last (longest) value repeats
    assert _auto_resume_backoff_s(len(AUTO_RESUME_BACKOFF_S) + 5) == \
        AUTO_RESUME_BACKOFF_S[-1]


# -------------------------------------------------------- give up
def test_crash_loop_gives_up_and_the_reason_is_readable_from_status(ctl):
    """A run that dies instantly on every resume is not resurrected in a
    loop: attempts increment to maxAttempts, then `doctor --fix` stops
    touching it and `ralphctl status` explains why."""
    _start(ctl, "tst-guard-loop", "--auto-resume")
    for n in range(AUTO_RESUME_MAX_ATTEMPTS):
        _kill_container(ctl, "tst-guard-loop")   # same iterationsUsed: no progress
        doc = _doctor_fix(ctl, "--fix")
        assert doc["autoResume"]["resumed"] == ["tst-guard-loop"], n
        assert _guard(ctl, "tst-guard-loop")["attempts"] == n + 1
        _backdate(ctl, "tst-guard-loop")         # skip the wait, not the guard

    runs_at_give_up = len(_docker_runs(ctl))
    assert runs_at_give_up == 1 + AUTO_RESUME_MAX_ATTEMPTS   # start + attempts

    # the next sweep gives up -- and every sweep after it stays given up
    for _ in range(2):
        _kill_container(ctl, "tst-guard-loop")
        _backdate(ctl, "tst-guard-loop")
        doc = _doctor_fix(ctl, "--fix")
        assert doc["autoResume"]["resumed"] == []
        gave = doc["autoResume"]["gaveUp"]
        assert [g["runId"] for g in gave] == ["tst-guard-loop"], doc
        assert gave[0]["attempts"] == AUTO_RESUME_MAX_ATTEMPTS
        assert "gave up" in gave[0]["reason"]
        assert len(_docker_runs(ctl)) == runs_at_give_up, "resurrected in a loop"

    # still reported as dangling (never silently dropped)
    assert [d["runId"] for d in doc["danglingRegistryEntries"]] == ["tst-guard-loop"]

    state = _guard(ctl, "tst-guard-loop")
    assert state["gaveUp"] is True
    assert state["attempts"] == AUTO_RESUME_MAX_ATTEMPTS
    assert "crash loop" in state["reason"]

    # ... and the reason is readable from status, human and --json
    res = ctl.run("status", "tst-guard-loop", env=_base_env(
        STUB_DOCKER_CONTAINERS="none"))
    assert "auto-resume:" in res.stdout, res.stdout
    assert "gave up after" in res.stdout, res.stdout
    res = ctl.run("--json", "status", "tst-guard-loop", env=_base_env(
        STUB_DOCKER_CONTAINERS="none"))
    payload = json.loads(res.stdout)
    assert payload["autoResume"]["gaveUp"] is True
    assert payload["autoResume"]["maxAttempts"] == AUTO_RESUME_MAX_ATTEMPTS
    assert "crash loop" in payload["autoResume"]["reason"]

    report = ctl.run("doctor", "--fix", env=_base_env(
        STUB_DOCKER_CONTAINERS="none")).stdout
    assert "gave up after" in report, report
    assert "given up on" in report, report


def test_status_of_a_run_that_never_needed_recovery_says_nothing(ctl):
    _start(ctl, "tst-guard-quiet", "--auto-resume")
    _kill_container(ctl, "tst-guard-quiet")
    res = ctl.run("status", "tst-guard-quiet", env=_base_env(
        STUB_DOCKER_CONTAINERS="none"))
    assert "auto-resume" not in res.stdout, res.stdout
    payload = json.loads(ctl.run("--json", "status", "tst-guard-quiet", env=_base_env(
        STUB_DOCKER_CONTAINERS="none")).stdout)
    assert payload["autoResume"] is None


# ------------------------------------------------------------- reset
def test_progress_since_the_last_attempt_resets_the_counter(ctl):
    """A long-lived job recovered once, that then did real work and later died
    again, must not inherit the earlier attempt: the second incident starts
    from attempt 1 (and is therefore allowed immediately)."""
    _start(ctl, "tst-guard-reset", "--auto-resume")
    _kill_container(ctl, "tst-guard-reset", iterations_used=2)
    assert _doctor_fix(ctl, "--fix")["autoResume"]["resumed"] == ["tst-guard-reset"]
    assert _guard(ctl, "tst-guard-reset")["attempts"] == 1

    # the resumed run made progress, then died again -- no backdating: the
    # reset is what makes this attempt legal, not elapsed time
    _kill_container(ctl, "tst-guard-reset", iterations_used=9)
    doc = _doctor_fix(ctl, "--fix")
    assert doc["autoResume"]["resumed"] == ["tst-guard-reset"], doc
    state = _guard(ctl, "tst-guard-reset")
    assert state["attempts"] == 1, state
    assert state["iterationsUsed"] == 9


def test_opted_out_run_is_never_recorded_by_the_guard(ctl):
    # explicit opt-out: the default has been ON since v0.7 (task 027)
    _start(ctl, "tst-guard-off", "--no-auto-resume")
    _kill_container(ctl, "tst-guard-off")
    doc = _doctor_fix(ctl, "--fix")
    assert doc["autoResume"]["skipped"] == ["tst-guard-off"]
    assert not _guard_path(ctl, "tst-guard-off").exists()
