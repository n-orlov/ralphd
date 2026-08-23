"""Task 019 (#24): `ralphctl tasks <run> --approach N|--all` reads an
archived approach's OWN `tasks.json` (`approaches/NN/tasks.json`, written by
`LoopSupervisor._archive_approach()` in `engine/loop.py`) instead of the
live plan.

Coverage:
  * `--approach N` prints (and, with `--json`, carries) that approach's own
    task table -- proven distinct from the live plan by seeding the two
    with different task lists.
  * `--all` lists every archived approach with counts taken from ITS OWN
    archived file, never recomputed from the live plan.
  * a never-archived approach is a clean, non-zero-exit error with no
    traceback.
  * `--all` with no archived approaches at all is a clean, zero-exit notice.

Black-box tier: the real `ralphctl` executable over on-disk run-dir
fixtures via the stub-docker `Ctl` harness (no container, no engine) --
`_seed_run` records a `host.json` pointing at a port nothing listens on, so
`ralphctl` falls back to reading the run dir directly, exactly like a dead
run (the same fallback `test_tasks_stale_cli.py` exercises).
"""

from __future__ import annotations

import json

from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run

__all__ = ["ctl", "unix_sock"]


LIVE_PLAN = {
    "version": 1,
    "goal": "live plan",
    "tasks": [
        {"id": "101", "title": "live one", "status": "completed"},
        {"id": "102", "title": "live two", "status": "pending"},
    ],
}

APPROACH_1_PLAN = {
    "version": 1,
    "goal": "approach 1 plan",
    "tasks": [
        {"id": "001", "title": "archived one", "status": "completed"},
        {"id": "002", "title": "archived two", "status": "completed"},
        {"id": "003", "title": "archived three", "status": "failed"},
    ],
}

APPROACH_2_PLAN = {
    "version": 1,
    "goal": "approach 2 plan",
    "tasks": [
        {"id": "001", "title": "second attempt one", "status": "completed"},
        {"id": "002", "title": "second attempt two", "status": "pending"},
    ],
}


def _seed(ctl: Ctl, run_id: str, *, live_plan: dict | None = LIVE_PLAN,
          archives: dict[int, dict] | None = None):
    rdir, _cdir = _seed_run(ctl, run_id)
    if live_plan is not None:
        (rdir / "tasks.json").write_text(json.dumps(live_plan))
    for n, plan in (archives or {}).items():
        adir = rdir / "approaches" / f"{n:02d}"
        adir.mkdir(parents=True)
        (adir / "tasks.json").write_text(json.dumps(plan))
    return rdir


def _task_ids(stdout: str) -> list[str]:
    return [ln.split("] ", 1)[1].split(" ", 1)[0]
            for ln in stdout.splitlines() if ln.startswith("[")]


# --------------------------------------------------------------------------
# --approach N
# --------------------------------------------------------------------------

def test_tasks_approach_prints_that_approachs_own_table(ctl: Ctl):
    _seed(ctl, "tst-app1", archives={1: APPROACH_1_PLAN, 2: APPROACH_2_PLAN})
    res = ctl.run("tasks", "tst-app1", "--approach", "1")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert _task_ids(res.stdout) == ["001", "002", "003"]
    # Not the live plan's ids, and not approach 2's either.
    assert "101" not in res.stdout and "102" not in res.stdout


def test_tasks_approach_2_differs_from_approach_1(ctl: Ctl):
    _seed(ctl, "tst-app2", archives={1: APPROACH_1_PLAN, 2: APPROACH_2_PLAN})
    res = ctl.run("tasks", "tst-app2", "--approach", "2")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert _task_ids(res.stdout) == ["001", "002"]
    assert "second attempt one" in res.stdout
    assert "archived one" not in res.stdout


def test_tasks_approach_json_carries_the_archived_structure(ctl: Ctl):
    _seed(ctl, "tst-app-json", archives={1: APPROACH_1_PLAN})
    res = ctl.run("--json", "tasks", "tst-app-json", "--approach", "1")
    assert res.returncode == 0, (res.stdout, res.stderr)
    doc = json.loads(res.stdout)
    assert doc["approach"] == 1
    assert [t["id"] for t in doc["tasks"]] == ["001", "002", "003"]
    assert doc["goal"] == "approach 1 plan"


def test_tasks_approach_counts_come_from_the_archived_file_not_live(ctl: Ctl):
    """approach 1 has 2/3 completed; the live plan (1/2 completed) must not
    leak into the archived read at all."""
    _seed(ctl, "tst-app-counts", archives={1: APPROACH_1_PLAN})
    res = ctl.run("tasks", "tst-app-counts", "--approach", "1")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "2/3 completed" in res.stdout


def test_tasks_never_archived_approach_is_a_clean_error(ctl: Ctl):
    _seed(ctl, "tst-app-missing", archives={1: APPROACH_1_PLAN})
    res = ctl.run("tasks", "tst-app-missing", "--approach", "5")
    assert res.returncode != 0
    assert "Traceback" not in res.stderr
    assert "5" in res.stderr
    assert res.stdout == ""


def test_tasks_approach_unknown_run_exits_3(ctl: Ctl):
    res = ctl.run("tasks", "no-such-run", "--approach", "1")
    assert res.returncode == 3, res.stderr
    assert "Traceback" not in res.stderr
    assert "not found" in res.stderr


# --------------------------------------------------------------------------
# --all
# --------------------------------------------------------------------------

def test_tasks_all_lists_every_archived_approach_with_its_own_counts(ctl: Ctl):
    _seed(ctl, "tst-all", archives={1: APPROACH_1_PLAN, 2: APPROACH_2_PLAN})
    res = ctl.run("tasks", "tst-all", "--all")
    assert res.returncode == 0, (res.stdout, res.stderr)
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    assert any(ln.startswith("approach 01:") and "2/3 completed" in ln
               for ln in lines), lines
    assert any(ln.startswith("approach 02:") and "1/2 completed" in ln
               for ln in lines), lines


def test_tasks_all_json_carries_per_approach_counts(ctl: Ctl):
    _seed(ctl, "tst-all-json", archives={1: APPROACH_1_PLAN, 2: APPROACH_2_PLAN})
    res = ctl.run("--json", "tasks", "tst-all-json", "--all")
    assert res.returncode == 0, (res.stdout, res.stderr)
    doc = json.loads(res.stdout)
    by_approach = {row["approach"]: row for row in doc["approaches"]}
    assert by_approach[1]["counts"]["completed"] == 2
    assert by_approach[1]["counts"]["total"] == 3
    assert by_approach[2]["counts"]["completed"] == 1
    assert by_approach[2]["counts"]["total"] == 2


def test_tasks_all_with_no_archives_is_a_clean_notice_not_an_error(ctl: Ctl):
    _seed(ctl, "tst-all-empty", archives=None)
    res = ctl.run("tasks", "tst-all-empty", "--all")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert res.stdout == ""
    assert "no archived approaches" in res.stderr


def test_tasks_all_unknown_run_exits_3(ctl: Ctl):
    res = ctl.run("tasks", "no-such-run", "--all")
    assert res.returncode == 3, res.stderr
    assert "not found" in res.stderr


def test_tasks_approach_and_all_are_mutually_exclusive(ctl: Ctl):
    _seed(ctl, "tst-mutex", archives={1: APPROACH_1_PLAN})
    res = ctl.run("tasks", "tst-mutex", "--approach", "1", "--all")
    assert res.returncode != 0
    assert "Traceback" not in res.stderr
