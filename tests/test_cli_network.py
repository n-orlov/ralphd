"""Black-box tests for `ralphctl start/resume --network` (host-network jobs).

Reuses the stub-docker recording harness from test_cli_docker.py: no real
container, assertions are on the recorded docker argv and host.json.
"""

from __future__ import annotations

import json

from test_cli_docker import Ctl, ctl

__all__ = ["ctl"]


def _run_argv(c: Ctl) -> list[str]:
    runs = [a for a in c.recorded() if a[:2] == ["run", "-d"]]
    assert len(runs) == 1, f"expected one docker run, got: {c.recorded()}"
    return runs[0]


def _env_vars(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]


def test_default_network_publishes_port(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--run-id", "r1")
    assert res.returncode == 0, res.stderr
    argv = _run_argv(ctl)
    assert "--network" not in argv
    assert "-p" in argv
    pub = argv[argv.index("-p") + 1]
    assert pub.startswith("127.0.0.1:") and pub.endswith(":7777")
    envs = _env_vars(argv)
    assert not any(e.startswith("RALPHD_PORT=") for e in envs)
    assert not any(e.startswith("RALPHD_BIND=") for e in envs)


def test_network_host_skips_publish_and_sets_bind_env(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--run-id", "r2",
                  "--network", "host", "--port", "38222")
    assert res.returncode == 0, res.stderr
    argv = _run_argv(ctl)
    assert "-p" not in argv
    i = argv.index("--network")
    assert argv[i + 1] == "host"
    envs = _env_vars(argv)
    assert "RALPHD_PORT=38222" in envs
    assert "RALPHD_BIND=127.0.0.1" in envs
    meta = json.loads((ctl.registry / "runs" / "r2" / "host.json").read_text())
    assert meta["network"] == "host"
    assert meta["port"] == 38222
    assert meta["apiUrl"] == "http://127.0.0.1:38222"


def test_network_host_honors_api_bind(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--run-id", "r3",
                  "--network", "host", "--port", "38223",
                  "--api-bind", "0.0.0.0")
    assert res.returncode == 0, res.stderr
    envs = _env_vars(_run_argv(ctl))
    assert "RALPHD_BIND=0.0.0.0" in envs


def test_named_network_keeps_port_publish(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--run-id", "r4",
                  "--network", "mynet")
    assert res.returncode == 0, res.stderr
    argv = _run_argv(ctl)
    i = argv.index("--network")
    assert argv[i + 1] == "mynet"
    assert "-p" in argv
    envs = _env_vars(argv)
    assert not any(e.startswith("RALPHD_BIND=") for e in envs)


def test_resume_reuses_recorded_network(ctl):
    rdir = ctl.registry / "runs" / "r5"
    cdir = ctl.registry / "configs" / "r5"
    rdir.mkdir(parents=True)
    cdir.mkdir(parents=True)
    (cdir / "job.yaml").write_text('run_id: "r5"\niterations: 5\n')
    (rdir / "host.json").write_text(json.dumps(
        {"runId": "r5", "container": "f" * 12, "port": 1234,
         "apiUrl": "http://127.0.0.1:1234", "image": "ralphd:dev",
         "network": "host", "startedAt": "2024-01-01T00:00:00Z"}))
    (rdir / "status.json").write_text(json.dumps({"state": "failed"}))
    res = ctl.run("resume", "r5", "--port", "38224")
    assert res.returncode == 0, res.stderr
    argv = _run_argv(ctl)
    assert "-p" not in argv
    i = argv.index("--network")
    assert argv[i + 1] == "host"
    envs = _env_vars(argv)
    assert "RALPHD_PORT=38224" in envs
    meta = json.loads((rdir / "host.json").read_text())
    assert meta["network"] == "host"
