"""v0.38 (#306, #320) A.1: an applicable waiver satisfies the dependency for
every purpose, and a status-only ruling does not revoke it.

The settled-dependency rule reads the operator's current word on a task's
criteria. A `mode: status` ruling moves a status and carries no criteria text,
so it is neither a waiver nor a criteria replacement; only a later criteria
ruling (a replacement) supersedes an earlier waiver, exactly as the review
criteria reader treats the same entries. Every reader (scheduler, completion,
blocked-by, the on-disk views) goes through the one settled-id function, so
they must agree.
"""

from __future__ import annotations

import json

import pytest

from ralphd.engine import state
from ralphd.engine.state import RunDir, write_amendment
from test_petitioned_settled import (  # noqa: F401  (fixtures + helpers)
    OBLIGATION,
    _dependent,
    _petitioned,
    _plan_text,
    _select,
    _status_of,
    _sup,
    cfg_dir,
)


def _check_every_view_agrees_settled(sup, cfg_dir):
    doc = sup.run.read_tasks()
    settled = sup._settled_petitioned_ids(doc)
    assert settled == {"001"}
    picked = _select(sup)
    assert picked is not None and picked["id"] == "002"
    assert state.task_blocked_by(
        doc["tasks"], include_petitioned=True, settled=settled) == {}
    assert _status_of(sup, "001") == "petitioned"


def _check_every_view_agrees_unsettled(sup):
    doc = sup.run.read_tasks()
    assert sup._settled_petitioned_ids(doc) == frozenset()
    assert _select(sup) is None
    assert state.task_blocked_by(
        doc["tasks"], include_petitioned=True,
        settled=sup._settled_petitioned_ids(doc)) == {"002": ["001"]}


# --- the finding's own case ------------------------------------------------


def test_a_waiver_survives_a_status_only_ruling_for_every_view(tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    write_amendment(cfg_dir, "001", "operator accepts the petition", waive=True)
    assert _select(sup)["id"] == "002"
    # a no-op status ruling (the task is already petitioned)
    write_amendment(cfg_dir, "001", "restating the state", status="petitioned")

    _check_every_view_agrees_settled(sup, cfg_dir)


# --- cases of the same rule the finding did not list ------------------------


@pytest.mark.parametrize("status", ["validated", "skipped", "pending"])
def test_a_waiver_survives_a_status_ruling_naming_any_status(
        tmp_path, cfg_dir, status):
    """The ruling's target status is irrelevant to the waiver's standing: the
    engine may refuse or apply the move, the plan still reads the waiver."""
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    write_amendment(cfg_dir, "001", "operator accepts the petition", waive=True)
    write_amendment(cfg_dir, "001", "ruling", status=status)

    assert sup._settled_petitioned_ids() == {"001"}


def test_several_status_rulings_after_a_waiver_leave_it_standing(tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    write_amendment(cfg_dir, "001", "operator accepts the petition", waive=True)
    write_amendment(cfg_dir, "001", "one", status="petitioned")
    write_amendment(cfg_dir, "001", "two", status="petitioned")

    _check_every_view_agrees_settled(sup, cfg_dir)


def test_a_status_ruling_on_another_task_does_not_disturb_the_waiver(
        tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    write_amendment(cfg_dir, "001", "operator accepts the petition", waive=True)
    write_amendment(cfg_dir, "002", "unrelated", status="pending")

    _check_every_view_agrees_settled(sup, cfg_dir)


def test_a_status_ruling_alone_never_settles_the_petition(tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    write_amendment(cfg_dir, "001", "restating the state", status="petitioned")

    _check_every_view_agrees_unsettled(sup)


def test_a_status_ruling_before_the_waiver_changes_nothing(tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    write_amendment(cfg_dir, "001", "restating the state", status="petitioned")
    write_amendment(cfg_dir, "001", "operator accepts the petition", waive=True)

    _check_every_view_agrees_settled(sup, cfg_dir)


def test_a_waiver_pinned_to_another_approach_stays_unbinding_after_a_status_ruling(
        tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    write_amendment(cfg_dir, "001", "accepted", waive=True, approach=2)
    write_amendment(cfg_dir, "001", "ruling", status="petitioned")

    _check_every_view_agrees_unsettled(sup)


# --- a status ruling differs from a criteria replacement ---------------------


def test_a_later_criteria_replacement_still_revokes_the_waiver(tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    write_amendment(cfg_dir, "001", "operator accepts the petition", waive=True)
    write_amendment(cfg_dir, "001", "new criteria text replaces the old")

    _check_every_view_agrees_unsettled(sup)


def test_a_status_ruling_after_a_replacement_does_not_resurrect_an_earlier_waiver(
        tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    write_amendment(cfg_dir, "001", "operator accepts the petition", waive=True)
    write_amendment(cfg_dir, "001", "new criteria text replaces the old")
    write_amendment(cfg_dir, "001", "ruling", status="petitioned")

    _check_every_view_agrees_unsettled(sup)


def test_a_waiver_after_a_replacement_settles_again(tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    write_amendment(cfg_dir, "001", "new criteria text replaces the old")
    write_amendment(cfg_dir, "001", "operator accepts the petition", waive=True)
    write_amendment(cfg_dir, "001", "ruling", status="petitioned")

    _check_every_view_agrees_settled(sup, cfg_dir)


# --- completion and the on-disk readers agree --------------------------------


def test_plan_completion_agrees_with_a_waiver_after_a_status_ruling(
        tmp_path, cfg_dir):
    from test_petitioned_settled import _dependent as dep
    sup = _sup(tmp_path, [_petitioned(), dep(status="completed")])
    write_amendment(cfg_dir, "001", "operator accepts the petition", waive=True)
    write_amendment(cfg_dir, "001", "ruling", status="petitioned")
    doc = sup.run.read_tasks()

    assert sup._outstanding_for_completion(doc) == []


def test_on_disk_reader_agrees_with_the_engine_reader(tmp_path, cfg_dir):
    rdir = tmp_path / "ondisk"
    rdir.mkdir()
    (rdir / "status.json").write_text(json.dumps({"approach": 1}))
    tasks = [_petitioned(), _dependent()]
    write_amendment(cfg_dir, "001", "operator accepts the petition", waive=True)
    write_amendment(cfg_dir, "001", "ruling", status="petitioned")

    assert state.settled_petitioned_task_ids_on_disk(
        rdir, tasks, config_root=cfg_dir) == {"001"}
    write_amendment(cfg_dir, "001", "replacement criteria")
    assert state.settled_petitioned_task_ids_on_disk(
        rdir, tasks, config_root=cfg_dir) == frozenset()


def test_clause_waiver_is_unaffected_by_status_rulings(tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    sup.run.add_waiver(clause=OBLIGATION, finding="not a defect", judge="review")
    write_amendment(cfg_dir, "001", "ruling", status="petitioned")

    _check_every_view_agrees_settled(sup, cfg_dir)
