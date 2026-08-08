"""Black-box tests for `ralphctl logs` pretty rendering (PRD reqs 2, 4).

Runs a real `ralphd-engine` directly (no Docker) wired up under a temp
registry's runs/configs layout, writes the `host.json` that `ralphctl`
expects to find for a "live" run, then drives the real `ralphctl`
executable against it as a subprocess and asserts on stdout only —
strictly black-box.

The `LiveRun` harness and `live` fixture live in tests/conftest.py (shared
with tests/test_cli_logs_tail_syntax.py).
"""

from __future__ import annotations

import json


# --------------------------------------------------------------------------
def test_logs_pretty_rendering_default(live):
    run = live(stub_env={"STUB_RICH_EVENTS": "1", "STUB_BAD_LINE": "1",
                         "STUB_TASKS": "2"})
    run.wait_terminal()

    res = run.ralphctl("logs", run.run_id, "--tail", "0")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "Traceback" not in res.stderr
    out = res.stdout

    # iteration/phase boundary headers for multiple iterations
    assert "iteration 1" in out and "phase=planning" in out
    assert "iteration 2" in out and "phase=worker" in out
    assert "iteration 4" in out and "phase=review" in out

    # streamed assistant text is visible
    assert "planning done" in out
    assert "everything finished" in out

    # thinking is elided to a marker, never the raw thinking content
    assert "[thinking" in out
    assert "Picking the next pending task" not in out
    assert "Considering the best approach" not in out

    # tool calls render as compact one-liners with an outcome
    assert "bash(" in out
    assert "ok" in out

    # malformed line -> tolerant marker, no crash
    assert "malformed" in out
    assert "not json at all" not in out  # only in --raw, not pretty mode

    # per-iteration usage/cost footer
    assert "tokens=" in out

    # non-TTY: no ANSI escapes
    assert "\x1b[" not in out


def test_logs_raw_passthrough(live):
    run = live(run_id="logtest-raw",
              stub_env={"STUB_RICH_EVENTS": "1", "STUB_BAD_LINE": "1",
                        "STUB_TASKS": "1"})
    run.wait_terminal()

    res = run.ralphctl("logs", run.run_id, "--raw", "--tail", "0")
    assert res.returncode == 0, (res.stdout, res.stderr)
    lines = [l for l in res.stdout.splitlines() if l.strip()]
    json_lines = 0
    malformed_lines = 0
    for line in lines:
        try:
            json.loads(line)
            json_lines += 1
        except json.JSONDecodeError:
            malformed_lines += 1
    assert json_lines > 0
    assert malformed_lines == 1  # the raw malformed line, passed through verbatim
    assert "not json at all -- malformed line for tolerance testing" in res.stdout
    assert any(json.loads(l).get("type") == "ralphd.iteration"
              for l in lines if l.strip().startswith("{"))


def test_logs_iteration_filter(live):
    run = live(run_id="logtest-iter",
              stub_env={"STUB_RICH_EVENTS": "1", "STUB_TASKS": "2"})
    run.wait_terminal()

    res = run.ralphctl("logs", run.run_id, "--iteration", "1")
    assert res.returncode == 0, (res.stdout, res.stderr)
    out = res.stdout
    assert "planning done" in out
    assert "phase=worker" not in out
    assert "phase=review" not in out
    assert "iteration 2" not in out


def test_logs_unknown_event_type_silently_skipped(live):
    run = live(run_id="logtest-unknown",
              stub_env={"STUB_RICH_EVENTS": "1", "STUB_TASKS": "1"})
    run.wait_terminal()

    it1_output = run.run_dir / "iterations" / "0001" / "output.jsonl"
    with open(it1_output, "a") as f:
        f.write(json.dumps({"type": "totally.unknown.future.event",
                            "secret_marker_xyz": "should-not-render"}) + "\n")

    res = run.ralphctl("logs", run.run_id, "--tail", "0")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "secret_marker_xyz" not in res.stdout
    assert "should-not-render" not in res.stdout
    assert "totally.unknown.future.event" not in res.stdout
    # rest of the transcript still renders fine
    assert "planning done" in res.stdout
