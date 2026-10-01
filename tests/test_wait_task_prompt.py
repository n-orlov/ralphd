"""Task 002 (PRD v0.29 A.5, #279): worker.md's "Return before the cap" bullet
replaces "prefer recording the run id you are waiting on in the notes over
sleeping through the rest of your clock" with a three-way rule for a task
whose criteria genuinely are the wait itself: (a) a bounded poll inside the
iteration when the estimate fits before the 85% line, (b) `deferred` with
`until` set to the estimated completion when it does not, (c) poll up to
the 85% line then hand off otherwise -- and says a single check of a
still-running lane that returns within minutes is none of these three. The
"Deferring a task that must wait" section names a dispatched CI lane as an
example of a condition a task must wait on.

Unit-level, the same style `tests/test_approach_progress_context.py` uses
for prompt-content assertions: a `LoopSupervisor` built via
`supervisor_factory`, and `build_prompt("worker")` read for the rendered
text. This fails against the 0.28.0 worker.md, which still carries the old
"prefer recording the run id..." sentence and neither the three-way rule
nor the dispatched-CI-lane example in the deferred section.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from tests.conftest import supervisor_factory

_supervisor = supervisor_factory(run_id="unit", run_root="run", workspace="workspace",
                                 max_approaches=5)


def _prompt():
    with tempfile.TemporaryDirectory() as td:
        sup = _supervisor(Path(td))
        return sup.build_prompt("worker")


def test_old_sleeping_through_the_clock_sentence_is_gone():
    prompt = _prompt()
    assert "prefer recording the run id you are waiting on in the notes over sleeping through the rest of your clock" not in prompt
    assert "sleeping through the rest of\n  your clock" not in prompt


def test_return_before_the_cap_gives_the_three_way_rule():
    prompt = _prompt()
    cap_idx = prompt.index("Return before the cap")
    section = prompt[cap_idx:cap_idx + 2000]
    assert "the estimate fits before the 85% line" in section
    assert "wait inside this iteration in a bounded poll loop" in section
    assert "it does not fit" in section
    assert "`deferred` with `until` set to the estimated completion" in section
    assert "poll inside the iteration up to the 85% line, then hand off" in section


def test_a_single_quick_recheck_is_none_of_the_three():
    prompt = _prompt()
    assert "is none of these three" in prompt


def test_deferred_section_names_a_dispatched_ci_lane_as_an_example():
    prompt = _prompt()
    defer_idx = prompt.index("## Deferring a task that must wait")
    section = prompt[defer_idx:defer_idx + 1500]
    assert "dispatched CI lane" in section
