"""Auto-resume must never resurrect a run the operator killed (task 029,
issue #8, PRD req F).

Self-recovery only ever restarts a run whose container *vanished on its own*.
Three fixtures make the distinction explicit:

* a **terminal** run (succeeded/failed/aborted) with no container -- not
  dangling at all, so `doctor --fix` never even considers it;
* an **operator-aborted** run: `operator-termination.json` records
  `action: abort` while status.json may still say `running` (the container
  died before the engine wrote its terminal state);
* an **operator-stopped** run: `ralphctl stop --force` removes the container
  itself, which on disk is otherwise indistinguishable from a crash.

The control case in every test is the same fixture *without* the marker, which
is resumed -- so the assertions cannot pass by accident (e.g. because the
sweep silently stopped resuming anything).

Uses the recording stub docker (test_cli_docker.Ctl): no real container, no
real engine.
"""

from __future__ import annotations

import json

import pytest
from test_cli_docker import Ctl

from ralphd.engine.state import read_operator_termination

EMPTY_SWEEP = {"resumed": [], "skipped": [], "failed": [], "waiting": [],
               "gaveUp": [], "operatorTerminated": [], "recovered": []}


@pytest.fixture
def ctl(tmp_path):
    return Ctl(tmp_path)


def _base_env(**extra) -> dict:
    return {"STUB_DOCKER_INSPECT_OK": "1", **extra}


def _start(ctl: Ctl, run_id: str, *extra: str) -> None:
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", run_id, "--auto-resume", *extra)
    assert res.returncode == 0, res.stderr


def _docker_runs(ctl: Ctl) -> list[list[str]]:
    return [a for a in ctl.recorded() if a[:2] == ["run", "-d"]]


def _write_status(ctl: Ctl, run_id: str, state: str) -> None:
    (ctl.registry / "runs" / run_id / "status.json").write_text(
        json.dumps({"state": state, "schemaVersion": 1, "iterationsUsed": 3}))


def _mark_terminated(ctl: Ctl, run_id: str, action: str) -> None:
    """What `ralphctl abort`/`stop` (and the engine's own POST /abort
    handler) leave behind."""
    (ctl.registry / "runs" / run_id / "operator-termination.json").write_text(
        json.dumps({"action": action, "at": "2026-01-01T00:00:00Z",
                    "reason": f"{action}ed by operator", "source": "cli"}))


def _doctor_fix(ctl: Ctl, *extra: str) -> dict:
    res = ctl.run("--json", "doctor", *extra, env=_base_env(
        STUB_DOCKER_CONTAINERS="some-unrelated-container"))
    assert res.stdout, res.stderr
    return json.loads(res.stdout)


# ------------------------------------------------------------- terminal
@pytest.mark.parametrize("state", ["succeeded", "failed", "aborted"])
def test_fix_never_resumes_a_terminal_run(ctl, state):
    """A finished run with no container is not a zombie: nothing to report,
    nothing to restart -- even with auto_resume on."""
    _start(ctl, "tst-term")
    _write_status(ctl, "tst-term", state)

    doc = _doctor_fix(ctl, "--fix")
    assert doc["danglingRegistryEntries"] == []
    assert doc["autoResume"] == EMPTY_SWEEP
    assert len(_docker_runs(ctl)) == 1, "terminal run must not be resumed"


def test_fix_skips_a_run_that_went_terminal_during_the_sweep(ctl):
    """The scan-to-resume window: doctor listed the run as dangling, then it
    reached a terminal state before the resume was issued. Resuming it then
    would restart a job that had already succeeded, so the dangling
    condition is re-asked immediately before the restart."""
    import argparse

    from ralphd.cli import main as cli

    _start(ctl, "tst-race")
    _write_status(ctl, "tst-race", "succeeded")
    dangling = [{"runId": "tst-race", "container": "ralphd-tst-race"}]
    args = argparse.Namespace(image="ralphd:dev", json=False, fix=True)
    with pytest.MonkeyPatch.context() as mp:
        # only the registry matters: a terminal recorded state short-circuits
        # the dangling check before it ever asks docker anything
        mp.setenv("RALPHD_REGISTRY", str(ctl.registry))
        result = cli._auto_resume_dangling(args, dangling)
    assert result["recovered"] == ["tst-race"]
    assert result["resumed"] == []
    assert len(_docker_runs(ctl)) == 1


# --------------------------------------------------- operator-terminated
@pytest.mark.parametrize("action", ["abort", "stop"])
def test_fix_never_resumes_an_operator_terminated_run(ctl, action):
    _start(ctl, "tst-opterm")
    _write_status(ctl, "tst-opterm", "running")     # container vanished shape
    _mark_terminated(ctl, "tst-opterm", action)

    doc = _doctor_fix(ctl, "--fix")
    # still reported (the operator can see the leftover entry) ...
    assert doc["danglingRegistryEntries"] == [
        {"runId": "tst-opterm", "container": "ralphd-tst-opterm"}]
    # ... but classified as operator-terminated, not resumed or "opted out"
    assert doc["autoResume"]["resumed"] == []
    assert doc["autoResume"]["skipped"] == []
    assert doc["autoResume"]["operatorTerminated"] == [
        {"runId": "tst-opterm", "action": action,
         "at": "2026-01-01T00:00:00Z", "reason": f"{action}ed by operator"}]
    assert len(_docker_runs(ctl)) == 1, "operator-killed run must not be resumed"


def test_human_report_says_why_an_operator_terminated_run_was_left_alone(ctl):
    _start(ctl, "tst-opterm-say")
    _write_status(ctl, "tst-opterm-say", "running")
    _mark_terminated(ctl, "tst-opterm-say", "stop")
    res = ctl.run("doctor", "--fix", env=_base_env(
        STUB_DOCKER_CONTAINERS="none"))
    assert "terminated by the operator" in res.stdout, res.stdout
    assert "operator-terminated" in res.stdout, res.stdout


def test_the_same_fixture_without_the_marker_is_resumed(ctl):
    """The control: identical run dir, marker removed -> resumed. Proves the
    two tests above are the marker's doing, not a broken sweep."""
    _start(ctl, "tst-noterm")
    _write_status(ctl, "tst-noterm", "running")

    doc = _doctor_fix(ctl, "--fix")
    assert doc["autoResume"]["resumed"] == ["tst-noterm"]
    assert doc["autoResume"]["operatorTerminated"] == []
    assert len(_docker_runs(ctl)) == 2


# ------------------------------------------------- stop writes the marker
def test_stop_force_records_the_marker_and_blocks_auto_resume(ctl):
    """End-to-end for the sharpest case: `stop --force` removes the container
    of a run recorded `running`. Without the marker the run dir is exactly
    the crashed-container shape and `doctor --fix` would restart it."""
    _start(ctl, "tst-stopped")
    _write_status(ctl, "tst-stopped", "running")

    res = ctl.run("stop", "tst-stopped", "--force")
    assert res.returncode == 0, res.stderr
    assert ["rm", "-f", "ralphd-tst-stopped"] in ctl.recorded()

    term = read_operator_termination(ctl.registry / "runs" / "tst-stopped")
    assert term is not None and term["action"] == "stop"
    assert term["source"] == "cli"

    doc = _doctor_fix(ctl, "--fix")
    assert [t["runId"] for t in doc["autoResume"]["operatorTerminated"]] == [
        "tst-stopped"]
    assert doc["autoResume"]["resumed"] == []
    assert len(_docker_runs(ctl)) == 1


def test_marker_reader_ignores_a_malformed_file(ctl):
    _start(ctl, "tst-junk")
    (ctl.registry / "runs" / "tst-junk" / "operator-termination.json").write_text(
        "not json")
    assert read_operator_termination(ctl.registry / "runs" / "tst-junk") is None
    _write_status(ctl, "tst-junk", "running")
    doc = _doctor_fix(ctl, "--fix")
    # unreadable marker must not silently block recovery
    assert doc["autoResume"]["resumed"] == ["tst-junk"]
