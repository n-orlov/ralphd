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
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
WRAPPER = REPO / "tests" / "docker-hostpath-wrapper" / "docker"
RALPHCTL = Path(sys.executable).parent / "ralphctl"
DOCKER_SOCK = "/var/run/docker.sock"
REAL_DOCKER = shutil.which("docker")

RALPHD_RUN_ID = os.environ.get("RALPHD_RUN_ID", "local-dev")
IMAGE_TAG = f"ralphd:test-{RALPHD_RUN_ID}"
BASE_IMAGE_TAG = f"ralphd:test-{RALPHD_RUN_ID}-base"
OVERLAY_DOCKERFILE = REPO / "tests" / "docker-hostpath-wrapper" / "Dockerfile.stub-overlay"


def _docker_available() -> bool:
    if not REAL_DOCKER or not Path(DOCKER_SOCK).is_socket():
        return False
    return subprocess.run([REAL_DOCKER, "version"],
                          capture_output=True, timeout=10).returncode == 0


pytestmark = [pytest.mark.docker,
              pytest.mark.skipif(not _docker_available(),
                                  reason="docker socket not available")]


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
    yield root
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

    def run(self, *argv: str, timeout: int = 90) -> subprocess.CompletedProcess:
        return subprocess.run([str(RALPHCTL), "--json", *argv], env=self.env,
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
                  interval: float = 0.4) -> dict:
    deadline = time.time() + timeout
    last: dict | None = None
    last_err = None
    while time.time() < deadline:
        try:
            req = urllib.request.Request(api_url + "/status")
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


def test_resume_continues_after_container_stop(ctl):
    run_id = _unique_run_id("e041-resume")
    # STUB_SLEEP gives a wide window to catch the job genuinely mid-run
    # (not yet terminal) before stopping the container out from under it.
    # This test explicitly stops the container mid-run itself (that's the
    # scenario under test), so on_complete's default doesn't matter for the
    # first container -- but keep it explicit for clarity/stability.
    res = ctl.start(run_id, extra_env={"STUB_TASKS": "2", "STUB_SLEEP": "4"},
                    extra=("--on-complete", "idle"))
    assert res.returncode == 0, res.stderr
    meta = json.loads(res.stdout)
    container = f"ralphd-{run_id}"

    status = _wait_status(meta["apiUrl"], lambda s: True, timeout=30)
    assert status.get("state") == "running", (
        "job reached a terminal state before it could be stopped mid-run -- "
        "increase STUB_SLEEP or poll faster")

    res = subprocess.run([REAL_DOCKER, "stop", "-t", "5", container],
                        capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    assert _container_running(container) is False  # stopped, not removed

    rdir = ctl.registry / "runs" / run_id
    pre_status = json.loads((rdir / "status.json").read_text())
    assert pre_status.get("state") != "succeeded", (
        "job already finished before the stop -- not a genuine resume proof")

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
