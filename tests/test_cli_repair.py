"""Black-box tests for task 008: `ralphctl repair <run-id>` diagnosis mode --
validates status.json/tasks.json/host.json against their expected shapes,
refuses to touch a run whose container is currently running, and appends a
`type: repair` audit line to events.jsonl for every invocation.

Reuses the stub-docker recording harness + `_seed_run` helper from
test_cli_docker.py / test_cli_resume.py (no real container/engine needed).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

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
    assert sorted(doc["checked"]) == ["container", "host.json", "status.json",
                                      "tasks.json"]


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
    assert sorted(ev["checked"]) == ["container", "host.json", "status.json",
                                     "tasks.json"]
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


# --- task 021 (#8): the dangling-container condition ---------------------


def test_repair_reports_dangling_container_for_running_run(ctl: Ctl):
    """A run recorded non-terminal whose container no longer exists at all
    must stop being reported as 'no issues found' -- and the report must
    name the guarded fix."""
    rdir, _cdir = _seed_run(ctl, "tst-zombie")
    (rdir / "status.json").write_text(json.dumps(
        {"state": "running", "schemaVersion": 1}))
    (rdir / "tasks.json").write_text(json.dumps(
        {"version": 1, "tasks": [
            {"id": "001", "title": "do the thing", "status": "in-progress"},
        ]}))
    # no STUB_DOCKER_CONTAINERS -> `docker inspect` on ralphd-tst-zombie
    # exits nonzero, i.e. the container does not exist
    res = ctl.run("--json", "repair", "tst-zombie")
    assert res.returncode == 1, res.stderr
    doc = json.loads(res.stdout)
    assert doc["ok"] is False
    assert doc["dangling"] == {"runId": "tst-zombie",
                              "container": "ralphd-tst-zombie",
                              # task 021 (#31): which of the two dangling
                              # shapes this is -- nothing exists here
                              "liveness": "absent"}
    joined = "\n".join(doc["issues"])
    assert "ralphd-tst-zombie no longer exists" in joined
    assert "'running'" in joined
    # names the guarded fix (and the alternative)
    assert "repair tst-zombie --set-state aborted" in joined
    assert "resume tst-zombie" in joined
    # audit trail records the same finding
    repairs = [e for e in _events(rdir) if e.get("type") == "repair"]
    assert len(repairs) == 1
    assert repairs[0]["issueCount"] == len(doc["issues"])
    assert "container" in repairs[0]["checked"]


def test_repair_human_output_names_the_dangling_container(ctl: Ctl):
    rdir, _cdir = _seed_run(ctl, "tst-zombie-human")
    (rdir / "status.json").write_text(json.dumps({"state": "starting"}))
    res = ctl.run("repair", "tst-zombie-human")
    assert res.returncode == 1, res.stderr
    assert "issue(s) found" in res.stdout
    assert "ralphd-tst-zombie-human no longer exists" in res.stdout
    assert "--set-state aborted" in res.stdout


def test_repair_terminal_run_without_container_is_not_dangling(ctl: Ctl):
    """A finished run legitimately has no container -- no false positive."""
    rdir, _cdir = _seed_run(ctl, "tst-done")
    (rdir / "status.json").write_text(json.dumps(
        {"state": "succeeded", "schemaVersion": 1}))
    res = ctl.run("--json", "repair", "tst-done")
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    assert doc["ok"] is True
    assert doc["dangling"] is None
    assert doc["issues"] == []


def test_repair_running_run_with_exited_container_is_dangling(ctl: Ctl):
    """Task 021 (#31) retargeted this test: an exited-but-present container
    with a run dir recorded `running` IS the dangling condition (nothing is
    running for that run), and repair must diagnose it instead of reporting
    'no issues found'. The wording says it exited rather than claiming it
    vanished; the exhaustive per-surface coverage lives in
    tests/test_cli_exited_container_dangling.py."""
    rdir, _cdir = _seed_run(ctl, "tst-exited")
    (rdir / "status.json").write_text(json.dumps({"state": "running"}))
    res = ctl.run("--json", "repair", "tst-exited", env={
        "STUB_DOCKER_CONTAINERS": "ralphd-tst-exited",
        "STUB_DOCKER_RUNNING": "",
    })
    assert res.returncode == 1, res.stderr
    doc = json.loads(res.stdout)
    assert doc["dangling"] == {"runId": "tst-exited",
                               "container": "ralphd-tst-exited",
                               "liveness": "exited"}
    joined = "\n".join(doc["issues"])
    assert "ralphd-tst-exited exists but has exited" in joined
    assert "no longer exists" not in joined


def test_repair_still_refuses_dangling_check_on_live_container(ctl: Ctl):
    """A live container is never diagnosed at all (unchanged refusal)."""
    rdir, _cdir = _seed_run(ctl, "tst-zombie-alive")
    (rdir / "status.json").write_text(json.dumps({"state": "running"}))
    res = ctl.run("repair", "tst-zombie-alive", env={
        "STUB_DOCKER_CONTAINERS": "ralphd-tst-zombie-alive",
        "STUB_DOCKER_RUNNING": "ralphd-tst-zombie-alive",
    })
    assert res.returncode == 5, res.stderr
    assert "running" in res.stderr
    assert _events(rdir) == []


def test_repair_set_state_aborted_writes_vanished_container_reason(ctl: Ctl):
    rdir, _cdir = _seed_run(ctl, "tst-zombie-fix")
    (rdir / "status.json").write_text(json.dumps(
        {"state": "running", "schemaVersion": 1, "verdict": None}))
    res = ctl.run("--json", "repair", "tst-zombie-fix", "--set-state",
                  "aborted")
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    assert doc["new"] == "aborted"
    reason = doc["reason"]
    assert "ralphd-tst-zombie-fix no longer exists" in reason

    on_disk = json.loads((rdir / "status.json").read_text())
    assert on_disk["state"] == "aborted"
    assert on_disk["reason"] == reason
    assert on_disk["schemaVersion"] == 1  # other fields untouched

    repairs = [e for e in _events(rdir) if e.get("type") == "repair"]
    assert len(repairs) == 1
    assert repairs[0]["action"] == "set-state"
    assert repairs[0]["old"] == "running"
    assert repairs[0]["new"] == "aborted"
    assert repairs[0]["reason"] == reason


def test_repair_set_state_on_terminal_run_records_no_vanished_reason(ctl: Ctl):
    """Nothing vanished: a state flip on an already-terminal run must not
    invent a container-died reason."""
    rdir, _cdir = _seed_run(ctl, "tst-flip")
    (rdir / "status.json").write_text(json.dumps({"state": "failed"}))
    res = ctl.run("--json", "repair", "tst-flip", "--set-state", "aborted")
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    assert doc["reason"] is None
    on_disk = json.loads((rdir / "status.json").read_text())
    assert on_disk["state"] == "aborted"
    assert "reason" not in on_disk


def test_repair_dangling_check_has_one_implementation():
    """doctor and repair must share the condition (task 021's criteria):
    exactly one place computes 'recorded non-terminal, container gone'."""
    src = (Path(__file__).resolve().parents[1]
           / "src" / "ralphd" / "cli" / "main.py").read_text()
    assert src.count("def _dangling_run_entry(") == 1
    assert src.count("def _dangling_registry_entries(") == 1
    # the registry sweep delegates instead of re-deriving the condition
    sweep = src.split("def _dangling_registry_entries(")[1].split("\ndef ")[0]
    assert "_dangling_run_entry(" in sweep
    assert "_container_running(" not in sweep


# --- task 025 (#8): doctor's and repair's remedy tell ONE story ----------


def _ralphctl_commands(text: str) -> list[str]:
    """Every backticked `ralphctl ...` command in a CLI report, in order."""
    return re.findall(r"`(ralphctl [^`]+)`", text)


def test_doctor_and_repair_recommend_the_same_next_command(ctl: Ctl):
    """The dangling-container remedy must not point two ways: doctor's
    registry sweep and repair's per-run diagnosis, over the *same* fixture,
    recommend the same first next command (and the same alternative)."""
    rdir, _cdir = _seed_run(ctl, "tst-onestory")
    (rdir / "status.json").write_text(json.dumps(
        {"state": "running", "schemaVersion": 1}))

    repair = ctl.run("repair", "tst-onestory")
    assert repair.returncode == 1, repair.stderr
    doctor = ctl.run("doctor")
    assert "tst-onestory" in doctor.stdout, doctor.stdout

    repair_cmds = _ralphctl_commands(repair.stdout)
    doctor_cmds = _ralphctl_commands(
        # only the dangling block, so unrelated doctor advice can't leak in
        # (task 021/#31 reworded the header: the condition is "no *live*
        # container", which now also covers an exited one)
        doctor.stdout.split("no live container:")[1])
    assert repair_cmds, repair.stdout
    assert doctor_cmds, doctor.stdout
    # same first recommendation, naming this run (never a `<run-id>` stub)
    assert repair_cmds[0] == doctor_cmds[0] == "ralphctl resume tst-onestory"
    # and the same alternative, in the same order
    assert repair_cmds == doctor_cmds


def test_dangling_remedy_text_has_one_implementation():
    """One story = one string: doctor and repair both call the helper
    rather than each spelling out its own advice (task 025)."""
    src = (Path(__file__).resolve().parents[1]
           / "src" / "ralphd" / "cli" / "main.py").read_text()
    assert src.count("def _dangling_remedy(") == 1
    assert src.count("_dangling_remedy(") == 3  # def + doctor + repair
