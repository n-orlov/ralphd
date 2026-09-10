"""Task 035 (PRD requirement F.1/F.4, #172): "an approach has a name" --
`approach N of M`, the cure-pass count for approach N, and the remaining
iteration budget, rendered by ONE shared method inside `build_prompt`'s
`## Job context` builder (`LoopSupervisor._approach_progress_context`) so
the worker and review phases (and every other hat that reaches
`build_prompt`) get the same three lines from the same code path.

Unit-level, the same style `tests/test_debt_resolution.py` and
`tests/test_steering_ledger.py` already use for a context block appended
to every hat's prompt: a `LoopSupervisor` built directly via
`supervisor_factory`, `status.json` seeded through the `status=` kwarg,
and `build_prompt(phase)` read for the three rendered lines.
"""

from __future__ import annotations

from tests.conftest import supervisor_factory

_supervisor = supervisor_factory(run_id="unit", run_root="run", workspace="workspace",
                                 max_approaches=5)


def _seeded(tmp_path, **status_overrides):
    status = {"approach": 2, "maxApproaches": 5, "curePasses": {"2": 1},
              "iterationsUsed": 4, "iterationsBudget": 12}
    status.update(status_overrides)
    return _supervisor(tmp_path, status=status)


def test_worker_prompt_carries_approach_cure_passes_and_remaining_budget():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        sup = _seeded(Path(td))
        prompt = sup.build_prompt("worker")
        assert "## Job context" in prompt
        assert "- Approach 2 of 5" in prompt
        assert "- Cure passes used for this approach: 1" in prompt
        assert "- Remaining iteration budget: 8" in prompt


def test_review_prompt_carries_approach_cure_passes_and_remaining_budget():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        sup = _seeded(Path(td))
        prompt = sup.build_prompt("review")
        assert "## Job context" in prompt
        assert "- Approach 2 of 5" in prompt
        assert "- Cure passes used for this approach: 1" in prompt
        assert "- Remaining iteration budget: 8" in prompt


def test_cure_passes_defaults_to_zero_for_an_approach_never_cured():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        sup = _seeded(Path(td), curePasses={})
        prompt = sup.build_prompt("worker")
        assert "- Cure passes used for this approach: 0" in prompt


def test_silent_before_any_approach_is_committed_to_status_json():
    """The very first planning attempt of a fresh run has not written
    `approach` to status.json yet (task 035's own docstring) -- nothing to
    render, and nothing crashes on the missing keys."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        sup = _supervisor(Path(td))
        prompt = sup.build_prompt("planning")
        assert "- Approach" not in prompt
        assert "Remaining iteration budget" not in prompt
