"""Task 026 (E.4, E.5): `tests/test_status_docker_absent_task080.py` (an
earlier wave) already proved `sh()`/`_container_liveness()` degrade rather
than raise when the DOCKER binary NAME is pointed (via `RALPHD_DOCKER`) at
something that never resolves. That is not quite the same claim as this
task's own wording: "`docker` removed from PATH" -- the ordinary shape a
plain-upstream nightly dev-layout image actually hits, where nothing
overrides `RALPHD_DOCKER` at all and the bare default (`"docker"`) simply
has no directory on `PATH` that provides it. `subprocess.run(["docker",
...])` resolves `cmd[0]` by searching `PATH` itself (no shell, no explicit
`env=` at any `sh()` call site) -- overriding `RALPHD_DOCKER` to a bogus
name and leaving `PATH` untouched never exercises that lookup at all, since
the bogus name was never going to be found regardless of `PATH`'s
contents.

This module drives the real lookup path: it builds a `PATH` from the
CURRENT environment's own directories with every directory that actually
provides a `docker` executable filtered out (verified empty via
`shutil.which("docker", path=...)` on the constructed string before use,
so the test fails loudly rather than silently passing against an ambient
docker this dev container happens to carry), and calls
`ralphctl status`/`--json status` through `call_ralphctl_in_process` with
`RALPHD_DOCKER` UNSET -- so the CLI's own default (`"docker"`) is exactly
what fails to resolve, the same as every other `sh([DOCKER, ...])` call
site downstream of `cmd_status`.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from test_cli_features import _recovered_run
from test_tier_cost_breakdown import _two_tier_run
from tests.conftest import call_ralphctl_in_process

from ralphd.cli.main import (CONTAINER_LIVENESS_UNKNOWN, _container_liveness,
                             _container_running)


def _path_with_docker_removed() -> str:
    """The current process's own `PATH`, minus every directory that
    resolves a `docker` executable -- built from live directory entries
    (never a single hard-coded absolute path), so this keeps working
    whatever image/host actually runs it."""
    kept = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry) / "docker"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            continue
        kept.append(entry)
    stripped = os.pathsep.join(kept)
    assert shutil.which("docker", path=stripped) is None, (
        "the constructed PATH must genuinely lack a docker binary, or this "
        "test proves nothing about the absent-binary case it exists for"
    )
    return stripped


def _env_without_docker(base_env: dict) -> dict:
    env = dict(base_env)
    env.pop("RALPHD_DOCKER", None)  # the CLI's own bare default must be hit
    env["PATH"] = _path_with_docker_removed()
    return env


def test_docker_binary_is_genuinely_absent_from_the_constructed_path(
        monkeypatch):
    """Precondition proof, isolated from the CLI: the helper this module
    relies on for every other test here really does remove docker, using
    this host's own ambient PATH (which does carry a real docker client --
    that is exactly why this must be proven by construction, not assumed).

    Task 026 (E.4), at the unit level the CLI reads through: with that
    constructed PATH in force and `DOCKER` left at the CLI's own bare
    default, the liveness probe answers `unknown` -- it could not decide --
    rather than `absent`, which is docker's own answer about a container it
    looked for and did not find.

    Task 069's nightly re-take found the precondition below asserting
    against an environment that never carries docker in the first place:
    the nightly dev-layout cells (the Jenkinsfile) deliberately install
    no docker client at all (`-m docker` coverage stays in the Docker lane), so
    "this dev container is expected to carry a real docker client" is false
    there by that lane's own design, not by accident. Skipping rather
    than failing keeps the precondition's job -- catching an accidental
    ambient-docker pass on a host that DOES carry one -- without turning a
    docker-less environment's absence of anything to remove into a defect."""
    if shutil.which("docker") is None:
        pytest.skip(
            "no ambient docker client in this environment (expected on a "
            "nightly dev-layout cell, which installs none by design) -- "
            "nothing here for the removal below to exercise"
        )
    stripped = _path_with_docker_removed()
    assert shutil.which("docker", path=stripped) is None
    monkeypatch.setattr("ralphd.cli.main.DOCKER", "docker")
    monkeypatch.setenv("PATH", stripped)
    assert _container_liveness("ralphd-whatever") == CONTAINER_LIVENESS_UNKNOWN
    # the boolean view says "nothing confirmed", never "exists, stopped"
    assert _container_running("ralphd-whatever") is None


def test_status_exits_0_on_a_recovered_run_with_docker_removed_from_path(
        tmp_path, capsys):
    """Task 080/085's exact second named node
    (`test_status_prints_no_reason_line_for_a_run_that_is_still_going`'s own
    fixture), re-driven with `docker` removed from `PATH` itself rather than
    `RALPHD_DOCKER` pointed at a bogus name. `ralphctl status` must still
    exit 0, render the on-disk fallback, and word the container's liveness
    as undecided (task 026, E.4) rather than raising -- and specifically
    must not tell the operator the container is gone, since with no docker
    client here nothing was ever looked at."""
    registry, _ = _recovered_run(tmp_path)
    env = _env_without_docker({**os.environ, "RALPHD_REGISTRY": str(registry)})
    res = call_ralphctl_in_process(["status", "recovered"], env=env,
                                   capsys=capsys)
    assert res.returncode == 0, res.stderr
    assert "reason:" not in res.stdout
    assert "Traceback" not in res.stderr
    assert "FileNotFoundError" not in res.stderr
    assert "has unknown liveness (no docker client here to ask)" in res.stdout
    assert "install a docker client to tell which" in res.stdout
    assert "appears gone" not in res.stdout


def test_status_json_exits_0_on_the_tier_breakdown_run_with_docker_removed(
        tmp_path, capsys):
    """Task 080/085's exact first named node
    (`test_ralphctl_status_json_exposes_the_tier_bucket`'s own fixture),
    re-driven the same way. The tier breakdown itself has nothing to do
    with container liveness, so it must be unaffected; `containerLiveness`
    is asserted to be the undecided reading (`CONTAINER_LIVENESS_UNKNOWN`,
    i.e. `"unknown"`) that task 026 (E.4) requires of a liveness probe with
    no docker client to ask -- distinct from `"absent"`, which is docker's
    own answer about a container it looked for and did not find. The
    narrower `containerGone` field stays False for it (nothing established
    that anything vanished), while the condition itself is still reported
    (`dangling` true: a non-terminal run whose API is unreachable and whose
    engine could not be confirmed alive)."""
    registry = tmp_path / "registry"
    _two_tier_run(registry, run_id="tier-cost")
    env = _env_without_docker({**os.environ, "RALPHD_REGISTRY": str(registry)})
    res = call_ralphctl_in_process(["--json", "status", "tier-cost"], env=env,
                                   capsys=capsys)
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    by_tier = doc["usage"]["byTier"]
    assert set(by_tier) == {"trivial", "hard"}
    assert by_tier["trivial"]["totalTokens"] == 15
    assert by_tier["hard"]["totalTokens"] == 600
    assert doc["containerLiveness"] == CONTAINER_LIVENESS_UNKNOWN
    assert doc["containerLiveness"] == "unknown"
    assert doc["containerGone"] is False
    assert doc["dangling"] is True
