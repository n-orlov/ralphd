"""Docker tier: task 106 (#60 requirement D follow-up) -- prove that task
105's root-owned ``tasks.json`` does not turn the run dir into something
the CLI/hub can no longer operate on once the job is over.

Task 105 made ``tasks.json`` itself root-owned/0644 under the uid boundary.
Every surface this file exercises only ever *reads* that file (``repair``'s
diagnosis, ``tasks``, the hub's ``/api/runs`` row) or deletes the *directory*
it lives in (``rm --force``, the hub's ``DELETE /api/runs/<id>``) -- deleting
a file only needs write permission on its parent directory, not on the file
itself, and the run directory stays owned by the agent uid throughout,
so none of that should be affected by the one file inside it being
root-owned. This module proves it against a REAL sibling container (never
``tests/stub-docker``), reusing ``test_docker_sibling_e2e``'s fixtures/
cleanup discipline exactly like ``test_engine_uid_docker.py`` does.
"""

from __future__ import annotations

import json
import time

import pytest

from test_docker_sibling_e2e import (  # noqa: F401  (fixtures used by name)
    REAL_DOCKER,
    Ctl,
    _container_running,
    _unique_run_id,
    _wait_status,
    ctl,
    gateway_ip,
    pytestmark,
    test_image,
    work_root,
)
from test_cli_ui import ui  # noqa: F401  (fixture used by name)


def _run_to_terminal_with_container_exited(ctl_: Ctl, prefix: str) -> tuple[str, "object"]:
    """Start a one-task job with the product default (`on_complete=exit`,
    task 010) so the container tears itself down on its own, blocking on
    `--no-detach` (exactly `test_no_detach_exits_0_on_verified...`'s
    pattern) rather than racing the API's own teardown by polling `/status`
    -- with `STUB_SLEEP=0` the job can finish, and the API server with it,
    before a poll loop started after `start` returns gets its first request
    in, which is a real race this file hit once. Reading the terminal state
    from the run dir's own `status.json` afterwards (rather than the API)
    sidesteps that race entirely, and the CLI/hub operations under test all
    refuse to touch a run whose container is still recorded as running
    (`repair` explicitly; `rm`/`DELETE` only past `--force`/the
    terminal-state gate), so a real test of "the run is over" has to wait
    for the real exit rather than assuming instantaneity."""
    run_id = _unique_run_id(prefix)
    res = ctl_.start(run_id, extra_env={"STUB_TASKS": "1", "STUB_SLEEP": "0"},
                     extra=("--on-complete", "exit", "--no-detach"),
                     timeout=120)
    assert res.returncode == 0, (res.stdout, res.stderr)

    rdir = ctl_.registry / "runs" / run_id
    status = json.loads((rdir / "status.json").read_text())
    assert status["state"] == "succeeded", status
    assert status["verdict"] == "verified", status

    container = f"ralphd-{run_id}"
    deadline = time.time() + 30
    while time.time() < deadline and _container_running(container) is not False:
        time.sleep(0.5)
    assert _container_running(container) is False, (
        "container never reached a non-running state -- on_complete=exit "
        "regressed, or the wait above was too short")

    tasks_path = rdir / "tasks.json"
    assert tasks_path.is_file()
    assert tasks_path.stat().st_uid == 0, (
        f"tasks.json is owned by uid {tasks_path.stat().st_uid}, not root -- "
        "task 105 regressed; this test's whole premise depends on it")
    assert (tasks_path.stat().st_mode & 0o777) == 0o644, (
        oct(tasks_path.stat().st_mode))
    return run_id, rdir


def test_repair_tasks_and_rm_work_against_a_root_owned_record(ctl):
    run_id, rdir = _run_to_terminal_with_container_exited(ctl, "e106-cli")

    # `repair`'s diagnosis reads tasks.json (world-readable at 0644) --
    # it must find no issue caused by the ownership change.
    res = ctl.run("repair", run_id)
    assert res.returncode == 0, res.stderr
    diag = json.loads(res.stdout)
    assert diag["issues"] == [], diag
    assert diag["ok"] is True, diag

    # `tasks`, both renderings -- pure reads of the same root-owned file.
    res = ctl.run("tasks", run_id)
    assert res.returncode == 0, res.stderr
    # `ctl.run` already prepends the global `--json` flag (see `Ctl.run`);
    # `cmd_tasks` renders JSON off that alone, with no subcommand-level flag.
    tasks_doc = json.loads(res.stdout)
    assert len(tasks_doc["tasks"]) == 1
    assert tasks_doc["tasks"][0]["status"] == "completed", tasks_doc

    # `rm --force`: deleting a root-owned FILE only needs write permission
    # on its parent DIRECTORY (still agent-owned) -- so the whole run dir,
    # tasks.json included, must genuinely disappear, not merely refuse.
    res = ctl.run("rm", run_id, "--force", "--yes")
    assert res.returncode == 0, res.stderr
    assert not rdir.exists(), (
        f"{rdir} still exists after `ralphctl rm --force` -- deletion of a "
        "root-owned tasks.json inside an agent-owned run dir must succeed")


def test_hub_reader_and_delete_endpoint_work_against_a_root_owned_record(ctl, ui):
    run_id, rdir = _run_to_terminal_with_container_exited(ctl, "e106-hub")

    server = ui(ctl.registry)

    # The hub's own reader: `GET /api/runs` builds each row from
    # `read_tasks_doc(persist=False)` (`ui_server._row_tasks`) -- a pure
    # read of the same root-owned tasks.json, never a write.
    code, payload = server.get("/api/runs")
    assert code == 200, payload
    row = next((r for r in payload["runs"] if r.get("runId") == run_id), None)
    assert row is not None, payload
    assert row["tasksTotal"] == 1, row
    assert row["tasksCompleted"] == 1, row

    # `DELETE /api/runs/<id>` -- the hub's one-command delete -- reuses the
    # exact same `remove_run_state` sequence `rm --force` does; the run dir
    # directory must genuinely disappear from disk, not merely answer 200
    # while leaving the root-owned file (or the dir) behind.
    code, payload = server.delete(f"/api/runs/{run_id}")
    assert code == 200, payload
    assert not rdir.exists(), (
        f"{rdir} still exists after the hub's DELETE /api/runs/{run_id} -- "
        "deletion of a root-owned tasks.json inside an agent-owned run dir "
        "must succeed")

    # And the hub's own listing agrees the run is gone.
    code, payload = server.get("/api/runs")
    assert code == 200, payload
    assert not any(r.get("runId") == run_id for r in payload["runs"]), payload
