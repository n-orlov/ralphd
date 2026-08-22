"""Task 021 (issue #31): liveness has three states, so an exited-but-present
container is a dangling run.

`_container_running` returned `None` only for "no such container" and `False`
for "exists but has exited", and `_dangling_run_entry` bailed on `if
_container_running(name) is not None` -- i.e. it read "a container record
exists" as "something is alive there". So the shape *every* engine death
leaves behind (the container is still listed, stopped) was reported as a
healthy run by all four of that helper's callers at once: `status`, `doctor`,
`repair` and the `doctor --fix` auto-resume sweep.

The fix is at the shared helper (`_container_liveness` +
`_dangling_run_entry`), never in the callers -- but the point of the bug is
that a caller misread the helper's return value, so this module asserts on
**each of the four surfaces** as well as on the helper.

Two tiers, no real container and no real engine:
- in-process tables over the helpers, with `sh` stubbed to spell docker's
  three answers;
- black-box `ralphctl status` / `doctor` / `doctor --fix` / `repair` over
  on-disk run dirs, with the recording stub docker's
  STUB_DOCKER_CONTAINERS/STUB_DOCKER_RUNNING deciding liveness.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run

from ralphd.cli.main import (CONTAINER_ABSENT, CONTAINER_EXITED,
                             CONTAINER_LIVENESS_STATES, CONTAINER_RUNNING,
                             _container_liveness, _container_running,
                             _dangling_run_entry)

__all__ = ["ctl", "unix_sock"]

REPO = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ helpers
def _stub_docker(monkeypatch, answer: str | None):
    """`docker inspect --format {{.State.Running}}` answering `true`/`false`,
    or failing (exit 1) for a container that does not exist."""
    def fake_sh(cmd, **kw):
        assert "{{.State.Running}}" in cmd, cmd
        if answer is None:
            return subprocess.CompletedProcess(cmd, 1, "", "No such object")
        return subprocess.CompletedProcess(cmd, 0, answer + "\n", "")
    monkeypatch.setattr("ralphd.cli.main.sh", fake_sh)


def _seed_status(ctl: Ctl, run_id: str, state: str = "running") -> Path:
    rdir, _cdir = _seed_run(ctl, run_id)
    (rdir / "status.json").write_text(json.dumps(
        {"state": state, "schemaVersion": 1, "runId": run_id,
         "iterationsUsed": 3, "startedAt": "2024-01-01T00:00:00Z",
         "updatedAt": "2024-01-01T00:10:00Z"}))
    return rdir


def _exited_env(run_id: str) -> dict:
    """The container exists and is stopped -- the shape an engine death
    leaves behind."""
    return {"STUB_DOCKER_CONTAINERS": f"ralphd-{run_id}",
            "STUB_DOCKER_RUNNING": ""}


# ------------------------------------------------------- tier 1: the helper
_LIVENESS = [
    (None, CONTAINER_ABSENT, None),      # docker inspect fails: no container
    ("true", CONTAINER_RUNNING, True),   # exists, running
    ("false", CONTAINER_EXITED, False),  # exists, exited  <-- #31
]


@pytest.mark.parametrize("answer,liveness,boolean", _LIVENESS)
def test_container_liveness_has_three_states(monkeypatch, answer, liveness,
                                             boolean):
    _stub_docker(monkeypatch, answer)
    assert _container_liveness("ralphd-x") == liveness
    # the boolean view is derived from it, never a second docker reading
    assert _container_running("ralphd-x") is boolean


def test_the_three_states_are_named_in_one_place():
    assert CONTAINER_LIVENESS_STATES == (CONTAINER_ABSENT, CONTAINER_RUNNING,
                                         CONTAINER_EXITED)
    assert len(set(CONTAINER_LIVENESS_STATES)) == 3


@pytest.mark.parametrize("answer,liveness,_boolean", _LIVENESS)
@pytest.mark.parametrize("state", ["starting", "running"])
def test_dangling_entry_matches_every_non_running_liveness(
        tmp_path, monkeypatch, answer, liveness, _boolean, state):
    """A non-terminal recorded state plus anything other than a *running*
    container is the dangling condition."""
    reg = tmp_path / "registry"
    (reg / "runs" / "z").mkdir(parents=True)
    (reg / "runs" / "z" / "status.json").write_text(json.dumps({"state": state}))
    monkeypatch.setenv("RALPHD_REGISTRY", str(reg))
    _stub_docker(monkeypatch, answer)
    entry = _dangling_run_entry("z")
    if liveness == CONTAINER_RUNNING:
        assert entry is None
    else:
        assert entry == {"runId": "z", "container": "ralphd-z",
                         "liveness": liveness}


@pytest.mark.parametrize("answer", [None, "true", "false"])
def test_a_terminal_run_is_never_dangling(tmp_path, monkeypatch, answer):
    reg = tmp_path / "registry"
    (reg / "runs" / "z").mkdir(parents=True)
    (reg / "runs" / "z" / "status.json").write_text(
        json.dumps({"state": "succeeded"}))
    monkeypatch.setenv("RALPHD_REGISTRY", str(reg))
    _stub_docker(monkeypatch, answer)
    assert _dangling_run_entry("z") is None


def test_the_condition_is_decided_at_the_helper_not_in_the_callers():
    """#31's shape: one helper, four blind callers. The liveness comparison
    lives in `_dangling_run_entry`; no caller re-derives it."""
    src = (REPO / "src" / "ralphd" / "cli" / "main.py").read_text()
    assert src.count("def _container_liveness(") == 1
    assert src.count("def _dangling_run_entry(") == 1
    # None of the four callers decides liveness for itself: each one asks
    # `_dangling_run_entry`, and none of them mentions the running state or
    # calls the liveness helper directly (#31 was one misread return value
    # blinding all four at once).
    for fn in ("def cmd_status(", "def _diagnose_dangling_container(",
               "def cmd_repair(", "def _auto_resume_dangling("):
        assert src.count(fn) == 1, fn
        caller = src.split(fn)[1].split("\ndef ")[0]
        assert "_dangling_run_entry(" in caller, fn
        assert "_container_liveness(" not in caller, fn
        assert "CONTAINER_RUNNING" not in caller, fn
    body = src.split("def _dangling_run_entry(")[1].split("\ndef ")[0]
    assert "_container_liveness(" in body
    assert "== CONTAINER_RUNNING" in body
    assert "== CONTAINER_RUNNING" in body


# --------------------------------------------------- tier 2: the 4 surfaces
def test_status_reports_an_exited_container_as_dangling(ctl: Ctl):
    """Surface 1 of 4: `ralphctl status`."""
    _seed_status(ctl, "tst-x-status")
    res = ctl.run("status", "tst-x-status", env=_exited_env("tst-x-status"))
    assert res.returncode == 0, res.stderr
    assert "container: ralphd-tst-x-status still exists but has exited" \
        in res.stdout
    assert "records state 'running'" in res.stdout
    assert "ralphctl repair tst-x-status" in res.stdout
    # ... and it stops ticking, like any other dangling run
    assert "(since last update)" in res.stdout
    assert "(elapsed)" not in res.stdout

    doc = json.loads(ctl.run("--json", "status", "tst-x-status",
                             env=_exited_env("tst-x-status")).stdout)
    assert doc["dangling"] is True
    assert doc["containerLiveness"] == CONTAINER_EXITED
    # `containerGone` keeps its narrower pre-#31 meaning: nothing vanished
    assert doc["containerGone"] is False
    assert doc["sinceLastUpdateSeconds"] is not None


def test_doctor_reports_an_exited_container_as_dangling(ctl: Ctl):
    """Surface 2 of 4: `ralphctl doctor`'s registry sweep."""
    _seed_status(ctl, "tst-x-doctor")
    env = {"STUB_DOCKER_INSPECT_OK": "1", **_exited_env("tst-x-doctor")}
    res = ctl.run("--json", "doctor", env=env)
    doc = json.loads(res.stdout)
    assert doc["danglingRegistryEntries"] == [
        {"runId": "tst-x-doctor", "container": "ralphd-tst-x-doctor",
         "liveness": CONTAINER_EXITED}]

    human = ctl.run("doctor", env=env).stdout
    assert "no live container:" in human
    assert "tst-x-doctor  container=ralphd-tst-x-doctor (exited)" in human
    assert "exited without the engine recording a terminal state" in human
    assert "ralphctl resume tst-x-doctor" in human


def test_repair_reports_an_exited_container_as_dangling(ctl: Ctl):
    """Surface 3 of 4: `ralphctl repair`'s per-run diagnosis."""
    rdir = _seed_status(ctl, "tst-x-repair")
    res = ctl.run("--json", "repair", "tst-x-repair",
                  env=_exited_env("tst-x-repair"))
    assert res.returncode == 1, res.stderr
    doc = json.loads(res.stdout)
    assert doc["ok"] is False
    assert doc["dangling"] == {"runId": "tst-x-repair",
                               "container": "ralphd-tst-x-repair",
                               "liveness": CONTAINER_EXITED}
    joined = "\n".join(doc["issues"])
    assert "ralphd-tst-x-repair exists but has exited" in joined
    assert "'running'" in joined
    assert "repair tst-x-repair --set-state aborted" in joined
    assert "resume tst-x-repair" in joined
    assert rdir.joinpath("events.jsonl").is_file()


def test_repair_set_state_records_why_for_an_exited_container(ctl: Ctl):
    """The recorded `reason` says what actually happened to the container --
    not that it vanished, since it is still there to inspect."""
    rdir = _seed_status(ctl, "tst-x-setstate")
    res = ctl.run("--json", "repair", "tst-x-setstate", "--set-state",
                  "aborted", env=_exited_env("tst-x-setstate"))
    assert res.returncode == 0, res.stderr
    reason = json.loads(res.stdout)["reason"]
    assert "ralphd-tst-x-setstate exists but has exited" in reason
    assert "no longer exists" not in reason
    assert json.loads((rdir / "status.json").read_text())["reason"] == reason


def test_auto_resume_resumes_a_run_whose_container_exited(ctl: Ctl):
    """Surface 4 of 4: the `doctor --fix` sweep. An engine that died inside
    its container is exactly what auto-resume is for."""
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tst-x-fix", "--auto-resume")
    assert res.returncode == 0, res.stderr
    (ctl.registry / "runs" / "tst-x-fix" / "status.json").write_text(
        json.dumps({"state": "running", "schemaVersion": 1,
                    "iterationsUsed": 3}))

    env = {"STUB_DOCKER_INSPECT_OK": "1", **_exited_env("tst-x-fix")}
    doc = json.loads(ctl.run("--json", "doctor", "--fix", env=env).stdout)
    assert doc["autoResume"]["resumed"] == ["tst-x-fix"]
    assert doc["autoResume"]["recovered"] == []
    assert doc["danglingRegistryEntries"] == [
        {"runId": "tst-x-fix", "container": "ralphd-tst-x-fix",
         "liveness": CONTAINER_EXITED}]
    runs = [a for a in ctl.recorded() if a[:2] == ["run", "-d"]]
    assert len(runs) == 2, runs          # the start + the auto-resume
    assert "ralphd-tst-x-fix" in runs[1]
    # the stale container occupying the name is removed first (`resume` does
    # this, which is why the auto-resume path needed no change of its own)
    assert ["rm", "-f", "ralphd-tst-x-fix"] in ctl.recorded()


def test_a_live_container_is_still_never_dangling_on_any_surface(ctl: Ctl):
    """The other side of the fix: a *running* container keeps every surface
    quiet (and `repair` still refuses it outright)."""
    _seed_status(ctl, "tst-x-live")
    live = {"STUB_DOCKER_CONTAINERS": "ralphd-tst-x-live",
            "STUB_DOCKER_RUNNING": "ralphd-tst-x-live"}
    status = json.loads(ctl.run("--json", "status", "tst-x-live",
                                env=live).stdout)
    assert status["dangling"] is False
    assert status["containerLiveness"] is None
    doctor = json.loads(ctl.run("--json", "doctor",
                                env={"STUB_DOCKER_INSPECT_OK": "1",
                                     **live}).stdout)
    assert doctor["danglingRegistryEntries"] == []
    assert doctor["autoResume"] is None
    repair = ctl.run("repair", "tst-x-live", env=live)
    assert repair.returncode == 5, repair.stderr


# ------------------------------------------- the vanished shape, unchanged
def test_the_vanished_container_wording_is_unchanged(ctl: Ctl):
    """#31 widened the condition; it must not have reworded the shape v0.6
    already reported (the operator's muscle memory, and every doc example)."""
    _seed_status(ctl, "tst-x-gone")
    gone = {"STUB_DOCKER_CONTAINERS": "some-other-container"}
    out = ctl.run("status", "tst-x-gone", env=gone).stdout
    assert "container: ralphd-tst-x-gone appears gone (no such container)" in out
    doc = json.loads(ctl.run("--json", "status", "tst-x-gone",
                             env=gone).stdout)
    assert doc["containerGone"] is True
    assert doc["dangling"] is True
    assert doc["containerLiveness"] == CONTAINER_ABSENT
    issues = json.loads(ctl.run("--json", "repair", "tst-x-gone",
                                env=gone).stdout)["issues"]
    assert "ralphd-tst-x-gone no longer exists" in "\n".join(issues)


# ------------------------------------------------------------ doc claims
def test_docs_cli_documents_the_three_states_and_the_new_fields():
    text = (REPO / "docs" / "cli.md").read_text()
    assert "still exists but has exited (nothing is running in it)" in text
    assert "`containerLiveness`" in text
    assert "no live container:" in text
    assert "container=ralphd-myrun (absent)" in text
    # the narrower field's meaning is spelled out, not left to be guessed
    assert "keeps its original narrower meaning" in text


def test_spec_documents_that_liveness_has_three_states():
    text = (REPO / "SPEC.md").read_text()
    section = text.split("### 8.7 ")[1].split("\n### ")[0]
    assert "three" in section and "#31" in section
    for word in (CONTAINER_ABSENT, CONTAINER_RUNNING, CONTAINER_EXITED):
        assert f"`{word}`" in section, word
    assert "containerLiveness" in section
    assert "auto-resume-eligible" in section
