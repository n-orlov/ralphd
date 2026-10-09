"""The stub Codex CLI (`tests/stub-codex/codex`), one test per scenario. It is
run as a plain subprocess, the way the codex wrapper runs the real CLI. No
network, no credential."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

STUB = Path(__file__).resolve().parent / "stub-codex" / "codex"
FAKE_TOKEN = "fake-bearer-token-not-a-secret-0001"


def _run(tmp_path, scenario=None, env=None, prompt="hello", home=True):
    """Run the stub to completion. Returns (events, proc, observed)."""
    observe = tmp_path / "observed.json"
    full = {k: v for k, v in os.environ.items() if not k.startswith("STUB_CODEX_")}
    full["STUB_CODEX_OBSERVE"] = str(observe)
    if scenario:
        full["STUB_CODEX_SCENARIO"] = scenario
    if home:
        ch = tmp_path / "codex-home"
        ch.mkdir(exist_ok=True)
        (ch / "config.toml").write_text('model = "stub"\n')
        full["CODEX_HOME"] = str(ch)
    full.update(env or {})
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(STUB), "exec", "--json", "-C", str(ws), "-"],
        input=prompt, capture_output=True, text=True, timeout=30, cwd=ws, env=full)
    events = []
    for line in proc.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    obs = json.loads(observe.read_text()) if observe.exists() else None
    return events, proc, obs


def _types(events):
    return [e["type"] for e in events]


def _items(events, kind):
    return [e["item"] for e in events if "item" in e and e["item"].get("type") == kind]


def test_version_probe():
    out = subprocess.run([str(STUB), "--version"], capture_output=True, text=True, timeout=20)
    assert out.stdout.strip() == "codex-cli 0.156.1"
    assert out.returncode == 0
    env = {**os.environ, "STUB_CODEX_VERSION": "0.150.0"}
    out = subprocess.run([str(STUB), "--version"], capture_output=True, text=True, timeout=20,
                         env=env)
    assert out.stdout.strip() == "codex-cli 0.150.0"


def test_observation_file(tmp_path):
    events, proc, obs = _run(tmp_path, prompt="the prompt\nline two",
                             env={"AWS_BEARER_TOKEN_BEDROCK": FAKE_TOKEN})
    assert proc.returncode == 0
    assert obs["argv"] == ["exec", "--json", "-C", str(tmp_path / "ws"), "-"]
    assert obs["stdin"] == "the prompt\nline two"
    assert obs["uid"] == os.getuid()
    assert obs["cwd"] == str(tmp_path / "ws")
    assert obs["pid"] > 0
    assert obs["env"]["AWS_BEARER_TOKEN_BEDROCK"] == FAKE_TOKEN
    assert obs["env"]["CODEX_HOME"] == str(tmp_path / "codex-home")
    assert obs["codex_home_exists"] is True
    assert obs["codex_home_entries"] == ["config.toml"]
    assert obs["config_toml"] == 'model = "stub"\n'


def test_observation_without_codex_home(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "CODEX_HOME"}
    observe = tmp_path / "o.json"
    env["STUB_CODEX_OBSERVE"] = str(observe)
    subprocess.run([sys.executable, str(STUB), "exec"], input="x", text=True,
                   capture_output=True, timeout=30, env=env)
    obs = json.loads(observe.read_text())
    assert obs["codex_home"] is None and obs["config_toml"] is None
    assert obs["codex_home_exists"] is False


def test_default_observation_path_follows_stub_run_dir(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("STUB_CODEX_")}
    env["STUB_RUN_DIR"] = str(tmp_path)
    subprocess.run([sys.executable, str(STUB), "exec"], input="x", text=True,
                   capture_output=True, timeout=30, env=env)
    assert json.loads((tmp_path / ".stub-codex-observed.json").read_text())["stdin"] == "x"


def test_text_scenario(tmp_path):
    events, proc, _ = _run(tmp_path)
    assert proc.returncode == 0
    assert _types(events) == ["thread.started", "turn.started", "item.completed",
                              "turn.completed"]
    assert _items(events, "agent_message")[0]["text"] == "done\n<promise>COMPLETE</promise>"
    usage = events[-1]["usage"]
    assert usage["input_tokens"] == 2400 and usage["cached_input_tokens"] == 2000
    assert usage["reasoning_output_tokens"] == 40


def test_text_and_usage_overrides(tmp_path):
    events, proc, _ = _run(tmp_path, env={
        "STUB_CODEX_TEXT": "custom",
        "STUB_CODEX_USAGE": json.dumps({"input_tokens": 7, "output_tokens": 9})})
    assert proc.returncode == 0
    assert _items(events, "agent_message")[0]["text"] == "custom"
    assert events[-1]["usage"] == {"input_tokens": 7, "output_tokens": 9}


def test_tools_scenario(tmp_path):
    events, proc, _ = _run(tmp_path, "tools")
    assert proc.returncode == 0
    assert events[-1]["type"] == "turn.completed"
    cmds = [e for e in events if e.get("item", {}).get("type") == "command_execution"]
    assert [(e["type"], e["item"]["status"]) for e in cmds] == [
        ("item.started", "in_progress"), ("item.completed", "completed"),
        ("item.started", "in_progress"), ("item.completed", "failed")]
    assert cmds[0]["item"]["exit_code"] is None
    assert cmds[1]["item"]["exit_code"] == 0 and cmds[1]["item"]["aggregated_output"] == "hello\n"
    assert cmds[3]["item"]["exit_code"] == 3
    assert cmds[0]["item"]["id"] == cmds[1]["item"]["id"] != cmds[2]["item"]["id"]
    change = _items(events, "file_change")[0]
    assert change["changes"] == [{"path": "/abs/patched.txt", "kind": "add"},
                                 {"path": "/abs/existing.txt", "kind": "update"}]
    assert change["status"] == "completed"
    mcp = [e for e in events if e.get("item", {}).get("type") == "mcp_tool_call"]
    assert [e["type"] for e in mcp] == ["item.started", "item.completed"]
    assert mcp[1]["item"]["server"] == "ralphd" and mcp[1]["item"]["tool"] == "task-insert"
    assert mcp[1]["item"]["arguments"] == {"title": "stub task"}
    assert mcp[1]["item"]["result"]["content"][0]["text"] == "inserted stub task"
    assert _items(events, "reasoning") and _items(events, "agent_message")


def test_failed_scenario(tmp_path):
    events, proc, _ = _run(tmp_path, "failed")
    assert proc.returncode == 1
    assert events[-1] == {"type": "turn.failed", "error": {
        "message": "stream disconnected before completion: stub failure"}}
    assert any(e["type"] == "error" and e["message"].startswith("Reconnecting...")
               for e in events)
    assert "turn.completed" not in _types(events)


def test_failed_message_knob(tmp_path):
    events, proc, _ = _run(tmp_path, "failed", env={
        "STUB_CODEX_FAILED_MESSAGE": "unexpected status 502 Bad Gateway: nope"})
    assert proc.returncode == 1
    assert events[-1]["error"]["message"] == "unexpected status 502 Bad Gateway: nope"


def test_incomplete_scenario(tmp_path):
    events, proc, _ = _run(tmp_path, "incomplete")
    assert proc.returncode == 1
    assert _types(events)[-2:] == ["item.completed", "turn.failed"]
    assert _items(events, "agent_message")[0]["text"]
    assert "max_output_tokens" in events[-1]["error"]["message"]


def test_no_terminal_scenario(tmp_path):
    events, proc, _ = _run(tmp_path, "no_terminal")
    assert proc.returncode == 0
    assert _items(events, "agent_message")
    assert not {"turn.completed", "turn.failed"} & set(_types(events))


def test_unknown_items_scenario(tmp_path):
    events, proc, _ = _run(tmp_path, "unknown_items")
    assert proc.returncode == 0
    known = {"agent_message", "reasoning", "command_execution", "file_change",
             "mcp_tool_call", "error"}
    unknown = {e["item"]["type"] for e in events if "item" in e} - known
    assert unknown == {"web_search", "todo_list", "future_widget", "no_id_item"}
    assert "future.event" in _types(events)
    assert any("id" not in e["item"] for e in events if "item" in e)
    assert events[-1]["type"] == "turn.completed"


def test_echo_token_scenario(tmp_path):
    events, proc, _ = _run(tmp_path, "echo_token", env={
        "AWS_BEARER_TOKEN_BEDROCK": FAKE_TOKEN})
    assert proc.returncode == 0
    assert FAKE_TOKEN in proc.stdout and FAKE_TOKEN in proc.stderr
    # inside events and as a bare, non-JSON stdout line
    assert any(FAKE_TOKEN in json.dumps(e) for e in events)
    assert any(FAKE_TOKEN in ln and not ln.startswith("{") for ln in proc.stdout.splitlines())
    assert events[-1]["type"] == "turn.completed"


def test_unknown_scenario_exits_2(tmp_path):
    _, proc, _ = _run(tmp_path, "no-such-scenario")
    assert proc.returncode == 2 and "unknown scenario" in proc.stderr


def _spawn_hang(tmp_path, scenario, env=None):
    observe = tmp_path / "o.json"
    full = {k: v for k, v in os.environ.items() if not k.startswith("STUB_CODEX_")}
    full.update({"STUB_CODEX_SCENARIO": scenario, "STUB_CODEX_OBSERVE": str(observe), **(env or {})})
    proc = subprocess.Popen(
        [sys.executable, str(STUB), "exec", "--json", "-"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=full, text=True, cwd=tmp_path,
        # runners ignore SIGINT and children inherit it: reset before the stub starts
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    proc.stdin.write("hi")
    proc.stdin.close()
    return proc


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_hang_exits_1_on_sigint_and_leaves_no_child(tmp_path):
    pidfile = tmp_path / "child.pid"
    proc = _spawn_hang(tmp_path, "hang", {"STUB_CODEX_CHILD_PIDFILE": str(pidfile)})
    try:
        assert json.loads(proc.stdout.readline())["type"] == "thread.started"
        child = int(pidfile.read_text())
        assert _alive(child)
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=20) == 1
        deadline = time.monotonic() + 10
        while _alive(child) and time.monotonic() < deadline:
            try:
                os.waitpid(child, os.WNOHANG)
            except ChildProcessError:
                pass
            time.sleep(0.05)
        assert not _alive(child)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_hang_writes_marker_file_and_can_be_silent(tmp_path):
    proc = _spawn_hang(tmp_path, "hang", {"STUB_CODEX_SILENT": "1",
                                          "STUB_CODEX_WRITE_FILE": "edit.txt"})
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "edit.txt").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert (tmp_path / "edit.txt").read_text().startswith("written by stub-codex")
        time.sleep(0.3)
        assert proc.poll() is None
        proc.kill()
        assert proc.stdout.read() == ""
    finally:
        if proc.poll() is None:
            proc.kill()


def test_ignore_sigint_survives_sigint_until_killed(tmp_path):
    proc = _spawn_hang(tmp_path, "ignore_sigint")
    try:
        assert json.loads(proc.stdout.readline())["type"] == "thread.started"
        proc.send_signal(signal.SIGINT)
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=1.0)
        proc.kill()
        assert proc.wait(timeout=20) == -signal.SIGKILL
    finally:
        if proc.poll() is None:
            proc.kill()


def test_every_stub_env_knob_is_documented():
    source = STUB.read_text()
    doc = source.split('"""', 2)[1]
    used = set(re.findall(r"STUB_CODEX_[A-Z_]+", source.split('"""', 2)[2]))
    missing = sorted(k for k in used if k not in doc)
    assert not missing, f"undocumented knobs: {missing}"
    assert os.access(STUB, os.X_OK)
