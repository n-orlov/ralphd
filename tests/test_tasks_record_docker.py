"""Docker tier: task 004 (requirement B, #202) proved against a *real*
container -- the standing rule for tasks 003/004/005: a disposable
container, uid separation genuinely active, a child spawned through
`privsep.agent_child_kwargs()` itself (not merely `docker exec --user
1000:1000`, which reaches the same uid by a different route -- this module
exercises the engine's own production call), and both the in-place write
attempt and the directory-entry REPLACEMENT attempt.

Task 105 (#60 requirement D) made `tasks.json` itself root-owned/0644, but
that alone left a hole: the FILE was protected, the DIRECTORY it lived in
(the bind-mounted run root, agent-owned) was not, and a directory-entry
operation -- `os.replace`, `os.rename`, `os.unlink`, `os.link`, `os.symlink`
-- is gated by the PARENT's permissions, never the target's own. Task 004
closes it by moving the authoritative copy into `.tasks-protected/`, a
directory the agent cannot write into at all, self-healed against a
precreated file or a planted symlink at its own name on every write, not
only at creation.

Three things, each needing a real container:

1. A fully dropped agent-uid child (`privsep.agent_child_kwargs()`, driven
   by a root driver reproducing `privsep.separate_engine_identity()`, the
   engine's own posture) cannot read, truncate-write, or replace/rename/
   unlink/link/symlink the authoritative `tasks.json`'s directory entry --
   with nothing in this module patching `os.replace` -- and its own
   `os.replace()` of a *valid* substituted tasks.json document (parseable,
   right shape) fails exactly the same way a malformed one would, because
   the refusal happens at the permission check, before any content is ever
   read back (`test_a_dropped_child_cannot_replace_or_write_the_record`).
   Reuses `test_control_plane_auth_docker`'s driver/probe/negative-control
   machinery wholesale -- the shape of the check (a fully dropped child,
   directory-entry ops against a root-owned 0700 parent) is identical to
   what that module already proved for `.control-token`; only the target
   directory changes.
2. A file precreated at the write's own privileged temporary name
   (`tasks.json.tmp`), and a symlink planted there instead, both fail to
   redirect the write -- proven by running the real `write_protected_tasks_
   json()` (as the engine's own root driver would call it) against a
   directory an attacker got to first, before protection existed
   (`test_a_precreated_file_and_a_symlink_at_the_temp_name_cannot_redirect_
   the_write`).
3. A resumed container, over the exact same bind-mounted run directory,
   still reads the authoritative record -- continuity across the
   release-at-shutdown / re-protect-on-first-write cycle
   (`test_a_resumed_container_still_reads_the_protected_record`).

Fixtures, cleanup discipline and the two environment-specific wrinkles
(bind-mount path translation, API reachability from inside this job's own
container) are exactly `tests/test_docker_sibling_e2e.py`'s.
"""

from __future__ import annotations

import json
import subprocess
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
from test_control_plane_auth_docker import (
    _assert_the_probe_can_actually_see_a_hole,
    _run_dropped_child_probe,
)

_PROTECTED_DIR_IN_CONTAINER = "/run/ralphd/.tasks-protected"

# A dedicated, minimal probe for the specific claim success criteria names
# by name: an os.replace() of a VALID substituted document (parseable, the
# right shape) fails exactly like a malformed one would, from a fully
# dropped agent child spawned through `privsep.agent_child_kwargs()`.
_VALID_REPLACE_CHILD = r'''
import errno
import os
import sys

scratch = "/tmp/a202-valid-sub.json"
with open(scratch, "w") as fh:
    fh.write(sys.argv[1])
try:
    os.replace(scratch, sys.argv[2])
except OSError as exc:
    print("DENIED", errno.errorcode.get(exc.errno, exc.errno))
else:
    print("REPLACED")
'''

_VALID_REPLACE_DRIVER = r'''
import subprocess
import sys

from ralphd.engine import privsep

privsep.separate_engine_identity()
kwargs = privsep.agent_child_kwargs()
res = subprocess.run([sys.executable, "-c", CHILD, sys.argv[1], sys.argv[2]],
                     capture_output=True, text=True, timeout=30, **kwargs)
sys.stdout.write(res.stdout + res.stderr)
'''


def test_a_dropped_child_cannot_replace_or_write_the_record(ctl):
    """Requirement B (#202): the same fully-dropped-child probe
    `test_control_plane_auth_docker.py` proves against `.control-token`,
    pointed at the authoritative task record instead. The probe's own
    negative control runs first, in the same container, so a pass below
    means something."""
    run_id = _unique_run_id("e202-tasks-probe")
    res = ctl.start(run_id, extra_env={"STUB_TASKS": "1", "STUB_SLEEP": "0"},
                    extra=("--on-complete", "idle"))
    assert res.returncode == 0, res.stderr
    meta = json.loads(res.stdout)
    container = f"ralphd-{run_id}"
    _wait_status(meta["apiUrl"], lambda s: s.get("state") in
                 ("succeeded", "failed", "aborted"))
    assert _container_running(container) is True

    # the probe can actually see a hole when there is one -- otherwise a
    # pass below would prove nothing about this specific directory
    _assert_the_probe_can_actually_see_a_hole(container)

    # `scratch` moves inside the bind-mounted run dir (task 004, #202):
    # `.tasks-protected` is genuinely a different device from `/tmp` inside
    # this container (unlike `.control-token`'s never-bind-mounted dir), so
    # the default scratch dir would make every rename/link/replace attempt
    # fail EXDEV -- a filesystem fact, not the permission boundary this
    # probe exists to demonstrate.
    doc = _run_dropped_child_probe(
        container, ["tasks.json"], control_dir=_PROTECTED_DIR_IN_CONTAINER,
        scratch="/run/ralphd/.a202-scratch")
    attempts = doc["attempts"]
    denied = ("EACCES", "EPERM")
    for op in ("read", "listdir-parent", "open-truncate", "os.replace",
               "os.rename", "os.unlink", "os.link", "os.symlink"):
        got = attempts[f"tasks.json:{op}"]
        assert got["ok"] is False and got["errno"] in denied, (op, got)
    # Unlike `.control-token`'s directory (never bind-mounted, parented by
    # a root-owned `/var/lib/ralphd/control`), `.tasks-protected`'s own
    # PARENT is the bind-mounted, agent-owned run root -- so a dropped
    # child genuinely CAN replace/rmdir the `.tasks-protected` entry itself
    # (that permission lives one level up, on `/run/ralphd`, not on
    # `.tasks-protected`). The design's own answer to that is
    # `_ensure_protected_tasks_dir`'s self-heal on every write, not a
    # denial here -- `<dir>:os.rmdir` on a non-empty directory the child
    # cannot even list would raise ENOTEMPTY before any permission check
    # ever ran, which is what made the borrowed control-token assertion
    # false for this target rather than merely unproven. Nothing above
    # (or in the success criteria) claims the DIRECTORY entry itself is
    # unreplaceable; only its FILE contents (`tasks.json:*`, asserted
    # above and by the valid-substitute os.replace below) and its own
    # TEMP name (proved by the file/symlink-plant test in this module) are.

    # and the specific claim success criteria names by name: an os.replace
    # of a VALID substituted document -- never merely malformed content,
    # so the refusal cannot be mistaken for a parse rejection -- also
    # fails, from the same fully dropped child, with nothing here patching
    # os.replace itself.
    valid_substitute = json.dumps({"version": 1, "tasks": [
        {"id": "1", "title": "substituted", "successCriteria": "x",
         "status": "pending"}]})
    res2 = subprocess.run(
        [REAL_DOCKER, "exec", container, "python3", "-c",
         "CHILD = " + repr(_VALID_REPLACE_CHILD) + "\n" + _VALID_REPLACE_DRIVER,
         valid_substitute, f"{_PROTECTED_DIR_IN_CONTAINER}/tasks.json"],
        capture_output=True, text=True, timeout=30)
    assert res2.returncode == 0, res2.stdout + res2.stderr
    assert "DENIED" in res2.stdout, (
        "a fully dropped agent child's os.replace() of a valid substituted "
        f"tasks.json document must fail: {res2.stdout}")

    # the record on disk is untouched by any of the above -- read through
    # `docker exec` (root inside the container), not a direct host-side
    # `Path.read_text()`: this test process runs at the same agent uid the
    # protected 0700/0600 pair denies, exactly what the whole test is
    # about, so a host-side read here would hit the identical EACCES.
    cat_res = subprocess.run(
        [REAL_DOCKER, "exec", container, "cat",
         f"{_PROTECTED_DIR_IN_CONTAINER}/tasks.json"],
        capture_output=True, text=True, timeout=15)
    assert cat_res.returncode == 0, cat_res.stdout + cat_res.stderr
    on_disk = json.loads(cat_res.stdout)
    assert "substituted" not in json.dumps(on_disk), (
        "the substituted document reached the record", on_disk)

    # `--on-complete idle` (needed above so the container stayed up for the
    # probe) means `amain()` never returns on its own -- the `ctl` fixture's
    # own teardown is a hard `docker rm -f`, which gives the engine no
    # chance to run `privsep.release_protected_tasks_dir()`. A graceful
    # `ralphctl stop` here (a `/shutdown` request, exactly what SIGTERM
    # drives) lets that funnel run before the container goes away, so this
    # test does not leave `.tasks-protected` root-owned inside the bind-
    # mounted work_root -- unremovable by the *next* module's own
    # `shutil.rmtree(..., ignore_errors=True)`, and (worse) sitting under
    # this repo checkout where a later docker build's context walk would
    # trip over it.
    stop_res = ctl.stop(run_id, force=True)
    assert stop_res.returncode == 0, stop_res.stderr


_SELF_HEAL_DRIVER = r'''
import json
import os
import sys
from pathlib import Path

from ralphd.engine import privsep

privsep.separate_engine_identity()

protected_path = Path(sys.argv[1])
view_path = Path(sys.argv[2])
doc = {"version": 1, "tasks": [{"id": "1", "title": "real", "successCriteria": "x",
                                "status": "pending"}]}
privsep.write_protected_tasks_json(protected_path, view_path, doc)

out = {}
with privsep.root_for_write():
    with open(protected_path) as fh:
        out["protectedContent"] = json.load(fh)
    st = os.lstat(protected_path)
    out["protectedIsRegularFile"] = not os.path.islink(protected_path)
    out["protectedMode"] = oct(st.st_mode & 0o777)
    out["protectedUid"] = st.st_uid
sys.stdout.write(json.dumps(out))
'''


def _run_self_heal_driver(container: str, scratch: str, plant: str) -> dict:
    """Set up `scratch/.tasks-protected/` as an agent-writable directory
    (the shape it has before protection is ever established -- the exact
    window a planted entry would need to matter), plant something hostile
    at the write's own temp name inside it, then call
    `privsep.write_protected_tasks_json()` for real (as root, reproducing
    the engine's own call site) and report what actually landed.

    `plant`: "file" precreates a plain file at the temp name; "symlink"
    plants a symlink pointing at an agent-writable decoy elsewhere, so a
    write that naively followed it would land the real content somewhere
    an agent could read it back out unprotected -- and the decoy's own
    content proves whether that happened.
    """
    protected_dir = f"{scratch}/.tasks-protected"
    protected_path = f"{protected_dir}/tasks.json"
    view_path = f"{scratch}/tasks.json"
    decoy = f"{scratch}/decoy-elsewhere.json"
    setup = [
        f"rm -rf {scratch}", f"mkdir -p {protected_dir}",
        f"chown -R 1000:1000 {scratch}",
    ]
    if plant == "symlink":
        setup.append(f"printf 'decoy-untouched\\n' > {decoy}")
        setup.append(f"chown 1000:1000 {decoy}")
    res = subprocess.run(
        [REAL_DOCKER, "exec", container, "sh", "-c", " && ".join(setup)],
        capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stdout + res.stderr

    # the plant itself is done AS THE AGENT UID (`docker exec --user
    # 1000:1000`) -- the identity that would genuinely have this window,
    # not root setting up its own trap.
    if plant == "file":
        plant_cmd = f"printf 'PLANTED-FILE\\n' > {protected_path}.tmp"
    elif plant == "symlink":
        plant_cmd = f"ln -s {decoy} {protected_path}.tmp"
    else:
        raise AssertionError(plant)
    plant_res = subprocess.run(
        [REAL_DOCKER, "exec", "--user", "1000:1000", container, "sh", "-c", plant_cmd],
        capture_output=True, text=True, timeout=15)
    assert plant_res.returncode == 0, plant_res.stdout + plant_res.stderr

    driver_res = subprocess.run(
        [REAL_DOCKER, "exec", container, "python3", "-c", _SELF_HEAL_DRIVER,
         protected_path, view_path],
        capture_output=True, text=True, timeout=30)
    assert driver_res.returncode == 0, driver_res.stdout + driver_res.stderr
    out = json.loads(driver_res.stdout)

    if plant == "symlink":
        decoy_res = subprocess.run(
            [REAL_DOCKER, "exec", container, "cat", decoy],
            capture_output=True, text=True, timeout=15)
        out["decoyContent"] = decoy_res.stdout
    subprocess.run([REAL_DOCKER, "exec", container, "rm", "-rf", scratch],
                   capture_output=True, timeout=30)
    return out


@pytest.mark.parametrize("plant", ["file", "symlink"])
def test_a_precreated_file_and_a_symlink_at_the_temp_name_cannot_redirect_the_write(
        ctl, plant):
    run_id = _unique_run_id(f"e202-tmp-{plant}")
    res = ctl.start(run_id, extra_env={"STUB_TASKS": "1", "STUB_SLEEP": "0"},
                    extra=("--on-complete", "idle"))
    assert res.returncode == 0, res.stderr
    meta = json.loads(res.stdout)
    container = f"ralphd-{run_id}"
    _wait_status(meta["apiUrl"], lambda s: s.get("state") in
                 ("succeeded", "failed", "aborted"))

    out = _run_self_heal_driver(container, "/tmp/a202-self-heal", plant)
    # the real document landed -- the planted entry was discarded, never
    # trusted, and the write was never redirected through it
    assert out["protectedContent"]["tasks"][0]["title"] == "real", out
    assert out["protectedIsRegularFile"] is True, out  # never a symlink
    assert out["protectedMode"] == "0o600", out
    assert out["protectedUid"] == 0, out
    if plant == "symlink":
        # the decoy the symlink pointed at was never written through --
        # if the write had followed the symlink, the real content would
        # have landed THERE instead, agent-readable, which is the whole
        # point of planting it
        assert out["decoyContent"] == "decoy-untouched\n", out

    # `ctl.start` above (`--on-complete idle`) also started a REAL job of
    # its own, whose real planning wrote a real `.tasks-protected` under
    # this run's OWN bind-mounted run dir -- independent of
    # `_run_self_heal_driver`'s scratch dir, which lives inside the
    # container's own writable layer and needs no cleanup of its own. A
    # graceful stop here (see the sibling comment in
    # `test_a_dropped_child_cannot_replace_or_write_the_record`) lets
    # `privsep.release_protected_tasks_dir()` run before the `ctl` fixture's
    # hard `docker rm -f` teardown, so this run's own directory does not
    # join the litter that poisoned an earlier attempt at this task.
    stop_res = ctl.stop(run_id, force=True)
    assert stop_res.returncode == 0, stop_res.stderr


def test_a_resumed_container_still_reads_the_protected_record(ctl, work_root):
    """A resumed container, over the exact same bind-mounted run dir, still
    reads the authoritative record -- across the release-at-shutdown /
    re-protect-on-first-write cycle `privsep.release_protected_tasks_dir`/
    `_ensure_protected_tasks_dir` describe."""
    run_id = _unique_run_id("e202-resume")
    hold_rel = ".hold-e202-resume"
    hold_file_host = work_root / "ws" / hold_rel
    res = ctl.start(run_id, extra_env={
        "STUB_TASKS": "2",
        "STUB_HOLD_FILE": f"/workspace/{hold_rel}",
        # Task 008 (B.2): without STUB_HOLD_PHASE the hold catches EVERY
        # invocation, planning included -- which would mean `.tasks-
        # protected` never exists at all while this first container is up
        # (exactly `test_resume_continues_after_container_stop`'s own
        # pattern, which never inspects the record for that reason). This
        # test needs the opposite: planning through immediately, so the
        # authoritative copy already exists before the stop, and only the
        # first WORKER iteration blocked -- naming the target phase is what
        # buys that.
        "STUB_HOLD_PHASE": "worker",
        "STUB_HOLD_TIMEOUT_S": "60"},
        extra=("--on-complete", "idle"))
    assert res.returncode == 0, res.stderr
    meta = json.loads(res.stdout)
    container = f"ralphd-{run_id}"

    # planning (unheld) writes the record deterministically fast; poll the
    # run-root agent-readable VIEW copy (`tasks.json`, host-readable,
    # unlike the protected copy) rather than trust `state == "running"`
    # (set at the very top of `_run_job_core`, before planning has run at
    # all) or the first readable `/status` (same problem, one layer up).
    rdir = ctl.registry / "runs" / run_id
    deadline = time.time() + 30
    tasks_doc = None
    while time.time() < deadline:
        try:
            tasks_doc = json.loads((rdir / "tasks.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            tasks_doc = None
        if tasks_doc and len(tasks_doc.get("tasks", [])) >= 2:
            break
        time.sleep(0.4)
    assert tasks_doc and len(tasks_doc.get("tasks", [])) >= 2, (
        "planning's first write never landed within the deadline -- not a "
        f"genuine mid-run proof: {tasks_doc}")

    # while the first container is still up: the authoritative copy already
    # exists (planning's own first write) and is root-owned inside a
    # root-owned 0700 directory
    check = subprocess.run(
        [REAL_DOCKER, "exec", container, "sh", "-c",
         "stat -c '%U %a' /run/ralphd/.tasks-protected && "
         "stat -c '%U %a' /run/ralphd/.tasks-protected/tasks.json"],
        capture_output=True, text=True, timeout=15)
    assert check.returncode == 0, check.stdout + check.stderr
    lines = check.stdout.splitlines()
    assert lines[0] == "root 700", lines
    assert lines[1] == "root 600", lines

    res = subprocess.run([REAL_DOCKER, "stop", "-t", "5", container],
                        capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    assert _container_running(container) is False

    hold_file_host.touch()  # release, exactly test_resume_continues...'s pattern

    res = ctl.resume(run_id, extra=("--iterations", "+10"))
    assert res.returncode == 0, res.stderr
    meta2 = json.loads(res.stdout)
    container2 = f"ralphd-{run_id}"
    assert meta2["container"] != meta["container"]

    final = _wait_status(meta2["apiUrl"], lambda s: s.get("state") in
                         ("succeeded", "failed", "aborted"), timeout=90)
    assert final["verdict"] == "verified", final

    # the resumed container's own engine re-established protection on its
    # first write, and the AUTHORITATIVE copy inside it carries the real,
    # continued task history -- both tasks completed, not reset
    check2 = subprocess.run(
        [REAL_DOCKER, "exec", container2, "sh", "-c",
         "stat -c '%U %a' /run/ralphd/.tasks-protected && "
         "cat /run/ralphd/.tasks-protected/tasks.json"],
        capture_output=True, text=True, timeout=15)
    assert check2.returncode == 0, check2.stdout + check2.stderr
    first_line, _, rest = check2.stdout.partition("\n")
    assert first_line == "root 700", first_line
    protected_doc = json.loads(rest)
    assert len(protected_doc["tasks"]) == 2
    assert all(t["status"] == "completed" for t in protected_doc["tasks"]), protected_doc

    # and every ordinary reader -- `ralphctl tasks`, host-side, unprivileged
    # -- agrees, reading the agent-readable view, never needing root itself
    res = ctl.run("tasks", run_id)
    assert res.returncode == 0, res.stderr
    view_doc = json.loads(res.stdout)
    assert len(view_doc["tasks"]) == 2
    assert all(t["status"] == "completed" for t in view_doc["tasks"]), view_doc

    # `resume` inherits `--on-complete idle` from the original `start` (it
    # is persisted job config, not a fresh `--env` pair), so container2 is
    # still up right now -- graceful `ralphctl stop` lets
    # `privsep.release_protected_tasks_dir()` run before the `ctl` fixture's
    # hard `docker rm -f` teardown, same as the first container above.
    stop_res2 = ctl.stop(run_id, force=True)
    assert stop_res2.returncode == 0, stop_res2.stderr
