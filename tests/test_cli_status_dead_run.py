"""Task 022 (#8): `ralphctl status` on an unreachable run whose recorded state
is non-terminal says the container appears gone, instead of leaving the
operator to join `state: running` with `(live api: False)` -- and stops
printing an ever-growing `elapsed` for a run that stopped elapsing, showing
the staleness (time since the last status.json write) instead.

Two tiers, matching test_cli_status_degraded.py:
- black-box `ralphctl status` over on-disk run-dir fixtures (the CLI's
  status.json fallback path) with the stub-docker `Ctl` harness deciding
  whether the container "exists";
- one real (no-Docker) engine via the `live` fixture, proving a live run's
  output is unchanged.
"""

from __future__ import annotations

import json
import time

from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run

from ralphd.cli.main import _format_container_gone_lines
from ralphd.engine.state import utc_from_epoch

__all__ = ["ctl", "unix_sock"]


_BASE_STATUS = {
    "state": "running",
    "verdict": None,
    "phase": "worker",
    "approach": 1,
    "iterationsUsed": 7,
    "iterationsBudget": 250,
    "startedAt": "2024-01-01T00:00:00Z",
    "schemaVersion": 1,
    "tasks": {"total": 3, "completed": 1, "pending": 2},
    "usage": {"costUSD": 0.5, "totalTokens": 12000},
}


def _seed_status(ctl: Ctl, run_id: str, **status_over) -> None:
    rdir, _cdir = _seed_run(ctl, run_id)
    doc = {**_BASE_STATUS, "runId": run_id, **status_over}
    (rdir / "status.json").write_text(json.dumps(doc))


# --------------------------------------------------------------------------
# formatter unit test
# --------------------------------------------------------------------------

def test_container_gone_lines_name_the_container_state_and_remedy():
    lines = _format_container_gone_lines(
        "tst-zombie", {"state": "running"},
        {"runId": "tst-zombie", "container": "ralphd-tst-zombie"}, False)
    joined = " ".join(lines)
    assert lines[0].startswith("container: ralphd-tst-zombie appears gone")
    assert "'running'" in joined
    # one story with repair/doctor: status points at the diagnosis command
    assert "ralphctl repair tst-zombie" in joined
    for extra in lines[1:]:
        assert extra.startswith("           ")


# --------------------------------------------------------------------------
# black-box `ralphctl status` over run-dir fixtures
# --------------------------------------------------------------------------

def test_status_warns_that_the_container_is_gone(ctl: Ctl):
    # no STUB_DOCKER_CONTAINERS -> `docker inspect ralphd-...` fails, i.e.
    # the container does not exist at all
    _seed_status(ctl, "tst-gone", updatedAt=utc_from_epoch(time.time() - 300))
    res = ctl.run("status", "tst-gone")
    assert res.returncode == 0, res.stderr
    gone = [ln for ln in res.stdout.splitlines() if ln.startswith("container:")]
    assert len(gone) == 1, res.stdout
    assert "ralphd-tst-gone appears gone" in gone[0]
    assert "'running'" in res.stdout
    assert "ralphctl repair tst-gone" in res.stdout


def test_status_relabels_the_duration_as_time_since_last_update(ctl: Ctl):
    _seed_status(ctl, "tst-gone-dur",
                 updatedAt=utc_from_epoch(time.time() - 300))
    res = ctl.run("status", "tst-gone-dur")
    assert res.returncode == 0, res.stderr
    dur = [ln for ln in res.stdout.splitlines() if ln.startswith("duration:")]
    assert len(dur) == 1, res.stdout
    # the growing "elapsed since startedAt" value is gone...
    assert "(elapsed)" not in res.stdout
    # ...replaced by a labelled staleness value (~5m since updatedAt, NOT
    # the years since the 2024 startedAt)
    assert "(since last update)" in dur[0]
    assert "5m" in dur[0], dur[0]


def test_status_json_carries_container_gone_and_staleness(ctl: Ctl):
    _seed_status(ctl, "tst-gone-json",
                 updatedAt=utc_from_epoch(time.time() - 120),
                 currentIteration={"number": 8, "phase": "worker",
                                   "startedAt": utc_from_epoch(time.time() - 180)})
    res = ctl.run("--json", "status", "tst-gone-json")
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    assert doc["live"] is False
    assert doc["containerGone"] is True
    assert 110 <= doc["sinceLastUpdateSeconds"] <= 200
    # durationSeconds stops growing too: bounded by the last write, not now
    assert doc["durationSeconds"] is not None
    # the in-flight iteration's elapsed is frozen at the last write (60s),
    # not still counting from 180s ago
    assert 50 <= doc["currentIteration"]["elapsedSeconds"] <= 70


def test_status_freezes_the_in_flight_iteration_elapsed_line(ctl: Ctl):
    _seed_status(ctl, "tst-gone-iter",
                 updatedAt=utc_from_epoch(time.time() - 120),
                 currentIteration={"number": 8, "phase": "worker",
                                   "startedAt": utc_from_epoch(time.time() - 180)})
    res = ctl.run("status", "tst-gone-iter")
    assert res.returncode == 0, res.stderr
    line = [ln for ln in res.stdout.splitlines()
            if ln.startswith("iteration elapsed:")]
    assert len(line) == 1, res.stdout
    assert "at last update" in line[0]
    assert "1m" in line[0], line[0]


def test_status_of_a_terminal_unreachable_run_is_unchanged(ctl: Ctl):
    """A finished run legitimately has no container -- no warning, and the
    duration keeps saying `total`."""
    _seed_status(ctl, "tst-done", state="succeeded", verdict="verified",
                 endedAt="2024-01-01T01:02:03Z",
                 updatedAt="2024-01-01T01:02:03Z")
    res = ctl.run("status", "tst-done")
    assert res.returncode == 0, res.stderr
    assert "appears gone" not in res.stdout
    assert "(total)" in res.stdout
    assert "since last update" not in res.stdout
    doc = json.loads(ctl.run("--json", "status", "tst-done").stdout)
    assert doc["containerGone"] is False
    assert "sinceLastUpdateSeconds" not in doc


def test_status_of_a_run_whose_container_exists_but_exited_is_unchanged(ctl: Ctl):
    """The container still exists (exited): not the vanished-container
    condition doctor/repair report, so status must not claim it is gone."""
    _seed_status(ctl, "tst-exited", updatedAt=utc_from_epoch(time.time() - 60))
    res = ctl.run("status", "tst-exited", env={
        "STUB_DOCKER_CONTAINERS": "ralphd-tst-exited",
        "STUB_DOCKER_RUNNING": "",
    })
    assert res.returncode == 0, res.stderr
    assert "appears gone" not in res.stdout
    assert "(elapsed)" in res.stdout


# --------------------------------------------------------------------------
# live run: output unchanged
# --------------------------------------------------------------------------

def test_live_run_output_is_unchanged(live):
    """A reachable engine is never diagnosed as a zombie: no warning line,
    the duration keeps its `elapsed`/`total` label, and --json says so."""
    run = live(run_id="dead-live", job={"iterations": 6},
               stub_env={"STUB_TASKS": "1", "STUB_SLEEP": "3"})
    run.wait_api()
    res = run.ralphctl("status", run.run_id)
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "(live api: True)" in res.stdout
    assert "appears gone" not in res.stdout
    assert "since last update" not in res.stdout
    assert "(elapsed)" in res.stdout or "(total)" in res.stdout
    doc = json.loads(run.ralphctl("--json", "status", run.run_id).stdout)
    assert doc["containerGone"] is False
    assert "sinceLastUpdateSeconds" not in doc
    run.wait_terminal(timeout=90)
