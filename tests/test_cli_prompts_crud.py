"""Black-box tests for `ralphctl prompts <run-id> ls|set <phase> <file>`
(PRD req 12).

Drives a real, directly-launched `ralphd-engine` (the `live` fixture from
conftest.py) -- no engine internals imported -- but `ralphctl` itself now
runs in-process through `tests/conftest.py`'s `call_ralphctl_in_process`
(task 002, requirement F): only the CLI's stdout/exit code and the
`GET /config/prompts` JSON it prints back are asserted on, same as before,
just without spawning a second real process for every CLI call.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.conftest import call_ralphctl_in_process


def _in_process_ralphctl(run, capsys):
    def ralphctl(*argv: str):
        env = {**os.environ, "RALPHD_REGISTRY": str(run.registry)}
        return call_ralphctl_in_process(list(argv), env=env, capsys=capsys)
    run.ralphctl = ralphctl
    return run


@pytest.fixture
def prompts_run(tmp_path, live, capsys):
    home = tmp_path / "home"
    home.mkdir()
    r = live(run_id="prompts-cli", job={"iterations": 8, "vigilant": False},
             stub_env={"HOME": str(home)})
    r.wait_api()
    yield _in_process_ralphctl(r, capsys)


def _mounted_prompt(config_dir: Path, name: str, text: str) -> None:
    d = config_dir / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(text)


def test_prompts_ls_shows_every_phase_with_source(prompts_run):
    res = prompts_run.ralphctl("--json", "prompts", prompts_run.run_id, "ls")
    assert res.returncode == 0, res.stderr
    rows = json.loads(res.stdout)
    by_name = {r["name"]: r["source"] for r in rows}
    assert by_name == {
        "planning": "builtin",
        "worker": "builtin",
        "review": "builtin",
        "task-verify": "builtin",
    }


def test_prompts_ls_reflects_mounted_source(prompts_run):
    _mounted_prompt(prompts_run.config_dir, "review", "# Role: Review\n\nmounted.\n")
    res = prompts_run.ralphctl("--json", "prompts", prompts_run.run_id, "ls")
    rows = json.loads(res.stdout)
    by_name = {r["name"]: r["source"] for r in rows}
    assert by_name["review"] == "mounted"
    assert by_name["worker"] == "builtin"


def test_prompts_set_uploads_and_ls_reflects_api(prompts_run, tmp_path):
    f = tmp_path / "worker-override.md"
    f.write_text("# Role: Worker\n\nCLI-OVERRIDE-MARKER-xyz.\n")
    res = prompts_run.ralphctl("prompts", prompts_run.run_id, "set", "worker", str(f))
    assert res.returncode == 0, res.stderr

    ls = json.loads(prompts_run.ralphctl(
        "--json", "prompts", prompts_run.run_id, "ls").stdout)
    by_name = {r["name"]: r["source"] for r in ls}
    assert by_name["worker"] == "api"
    assert by_name["planning"] == "builtin"


def test_prompts_set_invalid_phase_exits_2(prompts_run, tmp_path):
    f = tmp_path / "whatever.md"
    f.write_text("irrelevant\n")
    res = prompts_run.ralphctl("prompts", prompts_run.run_id, "set", "not-a-phase", str(f))
    assert res.returncode == 2
    assert "planning" in res.stderr or "phase" in res.stderr

    ls = json.loads(prompts_run.ralphctl(
        "--json", "prompts", prompts_run.run_id, "ls").stdout)
    assert all(r["name"] != "not-a-phase" for r in ls)


def test_prompts_set_missing_file_exits_2(prompts_run, tmp_path):
    res = prompts_run.ralphctl("prompts", prompts_run.run_id, "set", "worker",
                               str(tmp_path / "no-such-file.md"))
    assert res.returncode == 2


def test_prompts_missing_run_exits_3(live, capsys):
    r = live(run_id="prompts-cli-exists-only")
    r = _in_process_ralphctl(r, capsys)
    res = r.ralphctl("prompts", "no-such-run-at-all", "ls")
    assert res.returncode == 3


def test_prompts_json_output_stable(prompts_run):
    res = prompts_run.ralphctl("--json", "prompts", prompts_run.run_id, "ls")
    assert res.returncode == 0
    rows = json.loads(res.stdout)
    assert isinstance(rows, list)
    for row in rows:
        assert set(row.keys()) >= {"name", "source"}
