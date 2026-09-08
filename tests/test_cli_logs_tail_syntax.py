"""Black-box tests for `ralphctl logs`'s tail-style syntax (PRD req 3):
`-N`, `-Nf`, `-f`, the `logsf` alias, and default tail 50.

Uses the shared `live` fixture / `LiveRun` harness from tests/conftest.py
(real `ralphd-engine`, no Docker); `ralphctl` itself runs in-process through
`tests/conftest.py`'s `call_ralphctl_in_process` (task 002, requirement F),
including the follow-across-iterations node, which drives that same
in-process call from a background thread while the main thread polls the
run's own `status.json` -- no second real OS process is needed, since the
thread never overlaps a second `ralphctl` call (the main thread makes none
until after `t1.join()`), only the concurrently-running child `ralphd-engine`
subprocess itself (unaffected -- it was never the thing made in-process).
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest

from ralphd.cli.main import _preprocess_logs_argv
from tests.conftest import call_ralphctl_in_process


def _in_process_ralphctl(run, capsys):
    def ralphctl(*argv: str):
        env = {**os.environ, "RALPHD_REGISTRY": str(run.registry)}
        return call_ralphctl_in_process(list(argv), env=env, capsys=capsys)
    run.ralphctl = ralphctl
    return run


def _wait_api_ready(port: int, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
            return
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.1)
    raise TimeoutError("engine API never became reachable")


# --------------------------------------------------------------------------
# Pure argv-rewriting unit tests (no process spawned)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("argv,expected", [
    (["logs", "id1"], ["logs", "id1"]),
    (["logs", "id1", "-100"], ["logs", "id1", "--tail", "100"]),
    (["logs", "id1", "-150f"], ["logs", "id1", "--tail", "150", "--follow"]),
    (["logs", "id1", "-f"], ["logs", "id1", "--follow"]),
    (["logsf", "id1"], ["logs", "id1", "--follow"]),
    (["--json", "logs", "id1", "-100"], ["--json", "logs", "id1", "--tail", "100"]),
    (["logs", "id1", "--iteration", "2"], ["logs", "id1", "--iteration", "2"]),
    (["status", "id1"], ["status", "id1"]),
])
def test_preprocess_logs_argv_rewrites_recognized_forms(argv, expected):
    assert _preprocess_logs_argv(argv) == expected


@pytest.mark.parametrize("argv", [
    ["logs", "id1", "-abc"],
    ["logs", "id1", "-0abc"],
    ["logs", "id1", "-f100"],
])
def test_preprocess_logs_argv_leaves_unrecognized_forms_for_argparse(argv):
    # Unrecognized dash-tokens are passed through untouched; argparse itself
    # then rejects them (exit 2) since they match no defined option.
    assert _preprocess_logs_argv(argv) == argv


# --------------------------------------------------------------------------
# Black-box CLI tests against a live test engine
# --------------------------------------------------------------------------


def test_logs_default_tail_is_50(live, capsys):
    run = _in_process_ralphctl(
        live(stub_env={"STUB_RICH_EVENTS": "1", "STUB_TASKS": "4"}), capsys)
    run.wait_terminal()

    default_res = run.ralphctl("logs", run.run_id, "--raw")
    assert default_res.returncode == 0, (default_res.stdout, default_res.stderr)

    full_res = run.ralphctl("logs", run.run_id, "--raw", "--tail", "0")
    assert full_res.returncode == 0

    def content_lines(out: str) -> list[str]:
        result = []
        for line in out.splitlines():
            if not line.strip():
                continue
            if "ralphd.iteration" in line:
                continue
            result.append(line)
        return result

    default_content = content_lines(default_res.stdout)
    full_content = content_lines(full_res.stdout)

    # this stub job produces far more than 50 transcript lines
    assert len(full_content) > 50
    assert len(default_content) == 50


def test_logs_tail_dash_n(live, capsys):
    run = _in_process_ralphctl(live(run_id="logtest-n",
              stub_env={"STUB_RICH_EVENTS": "1", "STUB_TASKS": "4"}), capsys)
    run.wait_terminal()

    res = run.ralphctl("logs", run.run_id, "-100", "--raw")
    assert res.returncode == 0, (res.stdout, res.stderr)
    content = [l for l in res.stdout.splitlines()
              if l.strip() and "ralphd.iteration" not in l]
    assert len(content) == 100


def test_logsf_alias_and_dash_f_follow_across_iterations(live, capsys):
    run = _in_process_ralphctl(live(run_id="logtest-follow",
              stub_env={"STUB_RICH_EVENTS": "1", "STUB_TASKS": "2",
                        "STUB_SLEEP": "0"}), capsys)

    results = {}

    def go(name, argv):
        results[name] = run.ralphctl(*argv)

    _wait_api_ready(run.port)
    t1 = threading.Thread(target=go, args=("f", ["logs", run.run_id, "-f", "--raw"]))
    t1.start()
    time.sleep(0.3)

    run.wait_terminal(timeout=60)
    t1.join(timeout=30)

    assert "f" in results, "follow invocation never returned"
    res = results["f"]
    assert res.returncode == 0, (res.stdout, res.stderr)
    numbers = set()
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "ralphd.iteration" and ev.get("event") == "start":
            numbers.add(ev.get("number"))
    assert len(numbers) >= 2, res.stdout

    # `logsf <id>` (no --raw, default rendering) behaves like `logs -f`:
    # streams the whole (already-finished) job and exits cleanly.
    res2 = run.ralphctl("logsf", run.run_id)
    assert res2.returncode == 0, (res2.stdout, res2.stderr)
    assert "iteration 1" in res2.stdout


def test_logs_invalid_tail_form_exits_2(live, capsys):
    run = _in_process_ralphctl(
        live(run_id="logtest-invalid", stub_env={"STUB_RICH_EVENTS": "1"}), capsys)
    run.wait_terminal()

    res = run.ralphctl("logs", run.run_id, "-abc")
    assert res.returncode == 2
