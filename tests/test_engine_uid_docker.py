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
   (`Uid: 0 1000 0 1000` -- real/saved root, effective agent), and every file
   it wrote into the bind-mounted run dir is still owned by uid 1000, which is
   the ownership half of the contract this route exists to preserve.

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

    # 3. the documented credential shape, and unchanged file ownership
    creds = _engine_creds(container)
    assert creds["uid"] == [0, 1000, 0, 1000], creds  # real, effective, saved, fs
    assert creds["gid"] == [0, 1000, 0, 1000], creds
    assert "uid boundary active" in _container_log(container)
    for name in ("status.json", "events.jsonl", "tasks.json"):
        path = rdir / name
        assert path.is_file(), name
        assert path.stat().st_uid == 1000, (
            f"{name} is owned by uid {path.stat().st_uid}: the engine's "
            "effective uid must stay the agent's so the host user still owns "
            "its own run dir")


def test_the_engine_can_still_stop_its_own_iteration(ctl):
    """The boundary is one-directional on purpose: signals flow downward. A
    `POST /interrupt` still reaches the running iteration -- otherwise the uid
    split would have cost the engine its own control over pi."""
    run_id = _unique_run_id("e048-interrupt")
    res = ctl.start(run_id, extra_env={"STUB_TASKS": "2", "STUB_SLEEP": "30"},
                    extra=("--on-complete", "idle"))
    assert res.returncode == 0, res.stderr
    meta = json.loads(res.stdout)
    _wait_status(meta["apiUrl"], lambda s: s.get("state") == "running", timeout=60)

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
