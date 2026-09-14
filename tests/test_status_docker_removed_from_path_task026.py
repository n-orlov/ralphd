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

from test_cli_features import _recovered_run
from test_tier_cost_breakdown import _two_tier_run
from tests.conftest import call_ralphctl_in_process


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


def test_docker_binary_is_genuinely_absent_from_the_constructed_path():
    """Precondition proof, isolated from the CLI: the helper this module
    relies on for every other test here really does remove docker, using
    this host's own ambient PATH (which does carry a real docker client --
    that is exactly why this must be proven by construction, not assumed)."""
    assert shutil.which("docker") is not None, (
        "this dev container is expected to carry a real docker client -- "
        "if it does not, the removal below is not exercising anything"
    )
    assert shutil.which("docker", path=_path_with_docker_removed()) is None


def test_status_exits_0_on_a_recovered_run_with_docker_removed_from_path(
        tmp_path, capsys):
    """Task 080/085's exact second named node
    (`test_status_prints_no_reason_line_for_a_run_that_is_still_going`'s own
    fixture), re-driven with `docker` removed from `PATH` itself rather than
    `RALPHD_DOCKER` pointed at a bogus name. `ralphctl status` must still
    exit 0, render the on-disk fallback, and show container liveness as
    unknown/absent rather than raising."""
    registry, _ = _recovered_run(tmp_path)
    env = _env_without_docker({**os.environ, "RALPHD_REGISTRY": str(registry)})
    res = call_ralphctl_in_process(["status", "recovered"], env=env,
                                   capsys=capsys)
    assert res.returncode == 0, res.stderr
    assert "reason:" not in res.stdout
    assert "Traceback" not in res.stderr
    assert "FileNotFoundError" not in res.stderr


def test_status_json_exits_0_on_the_tier_breakdown_run_with_docker_removed(
        tmp_path, capsys):
    """Task 080/085's exact first named node
    (`test_ralphctl_status_json_exposes_the_tier_bucket`'s own fixture),
    re-driven the same way. The tier breakdown itself has nothing to do
    with container liveness, so it must be unaffected; `containerLiveness`
    is asserted to be the CLI's documented "no container found" reading
    (`CONTAINER_ABSENT`, i.e. `"absent"`) -- the shape a missing docker
    binary degrades to, same as a present-but-non-matching docker answers,
    since the two are indistinguishable from the caller's own liveness
    question."""
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
    assert doc["containerLiveness"] == "absent"
