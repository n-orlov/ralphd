"""Task 025 (requirement E.3, #183): when a job container starts but its
engine dies before ever binding the control API, `ralphctl start` reports
the container's own exit code and a tail of its logs alongside the
unreachable-API line, instead of leaving the operator to run `docker
inspect`/`docker logs` by hand to tell "the image is broken" from "still
starting up".

Driven black-box, in-process (task 036, requirement F.1), against the
recording stub `docker` (tests/stub-docker/docker) with `STUB_DOCKER_
CONTAINERS`/`STUB_DOCKER_RUNNING` staging an exited-before-bind container
(no `STUB_DOCKER_LIVE_ENGINE`, so nothing ever answers the mapped port --
the API genuinely never binds) and the three new knobs `STUB_DOCKER_EXIT_
CODE` / `STUB_DOCKER_LOGS` / `STUB_DOCKER_LOGS_STDERR` standing in for the
real `docker inspect --format {{.State.ExitCode}}` / `docker logs --tail N`
answers.

The stderr knob is the point of the second assertion: real `docker logs`
replays the container's own two streams onto its own two streams, so an
engine that dies with a fatal message on stderr -- the commonest shape of a
die-before-bind, proved against a real sibling that wrote
`engine-fatal-on-stderr` and exited 17 -- puts that line on `docker logs`'s
STDERR and nowhere else. A stub that staged logs on stdout only could not
see a reader that read stdout only.
"""

from __future__ import annotations

from pathlib import Path

from test_cli_docker import Ctl, ctl  # noqa: F401  (fixture import)

__all__ = ["ctl"]

FAKE_CONTAINER_ID = "f" * 64


def test_start_reports_exit_code_and_log_tail_when_the_engine_dies_before_binding(
        ctl: Ctl):
    res = ctl.run(
        "start", "--prd", str(ctl.prd), "--no-detach", "--llm", "none",
        "--run-id", "tst-deadbind",
        env={
            "STUB_DOCKER_CONTAINERS": FAKE_CONTAINER_ID,
            "STUB_DOCKER_RUNNING": "",  # exists, but not running: exited
            "STUB_DOCKER_EXIT_CODE": "17",
            "STUB_DOCKER_LOGS": "engine: starting up\n",
            "STUB_DOCKER_LOGS_STDERR":
                "engine: fatal: could not load job.yaml\n",
        },
    )

    # The container never binds its API and never writes status.json, so
    # `start --no-detach` falls through to exit 1 (never "verified").
    assert res.returncode == 1, (res.stdout, res.stderr)
    combined = res.stdout + res.stderr
    assert "API unreachable" in combined, combined  # the line this sits beside
    assert "exited with code 17" in combined, combined
    assert "engine: starting up" in combined, combined
    # The line a dying process actually writes: on docker logs' own stderr.
    assert "engine: fatal: could not load job.yaml" in combined, combined
