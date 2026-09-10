"""Task 080 (steer 011 defect 3): does `ralphctl status` raise when no
`docker` binary is on `PATH`, or does it already degrade?

The two nodes steer 011 named --
`tests/test_tier_cost_breakdown.py::test_ralphctl_status_json_exposes_the_tier_bucket`
and
`tests/test_cli_features.py::test_status_prints_no_reason_line_for_a_run_that_is_still_going`
-- both build a run dir whose `status.json` records a NON-terminal state
(`_two_tier_run`'s freshly-stepped run and `_recovered_run`'s "running" run
respectively) with no live engine behind it, so `cmd_status` falls onto the
on-disk path and calls `_dangling_run_entry()`, which calls
`_container_liveness()`, which is `sh([DOCKER, "inspect", ...])` --
`src/ralphd/cli/main.py`'s `sh()` (line 372-373) is a bare
`subprocess.run(...)`, and `DOCKER` (line 122) is
`os.environ.get("RALPHD_DOCKER", "docker")`. When that binary is not on
`PATH`, `subprocess.run` raises `FileNotFoundError`, and before this task's
fix that propagated straight out of `_container_liveness`
(`src/ralphd/cli/main.py:2437`), out of `_dangling_run_entry`
(`src/ralphd/cli/main.py:6746`), out of `cmd_status`
(`src/ralphd/cli/main.py:3303`) and out of `ralphctl status` itself --
answer: (i), the CLI raised.

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
from pathlib import Path

from test_cli_features import _recovered_run
from test_tier_cost_breakdown import _two_tier_run
from tests.conftest import call_ralphctl_in_process

from ralphd.cli.main import CONTAINER_ABSENT, _container_liveness

# A name guaranteed absent from every PATH entry -- never a real binary.
NO_SUCH_DOCKER = "ralphd-task080-no-such-docker-binary-anywhere-on-path"


def test_container_liveness_degrades_instead_of_raising_when_docker_is_absent(
        monkeypatch):
    """The unit-level reading: `_container_liveness` used to let
    `FileNotFoundError` out unchanged; it now reports the same
    `CONTAINER_ABSENT` a failed (but present) docker binary already
    produces -- no new liveness state, no raise."""
    monkeypatch.setattr("ralphd.cli.main.DOCKER", NO_SUCH_DOCKER)
    assert _container_liveness("ralphd-whatever") == CONTAINER_ABSENT


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
