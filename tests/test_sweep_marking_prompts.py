"""PRD v0.29 requirement B.4-B.5 (#280): the planning prompt tells the
planner to mark its sweep tasks with `sweep: true`, and the review prompt
tells the reviewer that a post-sweep cure stales the sweep by itself --
the engine re-opens a `sweep: true` task, `invalidates` is for OTHER
evidence a cure stales, and prose in `review-findings.md` re-opens
nothing.

Style of `tests/test_review_clone_protocol.py`: a `LoopSupervisor` built
through `supervisor_factory`, prompts read via `build_prompt`, assertions
against whitespace-normalized text so an 80-column rewrap never breaks
the check.

Both assertions below fail against the 0.28.0 prompts (sha e8716a53),
which say nothing about `sweep: true` in planning.md and nothing about
the engine re-opening a sweep in review.md.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from tests.conftest import supervisor_factory

_supervisor = supervisor_factory(run_id="unit", run_root="run",
                                 workspace="workspace")


def _norm(text: str) -> str:
    return " ".join(text.split())


def _prompt(phase: str) -> str:
    with tempfile.TemporaryDirectory() as td:
        sup = _supervisor(Path(td))
        return _norm(sup.build_prompt(phase))


def test_planning_prompt_tells_the_planner_to_mark_the_sweep_true():
    """B.4: `sweep: true` goes on a task whose deliverable is a
    whole-tier or CI-lane run over the final tree, and on nothing else."""
    prompt = _prompt("planning")
    assert "sweep: true" in prompt, prompt
    assert "whole-tier or CI-lane run over the final tree" in prompt, prompt
    assert "and on nothing else" in prompt, prompt


def test_review_prompt_for_a_full_review_states_the_sweep_reopen_rule():
    """B.5: a cure filed after the plan's sweep makes it stale even when
    it touches only tests; the engine re-opens `sweep: true` tasks by
    itself; `invalidates` is for other evidence the cure stales; prose in
    `review-findings.md` re-opens nothing."""
    prompt = _prompt("review")
    assert "makes the sweep stale" in prompt, prompt
    assert "even when the" in prompt and "cure touches only tests" in prompt, prompt
    assert "engine re-opens every" in prompt and "sweep: true" in prompt, prompt
    assert ("invalidates` is for other evidence the cure stales"
            in prompt or "invalidates` is for OTHER evidence" in prompt), prompt
    assert "review-findings.md` re-opens nothing" in prompt, prompt


def test_review_prompt_does_not_route_sweep_reopen_through_invalidates():
    """The engine's own re-open does not depend on `invalidates` naming the
    sweep's artifact path -- that would make the sweep re-open conditional
    on prose the reviewer writes, which is exactly the gap #280 found."""
    prompt = _prompt("review")
    assert "whether or not your finding" in prompt
    assert "do not route a sweep re-run through" in prompt
