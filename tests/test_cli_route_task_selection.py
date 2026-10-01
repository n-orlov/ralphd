"""Task 003 (#278 B.5): `ralphctl start`'s task-selection routing default
and its `--no-route-tasks` opt-out, plus `ralphctl resume`'s replay of
whichever value a run started with.

Reuses `tests/test_cli_templates.py`'s `Ctl`/`_job_yaml` shape (real
`ralphctl` subprocess-equivalent, stub recording docker, temp registry).
"""

from __future__ import annotations

import json

import yaml
from test_cli_docker import Ctl, ctl, unix_sock  # noqa: F401

__all__ = ["ctl", "unix_sock"]


def _job_yaml(c: Ctl, run_id: str) -> dict:
    matches = list(c.tmp.glob(f"**/configs/{run_id}/job.yaml"))
    assert len(matches) == 1, f"expected one job.yaml for {run_id}, found {matches}"
    return yaml.safe_load(matches[0].read_text())


# ---------------------------------------------------------------------------
# Criterion 1: routing is on by default, off with --no-route-tasks, and a
# resume replays whichever value the run started with.
# ---------------------------------------------------------------------------

def test_route_task_selection_defaults_true_with_neither_opt_out(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "rts-default")
    assert res.returncode == 0, res.stderr
    job = _job_yaml(ctl, "rts-default")
    assert job["route_task_selection"] is True


def test_a_job_yaml_route_task_selection_false_key_disables_it(ctl):
    tdir = ctl.registry / "templates" / "rts-off"
    tdir.mkdir(parents=True)
    (tdir / "job.yaml").write_text("route_task_selection: false\n")
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "rts-tpl-off", "--template", "rts-off")
    assert res.returncode == 0, res.stderr
    job = _job_yaml(ctl, "rts-tpl-off")
    assert job["route_task_selection"] is False


def test_no_route_tasks_flag_disables_it_and_wins_over_a_template(ctl):
    tdir = ctl.registry / "templates" / "rts-on"
    tdir.mkdir(parents=True)
    (tdir / "job.yaml").write_text("route_task_selection: true\n")
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "rts-flag-wins", "--template", "rts-on",
                  "--no-route-tasks")
    assert res.returncode == 0, res.stderr
    job = _job_yaml(ctl, "rts-flag-wins")
    assert job["route_task_selection"] is False


def test_resume_keeps_no_route_tasks_off_with_no_flag_of_its_own(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "rts-resume-off", "--no-route-tasks")
    assert res.returncode == 0, res.stderr
    assert _job_yaml(ctl, "rts-resume-off")["route_task_selection"] is False

    res = ctl.run("resume", "rts-resume-off")
    assert res.returncode == 0, res.stderr
    assert _job_yaml(ctl, "rts-resume-off")["route_task_selection"] is False


def test_resume_keeps_the_default_on_with_neither_opt_out(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "rts-resume-on")
    assert res.returncode == 0, res.stderr
    assert _job_yaml(ctl, "rts-resume-on")["route_task_selection"] is True

    res = ctl.run("resume", "rts-resume-on")
    assert res.returncode == 0, res.stderr
    assert _job_yaml(ctl, "rts-resume-on")["route_task_selection"] is True
