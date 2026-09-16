"""Container-level docker-sibling e2e tests (task 041, PRD testing rules).

Marked ``@pytest.mark.docker``; the whole module skips cleanly if the docker
socket is unreachable. Builds ``container/Dockerfile`` as a real sibling
image tagged ``ralphd:test-<RALPHD_RUN_ID>`` (labeled
``ralphd.run=<RALPHD_RUN_ID>``), then drives the *real* ``ralphctl`` binary
against the *real* docker daemon (never ``tests/stub-docker``) to prove: creds
land at ``~/.creds`` in-container, skills are symlinked, the API is reachable,
run-dir files appear host-side, ``stop`` reaps the container, ``resume`` works
after a container stop, and ``--no-detach`` exits correctly on both verdicts.

Every assertion is made purely through the HTTP API, ``docker`` CLI
introspection (never internal Python imports of the engine/CLI), and the run
dir's own files -- strictly black-box, per the PRD's testing rules.

## Two environment-specific wrinkles this file has to work around

This test file is itself executed *inside* a ralphd job container (this very
selfdev job's own engine container), not on a bare host shell. Two
consequences follow directly from that, both solved without touching any
non-test source file:

1. **Bind-mount path translation.** ``ralphctl`` computes every
   ``-v SRC:DST`` mount source from ``RALPHD_REGISTRY`` and passes that exact
   string to ``docker run``. For the real daemon (a sibling from this
   container's point of view) to resolve SRC on its own filesystem, SRC must
   be a real host path -- but this container's own filesystem only exposes
   ``/workspace`` (bind-mounted from ``$RALPHD_HOST_WORKSPACE``) and
   ``/run/ralphd`` (from ``$RALPHD_HOST_RUN_DIR``). ``RALPHD_DOCKER`` (an env
   var ``ralphctl`` already reads everywhere it shells out to ``docker``,
   used elsewhere for the recording stub) is pointed at
   ``tests/docker-hostpath-wrapper/docker``, a thin real-docker-executing
   shim that rewrites any ``-v`` source under ``/workspace`` to the
   ``$RALPHD_HOST_WORKSPACE``-prefixed equivalent before exec'ing the real
   binary. This is why every temp dir this file uses lives under
   ``/workspace`` (this repo's own root), matching the requirement that "the
   registry for these tests must live inside the workspace so the
   container's mounts resolve". On a bare host (no ``RALPHD_HOST_WORKSPACE``
   set) the map degenerates to a no-op and every path is used as-is.
2. **API reachability.** ``-p 127.0.0.1:PORT:7777`` (the default
   ``--api-bind``) publishes on the *host's own* loopback network
   namespace, which this container does not share (it is a separate
   container, not the true host). The docker0 bridge gateway address is a
   real interface of the host's own network namespace and *is* reachable
   from any container on the default bridge network, so every
   ``start``/``resume`` call below passes ``--api-bind <bridge-gateway-ip>``
   (discovered once via ``docker network inspect bridge``) -- this makes
   ``ralphctl``'s own built-in status/event-stream polling
   (``--no-detach``, event follow) work completely unmodified, exactly as it
   would from a real host shell where 127.0.0.1 is the right answer.

## Discovered gap: `resume` does not re-apply `--env`

While building the resume test below, this file surfaced a genuine,
pre-existing gap: `ralphctl resume` (`cmd_resume()`, `src/ralphd/cli/main.py`)
does not persist or re-apply arbitrary `--env`/`--llm-env`/`--forward-env`
values from the original `start` invocation -- a fresh container made by
`resume` only gets the `.api-token` (if any) and, with `--allow-docker`, the
socket/host-path vars; everything else from `start`'s own `--env` is simply
lost. This test file works around it (by baking the stub `pi` into the test
image itself, see `test_image`, instead of depending on a `PATH`/
`STUB_RUN_DIR` env override surviving a resume) rather than depending on a
fix, since fixing the CLI is out of scope for this task; it is filed as its
own follow-up task in `tasks.json` (a real production job using `--llm host`
or a custom `--env`/`--forward-env` would suffer the same loss of env on
resume).

## Cleanup discipline

This job's *own* production engine container carries the exact same
``ralphd.run=<RALPHD_RUN_ID>`` label value that the PRD's labeling
convention asks siblings to use (since ``RALPHD_RUN_ID`` here is this whole
selfdev job's run id, not a fresh one). A label-filtered reap query
(``docker ps --filter label=ralphd.run=<RALPHD_RUN_ID>``) would therefore be
able to match -- and destroy -- this very container. Every container this
file creates instead gets its own freshly generated, unique run id (used for
``--run-id``/the resulting ``ralphd-<run_id>`` container name and, in turn,
that job's own ``ralphd.run=<run_id>`` label), and cleanup always force-removes
by that exact tracked name, never by a label query. Only the *image* build
uses ``ralphd.run=<RALPHD_RUN_ID>`` (per the PRD's literal instruction) --
images aren't touched by ``docker ps``/container reaping at all, so this is
safe, and the image is removed by its own exact tag at teardown.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from tests.conftest import RALPHCTL

REPO = Path(__file__).parent.parent
WRAPPER = REPO / "tests" / "docker-hostpath-wrapper" / "docker"
DOCKER_SOCK = "/var/run/docker.sock"
REAL_DOCKER = shutil.which("docker")

RALPHD_RUN_ID = os.environ.get("RALPHD_RUN_ID", "local-dev")
IMAGE_TAG = f"ralphd:test-{RALPHD_RUN_ID}"
BASE_IMAGE_TAG = f"ralphd:test-{RALPHD_RUN_ID}-base"
OVERLAY_DOCKERFILE = REPO / "tests" / "docker-hostpath-wrapper" / "Dockerfile.stub-overlay"


def _docker_available() -> bool:
    # `tests/records/test_conformance_ledger.py`'s in-process node-id
    # collection (task 033, E.2) imports this module -- transitively, via
    # `tests/test_worker_hat.py` -> `tests/test_steer.py` -- purely to check
    # a cited node exists, never to run it; the module-level skipif below
    # would otherwise spend a real `docker version` subprocess call on a
    # collect-only pass that never looks at its result (task 035, E.4: the
    # records family's own subprocess budget is `git ls-files` alone).
    if os.environ.get("RALPHD_SKIP_DOCKER_PROBE"):
        return False
    if not REAL_DOCKER or not Path(DOCKER_SOCK).is_socket():
        return False
    return subprocess.run([REAL_DOCKER, "version"],
                          capture_output=True, timeout=10).returncode == 0


pytestmark = [pytest.mark.docker,
              pytest.mark.skipif(not _docker_available(),
                                  reason="docker socket not available")]


def _host_path(path: Path) -> str:
    """`path`'s spelling on the REAL host, which is what a sibling
    container's `-v` source must be. Identical to `path` on a bare host or
    a CI runner; inside this repo's own selfdev job container `/workspace`
    is itself a bind mount, so the same translation the hostpath-wrapper
    applies to `ralphctl`'s mounts applies here (see this module's
    docstring)."""
    host_repo = os.environ.get("RALPHD_HOST_WORKSPACE")
    if not host_repo:
        return str(path)
    rel = path.relative_to(REPO)
    return str(Path(host_repo) / rel) if str(rel) != "." else host_repo


def _reap_image() -> str:
    """The image the reapers below run root in: whatever
    `container/Dockerfile` is FROM. Deliberately NOT this tier's own
    `test_image` -- that fixture removes its tags when it finalizes, and a
    reaper must not depend on being ordered before it -- and not a
    hardcoded name either, so the base stays a single source of truth."""
    for line in (REPO / "container" / "Dockerfile").read_text().splitlines():
        if line.startswith("FROM "):
            return line.split()[1]
    return ""


def _reap_root_owned(target: Path) -> None:
    """Remove `target` (a path inside this repo) as ROOT, through a sibling
    container, and tolerate every failure.

    Why this exists (task 004, requirement B, #202): the authoritative task
    record lives in a root-owned 0700 directory bind-mounted from
    `<registry>/configs/<run-id>/record`, and the registry of this tier is a
    throwaway directory inside the repo checkout (`work_root`, which must
    live here for the hostpath translation to work). The engine hands that
    directory back to the agent uid at its own graceful shutdown, but a test
    that ends with `docker rm -f` -- the `ctl` fixture's own teardown, and
    any hard container death -- never reaches that funnel, so the directory
    survives as root-owned litter that NOTHING unprivileged can remove:
    `shutil.rmtree(ignore_errors=True)` leaves it, the next docker build's
    context walk cannot even stat it, and on a self-hosted runner the
    workspace cleanup step fails the whole job (heavy 35105603345, red with
    a green test tier for exactly this reason). Root inside a container is
    the only identity that can clear it, and this tier already has one.

    Never a hard failure: if docker is unavailable, this is a no-op and the
    caller's own `shutil.rmtree` still does what it can."""
    image = _reap_image()
    if not REAL_DOCKER or not image:
        return
    if not target.name.startswith(".docker-e2e-tmp-"):
        return
    subprocess.run(
        [REAL_DOCKER, "run", "--rm", "--user", "0:0",
         "--label", f"ralphd.run={RALPHD_RUN_ID}",
         "--label", "ralphd.role=sibling",
         "-v", f"{_host_path(REPO)}:/reap", image,
         "sh", "-c", f"rm -rf /reap/{target.name}"],
        capture_output=True, timeout=120)


SESSION_MARKER = ".session-pid"


def _is_stale_litter(d: Path) -> bool:
    """Whether `d` -- a `.docker-e2e-tmp-*` directory in this repo -- belongs
    to a pytest process that is no longer alive. `work_root` stamps its own
    pid into `SESSION_MARKER` when it creates one, so a CONCURRENT module
    (this tier runs `-n 2 --dist loadfile` in CI) is never swept out from
    under itself: an unreadable marker means "from before this convention",
    a live pid means "leave it alone", and only a dead or absent owner makes
    the directory litter. Conservative on purpose -- a false "live" leaves a
    directory behind, a false "stale" would delete a running test's registry."""
    try:
        pid = int((d / SESSION_MARKER).read_text().strip())
    except (OSError, ValueError):
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False   # exists but not ours to signal: assume live
    return False


def _reap_stale_litter() -> None:
    """The same reaping as `_reap_root_owned`, for litter a PREVIOUS session
    left behind: a self-hosted runner's workspace is reused across jobs, so a
    root-owned `record/` directory from a hard-killed container would
    otherwise poison the very first `docker build` of this session (its
    context walk) and the workspace cleanup of every job after it. Runs
    before this module's own build for that reason, in `_reap_image()` --
    the one image guaranteed resolvable before anything of ours is built."""
    base = _reap_image()
    if not REAL_DOCKER or not base:
        return
    stale = [d.name for d in REPO.glob(".docker-e2e-tmp-*")
             if d.is_dir() and _is_stale_litter(d)]
    if not stale:
        return
    subprocess.run(
        [REAL_DOCKER, "run", "--rm", "--user", "0:0",
         "--label", f"ralphd.run={RALPHD_RUN_ID}",
         "--label", "ralphd.role=sibling",
         "-v", f"{_host_path(REPO)}:/reap", base,
         "sh", "-c", "cd /reap && rm -rf " + " ".join(
             f"./{name}" for name in stale)],
        capture_output=True, timeout=300)


def _bridge_gateway() -> str:
    res = subprocess.run(
        [REAL_DOCKER, "network", "inspect", "bridge", "--format",
         "{{(index .IPAM.Config 0).Gateway}}"],
        capture_output=True, text=True, timeout=15)
    assert res.returncode == 0, res.stderr
    ip = res.stdout.strip()
    assert ip, "could not determine the docker0 bridge gateway address"
    return ip


def _unique_run_id(prefix: str) -> str:
    rid = f"{prefix}-{uuid.uuid4().hex[:8]}"
    assert rid != RALPHD_RUN_ID  # never collide with this job's own label
    return rid


@pytest.fixture(scope="module")
def gateway_ip():
    return _bridge_gateway()


@pytest.fixture(scope="module")
def test_image():
    """Build container/Dockerfile as a sibling, tagged+labeled per the PRD,
    then layer on a thin test-only overlay (Dockerfile.stub-overlay) that
    bakes the stub `pi` into the image *permanently*, replacing the real
    npm-installed binary, instead of relying on a `PATH`/`STUB_RUN_DIR`
    override passed via `docker run -e` -- `ralphctl resume` does not
    currently re-apply arbitrary `--env` values from the original `start`
    invocation (a real gap this test file's resume test discovered; see
    the module docstring and the new task filed for it), so every
    container made from this final tag -- including ones `resume` creates
    fresh -- must resolve `pi` to the stub with zero env dependency.
    Both stages are layer-cached against this daemon's prior builds (e.g.
    this very job's own ``ralphd:dev``), so this is fast in practice even
    though both are genuine, from-source ``docker build``s."""
    # before the first build: clear any root-owned litter an earlier
    # session left in this repo, which the build context walk would
    # otherwise trip over (see `_reap_stale_litter`).
    _reap_stale_litter()
    res = subprocess.run(
        [REAL_DOCKER, "build", "-f", str(REPO / "container" / "Dockerfile"),
         "-t", BASE_IMAGE_TAG, "--label", f"ralphd.run={RALPHD_RUN_ID}", str(REPO)],
        capture_output=True, text=True, timeout=1800)
    assert res.returncode == 0, (res.stdout[-4000:] + res.stderr[-4000:])
    res = subprocess.run(
        [REAL_DOCKER, "build", "-f", str(OVERLAY_DOCKERFILE),
         "--build-arg", f"BASE_IMAGE={BASE_IMAGE_TAG}",
         "-t", IMAGE_TAG, "--label", f"ralphd.run={RALPHD_RUN_ID}",
         str(REPO / "tests")],
        capture_output=True, text=True, timeout=300)
    assert res.returncode == 0, (res.stdout[-4000:] + res.stderr[-4000:])
    yield IMAGE_TAG
    subprocess.run([REAL_DOCKER, "rmi", "-f", IMAGE_TAG], capture_output=True)
    subprocess.run([REAL_DOCKER, "rmi", "-f", BASE_IMAGE_TAG], capture_output=True)


@pytest.fixture(scope="module")
def work_root():
    """Everything under here is bind-mounted into siblings (via the
    hostpath-wrapper's /workspace translation), so it must live under this
    repo's own workspace root -- never /tmp."""
    root = REPO / f".docker-e2e-tmp-{uuid.uuid4().hex[:8]}"
    ws = root / "ws"
    ws.mkdir(parents=True)
    (root / "registry").mkdir()
    creds_src = root / "creds-src"
    creds_src.mkdir()
    (creds_src / "testcred.env").write_text("TESTCRED_MARKER=e2e-041-fixture-value\n")
    skills_src = root / "skills-src" / "demo-skill"
    skills_src.mkdir(parents=True)
    (skills_src / "SKILL.md").write_text("# demo-skill\n\nA fixture skill for task 041.\n")
    # whoever sweeps stale litter later can tell a live registry from a
    # dead one by this marker (`_is_stale_litter`).
    (root / SESSION_MARKER).write_text(f"{os.getpid()}\n")
    yield root
    # root first (the task record's own directory, if any test in this
    # module left one behind), then the ordinary unprivileged sweep.
    _reap_root_owned(root)
    shutil.rmtree(root, ignore_errors=True)


class Ctl:
    """Real `ralphctl` runner: real docker (via the path-translating
    wrapper), a temp registry under the workspace, unique run ids."""

    def __init__(self, work_root: Path, gateway_ip: str, image: str):
        self.registry = work_root / "registry"
        self.ws = work_root / "ws"
        self.gateway_ip = gateway_ip
        self.image = image
        self.env = {
            **os.environ,
            "RALPHD_REGISTRY": str(self.registry),
            "RALPHD_DOCKER": str(WRAPPER),
            "RALPHD_REAL_DOCKER": REAL_DOCKER,
            "RALPHD_HOSTPATH_MAP": json.dumps(
                {"/workspace": os.environ.get("RALPHD_HOST_WORKSPACE", "/workspace")}),
        }
        self.cleanup: list[str] = []  # exact container names to force-remove

    def run(self, *argv: str, timeout: int = 90,
            env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([str(RALPHCTL), "--json", *argv],
                              env=env or self.env,
                              capture_output=True, text=True, timeout=timeout)

    def start(self, run_id: str, *, extra_env: dict | None = None,
               extra: tuple = (), timeout: int = 90) -> subprocess.CompletedProcess:
        self.cleanup.append(f"ralphd-{run_id}")
        prd = self.registry / f"{run_id}-prd.md"
        prd.parent.mkdir(parents=True, exist_ok=True)
        prd.write_text("# Container e2e PRD\n\nDo the thing.\n")
        argv = ["start", "--prd", str(prd), "--run-id", run_id, "--llm", "none",
                "--image", self.image, "--api-bind", self.gateway_ip,
                "--workspace", str(self.ws)]
        for k, v in (extra_env or {}).items():
            argv += ["--env", f"{k}={v}"]
        argv += list(extra)
        return self.run(*argv, timeout=timeout)

    def stop(self, run_id: str, force: bool = True, timeout: int = 30):
        argv = ["stop", run_id] + (["--force"] if force else [])
        return self.run(*argv, timeout=timeout)

    def resume(self, run_id: str, *, extra: tuple = (),
                timeout: int = 90) -> subprocess.CompletedProcess:
        argv = ["resume", run_id, "--image", self.image,
                "--api-bind", self.gateway_ip, *extra]
        return self.run(*argv, timeout=timeout)

    def force_cleanup(self):
        for name in self.cleanup:
            subprocess.run([REAL_DOCKER, "rm", "-f", name], capture_output=True)


@pytest.fixture
def ctl(work_root, gateway_ip, test_image):
    c = Ctl(work_root, gateway_ip, test_image)
    yield c
    c.force_cleanup()


def _wait_status(api_url: str, predicate, timeout: float = 90,
                  interval: float = 0.4, headers: dict | None = None) -> dict:
    deadline = time.time() + timeout
    last: dict | None = None
    last_err = None
    while time.time() < deadline:
        try:
            req = urllib.request.Request(api_url + "/status")
            for name, value in (headers or {}).items():
                req.add_header(name, value)
            with urllib.request.urlopen(req, timeout=5) as resp:
                last = json.loads(resp.read())
            if predicate(last):
                return last
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            last_err = e
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for status predicate; "
                         f"last={last} last_err={last_err}")


def _docker_exec(name: str, *cmd: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run([REAL_DOCKER, "exec", name, *cmd],
                          capture_output=True, text=True, timeout=timeout)


def _container_running(name: str) -> bool | None:
    res = subprocess.run([REAL_DOCKER, "inspect", "--format", "{{.State.Running}}",
                          name], capture_output=True, text=True, timeout=15)
    if res.returncode != 0:
        return None
    return res.stdout.strip() == "true"


# --------------------------------------------------------------------------
def test_creds_placed_skills_symlinked_api_reachable_rundir_files_appear(ctl, work_root):
    run_id = _unique_run_id("e041-happy")
    # This test inspects the container after it reaches a terminal state
    # (docker exec for creds/skills checks), so it needs the container kept
    # alive rather than the product default of tearing itself down on
    # completion (task 010 flipped the default on_complete to "exit").
    res = ctl.start(run_id, extra_env={"STUB_TASKS": "2", "STUB_SLEEP": "0"},
                    extra=("--creds", str(work_root / "creds-src"),
                           "--skills", str(work_root / "skills-src"),
                           "--on-complete", "idle"))
    assert res.returncode == 0, res.stderr
    meta = json.loads(res.stdout)
    api_url = meta["apiUrl"]
    container = f"ralphd-{run_id}"

    status = _wait_status(api_url, lambda s: s.get("state") in
                          ("succeeded", "failed", "aborted"))
    assert status["verdict"] == "verified", status

    # creds: engine places *.env content verbatim under ~/.creds (task 006)
    cat = _docker_exec(container, "cat", "/home/agent/.creds/testcred.env")
    assert cat.returncode == 0, cat.stderr
    assert "TESTCRED_MARKER=e2e-041-fixture-value" in cat.stdout

    # skills: engine symlinks ~/.pi/agent/skills/<name> -> /config/skills/<name>
    link = _docker_exec(container, "readlink", "-f",
                        "/home/agent/.pi/agent/skills/demo-skill")
    assert link.returncode == 0, link.stderr
    assert link.stdout.strip() == "/config/skills/demo-skill"

    # run-dir files appear host-side: the run dir is bind-mounted from a
    # real host path (via the hostpath-wrapper translation), so this
    # container's own filesystem view of it *is* the host-side view.
    rdir = ctl.registry / "runs" / run_id
    assert (rdir / "status.json").is_file()
    tasks_doc = json.loads((rdir / "tasks.json").read_text())
    assert len(tasks_doc["tasks"]) == 2
    assert all(t["status"] == "completed" for t in tasks_doc["tasks"])
    assert (rdir / "iterations").is_dir() and any((rdir / "iterations").iterdir())


def test_stop_reaps_container_and_keeps_run_dir(ctl):
    run_id = _unique_run_id("e041-stop")
    # Needs the container to still be alive at terminal state so the
    # explicit "ralphctl stop --force" below is the thing that reaps it,
    # not the on_complete=exit default (task 010).
    res = ctl.start(run_id, extra_env={"STUB_TASKS": "1", "STUB_SLEEP": "0"},
                    extra=("--on-complete", "idle"))
    assert res.returncode == 0, res.stderr
    meta = json.loads(res.stdout)
    container = f"ralphd-{run_id}"
    assert _container_running(container) is True

    _wait_status(meta["apiUrl"], lambda s: s.get("state") in
                 ("succeeded", "failed", "aborted"))

    res = ctl.stop(run_id, force=True)
    assert res.returncode == 0, res.stderr
    assert _container_running(container) is None  # container gone entirely
    rdir = ctl.registry / "runs" / run_id
    assert rdir.is_dir() and (rdir / "status.json").is_file()  # run dir kept


def test_resume_continues_after_container_stop(ctl, work_root):
    run_id = _unique_run_id("e041-resume")
    # STUB_HOLD_FILE (a container-side path under /workspace, mounted from
    # `work_root/ws` below) blocks the job indefinitely -- deterministically
    # catching it genuinely mid-run before the stop, rather than racing a
    # fixed STUB_SLEEP window against how fast the container reaches a
    # terminal state. Unlike every other hold-based test in this plan, this
    # one MUST release the hold before calling `resume`: `cmd_resume`
    # reproduces `start`'s resolved `--env` pairs verbatim via
    # `_read_extra_env_wiring` (`src/ralphd/cli/main.py`), so the resumed
    # container's own stub-pi invocation(s) see the exact same
    # STUB_HOLD_FILE/STUB_HOLD_TIMEOUT_S again -- an unreleased hold would
    # just as deterministically re-block the resumed run for the same 60s
    # (confirmed live: the resumed container sat in `phase: planning`,
    # never crashing, until the bound). Touching the file after the stop
    # (below) is the same release signal every hold-based test uses, only
    # applied across the resume boundary instead of within one process's
    # lifetime; the module's own docstring claim that `resume` drops
    # arbitrary `--env` is stale for this exact pair -- superseded by the
    # `_read_extra_env_wiring` reproduction path, not re-verified here since
    # it is out of this task's scope.
    hold_rel = ".hold-resume-boundary"
    hold_file_host = work_root / "ws" / hold_rel
    res = ctl.start(run_id, extra_env={"STUB_TASKS": "2",
                                       "STUB_HOLD_FILE": f"/workspace/{hold_rel}",
                                       "STUB_HOLD_TIMEOUT_S": "60"},
                    extra=("--on-complete", "idle"))
    assert res.returncode == 0, res.stderr
    meta = json.loads(res.stdout)
    container = f"ralphd-{run_id}"

    status = _wait_status(meta["apiUrl"], lambda s: True, timeout=30)
    assert status.get("state") == "running", (
        "job reached a terminal state before it could be stopped mid-run")

    res = subprocess.run([REAL_DOCKER, "stop", "-t", "5", container],
                        capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    assert _container_running(container) is False  # stopped, not removed

    rdir = ctl.registry / "runs" / run_id
    pre_status = json.loads((rdir / "status.json").read_text())
    assert pre_status.get("state") != "succeeded", (
        "job already finished before the stop -- not a genuine resume proof")

    hold_file_host.touch()  # release: the resumed container's own hold
                            # checks (same env, reapplied) pass instantly

    res = ctl.resume(run_id, extra=("--iterations", "+10"))
    assert res.returncode == 0, res.stderr
    meta2 = json.loads(res.stdout)
    assert meta2["container"] != meta["container"]  # a genuinely fresh container

    final = _wait_status(meta2["apiUrl"], lambda s: s.get("state") in
                         ("succeeded", "failed", "aborted"), timeout=90)
    assert final["verdict"] == "verified", final
    tasks_doc = json.loads((rdir / "tasks.json").read_text())
    assert len(tasks_doc["tasks"]) == 2
    assert all(t["status"] == "completed" for t in tasks_doc["tasks"])
    # iteration numbering carried over strictly increasing, no resets/dupes
    iter_dirs = sorted(int(p.name) for p in (rdir / "iterations").iterdir())
    assert iter_dirs == sorted(set(iter_dirs))
    assert len(iter_dirs) >= 2


def _first_json_object(stdout: str) -> dict:
    """Parse only the leading JSON object of a `--json` CLI stdout.

    `--no-detach` prints the start meta object first and then *streams the
    run's events* on the same stdout until the job ends, so the buffer is a
    JSON object followed by arbitrary trailing lines -- `json.loads()` over
    the whole thing only works when the CLI happens to lose the race to
    connect to `/events` and streams nothing (flaky by construction). Decode
    just the first value and ignore whatever the follower appended."""
    obj, _end = json.JSONDecoder().raw_decode(stdout.lstrip())
    assert isinstance(obj, dict), obj
    return obj


def test_no_detach_exits_0_on_verified_and_nonzero_when_unverified(ctl):
    run_id = _unique_run_id("e041-nodetach-ok")
    res = ctl.start(run_id, extra_env={"STUB_TASKS": "1", "STUB_SLEEP": "0"},
                    extra=("--on-complete", "exit", "--no-detach"), timeout=90)
    assert res.returncode == 0, (res.stdout, res.stderr)
    meta = _first_json_object(res.stdout)
    assert meta["runId"] == run_id

    bad_id = _unique_run_id("e041-nodetach-bad")
    res = ctl.start(bad_id,
                    extra_env={"STUB_TASKS": "1", "STUB_SLEEP": "0",
                               "STUB_REVIEW_FAILS": "99"},
                    extra=("--iterations", "3", "--max-approaches", "1",
                           "--on-complete", "exit", "--no-detach"),
                    timeout=90)
    assert res.returncode != 0, (res.stdout, res.stderr)
