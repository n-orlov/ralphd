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

Task 004 (requirement B, #202) moved the AUTHORITATIVE root-owned record
off this run-root path entirely -- first into ``.tasks-protected/tasks.json``
inside the run dir, and then (that attempt's own rejection: a root-owned
0700 directory whose PARENT is the agent-owned run dir can still be renamed
away and replaced by a dropped child) out of the run dir altogether, into
the bind-mounted ``/var/lib/ralphd/record/tasks.json`` whose every
container ancestor is root-owned -- backed on the host by
``<registry>/configs/<run-id>/record/`` (``cli.main.task_record_dir``).
A root-owned FILE in an agent-writable DIRECTORY still let a dropped child
``os.replace()`` a substituted document onto it, which is exactly what
``test_control_plane_auth_docker.py``'s pattern proves for the control
creds and what this task's own docker proof (see
``test_tasks_record_docker.py``) now proves here. The run-root ``tasks.json``
this module reads is the VIEW copy `write_tasks()` refreshes alongside the
protected write -- still root-owned 0644, exactly the posture task 105 gave
it, so this module's premise is untouched: CLI `tasks`/`repair` and the
hub's row reader read ONLY that view (`read_tasks_doc(persist=False)`
against the run root), never the protected copy. What is new below is the
*authoritative* record's own ownership assertion, now against the mounted
path, plus the assertion that the shutdown funnel has handed the
once-root-owned record directory back to the agent uid so `rm
--force`/`DELETE` (host-side, no root) can still remove both directories.
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

    # Task 004 (requirement B, #202): the authoritative record now lives at
    # `<registry>/configs/<run-id>/record/tasks.json` on the host --
    # bind-mounted at `/var/lib/ralphd/record` inside the container, whose
    # every container ancestor is root-owned, which is what this task's
    # first attempt (a root-owned directory inside the agent-owned run dir)
    # did NOT have. The shutdown funnel
    # (`privsep.release_protected_tasks_dir`, called from
    # `engine.main.amain()`) hands that directory back to the agent uid
    # once the job is over -- this container already exited above, so both
    # are readable from THIS (unprivileged) test process by now; a
    # regression here would be release never firing, which this asserts
    # directly by simply being able to stat/read them at all.
    protected_dir = ctl_.registry / "configs" / run_id / "record"
    protected_path = protected_dir / "tasks.json"
    assert not (rdir / ".tasks-protected").exists(), (
        "the record is still inside the run dir -- task 004's rejected first "
        "layout, whose protected directory a dropped child can rename away "
        "through the agent-owned run root")
    assert protected_path.is_file(), (
        "no authoritative record was ever written -- task 004 regressed, "
        "or nothing ever called RunDir.write_tasks()")
    assert protected_dir.stat().st_uid == 1000, (
        f"the record dir is owned by uid {protected_dir.stat().st_uid} after "
        "shutdown -- release_protected_tasks_dir() must hand it back to the "
        "agent uid so a host-side rm/DELETE (no root there) can still "
        "remove the whole run's state")
    assert protected_path.stat().st_uid == 1000, (
        f"record/tasks.json is owned by uid {protected_path.stat().st_uid} "
        "after shutdown -- same release, one level down")

    # The run-root tasks.json this test otherwise reads is the VIEW copy
    # `write_tasks()` refreshes alongside the protected write -- and it
    # keeps task 105's own posture unchanged: root-owned 0644, i.e. exactly
    # the file whose operability this module exists to prove. Task 004 did
    # not make it agent-owned (`privsep.write_protected_tasks_json`
    # refreshes it inside the same elevated window as the record), because
    # read-only is all a view has to be and dropping the bit would let an
    # iteration hand-edit what `ralphctl tasks`/the hub render. It is
    # deliberately NOT released at shutdown either -- an unprivileged
    # host-side `rm` removes a root-owned file inside an agent-owned
    # directory just fine, which is this test's entire point.
    tasks_path = rdir / "tasks.json"
    assert tasks_path.is_file()
    assert tasks_path.stat().st_uid == 0, (
        f"the run-root tasks.json VIEW is owned by uid "
        f"{tasks_path.stat().st_uid}, not root -- task 105's own property "
        "regressed; the agent uid must not be able to open it for writing")
    assert (tasks_path.stat().st_mode & 0o777) == 0o644, (
        oct(tasks_path.stat().st_mode))
    assert json.loads(tasks_path.read_text()) == json.loads(protected_path.read_text()), (
        "the view copy and the authoritative record disagree")
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
    # ... and the record directory task 004 moved the authoritative copy
    # into goes with it: it lives under `config_root(run_id)`, which
    # `remove_run_state` already removes wholesale, and the shutdown funnel
    # released it back to the agent uid so this unprivileged `rmtree` can.
    assert not (ctl.registry / "configs" / run_id).exists(), (
        "the run's config dir (which now carries `record/tasks.json`, the "
        "authoritative record) survived `rm --force` -- root-owned record "
        "litter an operator cannot delete without root")


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
