"""Task 027 (PRD requirement D.3, #157): "A numeric target far from its
baseline is a smell." A criterion of the shape `<metric> under <target>`
whose plan-quoted baseline is more than 2x the target is refused at plan
ingestion unless the plan names the measurement task that re-measures the
baseline at the launch sha and stages the descent -- the offending task's
id is named in the refusal.

Folded into `lint_proposal()` itself (`src/ralphd/engine/plan_ingestion.py`),
reusing D.2's own cross-task linkage primitive (`_is_stage_task_of`) rather
than inventing a second one: the measurement task is bound to the
offending task exactly the way a D.2 stage task is bound to its oversized
parent (`dependsOn` in either direction, or by naming the offending task's
id), and it "stages the descent" the same way D.2's own stage tasks do --
a `Stage N` marker.

Three nodes are the pinned regression the commit message quotes:
`test_a_baseline_more_than_2x_the_target_with_no_measurement_task_is_refused_naming_the_task`
(the refusal), `test_a_linked_measurement_task_that_remeasures_at_the_launch_sha_and_stages_is_accepted`
(the accepted staged form), and
`test_a_baseline_within_2x_the_target_passes_untouched` (a within-2x
criterion, no measurement task needed).
"""

from __future__ import annotations

from ralphd.engine import plan_ingestion as pi


def _basic_task(task_id: str, **overrides) -> dict:
    task = {
        "id": task_id,
        "title": f"do thing {task_id}",
        "successCriteria": f"thing {task_id} is verifiably done",
    }
    task.update(overrides)
    return task


def test_a_baseline_more_than_2x_the_target_with_no_measurement_task_is_refused_naming_the_task():
    task = _basic_task(
        "001",
        successCriteria=(
            "the fast tier's collected test count stays under 3000 tests "
            "(currently ~8000)."
        ),
    )
    gap = pi._quoted_baseline_target_gap(task, [task])
    assert gap is not None
    assert "8000" in gap and "3000" in gap

    errors = pi.lint_proposal({"tasks": [task]})
    assert any("001" in e and "baseline" in e for e in errors)


def test_a_linked_measurement_task_that_remeasures_at_the_launch_sha_and_stages_is_accepted():
    task = _basic_task(
        "001",
        successCriteria=(
            "the fast tier's collected test count stays under 3000 tests "
            "(currently ~8000)."
        ),
    )
    measurement = _basic_task(
        "002",
        title="Stage 1: re-measure the fast tier baseline at the launch sha",
        successCriteria=(
            "the fast tier count is re-measured at the launch sha and "
            "docs/testing.md records the descent plan toward 3000."
        ),
        dependsOn=["001"],
    )
    assert pi._quoted_baseline_target_gap(task, [task, measurement]) is None
    errors = pi.lint_proposal({"tasks": [task, measurement]})
    assert not any("baseline" in e for e in errors)


def test_a_measurement_task_missing_the_launch_sha_phrase_is_not_enough():
    task = _basic_task(
        "001",
        successCriteria=(
            "the fast tier's collected test count stays under 3000 tests "
            "(currently ~8000)."
        ),
    )
    measurement = _basic_task(
        "002",
        title="Stage 1: re-measure the fast tier baseline",
        successCriteria="the fast tier count is re-measured and recorded.",
        dependsOn=["001"],
    )
    gap = pi._quoted_baseline_target_gap(task, [task, measurement])
    assert gap is not None


def test_an_unbound_measurement_task_does_not_count():
    task = _basic_task(
        "001",
        successCriteria=(
            "the fast tier's collected test count stays under 3000 tests "
            "(currently ~8000)."
        ),
    )
    stray = _basic_task(
        "003",
        title="Stage 1: re-measure some other baseline at the launch sha",
        successCriteria="a different metric is re-measured at the launch sha.",
        dependsOn=["002"],
    )
    gap = pi._quoted_baseline_target_gap(task, [task, stray])
    assert gap is not None


def test_a_baseline_within_2x_the_target_passes_untouched():
    task = _basic_task(
        "001",
        successCriteria=(
            "the fast tier's collected test count stays under 3000 tests "
            "(currently ~5000)."
        ),
    )
    assert pi._quoted_baseline_target_gap(task, [task]) is None
    errors = pi.lint_proposal({"tasks": [task]})
    assert not any("baseline" in e for e in errors)


def test_a_target_with_no_quoted_baseline_at_all_passes_untouched():
    task = _basic_task(
        "001",
        successCriteria="the fast tier's collected test count stays under 3000 tests.",
    )
    assert pi._quoted_baseline_target_gap(task, [task]) is None


def test_a_baseline_exactly_at_2x_the_target_passes_untouched():
    """2x itself is the boundary, not past it -- "more than 2x" (PRD's own
    wording) excludes the exact multiple."""
    task = _basic_task(
        "001",
        successCriteria=(
            "the fast tier's collected test count stays under 3000 tests "
            "(currently 6000)."
        ),
    )
    assert pi._quoted_baseline_target_gap(task, [task]) is None
