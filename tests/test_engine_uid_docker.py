"""Docker tier: `pkill -f ralphd-engine` inside a task iteration does not end
the run (task 020, requirement I, #48).

This is the one test that can prove requirement I, because the property is a
real container property: it needs an engine that actually started as root (the
shipped `container/Dockerfile`), a real `pi` subprocess actually dropped to the
`agent` uid, and the kernel's own `kill(2)` permission check. The fast-lane
half -- the credentials privsep asks for, the wiring, the image and the
documented claims -- is `tests/test_engine_uid_boundary.py`.

The iteration attacks its supervisor the way the incident did: the stub `pi`'s
`STUB_SIGNAL_ENGINE` knob (see `tests/stub-pi/pi`) runs `pkill -f
ralphd-engine` from inside every *worker* iteration and then SIGTERMs and
SIGKILLs, by pid, everything whose `/proc/<pid>/cmdline` mentions the engine.
It records what each attempt got. Three things then have to be true at once,
and only all three together mean the boundary is real:

1. the run reaches its normal terminal state with its normal verdict -- the
   engine survived the whole thing;
2. every attempt was refused with `EPERM` (and `pkill` exited nonzero), made
   from uid 1000 -- so the attempts genuinely happened and were genuinely
   blocked, rather than silently matching nothing;
3. the engine's own credentials in the container are the documented shape
   (`Uid: 0 1000 0 1000` -- real/saved root, effective agent); every file it
   wrote into the bind-mounted run dir is still owned by uid 1000 EXCEPT
   the authoritative task record. Task 105 (#60 requirement D) first made
   `tasks.json` itself root-owned; task 004 (requirement B, #202) moved
   that authoritative copy OFF this run-root path entirely, into
   `/var/lib/ralphd/record/tasks.json` (root-owned 0600 inside a
   root-owned 0700 directory whose every container ancestor is root-owned
   too, bind-mounted from `<registry>/configs/<run-id>/record` on the
   host) -- a root-owned FILE in an agent-writable DIRECTORY still let a
   dropped child `os.replace()` a substituted document onto it, a
   directory-entry operation gated by the parent, never the target, and a
   root-owned directory INSIDE the agent-owned run dir (this task's own
   rejected first attempt) still let the child rename the DIRECTORY away
   and plant its own in its place.
   The run-root `tasks.json` this module otherwise reads is the VIEW copy
   `write_tasks()` refreshes alongside the protected write, and it keeps
   task 105's own posture: root-owned 0644, so the agent uid still cannot
   open it for writing either. A fourth check proves the moved boundary is
   real: an attempt to write the PROTECTED copy from inside the container,
   as uid 1000 (the exact uid every iteration runs as), is refused with
   `Permission denied` -- both an in-place truncate and an `os.replace` of
   a valid substituted document -- whereas against the VIEW copy only the
   in-place write is refused: an `os.replace` of the view's own directory
   ENTRY still succeeds, because the run root is agent-owned by
   construction and no file mode can override a parent's permissions.
   That asymmetry is the whole reason the record left the run dir.

Environment wrinkles (bind-mount path translation, API reachability from
inside this job container) and the cleanup discipline are exactly those of
`tests/test_docker_sibling_e2e.py`; this module reuses that file's helpers by
importing them, so there is one implementation of each. Its own containers get
their own unique run ids and are removed by exact name, never by a label query
that could match this job's own container; the image tags it builds are the
same two that module builds (and removes) so the layers are shared.
"""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from test_docker_sibling_e2e import (  # noqa: F401  (fixtures are used by name)
    REAL_DOCKER,
    Ctl,
    _container_running,
    _docker_exec,
    _unique_run_id,
    _wait_status,
    ctl,
    gateway_ip,
    pytestmark,
    test_image,
    work_root,
)
from tests.conftest import wait_hold_entered


def _engine_creds(container: str) -> dict[str, list[int]]:
    """The engine's own uid/gid credentials, read out of its /proc entry.

    Not PID 1: `ralphctl` runs the job container with `docker run --init`, so
    PID 1 is docker's own init (root, and unrelated to this property) and the
    engine is its child. The engine is found the same way the attacking
    iteration finds it -- by `ralphd-engine` appearing in a cmdline.
    """
    finder = (
        "import os,sys\n"
        "me = str(os.getpid())\n"
        "for e in sorted(os.listdir('/proc'), key=lambda v: v.zfill(9)):\n"
        "    if not e.isdigit() or e == me:\n"
        "        continue\n"
        "    try:\n"
        "        c = open('/proc/%s/cmdline' % e, 'rb').read().decode('utf8','replace')\n"
        "    except OSError:\n"
        "        continue\n"
        # the engine is a #! console script, so its argv0 is the python
        # interpreter and `ralphd-engine` appears later in the cmdline --
        # which is exactly why `pkill -f ralphd-engine` matched it. Skipping
        # this probe's OWN pid matters for the same reason: this source code
        # mentions the engine too.
        "    if 'ralphd-engine' not in c:\n"
        "        continue\n"
        "    sys.stdout.write(e + '\\n')\n"
        "    sys.stdout.write(open('/proc/%s/status' % e).read())\n"
        "    break\n"
        "else:\n"
        "    sys.exit('no ralphd-engine process in this container')\n")
    res = _docker_exec(container, "python3", "-c", finder)
    assert res.returncode == 0, res.stderr
    pid, _, status = res.stdout.partition("\n")
    out: dict[str, list[int]] = {}
    for line in status.splitlines():
        if line.startswith(("Uid:", "Gid:")):
            key, _, rest = line.partition(":")
            out[key.lower()] = [int(v) for v in rest.split()]
    assert "uid" in out and "gid" in out, res.stdout
    out["pid"] = [int(pid)]
    return out


def _container_log(container: str) -> str:
    res = subprocess.run([REAL_DOCKER, "logs", container],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return res.stdout + res.stderr


def test_a_worker_iteration_cannot_kill_the_engine(ctl):
    run_id = _unique_run_id("e048-pkill")
    # `--on-complete idle` keeps the container alive after the verdict so the
    # engine's own credentials can still be read out of /proc/1/status.
    res = ctl.start(run_id, extra_env={"STUB_TASKS": "2", "STUB_SLEEP": "0",
                                       "STUB_SIGNAL_ENGINE": "1"},
                    extra=("--on-complete", "idle"))
    assert res.returncode == 0, res.stderr
    meta = json.loads(res.stdout)
    container = f"ralphd-{run_id}"

    # 1. the run survived every attempt and reached its normal verdict
    status = _wait_status(meta["apiUrl"], lambda s: s.get("state") in
                          ("succeeded", "failed", "aborted"))
    assert status["state"] == "succeeded", status
    assert status["verdict"] == "verified", status
    assert _container_running(container) is True

    rdir = ctl.registry / "runs" / run_id
    tasks_doc = json.loads((rdir / "tasks.json").read_text())
    assert all(t["status"] == "completed" for t in tasks_doc["tasks"]), tasks_doc

    # 2. the attempts really happened, from the agent uid, and were refused
    record = json.loads((rdir / ".stub-signal-engine.json").read_text())
    assert record["uid"] == record["euid"] == 1000, record
    attempts = record["attempts"]
    pkills = [a for a in attempts if a["how"].startswith("pkill")]
    kills = [a for a in attempts if a["how"].startswith("kill ")]
    assert pkills and all(a["rc"] != 0 for a in pkills), record
    assert kills, ("the iteration found no engine process to signal -- the "
                   f"attack itself did not happen: {record}")
    assert all(a["errno"] == 1 for a in kills), record  # EPERM, every one

    # 3. the documented credential shape, and unchanged file ownership --
    # except the authoritative task record. Task 004 (requirement B, #202)
    # moved that record off `tasks.json` at the run root -- which stays the
    # root-owned 0644 VIEW copy task 105 made it -- into the bind-mounted
    # `/var/lib/ralphd/record/tasks.json`, root-owned inside a root-owned
    # 0700 directory with only root-owned ancestors above it.
    creds = _engine_creds(container)
    assert creds["uid"] == [0, 1000, 0, 1000], creds  # real, effective, saved, fs
    assert creds["gid"] == [0, 1000, 0, 1000], creds
    assert "uid boundary active" in _container_log(container)
    for name in ("status.json", "events.jsonl"):
        path = rdir / name
        assert path.is_file(), name
        assert path.stat().st_uid == 1000, (
            f"{name} is owned by uid {path.stat().st_uid}: the engine's "
            "effective uid must stay the agent's so the host user still owns "
            "its own run dir")
    view_path = rdir / "tasks.json"
    assert view_path.is_file()
    assert view_path.stat().st_uid == 0, (
        f"the run-root tasks.json VIEW is owned by uid "
        f"{view_path.stat().st_uid}, not root -- task 105's property must "
        "survive task 004's split (the view is refreshed inside the same "
        "elevated window as the record it mirrors)")
    # The record directory is root-owned 0700: even `Path.is_file()`/`.stat()`
    # from THIS (unprivileged, agent-uid) host-side test process raises
    # `PermissionError` rather than returning False -- traversal into the
    # directory itself is denied, not merely opening the file inside it.
    # Ownership/mode/content all have to be read through `docker exec`
    # (root inside the container), exactly `test_root_owned_record_
    # operability_docker.py`'s own pattern for the same directory.
    protected_check = subprocess.run(
        [REAL_DOCKER, "exec", container, "sh", "-c",
         "stat -c '%U %a' /var/lib/ralphd/record && "
         "stat -c '%U %a' /var/lib/ralphd/record/tasks.json && "
         "cat /var/lib/ralphd/record/tasks.json"],
        capture_output=True, text=True, timeout=15)
    assert protected_check.returncode == 0, (
        protected_check.stdout + protected_check.stderr)
    dir_line, file_line, doc_text = protected_check.stdout.split("\n", 2)
    assert dir_line == "root 700", (
        f"the record dir is {dir_line!r}, not root-owned 0700 -- task "
        "004's whole point is that the agent uid can no longer write into "
        "the directory holding the authoritative task record")
    assert file_line == "root 600", (
        f"record/tasks.json is {file_line!r}, not root-owned 0600")
    protected_doc = json.loads(doc_text)
    assert protected_doc == tasks_doc, (
        "the authoritative copy and the agent-readable view disagree", protected_doc, tasks_doc)

    # 4. the agent uid genuinely cannot write the AUTHORITATIVE copy:
    # attempted from inside the same container, as the same uid every
    # iteration runs as (`--user 1000:1000`, not asserted from the host,
    # where ownership alone would prove nothing about the container's own
    # permission check). Both an in-place truncate and an `os.replace` of a
    # valid substituted document (never merely malformed content, so the
    # failure cannot be a parse rejection in disguise) must fail -- the
    # second is the directory-entry operation task 105's root-owned FILE
    # alone never blocked, which is the regression #202/task 004 closes.
    denied = subprocess.run(
        [REAL_DOCKER, "exec", "--user", "1000:1000", container, "sh", "-c",
         "echo '{\"tasks\": []}' > /var/lib/ralphd/record/tasks.json; echo RC=$?"],
        capture_output=True, text=True, timeout=15)
    assert denied.returncode == 0, denied.stderr  # docker exec itself worked
    assert "RC=0" not in denied.stdout, denied.stdout  # the shell write failed
    combined = denied.stdout + denied.stderr
    assert "Permission denied" in combined, combined

    valid_substitute = json.dumps({"version": 1, "tasks": [
        {"id": "1", "title": "substituted", "successCriteria": "x",
         "status": "pending"}]})
    replace_attempt = subprocess.run(
        [REAL_DOCKER, "exec", "--user", "1000:1000", container, "python3", "-c",
         "import os,sys\n"
         "scratch = '/tmp/a202-replace-src.json'\n"
         "open(scratch, 'w').write(sys.argv[1])\n"
         "try:\n"
         "    os.replace(scratch, '/var/lib/ralphd/record/tasks.json')\n"
         "except OSError as exc:\n"
         "    print('DENIED', exc.errno)\n"
         "else:\n"
         "    print('REPLACED')\n",
         valid_substitute],
        capture_output=True, text=True, timeout=15)
    assert replace_attempt.returncode == 0, replace_attempt.stderr
    assert "DENIED" in replace_attempt.stdout, (
        "a dropped agent child's os.replace() of a valid substituted "
        f"document onto the authoritative record must fail: {replace_attempt.stdout}")

    # and the file on disk is untouched -- neither attempted write produced
    # any partial content, truncation, redirection or ownership change. The
    # VIEW copy, in contrast, is only half-protected, and deliberately so:
    # an in-place write is refused (root-owned 0644) but a directory-ENTRY
    # replacement succeeds, since its parent is the agent-owned run root.
    # Nothing the engine trusts reads it back -- that asymmetry is exactly
    # why the authoritative record no longer lives in this directory.
    # Again through `docker exec` -- see the comment above `protected_check`.
    recheck = subprocess.run(
        [REAL_DOCKER, "exec", container, "cat",
         "/var/lib/ralphd/record/tasks.json"],
        capture_output=True, text=True, timeout=15)
    assert recheck.returncode == 0, recheck.stdout + recheck.stderr
    assert json.loads(recheck.stdout) == tasks_doc
    view_write = subprocess.run(
        [REAL_DOCKER, "exec", "--user", "1000:1000", container, "sh", "-c",
         "echo '{\"tasks\": []}' > /run/ralphd/tasks.json; echo RC=$?"],
        capture_output=True, text=True, timeout=15)
    assert "RC=0" not in view_write.stdout, (
        "an in-place write to the run-root VIEW copy must still be refused "
        f"(root-owned 0644, task 105): {view_write.stdout}")
    view_replace = subprocess.run(
        [REAL_DOCKER, "exec", "--user", "1000:1000", container, "python3", "-c",
         "import os,sys\n"
         # the source has to live on the run dir's OWN mount: /tmp is a
         # different filesystem in this container, and a cross-device
         # rename fails with EXDEV for reasons that have nothing to do
         # with permissions -- which would make this assertion a lie.
         "src = '/run/ralphd/.a202-view-src.json'\n"
         "open(src, 'w').write(sys.argv[1])\n"
         "try:\n"
         "    os.replace(src, '/run/ralphd/tasks.json')\n"
         "except OSError as exc:\n"
         "    print('DENIED', exc.errno)\n"
         "else:\n"
         "    print('REPLACED')\n",
         valid_substitute],
        capture_output=True, text=True, timeout=15)
    assert "REPLACED" in view_replace.stdout, (
        "replacing the VIEW copy's directory entry must still SUCCEED -- if "
        "it did not, this test would be claiming a protection the run dir's "
        f"own ownership cannot give: {view_replace.stdout}")
    still_authoritative = subprocess.run(
        [REAL_DOCKER, "exec", container, "cat",
         "/var/lib/ralphd/record/tasks.json"],
        capture_output=True, text=True, timeout=15)
    assert json.loads(still_authoritative.stdout) == tasks_doc, (
        "substituting the view must not reach the authoritative record")

    # `--on-complete idle` kept this container up for every check above;
    # before task 004 that was harmless litter-wise (`tasks.json` was only
    # ever a root-owned FILE inside an agent-owned parent, which the `ctl`
    # fixture's hard `docker rm -f` teardown could always remove afterward
    # via `shutil.rmtree`). Now the record directory is a root-owned
    # DIRECTORY (on the host, `<registry>/configs/<run-id>/record`) the same
    # teardown cannot remove without the engine's own release funnel running
    # first -- a graceful `ralphctl stop` (a `/shutdown` request, exactly
    # what SIGTERM drives) buys that.
    stop_res = ctl.stop(run_id, force=True)
    assert stop_res.returncode == 0, stop_res.stderr


def test_the_engine_can_still_stop_its_own_iteration(ctl, work_root):
    """The boundary is one-directional on purpose: signals flow downward. A
    `POST /interrupt` still reaches the running iteration -- otherwise the uid
    split would have cost the engine its own control over pi."""
    run_id = _unique_run_id("e048-interrupt")
    # task 010 (B.4): STUB_HOLD_FILE (a container-side path under
    # /workspace, mounted from work_root/ws) holds the worker iteration
    # open deterministically instead of racing a fixed STUB_SLEEP window
    # against how fast the stub would otherwise finish -- no release is
    # needed, since `interrupt` ends the held iteration outright the same
    # way SIGTERM does for the in-process hold-based tests.
    hold_rel = ".hold-interrupt-boundary"
    hold_file_host = work_root / "ws" / hold_rel
    res = ctl.start(run_id, extra_env={"STUB_TASKS": "2",
                                       "STUB_HOLD_FILE": f"/workspace/{hold_rel}",
                                       "STUB_HOLD_PHASE": "worker",
                                       "STUB_HOLD_TIMEOUT_S": "60"},
                    extra=("--on-complete", "idle"))
    assert res.returncode == 0, res.stderr
    meta = json.loads(res.stdout)
    _wait_status(meta["apiUrl"], lambda s: s.get("state") == "running", timeout=60)
    # Task 012 (requirement I, live docker-tier finding): `state: running`
    # is true from the first iteration onward, not proof the worker CHILD
    # this call means to interrupt has actually been spawned yet -- the
    # exact race `tests/conftest.py::wait_hold_entered`'s own docstring
    # names (two hosted-gate flakes at `7e685e2`/`e7115f2`). Caught live
    # under this task's own heavier (root) docker-tier job: `interrupt`
    # landed between the planning iteration's exit and the worker child's
    # spawn, so the API correctly answered 409 "no iteration running" --
    # not a boundary defect, a test racing its own setup. CAUSE and OBSERVE
    # instead of a longer timeout: block until the worker iteration's own
    # hold-entered marker exists, proof the child this test means to
    # interrupt is genuinely parked.
    wait_hold_entered(hold_file_host)

    res = ctl.run("interrupt", run_id)
    assert res.returncode == 0, res.stderr
    deadline = time.time() + 60
    events = (ctl.registry / "runs" / run_id / "events.jsonl")
    while time.time() < deadline:
        text = events.read_text() if events.exists() else ""
        if '"interrupt"' in text or "interrupted" in text:
            break
        time.sleep(0.4)
    else:
        pytest.fail("the engine never recorded reaching its own iteration")

    # same litter hygiene as the two tests above: `--on-complete idle`
    # left this container up, and task 004's record directory is
    # root-owned under this run's CONFIG dir regardless of what this test
    # itself checks.
    stop_res = ctl.stop(run_id, force=True)
    assert stop_res.returncode == 0, stop_res.stderr


def test_the_shipped_image_starts_as_root_and_the_iteration_does_not(ctl, test_image):
    """The two halves of the arrangement, read off a live container: the image
    sets no `USER` (so PID 1 could establish the boundary), and a *tool* the
    iteration runs is uid 1000 with a saved uid of 1000 -- fully dropped, with
    no way back to the engine's real uid."""
    res = subprocess.run([REAL_DOCKER, "image", "inspect", test_image,
                          "--format", "{{.Config.User}}"],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    # `container/Dockerfile` pins no USER at all; the test overlay on top of
    # it re-declares `USER root` for its COPY (and deliberately does not put
    # `USER agent` back). Either way the engine must start as uid 0 -- the one
    # thing that must never appear here is the agent.
    assert res.stdout.strip() in ("", "root", "0"), (
        f"image {test_image} pins USER {res.stdout.strip()!r}: the engine "
        "cannot establish the uid boundary")

    run_id = _unique_run_id("e048-creds")
    res = ctl.start(run_id, extra_env={"STUB_TASKS": "1", "STUB_SLEEP": "0"},
                    extra=("--on-complete", "idle"))
    assert res.returncode == 0, res.stderr
    meta = json.loads(res.stdout)
    _wait_status(meta["apiUrl"], lambda s: s.get("state") in
                 ("succeeded", "failed", "aborted"))
    # the stub records its own environment and identity per invocation; the
    # env dump is written by the iteration process itself, so its owner *is*
    # the uid iterations run as.
    env_marker = ctl.registry / "runs" / run_id / ".stub-env.json"
    assert env_marker.is_file()
    assert env_marker.stat().st_uid == 1000
    assert json.loads(env_marker.read_text())["HOME"] == "/home/agent"
    creds = _engine_creds(f"ralphd-{run_id}")
    assert creds["uid"] == [0, 1000, 0, 1000], creds

    # `--on-complete idle` kept this container up for the credential read
    # above; task 004's record directory is root-owned under this run's
    # config dir too (planning writes it in every run, not only ones that
    # test it directly), so the same graceful-stop hygiene applies here as
    # in `test_a_worker_iteration_cannot_kill_the_engine` above -- without
    # it, this run's state is left with a root-owned directory `ctl`'s hard
    # `docker rm -f` teardown cannot remove, which then poisons the next
    # docker-tier module's own build context (a real failure this task hit
    # live, not a hypothetical).
    stop_res = ctl.stop(run_id, force=True)
    assert stop_res.returncode == 0, stop_res.stderr
