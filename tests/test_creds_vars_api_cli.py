"""`kind` (`env` or `vars`) on `PUT`/`DELETE /config/creds/{name}` and on
`ralphctl creds` (#314, PRD F.1).

A `vars` entry is a non-secret `<name>.vars` file: the API writes it to and
removes it from `~/.creds/<name>.vars`, and `ralphctl creds` does the same
through `--kind vars` (or a `.vars` file's own suffix). Every node here
depends on the kind support: none of them can pass against an engine or CLI
that only knows `.env`.
"""

from __future__ import annotations

import json
import os
import stat
import urllib.error
import urllib.request

import pytest

from tests.conftest import call_ralphctl_in_process

VARS_BODY = "SERVER_URL=https://ci.example.test\nBUILD_USER=robot\n"
ENV_BODY = "SERVER_TOKEN=tok-env-not-the-vars-file\n"


def _call(port, method, path, body=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method, data=body,
        headers={"Content-Type": "text/plain"} if body is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.fixture
def vrun(tmp_path, live, capsys):
    home = tmp_path / "home"
    home.mkdir()
    r = live(run_id="creds-vars-kind", job={"iterations": 8, "vigilant": False},
             stub_env={"HOME": str(home)})
    r.wait_api()
    r.home = home

    def ralphctl(*argv):
        env = {**os.environ, "RALPHD_REGISTRY": str(r.registry)}
        return call_ralphctl_in_process(list(argv), env=env, capsys=capsys)
    r.ralphctl = ralphctl
    return r


def test_api_put_vars_writes_a_vars_file(vrun):
    st, _ = _call(vrun.port, "PUT", "/config/creds/ci?kind=vars", VARS_BODY.encode())
    assert st == 204
    placed = vrun.home / ".creds" / "ci.vars"
    assert placed.read_text() == VARS_BODY
    assert stat.S_IMODE(placed.stat().st_mode) == 0o600
    assert not (vrun.home / ".creds" / "ci.env").exists()


def test_api_vars_and_env_of_one_name_coexist(vrun):
    assert _call(vrun.port, "PUT", "/config/creds/ci", ENV_BODY.encode())[0] == 204
    assert _call(vrun.port, "PUT", "/config/creds/ci?kind=vars",
                 VARS_BODY.encode())[0] == 204
    assert (vrun.home / ".creds" / "ci.env").read_text() == ENV_BODY
    assert (vrun.home / ".creds" / "ci.vars").read_text() == VARS_BODY
    # reading back, listing and deleting are per kind
    assert _call(vrun.port, "GET", "/config/creds/ci?kind=vars")[1].decode() == VARS_BODY
    assert _call(vrun.port, "GET", "/config/creds/ci")[1].decode() == ENV_BODY
    rows = json.loads(_call(vrun.port, "GET", "/config/creds?kind=vars")[1])
    assert [r["name"] for r in rows] == ["ci"]
    assert _call(vrun.port, "DELETE", "/config/creds/ci?kind=vars")[0] == 204
    assert not (vrun.home / ".creds" / "ci.vars").exists()
    assert (vrun.home / ".creds" / "ci.env").read_text() == ENV_BODY


def test_api_delete_vars_removes_the_file_then_404s(vrun):
    _call(vrun.port, "PUT", "/config/creds/ci?kind=vars", VARS_BODY.encode())
    placed = vrun.home / ".creds" / "ci.vars"
    assert placed.is_file()
    assert _call(vrun.port, "DELETE", "/config/creds/ci?kind=vars")[0] == 204
    assert not placed.exists()
    assert _call(vrun.port, "DELETE", "/config/creds/ci?kind=vars")[0] == 404
    assert _call(vrun.port, "GET", "/config/creds/ci?kind=vars")[0] == 404
    # a re-add after the delete is not suppressed by the tombstone
    _call(vrun.port, "PUT", "/config/creds/ci?kind=vars", VARS_BODY.encode())
    assert placed.read_text() == VARS_BODY


def test_api_delete_of_a_mounted_vars_entry_tombstones_it(vrun):
    cdir = vrun.config_dir / "creds"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "ci.vars").write_text(VARS_BODY)
    # any PUT re-runs placement, which picks the mounted entry up
    _call(vrun.port, "PUT", "/config/creds/other?kind=vars", b"A=1\n")
    placed = vrun.home / ".creds" / "ci.vars"
    assert placed.read_text() == VARS_BODY
    assert _call(vrun.port, "DELETE", "/config/creds/ci?kind=vars")[0] == 204
    assert not placed.exists()
    assert _call(vrun.port, "GET", "/config/creds/ci?kind=vars")[0] == 404


def test_api_rejects_an_unknown_kind(vrun):
    st, _ = _call(vrun.port, "PUT", "/config/creds/ci?kind=toml", b"A=1\n")
    assert st == 422
    assert not (vrun.home / ".creds").exists() or not list(
        (vrun.home / ".creds").glob("ci.*"))
    assert _call(vrun.port, "DELETE", "/config/creds/ci?kind=toml")[0] == 422


def test_cli_add_vars_file_by_suffix_then_rm(vrun, tmp_path):
    f = tmp_path / "ci.vars"
    f.write_text(VARS_BODY)
    res = vrun.ralphctl("creds", vrun.run_id, "add", str(f))
    assert res.returncode == 0, res.stderr
    assert (vrun.home / ".creds" / "ci.vars").read_text() == VARS_BODY
    got = vrun.ralphctl("creds", vrun.run_id, "get", "ci", "--kind", "vars")
    assert got.returncode == 0, got.stderr
    assert got.stdout == VARS_BODY
    ls = json.loads(vrun.ralphctl("--json", "creds", vrun.run_id, "ls",
                                  "--kind", "vars").stdout)
    assert [r["name"] for r in ls] == ["ci"]
    res = vrun.ralphctl("creds", vrun.run_id, "rm", "ci", "--kind", "vars")
    assert res.returncode == 0, res.stderr
    assert not (vrun.home / ".creds" / "ci.vars").exists()
    res = vrun.ralphctl("creds", vrun.run_id, "rm", "ci", "--kind", "vars")
    assert res.returncode != 0


def test_cli_add_with_kind_vars_flag(vrun, tmp_path):
    f = tmp_path / "site.vars"
    f.write_text(VARS_BODY)
    res = vrun.ralphctl("--json", "creds", vrun.run_id, "add", str(f),
                        "--kind", "vars")
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == {"added": "site", "kind": "vars"}
    res = vrun.ralphctl("--json", "creds", vrun.run_id, "rm", "site",
                        "--kind", "vars")
    assert json.loads(res.stdout) == {"removed": "site", "kind": "vars"}


def test_cli_add_kind_must_match_the_file_suffix(vrun, tmp_path):
    f = tmp_path / "ci.env"
    f.write_text(ENV_BODY)
    res = vrun.ralphctl("creds", vrun.run_id, "add", str(f), "--kind", "vars")
    assert res.returncode == 2
    assert "does not match ci.env" in res.stderr
    assert not (vrun.home / ".creds" / "ci.vars").exists()
