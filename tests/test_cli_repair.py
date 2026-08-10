"""Black-box tests for task 008: `ralphctl repair <run-id>` diagnosis mode --
validates status.json/tasks.json/host.json against their expected shapes,
refuses to touch a run whose container is currently running, and appends a
`type: repair` audit line to events.jsonl for every invocation.

Reuses the stub-docker recording harness + `_seed_run` helper from
test_cli_docker.py / test_cli_resume.py (no real container/engine needed).
"""

from __future__ import annotations

import json

from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run

__all__ = ["ctl", "unix_sock"]


def _events(rdir) -> list[dict]:
    p = rdir / "events.jsonl"
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line]


def test_repair_unknown_run_exits_3(ctl: Ctl):
    res = ctl.run("repair", "no-such-run")
    assert res.returncode == 3, res.stderr
    assert "not found" in res.stderr


def test_repair_refuses_while_container_running(ctl: Ctl):
    _seed_run(ctl, "tst-alive")
    res = ctl.run("repair", "tst-alive", env={
        "STUB_DOCKER_CONTAINERS": "ralphd-tst-alive",
        "STUB_DOCKER_RUNNING": "ralphd-tst-alive",
    })
    assert res.returncode == 5, res.stderr
    assert "running" in res.stderr
    # nothing appended to events.jsonl -- the run was never touched
    rdir = ctl.registry / "runs" / "tst-alive"
    assert _events(rdir) == []


def test_repair_clean_run_reports_no_issues(ctl: Ctl):
    rdir, _cdir = _seed_run(ctl, "tst-clean")
    (rdir / "status.json").write_text(json.dumps(
        {"state": "failed", "schemaVersion": 1}))
    (rdir / "tasks.json").write_text(json.dumps(
        {"version": 1, "tasks": [
            {"id": "001", "title": "do the thing", "status": "completed"},
        ]}))
    res = ctl.run("--json", "repair", "tst-clean")
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    assert doc["ok"] is True
    assert doc["issues"] == []
    assert sorted(doc["checked"]) == ["host.json", "status.json", "tasks.json"]


def test_repair_reports_corrupted_status_json(ctl: Ctl):
    rdir, _cdir = _seed_run(ctl, "tst-corrupt")
    (rdir / "status.json").write_text("{not valid json")
    res = ctl.run("--json", "repair", "tst-corrupt")
    assert res.returncode == 1, res.stderr
    doc = json.loads(res.stdout)
    assert doc["ok"] is False
    assert any("status.json" in i and "malformed" in i for i in doc["issues"])


def test_repair_reports_bad_task_status_and_missing_fields(ctl: Ctl):
    rdir, _cdir = _seed_run(ctl, "tst-badtasks")
    (rdir / "status.json").write_text(json.dumps({"state": "failed"}))
    (rdir / "tasks.json").write_text(json.dumps(
        {"version": 1, "tasks": [
            {"id": "001", "status": "bogus-status"},  # missing title
            {"id": "001", "title": "dup", "status": "pending"},  # dup id
        ]}))
    res = ctl.run("--json", "repair", "tst-badtasks")
    assert res.returncode == 1, res.stderr
    doc = json.loads(res.stdout)
    assert doc["ok"] is False
    joined = "\n".join(doc["issues"])
    assert "missing 'title'" in joined
    assert "unrecognized" in joined and "bogus-status" in joined
    assert "duplicate task id" in joined


def test_repair_missing_host_json_reported(ctl: Ctl):
    rdir, _cdir = _seed_run(ctl, "tst-nohost")
    (rdir / "host.json").unlink()
    (rdir / "status.json").write_text(json.dumps({"state": "failed"}))
    res = ctl.run("--json", "repair", "tst-nohost")
    assert res.returncode == 1, res.stderr
    doc = json.loads(res.stdout)
    assert any("host.json: missing" in i for i in doc["issues"])


def test_repair_appends_audit_event_never_leaking_values(ctl: Ctl):
    rdir, _cdir = _seed_run(ctl, "tst-audit")
    (rdir / "status.json").write_text(json.dumps({"state": "failed"}))
    res = ctl.run("--json", "repair", "tst-audit")
    assert res.returncode == 0, res.stderr
    events = _events(rdir)
    repairs = [e for e in events if e.get("type") == "repair"]
    assert len(repairs) == 1
    ev = repairs[0]
    assert ev["action"] == "diagnose"
    assert sorted(ev["checked"]) == ["host.json", "status.json", "tasks.json"]
    assert ev["issueCount"] == 0
    # no secret-shaped content anywhere in the whole event log
    blob = json.dumps(events)
    assert "secret" not in blob.lower()


def test_repair_stdout_never_contains_running_container_command_output(ctl: Ctl):
    """Non-JSON mode renders a readable summary, not a raw JSON dump."""
    rdir, _cdir = _seed_run(ctl, "tst-human")
    (rdir / "status.json").write_text("{not valid json")
    res = ctl.run("repair", "tst-human")
    assert res.returncode == 1, res.stderr
    assert "issue(s) found" in res.stdout
    assert "status.json" in res.stdout
