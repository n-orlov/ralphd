"""Task 006 (PRD v0.29 D.2/E.2, #282/#283): planning.md's rule 7 moves a
sweep's execution shape into the harness-pinned `## Standing rules` block
(the recipe, its `timeout`, whether/how it backgrounds and polls) and
leaves the sweep task's own `successCriteria` naming only the observable
-- plus how a wait past ~85% of the iteration cap is spanned with
`deferred` (A.5's option (b)). worker.md's whole-suite rule says "the
whole suite" means the tier recipe the notes' Standing rules block gives,
VERBATIM, with its parallelism; an unparallelised substitute is not a
cheaper alternative; and when the Standing rules reserve whole tiers for
sweep tasks, a non-sweep task runs none and proves itself on the modules
its own criteria name.

D.2's three tests:
  1. the planning prompt no longer asks for a command, timeout or polling
     cadence in `successCriteria` (fails against the 0.28.0 prompt, sha
     e8716a53, whose rule 7 said "successCriteria state its execution
     shape -- the exact command, its timeout, and whether/how it
     backgrounds and polls").
  2. a criterion written the way the NEW rule 7 asks (an observable only)
     passes `lint_proposal()` -- a regression guard over the existing
     command-form lint (#282's own negative control), need not fail on
     0.28.0.
  3. a criterion written the way the OLD rule asked (the literal command,
     a wrapper, a polling cadence, "and nothing else") is refused by
     `lint_proposal()` -- also a regression guard, need not fail on
     0.28.0.

E.2's one test:
  4. the worker prompt built for a worker iteration carries E.1's rule
     (fails against the 0.28.0 worker.md, which says nothing about the
     tier recipe being verbatim-with-parallelism or about a non-sweep
     task running no whole tier).

Style of `tests/test_sweep_marking_prompts.py` for the prompt tests
(1 and 4): a `LoopSupervisor` built via `supervisor_factory`, prompts read
through `build_prompt`, assertions against whitespace-normalized text.
Style of `tests/test_ingestion_lints.py` for the lint tests (2 and 3):
`plan_ingestion.lint_proposal()` called directly on a minimal payload.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from ralphd.engine import plan_ingestion as pi
from tests.conftest import supervisor_factory

_supervisor = supervisor_factory(run_id="unit", run_root="run", workspace="workspace")


def _norm(text: str) -> str:
    return " ".join(text.split())


def _prompt(phase: str) -> str:
    with tempfile.TemporaryDirectory() as td:
        sup = _supervisor(Path(td))
        return _norm(sup.build_prompt(phase))


def _sweep_task(task_id: str, success_criteria: str) -> dict:
    return {
        "id": task_id,
        "title": f"sweep {task_id}",
        "successCriteria": success_criteria,
    }


# ---------------------------------------------------------------------
# D.2.1 -- the planning prompt
# ---------------------------------------------------------------------

def test_planning_prompt_no_longer_asks_for_execution_shape_in_criteria():
    prompt = _prompt("planning")
    # The old rule 7's own words are gone -- it used to ask the task's
    # OWN successCriteria to state the execution shape.
    assert "successCriteria state its execution shape" not in prompt, prompt
    # The new rule 7: the shape goes in the Standing rules block instead.
    assert "harness-pinned" in prompt and "Standing rules" in prompt, prompt
    assert "never in its own successCriteria" in prompt, prompt
    assert (
        "sweep task's own successCriteria names only the observable"
        in prompt.replace("`", "")
    ), prompt
    assert (
        "never the command, the timeout value or the polling cadence"
        in prompt.replace("`", "")
    ), prompt
    # The 85%-of-cap wait is spanned with `deferred`, naming A.5's option (b).
    assert "about 85% of the iteration cap" in prompt, prompt
    assert "deferred` with `until` set to the estimated completion" in prompt, prompt
    assert "A.5's option (b)" in prompt, prompt


# ---------------------------------------------------------------------
# D.2.2 -- a new-rule-7-shaped criterion (observable only) is accepted
# ---------------------------------------------------------------------

def test_new_rule_7_shaped_criterion_passes_lint_proposal():
    criteria = (
        "The fast tier's pytest run exits 0 and "
        "/run/ralphd/artifacts/sweep/fast.exitstatus records 0. "
        "/run/ralphd/artifacts/sweep/fast.log records every collected "
        "test passing."
    )
    assert pi._command_form_shape_clause(criteria) is None, (
        "the observable-only criterion should trip no command-form cue"
    )
    payload = {"tasks": [_sweep_task("014", criteria)]}
    errors = pi.lint_proposal(payload)
    assert not any("command form" in e for e in errors), (
        f"a rule-7-shaped (observable-only) criterion was wrongly refused: {errors!r}"
    )


# ---------------------------------------------------------------------
# D.2.3 -- an old-rule-shaped criterion (command/timeout/cadence) is refused
# ---------------------------------------------------------------------

def test_old_rule_shaped_criterion_is_refused_by_lint_proposal():
    criteria = (
        "The sweep is launched as `nohup timeout 1100 .venv/bin/python -m "
        "pytest -q -m fast -n 8 --dist loadfile > /tmp/fast.log 2>&1 &`, "
        "polled every 30 seconds, and nothing else."
    )
    clause = pi._command_form_shape_clause(criteria)
    assert clause is not None, (
        "the old-rule-shaped criterion (literal command + cadence + "
        "exclusivity) should trip the command-form lint"
    )
    payload = {"tasks": [_sweep_task("014", criteria)]}
    errors = pi.lint_proposal(payload)
    assert any("014" in e and "command form" in e for e in errors), (
        f"no command-form refusal for the old-rule-shaped criterion: {errors!r}"
    )


# ---------------------------------------------------------------------
# E.2 -- the worker prompt carries E.1's whole-suite rule
# ---------------------------------------------------------------------

def test_worker_prompt_carries_the_whole_suite_means_the_recipe_rule():
    prompt = _prompt("worker")
    idx = prompt.index("Budget your test runs against the iteration clock")
    section = prompt[idx:idx + 1500]
    assert (
        "the whole suite" in section.lower()
        and "tier recipe" in section
        and "Standing rules" in section
    ), section
    assert "verbatim, with its parallelism" in section, section
    assert "is not a cheaper alternative" in section, section
    assert (
        "a task that is not a sweep runs no whole tier" in section
        or "runs no whole tier at all" in section
    ), section
    assert "proves itself on the modules its own criteria name" in section, section
