"""Docker tier: task 004 (#62 candidate 1, requirement A) proved against a
*real* container, not only the ASGI-level `tests/test_control_plane_auth.py`.

Two things only a real container can show:

1. `ralphctl start` provisions `.control-token` unconditionally (no
   `--api-token` passed below) and every mutating command this file drives
   -- `pause`, `budget`, `unpause`, `abort` -- still works exactly as an
   operator's `ralphctl` always could, over the surface task 004 moved.
2. A raw HTTP client that presents no `X-Ralphd-Control-Token` header --
   exactly what a spawned iteration's own client would be, since
   `RALPHD_CONTROL_TOKEN` is stripped from its environment (task 003,
   extended) -- is refused on the real, running container's mutating
   routes, while `GET /status` still succeeds for it.

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

    # The run dir is host-visible (bind-mounted), so `.control-token` is
    # readable right here -- provisioned unconditionally, no `--api-token`
    # was passed to `start` above.
    rdir = ctl.registry / "runs" / run_id
    control_token_file = rdir / ".control-token"
    assert control_token_file.is_file(), (
        "ralphctl start did not provision a control token unconditionally")
    control_token = control_token_file.read_text().strip()
    assert control_token

    _wait_status(api_url, lambda s: True, timeout=30)

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
    res = ctl.run("budget", run_id, "+3", timeout=30)
    assert res.returncode == 0, res.stderr

    res = ctl.run("unpause", run_id, timeout=30)
    assert res.returncode == 0, res.stderr

    hold_file_host.touch()  # release: let the held phase proceed and finish

    final = _wait_status(api_url, lambda s: s.get("state") in
                         ("succeeded", "failed", "aborted"), timeout=60)
    assert final["verdict"] == "verified", final
