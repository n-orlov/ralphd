"""Task 011 (#51): `PATCH /config/budget`'s old "live-engine change" wording
promised the *opposite* of what task 010 made true. Before task 010, a live
top-up (`ralphctl budget <run-id> +N`) really was undone by any container
restart, so `engine/api.py`, `docs/cli.md` and `SPEC.md` all said "use
`resume <run-id> --iterations +N` when the new budget has to survive a fresh
container" -- correctly, at the time. Task 010's `_seed_iteration_budget`
(engine/main.py) made a *plain* resume carry the top-up forward automatically
(it seeds `cfg.iterations` from `max(status.json's iterationsBudget, job.yaml's
iterations)`), so the old wording became false without anyone touching it:
the docs kept telling an operator to pass `--iterations +N` just to *keep*
a budget that a plain resume already preserves.

Two things are pinned here, one per claim:

1. A **doc-text check**: all three places that made the promise now say the
   change "persists across a resume" (or the equivalent -- see the exact
   phrase below) and none of them still carries the stale phrasing that
   implied `--iterations +N` was required just to survive a restart. The
   mutation case is literal: the assertions fail if the old phrase is put
   back (this test IS the mutation-sensitive check -- reverting the wording
   edit reintroduces the exact string this test asserts is gone).
2. A **behaviour check** tying the doc claim to the code that makes it true:
   a real `resume --iterations +N` over a run whose status.json budget
   already exceeds job.yaml's adds N to the *status.json* figure, not
   job.yaml's smaller one -- proving the "persists, and stacks" claim rather
   than just the wording. (The full black-box coverage for this lives in
   tests/test_cli_resume.py
   ::test_resume_iterations_topup_adds_to_status_json_not_job_yaml_when_higher;
   this module's own case below is the same assertion at the
   `_effective_budget_base` unit level, so a revert of *either* the doc
   wording or the function's max()-not-job.yaml's-alone logic is caught by
   one of the two tests in this file.)
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DOCS_MAKING_THE_CLAIM = [
    REPO_ROOT / "src" / "ralphd" / "engine" / "api.py",
    REPO_ROOT / "docs" / "cli.md",
    REPO_ROOT / "SPEC.md",
]

# The phrase task 011 corrected each of the three files to contain.
CORRECT_PHRASE = "persists across a resume"

# The stale claim task 011 removes: it told the operator --iterations +N was
# needed just to keep a top-up alive, which stopped being true the moment
# task 010's engine-startup seeding shipped.
STALE_PHRASE_FRAGMENTS = [
    "is what carries a bigger",
    "has to survive a fresh container",
    "making an increase outlive the container needs the resume form",
    "when the new budget has to survive a fresh",
]


def test_the_three_budget_docs_say_the_topup_persists_across_a_resume():
    missing = []
    for path in DOCS_MAKING_THE_CLAIM:
        text = path.read_text(encoding="utf-8")
        if CORRECT_PHRASE not in text:
            missing.append(str(path.relative_to(REPO_ROOT)))
    assert not missing, (
        f"these files no longer state the corrected persistence claim "
        f"({CORRECT_PHRASE!r}): {missing}")


def test_none_of_the_three_still_carries_the_pre_task_010_wording():
    offenders = {}
    for path in DOCS_MAKING_THE_CLAIM:
        text = path.read_text(encoding="utf-8")
        hits = [frag for frag in STALE_PHRASE_FRAGMENTS if frag in text]
        if hits:
            offenders[str(path.relative_to(REPO_ROOT))] = hits
    assert not offenders, (
        f"stale pre-task-010 wording ('needs a resume with --iterations "
        f"just to survive a restart') still present: {offenders}")


def test_stale_fragment_check_is_substantive_not_a_typo():
    """The stale fragments really did appear verbatim in the pre-task-011
    text -- otherwise the check above would pass trivially forever."""
    pre_011_engine_api_snippet = (
        "It is a *live-engine* change: `/config/job.yaml` is a read-only "
        "mount, so the engine cannot persist it there -- the new budget "
        "lives in this engine process (and in status.json's "
        "iterationsBudget); a later `ralphctl resume <run-id> --iterations "
        "+N` is what carries a bigger budget into a fresh container.")
    assert any(frag in pre_011_engine_api_snippet
               for frag in STALE_PHRASE_FRAGMENTS)


# --------------------------------------------------------------------------
# Behaviour side of the claim: the CLI function the wording describes.
# --------------------------------------------------------------------------
from ralphd.cli.main import _effective_budget_base


def test_effective_budget_base_prefers_the_larger_of_the_two(tmp_path):
    rdir = tmp_path / "run"
    rdir.mkdir()
    job = {"iterations": 5}
    # No status.json at all: job.yaml's own figure is the base.
    assert _effective_budget_base(job, rdir) == 5

    # status.json lower than job.yaml: job.yaml still wins (an operator who
    # raised job.yaml itself between episodes must not be undercut).
    (rdir / "status.json").write_text(json.dumps({"iterationsBudget": 3}))
    assert _effective_budget_base(job, rdir) == 5

    # status.json higher than job.yaml: the live top-up it recorded wins --
    # this is the exact number `resume --iterations +N` must add to.
    (rdir / "status.json").write_text(json.dumps({"iterationsBudget": 40}))
    assert _effective_budget_base(job, rdir) == 40
