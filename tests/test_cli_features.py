"""Feature module for the subject below (task 028, requirement D.7): the
per-task pin modules listed here are folded in whole -- every test body,
assertion and docstring verbatim, only the module they live in changed.
Sources absorbed (each was `tests/<name>`, deleted in the same commit, with
one `docs/test-suite-audit.md` § "Subsumed" row per node):

- `test_cli_budget_resume_94_flags_task004.py` -- Task 004 (#94 (c)+(d), operator-added scope): `--approaches`, `--timeout` and `--iteration-timeout` on both `ralphctl budget` and `ralphctl resume`, plus the resume-persistence decision for the three (process-only -- a plain resume REVERTS them, unlike `iterations`, which persists -- see engine/api.
- `test_cli_tasks_approach.py` -- Task 019 (#24): `ralphctl tasks <run> --approach N|--all` reads an archived approach's OWN `tasks.json` (`approaches/NN/tasks.json`, written by `LoopSupervisor._archive_approach()` in `engine/loop.py`) instead of the live plan. Coverage: * `--approach N` prints (and, with `--json`, carries) that app
- `test_inflight_budget_setters_task094.py` -- #94 (operator-added scope, steer 003 item 3): the in-flight budget knobs `ralphctl budget`/`PATCH /config/budget` do NOT yet cover -- `max_approaches`, `iteration_timeout_s`, and (as of task 002) the job wall-clock deadline. This module drives ONLY the engine-side setters (`LoopSupervisor.set_max_ap
- `test_cli_contract_violation_surfacing_task012.py` -- v0.13 task 012 (#92, requirement G's second half): black-box CLI proof that `ralphctl status`' `reason:` line and `ralphctl iteration N` both say a hat broke its contract and quote the validation error, for a run-dir fixture whose `events.jsonl` holds `contract.violation` (a hat that recovered) and 
- `test_cli_runs_tasks_column.py` -- Task 015 (#21): `ralphctl runs` gains the hub's TASKS column, from the same counts. Task 013/014 gave the hub run list a `5/7` TASKS column with trouble flags; the CLI list still showed state/verdict/phase/approach/iterations only, so an operator on a terminal had to open every run to learn how far 

Renamed to resolve cross-source collisions of module-private names (no test
function was renamed):
- `_seed` -> `_cbr9004_seed` (from `test_cli_budget_resume_94_flags_task004.py`)
- `_seed` -> `_ca_seed` (from `test_cli_tasks_approach.py`)
"""

from __future__ import annotations


# ----------------------------------------------------------------------
# Folded from tests/test_cli_budget_resume_94_flags_task004.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import json
from pathlib import Path

import pytest
from test_cli_budget import _seed as _cbr9004_seed, engine
from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run

__all__ = ["ctl", "unix_sock", "engine"]


# --------------------------------------------------------------------------
# `ralphctl budget`
# --------------------------------------------------------------------------

def test_budget_bare_positional_still_means_the_iteration_spec(ctl: Ctl, engine):
    """The exact regression this task's successCriteria pins: an existing
    `ralphctl budget <run> +5` invocation parses and reaches the engine
    exactly as it always did, unaccompanied by any of the three new flags."""
    eng = engine()
    _cbr9004_seed(ctl, "tst-94-bare", eng)
    res = ctl.run("budget", "tst-94-bare", "+5")
    assert res.returncode == 0, res.stderr
    assert eng.requests[-1]["body"] == {"iterations": "+5"}


def test_budget_approaches_flag_alone(ctl: Ctl, engine):
    eng = engine(body={"maxApproaches": 4, "previousMaxApproaches": 3})
    _cbr9004_seed(ctl, "tst-94-approaches", eng)
    res = ctl.run("budget", "tst-94-approaches", "--approaches", "+1")
    assert res.returncode == 0, res.stderr
    assert eng.requests[-1]["body"] == {"maxApproaches": "+1"}
    assert "3 -> 4" in res.stdout


def test_budget_timeout_flag_alone(ctl: Ctl, engine):
    eng = engine(body={"jobDeadlineEpochS": 999999, "previousJobDeadlineEpochS": 900000,
                       "deadlineAt": "2001-01-01T00:00:00Z"})
    _cbr9004_seed(ctl, "tst-94-timeout", eng)
    res = ctl.run("budget", "tst-94-timeout", "--timeout", "+3600")
    assert res.returncode == 0, res.stderr
    assert eng.requests[-1]["body"] == {"jobDeadlineEpochS": "+3600"}
    assert "900000 -> 999999" in res.stdout


def test_budget_iteration_timeout_flag_alone(ctl: Ctl, engine):
    eng = engine(body={"iterationTimeoutS": 900, "previousIterationTimeoutS": 600})
    _cbr9004_seed(ctl, "tst-94-itertimeout", eng)
    res = ctl.run("budget", "tst-94-itertimeout", "--iteration-timeout", "900")
    assert res.returncode == 0, res.stderr
    assert eng.requests[-1]["body"] == {"iterationTimeoutS": "900"}
    assert "600s -> 900s" in res.stdout


def test_budget_combines_positional_and_flags_in_one_request(ctl: Ctl, engine):
    eng = engine(body={"iterations": 30, "previous": 25, "iterationsUsed": 5,
                       "maxApproaches": 4, "previousMaxApproaches": 3})
    _cbr9004_seed(ctl, "tst-94-combo", eng)
    res = ctl.run("budget", "tst-94-combo", "+5", "--approaches", "+1")
    assert res.returncode == 0, res.stderr
    assert eng.requests[-1]["body"] == {"iterations": "+5", "maxApproaches": "+1"}


def test_budget_malformed_approaches_exits_2_without_calling_the_api(ctl: Ctl, engine):
    eng = engine()
    _cbr9004_seed(ctl, "tst-94-bad-approaches", eng)
    res = ctl.run("budget", "tst-94-bad-approaches", "--approaches", "one")
    assert res.returncode == 2, (res.returncode, res.stderr)
    assert "--approaches" in res.stderr and "invalid value" in res.stderr
    assert eng.requests == []


def test_budget_no_knob_at_all_exits_2(ctl: Ctl, engine):
    eng = engine()
    _cbr9004_seed(ctl, "tst-94-empty", eng)
    res = ctl.run("budget", "tst-94-empty")
    assert res.returncode == 2, (res.returncode, res.stderr)
    assert "nothing to change" in res.stderr
    assert eng.requests == []


def test_budget_flags_documented_in_cli_docs(ctl: Ctl):
    doc = (Path(__file__).resolve().parents[1] / "docs" / "cli.md").read_text()
    heading = "### `ralphctl budget <run-id> <+N|N>`"
    assert heading in doc
    section = doc.split(heading, 1)[1].split("\n### ", 1)[0]
    for flag in ("--approaches", "--timeout", "--iteration-timeout"):
        assert flag in section, f"{flag} not documented in the budget section"
    # the per-knob persistence table: 4 knob rows, one saying "live", three
    # saying "reverts"
    assert section.count("**live**") == 1
    assert section.count("**reverts**") == 3


# --------------------------------------------------------------------------
# `ralphctl resume`
# --------------------------------------------------------------------------

def _job_yaml_dict(cdir: Path) -> dict:
    text = (cdir / "job.yaml").read_text()
    return {k: json.loads(v) for k, v in
            (line.split(": ", 1) for line in text.splitlines() if line)}


def test_resume_approaches_topup_bumps_job_yaml(ctl):
    _rdir, cdir = _seed_run(ctl, "tst-94r-approaches")
    # _seed_run's job.yaml already carries max_approaches: 1
    res = ctl.run("resume", "tst-94r-approaches", "--approaches", "+2")
    assert res.returncode == 0, res.stderr
    assert _job_yaml_dict(cdir)["max_approaches"] == 3


def test_resume_timeout_topup_bumps_job_yaml(ctl):
    _rdir, cdir = _seed_run(ctl, "tst-94r-timeout")
    res = ctl.run("resume", "tst-94r-timeout", "--timeout", "3600")
    assert res.returncode == 0, res.stderr
    assert _job_yaml_dict(cdir)["job_timeout_s"] == 3600


def test_resume_iteration_timeout_topup_bumps_job_yaml(ctl):
    _rdir, cdir = _seed_run(ctl, "tst-94r-itertimeout")
    res = ctl.run("resume", "tst-94r-itertimeout", "--iteration-timeout", "+300")
    assert res.returncode == 0, res.stderr
    from ralphd.engine.config import JobConfig
    expected = JobConfig().iteration_timeout_s + 300
    assert _job_yaml_dict(cdir)["iteration_timeout_s"] == expected


def test_resume_all_three_together_next_to_iterations(ctl):
    _rdir, cdir = _seed_run(ctl, "tst-94r-combo", iterations=5)
    res = ctl.run("resume", "tst-94r-combo", "--iterations", "+10",
                 "--approaches", "+1", "--timeout", "+100",
                 "--iteration-timeout", "+50")
    assert res.returncode == 0, res.stderr
    job = _job_yaml_dict(cdir)
    assert job["iterations"] == 15
    assert job["max_approaches"] == 2  # seeded job.yaml had max_approaches: 1
    from ralphd.engine.config import JobConfig
    d = JobConfig()
    assert job["job_timeout_s"] == d.job_timeout_s + 100
    assert job["iteration_timeout_s"] == d.iteration_timeout_s + 50


def test_resume_bad_approaches_value_exits_2(ctl):
    _seed_run(ctl, "tst-94r-bad")
    res = ctl.run("resume", "tst-94r-bad", "--approaches", "banana")
    assert res.returncode == 2, res.stderr
    assert "--approaches" in res.stderr and "invalid value" in res.stderr
    assert not any(a[:2] == ["run", "-d"] for a in ctl.recorded())


def test_resume_with_no_new_flags_reverts_to_job_yamls_own_value(ctl):
    """The #94 (d) decision, proven at the CLI-black-box level: the three
    new knobs are process-only. This harness never HAS a live process to
    revert away from (there is no engine running against `_seed_run`'s run
    dir) -- which is exactly the point of the decision: a plain resume
    passes the three knobs from job.yaml's own recorded values, never from
    anything a live container might have set. So job.yaml's own
    `max_approaches`/`job_timeout_s`/`iteration_timeout_s` are left
    completely untouched when the resume passes none of the three flags,
    unlike `iterations`, whose value CAN come from status.json (task
    010/#51) -- see test_cli_resume.py's
    `test_resume_iterations_topup_adds_to_status_json_not_job_yaml_when_higher`
    for the contrasting case that pins `iterations` on the other side of
    this same decision."""
    rdir, cdir = _seed_run(ctl, "tst-94r-noflags")
    before = _job_yaml_dict(cdir)
    res = ctl.run("resume", "tst-94r-noflags")
    assert res.returncode == 0, res.stderr
    after = _job_yaml_dict(cdir)
    assert after.get("max_approaches") == before.get("max_approaches")
    assert "job_timeout_s" not in after
    assert "iteration_timeout_s" not in after


# ----------------------------------------------------------------------
# Folded from tests/test_cli_tasks_approach.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

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


def _ca_seed(ctl: Ctl, run_id: str, *, live_plan: dict | None = LIVE_PLAN,
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
    _ca_seed(ctl, "tst-app1", archives={1: APPROACH_1_PLAN, 2: APPROACH_2_PLAN})
    res = ctl.run("tasks", "tst-app1", "--approach", "1")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert _task_ids(res.stdout) == ["001", "002", "003"]
    # Not the live plan's ids, and not approach 2's either.
    assert "101" not in res.stdout and "102" not in res.stdout


def test_tasks_approach_2_differs_from_approach_1(ctl: Ctl):
    _ca_seed(ctl, "tst-app2", archives={1: APPROACH_1_PLAN, 2: APPROACH_2_PLAN})
    res = ctl.run("tasks", "tst-app2", "--approach", "2")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert _task_ids(res.stdout) == ["001", "002"]
    assert "second attempt one" in res.stdout
    assert "archived one" not in res.stdout


def test_tasks_approach_json_carries_the_archived_structure(ctl: Ctl):
    _ca_seed(ctl, "tst-app-json", archives={1: APPROACH_1_PLAN})
    res = ctl.run("--json", "tasks", "tst-app-json", "--approach", "1")
    assert res.returncode == 0, (res.stdout, res.stderr)
    doc = json.loads(res.stdout)
    assert doc["approach"] == 1
    assert [t["id"] for t in doc["tasks"]] == ["001", "002", "003"]
    assert doc["goal"] == "approach 1 plan"


def test_tasks_approach_counts_come_from_the_archived_file_not_live(ctl: Ctl):
    """approach 1 has 2/3 completed; the live plan (1/2 completed) must not
    leak into the archived read at all."""
    _ca_seed(ctl, "tst-app-counts", archives={1: APPROACH_1_PLAN})
    res = ctl.run("tasks", "tst-app-counts", "--approach", "1")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "2/3 completed" in res.stdout


def test_tasks_never_archived_approach_is_a_clean_error(ctl: Ctl):
    _ca_seed(ctl, "tst-app-missing", archives={1: APPROACH_1_PLAN})
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
    _ca_seed(ctl, "tst-all", archives={1: APPROACH_1_PLAN, 2: APPROACH_2_PLAN})
    res = ctl.run("tasks", "tst-all", "--all")
    assert res.returncode == 0, (res.stdout, res.stderr)
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    assert any(ln.startswith("approach 01:") and "2/3 completed" in ln
               for ln in lines), lines
    assert any(ln.startswith("approach 02:") and "1/2 completed" in ln
               for ln in lines), lines


def test_tasks_all_json_carries_per_approach_counts(ctl: Ctl):
    _ca_seed(ctl, "tst-all-json", archives={1: APPROACH_1_PLAN, 2: APPROACH_2_PLAN})
    res = ctl.run("--json", "tasks", "tst-all-json", "--all")
    assert res.returncode == 0, (res.stdout, res.stderr)
    doc = json.loads(res.stdout)
    by_approach = {row["approach"]: row for row in doc["approaches"]}
    assert by_approach[1]["counts"]["completed"] == 2
    assert by_approach[1]["counts"]["total"] == 3
    assert by_approach[2]["counts"]["completed"] == 1
    assert by_approach[2]["counts"]["total"] == 2


def test_tasks_all_with_no_archives_is_a_clean_notice_not_an_error(ctl: Ctl):
    _ca_seed(ctl, "tst-all-empty", archives=None)
    res = ctl.run("tasks", "tst-all-empty", "--all")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert res.stdout == ""
    assert "no archived approaches" in res.stderr


def test_tasks_all_unknown_run_exits_3(ctl: Ctl):
    res = ctl.run("tasks", "no-such-run", "--all")
    assert res.returncode == 3, res.stderr
    assert "not found" in res.stderr


def test_tasks_approach_and_all_are_mutually_exclusive(ctl: Ctl):
    _ca_seed(ctl, "tst-mutex", archives={1: APPROACH_1_PLAN})
    res = ctl.run("tasks", "tst-mutex", "--approach", "1", "--all")
    assert res.returncode != 0
    assert "Traceback" not in res.stderr


# ----------------------------------------------------------------------
# Folded from tests/test_inflight_budget_setters_task094.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import time

import pytest

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir
from tests.conftest import supervisor_factory


_supervisor = supervisor_factory(run_id="task094-budget-setters", cfg_defaults=False,
                                 status={"state": "running"})


def test_set_max_approaches_rebinds_cfg_and_status(tmp_path):
    sup = _supervisor(tmp_path, max_approaches=3)
    sup.set_max_approaches(7)
    assert sup.cfg.max_approaches == 7
    assert sup.run.read_status()["maxApproaches"] == 7
    # GET /config's live read (cfg.effective()) agrees too -- no separate
    # mechanism, same field.
    assert sup.cfg.effective()["budgets"]["maxApproaches"] == 7


def test_set_max_approaches_is_observed_at_the_next_replan_decision(tmp_path):
    """Pins the read site itself: `_step_replan`'s
    `if self._approach < self.cfg.max_approaches:` guard, at
    `src/ralphd/engine/loop.py`. Before the setter runs, the budget is
    already exhausted (`_approach == max_approaches`); after it, the SAME
    approach counter has room again -- proving the change is observed by a
    live read, not just by the accessor that set it."""
    sup = _supervisor(tmp_path, max_approaches=1)
    sup._approach = 1

    exhausted = sup._step_replan()
    assert exhausted == ("tail",)
    assert sup._approach == 1, "an exhausted replan must not bump the approach"

    sup.set_max_approaches(3)
    advanced = sup._step_replan()
    assert advanced == ("advance", "planning", "budget_and_approach_left")
    assert sup._approach == 2


def test_set_iteration_timeout_rebinds_cfg(tmp_path):
    sup = _supervisor(tmp_path, iteration_timeout_s=45 * 60)
    sup.set_iteration_timeout(600)
    assert sup.cfg.iteration_timeout_s == 600
    # No status.json field of its own (unlike iterations/max_approaches) --
    # GET /config's live cfg.effective() read is the only surface, and it
    # already reflects the change with no extra write.
    assert sup.cfg.effective()["budgets"]["iterationTimeoutS"] == 600


def test_set_iteration_timeout_is_observed_by_run_iterations_own_formula(tmp_path):
    """Pins the read site: `run_iteration`'s own
    `timeout = min(self.cfg.iteration_timeout_s, max(...))` -- reproduced
    here (not re-imported, since it is inline in `run_iteration`, not a
    standalone helper) with a deadline far enough out that the configured
    timeout, not the deadline floor, is the binding term both before and
    after the change."""
    sup = _supervisor(tmp_path, iteration_timeout_s=45 * 60,
                      job_timeout_s=8 * 3600)

    def _binding_timeout(phase: str) -> float:
        return min(sup.cfg.iteration_timeout_s,
                   max(sup.REFLECT_TAIL_TIMEOUT_S if phase == "reflect"
                       else 60, int(sup.deadline - time.monotonic())))

    assert _binding_timeout("worker") == 45 * 60
    sup.set_iteration_timeout(90)
    assert _binding_timeout("worker") == 90


def test_set_job_deadline_moves_monotonic_and_epoch_deadlines_together(tmp_path):
    """Task 002 (#94), engine-side setter 3 of 3: `set_job_deadline` must
    move BOTH `self.deadline` (monotonic, `budget_left()`'s own clock) and
    `self._deadline_epoch` (wall clock, published as `deadlineAt`) by the
    SAME delta -- exactly the two-attribute move `_account_infra_wait`
    already performs for an infra extension, applied here for an
    operator-driven change instead. A setter that touched only one of them
    would let `budget_left()` and `GET /status`'s `deadlineAt` disagree
    about how much run time is actually left.
    """
    sup = _supervisor(tmp_path, job_timeout_s=3600)
    before_mono = sup.deadline
    before_epoch = sup._deadline_epoch

    new_epoch = before_epoch + 900.0
    sup.set_job_deadline(new_epoch)

    assert sup._deadline_epoch == new_epoch
    assert sup.deadline == pytest.approx(before_mono + 900.0)


def test_set_job_deadline_republishes_deadline_at_in_status(tmp_path):
    """`deadlineAt` is published once at run start and nowhere else
    automatically -- `set_job_deadline` must rewrite it itself, the same
    way `set_iteration_budget` rewrites `iterationsBudget`, so `GET
    /status` reflects an operator-driven deadline change without waiting
    for the next infra wait to happen to touch the same field."""
    from ralphd.engine.state import utc_from_epoch

    sup = _supervisor(tmp_path, job_timeout_s=3600)
    new_epoch = sup._deadline_epoch + 1800.0

    sup.set_job_deadline(new_epoch)

    assert sup.run.read_status()["deadlineAt"] == utc_from_epoch(new_epoch)


def test_set_job_deadline_emits_an_event_distinguishable_from_infra_extension(tmp_path):
    """An operator-requested deadline change must not be mistaken for an
    automatic infra-outage extension when read back from `events.jsonl` --
    `_account_infra_wait` emits `deadline_extended` with `reason="infra
    wait"`; `set_job_deadline` must emit something else entirely (a
    different event type, not just a different `reason` on the same type),
    and must never emit `deadline_extended` itself. `sup.runner` is stubbed
    even though this setter never touches it, per this module's own rule
    for anything reached through the supervisor rather than a bare
    attribute rebind."""
    from ralphd.engine.state import read_events

    sup = _supervisor(tmp_path, job_timeout_s=3600)
    sup.runner = object()
    new_epoch = sup._deadline_epoch + 300.0

    sup.set_job_deadline(new_epoch)

    events = read_events(sup.run.root)
    deadline_events = [e for e in events if e.get("type") in
                      ("deadline_set", "deadline_extended")]
    assert len(deadline_events) == 1
    ev = deadline_events[0]
    assert ev["type"] == "deadline_set"
    assert ev["type"] != "deadline_extended"
    assert ev.get("reason") != "infra wait"


# ----------------------------------------------------------------------
# Folded from tests/test_cli_contract_violation_surfacing_task012.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import json
import os
import subprocess
from pathlib import Path

import pytest

from ralphd.engine.contract_enforcement import (
    EVENT_AGENT_FAULT,
    EVENT_VIOLATION,
    violation_message,
)
from tests.conftest import RALPHCTL

HAT = "worker"
ERRORS = ["$.task_id: expected string, got int"]
MAX_ATTEMPTS = 3


def _ctl(registry: Path, *argv: str, timeout: int = 60):
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    return subprocess.run([str(RALPHCTL), *argv], env=env,
                          capture_output=True, text=True, timeout=timeout)


def _write_iteration(run_dir: Path, n: int, meta: dict) -> None:
    d = run_dir / "iterations" / f"{n:04d}"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(meta))


def _base_meta(n: int = 1, **over) -> dict:
    meta = {"number": n, "phase": "worker", "approach": 1,
            "startedAt": "2026-09-10T10:00:00Z", "endedAt": "2026-09-10T10:00:31Z",
            "exitCode": 0, "interrupted": False, "timedOut": False,
            "noTrafficTimeout": False, "sawComplete": True, "sawVerified": False,
            "error": None, "faultClass": None, "usage": {"input": 5, "output": 5,
                                                          "totalTokens": 10}}
    meta.update(over)
    return meta


def _violation_event(eid: int, attempt: int) -> dict:
    return {"id": eid, "type": EVENT_VIOLATION, "hat": HAT, "attempt": attempt,
            "maxAttempts": MAX_ATTEMPTS, "errors": list(ERRORS)}


def _fault_event(eid: int) -> dict:
    return {"id": eid, "type": EVENT_AGENT_FAULT, "hat": HAT,
            "attempts": MAX_ATTEMPTS, "errors": list(ERRORS)}


def _write_events(run_dir: Path, events: list[dict]) -> None:
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(ev) + "\n" for ev in events))


def _recovered_run(tmp_path: Path, run_id: str = "recovered") -> tuple[Path, Path]:
    """A run still going: iteration 1's ladder recovered on the re-ask, so
    there is exactly one `contract.violation` and no `contract.agent_fault`,
    and the run itself never ended -- `status.json` carries no `reason` at
    all."""
    registry = tmp_path / "registry"
    run_dir = registry / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps(
        {"runId": run_id, "state": "running", "iterationsUsed": 1}))
    _write_iteration(run_dir, 1, _base_meta())
    _write_events(run_dir, [
        {"id": 1, "type": "iteration.start", "number": 1, "phase": "worker"},
        {"id": 2, "type": "iteration.end", "number": 1, "phase": "worker",
         "exitCode": 0},
        _violation_event(3, attempt=1),
    ])
    return registry, run_dir


def _exhausted_run(tmp_path: Path, run_id: str = "exhausted") -> tuple[Path, Path]:
    """A run the ladder never recovered for: every attempt at iteration 1
    repeated the same violation, `contract.agent_fault` was emitted, and --
    since this happened with no budget left -- the run ended `aborted`
    with `reason` set to exactly what the engine now writes there
    (`contract_enforcement.violation_message`, computed here, never a
    literal re-typed by hand)."""
    registry = tmp_path / "registry"
    run_dir = registry / "runs" / run_id
    run_dir.mkdir(parents=True)
    reason = violation_message(HAT, ERRORS)
    (run_dir / "status.json").write_text(json.dumps(
        {"runId": run_id, "state": "aborted", "verdict": "unverified",
         "reason": reason, "iterationsUsed": 1}))
    _write_iteration(run_dir, 1, _base_meta(sawComplete=False))
    events = [
        {"id": 1, "type": "iteration.start", "number": 1, "phase": "worker"},
        {"id": 2, "type": "iteration.end", "number": 1, "phase": "worker",
         "exitCode": 0},
    ]
    eid = 3
    for attempt in range(1, MAX_ATTEMPTS + 1):
        events.append(_violation_event(eid, attempt))
        eid += 1
    events.append(_fault_event(eid))
    _write_events(run_dir, events)
    return registry, run_dir


# --------------------------------------------------------------- recovered

def test_iteration_n_names_the_contract_violation_when_it_recovered(tmp_path):
    registry, _ = _recovered_run(tmp_path)
    res = _ctl(registry, "iteration", "recovered", "1", "--no-log")
    assert res.returncode == 0, res.stderr
    expected = violation_message(HAT, ERRORS)
    assert f"contract:  {expected}" in res.stdout
    assert "expected string, got int" in res.stdout


def test_iteration_n_json_carries_the_contract_fault_recovered(tmp_path):
    registry, _ = _recovered_run(tmp_path)
    res = _ctl(registry, "--json", "iteration", "recovered", "1", "--no-log")
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    cf = doc["contractFault"]
    assert cf["hat"] == HAT
    assert cf["errors"] == ERRORS
    assert cf["agentFault"] is False
    assert cf["text"] == violation_message(HAT, ERRORS)


def test_status_prints_no_reason_line_for_a_run_that_is_still_going(tmp_path):
    """The recovered case genuinely never ended -- `status.json` carries no
    `reason`, so `ralphctl status` must print none either (never invent
    one from the mere PRESENCE of a `contract.violation` event)."""
    registry, _ = _recovered_run(tmp_path)
    res = _ctl(registry, "status", "recovered")
    assert res.returncode == 0, res.stderr
    assert "reason:" not in res.stdout


# --------------------------------------------------------------- exhausted

def test_status_reason_line_names_the_contract_violation_when_it_did_not_recover(tmp_path):
    registry, _ = _exhausted_run(tmp_path)
    res = _ctl(registry, "status", "exhausted")
    assert res.returncode == 0, res.stderr
    # long text is wrapped across `reason:`/continuation lines (task 003) --
    # normalise before checking the full sentence is there, verbatim.
    rejoined = " ".join(
        line.removeprefix("reason:    ").removeprefix("           ").strip()
        for line in res.stdout.splitlines()
        if line.startswith("reason:") or line.startswith("           "))
    assert violation_message(HAT, ERRORS) in rejoined


def test_iteration_n_names_the_contract_violation_when_it_did_not_recover(tmp_path):
    registry, _ = _exhausted_run(tmp_path)
    res = _ctl(registry, "iteration", "exhausted", "1", "--no-log")
    assert res.returncode == 0, res.stderr
    expected = violation_message(HAT, ERRORS)
    assert f"contract:  {expected}" in res.stdout


def test_iteration_n_json_carries_the_contract_fault_exhausted(tmp_path):
    registry, _ = _exhausted_run(tmp_path)
    res = _ctl(registry, "--json", "iteration", "exhausted", "1", "--no-log")
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    cf = doc["contractFault"]
    assert cf["hat"] == HAT
    assert cf["errors"] == ERRORS
    assert cf["agentFault"] is True
    assert cf["text"] == violation_message(HAT, ERRORS)


def test_status_and_iteration_agree_on_the_exact_same_sentence(tmp_path):
    """The point of the shared derivation: both surfaces, over the SAME
    fixture, quote byte-identical text -- not two independently-worded
    strings that happen to mention the same error today."""
    registry, _ = _exhausted_run(tmp_path)
    status_out = _ctl(registry, "status", "exhausted").stdout
    iteration_out = _ctl(registry, "iteration", "exhausted", "1",
                         "--no-log").stdout
    expected = violation_message(HAT, ERRORS)
    rejoined = " ".join(
        line.removeprefix("reason:    ").removeprefix("           ").strip()
        for line in status_out.splitlines()
        if line.startswith("reason:") or line.startswith("           "))
    assert expected in rejoined
    assert f"contract:  {expected}" in iteration_out


# ----------------------------------------------------------------------
# Folded from tests/test_cli_runs_tasks_column.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import argparse
import json
import re
from pathlib import Path

import pytest
from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_ui import StubEngineApi, UiServer, ui

from ralphd.cli import main as cli_main
from ralphd.cli import ui_server
from ralphd.engine.state import (
    TASK_TROUBLE_MARKER,
    TASKS_LAST_GOOD_NAME,
    TASKS_STALE_LABEL,
    format_task_column,
    format_task_counts,
    format_task_fraction,
    read_tasks_doc,
    task_counts,
)

__all__ = ["StubEngineApi", "UiServer", "ctl", "ui", "unix_sock"]

APP_JS = Path(ui_server.STATIC_DIR) / "app.js"

# 7 tasks, 5 done, one in-progress, one validation-failed -- the shape the PRD
# names (`5/7` with trouble to flag), same fixture as the hub's tests.
MID_PLAN = {"version": 1, "goal": "ship it", "tasks": (
    [{"id": f"{i:03d}", "status": "completed"} for i in range(5)]
    + [{"id": "005", "status": "in-progress"},
       {"id": "006", "status": "validation-failed"}])}
CLEAN_PLAN = {"version": 1, "tasks": [{"id": "001", "status": "completed"},
                                      {"id": "002", "status": "completed"}]}
BIG_PLAN = {"version": 1, "tasks": (
    [{"id": f"{i:03d}", "status": "completed"} for i in range(100)]
    + [{"id": f"p{i:03d}", "status": "pending"} for i in range(150)])}
TRUNCATED = '{"version": 1, "goal": "ship it", "tasks": [{"id": "001", "sta'


# --------------------------------------------------------------------------
# unit tier: the flattened column string
# --------------------------------------------------------------------------

@pytest.mark.parametrize("counts,stale,expected", [
    (task_counts(MID_PLAN["tasks"]), False, "5/7 \u26a0"),
    (task_counts(CLEAN_PLAN["tasks"]), False, "2/2"),
    ({"total": 7, "completed": 7}, False, "7/7"),
    ({"total": 4, "completed": 4}, True, "4/4 " + TASKS_STALE_LABEL),
    (task_counts(MID_PLAN["tasks"]), True, "5/7 \u26a0 " + TASKS_STALE_LABEL),
    # no plan -> no cell at all, and therefore no marker to misread
    ({}, False, ""),
    ({"total": 0}, False, ""),
    ({}, True, ""),
    ({"total": 0, "inProgress": 0}, False, ""),
    ({"total": "seven"}, False, ""),                      # junk degrades
    (None, False, ""),
])
def test_format_task_column_renderings(counts, stale, expected):
    assert format_task_column(counts, stale=stale) == expected


def test_the_column_never_renders_zero_over_zero():
    """`format_task_fraction`'s rule, inherited: a run whose agent has not
    written a plan yet has no denominator, and an unreadable `tasks.json` is
    ignorance rather than a plan of zero tasks."""
    for counts in ({}, {"total": 0, "completed": 0}, {"total": None}):
        for stale in (False, True):
            assert format_task_column(counts, stale=stale) == ""


def test_the_column_starts_with_the_shared_fraction():
    """One fraction formatter for `ralphctl runs`, `ralphctl status` and the
    hub -- the flattening only ever APPENDS markers."""
    counts = task_counts(MID_PLAN["tasks"])
    fraction = format_task_fraction(counts)
    assert format_task_column(counts).split(" ")[0] == fraction == "5/7"
    assert fraction in format_task_counts(counts)


def test_the_trouble_marker_is_the_glyph_the_hub_cell_appends():
    """`app.js taskCell` marks a troubled plan with `\\u26A0`; the CLI column
    marks it with `TASK_TROUBLE_MARKER`. One vocabulary, so this test fails if
    either side starts using its own glyph."""
    assert TASK_TROUBLE_MARKER == "\u26a0"
    js = APP_JS.read_text()
    assert re.search(r"\\u26A0", js, re.IGNORECASE), (
        "app.js no longer appends the shared trouble marker")


def test_the_marker_is_only_for_the_two_trouble_statuses():
    """Pending work is not trouble: a plan with 5 pending tasks is a plan in
    progress, and marking it would make the marker meaningless."""
    assert format_task_column({"total": 7, "completed": 2, "pending": 5}) == "2/7"
    assert format_task_column({"total": 7, "completed": 6,
                               "inProgress": 1}) == "6/7 \u26a0"
    assert format_task_column({"total": 7, "completed": 6,
                               "validationFailed": 1}) == "6/7 \u26a0"


# --------------------------------------------------------------------------
# unit tier: ONE row builder behind both list surfaces
# --------------------------------------------------------------------------

def _seed_run_dir(registry: Path, run_id: str, *, tasks_text: str | None = None,
                  last_good: dict | None = None, **status_over) -> Path:
    run_dir = registry / "runs" / run_id
    run_dir.mkdir(parents=True)
    status = {"runId": run_id, "state": "succeeded", "verdict": "verified",
              "phase": "worker", "approach": 1, "maxApproaches": 3,
              "iterationsUsed": 3, "iterationsBudget": 25,
              "startedAt": "2026-01-01T00:00:00Z", **status_over}
    (run_dir / "status.json").write_text(json.dumps(status))
    if tasks_text is not None:
        (run_dir / "tasks.json").write_text(tasks_text)
    if last_good is not None:
        (run_dir / TASKS_LAST_GOOD_NAME).write_text(json.dumps(last_good))
    return run_dir


def test_hub_row_fields_come_from_the_shared_builder(tmp_path):
    """`ui_server._row_tasks` is now the shared builder plus a read: whatever
    the hub row says about a plan, `TasksRead.row_fields` said."""
    run_dir = _seed_run_dir(tmp_path / "reg", "shared",
                            tasks_text=json.dumps(MID_PLAN))
    assert ui_server._row_tasks(run_dir) == read_tasks_doc(
        run_dir, persist=False).row_fields


def test_the_builder_carries_raw_counts_rendered_strings_and_the_contract(tmp_path):
    run_dir = _seed_run_dir(tmp_path / "reg", "fields",
                            tasks_text=json.dumps(MID_PLAN))
    fields = read_tasks_doc(run_dir, persist=False).row_fields
    assert fields == {
        "tasksTotal": 7, "tasksCompleted": 5, "tasksInProgress": 1,
        "tasksValidationFailed": 1,
        # Task 025 (#33) RETARGETED this exact-dict assertion: a row now also
        # carries the terminal-failure counts and both meanings of them, so a
        # consumer reading the row alone can tell a plan that finished from one
        # that gave up (and on which of the two grounds). Zeros here, because
        # MID_PLAN has no failed task -- every rendered string below is
        # byte-identical to before.
        "tasksFailed": 0,
        "tasksFailedValidationExhausted": 0,
        "tasksFailedRequirementUnmet": 0,
        # Task 021 (#60 pillar 3) added this raw count alongside the failure
        # ones above; task 123 caught this exact-dict assertion up (it went
        # red at task 101, unrelated to this wave's own worker/prompt work) --
        # 0 here too, because MID_PLAN has no dead-dependency subtree either.
        "tasksBlocked": 0,
        # v011-model-per-hat task 019 (#77/#78) added these three alongside the
        # completion-gate vocabulary fix: MID_PLAN has no `validated`/
        # `awaitingValidation` task and no tier field set anywhere, so all
        # three read as the empty/unset case -- 0, 0 and None respectively --
        # while the rendered strings below stay byte-identical to before.
        "tasksValidated": 0, "tasksAwaitingValidation": 0,
        "taskEffectiveTier": None,
        "tasksDisplay": "5/7",
        "tasksSummary": "5/7 completed (1 in-progress, 1 validation-failed)",
        "tasksTrouble": ["1 validation-failed", "1 in-progress"],
        "tasksColumn": "5/7 \u26a0",
        "tasksStale": False, "tasksSource": "file",
    }


def test_the_builder_carries_the_blocked_count_and_leads_the_trouble_list(tmp_path):
    """Task 123 (#60): the rendered-string case the previous test's own name
    already promised ("raw counts, RENDERED STRINGS and the contract") but
    never actually exercised for `blocked` -- a dead-dependency subtree must
    show up as a raw count, in the trouble sentence list (leading it, per
    `TASK_TROUBLE_KEYS`), and in the compact column's warning glyph, exactly
    like `validationFailed`/`inProgress` already do above."""
    blocked_plan = {"version": 1, "goal": "ship it", "tasks": [
        {"id": "001", "status": "failed"},
        {"id": "002", "status": "pending", "dependsOn": ["001"]},
        {"id": "003", "status": "completed"},
    ]}
    run_dir = _seed_run_dir(tmp_path / "reg", "blocked-fields",
                            tasks_text=json.dumps(blocked_plan))
    fields = read_tasks_doc(run_dir, persist=False).row_fields
    assert fields["tasksBlocked"] == 1
    assert fields["tasksTrouble"][0] == "1 blocked"
    assert fields["tasksColumn"] == "1/3 \u26a0"


def test_cli_and_hub_rows_carry_the_identical_task_fields(tmp_path):
    """The two list surfaces read the same run dir independently; every
    `tasks*` field must match key for key (this is what makes the black-box
    agreement tests below a check on the rendering, not on the data)."""
    reg = tmp_path / "reg"
    for run_id, tasks_text, last_good in (
            ("mid", json.dumps(MID_PLAN), None),
            ("clean", json.dumps(CLEAN_PLAN), None),
            ("planless", None, None),
            ("stale", TRUNCATED, CLEAN_PLAN),
            ("unreadable", TRUNCATED, None)):
        run_dir = _seed_run_dir(reg, run_id, tasks_text=tasks_text,
                                last_good=last_good)
        hub_row = ui_server._row_tasks(run_dir)
        cli_row = read_tasks_doc(run_dir, persist=False).row_fields
        assert hub_row == cli_row, run_id


# --------------------------------------------------------------------------
# in-process: one hardened read per LISTED row, persist=False, no writes
# --------------------------------------------------------------------------

def _run_cmd_runs(monkeypatch, capsys, reg: Path, **args_over) -> list[dict]:
    monkeypatch.setenv("RALPHD_REGISTRY", str(reg))
    defaults = {"state": None, "json": True, "sort": None, "reverse": False}
    args = argparse.Namespace(**{**defaults, **args_over})
    cli_main.cmd_runs(args)
    return json.loads(capsys.readouterr().out)


def test_cmd_runs_reads_each_plan_once_through_the_hardened_reader(
        tmp_path, monkeypatch, capsys):
    reg = tmp_path / "reg"
    for run_id in ("run-a", "run-b", "run-c"):
        _seed_run_dir(reg, run_id, tasks_text=json.dumps(MID_PLAN))

    calls: list[tuple[str, bool]] = []
    real = cli_main.read_tasks_doc

    def spy(run_root, **kw):
        calls.append((str(run_root), kw.get("persist")))
        return real(run_root, **kw)

    monkeypatch.setattr(cli_main, "read_tasks_doc", spy)
    rows = _run_cmd_runs(monkeypatch, capsys, reg)

    assert [r["tasksDisplay"] for r in rows] == ["5/7", "5/7", "5/7"]
    assert len(calls) == 3, calls                    # exactly one per row
    assert len({c[0] for c in calls}) == 3           # a different dir each
    # `persist=False`: `ralphctl runs` is a viewer of somebody else's run dir
    # (possibly a LIVE run's) and must never leave a last-good cache behind.
    assert {c[1] for c in calls} == {False}


def test_cmd_runs_does_not_read_the_plan_of_a_filtered_out_run(
        tmp_path, monkeypatch, capsys):
    """`--state` filters before the read, so listing one running run out of a
    hundred finished ones costs one read, not a hundred."""
    reg = tmp_path / "reg"
    _seed_run_dir(reg, "kept", state="running", tasks_text=json.dumps(MID_PLAN))
    _seed_run_dir(reg, "dropped", state="succeeded",
                  tasks_text=json.dumps(MID_PLAN))

    calls: list[str] = []
    real = cli_main.read_tasks_doc
    monkeypatch.setattr(cli_main, "read_tasks_doc",
                        lambda root, **kw: (calls.append(str(root)),
                                            real(root, **kw))[1])
    rows = _run_cmd_runs(monkeypatch, capsys, reg, state="running")
    assert [r["runId"] for r in rows] == ["kept"]
    assert [Path(c).name for c in calls] == ["kept"]


def test_cmd_runs_writes_nothing_into_the_run_dir(tmp_path, monkeypatch, capsys):
    reg = tmp_path / "reg"
    run_dir = _seed_run_dir(reg, "mid-write", tasks_text=TRUNCATED,
                            last_good=CLEAN_PLAN)
    before = {p.name: p.stat().st_mtime_ns for p in run_dir.iterdir()}
    rows = _run_cmd_runs(monkeypatch, capsys, reg)
    assert rows[0]["tasksColumn"] == "2/2 " + TASKS_STALE_LABEL
    assert {p.name: p.stat().st_mtime_ns for p in run_dir.iterdir()} == before


# --------------------------------------------------------------------------
# black-box tier: the rendered column, no container anywhere
# --------------------------------------------------------------------------

# runId              tasks.json               last-good      expected cell
_RUNS_FIXTURE = [
    ("aaa-mid", json.dumps(MID_PLAN), None, "5/7 \u26a0"),
    ("bbb-clean", json.dumps(CLEAN_PLAN), None, "2/2"),
    ("ccc-planless", None, None, ""),
    ("ddd-stale", TRUNCATED, CLEAN_PLAN, "2/2 " + TASKS_STALE_LABEL),
    ("eee-unreadable", TRUNCATED, None, ""),
]


def _seed_ctl_runs(ctl: Ctl) -> None:
    for run_id, tasks_text, last_good, _cell in _RUNS_FIXTURE:
        _seed_run_dir(ctl.registry, run_id, tasks_text=tasks_text,
                      last_good=last_good)


def _cells(stdout: str, column: str, next_column: str) -> dict:
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    header = lines[0]
    start, end = header.index(column), header.index(next_column)
    return {ln.split()[0]: ln[start:end].strip() for ln in lines[1:]}


def test_runs_header_has_a_tasks_column_between_approach_and_iterations(ctl):
    """The hub's column order (task 014), so the two lists read the same."""
    _seed_ctl_runs(ctl)
    res = ctl.run("runs")
    assert res.returncode == 0, res.stderr
    header = res.stdout.splitlines()[0]
    assert header.index("APPROACH") < header.index("TASKS") < header.index("ITER")


def test_runs_renders_every_task_cell(ctl):
    _seed_ctl_runs(ctl)
    res = ctl.run("runs")
    assert res.returncode == 0, res.stderr
    assert _cells(res.stdout, "TASKS", "ITER") == {
        run_id: cell for run_id, _t, _lg, cell in _RUNS_FIXTURE}
    # never a fabricated denominator, and the iterations column is untouched
    assert "0/0" not in res.stdout
    assert _cells(res.stdout, "ITER", "STARTED")["aaa-mid"] == "3/25"


def test_runs_json_carries_the_raw_counts_and_the_flag_wording(ctl):
    _seed_ctl_runs(ctl)
    rows = {r["runId"]: r for r in json.loads(ctl.run("--json", "runs").stdout)}
    mid = rows["aaa-mid"]
    assert (mid["tasksTotal"], mid["tasksCompleted"]) == (7, 5)
    assert (mid["tasksInProgress"], mid["tasksValidationFailed"]) == (1, 1)
    # the sentences the column cannot fit, verbatim from the shared formatter
    assert mid["tasksTrouble"] == ["1 validation-failed", "1 in-progress"]
    assert mid["tasksSummary"] == format_task_counts(task_counts(MID_PLAN["tasks"]))
    assert (mid["tasksStale"], mid["tasksSource"]) == (False, "file")
    # task 002's contract travels with the row, so a consumer can tell a
    # last-good fraction from a fresh one without parsing the cell
    stale = rows["ddd-stale"]
    assert (stale["tasksStale"], stale["tasksSource"]) == (True, "last-good")
    assert (stale["tasksTotal"], stale["tasksCompleted"]) == (2, 2)
    # a plan-less run: explicit zeros with empty display strings, never `0/0`
    blank = rows["ccc-planless"]
    assert (blank["tasksTotal"], blank["tasksDisplay"],
            blank["tasksSummary"], blank["tasksColumn"]) == (0, "", "", "")
    assert blank["tasksSource"] == "absent"
    # ... and an unreadable plan is ignorance, not an empty plan
    assert rows["eee-unreadable"]["tasksSource"] == "unreadable"
    assert rows["eee-unreadable"]["tasksColumn"] == ""


def test_runs_sorts_on_the_completion_fraction_with_plan_less_runs_last(ctl):
    """The hub's dialect (task 014): `5/7` (0.71) outranks `100/250` (0.4),
    and a run with no plan has no fraction at all -- it sorts last ascending
    rather than pretending to be 0% done."""
    _seed_run_dir(ctl.registry, "aaa-mid", tasks_text=json.dumps(MID_PLAN))
    _seed_run_dir(ctl.registry, "bbb-big", tasks_text=json.dumps(BIG_PLAN))
    _seed_run_dir(ctl.registry, "ccc-clean", tasks_text=json.dumps(CLEAN_PLAN))
    _seed_run_dir(ctl.registry, "ddd-planless")

    res = ctl.run("runs", "--sort", "tasks")
    assert res.returncode == 0, res.stderr
    order = [ln.split()[0] for ln in res.stdout.splitlines()[1:] if ln.strip()]
    assert order == ["bbb-big", "aaa-mid", "ccc-clean", "ddd-planless"]

    rev = ctl.run("runs", "--sort", "tasks", "--reverse")
    assert rev.returncode == 0, rev.stderr
    rev_order = [ln.split()[0] for ln in rev.stdout.splitlines()[1:] if ln.strip()]
    # Descending flips the WHOLE comparison, missing values included -- the
    # hub's `sortRuns` does exactly the same (`cmpValues(...) * dir`), so the
    # plan-less run leads here rather than trailing. Asserted rather than
    # "fixed": the two surfaces agreeing is the property that matters.
    assert rev_order == ["ddd-planless", "ccc-clean", "aaa-mid", "bbb-big"]


# --------------------------------------------------------------------------
# agreement: hub row == `ralphctl runs` == `ralphctl status`
# --------------------------------------------------------------------------

def _hub_rows(server) -> dict:
    code, body = server.get("/api/runs")
    assert code == 200, body
    return {r["runId"]: r for r in body["runs"]}


def _assert_agrees(hub_row: dict, cli_row: dict, status: dict) -> None:
    counts = status.get("tasks") or {}
    for key in ("tasksTotal", "tasksCompleted", "tasksInProgress",
                "tasksValidationFailed", "tasksDisplay", "tasksSummary",
                "tasksTrouble", "tasksColumn", "tasksStale", "tasksSource"):
        assert hub_row[key] == cli_row[key], key
    # ... and both agree with what `ralphctl status` counted for the same run
    assert cli_row["tasksTotal"] == counts.get("total", 0)
    assert cli_row["tasksCompleted"] == counts.get("completed", 0)
    assert cli_row["tasksDisplay"] == format_task_fraction(counts)
    assert cli_row["tasksColumn"] == format_task_column(
        counts, stale=bool(status.get("tasksStale")))
    if cli_row["tasksDisplay"]:
        assert cli_row["tasksSummary"] == format_task_counts(counts)


def test_hub_runs_and_status_agree_with_the_container_gone(ctl, ui):
    """#21's case: a finished run whose container is long gone. All three
    surfaces read the same `tasks.json` with the same hardened reader."""
    _seed_ctl_runs(ctl)
    hub = _hub_rows(ui(ctl.registry))
    cli = {r["runId"]: r for r in json.loads(ctl.run("--json", "runs").stdout)}
    for run_id, _t, _lg, cell in _RUNS_FIXTURE:
        status = json.loads(ctl.run("--json", "status", run_id).stdout)
        assert status["live"] is False, run_id
        _assert_agrees(hub[run_id], cli[run_id], status)
        assert cli[run_id]["tasksColumn"] == cell
    # the human surfaces say it too, not only --json
    assert _cells(ctl.run("runs").stdout, "TASKS", "ITER")["aaa-mid"] == "5/7 \u26a0"
    text = ctl.run("status", "aaa-mid").stdout
    assert "tasks:     5/7 completed (1 in-progress, 1 validation-failed)" in text


def test_hub_runs_and_status_agree_for_a_live_run(live, ui):
    """The same three surfaces against a REAL engine that is still answering:
    `ralphctl status` takes its live `GET /status` path (counts synthesised by
    the engine from `tasks.json`) while `ralphctl runs` and the hub list read
    the file directly -- and they still say the same thing."""
    run = live(run_id="tasks-column-live", job={"iterations": 6},
               stub_env={"STUB_TASKS": "3"})
    run.wait_api()
    run.wait_terminal(timeout=180)

    status = json.loads(run.ralphctl("--json", "status", run.run_id).stdout)
    assert status["live"] is True, "engine should still be answering (idle)"
    counts = status["tasks"]
    assert counts["total"] == 3

    cli = {r["runId"]: r
           for r in json.loads(run.ralphctl("--json", "runs").stdout)}
    hub = _hub_rows(ui(run.registry))
    _assert_agrees(hub[run.run_id], cli[run.run_id], status)
    assert cli[run.run_id]["tasksDisplay"] == f"{counts['completed']}/3"


def test_a_live_run_keeps_the_columns_source_local(tmp_path, ui, ctl):
    """`ralphctl runs` must not start proxying: the fraction for a run with a
    live API comes off disk, exactly like the hub list's (task 013). Asserted
    against a stub engine that records every request it receives."""
    engine = StubEngineApi(status={"runId": "run-live", "state": "running"},
                           tasks={"tasks": [], "total": 0})
    try:
        run_dir = _seed_run_dir(ctl.registry, "run-live", state="running",
                               tasks_text=json.dumps(MID_PLAN))
        (run_dir / "host.json").write_text(json.dumps(
            {"runId": "run-live", "port": engine.port,
             "apiUrl": f"http://127.0.0.1:{engine.port}"}))
        rows = json.loads(ctl.run("--json", "runs").stdout)
        assert rows[0]["tasksDisplay"] == "5/7"     # disk, not the empty live plan
        assert engine.requests == []
        # control: `status` DOES go live for the same run, so the assertion
        # above is about `runs` specifically, not about a silent stub
        doc = json.loads(ctl.run("--json", "status", "run-live").stdout)
        assert doc["live"] is True
        assert [p for _m, p, _a in engine.requests] != []
    finally:
        engine.close()
