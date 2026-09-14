"""Task 080 (steer 011 defect 3): does `ralphctl status` raise when no
`docker` binary is on `PATH`, or does it already degrade? Task 085 (steer
013 section 2, #183) broadened the same question to every OTHER `ralphctl`
command that calls `sh([DOCKER, ...])` -- `cmd_doctor`'s own docker check
chief among them, since it is a second, independent call to `sh()` that
task 080's narrower `_container_liveness()` catch never covered.

The two nodes steer 011 named --
`tests/test_tier_cost_breakdown.py::test_ralphctl_status_json_exposes_the_tier_bucket`
and
`tests/test_cli_features.py::test_status_prints_no_reason_line_for_a_run_that_is_still_going`
-- both build a run dir whose `status.json` records a NON-terminal state
(`_two_tier_run`'s freshly-stepped run and `_recovered_run`'s "running" run
respectively) with no live engine behind it, so `cmd_status` falls onto the
on-disk path and calls `_dangling_run_entry()`, which calls
`_container_liveness()`, which is `sh([DOCKER, "inspect", ...])`. Before
task 080's fix, `sh()` was a bare `subprocess.run(...)` and a missing
`docker` binary raised `FileNotFoundError` straight out of
`_container_liveness`, out of `_dangling_run_entry`, out of `cmd_status` and
out of `ralphctl status` itself -- answer: (i), the CLI raised. Task 080
caught that one call site; task 085 moved the catch into `sh()` itself
(returning a `subprocess.CompletedProcess` with `returncode == 127`, the
shell's own "command not found", instead of raising) so `cmd_doctor`'s own
direct `sh([DOCKER, "version", ...])` -- and `run_container_exists()`, the
dangling-run sweep, sibling-container cleanup and the image/container
inspect helpers below it -- degrade the same way without a second catch
at each site.

This module drives that absence DIRECTLY (`RALPHD_DOCKER` pointed at a name
no `PATH` entry provides), never by relying on the ambient host lacking a
docker client -- this dev container has one at `/usr/local/bin/docker`,
which is exactly why the gap was invisible here and only showed up in a
plain-upstream nightly dev-layout image with no docker client installed
(steer 011). Neither of the two named nodes is touched or marked here --
they keep exercising the ordinary (docker-present) path, unmodified, and are
covered by ownership under this same task instead.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from test_cli_features import _recovered_run
from test_tier_cost_breakdown import _two_tier_run
from tests.conftest import call_ralphctl_in_process

from ralphd.cli.main import (CONTAINER_LIVENESS_UNKNOWN, _container_liveness,
                             container_record_exists, sh)

# A name guaranteed absent from every PATH entry -- never a real binary.
NO_SUCH_DOCKER = "ralphd-task080-no-such-docker-binary-anywhere-on-path"


def test_container_liveness_degrades_instead_of_raising_when_docker_is_absent(
        monkeypatch):
    """The unit-level reading: `_container_liveness` used to let
    `FileNotFoundError` out unchanged; it degrades instead -- no raise.

    Task 026 (E.4) changed WHAT it degrades to, not whether it degrades:
    this used to read `CONTAINER_ABSENT`, the same value a present-but-
    failing docker binary produces, and E.4 requires the two apart --
    "docker looked and found no such container" versus "there is no docker
    client here to look", which is `CONTAINER_LIVENESS_UNKNOWN`."""
    monkeypatch.setattr("ralphd.cli.main.DOCKER", NO_SUCH_DOCKER)
    assert _container_liveness("ralphd-whatever") == CONTAINER_LIVENESS_UNKNOWN


def test_status_survives_a_recovered_run_with_no_docker_binary_reachable(
        tmp_path, capsys):
    """Task 080's exact second named node
    (`test_status_prints_no_reason_line_for_a_run_that_is_still_going`'s own
    fixture), re-driven with the docker binary made unreachable on purpose.
    Before the fix this raised `FileNotFoundError` out of `ralphctl status`;
    now it prints the same status output (no `reason:` line -- the run
    genuinely never ended, unaffected by the container-liveness question)
    and exits 0."""
    registry, _ = _recovered_run(tmp_path)
    env = {**os.environ, "RALPHD_REGISTRY": str(registry),
           "RALPHD_DOCKER": NO_SUCH_DOCKER}
    res = call_ralphctl_in_process(["status", "recovered"], env=env,
                                   capsys=capsys)
    assert res.returncode == 0, res.stderr
    assert "reason:" not in res.stdout
    assert "Traceback" not in res.stderr
    assert "FileNotFoundError" not in res.stderr


def test_status_json_survives_the_tier_breakdown_run_with_no_docker_binary(
        tmp_path, capsys):
    """Task 080's exact first named node
    (`test_ralphctl_status_json_exposes_the_tier_bucket`'s own fixture),
    re-driven with the docker binary made unreachable on purpose. Before the
    fix this raised `FileNotFoundError` out of `ralphctl --json status`; now
    it still emits the same `usage.byTier` breakdown."""
    registry = tmp_path / "registry"
    _two_tier_run(registry, run_id="tier-cost")
    env = {**os.environ, "RALPHD_REGISTRY": str(registry),
           "RALPHD_DOCKER": NO_SUCH_DOCKER}
    res = call_ralphctl_in_process(["--json", "status", "tier-cost"], env=env,
                                   capsys=capsys)
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    by_tier = doc["usage"]["byTier"]
    assert set(by_tier) == {"trivial", "hard"}
    assert by_tier["trivial"]["totalTokens"] == 15
    assert by_tier["hard"]["totalTokens"] == 600


def test_doctor_reports_docker_unreachable_instead_of_raising(
        tmp_path, capsys):
    """Task 085 (steer 013 section 2, #183): task 080's catch lived at the
    ONE `_container_liveness()` call site; `cmd_doctor`'s own docker check
    (`checks["docker"] = sh([DOCKER, "version", ...])`) is a SEPARATE,
    unguarded call to `sh()` that `_container_liveness`'s catch never
    covered and that still raised `FileNotFoundError` before this task moved
    the catch into `sh()` itself. Assert on the REPORT, not the exit code:
    `doctor` legitimately exits non-zero whenever a check is not OK
    (`ok = all(checks.values())`), and the docker check being False is
    exactly that -- correct, not a defect -- so pinning the code would pin
    the wrong thing. What must hold is that it reports rather than
    tracebacks."""
    registry = tmp_path / "registry"
    registry.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "RALPHD_REGISTRY": str(registry),
           "RALPHD_DOCKER": NO_SUCH_DOCKER}
    res = call_ralphctl_in_process(["--json", "doctor"], env=env, capsys=capsys)
    assert "Traceback" not in res.stderr
    assert "FileNotFoundError" not in res.stderr
    doc = json.loads(res.stdout)
    assert doc["checks"]["docker"] is False
    assert doc["ok"] is False


def test_sh_reports_127_and_names_the_command_instead_of_raising():
    """Task 085's fix at the level it actually lives (steer 013 section 2,
    #183): `sh()` itself, driven directly rather than through any one of its
    callers, so the property every caller in `cli/main.py` inherits is pinned
    in ONE place. `127` is deliberate and is the shell's own "command not
    found" -- distinct from a 1-125 in-command failure and from a signal
    death -- and the stderr must NAME the missing binary, since a caller that
    surfaces that text is the only way an operator learns which binary was
    absent. The absence is caused here (a name no `PATH` entry provides,
    asserted), never inherited from the ambient host: this dev container
    carries a real docker client."""
    assert shutil.which(NO_SUCH_DOCKER) is None, (
        "this node must cause the absence itself, not assume it")
    cmd = [NO_SUCH_DOCKER, "inspect", "ralphd-whatever"]
    res = sh(cmd)
    assert isinstance(res, subprocess.CompletedProcess)
    assert res.returncode == 127
    assert res.args == cmd
    assert res.stdout == ""
    assert NO_SUCH_DOCKER in res.stderr


def test_container_record_exists_is_false_when_docker_is_unreachable(
        monkeypatch):
    """`container_record_exists()` -- the shared "does docker still know a
    container by this run's job container name?" helper (task 030, #19),
    which `ralphctl rm` and the hub's delete endpoint both ask through, and
    which steer 013 section 2 refers to by its call shape as
    `run_container_exists()` (`src/ralphd/cli/main.py:5254`) -- is one of the
    bare `sh([DOCKER, ...])` call sites that never went through
    `_container_liveness` and so was untouched by task 080's narrower catch.
    With the binary unreachable it must answer False ("docker does not know
    this container"), exactly as it answers for a present docker that returns
    non-zero, rather than raising `FileNotFoundError` at its caller."""
    monkeypatch.setattr("ralphd.cli.main.DOCKER", NO_SUCH_DOCKER)
    assert container_record_exists("task085-no-such-run") is False
