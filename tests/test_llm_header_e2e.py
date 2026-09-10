"""Task 048 (#177, requirement J.5's last clause): "at the e2e tier the
stub-`pi` (or a capture endpoint) observes the header on a request."

Every earlier task in this PRD requirement proved the header lands in a
FILE the config dir carries (`<cdir>/pi/models.json`,
`tests/test_cli_llm_profiles.py`) or is displayed, redacted, on four
surfaces (`tests/test_llm_header_visibility.py`) -- none of them prove the
header ever reaches the process that would actually build the outgoing
LLM request from it. This module closes that gap with a real
`ralphd-engine` process (the STUB_DOCKER_LIVE_ENGINE knob,
tests/stub-docker/docker + live_engine_supervisor.py, the same mechanism
tests/test_cli_start_nodetach.py and tests/test_cli_resume_llm_wiring.py
already use) spawning the real `tests/stub-pi/pi` standing in for `pi`
itself.

`live_engine_supervisor.py` now mirrors `container/entrypoint.sh`'s own
"pi provider/model config injected by the CLI" step (`<config-dir>/pi/*`
copied into `$HOME/.pi/agent/`) before the engine ever runs, so the
config dir `ralphctl start --llm-header` wrote reaches the ambient home
exactly the way a real container would place it. `tests/stub-pi/pi` dumps
the provider `headers` maps it finds in that same ambient
`~/.pi/agent/models.json` to `<run>/.stub-llm-headers.json` on every
invocation, unconditionally -- this test reads that marker once the first
iteration has run, for proof the header reached the process that would
build the request, not merely the config file `start` staged.
"""

from __future__ import annotations

from test_cli_llm_profiles import Ctl, ctl  # noqa: F401 (fixture factory reused)
from test_cli_start_nodetach import _live_env, _wait_for_supervisor
from test_llm_api import wait_for_env_marker

__all__ = ["ctl"]


def test_a_run_started_with_llm_header_shows_the_stub_pi_observing_it(ctl):
    ctl.write_profile("acme-e2e", {
        "model": "acme/big", "env": {},
        "pi": {"providers": {"acme": {"baseUrl": "https://a.example.com"}}},
    })
    run_id = "tst-header-e2e"
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "acme-e2e",
                  "--llm-header", "X-Jira-ID=EST-6588",
                  "--run-id", run_id, "--on-complete", "idle",
                  "--iterations", "10", "--max-approaches", "1",
                  env={**_live_env(), "STUB_TASKS": "1", "STUB_SLEEP": "0"})
    assert res.returncode == 0, res.stderr
    run_dir = ctl.registry / "runs" / run_id
    sup = _wait_for_supervisor(run_dir)
    assert sup["killed"] is True  # the run really did reach a terminal state

    observed = wait_for_env_marker(run_dir / ".stub-llm-headers.json")
    assert observed, "stub pi never dumped its observed headers"
    assert observed == {"acme": {"X-Jira-ID": "EST-6588"}}


def test_a_header_ref_derived_value_is_resolved_before_the_stub_pi_sees_it(ctl):
    """A `${env:}` header value resolves ON THE HOST, at `start` time (PRD
    requirement J.1) -- the process the header reaches sees the resolved
    literal, never the `${env:}` spec itself."""
    ctl.write_profile("acme-e2e-ref", {
        "model": "acme/big", "env": {},
        "pi": {"providers": {"acme": {"baseUrl": "https://a.example.com"}}},
    })
    run_id = "tst-header-e2e-ref"
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "acme-e2e-ref",
                  "--llm-header", "X-Jira-ID=${env:E2E_HEADER_VAR}",
                  "--run-id", run_id, "--on-complete", "idle",
                  "--iterations", "10", "--max-approaches", "1",
                  env={**_live_env(), "STUB_TASKS": "1", "STUB_SLEEP": "0",
                       "E2E_HEADER_VAR": "EST-9001"})
    assert res.returncode == 0, res.stderr
    run_dir = ctl.registry / "runs" / run_id
    _wait_for_supervisor(run_dir)

    observed = wait_for_env_marker(run_dir / ".stub-llm-headers.json")
    assert observed == {"acme": {"X-Jira-ID": "EST-9001"}}
