"""`start`/`resume` publish the API on a port ralphctl probed on loopback; a
sibling container can take that port on the publish address before docker
binds it. A ralphctl-chosen port is retried; an explicit one never is."""

from __future__ import annotations

import subprocess

from ralphd.cli import main as cli

TAKEN = ("docker: Error response from daemon: failed to set up container "
         "networking: Bind for 172.17.0.1:%d failed: port is already allocated")


def _cp(rc, stdout="", stderr=""):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


def _script(monkeypatch, taken_ports, ports):
    calls: list[list[str]] = []
    it = iter(ports)
    monkeypatch.setattr(cli, "free_port", lambda: next(it))

    def fake_sh(cmd, **kw):
        calls.append(list(cmd))
        if cmd[1] == "rm":
            return _cp(0)
        port = int(cmd[cmd.index("-p") + 1].split(":")[1])
        if port in taken_ports:
            return _cp(125, stderr=TAKEN % port)
        return _cp(0, stdout="cid\n")

    monkeypatch.setattr(cli, "sh", fake_sh)
    return calls


def _cmd(port):
    return [cli.DOCKER, "run", "-d", "--name", "ralphd-x", "-p",
            f"172.17.0.1:{port}:7777", "img"]


def test_a_taken_chosen_port_is_replaced_and_the_half_created_container_removed(
        monkeypatch):
    calls = _script(monkeypatch, {100, 101}, [101, 102])
    res, port = cli.docker_run_publishing(_cmd(100), "ralphd-x", 100,
                                          "172.17.0.1", False)
    assert res.returncode == 0 and port == 102
    runs = [c for c in calls if c[1] == "run"]
    assert [c[c.index("-p") + 1] for c in runs] == [
        "172.17.0.1:100:7777", "172.17.0.1:101:7777", "172.17.0.1:102:7777"]
    assert [c for c in calls if c[1] == "rm"] == [
        [cli.DOCKER, "rm", "-f", "ralphd-x"]] * 2


def test_an_explicit_port_is_never_replaced(monkeypatch):
    calls = _script(monkeypatch, {100}, [101])
    res, port = cli.docker_run_publishing(_cmd(100), "ralphd-x", 100,
                                          "172.17.0.1", True)
    assert res.returncode != 0 and port == 100 and len(calls) == 1


def test_the_retry_is_bounded(monkeypatch):
    calls = _script(monkeypatch, set(range(100, 200)), list(range(101, 200)))
    res, _ = cli.docker_run_publishing(_cmd(100), "ralphd-x", 100,
                                       "172.17.0.1", False)
    assert res.returncode != 0
    assert len([c for c in calls if c[1] == "run"]) == cli.PORT_PUBLISH_ATTEMPTS


def test_another_docker_failure_is_not_retried(monkeypatch):
    calls: list = []
    monkeypatch.setattr(cli, "sh", lambda cmd, **kw: (
        calls.append(cmd), _cp(125, stderr="no such image"))[1])
    res, port = cli.docker_run_publishing(_cmd(100), "ralphd-x", 100,
                                          "172.17.0.1", False)
    assert res.returncode == 125 and port == 100 and len(calls) == 1
