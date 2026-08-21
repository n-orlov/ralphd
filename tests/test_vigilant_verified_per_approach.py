"""The vigilant verified-task record is per-approach (issue #29).

Every approach's planning pass rewrites tasks.json renumbering its tasks from
"001". The record of which tasks already got a passing verify iteration was a
flat list of bare ids, so approach 2's task "001" collided with approach 1's
already-verified task "001": `pending_verify` computed empty on every worker
iteration and per-task verification was silently skipped for every approach
after the first -- observed on a real run as 17 consecutive worker iterations
with no verify at all.
"""

from __future__ import annotations

import json

from ralphd.engine.state import RunDir


def _run(tmp_path, approach: int) -> RunDir:
    run = RunDir(tmp_path)
    run.update_status(approach=approach)
    return run


def test_a_later_approach_does_not_inherit_the_earlier_one_s_verified_ids(tmp_path):
    run = _run(tmp_path, 1)
    run.mark_task_verified("001")
    run.mark_task_verified("002")
    assert run.read_verified_tasks() == {"001", "002"}

    # Approach 2's planning pass renumbers from "001" again. Those are
    # different tasks, and none of them has been verified.
    run.update_status(approach=2)
    assert run.read_verified_tasks() == set()

    run.mark_task_verified("001")
    assert run.read_verified_tasks() == {"001"}

    # ... and approach 1's record is intact, not clobbered: the file keeps
    # both approaches' entries so the crash/resume protection survives an
    # approach boundary.
    run.update_status(approach=1)
    assert run.read_verified_tasks() == {"001", "002"}

    on_disk = json.loads((tmp_path / "vigilant-verified.json").read_text())
    assert on_disk == ["1:001", "1:002", "2:001"]


def test_unprefixed_entries_from_an_older_engine_read_as_approach_1(tmp_path):
    (tmp_path / "vigilant-verified.json").write_text(json.dumps(["001", "002"]))

    run = _run(tmp_path, 1)
    assert run.read_verified_tasks() == {"001", "002"}, (
        "a run whose file predates the namespacing must not re-verify "
        "approach 1's already-verified work on resume")

    run.update_status(approach=2)
    assert run.read_verified_tasks() == set()


def test_a_run_with_no_recorded_approach_is_approach_1(tmp_path):
    run = RunDir(tmp_path)
    run.mark_task_verified("001")
    assert json.loads((tmp_path / "vigilant-verified.json").read_text()) == ["1:001"]
    assert run.read_verified_tasks() == {"001"}
