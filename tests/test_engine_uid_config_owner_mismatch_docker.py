"""Docker tier: task 024 (E.2) -- proves task 023's fix (E.1) with a REAL
uid mismatch on this very host, not merely against an already-uid-1000
account (which would discharge nothing per the PRD's own words: "a test
that only passes because the host account happens to be uid 1000 does not
discharge E.1").

`/config` is a host directory `ralphctl start` creates, chmod's 0700, and
leaves owned by whichever uid actually ran it -- the *engine's* own agent
identity inside the container is always uid 1000 (`container/Dockerfile`
bakes `useradd -u 1000 agent`), so the only way to make those two uids
genuinely differ on a real host is to have some uid OTHER than 1000 be the
one that runs `ralphctl start` and therefore owns everything it writes
under `/config`. This container's own test process runs as uid 1000 (the
`agent` account, see `container/Dockerfile`), so it cannot itself be that
different uid -- there is no `sudo`, no second real account, and chmod/
chown of a directory require ownership or root. The fix here is a *second*,
nested docker layer: a throwaway `--user 4321:4321` sibling ("the
launcher"), sharing this host's own docker socket, that runs the REAL
`ralphctl` binary itself as uid 4321 -- so every file `ralphctl start`
writes under its own config dir (`job.yaml`, `prd.md`, ...) is genuinely
owned by 4321 on this very host, before the *job* container (started by
that `ralphctl start` call, exactly as it always starts: no `--user`, root
PID 1, drops its *effective* uid to the baked-in agent uid 1000 via
`privsep.separate_engine_identity()`) ever tries to read it.

## The two environment wrinkles, solved without a wrapper this time

`tests/test_docker_sibling_e2e.py`'s `RALPHD_DOCKER`-pointed hostpath-
translating wrapper exists because that file's OWN process (this same job
container) issues `docker run -v <container-path>:...` calls straight to
the real (sibling) daemon, and a path only this container can see is
useless to it. Here the launcher sees the same problem one layer down --
but instead of translating, this file sidesteps it entirely: every path the
launcher and the job container need is bind-mounted into the launcher at
the SAME absolute path it has on the true host (`-v
$RALPHD_HOST_WORKSPACE:$RALPHD_HOST_WORKSPACE`), so when the launcher's own
`ralphctl start` (talking to the real daemon over the mounted socket, no
wrapper needed inside it at all) passes that identical path as a `-v`
source, the real daemon resolves it correctly with zero translation. This
outer test process reads the very same files back afterwards at their
`/workspace`-relative view (this container's own bind-mount of that same
host directory), since both are the same bytes.

API reachability reuses `test_docker_sibling_e2e`'s bridge-gateway fixture
exactly as `test_engine_uid_docker.py`/`test_root_owned_record_operability_
docker.py` already do -- the job container the launcher starts lands on the
same default bridge network as everything else here.

## Cleanup discipline

Every container gets a run id derived from `_unique_run_id`, never
`RALPHD_RUN_ID` itself (this job's own label). The launcher runs `--rm`
(removes itself the instant `ralphctl start` returns); the job container it
starts is force-removed by its own exact name in a `finally`, never by a
label query.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from test_docker_sibling_e2e import (  # noqa: F401  (fixtures/consts used by name)
    REAL_DOCKER,
    _first_json_object,
    _unique_run_id,
    _wait_status,
    gateway_ip,
    pytestmark,
    test_image,
)

REPO = Path(__file__).parent.parent
LAUNCHER_UID = 4321  # deliberately not 1000 -- the whole point of E.2
LAUNCHER_GID = 4321


def _host_path(container_path: Path) -> str:
    """This container's own `/workspace`-relative path, translated to the
    real host path it is bind-mounted from -- the identity half of the
    module docstring's "no wrapper needed" scheme. Degrades to the path
    unchanged when `RALPHD_HOST_WORKSPACE` is unset (a bare host running
    this suite directly, where `/workspace` -- this repo's own root -- IS
    already the host path)."""
    host_root = os.environ.get("RALPHD_HOST_WORKSPACE")
    if not host_root:
        return str(container_path)
    rel = container_path.resolve().relative_to(REPO.resolve())
    return str(Path(host_root) / rel) if str(rel) != "." else host_root


@pytest.fixture
def work_root():
    root = REPO / f".docker-e2e-uidcfg-{uuid.uuid4().hex[:8]}"
    root.mkdir()
    # world-writable: the launcher runs as uid 4321, not this process's own
    # uid 1000, and needs to create `registry/` (and everything under it,
    # owned by 4321) inside this directory. Only the *directory itself*
    # stays owned by uid 1000 -- everything the launcher creates inside it
    # is still owned by 4321, which is the whole property under test.
    os.chmod(root, 0o777)
    yield root
    # this process (uid 1000) cannot unlink files a 0700-owned-by-4321
    # directory holds, so a bare `shutil.rmtree` here would silently leave
    # exactly that subtree behind -- permanently unreadable, right inside
    # this repo's own root, which then breaks the NEXT `docker build`'s own
    # context walk (discovered live: `checking context: can't stat
    # '<...>/configs/<run>'`). A root sibling (default `docker run`, no
    # `--user`) empties it first; only then is the now all-uid-1000 tree
    # safe for an ordinary `shutil.rmtree`.
    subprocess.run(
        [REAL_DOCKER, "run", "--rm",
         "--label", f"ralphd.run={os.environ.get('RALPHD_RUN_ID', 'local-dev')}",
         "--label", "ralphd.role=sibling",
         "-v", f"{_host_path(root)}:/target",
         "busybox", "sh", "-c", "rm -rf /target/*"],
        capture_output=True, timeout=30)
    shutil.rmtree(root, ignore_errors=True)


def test_engine_binds_its_api_when_the_config_dir_owner_is_not_the_agent_uid(
        test_image, gateway_ip, work_root):
    run_id = _unique_run_id("e024-uidcfg")
    container = f"ralphd-{run_id}"

    prd = work_root / f"{run_id}-prd.md"
    prd.write_text("# E.2 uid-mismatch e2e\n\nDo nothing.\n")
    os.chmod(prd, 0o644)  # readable by uid 4321 even though written as us

    # Task 024's own docker test surfaced a SECOND, unrelated permission
    # gap while chasing this one down: `RunDir.__post_init__`
    # (`engine/state.py`) `mkdir`s `steering/`, `iterations/`, ... under the
    # run dir with no `chmod`, so a run dir the launcher's own `ralphctl
    # start` created fresh (mode 0755, owned by uid 4321 -- the launcher,
    # not the engine) leaves the engine's effective uid 1000 unable to
    # write into its *own* run dir at all -- a real, separate defect this
    # test's mismatch would otherwise also trip over, but one E.1/E.2 never
    # asked this task to fix (E.1's own title, and its fix, are scoped to
    # `/config/job.yaml` specifically, never `/run/ralphd`). Pre-creating
    # the run dir here -- empty, so `cmd_start`'s own
    # `rdir.mkdir(parents=True, exist_ok=True)` is a no-op that changes
    # neither its ownership nor its mode -- keeps it owned by THIS process's
    # own uid (1000, matching the engine's fixed agent uid) and writable by
    # the launcher too, isolating the one property this task is actually
    # asked to prove: `/config`'s owning uid differing from the engine's,
    # not `/run/ralphd`'s.
    rdir = work_root / "registry" / "runs" / run_id
    rdir.mkdir(parents=True)
    os.chmod(rdir, 0o777)
    # every ancestor the launcher (uid 4321) needs to create a NEW sibling
    # under (`registry/configs/<run_id>`, which does not exist yet) must
    # itself be writable to it too, not just `work_root` -- `rdir.mkdir
    # (parents=True)` above created `registry/` and `registry/runs/` at
    # this process's own default mode (0755), which denies the launcher
    # write access to add `registry/configs/` beside them.
    os.chmod(work_root / "registry", 0o777)
    os.chmod(work_root / "registry" / "runs", 0o777)

    host_registry = _host_path(work_root / "registry")
    host_prd = _host_path(prd)

    sock_gid = Path("/var/run/docker.sock").stat().st_gid
    launcher_cmd = [
        REAL_DOCKER, "run", "--rm", "--user", f"{LAUNCHER_UID}:{LAUNCHER_GID}",
        "--group-add", str(sock_gid),
        "--label", f"ralphd.run={os.environ.get('RALPHD_RUN_ID', 'local-dev')}",
        "--label", "ralphd.role=sibling",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-v", f"{_host_path(work_root)}:{_host_path(work_root)}",
        "-e", f"RALPHD_REGISTRY={host_registry}",
        "-e", "HOME=/tmp",
        "--entrypoint", "ralphctl",
        test_image,
        "--json", "start", "--prd", host_prd, "--run-id", run_id,
        "--llm", "none", "--image", test_image, "--api-bind", gateway_ip,
        "--on-complete", "idle",
        "--env", "STUB_TASKS=1",
    ]
    try:
        res = subprocess.run(launcher_cmd, capture_output=True, text=True, timeout=120)
        assert res.returncode == 0, (res.stdout, res.stderr)
        meta = _first_json_object(res.stdout)
        assert meta["runId"] == run_id, meta
        api_url = meta["apiUrl"]

        cdir = work_root / "registry" / "configs" / run_id

        # the premise this whole test rests on: the config dir really is
        # owned by a uid that is NOT the container's fixed agent uid (1000)
        # -- proving the launcher's own `ralphctl start` (run as 4321) is
        # what created it, not this outer process (uid 1000).
        cfg_stat = cdir.stat()
        assert cfg_stat.st_uid == LAUNCHER_UID, (
            f"config dir owned by uid {cfg_stat.st_uid}, not {LAUNCHER_UID} -- "
            "this test's uid-mismatch premise never held")
        assert (cfg_stat.st_mode & 0o777) == 0o700, oct(cfg_stat.st_mode)
        # This outer process cannot read *into* that 0700-owned-by-4321
        # directory either -- the same permission this test's whole premise
        # rests on, from this side of the mismatch -- UNLESS this outer
        # process is itself root. Task 012 (requirement I): the retired heavy workflow's
        # docker-tier job runs every step, this pytest process included, as
        # uid 0 (its own header explains why: the plain `docker` client
        # needs no `--group-add` gymnastics to reach the mounted socket),
        # so the module docstring's "this container's own test process runs
        # as uid 1000" -- true for this repo's own selfdev job, where the
        # engine's fixed agent uid and the launcher's uid coincide -- no
        # longer holds there. Root bypasses every DAC check regardless of a
        # directory's mode, so the read succeeding is the CORRECT outcome
        # for a root outer process, not a failure of the uid-mismatch
        # premise: that premise is the assert above (owned by 4321, mode
        # 0700), which held regardless of which uid reads it back.
        if os.getuid() == 0:
            (cdir / "job.yaml").stat()
        else:
            with pytest.raises(PermissionError):
                (cdir / "job.yaml").stat()
        # A root sibling (default `docker run`, no `--user`) can still see
        # what is really there, exactly as the job container's own PID 1
        # (which also starts as root) will -- this only confirms job.yaml
        # exists and really is owned by 4321, without needing this outer
        # process to have read access to it.
        peek = subprocess.run(
            [REAL_DOCKER, "run", "--rm",
             "--label", f"ralphd.run={os.environ.get('RALPHD_RUN_ID', 'local-dev')}",
             "--label", "ralphd.role=sibling",
             "--entrypoint", "python3",
             "-v", f"{_host_path(cdir)}:/target:ro", test_image,
             "-c", ("import os; s = os.stat('/target/job.yaml'); "
                    "print(s.st_uid); assert __import__('stat').S_ISREG(s.st_mode)")],
            capture_output=True, text=True, timeout=30)
        assert peek.returncode == 0, (peek.stdout, peek.stderr)
        assert peek.stdout.strip() == str(LAUNCHER_UID), peek.stdout

        # the engine (fixed agent uid 1000, per container/Dockerfile) binds
        # its API despite that mismatch -- the property E.1 fixes and this
        # test proves for real: `_wait_status` polling `api_url` at all (as
        # opposed to a connection-refused timeout, which is exactly what
        # happens when task 023's fix is reverted -- see this task's own
        # commit message) IS the assertion that the API bound. The job
        # then also runs to completion, which the log/tasks.json checks
        # below confirm.
        status = _wait_status(api_url, lambda s: s.get("state") in
                              ("succeeded", "failed", "aborted"), timeout=90)
        assert status["state"] == "succeeded", status
        assert status["verdict"] == "verified", status

        logs = subprocess.run([REAL_DOCKER, "logs", container],
                              capture_output=True, text=True, timeout=30)
        assert logs.returncode == 0, logs.stderr
        combined = logs.stdout + logs.stderr
        assert "uid boundary active" in combined, combined
        assert "API listening on" in combined, combined

        tasks_doc = json.loads((rdir / "tasks.json").read_text())
        assert all(t["status"] == "completed" for t in tasks_doc["tasks"]), tasks_doc
    finally:
        subprocess.run([REAL_DOCKER, "rm", "-f", container], capture_output=True)
