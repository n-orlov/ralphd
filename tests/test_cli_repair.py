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


# --- task 009: `repair --set-state <state>` guarded escape hatch --------


def test_repair_set_state_success(ctl: Ctl):
    rdir, _cdir = _seed_run(ctl, "tst-setstate")
    (rdir / "status.json").write_text(json.dumps(
        {"state": "running", "schemaVersion": 1}))
    res = ctl.run("--json", "repair", "tst-setstate", "--set-state", "failed")
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    assert doc["old"] == "running"
    assert doc["new"] == "failed"
    on_disk = json.loads((rdir / "status.json").read_text())
    assert on_disk["state"] == "failed"
    # schemaVersion and other fields survive untouched
    assert on_disk["schemaVersion"] == 1


def test_repair_set_state_invalid_value_rejected(ctl: Ctl):
    rdir, _cdir = _seed_run(ctl, "tst-setstate-bad")
    (rdir / "status.json").write_text(json.dumps({"state": "running"}))
    res = ctl.run("repair", "tst-setstate-bad", "--set-state", "bogus")
    assert res.returncode == 2, res.stderr
    assert "invalid state" in res.stderr
    # nothing was written -- status.json unchanged, no audit event
    on_disk = json.loads((rdir / "status.json").read_text())
    assert on_disk["state"] == "running"
    assert _events(rdir) == []


def test_repair_set_state_refuses_while_container_running(ctl: Ctl):
    rdir, _cdir = _seed_run(ctl, "tst-setstate-alive")
    (rdir / "status.json").write_text(json.dumps({"state": "running"}))
    res = ctl.run("repair", "tst-setstate-alive", "--set-state", "failed",
                  env={
                      "STUB_DOCKER_CONTAINERS": "ralphd-tst-setstate-alive",
                      "STUB_DOCKER_RUNNING": "ralphd-tst-setstate-alive",
                  })
    assert res.returncode == 5, res.stderr
    on_disk = json.loads((rdir / "status.json").read_text())
    assert on_disk["state"] == "running"
    assert _events(rdir) == []


def test_repair_set_state_appends_audit_event_with_old_and_new(ctl: Ctl):
    rdir, _cdir = _seed_run(ctl, "tst-setstate-audit")
    (rdir / "status.json").write_text(json.dumps({"state": "starting"}))
    res = ctl.run("--json", "repair", "tst-setstate-audit", "--set-state",
                  "aborted")
    assert res.returncode == 0, res.stderr
    events = _events(rdir)
    repairs = [e for e in events if e.get("type") == "repair"]
    assert len(repairs) == 1
    ev = repairs[0]
    assert ev["action"] == "set-state"
    assert ev["old"] == "starting"
    assert ev["new"] == "aborted"


# --- task 010: `repair --env KEY=VAL` -- safely edit persisted env wiring -


def test_repair_env_adds_new_key_to_fresh_wiring_file(ctl: Ctl):
    rdir, cdir = _seed_run(ctl, "tst-envadd")
    (rdir / "status.json").write_text(json.dumps({"state": "failed"}))
    assert not (cdir / "env-wiring.json").exists()
    res = ctl.run("--json", "repair", "tst-envadd", "--env",
                  "AWS_BEARER_TOKEN_BEDROCK=sekrit-tok-123")
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    assert doc["action"] == "env"
    assert doc["keys"] == ["AWS_BEARER_TOKEN_BEDROCK"]

    wiring_path = cdir / "env-wiring.json"
    assert wiring_path.is_file()
    assert oct(wiring_path.stat().st_mode)[-3:] == "600"
    wdoc = json.loads(wiring_path.read_text())
    assert wdoc["extra_env"] == ["AWS_BEARER_TOKEN_BEDROCK=sekrit-tok-123"]


def test_repair_env_updates_existing_key_in_place(ctl: Ctl):
    rdir, cdir = _seed_run(ctl, "tst-envupd")
    (rdir / "status.json").write_text(json.dumps({"state": "failed"}))
    wiring_path = cdir / "env-wiring.json"
    wiring_path.write_text(json.dumps(
        {"extra_env": ["FIRST=one", "SECOND=orig-val", "THIRD=three"]}))
    wiring_path.chmod(0o600)

    res = ctl.run("--json", "repair", "tst-envupd", "--env",
                  "SECOND=new-val")
    assert res.returncode == 0, res.stderr

    wdoc = json.loads(wiring_path.read_text())
    # replaced in place, order preserved, no shadowing duplicate appended
    assert wdoc["extra_env"] == ["FIRST=one", "SECOND=new-val", "THIRD=three"]
    assert oct(wiring_path.stat().st_mode)[-3:] == "600"


def test_repair_env_resume_carries_updated_value(ctl: Ctl):
    from test_cli_resume_llm_wiring import _stop_container, docker_run_argv, env_vars

    rdir, _cdir = _seed_run(ctl, "tst-envresume")
    (rdir / "status.json").write_text(json.dumps({"state": "failed"}))
    res = ctl.run("repair", "tst-envresume", "--env", "MY_KEY=fixed-value")
    assert res.returncode == 0, res.stderr

    _stop_container(ctl)
    res = ctl.run("resume", "tst-envresume", env={
        "STUB_DOCKER_CONTAINERS": "ralphd-tst-envresume",
        "STUB_DOCKER_RUNNING": "",
    })
    assert res.returncode == 0, res.stderr
    ev = env_vars(docker_run_argv(ctl))
    assert "MY_KEY=fixed-value" in ev


def test_repair_env_value_never_echoed_or_in_events(ctl: Ctl):
    rdir, _cdir = _seed_run(ctl, "tst-envsecret")
    (rdir / "status.json").write_text(json.dumps({"state": "failed"}))
    secret = "sk-repair-env-never-leaked-42"
    res = ctl.run("repair", "tst-envsecret", "--env", f"MY_TOKEN={secret}")
    assert res.returncode == 0, res.stderr
    assert secret not in res.stdout
    assert secret not in res.stderr

    events = _events(rdir)
    repairs = [e for e in events if e.get("type") == "repair"]
    assert len(repairs) == 1
    ev = repairs[0]
    assert ev["action"] == "env"
    assert ev["keys"] == ["MY_TOKEN"]
    assert secret not in json.dumps(events)


def test_repair_env_invalid_kv_rejected(ctl: Ctl):
    rdir, cdir = _seed_run(ctl, "tst-envbad")
    (rdir / "status.json").write_text(json.dumps({"state": "failed"}))
    res = ctl.run("repair", "tst-envbad", "--env", "NOEQUALSIGN")
    assert res.returncode == 2, res.stderr
    assert "KEY=VAL" in res.stderr
    assert not (cdir / "env-wiring.json").exists()
    assert _events(rdir) == []


def test_repair_env_refuses_while_container_running(ctl: Ctl):
    rdir, cdir = _seed_run(ctl, "tst-envalive")
    (rdir / "status.json").write_text(json.dumps({"state": "running"}))
    res = ctl.run("repair", "tst-envalive", "--env", "K=v", env={
        "STUB_DOCKER_CONTAINERS": "ralphd-tst-envalive",
        "STUB_DOCKER_RUNNING": "ralphd-tst-envalive",
    })
    assert res.returncode == 5, res.stderr
    assert not (cdir / "env-wiring.json").exists()
    assert _events(rdir) == []
