"""The derived job image, built for real against the real docker daemon
(task 039, #20 H2).

Every other image test in this repo is fast by construction: `cli/image.py` is
docker-free, and `main.resolve_job_image()` is driven over
`tests/stub-docker/docker`, which *records* the argv it was given and answers
the image cache from a text file. That is the right way to pin argv, exit codes
and cache rules -- and it cannot tell us the one thing requirement H2 actually
promises: that the **generated recipe works**. A stub says `docker build`
was invoked; only a daemon says the resulting image has an engine in it.

So this module is the one place that spends real time and real bytes:

* it builds a genuinely minimal base for real (`debian:bookworm-slim` plus a
  marker file -- no python, no node, no curl, no user 1000), which is exactly
  the "the user's image only has to carry the toolchain their repo needs" case
  H2 exists for;
* it then calls the **production** path, `main.resolve_job_image(None,
  base=...)`, over this checkout, so the recipe under test is the one an
  operator gets, built with the builder the production code chooses
  (`_image_build_env()` pins `DOCKER_BUILDKIT=0`: a job container ships the
  static docker client only, with no buildx plugin);
* it asserts the derived image **runs `ralphd-engine`** -- both directly and
  through the shipped `container/entrypoint.sh`, which is the only path a real
  job ever takes -- and that the base survived underneath it;
* it asserts the second resolve of the same base is a pure tag lookup against a
  real daemon, not just against the stub's text file.

Cost: one ~90s build per run of the docker tier (the layers cannot be reused
across runs because a fresh base image id invalidates all of them, which is
precisely the invalidation rule H2 wants). That is the price of the only test
that can fail when the generated Dockerfile is wrong.

## Why this can and does run here

The task that added this module allowed for the possibility that a real build
is impossible inside a ralphd job container and would have to be recorded as
operator-verified-on-host instead. It is not impossible, and no report is
needed: unlike `docker run -v`, `docker build` needs no host-path translation
at all -- the build context is streamed to the daemon by the client, so paths
under this container's own `/tmp` and `/workspace` are fine, and the resulting
containers need no mounts to answer `--version`. See
`artifacts/reports/real-build-tier.md` for the recorded evidence of the run.

## Cleanup discipline

Both images are removed by their exact tags at teardown, and every container
this module starts is `--rm` and labeled `ralphd.run=<RALPHD_RUN_ID>` *and*
`ralphd.role=sibling`, so a role-filtered reap can find them and can never
match this job's own container. The base tag carries the same two labels; the
derived image is built by production code, which labels nothing, so it is
tracked by its returned tag and removed by that.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from ralphd.cli import image, main

REPO = Path(__file__).parent.parent
DOCKER_SOCK = "/var/run/docker.sock"
REAL_DOCKER = shutil.which("docker")

RALPHD_RUN_ID = os.environ.get("RALPHD_RUN_ID", "local-dev")
LABELS = ("--label", f"ralphd.run={RALPHD_RUN_ID}",
          "--label", "ralphd.role=sibling")

# Deliberately the barest base that still has a package manager: the recipe has
# to install python, node, curl, the docker client and user 1000 itself.
MINIMAL_BASE = "debian:bookworm-slim"
BASE_MARKER = "/etc/ralphd-real-build-base"
BASE_MARKER_TEXT = "task-039-minimal-base"

BUILD_TIMEOUT = 1800  # a cold build pulls a base, node, pi and the engine's deps
RUN_TIMEOUT = 120
# A cache hit is one `docker image inspect`. Generous enough not to be flaky on
# a loaded daemon, small enough that a silent rebuild (~90s) cannot pass.
CACHE_HIT_BUDGET = 30.0


def _docker_available() -> bool:
    if not REAL_DOCKER or not Path(DOCKER_SOCK).is_socket():
        return False
    return subprocess.run([REAL_DOCKER, "version"],
                          capture_output=True, timeout=30).returncode == 0


pytestmark = [pytest.mark.docker,
              pytest.mark.skipif(not _docker_available(),
                                 reason="docker socket not available")]


def docker(*argv: str, timeout: int = RUN_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run([REAL_DOCKER, *argv], capture_output=True, text=True,
                          timeout=timeout)


def in_image(tag: str, script: str) -> subprocess.CompletedProcess:
    """Run a shell snippet inside `tag` as a labeled, throwaway sibling."""
    return docker("run", "--rm", *LABELS, "--entrypoint", "sh", tag, "-c", script)


def pinned(name: str) -> str:
    """A pin read out of this repo's `container/Dockerfile` -- never restated
    here, so a version bump there cannot leave a stale copy in a test."""
    return image.arg_defaults((REPO / "container/Dockerfile").read_text())[name]


def pinned_node_major() -> int:
    text = (REPO / "container/Dockerfile").read_text()
    m = image.NODE_MAJOR_RE.search(text)
    assert m, "container/Dockerfile no longer declares a nodesource major"
    return int(m.group(1))


@pytest.fixture(scope="module")
def minimal_base(tmp_path_factory):
    """A real, freshly built minimal base image under a unique tag.

    Unique because the derived hash covers the base *reference*: a stable tag
    would let a second run of this tier find the derived tag already present and
    quietly assert nothing about building. The tag being fresh guarantees the
    build under test actually happens.
    """
    ctx = tmp_path_factory.mktemp("real-build-base")
    (ctx / "Dockerfile").write_text(
        f"FROM {MINIMAL_BASE}\n"
        f"RUN echo {BASE_MARKER_TEXT} > {BASE_MARKER}\n")
    tag = f"ralphd-test-base:real-build-{uuid.uuid4().hex[:8]}"
    res = subprocess.run(
        [REAL_DOCKER, "build", "-t", tag, *LABELS, str(ctx)],
        capture_output=True, text=True, timeout=BUILD_TIMEOUT,
        env={**os.environ, "DOCKER_BUILDKIT": "0"})
    assert res.returncode == 0, (res.stdout[-4000:] + res.stderr[-4000:])
    yield tag
    docker("rmi", "-f", tag)


@pytest.fixture(scope="module")
def derived(minimal_base):
    """The production resolve, for real: build the job image from that base.

    `main.DOCKER` is pinned to the real client for the duration -- the docker
    tier may be run with `RALPHD_DOCKER` pointing at the recording stub, and a
    stub cannot build anything.
    """
    saved = main.DOCKER
    main.DOCKER = REAL_DOCKER
    built = None
    try:
        resolved = main.resolve_job_image(None, base=minimal_base, root=REPO)
        built = resolved["image"]
        yield resolved
    finally:
        main.DOCKER = saved
        if built:
            docker("rmi", "-f", built)


# --- the build really happens, and the result really runs the engine -------

def test_the_production_resolve_builds_a_derived_tag_from_the_minimal_base(
        derived, minimal_base):
    assert derived["imageSource"] == main.IMAGE_SOURCE_BUILT
    assert derived["imageBase"] == minimal_base
    assert derived["image"] == image.derived_tag(derived["imageHash"])
    # The tag exists on the daemon now -- the hash namespace is not a promise
    # about a name, it is a promise about an image.
    assert docker("image", "inspect", derived["image"]).returncode == 0
    # ...and the base was never tagged as the job image (H2: it has no engine).
    assert derived["image"] != minimal_base


def test_the_derived_image_runs_ralphd_engine(derived):
    """Directly (`ralphd-engine --version`, which imports the whole engine) and
    through the shipped entrypoint, which is the only path a job ever takes."""
    tag = derived["image"]
    ver = docker("run", "--rm", *LABELS, "--entrypoint", "ralphd-engine", tag,
                 "--version")
    assert ver.returncode == 0, ver.stderr
    assert ver.stdout.strip().startswith("ralphd-engine "), ver.stdout

    # ENTRYPOINT reaches the engine: with no run dir and no PRD the engine
    # exits 2 with its own message -- proof the process started, not the shell.
    ep = docker("run", "--rm", *LABELS, tag)
    assert ep.returncode == 2, (ep.returncode, ep.stdout[-2000:], ep.stderr[-2000:])
    assert "no PRD at" in (ep.stdout + ep.stderr)

    # The run contract of `container/Dockerfile`, reproduced on a base that had
    # no such user: uid 1000 in /workspace with the engine's venv on PATH.
    who = in_image(tag, "id -u; pwd; command -v ralphd-engine")
    assert who.returncode == 0, who.stderr
    uid, cwd, engine_path = who.stdout.split()
    assert (uid, cwd) == ("1000", "/workspace")
    assert engine_path == "/opt/ralphd-venv/bin/ralphd-engine"


def test_the_derived_image_layers_onto_the_base_and_carries_the_pins(derived):
    tag = derived["image"]
    marker = in_image(tag, f"cat {BASE_MARKER}")
    assert marker.returncode == 0, marker.stderr
    assert marker.stdout.strip() == BASE_MARKER_TEXT  # the base is still under it

    # `command -v` takes ONE name (extra arguments are ignored), so the tools
    # the engine shells out to are checked one at a time or not at all.
    got = in_image(tag, "pi --version; node -p 'process.versions.node.split(\".\")[0]'; "
                        "for b in docker git jq rg ps python3; do "
                        "  command -v \"$b\" >/dev/null || { echo \"missing $b\"; exit 1; }; "
                        "done; echo tools-ok")
    assert got.returncode == 0, got.stderr
    pi_version, node_major, tools = got.stdout.split()
    assert pi_version == pinned(image.PI_PIN)
    assert int(node_major) >= pinned_node_major()
    assert tools == "tools-ok"


def test_a_second_resolve_of_the_same_base_is_a_pure_cache_hit(derived, minimal_base):
    """Same rule as the default image's, asserted against a real daemon: the
    tag exists, so nothing is built and the same reference comes back."""
    before = docker("image", "inspect", "--format", "{{.Id}}", derived["image"])
    assert before.returncode == 0
    saved = main.DOCKER
    main.DOCKER = REAL_DOCKER
    try:
        t0 = time.monotonic()
        again = main.resolve_job_image(None, base=minimal_base, root=REPO)
        elapsed = time.monotonic() - t0
    finally:
        main.DOCKER = saved
    assert again["image"] == derived["image"]
    assert again["imageSource"] == main.IMAGE_SOURCE_CACHED
    assert elapsed < CACHE_HIT_BUDGET, f"cache hit took {elapsed:.1f}s -- did it rebuild?"
    after = docker("image", "inspect", "--format", "{{.Id}}", derived["image"])
    assert after.stdout.strip() == before.stdout.strip()  # same image, not a rebuild


def test_the_build_does_not_ask_for_buildkit(derived):
    """The production builder choice, asserted where the real build happened:
    a job container has the static docker client and no buildx plugin, so the
    legacy builder is the one that can work there."""
    assert main._image_build_env()["DOCKER_BUILDKIT"] == "0"
