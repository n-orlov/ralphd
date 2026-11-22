"""v0.38 (#306, #320): a `petitioned` task has defined exits.

A petitioned task is SETTLED, and then satisfies every `dependsOn` naming it
and never blocks `plan_complete`, when any of three sources covers it: an
applied task-level waiver (`ralphctl amend <run> <task> --waive`), a
clause-bound waiver whose clause matches the petition's obligation, or a
CONFIRMED adjudication. It keeps its status throughout (no status lies). The
rule lives next to `TASK_DEPENDENCY_SATISFIED_STATUSES`
(`state.task_dependency_satisfied`, `state.settled_petitioned_task_ids`).

An operator status ruling moves a petitioned task out
(`petitioned -> validated|skipped`, both variants), beside the engine's own
CONFIRMED-adjudication application (`-> skipped`), and an operator ruling
takes the un-reopen edge on a cure-reopened task; a hat's report of either
stays refused.

Sections: (1) one test per settling source, (2) the plan_complete check,
(3) controls that hold before and after, (4) the operator exits, (5) the
display surfaces (`GET /tasks`, the hub's on-disk table, `ralphctl tasks`).
"""

from __future__ import annotations

import json

import pytest
from test_cli_docker import ctl, unix_sock  # noqa: F401  (fixtures)

from ralphd.cli import ui_server
from ralphd.engine import loop as loop_mod
from ralphd.engine import petitions, state, transitions
from ralphd.engine.scheduler import select_next_task
from ralphd.engine.state import (
    TASK_BLOCKED_FIELD,
    RunDir,
    apply_amendment_status_rulings,
    write_amendment,
)
from tests.conftest import make_supervisor

OBLIGATION = "Task 001: make the impossible thing work"


def _petitioned(**over) -> dict:
    task = {"id": "001", "title": "make the impossible thing work",
            "status": "petitioned", "successCriteria": "criterion 2 holds"}
    task.update(over)
    return task


def _dependent(**over) -> dict:
    task = {"id": "002", "title": "needs 001", "status": "pending",
            "dependsOn": ["001"], "successCriteria": "uses 001"}
    task.update(over)
    return task


@pytest.fixture
def cfg_dir(tmp_path, monkeypatch):
    """The `/config` directory the engine reads operator amendments from."""
    d = tmp_path / "config"
    d.mkdir()
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", d)
    return d


def _sup(tmp_path, tasks, vigilant=False):
    sup = make_supervisor(
        tmp_path / "run", run_id="petitioned-settled", cfg_defaults=False,
        workspace=tmp_path / "ws", iterations=5, max_approaches=1,
        vigilant=vigilant, on_complete="exit")
    sup.run.write_tasks({"version": 1, "tasks": tasks})
    return sup


def _select(sup):
    """What the worker loop does each pass: the scheduler over the plan, fed
    the settled ids the supervisor derives from the run's own records."""
    doc = sup.run.read_tasks()
    settled = sup._settled_petitioned_ids(doc)
    return select_next_task(doc["tasks"], doc.get("phases"), settled=settled)


def _status_of(sup, task_id):
    return next(t["status"] for t in sup.run.read_tasks()["tasks"]
                if t["id"] == task_id)


# ---------------------------------------------------------------------------
# (1) one test per settling source
# ---------------------------------------------------------------------------


def test_task_level_waiver_settles_a_petitioned_dependency(tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    write_amendment(cfg_dir, "001", "operator accepts the petition", waive=True)

    picked = _select(sup)

    assert picked is not None and picked["id"] == "002"
    assert _status_of(sup, "001") == "petitioned"


def test_clause_waiver_matching_the_obligation_settles_a_petitioned_dependency(
        tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    sup.run.add_waiver(clause=OBLIGATION, finding="the petition is not a defect",
                       judge="review")

    picked = _select(sup)

    assert picked is not None and picked["id"] == "002"
    assert _status_of(sup, "001") == "petitioned"


def test_confirmed_adjudication_settles_a_petitioned_dependency(tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    petitions.record_confirmed_adjudications(
        sup.run, [{"obligation": OBLIGATION, "adjudication": "CONFIRMED"}])

    picked = _select(sup)

    assert picked is not None and picked["id"] == "002"
    assert _status_of(sup, "001") == "petitioned"


def test_settled_petitioned_task_is_no_cause_in_the_blocked_by_overlay(
        tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    tasks = sup.run.read_tasks()["tasks"]
    assert state.task_blocked_by(tasks, include_petitioned=True) == {"002": ["001"]}
    write_amendment(cfg_dir, "001", "operator accepts the petition", waive=True)

    settled = sup._settled_petitioned_ids()

    assert state.task_blocked_by(
        tasks, include_petitioned=True, settled=settled) == {}


# ---------------------------------------------------------------------------
# (2) plan_complete
# ---------------------------------------------------------------------------


def test_plan_complete_accepts_a_plan_whose_only_open_task_is_a_waived_petition(
        tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(), _dependent(status="completed")])
    write_amendment(cfg_dir, "001", "operator accepts the petition", waive=True)
    doc = sup.run.read_tasks()

    assert sup._outstanding_for_completion(doc) == []
    assert sup._apply_worker_plan_complete_report(None, None, doc) == []
    assert _status_of(sup, "001") == "petitioned"


# ---------------------------------------------------------------------------
# (3) controls: these hold before and after the change
# ---------------------------------------------------------------------------


def test_control_unsettled_petitioned_dependency_still_blocks(tmp_path):
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    tasks = sup.run.read_tasks()["tasks"]

    assert select_next_task(tasks) is None
    assert state.task_blocked_by(tasks, include_petitioned=True) == {"002": ["001"]}
    assert [t["id"] for t in sup._outstanding_for_completion(sup.run.read_tasks())
            ] == ["001", "002"]


def test_control_skipped_dependency_still_does_not_satisfy(tmp_path):
    sup = _sup(tmp_path, [_petitioned(status="skipped"), _dependent()])
    tasks = sup.run.read_tasks()["tasks"]

    assert select_next_task(tasks) is None
    assert state.task_blocked_by(tasks) == {"002": ["001"]}


def test_skipped_dependency_is_not_satisfied_by_a_waiver_either(tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(status="skipped"), _dependent()])
    write_amendment(cfg_dir, "001", "waived", waive=True)

    assert _select(sup) is None


def test_an_unrelated_waiver_does_not_settle_the_petition(tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    write_amendment(cfg_dir, "009", "waives some other task", waive=True)
    sup.run.add_waiver(clause="an unrelated PRD clause about logging",
                       finding="not this task", judge="review")

    assert _select(sup) is None


def test_a_later_replace_ruling_withdraws_a_task_waiver(tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    write_amendment(cfg_dir, "001", "waived", waive=True)
    write_amendment(cfg_dir, "001", "actually, the criteria now read: ...")

    assert _select(sup) is None


@pytest.mark.parametrize("vigilant", [False, True])
def test_control_worker_report_petitioned_to_skipped_is_refused(vigilant):
    errors = transitions.task_status_report_errors(
        {"task_status": "skipped", "justification": "operator waived"},
        "petitioned", vigilant)

    assert errors and "not a legal transition" in errors[0]


@pytest.mark.parametrize("vigilant", [False, True])
def test_control_worker_report_petitioned_to_validated_is_refused(vigilant):
    assert transitions.task_status_report_errors(
        {"task_status": "validated"}, "petitioned", vigilant)


def test_control_worker_report_applied_through_the_supervisor_is_refused(tmp_path):
    sup = _sup(tmp_path, [_petitioned()])

    errors = sup._apply_reported_task_status(
        {"task_id": "001", "task_status": "skipped"})

    assert errors
    assert _status_of(sup, "001") == "petitioned"


@pytest.mark.parametrize("vigilant", [False, True])
@pytest.mark.parametrize("target", ["validated", "completed", "skipped", "failed"])
def test_control_cure_reopen_guard_still_refuses_a_hat(vigilant, target):
    errors = transitions.task_status_report_errors(
        {"task_status": target}, "pending", vigilant)

    # a hat's report out of `pending` is refused for every un-reopen target
    # the variant does not itself reach (`completed` non-vigilant is the
    # ordinary claim path's own terminal, still refused out of `pending`)
    assert errors


# ---------------------------------------------------------------------------
# (4) operator exits: these fail before the change and pass after it
# ---------------------------------------------------------------------------


def _run_with(tmp_path, tasks, **status) -> RunDir:
    run = RunDir(root=tmp_path / "run")
    run.tasks_file.write_text(json.dumps({"version": 1, "tasks": tasks}))
    if status:
        run.update_status(**status)
    return run


def _events(run):
    path = run.root / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip()] if path.exists() else []


@pytest.mark.parametrize("vigilant", [False, True])
@pytest.mark.parametrize("target", ["validated", "skipped"])
def test_operator_ruling_applies_petitioned_exit(tmp_path, vigilant, target):
    run = _run_with(tmp_path, [_petitioned()])
    cdir = tmp_path / "config"
    fname = write_amendment(cdir, "001", "operator ruling on the petition",
                            status=target)

    results = apply_amendment_status_rulings(run, cdir, vigilant)

    assert [(r["applied"], r["reason"]) for r in results] == [(True, "")]
    assert run.read_tasks()["tasks"][0]["status"] == target
    applied = [e for e in _events(run) if e.get("type") == "amendment_status_applied"]
    assert [(e["taskId"], e["previous"], e["status"], e["source"], e["file"])
            for e in applied] == [("001", "petitioned", target, "operator", fname)]


@pytest.mark.parametrize("vigilant", [False, True])
def test_operator_ruling_validated_on_a_cure_reopened_task_is_applied(
        tmp_path, vigilant):
    run = _run_with(tmp_path, [_dependent(id="001", dependsOn=[])],
                    preCureStatuses={"001": "validated"})
    cdir = tmp_path / "config"
    write_amendment(cdir, "001", "operator accepts the re-opened task",
                    status="validated")

    results = apply_amendment_status_rulings(run, cdir, vigilant)

    assert [(r["applied"], r["reason"]) for r in results] == [(True, "")]
    assert run.read_tasks()["tasks"][0]["status"] == "validated"
    # the un-reopen edge emits its own dedicated event, naming the operator
    reopened = [e for e in _events(run) if e.get("type") == state.UN_REOPEN_EVENT]
    assert [(e["taskId"], e["status"], e["source"]) for e in reopened] == [
        ("001", "validated", "operator")]


@pytest.mark.parametrize("vigilant", [False, True])
def test_operator_ruling_validated_on_a_never_reopened_task_is_still_refused(
        tmp_path, vigilant):
    run = _run_with(tmp_path, [_dependent(id="001", dependsOn=[])])
    cdir = tmp_path / "config"
    write_amendment(cdir, "001", "no cure ever re-opened this", status="validated")

    results = apply_amendment_status_rulings(run, cdir, vigilant)

    assert [r["applied"] for r in results] == [False]
    assert run.read_tasks()["tasks"][0]["status"] == "pending"


# ---------------------------------------------------------------------------
# (5) the display surfaces read the same rule: `GET /tasks` (with the run's
#     own approach), the hub's on-disk table and `ralphctl tasks`' on-disk
#     fallback. A settled petitioned dependency is no blocker on any of them.
# ---------------------------------------------------------------------------

def _plan_text(tasks) -> str:
    return json.dumps({"version": 1, "tasks": tasks})


@pytest.fixture
def api_get(tmp_path, monkeypatch):
    """`GET <path>` against the engine API over a run on approach 2, with
    the API module's `/config` pointed at a temp dir."""
    import asyncio

    import httpx

    from ralphd.engine import api as api_mod
    from ralphd.engine.api import create_app
    from ralphd.engine.config import JobConfig
    from ralphd.engine.loop import LoopSupervisor

    cdir = tmp_path / "config"
    cdir.mkdir()
    monkeypatch.setattr(api_mod, "CONFIG_DIR", cdir)
    run = RunDir(root=tmp_path / "run")
    run.update_status(state="running", approach=2)
    run.tasks_file.write_text(_plan_text([_petitioned(), _dependent()]))
    sup = LoopSupervisor(JobConfig(run_id="unit", infra_retry_backoff_s=[0.0],
                                  infra_retry_backoff_max_s=0.0), run, tmp_path)
    app = create_app(sup.cfg, run, sup)

    def get(path: str) -> dict:
        async def go():
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://engine") as c:
                r = await c.get(path)
                assert r.status_code == 200, r.text
                return r.json()
        return asyncio.run(go())

    get.run, get.cdir = run, cdir
    return get


def test_get_tasks_drops_a_dependency_waived_for_the_current_approach(api_get):
    assert api_get("/tasks")[TASK_BLOCKED_FIELD] == {"002": ["001"]}
    write_amendment(api_get.cdir, "001", "accepted on approach 2", waive=True,
                    approach=2)

    assert TASK_BLOCKED_FIELD not in api_get("/tasks")


def test_control_get_tasks_keeps_a_dependency_waived_for_another_approach(api_get):
    write_amendment(api_get.cdir, "001", "accepted on approach 1", waive=True,
                    approach=1)

    assert api_get("/tasks")[TASK_BLOCKED_FIELD] == {"002": ["001"]}


def test_get_tasks_drops_a_dependency_covered_by_a_clause_waiver(api_get):
    api_get.run.add_waiver(clause=OBLIGATION, finding="not a defect",
                           judge="review")

    assert TASK_BLOCKED_FIELD not in api_get("/tasks")


def _hub_registry(tmp_path, run_id: str):
    reg = tmp_path / "registry"
    rdir = reg / "runs" / run_id
    rdir.mkdir(parents=True)
    (reg / "configs" / run_id).mkdir(parents=True)
    (rdir / "status.json").write_text(json.dumps({
        "runId": run_id, "state": "failed", "approach": 1,
        "schemaVersion": 1, "startedAt": "2026-01-01T00:00:00Z"}))
    (rdir / "tasks.json").write_text(_plan_text([_petitioned(), _dependent()]))
    (rdir / "host.json").write_text(json.dumps({
        "runId": run_id, "container": f"ralphd-{run_id}", "port": 1,
        "apiUrl": "http://127.0.0.1:1", "image": "img", "startedAt": "x"}))
    return reg, rdir


def _hub_blocked(reg, run_id):
    return ui_server.run_detail(reg, run_id)["tasks"].get(TASK_BLOCKED_FIELD)


def test_hub_on_disk_drops_a_dependency_covered_by_a_clause_waiver(tmp_path):
    reg, rdir = _hub_registry(tmp_path, "hub-clause")
    # first read populates the per-run cache with the blocker...
    assert _hub_blocked(reg, "hub-clause") == {"002": ["001"]}
    # ...and a waiver landing later is seen though tasks.json never moved.
    RunDir(root=rdir).add_waiver(clause=OBLIGATION, finding="not a defect",
                                 judge="review")

    assert _hub_blocked(reg, "hub-clause") is None


def test_hub_on_disk_drops_a_dependency_covered_by_a_task_waiver(tmp_path):
    reg, _ = _hub_registry(tmp_path, "hub-task")
    write_amendment(reg / "configs" / "hub-task", "001", "accepted", waive=True)

    assert _hub_blocked(reg, "hub-task") is None


def test_hub_on_disk_drops_a_dependency_covered_by_a_confirmed_adjudication(
        tmp_path):
    reg, rdir = _hub_registry(tmp_path, "hub-confirmed")
    petitions.record_confirmed_adjudications(
        RunDir(root=rdir), [{"obligation": OBLIGATION,
                             "adjudication": "CONFIRMED"}])

    assert _hub_blocked(reg, "hub-confirmed") is None


def test_control_hub_on_disk_ignores_an_unwitnessed_adjudication_record(tmp_path):
    """A record the engine never announced writing (no witness MAC in
    events.jsonl) settles nothing on the read-only path either."""
    reg, rdir = _hub_registry(tmp_path, "hub-forged")
    entries = [{"obligation": OBLIGATION, "adjudication": "CONFIRMED"}]
    (rdir / "petition-adjudications.json").write_text(json.dumps({
        "entries": entries, "mac": "0" * 64,
        "digest": petitions._adjudications_digest(entries)}))

    assert _hub_blocked(reg, "hub-forged") == {"002": ["001"]}


def test_ralphctl_tasks_on_disk_drops_a_dependency_covered_by_a_clause_waiver(ctl):
    from test_cli_resume import _seed_run
    rdir, _ = _seed_run(ctl, "tst-settled-clause")
    (rdir / "tasks.json").write_text(_plan_text([_petitioned(), _dependent()]))
    RunDir(root=rdir).add_waiver(clause=OBLIGATION, finding="not a defect",
                                 judge="review")

    doc = json.loads(ctl.run("--json", "tasks", "tst-settled-clause").stdout)
    assert doc["live"] is False
    assert TASK_BLOCKED_FIELD not in doc
    res = ctl.run("tasks", "tst-settled-clause")
    assert res.returncode == 0, res.stderr
    line = next(ln for ln in res.stdout.splitlines() if "] 002 " in ln)
    assert "blocked" not in line


def test_ralphctl_tasks_on_disk_drops_a_dependency_covered_by_a_task_waiver(ctl):
    from test_cli_resume import _seed_run
    rdir, cdir = _seed_run(ctl, "tst-settled-task")
    (rdir / "tasks.json").write_text(_plan_text([_petitioned(), _dependent()]))
    write_amendment(cdir, "001", "accepted", waive=True)

    doc = json.loads(ctl.run("--json", "tasks", "tst-settled-task").stdout)
    assert TASK_BLOCKED_FIELD not in doc


def test_control_ralphctl_tasks_on_disk_still_shows_an_unsettled_blocker(ctl):
    from test_cli_resume import _seed_run
    rdir, _ = _seed_run(ctl, "tst-settled-none")
    (rdir / "tasks.json").write_text(_plan_text([_petitioned(), _dependent()]))

    doc = json.loads(ctl.run("--json", "tasks", "tst-settled-none").stdout)
    assert doc[TASK_BLOCKED_FIELD] == {"002": ["001"]}
