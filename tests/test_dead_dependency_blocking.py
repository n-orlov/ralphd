"""Task 021 (#60 pillar 3, PRD requirement F): dead dependencies are
harness-derived, not agent-noticed.

The PRD's own wording: "when a dependency reaches `failed` or `skipped`, the
harness marks dependents rather than the worker prompt asking an agent to
notice. Dead subtrees are visible in `ralphctl tasks` and the hub." and
"Per-task validation status is visible on every surface for the first time --
`awaiting-validation` vs `validated` vs `blocked`."

What is pinned here, one test (or pair) per surface:

  * the derivation itself (`engine.state.task_blocked_by`): direct, transitive
    through another merely-blocked (not dead) task, a completed/validated
    dependency is a SINK (does not propagate), a `skipped` dependency dooms
    exactly like a `failed` one, a dead task is a SOURCE never a KEY of its
    own map, no id, no dependsOn, and a cycle-shaped input (the lint at
    ingestion rejects cycles, but the derivation itself must still terminate
    rather than hang if one ever reaches it);
  * `task_counts()`'s `blocked` OVERLAY count, and the two invariants that
    make it safe to add (status keys still sum to `total`, sub/overlay keys
    excluded via `TASK_COUNT_OVERLAY_KEYS`), for a plan written before this
    existed too;
  * `format_task_status()`'s ` (blocked by ...)` suffix;
  * `TasksRead.payload`/`blocked_by`: the on-disk derivation, omitted when
    empty;
  * the engine API (`GET /tasks`, `GET /status`);
  * `ralphctl tasks` (text and `--json`) and `ralphctl status`, container-gone
    (on-disk snapshot) path;
  * the hub's server-side re-derivation for a LIVE payload
    (`ui_server._with_task_blocked_by`);
  * the shipped prompt no longer instructs an agent to notice (mutation case:
    the phrase this task removed, if reintroduced, is caught).

The hub's own browser-rendered pill/row-marker is covered separately by
`tests/test_browser_hub.py::test_run_detail_marks_a_dead_dependencys_subtree_blocked`
(browser tier, `@pytest.mark.browser`).
"""

from __future__ import annotations

import asyncio
import json
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
    TASK_BLOCKED_FIELD,
    TASK_COUNT_OVERLAY_KEYS,
    RunDir,
    format_task_status,
    read_tasks_doc,
    task_blocked_by,
    task_counts,
)

__all__ = ["ctl", "unix_sock"]

REPO = Path(__file__).resolve().parents[1]
WORKER_MD = (REPO / "src" / "ralphd" / "prompts" / "worker.md").read_text()


def _plan_json(doc: dict) -> str:
    return json.dumps(doc, indent=2) + "\n"


# A chain: 004 dies outright; 005 depends on it directly (blocked); 006
# depends on 005 (blocked transitively, through a task that is itself only
# blocked, not dead); 007 depends on 001 (a completed, SATISFIED dependency)
# and is untouched; 008 depends on 002, which is `skipped` -- a `skipped`
# dependency dooms exactly like a `failed` one; 009 has no dependsOn at all.
CHAIN_PLAN = {
    "version": 1,
    "goal": "ship it",
    "tasks": [
        {"id": "001", "title": "a", "status": "completed"},
        {"id": "002", "title": "b", "status": "skipped"},
        {"id": "004", "title": "d", "status": "failed"},
        {"id": "005", "title": "e", "status": "pending", "dependsOn": ["004"]},
        {"id": "006", "title": "f", "status": "in-progress", "dependsOn": ["005"]},
        {"id": "007", "title": "g", "status": "pending", "dependsOn": ["001"]},
        {"id": "008", "title": "h", "status": "validation-failed",
         "dependsOn": ["002"]},
        {"id": "009", "title": "i", "status": "pending"},
    ],
}


# --------------------------------------------------------------------------
# the derivation itself
# --------------------------------------------------------------------------

def test_direct_and_transitive_dead_dependencies_are_marked():
    blocked = task_blocked_by(CHAIN_PLAN["tasks"])
    assert blocked["005"] == ["004"]
    assert blocked["006"] == ["005"]           # transitive, through a merely
                                                # blocked (not dead) task
    assert blocked["008"] == ["002"]           # `skipped` dooms too
    assert "007" not in blocked                # a SATISFIED dependency
    assert "009" not in blocked                # no dependsOn at all


def test_a_dead_task_is_a_source_never_a_key_of_its_own_map():
    blocked = task_blocked_by(CHAIN_PLAN["tasks"])
    assert "004" not in blocked
    assert "002" not in blocked


def test_a_completed_or_validated_dependency_is_a_sink():
    """A task that finished despite a `dependsOn` on a dead task is not
    doomed itself, and -- crucially -- doom does not propagate PAST it to
    ITS OWN dependents either: the record says the work got done."""
    tasks = [
        {"id": "001", "status": "failed"},
        {"id": "002", "status": "completed", "dependsOn": ["001"]},
        {"id": "003", "status": "pending", "dependsOn": ["002"]},
        {"id": "004", "status": "validated", "dependsOn": ["001"]},
        {"id": "005", "status": "pending", "dependsOn": ["004"]},
    ]
    blocked = task_blocked_by(tasks)
    assert "002" not in blocked
    assert "003" not in blocked
    assert "004" not in blocked
    assert "005" not in blocked


def test_no_id_no_dependson_no_dict_are_all_handled_quietly():
    assert task_blocked_by([]) == {}
    assert task_blocked_by([{"status": "failed"}]) == {}          # no id
    assert task_blocked_by(["not a dict"]) == {}
    assert task_blocked_by([{"id": "1", "status": "pending"}]) == {}  # no deps


def test_a_cyclic_input_still_terminates_with_no_answer():
    """Semantic lint (`plan_ingestion`) rejects a `dependsOn` cycle at
    ingestion, so this should never reach the derivation live -- but the
    derivation itself must not hang or crash if a hand-edited/legacy plan
    ever does carry one: neither task in the cycle is dead, so the fixpoint
    adds nothing and returns empty rather than looping forever."""
    tasks = [
        {"id": "1", "status": "pending", "dependsOn": ["2"]},
        {"id": "2", "status": "pending", "dependsOn": ["1"]},
    ]
    assert task_blocked_by(tasks) == {}


def test_answers_are_sorted_and_stringified():
    tasks = [
        {"id": "1", "status": "failed"},
        {"id": 2, "status": "skipped"},
        {"id": "3", "status": "pending", "dependsOn": [2, "1"]},
    ]
    assert task_blocked_by(tasks) == {"3": ["1", "2"]}


# --------------------------------------------------------------------------
# task_counts()'s overlay count, and its invariants
# --------------------------------------------------------------------------

def test_counts_carry_a_blocked_overlay():
    counts = task_counts(CHAIN_PLAN["tasks"])
    assert counts["blocked"] == 3          # 005, 006, 008


def test_a_plan_with_no_dead_dependency_counts_exactly_as_before():
    clean = [{"id": "1", "status": "completed"}, {"id": "2", "status": "pending"}]
    counts = task_counts(clean)
    assert counts == {"total": 2, "completed": 1, "pending": 1}
    assert "blocked" not in counts


@pytest.mark.parametrize("plan", [CHAIN_PLAN["tasks"],
                                  [{"id": "1", "status": "completed"}], []])
def test_the_status_keys_still_sum_to_total_with_blocked_present(plan):
    counts = task_counts(plan)
    statuses = {k: v for k, v in counts.items()
                if k not in ("total", *TASK_COUNT_OVERLAY_KEYS)}
    assert sum(statuses.values()) == counts["total"]


# --------------------------------------------------------------------------
# format_task_status()
# --------------------------------------------------------------------------

def test_format_task_status_appends_the_blocked_by_suffix():
    task = {"id": "005", "status": "pending"}
    assert format_task_status(task) == "pending"
    assert format_task_status(task, ["004"]) == "pending (blocked by 004)"
    assert format_task_status(task, []) == "pending"
    assert format_task_status(task, None) == "pending"


# --------------------------------------------------------------------------
# TasksRead / the on-disk derivation
# --------------------------------------------------------------------------

def test_tasks_read_payload_carries_the_blocked_by_map(tmp_path):
    (tmp_path / "tasks.json").write_text(_plan_json(CHAIN_PLAN))
    res = read_tasks_doc(tmp_path)
    assert res.blocked_by == task_blocked_by(CHAIN_PLAN["tasks"])
    assert res.payload[TASK_BLOCKED_FIELD] == res.blocked_by


def test_tasks_read_payload_omits_the_key_when_nothing_is_blocked(tmp_path):
    clean = {"tasks": [{"id": "1", "status": "completed"}]}
    (tmp_path / "tasks.json").write_text(json.dumps(clean))
    res = read_tasks_doc(tmp_path)
    assert TASK_BLOCKED_FIELD not in res.payload


# --------------------------------------------------------------------------
# the engine API
# --------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    run = RunDir(root=tmp_path)
    run.update_status(state="running")
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

    return get


def test_get_tasks_publishes_the_derived_blocked_by_map(client, tmp_path):
    (tmp_path / "tasks.json").write_text(_plan_json(CHAIN_PLAN))
    doc = client("/tasks")
    assert doc[TASK_BLOCKED_FIELD] == task_blocked_by(CHAIN_PLAN["tasks"])


def test_get_status_publishes_the_blocked_overlay_count(client, tmp_path):
    (tmp_path / "tasks.json").write_text(_plan_json(CHAIN_PLAN))
    counts = client("/status")["tasks"]
    assert counts["blocked"] == 3


def test_get_tasks_of_a_clean_plan_carries_no_blocked_key(client, tmp_path):
    clean = {"tasks": [{"id": "1", "status": "completed"}]}
    (tmp_path / "tasks.json").write_text(json.dumps(clean))
    doc = client("/tasks")
    assert TASK_BLOCKED_FIELD not in doc
    assert TASK_BLOCKED_FIELD not in client("/status")


# --------------------------------------------------------------------------
# the CLI, container gone (the on-disk snapshot path)
# --------------------------------------------------------------------------

def test_tasks_of_a_dead_run_shows_the_blocked_suffix(ctl: Ctl):
    rdir, _ = _seed_run(ctl, "tst-blocked-tasks")
    (rdir / "tasks.json").write_text(_plan_json(CHAIN_PLAN))
    res = ctl.run("tasks", "tst-blocked-tasks")
    assert res.returncode == 0, res.stderr
    lines = {ln.split("] ")[1].split(" ")[0]: ln.split("]")[0].strip("[").strip()
             for ln in res.stdout.splitlines()}
    assert lines["005"] == "pending (blocked by 004)"
    assert lines["006"] == "in-progress (blocked by 005)"
    assert lines["007"] == "pending"
    assert lines["008"] == "validation-failed (blocked by 002)"
    # aligned: every status cell padded to the same width
    widths = {len(ln.split("]")[0]) for ln in res.stdout.splitlines()}
    assert len(widths) == 1, res.stdout


def test_tasks_json_of_a_dead_run_carries_the_blocked_by_map(ctl: Ctl):
    rdir, _ = _seed_run(ctl, "tst-blocked-tasks-json")
    (rdir / "tasks.json").write_text(_plan_json(CHAIN_PLAN))
    doc = json.loads(ctl.run("--json", "tasks", "tst-blocked-tasks-json").stdout)
    assert doc["live"] is False
    assert doc[TASK_BLOCKED_FIELD] == task_blocked_by(CHAIN_PLAN["tasks"])


def test_status_of_a_dead_run_flags_blocked_as_trouble(ctl: Ctl):
    _seed_status(ctl, "tst-blocked-status", tasks_json=CHAIN_PLAN, tasks=None)
    res = ctl.run("status", "tst-blocked-status")
    assert res.returncode == 0, res.stderr
    line = next(ln for ln in res.stdout.splitlines() if ln.startswith("tasks:"))
    assert "3 blocked" in line, line


def test_status_json_of_a_dead_run_carries_the_blocked_overlay(ctl: Ctl):
    _seed_status(ctl, "tst-blocked-status-json", tasks_json=CHAIN_PLAN, tasks=None)
    doc = json.loads(ctl.run("--json", "status", "tst-blocked-status-json").stdout)
    assert doc["tasks"] == task_counts(CHAIN_PLAN["tasks"])
    assert doc["tasks"]["blocked"] == 3


def test_run_list_row_carries_the_raw_blocked_count(ctl: Ctl):
    _seed_status(ctl, "tst-blocked-runs", tasks_json=CHAIN_PLAN, tasks=None)
    doc = json.loads(ctl.run("--json", "runs").stdout)
    row = next(r for r in doc if r["runId"] == "tst-blocked-runs")
    assert row["tasksBlocked"] == 3
    assert any("blocked" in t for t in row["tasksTrouble"])


# --------------------------------------------------------------------------
# the hub's server-side re-derivation for a LIVE payload
# --------------------------------------------------------------------------

def test_hub_with_task_blocked_by_derives_and_strips_a_forged_key():
    live = {"tasks": CHAIN_PLAN["tasks"], TASK_BLOCKED_FIELD: {"999": ["fake"]}}
    out = ui_server._with_task_blocked_by(live)
    assert out[TASK_BLOCKED_FIELD] == task_blocked_by(CHAIN_PLAN["tasks"])
    assert "999" not in out[TASK_BLOCKED_FIELD]


def test_hub_with_task_blocked_by_omits_the_key_when_nothing_is_blocked():
    live = {"tasks": [{"id": "1", "status": "completed"}]}
    out = ui_server._with_task_blocked_by(live)
    assert TASK_BLOCKED_FIELD not in out


# --------------------------------------------------------------------------
# the shipped prompt no longer asks an agent to notice
# --------------------------------------------------------------------------

def test_worker_prompt_no_longer_asks_the_agent_to_write_a_note_about_it():
    """The exact phrase this task removed from `worker.md` (before task 021,
    the scheduler bullet told the agent: "append a line to the handoff notes
    file naming the blocked task and the dead dependency"). Its absence is
    the property under test; its presence in the OLD text below is this
    test's own mutation case, proving the assertion can fail."""
    removed_phrase = "append a line to the handoff notes file naming the blocked"
    assert removed_phrase not in WORKER_MD
    assert removed_phrase in (
        "Do not silently grind against it or skip it forever in silence: "
        "append a line to the handoff notes file naming the blocked task "
        "and the dead dependency")   # the mutation case: this literal STILL
                                      # contains the phrase, proving the
                                      # `not in` check above is not vacuous.


def test_worker_prompt_now_says_the_harness_derives_it():
    assert "harness derives this" in WORKER_MD
    assert "taskBlockedBy" in WORKER_MD


def test_architecture_doc_names_the_derivation_not_a_worker_note():
    """`docs/architecture.md`'s own description of the worker's pick rule
    (predates this task) said the WORKER notes a dead dependency's blockage
    in the handoff file -- the same instruction task 021 removed from
    `worker.md` itself. It must say the harness derives it now too, or the
    doc and the shipped prompt disagree about who does this."""
    arch = (REPO / "docs" / "architecture.md").read_text()
    assert "task_blocked_by" in arch
    assert "the worker does" in arch and "not have to notice or record it" in arch
    assert "notes the blockage in the handoff notes file" not in arch
