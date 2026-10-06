"""Issue #297: `--allow-docker` is a property of the RUN, not of one container
launch -- it survives a bare `resume`, a `doctor --fix` auto-resume and every
other container restart, `resume --no-allow-docker` drops it for good, and a
run started without it never gains it.

Driven through the same stub-docker recording harness as
test_cli_docker.py/test_cli_resume.py: the recorded `run -d` argv is the
stub's equivalent of inspecting the resumed container's mounts and env (the
real-daemon equivalent lives in test_cli_docker_integration.py).
"""

from __future__ import annotations

import json
import os

from test_cli_docker import STUB_DOCKER, Ctl, ctl, unix_sock
from test_cli_exited_container_dangling import _exited_env
from test_cli_resume import _seed_run

from ralphd.cli import ui_server
from ralphd.cli.ui_server import run_detail
from ralphd.engine import state

__all__ = ["ctl", "unix_sock"]

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "ralphd", "cli")


def _runs(c: Ctl) -> list[list[str]]:
    return [a for a in c.recorded() if a[:2] == ["run", "-d"]]


def _env(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]


def _has_docker(argv: list[str]) -> bool:
    return (any(v.endswith(":/var/run/docker.sock") for v in argv)
            or "--group-add" in argv
            or any(v.startswith(("RALPHD_HOST_", "RALPHD_RUN_ID="))
                   for v in _env(argv)))


def _record(c: Ctl, run_id: str) -> dict | None:
    p = c.registry / "configs" / run_id / "docker-access.json"
    return json.loads(p.read_text()) if p.exists() else None


def _start(c: Ctl, run_id: str, *extra: str, env: dict | None = None):
    res = c.run("start", "--prd", str(c.prd), "--llm", "none",
                "--run-id", run_id, *extra, env=env)
    assert res.returncode == 0, res.stderr
    return res


# ----------------------------------------------------------- the reader
def test_state_reader_absent_malformed_and_string_bool(tmp_path):
    assert state.read_docker_access(tmp_path) is None
    f = tmp_path / state.DOCKER_ACCESS_FILE
    f.write_text("{not json")
    assert state.read_docker_access(tmp_path) is None
    f.write_text("[true]")
    assert state.read_docker_access(tmp_path) is None
    f.write_text(json.dumps({"allow_docker": "false", "setBy": "start"}))
    assert state.read_docker_access(tmp_path)["allow_docker"] is False
    f.write_text(json.dumps({"allow_docker": "true", "setBy": "start"}))
    assert state.read_docker_access(tmp_path)["allow_docker"] is True


def test_state_formatter_wording():
    fmt = state.format_docker_access
    view = state.docker_access_view
    assert fmt(view(None)).startswith("not recorded")
    assert view(None) == {"enabled": None, "setBy": None, "at": None}
    assert "ROOT-EQUIVALENT" in fmt(view({"allow_docker": True, "setBy": "start"}))
    assert fmt(view({"allow_docker": False, "setBy": "start"})) == "off"
    assert "--no-allow-docker" in fmt(view({"allow_docker": False, "setBy": "resume"}))


def test_state_unreadable_record_is_not_absent(tmp_path):
    assert state.docker_access_unreadable(tmp_path) is False
    f = tmp_path / state.DOCKER_ACCESS_FILE
    f.write_text('{"allow_docker": tr')
    assert state.docker_access_unreadable(tmp_path) is True
    f.write_text(json.dumps({"setBy": "start"}))
    assert state.docker_access_unreadable(tmp_path) is True
    f.write_text(json.dumps({"allow_docker": False, "setBy": "start"}))
    assert state.docker_access_unreadable(tmp_path) is False
    view = state.docker_access_view(None, unreadable=True)
    assert view["enabled"] is None and view["unreadable"] is True
    line = state.format_docker_access(view)
    assert line.startswith("unreadable") and "predates" not in line


# ------------------------------------------- acceptance 1: bare resume keeps it
def test_start_records_docker_access(ctl, unix_sock):
    sock_env = {"RALPHD_DOCKER_SOCK": str(unix_sock)}
    _start(ctl, "tst-rec-on", "--allow-docker", env=sock_env)
    _start(ctl, "tst-rec-off")
    on, off = _record(ctl, "tst-rec-on"), _record(ctl, "tst-rec-off")
    assert on["allow_docker"] is True and on["setBy"] == "start"
    assert off["allow_docker"] is False and off["setBy"] == "start"
    # host-side config dir, never the agent-writable run dir
    assert not (ctl.registry / "runs" / "tst-rec-on" / "docker-access.json").exists()


def test_bare_resume_replays_recorded_docker_access(ctl, tmp_path, unix_sock):
    sock_env = {"RALPHD_DOCKER_SOCK": str(unix_sock)}
    ws = tmp_path / "ws"
    ws.mkdir()
    _start(ctl, "tst-replay", "--allow-docker", "--workspace", str(ws), env=sock_env)
    res = ctl.run("resume", "tst-replay", env=sock_env)
    assert res.returncode == 0, res.stderr
    runs = _runs(ctl)
    assert len(runs) == 2
    argv = runs[1]
    assert f"{unix_sock}:/var/run/docker.sock" in argv
    assert argv[argv.index("--group-add") + 1] == str(os.stat(unix_sock).st_gid)
    ev = _env(argv)
    assert f"RALPHD_HOST_WORKSPACE={ws}" in ev
    assert f"RALPHD_HOST_RUN_DIR={ctl.registry / 'runs' / 'tst-replay'}" in ev
    assert "RALPHD_RUN_ID=tst-replay" in ev
    assert "ROOT-EQUIVALENT" in res.stderr
    assert "docker access: on (recorded" in res.stderr
    # a bare resume re-records nothing
    assert _record(ctl, "tst-replay")["setBy"] == "start"


def test_bare_resume_replays_multi_workspace_host_env(ctl, tmp_path, unix_sock):
    sock_env = {"RALPHD_DOCKER_SOCK": str(unix_sock)}
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _start(ctl, "tst-replay-multi", "--allow-docker",
           "--workspace", f"{a}:a", "--workspace", f"{b}:b", env=sock_env)
    res = ctl.run("resume", "tst-replay-multi", env=sock_env)
    assert res.returncode == 0, res.stderr
    ev = _env(_runs(ctl)[1])
    hostwss = [v for v in ev if v.startswith("RALPHD_HOST_WORKSPACES=")]
    assert len(hostwss) == 1
    assert json.loads(hostwss[0].split("=", 1)[1]) == {"a": str(a), "b": str(b)}


# ------------------------------------------- acceptance 2: auto-resume keeps it
def test_auto_resume_keeps_docker_access(ctl, unix_sock):
    sock_env = {"RALPHD_DOCKER_SOCK": str(unix_sock)}
    _start(ctl, "tst-auto-dock", "--allow-docker", "--auto-resume", env=sock_env)
    (ctl.registry / "runs" / "tst-auto-dock" / "status.json").write_text(
        json.dumps({"state": "running", "schemaVersion": 1, "iterationsUsed": 3}))
    env = {"STUB_DOCKER_INSPECT_OK": "1", **_exited_env("tst-auto-dock"), **sock_env}
    res = ctl.run("--json", "doctor", "--fix", env=env)
    doc = json.loads(res.stdout)
    assert doc["autoResume"]["resumed"] == ["tst-auto-dock"], doc["autoResume"]
    runs = _runs(ctl)
    assert len(runs) == 2
    assert f"{unix_sock}:/var/run/docker.sock" in runs[1]
    assert "RALPHD_RUN_ID=tst-auto-dock" in _env(runs[1])
    # the captured warning is still shown to the operator
    assert "ROOT-EQUIVALENT" in res.stderr


def test_auto_resume_missing_socket_fails_with_reason(ctl, tmp_path, unix_sock):
    _start(ctl, "tst-auto-nosock", "--allow-docker", "--auto-resume",
           env={"RALPHD_DOCKER_SOCK": str(unix_sock)})
    (ctl.registry / "runs" / "tst-auto-nosock" / "status.json").write_text(
        json.dumps({"state": "running", "schemaVersion": 1, "iterationsUsed": 3}))
    env = {"STUB_DOCKER_INSPECT_OK": "1", **_exited_env("tst-auto-nosock"),
           "RALPHD_DOCKER_SOCK": str(tmp_path / "gone.sock")}
    doc = json.loads(ctl.run("--json", "doctor", "--fix", env=env).stdout)
    failed = doc["autoResume"]["failed"]
    assert [f["runId"] for f in failed] == ["tst-auto-nosock"]
    assert "docker socket" in failed[0]["error"]
    assert "--no-allow-docker" in failed[0]["error"]
    assert failed[0]["error"].startswith("resume exited 2: ")
    assert len(_runs(ctl)) == 1


def _damage_record(c: Ctl, run_id: str) -> None:
    p = c.registry / "configs" / run_id / "docker-access.json"
    raw = p.read_bytes()
    p.write_bytes(raw[: len(raw) // 2])


def test_bare_resume_refuses_damaged_record(ctl, unix_sock):
    sock_env = {"RALPHD_DOCKER_SOCK": str(unix_sock)}
    _start(ctl, "tst-damaged", "--allow-docker", env=sock_env)
    _damage_record(ctl, "tst-damaged")
    res = ctl.run("resume", "tst-damaged", env=sock_env)
    assert res.returncode == 2, res.stderr
    assert "docker-access.json" in res.stderr
    assert "--allow-docker or --no-allow-docker" in res.stderr
    assert len(_runs(ctl)) == 1
    # status says the record is damaged, not that the run predates #297
    (ctl.registry / "runs" / "tst-damaged" / "status.json").write_text(
        json.dumps({"state": "failed", "schemaVersion": 1, "runId": "tst-damaged"}))
    da = json.loads(ctl.run("--json", "status", "tst-damaged").stdout)["dockerAccess"]
    assert da["enabled"] is None and da["unreadable"] is True
    assert da["line"].startswith("unreadable")
    assert run_detail(ctl.registry, "tst-damaged")["status"][
        "dockerAccessLine"].startswith("unreadable")
    # an explicit flag settles it and rewrites the record
    res = ctl.run("resume", "tst-damaged", "--allow-docker", env=sock_env)
    assert res.returncode == 0, res.stderr
    assert f"{unix_sock}:/var/run/docker.sock" in _runs(ctl)[1]
    assert _record(ctl, "tst-damaged")["allow_docker"] is True
    res = ctl.run("resume", "tst-damaged", env=sock_env)
    assert res.returncode == 0, res.stderr
    assert f"{unix_sock}:/var/run/docker.sock" in _runs(ctl)[2]


def test_auto_resume_damaged_record_fails_with_reason(ctl, unix_sock):
    sock_env = {"RALPHD_DOCKER_SOCK": str(unix_sock)}
    _start(ctl, "tst-auto-dmg", "--allow-docker", "--auto-resume", env=sock_env)
    _damage_record(ctl, "tst-auto-dmg")
    (ctl.registry / "runs" / "tst-auto-dmg" / "status.json").write_text(
        json.dumps({"state": "running", "schemaVersion": 1, "iterationsUsed": 3}))
    env = {"STUB_DOCKER_INSPECT_OK": "1", **_exited_env("tst-auto-dmg"), **sock_env}
    doc = json.loads(ctl.run("--json", "doctor", "--fix", env=env).stdout)
    failed = doc["autoResume"]["failed"]
    assert [f["runId"] for f in failed] == ["tst-auto-dmg"]
    assert failed[0]["error"].startswith("resume exited 2: ")
    assert "docker-access.json" in failed[0]["error"]
    assert len(_runs(ctl)) == 1


# ------------------------------- acceptance 3: --no-allow-docker drops it, sticky
def test_no_allow_docker_drops_and_stays_dropped(ctl, unix_sock):
    sock_env = {"RALPHD_DOCKER_SOCK": str(unix_sock)}
    _start(ctl, "tst-drop", "--allow-docker", env=sock_env)
    res = ctl.run("resume", "tst-drop", "--no-allow-docker", env=sock_env)
    assert res.returncode == 0, res.stderr
    assert not _has_docker(_runs(ctl)[1])
    assert "docker access dropped" in res.stderr
    assert "ROOT-EQUIVALENT" not in res.stderr
    rec = _record(ctl, "tst-drop")
    assert rec["allow_docker"] is False and rec["setBy"] == "resume"

    res = ctl.run("resume", "tst-drop", env=sock_env)
    assert res.returncode == 0, res.stderr
    assert not _has_docker(_runs(ctl)[2])
    assert "ROOT-EQUIVALENT" not in res.stderr


def test_resume_allow_and_no_allow_docker_are_mutually_exclusive(ctl, unix_sock):
    _seed_run(ctl, "tst-both")
    res = ctl.run("resume", "tst-both", "--allow-docker", "--no-allow-docker",
                  env={"RALPHD_DOCKER_SOCK": str(unix_sock)})
    assert res.returncode == 2
    # the group's own refusal, not "unrecognized arguments"
    assert "not allowed with argument" in res.stderr, res.stderr
    assert _runs(ctl) == []


def test_resume_allow_docker_re_enables_and_records(ctl, unix_sock):
    sock_env = {"RALPHD_DOCKER_SOCK": str(unix_sock)}
    _start(ctl, "tst-reon", "--allow-docker", env=sock_env)
    assert ctl.run("resume", "tst-reon", "--no-allow-docker",
                   env=sock_env).returncode == 0
    res = ctl.run("resume", "tst-reon", "--allow-docker", env=sock_env)
    assert res.returncode == 0, res.stderr
    assert f"{unix_sock}:/var/run/docker.sock" in _runs(ctl)[2]
    rec = _record(ctl, "tst-reon")
    assert rec["allow_docker"] is True and rec["setBy"] == "resume"
    res = ctl.run("resume", "tst-reon", env=sock_env)
    assert res.returncode == 0, res.stderr
    assert f"{unix_sock}:/var/run/docker.sock" in _runs(ctl)[3]


# ------------------------------- acceptance 4: never gained without the flag
def test_run_started_without_docker_never_gains_it(ctl, unix_sock):
    sock_env = {"RALPHD_DOCKER_SOCK": str(unix_sock)}
    _start(ctl, "tst-never", "--auto-resume", env=sock_env)
    for _ in range(2):
        res = ctl.run("resume", "tst-never", env=sock_env)
        assert res.returncode == 0, res.stderr
        assert "ROOT-EQUIVALENT" not in res.stderr
    (ctl.registry / "runs" / "tst-never" / "status.json").write_text(
        json.dumps({"state": "running", "schemaVersion": 1, "iterationsUsed": 3}))
    env = {"STUB_DOCKER_INSPECT_OK": "1", **_exited_env("tst-never"), **sock_env}
    doc = json.loads(ctl.run("--json", "doctor", "--fix", env=env).stdout)
    assert doc["autoResume"]["resumed"] == ["tst-never"]
    runs = _runs(ctl)
    assert len(runs) == 4
    assert not any(_has_docker(argv) for argv in runs)


def test_agent_written_host_json_cannot_grant_docker(ctl, unix_sock):
    """The run dir is bind-mounted read-write into the container; nothing the
    agent writes there may grant the root-equivalent socket on a resume."""
    sock_env = {"RALPHD_DOCKER_SOCK": str(unix_sock)}
    _start(ctl, "tst-forge", env=sock_env)
    rdir = ctl.registry / "runs" / "tst-forge"
    meta = json.loads((rdir / "host.json").read_text())
    meta.update({"allowDocker": True, "allow_docker": True})
    (rdir / "host.json").write_text(json.dumps(meta))
    (rdir / "docker-access.json").write_text(json.dumps({"allow_docker": True}))
    res = ctl.run("resume", "tst-forge", env=sock_env)
    assert res.returncode == 0, res.stderr
    assert not _has_docker(_runs(ctl)[1])


def test_legacy_run_without_record_behaves_as_before(ctl, unix_sock):
    _seed_run(ctl, "tst-legacy")
    res = ctl.run("resume", "tst-legacy", env={"RALPHD_DOCKER_SOCK": str(unix_sock)})
    assert res.returncode == 0, res.stderr
    assert not _has_docker(_runs(ctl)[0])
    assert _record(ctl, "tst-legacy") is None


def test_legacy_run_flag_is_recorded(ctl, unix_sock):
    _seed_run(ctl, "tst-legacy-flag")
    res = ctl.run("resume", "tst-legacy-flag", "--allow-docker",
                  env={"RALPHD_DOCKER_SOCK": str(unix_sock)})
    assert res.returncode == 0, res.stderr
    rec = _record(ctl, "tst-legacy-flag")
    assert rec["allow_docker"] is True and rec["setBy"] == "resume"


# ---------------------------------------------------- refusal and validation
def test_recorded_docker_missing_socket_refuses_before_mutating(ctl, tmp_path, unix_sock):
    _start(ctl, "tst-refuse", "--allow-docker",
           env={"RALPHD_DOCKER_SOCK": str(unix_sock)})
    job_yaml = ctl.registry / "configs" / "tst-refuse" / "job.yaml"
    before = job_yaml.read_text()
    n_before = len(ctl.recorded())
    res = ctl.run("resume", "tst-refuse", "--iterations", "+5",
                  env={"RALPHD_DOCKER_SOCK": str(tmp_path / "nonexistent"),
                       **_exited_env("tst-refuse")})
    assert res.returncode == 2
    assert "docker-access.json" in res.stderr
    assert "--no-allow-docker" in res.stderr
    later = ctl.recorded()[n_before:]
    assert not any(a[:2] == ["rm", "-f"] for a in later)
    assert len(_runs(ctl)) == 1
    assert job_yaml.read_text() == before
    assert _record(ctl, "tst-refuse")["allow_docker"] is True


def test_recorded_docker_non_socket_path_refuses(ctl, tmp_path, unix_sock):
    _start(ctl, "tst-refuse-plain", "--allow-docker",
           env={"RALPHD_DOCKER_SOCK": str(unix_sock)})
    plain = tmp_path / "plain"
    plain.write_text("x")
    res = ctl.run("resume", "tst-refuse-plain", env={"RALPHD_DOCKER_SOCK": str(plain)})
    assert res.returncode == 2
    assert "is not a socket" in res.stderr
    assert "--no-allow-docker" in res.stderr
    assert len(_runs(ctl)) == 1


def test_no_allow_docker_with_missing_socket_succeeds(ctl, tmp_path, unix_sock):
    _start(ctl, "tst-drop-nosock", "--allow-docker",
           env={"RALPHD_DOCKER_SOCK": str(unix_sock)})
    res = ctl.run("resume", "tst-drop-nosock", "--no-allow-docker",
                  env={"RALPHD_DOCKER_SOCK": str(tmp_path / "gone")})
    assert res.returncode == 0, res.stderr
    assert not _has_docker(_runs(ctl)[1])


def test_failed_docker_run_does_not_flip_record(ctl, tmp_path, unix_sock):
    sock_env = {"RALPHD_DOCKER_SOCK": str(unix_sock)}
    _start(ctl, "tst-fail-run", "--allow-docker", env=sock_env)
    failing = tmp_path / "docker-run-fails"
    failing.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = run ] && [ "$2" = -d ]; then echo boom >&2; exit 1; fi\n'
        f'exec {STUB_DOCKER} "$@"\n')
    failing.chmod(0o755)
    res = ctl.run("resume", "tst-fail-run", "--no-allow-docker",
                  env={**sock_env, "RALPHD_DOCKER": str(failing)})
    assert res.returncode == 1, res.stderr
    rec = _record(ctl, "tst-fail-run")
    assert rec["allow_docker"] is True and rec["setBy"] == "start"


# ------------------------------------------------------------ status and hub
def test_status_reports_docker_access(ctl, unix_sock):
    _start(ctl, "tst-st-on", "--allow-docker",
           env={"RALPHD_DOCKER_SOCK": str(unix_sock)})
    _start(ctl, "tst-st-off")
    _seed_run(ctl, "tst-st-legacy")
    expected = {"tst-st-on": True, "tst-st-off": False, "tst-st-legacy": None}
    for run_id, enabled in expected.items():
        (ctl.registry / "runs" / run_id / "status.json").write_text(
            json.dumps({"state": "failed", "schemaVersion": 1, "runId": run_id}))
        res = ctl.run("--json", "status", run_id)
        assert res.returncode == 0, res.stderr
        da = json.loads(res.stdout)["dockerAccess"]
        assert da["enabled"] is enabled
        assert da["line"] == state.format_docker_access(da)
        text = ctl.run("status", run_id).stdout
        assert f"docker:    {da['line']}" in text


def test_hub_run_detail_reports_docker_access(ctl, unix_sock):
    _start(ctl, "tst-hub-dock", "--allow-docker",
           env={"RALPHD_DOCKER_SOCK": str(unix_sock)})
    detail = run_detail(ctl.registry, "tst-hub-dock")
    assert detail["status"]["dockerAccess"]["enabled"] is True
    assert "ROOT-EQUIVALENT" in detail["status"]["dockerAccessLine"]


def test_hub_overwrites_forged_docker_access_in_live_payload(ctl, monkeypatch):
    _start(ctl, "tst-hub-forge")
    forged = {"state": "running", "dockerAccess": {"enabled": True},
              "dockerAccessLine": "on (forged)"}
    monkeypatch.setattr(ui_server, "_proxy_json",
                        lambda *a, **k: (True, 200, dict(forged)))
    detail = run_detail(ctl.registry, "tst-hub-forge")
    assert detail["status"]["dockerAccessLine"] == "off"
    assert detail["status"]["dockerAccess"]["enabled"] is False


def test_app_js_renders_docker_row():
    with open(os.path.join(SRC, "web", "app.js")) as f:
        src = f.read()
    assert 'rows.push(["docker", String(s.dockerAccessLine)])' in src
