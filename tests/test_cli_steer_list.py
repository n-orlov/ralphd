"""Task 018 (#17): `ralphctl steer <run> --list` -- steering stops being
write-only in the terminal too.

Issue #17's complaint is that an operator could POST a steering message and
then had no surface at all for "what did I queue, what has the loop already
applied, what did the text say". Tasks 016/017 gave the hub that view; this
module pins the CLI half:

  * `--list` prints pending AND applied entries as a table and, with the
    global `--json`, as the full documents (bodies included);
  * it works live (the running engine decides applied-ness) and after the
    container is gone (the on-disk snapshot, flagged on stderr like
    `ralphctl logs`/`ralphctl tasks`);
  * the CLI and the hub show the SAME entries for the same run -- by
    construction, because both go through `ui_server.steering_list`, and
    asserted here for a stub-live run, an on-disk snapshot and one REAL
    engine;
  * `--list` is a read: it never consumes stdin and never sends a message.

Tiers: unit (the preview formatter), black-box `ralphctl` subprocesses over
run-dir fixtures, black-box HTTP against a real `ralphctl ui` server for the
agreement assertions, and one real engine via the `live` fixture.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run
from test_cli_ui import StubEngineApi, UiServer, _write_run_with_api, ui

from ralphd.cli.main import _STEER_SNAPSHOT_NOTICE, _steer_preview
from ralphd.cli.ui_server import NO_STEERING
from ralphd.engine.state import STEERING_APPLIED, STEERING_CONSUMED_FILE, STEERING_PENDING

__all__ = ["UiServer", "ctl", "ui", "unix_sock"]

RALPHCTL = Path(sys.executable).parent / "ralphctl"

ONE = "Focus on the <tests> & drop the docs task.\n\nSecond paragraph.\n"
TWO = "Then ship it.\n"


def ralphctl(registry: Path, *argv: str, stdin: str | None = None):
    """Black-box `ralphctl` against a registry -- no stub docker needed: this
    command never shells out to docker, live or dead."""
    return subprocess.run([str(RALPHCTL), *argv],
                          env={**os.environ, "RALPHD_REGISTRY": str(registry)},
                          input=stdin, capture_output=True, text=True, timeout=60)


def _seed_steering(run_dir: Path, files: dict[str, str],
                   consumed: list[str] | None = None) -> None:
    sdir = run_dir / "steering"
    sdir.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (sdir / name).write_text(body)
    if consumed is not None:
        (sdir / STEERING_CONSUMED_FILE).write_text(json.dumps(consumed))


def _rows(stdout: str) -> list[list[str]]:
    """The table's data rows, split into columns (header dropped)."""
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    assert lines and lines[0].split()[:2] == ["SEQ", "STATE"], stdout
    return [ln.split() for ln in lines[1:]]


def _hub_entries(server: UiServer, run_id: str) -> tuple[bool, list[dict]]:
    code, body = server.get(f"/api/runs/{run_id}/steering")
    assert code == 200, (code, body)
    return body["live"], body["entries"]


def _cli_entries(res: subprocess.CompletedProcess) -> tuple[bool, list[dict]]:
    assert res.returncode == 0, (res.stdout, res.stderr)
    doc = json.loads(res.stdout)
    return doc["live"], doc["entries"]


def _wait_for(fn, timeout=30, what="condition"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(0.2)
    raise TimeoutError(f"{what} never happened; last: {last!r}")


# --------------------------------------------------------------------------
# the MESSAGE preview
# --------------------------------------------------------------------------

def test_preview_collapses_newlines_so_a_row_stays_a_row():
    """A steering message is multi-line prose; the table is an index."""
    assert _steer_preview("line one\n\nline two\n") == "line one line two"


def test_preview_truncates_a_long_message_with_an_ellipsis():
    out = _steer_preview("x" * 200, width=20)
    assert len(out) == 20
    assert out.endswith("\u2026")


def test_preview_of_an_empty_or_missing_body_is_empty():
    assert _steer_preview("") == ""
    assert _steer_preview(None) == ""
    assert _steer_preview("   \n\n") == ""


# --------------------------------------------------------------------------
# container gone: the on-disk snapshot
# --------------------------------------------------------------------------

def test_list_of_a_dead_run_prints_pending_and_applied(ctl: Ctl):
    """The case an operator most needs it: the run is over -- what was steered
    and did the loop ever act on it?"""
    rdir, _ = _seed_run(ctl, "st-dead")
    _seed_steering(rdir, {"001-focus.md": ONE, "002-ship.md": TWO},
                   consumed=["001-focus.md"])

    res = ralphctl(ctl.registry, "steer", "st-dead", "--list")
    assert res.returncode == 0, (res.stdout, res.stderr)
    rows = _rows(res.stdout)
    assert [r[0] for r in rows] == ["1", "2"]
    assert [r[1] for r in rows] == [STEERING_APPLIED, STEERING_PENDING]
    assert "focus" in res.stdout and "ship" in res.stdout
    assert "Focus on the <tests>" in res.stdout
    # the snapshot marker goes to stderr, never into the table
    assert _STEER_SNAPSHOT_NOTICE in res.stderr
    assert "snapshot" not in res.stdout


def test_list_json_of_a_dead_run_carries_the_full_bodies(ctl: Ctl):
    rdir, _ = _seed_run(ctl, "st-json")
    _seed_steering(rdir, {"001-focus.md": ONE}, consumed=[])

    res = ralphctl(ctl.registry, "--json", "steer", "st-json", "--list")
    live, entries = _cli_entries(res)
    assert live is False
    (entry,) = entries
    assert entry["file"] == "001-focus.md"
    assert entry["seq"] == 1
    assert entry["name"] == "focus"
    assert entry["state"] == STEERING_PENDING
    assert entry["consumed"] is False
    assert entry["body"] == ONE
    assert entry["tsLocal"]
    # a clean document: the notice is on stderr
    assert _STEER_SNAPSHOT_NOTICE in res.stderr


def test_a_run_nobody_steered_says_so_in_the_hub_s_words(ctl: Ctl):
    _seed_run(ctl, "st-none")
    res = ralphctl(ctl.registry, "steer", "st-none", "--list")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert res.stdout.strip() == NO_STEERING


def test_an_unknown_run_is_exit_3(ctl: Ctl):
    res = ralphctl(ctl.registry, "steer", "nosuch", "--list")
    assert res.returncode == 3, (res.stdout, res.stderr)
    assert "not found" in res.stderr


def test_an_entry_whose_body_is_empty_still_gets_a_row(ctl: Ctl):
    rdir, _ = _seed_run(ctl, "st-empty")
    _seed_steering(rdir, {"001-oops.md": "   \n"})
    res = ralphctl(ctl.registry, "steer", "st-empty", "--list")
    assert res.returncode == 0, (res.stdout, res.stderr)
    (row,) = _rows(res.stdout)
    assert row[:2] == ["1", STEERING_PENDING]
    assert row[-1] == "oops"  # nothing in MESSAGE, so NAME ends the row


# --------------------------------------------------------------------------
# --list is a read
# --------------------------------------------------------------------------

def test_list_does_not_consume_stdin_or_send_anything(ctl: Ctl):
    """`steer <run>` with no message reads stdin; `--list` must not -- being
    run in a pipeline cannot turn a listing into a POST."""
    rdir, _ = _seed_run(ctl, "st-stdin")
    _seed_steering(rdir, {"001-a.md": TWO})
    res = ralphctl(ctl.registry, "steer", "st-stdin", "--list",
                   stdin="this text must never become a steering message\n")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert [p.name for p in sorted((rdir / "steering").glob("*.md"))] == ["001-a.md"]
    assert TWO.strip() in res.stdout


def test_list_refuses_to_be_combined_with_sending(ctl: Ctl):
    rdir, _ = _seed_run(ctl, "st-both")
    for extra in (["a message"], ["--name", "x"], ["--now"]):
        res = ralphctl(ctl.registry, "steer", "st-both", "--list", *extra)
        assert res.returncode == 2, (extra, res.stdout, res.stderr)
        assert "--list only shows" in res.stderr
    assert not (rdir / "steering").exists()


# --------------------------------------------------------------------------
# CLI and hub agree
# --------------------------------------------------------------------------

def test_cli_and_hub_agree_for_a_dead_run(ctl: Ctl, ui):
    rdir, _ = _seed_run(ctl, "st-agree")
    _seed_steering(rdir, {"001-focus.md": ONE, "002-ship.md": TWO},
                   consumed=["001-focus.md"])
    server = ui(ctl.registry)

    hub_live, hub_entries = _hub_entries(server, "st-agree")
    cli_live, cli_entries = _cli_entries(
        ralphctl(ctl.registry, "--json", "steer", "st-agree", "--list"))
    assert (cli_live, cli_entries) == (hub_live, hub_entries)
    assert hub_live is False


def test_cli_and_hub_agree_for_a_live_run(tmp_path, ui):
    """A stub-live run: the CLI must take the LIVE answer (not the disk it can
    also see) and it must be the very same entries the hub serves."""
    registry = tmp_path / "registry"
    engine = StubEngineApi(steering=[{"file": "001-live.md", "seq": 1,
                                      "name": "live", "state": STEERING_APPLIED,
                                      "consumed": True,
                                      "ts": "2026-09-02T10:00:00Z",
                                      "bytes": len(TWO), "hasBody": True,
                                      "body": TWO}])
    try:
        run_dir = _write_run_with_api(registry, "st-live", engine)
        _seed_steering(run_dir, {"001-live.md": TWO})
        server = ui(registry)

        hub_live, hub_entries = _hub_entries(server, "st-live")
        res = ralphctl(registry, "--json", "steer", "st-live", "--list")
        cli_live, cli_entries = _cli_entries(res)
        assert cli_live is True and hub_live is True
        assert cli_entries == hub_entries
        assert cli_entries[0]["state"] == STEERING_APPLIED
        assert _STEER_SNAPSHOT_NOTICE not in res.stderr
    finally:
        engine.close()


def test_a_pre_v06_live_answer_is_completed_from_disk(tmp_path, ui):
    """A pre-v0.6 engine answers `GET /steering` with only file/consumed. The
    CLI table still shows a name, an arrival time and a preview (from disk),
    and still agrees with the hub."""
    registry = tmp_path / "registry"
    engine = StubEngineApi(steering=[{"file": "001-old.md", "consumed": True}])
    try:
        run_dir = _write_run_with_api(registry, "st-old", engine)
        _seed_steering(run_dir, {"001-old.md": TWO})
        server = ui(registry)

        res = ralphctl(registry, "steer", "st-old", "--list")
        assert res.returncode == 0, (res.stdout, res.stderr)
        (row,) = _rows(res.stdout)
        assert row[:2] == ["1", STEERING_APPLIED]
        assert row[-1] == "it."  # the preview's last word: the body is there

        _, hub_entries = _hub_entries(server, "st-old")
        json_res = ralphctl(registry, "--json", "steer", "st-old", "--list")
        assert _cli_entries(json_res)[1] == hub_entries
    finally:
        engine.close()


def test_real_engine_live_then_container_gone(live, ui):
    """The strongest test: steer a REAL engine from the CLI, watch `--list`
    report it pending and then applied, confirm the hub says the same, then
    stop the engine and assert `--list` still shows the same entries from the
    run dir with the snapshot notice."""
    run = live(run_id="steercli", job={"iterations": 8, "on_complete": "idle"},
               stub_env={"STUB_SLEEP": "1", "STUB_TASKS": "4"})
    run.wait_api()
    server = ui(run.registry)

    posted = run.ralphctl("steer", "steercli", ONE, "--name", "from-cli")
    assert posted.returncode == 0, (posted.stdout, posted.stderr)

    def listed():
        live_flag, entries = _cli_entries(
            run.ralphctl("--json", "steer", "steercli", "--list"))
        return (live_flag, entries) if entries else None

    live_flag, entries = _wait_for(listed, what="the queued entry appearing")
    assert live_flag is True
    (entry,) = entries
    assert entry["name"] == "from-cli"
    assert entry["body"].strip() == ONE.strip()

    def applied_yet():
        _, current = _cli_entries(
            run.ralphctl("--json", "steer", "steercli", "--list"))
        return current if current[0]["state"] == STEERING_APPLIED else None

    applied = _wait_for(applied_yet, what="the loop consuming the entry")
    assert _hub_entries(server, "steercli") == (True, applied)

    # the human table names the state and the operator's own --name
    table = run.ralphctl("steer", "steercli", "--list")
    (row,) = _rows(table.stdout)
    assert row[:2] == ["1", STEERING_APPLIED]
    assert "from-cli" in table.stdout

    run.stop()
    _wait_for(lambda: _hub_entries(server, "steercli")[0] is False,
              what="the API going away")
    dead = run.ralphctl("--json", "steer", "steercli", "--list")
    assert _cli_entries(dead) == (False, applied)
    assert _STEER_SNAPSHOT_NOTICE in dead.stderr
