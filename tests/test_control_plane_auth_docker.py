"""Docker tier: task 004 (#62 candidate 1, requirement A) proved against a
*real* container, not only the ASGI-level `tests/test_control_plane_auth.py`.

Five things only a real container can show:

1. `ralphctl start` provisions `.control-token` unconditionally (no
   `--api-token` passed below) and every mutating command this file drives
   -- `pause`, `budget`, `unpause`, `abort` -- still works exactly as an
   operator's `ralphctl` always could, over the surface task 004 moved.
2. A raw HTTP client that presents no `X-Ralphd-Control-Token` header --
   exactly what a spawned iteration's own client would be, since
   `RALPHD_CONTROL_TOKEN` is stripped from its environment (task 003,
   extended) -- is refused on the real, running container's mutating
   routes, while `GET /status` still succeeds for it.
3. Task 003 (requirement A, #201): the token is read back, host-side, from
   `<registry>/control/<run-id>/.control-token` -- NOT from anywhere under
   the bind-mounted `<registry>/runs/<run-id>/` (`/run/ralphd` in the
   container) -- because that is now the only place `start`/`resume` ever
   write it.
4. Task 003 (requirement A.6, #201), the regression this module exists to
   pin: a process running at the agent's own uid *inside* the job's own
   container -- exactly the identity a dropped iteration runs as -- can
   find the control credential in none of its plausible leak routes:
   (a) the REAL running iteration process's own environ, read directly
   from `/proc/<pid>/environ` by pid (not via `docker exec`'s own
   synthesised environment, which always carries the container's
   *launch-time* `-e` values regardless of `--user` and would otherwise be
   a false positive with nothing to do with the boundary), (b) the run
   dir's own bind-mounted, agent-writable path (`/run/ralphd`, host uid
   1000 == container uid 1000), (c) the read-only config mount (`/config`),
   and (d) the in-container, root-owned defense-in-depth copy
   (`engine.privsep.provision_control_credentials`) -- neither readable
   NOR replaceable at the dropped agent uid, the SAME kernel permission
   check `tests/test_engine_uid_docker.py` proves for `tasks.json`. The
   SAME (d) check runs against `.api-token`'s own directory entry too,
   whenever it exists -- success criteria's "so does its attempt to
   replace the optional .api-token's directory entry" is not a separate
   mechanism from `.control-token`'s, so it is not a separate check either.
   Proved on BOTH the layout a fresh `start` builds and the layout a
   `resume` reproduces (a second, independent container, same run id).
5. With `--api-token` enabled, the SAME dropped-agent-uid probe (4) above
   applied to `.api-token` finds it in none of the same leak routes
   either -- a separate, minimal test (enabling `--api-token` makes
   `cfg.api_token` gate every route unconditionally, not just the
   mutating half `.control-token` gates, which would otherwise dilute the
   "unconditional" claim in (1) and the tokenless-401 claim in (2)).

Fixtures, cleanup discipline and the two environment-specific wrinkles
(bind-mount path translation, API reachability from inside this job's own
container) are exactly `tests/test_docker_sibling_e2e.py`'s; this module
imports them by name instead of re-deriving a second copy.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request

import pytest

from test_docker_sibling_e2e import (  # noqa: F401  (fixtures used by name)
    REAL_DOCKER,
    Ctl,
    _unique_run_id,
    _wait_status,
    ctl,
    gateway_ip,
    pytestmark,
    test_image,
    work_root,
)


def _read_control_token(ctl_obj: Ctl, run_id: str) -> str:
    # Task 003 (requirement A, #201): the ONLY place `start`/`resume` ever
    # write the control token now -- host-only, never bind-mounted into any
    # container, a sibling of `runs/`/`configs/` in the registry.
    ccfile = ctl_obj.registry / "control" / run_id / ".control-token"
    assert ccfile.is_file(), (
        "ralphctl start did not provision a control token unconditionally "
        f"at its new host-only location ({ccfile})")
    token = ccfile.read_text().strip()
    assert token
    return token


def _assert_dropped_child_cannot_reach_control_credential(
        container: str, control_token: str, api_token: str | None = None) -> None:
    """Requirement A.6: probe the running `container` exactly as a fully
    dropped agent-uid process inside it would see the world -- `docker exec
    --user 1000:1000` needs no docker-socket access from inside the
    container itself, it only reproduces the identity a spawned iteration
    already runs as (`agent_child_kwargs()`, `engine/privsep.py`).

    `api_token`, when given (`--api-token` was enabled at `start`), gets
    every check `control_token` gets: success criteria's "With --api-token
    enabled that same dropped child cannot read .api-token either, so the
    extra gate reopens no path" is not a separate claim about a separate
    mechanism, it is the SAME claim about a second value the SAME code path
    now protects."""
    tokens = {".control-token": control_token}
    if api_token is not None:
        tokens[".api-token"] = api_token

    def probe(*cmd: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [REAL_DOCKER, "exec", "--user", "1000:1000", container, *cmd],
            capture_output=True, text=True, timeout=15)

    # (a) no readable process environ mentions either token value. Checked
    # against the REAL running iteration process specifically (`pgrep -f
    # /usr/bin/pi`, the stub's own path in this image) rather than a blanket
    # `/proc/[0-9]*/environ` scan: `docker exec` synthesises its OWN
    # process's environment from the container's *launch-time* `-e` values
    # regardless of `--user` (a Docker property, not a fork/exec from this
    # container's own process tree), so a blanket scan would always "find"
    # the token in the exec session's own environ -- a false positive with
    # nothing to do with the boundary. The iteration process, in contrast,
    # was genuinely forked/exec'd by the engine through
    # `PiRunner.run`/`agent_child_kwargs()`, so its environ is the real
    # thing (confirmed live: pid 13 in a manual repro had
    # `RALPHD_RUN_DIR`/`RALPHD_WORKSPACE_DIR`/etc. but none of the four
    # `CONTROL_PLANE_ENV_VARS`). The pattern is `[/]usr/bin/pi`, not
    # `/usr/bin/pi`, on purpose -- the plain string is a classic `pgrep -f`
    # self-match: it appears verbatim in the invoking `sh -c "...pgrep -f
    # '/usr/bin/pi'..."` script's OWN argv, so `pgrep` finds itself (and
    # inherits the exec session's launch-time env, the very false positive
    # this whole probe exists to avoid); the bracket expression matches the
    # real process's `/usr/bin/pi` identically while no longer appearing as
    # that literal substring in this script's own command line.
    script_a = (
        "for p in $(pgrep -f '[/]usr/bin/pi'); do "
        "tr '\\0' '\\n' < /proc/$p/environ 2>/dev/null; done"
    )
    res = probe("sh", "-c", script_a)
    assert res.returncode == 0, res.stderr
    for name, value in tokens.items():
        assert value not in res.stdout, (
            f"an iteration process's own environ names the {name} value: "
            + repr(res.stdout))

    # (b) no file under the bind-mounted run dir (host-writable by the same
    # uid, /run/ralphd in the container) is even named like a token.
    script_b = "find /run/ralphd -iname '*token*' 2>/dev/null; true"
    res = probe("sh", "-c", script_b)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "", (
        "a token-named file exists under the bind-mounted run dir: "
        + repr(res.stdout))

    # (c) the read-only config mount contains neither token value.
    script_c = "cat /config/job.yaml /config/*.json /config/**/*.json 2>/dev/null; " \
               "find /config -type f -exec cat {} + 2>/dev/null"
    res = probe("sh", "-c", script_c)
    assert res.returncode == 0, res.stderr
    for name, value in tokens.items():
        assert value not in res.stdout, (
            f"the {name} value is readable somewhere under /config: "
            + repr(res.stdout))

    # (d) the in-container, root-owned defense-in-depth copy
    # (`engine.privsep.provision_control_credentials`,
    # `/var/lib/ralphd/control`, never bind-mounted) is neither readable NOR
    # replaceable at the dropped agent uid -- the read half AND the
    # directory-entry replacement half of the boundary, both against a real
    # kernel permission check, exactly `test_engine_uid_docker.py`'s
    # `tasks.json` proof for the SAME uid -- for `.control-token` AND (when
    # enabled) the optional `.api-token` beside it.
    for name in tokens:
        path = f"/var/lib/ralphd/control/{name}"
        res = probe("sh", "-c", f"cat {path}; echo RC=$?")
        assert res.returncode == 0, res.stderr
        combined_read = res.stdout + res.stderr
        assert "RC=0" not in combined_read, (
            f"the dropped agent uid could read the in-container {name} "
            f"copy: {combined_read!r}")
        assert "Permission denied" in combined_read, combined_read

        res = probe("sh", "-c", f"echo pwned > {path}; echo RC=$?")
        assert res.returncode == 0, res.stderr
        combined_write = res.stdout + res.stderr
        assert "RC=0" not in combined_write, (
            f"the dropped agent uid could REPLACE the in-container {name} "
            f"copy: {combined_write!r}")
        assert "Permission denied" in combined_write, combined_write


def _assert_root_owned_credential_copy_matches_and_survived(
        container: str, control_token: str, api_token: str | None = None) -> None:
    """Root-side sanity for the (d) probe above: the in-container copy this
    task provisions is not a stray, unrelated artifact (it holds the SAME
    token(s) `ralphctl` uses) and the dropped agent uid's failed write
    attempt(s) truly changed nothing on disk -- checked as root (uid 0, the
    container's default identity per `container/Dockerfile`'s deliberate
    absence of a trailing `USER agent`, task 020/requirement I), the only
    identity able to read it at all."""
    tokens = {".control-token": control_token}
    if api_token is not None:
        tokens[".api-token"] = api_token
    for name, expected in tokens.items():
        res = subprocess.run(
            [REAL_DOCKER, "exec", container, "cat",
             f"/var/lib/ralphd/control/{name}"],
            capture_output=True, text=True, timeout=15)
        assert res.returncode == 0, res.stderr
        assert res.stdout == expected, (
            f"the in-container {name} copy does not match the real "
            f"token (got {res.stdout!r}, want {expected!r}) -- either "
            "provisioning is wrong or the dropped-uid write attempt "
            "silently succeeded")


def test_mutating_ralphctl_commands_still_work_and_the_raw_control_gate_holds(ctl, work_root):
    run_id = _unique_run_id("e004-ctlauth")
    # task 009 (B.3): STUB_HOLD_FILE (a container-side path under
    # /workspace, mounted from work_root/ws below) blocks the job
    # indefinitely -- deterministically wide enough to pause/budget/unpause
    # a running job -- instead of racing a fixed STUB_SLEEP window against
    # how fast the container reaches a terminal state on its own. Released
    # (touched) right before the final wait below, same pattern as
    # test_docker_sibling_e2e.py's test_resume_continues_after_container_stop.
    hold_rel = ".hold-ctlauth"
    hold_file_host = work_root / "ws" / hold_rel
    res = ctl.start(run_id, extra_env={"STUB_TASKS": "1",
                                       "STUB_HOLD_FILE": f"/workspace/{hold_rel}",
                                       "STUB_HOLD_TIMEOUT_S": "60"},
                    extra=("--on-complete", "idle"))
    assert res.returncode == 0, res.stderr
    meta = json.loads(res.stdout)
    api_url = meta["apiUrl"]
    container = f"ralphd-{run_id}"

    control_token = _read_control_token(ctl, run_id)

    _wait_status(api_url, lambda s: True, timeout=30)

    # -- A.6: the credential is unreachable to a dropped agent child, on
    # the layout a fresh `start` actually built ----------------------------
    _assert_dropped_child_cannot_reach_control_credential(container, control_token)
    _assert_root_owned_credential_copy_matches_and_survived(container, control_token)

    # -- 1. the raw boundary, against the real running container ----------
    # A client presenting no X-Ralphd-Control-Token header at all -- exactly
    # what a spawned iteration's own HTTP client would be able to build
    # (task 003 strips RALPHD_CONTROL_TOKEN from its environment) -- is
    # refused on a mutating route.
    req = urllib.request.Request(api_url + "/pause", method="POST", data=b"")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 401, exc.value.read()

    # The observation half is untouched by the same tokenless client.
    req = urllib.request.Request(api_url + "/status")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200

    # The correct header passes the same route.
    req = urllib.request.Request(api_url + "/pause", method="POST", data=b"")
    req.add_header("X-Ralphd-Control-Token", control_token)
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200

    # -- 2. the operator's own front door: ralphctl still does everything --
    # These four commands are the A.4 evidence: `ralphctl` (host-side,
    # reading the host-only control-creds dir this task moved the token
    # into) keeps authenticating to every mutating route exactly as before.
    res = ctl.run("budget", run_id, "+3", timeout=30)
    assert res.returncode == 0, res.stderr

    res = ctl.run("unpause", run_id, timeout=30)
    assert res.returncode == 0, res.stderr

    hold_file_host.touch()  # release: let the held phase proceed and finish

    final = _wait_status(api_url, lambda s: s.get("state") in
                         ("succeeded", "failed", "aborted"), timeout=60)
    assert final["verdict"] == "verified", final


def test_dropped_agent_child_cannot_read_the_optional_api_token_either(ctl, work_root):
    """Success criteria's own second clause: "With --api-token enabled that
    same dropped child cannot read .api-token either, so the extra gate
    reopens no path." A separate, minimal test rather than folding
    `--api-token` into the test above: enabling it makes `cfg.api_token`
    gate EVERY route (`engine/api.py`'s `auth` middleware, unconditionally,
    not just the mutating half `control_token` gates), so the tokenless-
    401/authenticated-200 raw-HTTP probe above would need an `Authorization`
    header added to distinguish the two independent gates -- kept separate
    here so neither test's own claim ("unconditional" above, "the extra
    gate reopens no path" here) is diluted by the other's setup."""
    run_id = _unique_run_id("e004-apitoken")
    hold_rel = ".hold-apitoken"
    hold_file_host = work_root / "ws" / hold_rel
    res = ctl.start(run_id, extra_env={"STUB_TASKS": "1",
                                       "STUB_HOLD_FILE": f"/workspace/{hold_rel}",
                                       "STUB_HOLD_TIMEOUT_S": "60"},
                    extra=("--on-complete", "idle", "--api-token", "auto"))
    assert res.returncode == 0, res.stderr
    meta = json.loads(res.stdout)
    container = f"ralphd-{run_id}"

    control_token = _read_control_token(ctl, run_id)
    api_token_file = ctl.registry / "control" / run_id / ".api-token"
    assert api_token_file.is_file(), (
        "--api-token auto did not provision .api-token at its new "
        f"host-only location ({api_token_file})")
    api_token = api_token_file.read_text().strip()
    assert api_token
    assert api_token != control_token

    _wait_status(meta["apiUrl"], lambda s: True, timeout=30,
                 headers={"Authorization": f"Bearer {api_token}"})

    _assert_dropped_child_cannot_reach_control_credential(
        container, control_token, api_token=api_token)
    _assert_root_owned_credential_copy_matches_and_survived(
        container, control_token, api_token=api_token)

    # The operator's own front door still works with both gates enabled:
    # `ralphctl` reads BOTH tokens back from the same host-only directory
    # and presents both headers automatically.
    res = ctl.run("budget", run_id, "+3", timeout=30)
    assert res.returncode == 0, res.stderr
    res = ctl.run("unpause", run_id, timeout=30)
    assert res.returncode == 0, res.stderr

    hold_file_host.touch()

    final = _wait_status(meta["apiUrl"], lambda s: s.get("state") in
                         ("succeeded", "failed", "aborted"), timeout=60,
                         headers={"Authorization": f"Bearer {api_token}"})
    assert final["verdict"] == "verified", final


def test_dropped_agent_child_cannot_reach_the_control_credential_after_resume(ctl):
    """Requirement A.6, resumed layout: the SAME three-route probe as the
    fresh-start test above, against a second, independent container that
    `ralphctl resume` brings up for the same run id. Also confirms `resume`
    reproduces `RALPHD_CONTROL_TOKEN` into the new container's own engine
    process without ever re-exposing it anywhere the run dir's bind mount
    (or the config mount) makes visible to an agent-uid process."""
    run_id = _unique_run_id("e004-ctlauth-resume")
    hold_rel = ".hold-ctlauth-resume"
    res = ctl.start(run_id, extra_env={"STUB_TASKS": "2",
                                       "STUB_HOLD_FILE": f"/workspace/{hold_rel}",
                                       "STUB_HOLD_TIMEOUT_S": "60"},
                    extra=("--on-complete", "idle"))
    assert res.returncode == 0, res.stderr
    meta = json.loads(res.stdout)
    container = f"ralphd-{run_id}"

    _wait_status(meta["apiUrl"], lambda s: s.get("state") == "running", timeout=30)

    res = subprocess.run([REAL_DOCKER, "stop", "-t", "5", container],
                        capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr

    # Deliberately NOT releasing the hold before resume this time (unlike
    # test_docker_sibling_e2e.py's own resume test): `_read_extra_env_wiring`
    # reproduces the SAME STUB_HOLD_FILE/STUB_HOLD_TIMEOUT_S pair into the
    # resumed container, so it re-blocks -- which is exactly the wide,
    # deterministic "still running" window the pause/abort probe below
    # needs, with no race against how fast the stub finishes on its own.
    res = ctl.resume(run_id, extra=("--iterations", "+10"))
    assert res.returncode == 0, res.stderr
    meta2 = json.loads(res.stdout)
    assert meta2["container"] != meta["container"]  # a genuinely fresh container

    control_token = _read_control_token(ctl, run_id)
    _wait_status(meta2["apiUrl"], lambda s: s.get("state") == "running", timeout=30)

    _assert_dropped_child_cannot_reach_control_credential(container, control_token)
    _assert_root_owned_credential_copy_matches_and_survived(container, control_token)

    # The resumed container's own mutating gate still authenticates too:
    # `pause` via the raw header, then `ralphctl abort` (the fourth mutating
    # command named in this test's own commit message) while genuinely
    # paused -- proving `abort` itself still authenticates, distinctly from
    # `pause`/`unpause`/`budget` already proven by the fresh-start test
    # above.
    req = urllib.request.Request(meta2["apiUrl"] + "/pause", method="POST", data=b"")
    req.add_header("X-Ralphd-Control-Token", control_token)
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200

    res = ctl.run("abort", run_id, timeout=30)
    assert res.returncode == 0, res.stderr

    final = _wait_status(meta2["apiUrl"], lambda s: s.get("state") == "aborted",
                         timeout=30)
    assert final["state"] == "aborted", final
