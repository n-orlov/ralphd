"""Task 025 (#33): every surface that counts task statuses reports BOTH
meanings of `failed`, and its tallies still add up.

Task 024 gave the two meanings a vocabulary (`failureKind`:
`validation-exhausted` = the engine spent this task's validation rounds,
`requirement-unmet` = a verifier judged the requirement not met) but left every
*counting* surface saying the one word `failed` for both, so an operator reading
`ralphctl status`, `ralphctl runs`, `ralphctl tasks`, `GET /status`, `GET /tasks`
or the hub could not tell which had happened without opening `tasks.json`.

What is pinned here:

  * the two invariants that make this safe to add: the status keys of
    `task_counts()` still sum to `total` (no sixth status was invented) and the
    kind SUB-counts sum to `failed`; both hold for a plan written before the
    label existed, because the kind is derived;
  * one vocabulary: `1 failed (validation-exhausted)` in a tally
    (`TASK_STATUS_LABELS`) and `failed (validation-exhausted)` on one record
    (`format_task_status`), rendered server-side for CLI, API and hub alike --
    and plain `2 failed` kept as the fallback for a counts dict with no
    sub-counts (a pre-v0.7 engine's `GET /status`), so one failed task is never
    counted or flagged twice;
  * per surface: `ralphctl status`, `ralphctl runs` (+`--json`), `ralphctl
    tasks` (+`--json`), `GET /status`, `GET /tasks`, the hub's `/api/runs` row
    and `/api/runs/<id>` detail, and the browser rendering itself;
  * the container-gone on-disk snapshot path for every CLI surface (a failed
    plan is exactly what an operator inspects after the run is over);
  * CLI parity: nothing the hub payload publishes about the two meanings is
    missing from `ralphctl --json`;
  * `repair`'s schema check learns the vocabulary together with `_TASK_STATUSES`.

Tiers: unit (the counters/formatters/readers), ASGI (the engine API), black-box
`ralphctl` subprocesses over hand-written registries with no container, the hub
server in-process, and one `browser`-tier test driving real Chromium.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path

import httpx
import pytest
from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run
from test_cli_status_dead_run import _seed_status

import ralphd.cli.main as cli_main
from ralphd.cli import ui_server
from ralphd.engine.api import create_app
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import (
    TASK_COUNT_SUBKEYS,
    TASK_FAILURE_KINDS,
    TASK_FAILURE_KINDS_FIELD,
    TASK_FAILURE_REQUIREMENT_UNMET,
    TASK_FAILURE_VALIDATION_EXHAUSTED,
    TASK_STATUS_LABELS,
    VALIDATION_ATTEMPT_LIMIT,
    RunDir,
    format_task_counts,
    format_task_status,
    format_task_trouble,
    read_tasks_doc,
    task_counts,
    task_failure_kinds,
)

__all__ = ["ctl", "unix_sock"]

REPO = Path(__file__).resolve().parents[1]
APP_JS = (REPO / "src" / "ralphd" / "cli" / "web" / "app.js").read_text()

EXHAUSTED = TASK_FAILURE_VALIDATION_EXHAUSTED
UNMET = TASK_FAILURE_REQUIREMENT_UNMET

# A plan with BOTH meanings in it, one of each spelled the two ways a record can
# spell it: 004 carries the engine's label, 005 is a PRE-v0.7 record with no
# label at all whose kind must be derived from its spent validation rounds.
BOTH_KINDS_PLAN = {
    "version": 1,
    "goal": "ship it",
    "tasks": [
        {"id": "001", "title": "a", "status": "completed"},
        {"id": "002", "title": "b", "status": "completed"},
        {"id": "003", "title": "c", "status": "in-progress"},
        {"id": "004", "title": "d", "status": "failed",
         "failureKind": UNMET},
        {"id": "005", "title": "e", "status": "failed",
         "validationAttempts": VALIDATION_ATTEMPT_LIMIT},
        {"id": "006", "title": "f", "status": "pending"},
    ],
}
CLEAN_PLAN = {
    "version": 1,
    "tasks": [{"id": "001", "title": "a", "status": "completed"},
              {"id": "002", "title": "b", "status": "in-progress"}],
}


def _plan_json(doc: dict) -> str:
    return json.dumps(doc, indent=2) + "\n"


# --------------------------------------------------------------------------
# the counters: both meanings counted, every tally still adding up
# --------------------------------------------------------------------------

def test_counts_carry_the_kind_subcounts_for_both_spellings():
    counts = task_counts(BOTH_KINDS_PLAN["tasks"])
    assert counts == {
        "total": 6, "completed": 2, "inProgress": 1, "failed": 2, "pending": 1,
        "failedRequirementUnmet": 1, "failedValidationExhausted": 1,
    }


@pytest.mark.parametrize("plan", [BOTH_KINDS_PLAN["tasks"], CLEAN_PLAN["tasks"],
                                  [{"id": "x", "status": "failed"}],
                                  [{"id": "y"}], []])
def test_the_status_keys_still_sum_to_total_and_the_subcounts_to_failed(plan):
    """The invariant that made this addable without a sixth status: a failure
    KIND is not a status, so it is counted beside the statuses, never among
    them."""
    counts = task_counts(plan)
    statuses = {k: v for k, v in counts.items()
                if k not in ("total", *TASK_COUNT_SUBKEYS)}
    assert sum(statuses.values()) == counts["total"]
    subs = sum(counts.get(k, 0) for k in TASK_COUNT_SUBKEYS)
    assert subs == counts.get("failed", 0)


def test_a_plan_with_nothing_failed_counts_exactly_as_before():
    """No sub-count keys appear at all, so a v0.6 consumer of these counts (and
    every existing assertion about them) sees byte-identical output."""
    counts = task_counts(CLEAN_PLAN["tasks"])
    assert counts == {"total": 2, "completed": 1, "inProgress": 1}
    assert not [k for k in counts if k in TASK_COUNT_SUBKEYS]


def test_a_legacy_failed_record_is_counted_under_the_derived_kind():
    """Neither record carries `failureKind`; the kind comes from the evidence
    (`validationAttempts` at/past the limit = the engine's own verdict)."""
    counts = task_counts([
        {"id": "1", "status": "failed", "validationAttempts": VALIDATION_ATTEMPT_LIMIT},
        {"id": "2", "status": "failed", "validationAttempts": 1},
    ])
    assert counts["failedValidationExhausted"] == 1
    assert counts["failedRequirementUnmet"] == 1
    assert counts["failed"] == 2


def test_a_garbled_failure_label_is_counted_never_dropped():
    counts = task_counts([{"id": "1", "status": "failed", "failureKind": "banana"}])
    assert counts["failed"] == 1
    assert sum(counts.get(k, 0) for k in TASK_COUNT_SUBKEYS) == 1


def test_the_per_task_map_is_derived_and_covers_only_failed_tasks():
    assert task_failure_kinds(BOTH_KINDS_PLAN["tasks"]) == {
        "004": UNMET, "005": EXHAUSTED}
    assert task_failure_kinds(CLEAN_PLAN["tasks"]) == {}
    # an id-less record has nothing for a consumer to key on
    assert task_failure_kinds([{"status": "failed"}]) == {}
    assert task_failure_kinds(["not a dict"]) == {}


# --------------------------------------------------------------------------
# the wording: one vocabulary, and the fallback that keeps it honest
# --------------------------------------------------------------------------

def test_a_summary_names_the_kinds_instead_of_a_bare_failed():
    summary = format_task_counts(task_counts(BOTH_KINDS_PLAN["tasks"]))
    assert summary == ("2/6 completed (1 in-progress, 1 pending, "
                       "1 failed (requirement-unmet), "
                       "1 failed (validation-exhausted))")
    # the entries in the parenthesis account for every task not completed:
    # 1 + 1 + 1 + 1 == 6 - 2, which a duplicated `2 failed` would break
    assert "2 failed" not in summary


def test_a_summary_falls_back_to_plain_failed_without_subcounts():
    """A counts dict from a pre-v0.7 engine's `GET /status` has no sub-counts;
    saying `2 failed` there is the honest answer, not silence."""
    assert format_task_counts({"total": 3, "completed": 1, "failed": 2}) == (
        "1/3 completed (2 failed)")


def test_the_tally_wording_and_the_task_wording_are_the_same_words():
    for kind, key in ((EXHAUSTED, "failedValidationExhausted"),
                      (UNMET, "failedRequirementUnmet")):
        assert TASK_STATUS_LABELS[key] == f"failed ({kind})"
        assert format_task_status({"status": "failed", "failureKind": kind}) == \
            f"failed ({kind})"


@pytest.mark.parametrize("task,expected", [
    ({"status": "completed"}, "completed"),
    ({"status": "in-progress"}, "in-progress"),
    ({"status": "failed", "failureKind": UNMET}, f"failed ({UNMET})"),
    ({"status": "failed", "validationAttempts": VALIDATION_ATTEMPT_LIMIT},
     f"failed ({EXHAUSTED})"),
    ({"status": "failed"}, f"failed ({UNMET})"),
    ({}, "unknown"),
    ({"status": None}, "unknown"),
    ("junk", "unknown"),
])
def test_format_task_status_says_which_kind_of_failed(task, expected):
    assert format_task_status(task) == expected


def test_a_failed_plan_is_trouble_under_both_meanings_and_flagged_once():
    counts = task_counts(BOTH_KINDS_PLAN["tasks"])
    assert format_task_trouble(counts) == [f"1 failed ({UNMET})",
                                           f"1 failed ({EXHAUSTED})",
                                           "1 in-progress"]
    # ... and never twice: no bare `failed` flag beside the kinds
    assert not [flag for flag in format_task_trouble(counts) if flag == "2 failed"]
    # a pre-v0.7 counts dict still gets flagged, by the fallback
    assert format_task_trouble({"total": 2, "failed": 1}) == ["1 failed"]
    # nothing failed, nothing invented
    assert format_task_trouble(task_counts(CLEAN_PLAN["tasks"])) == ["1 in-progress"]


def test_the_column_marks_a_plan_that_gave_up(tmp_path):
    """`ralphctl runs`/the hub cell cannot fit the sentences, so the marker is
    all the column has -- and a plan with a failed task must get it."""
    from ralphd.engine.state import TASK_TROUBLE_MARKER, format_task_column
    only_failed = task_counts([{"id": "1", "status": "completed"},
                               {"id": "2", "status": "failed",
                                "failureKind": UNMET}])
    assert format_task_column(only_failed) == f"1/2 {TASK_TROUBLE_MARKER}"


# --------------------------------------------------------------------------
# the shared read: one payload shape for /tasks, `ralphctl tasks` and the hub
# --------------------------------------------------------------------------

def test_the_read_payload_appends_the_derived_kinds_last(tmp_path):
    (tmp_path / "tasks.json").write_text(_plan_json(BOTH_KINDS_PLAN))
    res = read_tasks_doc(tmp_path, persist=False)
    payload = res.payload
    assert {k: payload[k] for k in BOTH_KINDS_PLAN} == BOTH_KINDS_PLAN  # verbatim
    assert (payload["tasksStale"], payload["tasksSource"]) == (False, "file")
    assert payload[TASK_FAILURE_KINDS_FIELD] == {"004": UNMET, "005": EXHAUSTED}


def test_a_plan_key_cannot_forge_the_failure_kinds(tmp_path):
    forged = {**BOTH_KINDS_PLAN, TASK_FAILURE_KINDS_FIELD: {"004": EXHAUSTED}}
    (tmp_path / "tasks.json").write_text(_plan_json(forged))
    payload = read_tasks_doc(tmp_path, persist=False).payload
    assert payload[TASK_FAILURE_KINDS_FIELD] == {"004": UNMET, "005": EXHAUSTED}


def test_the_kinds_field_is_absent_when_nothing_failed(tmp_path):
    (tmp_path / "tasks.json").write_text(_plan_json(CLEAN_PLAN))
    payload = read_tasks_doc(tmp_path, persist=False).payload
    assert TASK_FAILURE_KINDS_FIELD not in payload


def test_a_run_list_row_carries_the_raw_failure_counts(tmp_path):
    (tmp_path / "tasks.json").write_text(_plan_json(BOTH_KINDS_PLAN))
    fields = read_tasks_doc(tmp_path, persist=False).row_fields
    assert (fields["tasksFailed"], fields["tasksFailedValidationExhausted"],
            fields["tasksFailedRequirementUnmet"]) == (2, 1, 1)
    assert fields["tasksFailed"] == (fields["tasksFailedValidationExhausted"]
                                     + fields["tasksFailedRequirementUnmet"])
    # ... and an ASYMMETRIC plan, so a table sorting on either column cannot be
    # served the other one's number (1/1 would hide the swap).
    lopsided = {"tasks": [
        {"id": "001", "title": "a", "status": "failed", "failureKind": EXHAUSTED},
        {"id": "002", "title": "b", "status": "failed", "failureKind": EXHAUSTED},
        {"id": "003", "title": "c", "status": "failed", "failureKind": UNMET},
    ]}
    sub = tmp_path / "lopsided"
    sub.mkdir()
    (sub / "tasks.json").write_text(_plan_json(lopsided))
    lop = read_tasks_doc(sub, persist=False).row_fields
    assert (lop["tasksFailedValidationExhausted"],
            lop["tasksFailedRequirementUnmet"]) == (2, 1)
    clean = read_tasks_doc(tmp_path.parent, persist=False).row_fields
    assert clean["tasksFailed"] == 0


# --------------------------------------------------------------------------
# `repair`'s schema check learns the vocabulary with _TASK_STATUSES
# --------------------------------------------------------------------------

def test_repairs_schema_check_names_a_bad_failure_label(tmp_path):
    (tmp_path / "tasks.json").write_text(json.dumps({"tasks": [
        {"id": "001", "title": "a", "status": "failed", "failureKind": "banana"},
        {"id": "002", "title": "b", "status": "pending", "failureKind": UNMET},
        {"id": "003", "title": "c", "status": "failed", "failureKind": EXHAUSTED},
    ]}))
    issues = cli_main._diagnose_tasks_json(tmp_path)
    assert any("001" in i and "failureKind 'banana'" in i for i in issues), issues
    assert any("002" in i and "not 'failed'" in i for i in issues), issues
    assert not [i for i in issues if "003" in i], issues
    assert set(TASK_FAILURE_KINDS) == {EXHAUSTED, UNMET}


# --------------------------------------------------------------------------
# the engine API
# --------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    run = RunDir(root=tmp_path)
    run.update_status(state="running")
    sup = LoopSupervisor(JobConfig(run_id="unit"), run, tmp_path)
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

    return get


def test_get_status_publishes_the_kind_subcounts(client, tmp_path):
    (tmp_path / "tasks.json").write_text(_plan_json(BOTH_KINDS_PLAN))
    counts = client("/status")["tasks"]
    assert counts["failed"] == 2
    assert counts["failedRequirementUnmet"] == 1
    assert counts["failedValidationExhausted"] == 1


def test_get_tasks_publishes_the_derived_kinds_and_stays_verbatim(client, tmp_path):
    (tmp_path / "tasks.json").write_text(_plan_json(BOTH_KINDS_PLAN))
    doc = client("/tasks")
    assert {k: doc[k] for k in BOTH_KINDS_PLAN} == BOTH_KINDS_PLAN
    assert doc[TASK_FAILURE_KINDS_FIELD] == {"004": UNMET, "005": EXHAUSTED}
    assert (doc["tasksStale"], doc["tasksSource"]) == (False, "file")


def test_get_tasks_of_a_plan_with_nothing_failed_is_unchanged(client, tmp_path):
    (tmp_path / "tasks.json").write_text(_plan_json(CLEAN_PLAN))
    doc = client("/tasks")
    assert TASK_FAILURE_KINDS_FIELD not in doc
    assert TASK_FAILURE_KINDS_FIELD not in client("/status")


# --------------------------------------------------------------------------
# the CLI, container gone (the on-disk snapshot path)
# --------------------------------------------------------------------------

def test_status_of_a_dead_run_names_both_meanings(ctl: Ctl):
    _seed_status(ctl, "tst-kinds", tasks_json=BOTH_KINDS_PLAN, tasks=None)
    res = ctl.run("status", "tst-kinds")
    assert res.returncode == 0, res.stderr
    line = next(ln for ln in res.stdout.splitlines() if ln.startswith("tasks:"))
    assert f"1 failed ({UNMET})" in line, line
    assert f"1 failed ({EXHAUSTED})" in line, line
    assert "2 failed," not in line and "(2 failed" not in line, line


def test_status_json_of_a_dead_run_carries_the_subcounts(ctl: Ctl):
    _seed_status(ctl, "tst-kinds-json", tasks_json=BOTH_KINDS_PLAN, tasks=None)
    doc = json.loads(ctl.run("--json", "status", "tst-kinds-json").stdout)
    assert doc["tasks"] == task_counts(BOTH_KINDS_PLAN["tasks"])


def test_tasks_of_a_dead_run_says_which_kind_of_failed(ctl: Ctl):
    rdir, _ = _seed_run(ctl, "tst-tasks-kinds")
    (rdir / "tasks.json").write_text(_plan_json(BOTH_KINDS_PLAN))
    res = ctl.run("tasks", "tst-tasks-kinds")
    assert res.returncode == 0, res.stderr
    rows = {ln.split("] ")[1].split(" ")[0]: ln.split("]")[0].strip("[").strip()
            for ln in res.stdout.splitlines()}
    assert rows["004"] == f"failed ({UNMET})"
    assert rows["005"] == f"failed ({EXHAUSTED})"      # derived, no label on it
    assert rows["003"] == "in-progress"
    # aligned: every status cell is padded to the same width
    widths = {len(ln.split("]")[0]) for ln in res.stdout.splitlines()}
    assert len(widths) == 1, res.stdout


def test_tasks_of_a_plan_without_failures_prints_the_historical_format(ctl: Ctl):
    rdir, _ = _seed_run(ctl, "tst-tasks-clean")
    (rdir / "tasks.json").write_text(_plan_json(CLEAN_PLAN))
    res = ctl.run("tasks", "tst-tasks-clean")
    assert res.stdout == ("[completed        ] 001 a\n"
                          "[in-progress      ] 002 b\n")


def test_tasks_json_carries_the_derived_kinds(ctl: Ctl):
    rdir, _ = _seed_run(ctl, "tst-tasks-json")
    (rdir / "tasks.json").write_text(_plan_json(BOTH_KINDS_PLAN))
    doc = json.loads(ctl.run("--json", "tasks", "tst-tasks-json").stdout)
    assert doc["live"] is False
    assert doc[TASK_FAILURE_KINDS_FIELD] == {"004": UNMET, "005": EXHAUSTED}


def test_runs_flags_a_plan_that_gave_up(ctl: Ctl):
    rdir, _ = _seed_run(ctl, "aaa-gaveup")
    (rdir / "status.json").write_text(json.dumps({
        "runId": "aaa-gaveup", "state": "failed", "verdict": "unverified",
        "phase": "review", "iterationsUsed": 9, "iterationsBudget": 25,
        "startedAt": "2026-01-01T00:00:00Z", "schemaVersion": 1}))
    (rdir / "tasks.json").write_text(_plan_json(BOTH_KINDS_PLAN))
    res = ctl.run("runs")
    assert res.returncode == 0, res.stderr
    row = next(ln for ln in res.stdout.splitlines()
               if ln.startswith("aaa-gaveup"))
    assert "2/6 \u26a0" in row, row
    row_json = json.loads(ctl.run("--json", "runs").stdout)[0]
    assert row_json["tasksTrouble"] == [f"1 failed ({UNMET})",
                                        f"1 failed ({EXHAUSTED})",
                                        "1 in-progress"]
    assert (row_json["tasksFailed"], row_json["tasksFailedRequirementUnmet"],
            row_json["tasksFailedValidationExhausted"]) == (2, 1, 1)


# --------------------------------------------------------------------------
# the hub payloads, and CLI parity
# --------------------------------------------------------------------------

def _seed_registry(tmp_path: Path, run_id: str, plan: dict) -> Path:
    reg = tmp_path / "registry"
    rdir = reg / "runs" / run_id
    rdir.mkdir(parents=True)
    (rdir / "status.json").write_text(json.dumps({
        "runId": run_id, "state": "failed", "verdict": "unverified",
        "phase": "review", "iterationsUsed": 9, "iterationsBudget": 25,
        "startedAt": "2026-01-01T00:00:00Z", "schemaVersion": 1}))
    (rdir / "tasks.json").write_text(_plan_json(plan))
    (rdir / "host.json").write_text(json.dumps({
        "runId": run_id, "container": f"ralphd-{run_id}", "port": 1,
        "apiUrl": "http://127.0.0.1:1", "image": "img", "startedAt": "x"}))
    return reg


def test_the_hub_run_row_and_the_cli_row_agree_about_both_meanings(tmp_path):
    reg = _seed_registry(tmp_path, "hub-kinds", BOTH_KINDS_PLAN)
    row = ui_server.run_list(reg)[0]
    assert (row["tasksFailed"], row["tasksFailedRequirementUnmet"],
            row["tasksFailedValidationExhausted"]) == (2, 1, 1)
    assert row["tasksTrouble"] == [f"1 failed ({UNMET})",
                                   f"1 failed ({EXHAUSTED})", "1 in-progress"]
    assert row == {**row, **read_tasks_doc(
        reg / "runs" / "hub-kinds", persist=False).row_fields}


def test_the_hub_run_detail_derives_the_kinds_itself(tmp_path):
    reg = _seed_registry(tmp_path, "hub-detail", BOTH_KINDS_PLAN)
    detail = ui_server.run_detail(reg, "hub-detail")
    assert detail["tasks"][TASK_FAILURE_KINDS_FIELD] == {"004": UNMET,
                                                        "005": EXHAUSTED}
    # the records themselves are still served verbatim beside the derived map
    assert detail["tasks"]["tasks"] == BOTH_KINDS_PLAN["tasks"]


def test_a_forged_kinds_key_in_the_plan_is_replaced_by_the_derived_map(tmp_path):
    forged = {**BOTH_KINDS_PLAN, TASK_FAILURE_KINDS_FIELD: {"004": EXHAUSTED,
                                                            "999": UNMET}}
    reg = _seed_registry(tmp_path, "hub-forged", forged)
    detail = ui_server.run_detail(reg, "hub-forged")
    assert detail["tasks"][TASK_FAILURE_KINDS_FIELD] == {"004": UNMET,
                                                        "005": EXHAUSTED}


def test_a_live_pre_v07_engine_answer_still_gets_the_kinds(tmp_path):
    """The hub re-derives from the payload's own task list, so a live answer
    from an engine that never heard of `taskFailureKinds` (or of `failureKind`)
    is not shown as an unexplained `failed`."""
    legacy = {"tasks": BOTH_KINDS_PLAN["tasks"], "tasksStale": False,
              "tasksSource": "file"}
    assert ui_server._with_task_failure_kinds(legacy)[TASK_FAILURE_KINDS_FIELD] \
        == {"004": UNMET, "005": EXHAUSTED}
    clean = ui_server._with_task_failure_kinds({"tasks": CLEAN_PLAN["tasks"]})
    assert TASK_FAILURE_KINDS_FIELD not in clean


def test_a_live_engine_answer_is_labelled_at_the_hubs_call_site(tmp_path,
                                                                monkeypatch):
    """The on-disk read gets its kinds from `TasksRead.payload`; a live answer
    gets them at the proxy call site. Exactly one derivation on each path --
    which is why removing either one has to be a test failure."""
    reg = _seed_registry(tmp_path, "hub-live", CLEAN_PLAN)
    live = {"tasks": BOTH_KINDS_PLAN["tasks"],
            # a pre-v0.7 engine has no such field; an agent that wrote one into
            # tasks.json itself gets it overwritten, not proxied through
            TASK_FAILURE_KINDS_FIELD: {"004": EXHAUSTED, "999": UNMET}}

    def fake_proxy(reg_, run_id, method, path, **kw):
        if path == "/tasks":
            return True, 200, json.loads(json.dumps(live))
        return False, 0, None

    monkeypatch.setattr(ui_server, "_proxy_json", fake_proxy)
    detail = ui_server.run_detail(reg, "hub-live")
    assert detail["tasks"][TASK_FAILURE_KINDS_FIELD] == {"004": UNMET,
                                                        "005": EXHAUSTED}
    assert detail["tasks"]["tasks"] == BOTH_KINDS_PLAN["tasks"]


def test_the_hub_exposes_no_failure_vocabulary_the_cli_lacks(ctl: Ctl, tmp_path):
    """CLI parity (PRD requirement): every field the hub publishes about the
    two meanings is reachable from `ralphctl --json`."""
    reg = _seed_registry(tmp_path / "hub", "parity", BOTH_KINDS_PLAN)
    hub_row = ui_server.run_list(reg)[0]
    hub_detail = ui_server.run_detail(reg, "parity")

    rdir, _ = _seed_run(ctl, "parity")
    (rdir / "status.json").write_text(
        (reg / "runs" / "parity" / "status.json").read_text())
    (rdir / "tasks.json").write_text(_plan_json(BOTH_KINDS_PLAN))
    cli_row = json.loads(ctl.run("--json", "runs").stdout)[0]
    cli_tasks = json.loads(ctl.run("--json", "tasks", "parity").stdout)
    cli_status = json.loads(ctl.run("--json", "status", "parity").stdout)

    for key in ("tasksFailed", "tasksFailedValidationExhausted",
                "tasksFailedRequirementUnmet", "tasksTrouble", "tasksSummary"):
        assert hub_row[key] == cli_row[key], key
    assert hub_detail["tasks"][TASK_FAILURE_KINDS_FIELD] == \
        cli_tasks[TASK_FAILURE_KINDS_FIELD]
    for key in TASK_COUNT_SUBKEYS:
        assert cli_status["tasks"][key] == task_counts(
            BOTH_KINDS_PLAN["tasks"])[key]


# --------------------------------------------------------------------------
# the browser rendering: server-derived, never re-derived in JS
# --------------------------------------------------------------------------

def test_the_task_row_and_dialog_render_the_server_derived_kind():
    rows = APP_JS.split("function renderTasks(")[1].split("\nfunction ")[0]
    assert "taskFailureKind(doc, t)" in rows, rows
    assert 'data-failure-kind' in rows, rows
    assert "pill(kind)" in rows, rows
    dialog = APP_JS.split("function taskDialogText(")[1].split("\n}")[0]
    assert "taskStatusText(doc, t)" in dialog, dialog


def test_the_bundle_never_re_derives_the_migration_rule():
    """The kind arrives derived (`doc.taskFailureKinds`); a JS copy of the
    "at/past three validation attempts" rule is exactly the second vocabulary
    this task exists to prevent. (Named without the word the fast lane's
    `-k 'not browser'` filter matches: this reads app.js as text.)"""
    block = APP_JS.split("function taskFailureKind(")[1].split("\nfunction ")[0]
    status_block = APP_JS.split("function taskStatusText(")[1].split("\nfunction ")[0]
    assert "taskFailureKinds" in block, block
    # a kind is shown for a `failed` task and for nothing else, so a stray
    # `failureKind` left on a pending task is not rendered as a failure
    assert 'if (status !== "failed") return null;' in block, block
    for forbidden in ("validationAttempts", "VALIDATION_ATTEMPT_LIMIT", ">= 3"):
        assert forbidden not in block, forbidden
        assert forbidden not in status_block, forbidden
    # ... and the limit itself is nowhere in the bundle: the browser has no
    # business knowing how many validation rounds a task gets.
    for forbidden in ("VALIDATION_ATTEMPT_LIMIT", ">= 3"):
        assert forbidden not in APP_JS, forbidden


# --------------------------------------------------------------------------
# browser tier: the rendering, in a real browser
# --------------------------------------------------------------------------

@pytest.mark.browser
@pytest.mark.skipif(shutil.which("playwright-cli") is None,
                    reason="playwright-cli not on PATH")
def test_a_failed_task_row_names_its_kind_in_the_browser(tmp_path):
    from test_browser_hub import Pw, _wait_for
    from test_cli_ui import UiServer, _write_dead_run

    registry = tmp_path / "registry"
    run = _write_dead_run(registry, "run-kinds", state="failed",
                          verdict="unverified")
    (run / "tasks.json").write_text(_plan_json(BOTH_KINDS_PLAN))

    server = UiServer(registry)
    server.wait_ready()
    pw = Pw(f"kinds-{os.getpid()}-{time.time_ns()}")
    try:
        pw.open(f"{server.base}/#/run/run-kinds")
        _wait_for(pw, "document.body.innerText", UNMET)
        for tid, kind in (("004", UNMET), ("005", EXHAUSTED)):
            assert kind in pw.eval_js(
                f"document.querySelector('tr.task-row[data-task-id=\"{tid}\"]')"
                ".getAttribute('data-failure-kind')")
            assert kind in pw.eval_js(
                f"document.querySelector('tr.task-row[data-task-id=\"{tid}\"]')"
                ".textContent")
        # ... and the dialog says the same words `ralphctl tasks` prints
        pw.click('tr.task-row[data-task-id="005"]')
        body = _wait_for(pw, "document.querySelector('.text-dialog').textContent",
                         "Task 005")
        assert f"status: failed ({EXHAUSTED})" in body, body
    finally:
        pw.close()
        server.proc.terminate()


# --------------------------------------------------------------------------
# the docs say the rule (one check per claim)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path,needles", [
    ("SPEC.md",
     ["**Counting both meanings of `failed` (task 025, #33).**",
      "`failedValidationExhausted`/`failedRequirementUnmet`",
      "the status keys sum to `total`",
      "the sub-counts sum to `failed`",
      "`state.task_failure_kinds()`",
      "published as\n`taskFailureKinds`"]),
    ("docs/api.md",
     ["#### Both meanings of a `failed` task (`taskFailureKinds`, task 025, issue #33)",
      "**absent entirely** when nothing failed",
      "sub-counts of `failed`",
      '"failedRequirementUnmet": 1']),
    ("docs/cli.md",
     ["#### Both meanings of a `failed` task (task 025, issue #33)",
      "`failedValidationExhausted` /\n  `failedRequirementUnmet`",
      "the column is as wide as its widest",
      "`{\"014\": \"validation-exhausted\"}`, omitted entirely when nothing failed",
      "A **failed** row carries a second pill naming which kind of failed it is",
      "tasksFailedValidationExhausted, tasksFailedRequirementUnmet,"]),
    ("docs/architecture.md",
     ["Every surface that *counts* task statuses reports both meanings (task 025, #33)",
      "sub-counts `failedValidationExhausted`/`failedRequirementUnmet`, which sum to",
      "so the\nbrowser renders a derived kind rather than re-deriving it in JS"]),
])
def test_the_docs_state_the_counting_rule(path, needles):
    text = (REPO / path).read_text()
    for needle in needles:
        assert needle in text, f"{path}: missing {needle!r}"
