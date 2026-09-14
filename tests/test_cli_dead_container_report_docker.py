"""Task 025 (requirement E.3, #183), docker tier: what `docker logs` actually
answers for a container whose process died with a message on **stderr**.

`tests/test_cli_start_dead_container_report.py` drives the whole
`ralphctl start --no-detach` path against the recording stub `docker`, which
is where the operator-visible wording is proved. What a stub cannot prove is
the stream split: real `docker logs` replays the container's own stdout and
stderr onto its OWN stdout and stderr, so an engine that dies with a fatal
line on stderr -- the commonest shape of a die-before-bind -- leaves
`docker logs`' stdout EMPTY. Task 025's first attempt read only that stdout
and therefore reported the exit code with no log tail at all for exactly that
case; this module pins the real behaviour so the same reading cannot come
back.

Marked ``@pytest.mark.docker``; the whole module skips cleanly when the
docker socket is unreachable or no small image is already present locally
(this tier must never depend on a registry pull). The container it creates is
labeled `ralphd.role=sibling` and removed by its own exact name, never by a
label query -- a label query run from inside a job container can match the
job container itself.
"""

from __future__ import annotations

import io
import contextlib
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from ralphd.cli import main as cli

DOCKER_SOCK = "/var/run/docker.sock"
REAL_DOCKER = shutil.which("docker")
CANDIDATE_IMAGES = ["busybox:stable", "busybox:latest", "busybox:1.36",
                    "alpine:latest", "alpine:3", "alpine:3.20",
                    "python:3.12-slim-bookworm"]
EXIT_CODE = 17
STDERR_LINE = "engine-fatal-on-stderr"
STDOUT_LINE = "engine-boot-line"


def _docker_available() -> bool:
    if not REAL_DOCKER or not Path(DOCKER_SOCK).is_socket():
        return False
    return subprocess.run([REAL_DOCKER, "version"],
                          capture_output=True, timeout=10).returncode == 0


def _local_image() -> str | None:
    for image in CANDIDATE_IMAGES:
        if subprocess.run([REAL_DOCKER, "image", "inspect", image],
                          capture_output=True, timeout=30).returncode == 0:
            return image
    return None


pytestmark = [pytest.mark.docker,
              pytest.mark.skipif(not _docker_available(),
                                 reason="docker socket not available")]


@pytest.fixture
def dead_container():
    """A real container that printed to both streams and exited non-zero.

    Not `--rm`: the whole point is a container still sitting there dead, the
    state `docker inspect`/`docker logs` can still be asked about.
    """
    image = _local_image()
    if not image:
        pytest.skip("no small image available locally (refusing to pull)")
    name = f"t025-deadbind-{uuid.uuid4().hex[:8]}"
    run_id = os.environ.get("RALPHD_RUN_ID", "unknown")
    res = subprocess.run(
        [REAL_DOCKER, "run", "--name", name, "--user", "1000:1000",
         "--label", f"ralphd.run={run_id}",
         "--label", "ralphd.role=sibling", image,
         "sh", "-c", f"echo {STDOUT_LINE}; echo {STDERR_LINE} >&2; "
                     f"exit {EXIT_CODE}"],
        capture_output=True, text=True, timeout=180)
    assert res.returncode == EXIT_CODE, (res.returncode, res.stderr)
    try:
        yield name
    finally:
        subprocess.run([REAL_DOCKER, "rm", "-f", name],
                       capture_output=True, timeout=120)


def test_docker_logs_puts_the_dying_line_on_its_own_stderr(dead_container):
    """The premise the production reader has to cope with."""
    logs = subprocess.run(
        [REAL_DOCKER, "logs", "--tail", "20", dead_container],
        capture_output=True, text=True, timeout=120)
    assert logs.returncode == 0, logs.stderr
    assert STDOUT_LINE in logs.stdout
    assert STDERR_LINE not in logs.stdout, (
        "premise changed: the container's stderr now arrives on docker logs' "
        "stdout")
    assert STDERR_LINE in logs.stderr


def test_report_names_the_exit_code_and_both_log_streams(dead_container,
                                                         tmp_path):
    """The report a real dying container produces, exit code plus tail.

    `tmp_path` holds no `status.json`, which is the run-dir shape of an
    engine that never bound its control API.
    """
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        cli._report_dead_container_before_bind(dead_container, tmp_path)
    reported = buf.getvalue()
    assert f"exited with code {EXIT_CODE}" in reported, reported
    assert STDOUT_LINE in reported, reported
    assert STDERR_LINE in reported, reported


def test_a_run_that_bound_and_recorded_a_status_stays_silent(dead_container,
                                                            tmp_path):
    """Same dead container, but the run did write status.json: no report.

    A job that bound, ran and tore its own container down on completion must
    not have this diagnosis printed over its ordinary ending.
    """
    (tmp_path / "status.json").write_text('{"verdict": "verified"}')
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        cli._report_dead_container_before_bind(dead_container, tmp_path)
    assert buf.getvalue() == "", buf.getvalue()
