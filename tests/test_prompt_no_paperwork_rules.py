"""The prompts ask for two things only: working code proven by tests, and a
product spec (`SPEC.md`, the user docs) that does not contradict it.

A prompt's rule text is product behaviour -- it is what every hat is told to
do -- so each rule that keeps paperwork out of a run is pinned here by its own
node, and dropping or watering down any one of them fails the node that names
it:

- planning.md: closures are proven by an issue comment, never a tracked file;
  nothing is committed after the final sweep; every implementation task names
  its own behaviour-failing test; no task's deliverable is a tracked report.
- plan_review.md: a paperwork task and an implementation task with no
  behaviour-failing test are objection grounds.
- review.md / verdict_gate.md: the spec is checked against live behaviour,
  and prose outside the spec is neither evidence nor a rejection ground.
- worker.md / steer.md: run evidence goes to the artifacts directory, never
  into a commit, and the steer hat inserts no paperwork task.
"""

from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts"


def _norm(name: str) -> str:
    return " ".join((PROMPTS_DIR / name).read_text().split())


def _practices() -> str:
    text = _norm("planning.md")
    start = text.index("## Plan-building practices")
    return text[start:text.index("## Rules", start)]


# ---- planning.md -----------------------------------------------------------

def test_planning_closure_is_proven_by_an_issue_comment_not_a_tracked_file():
    section = _practices()
    assert "A closure is a task with evidence." in section
    assert ("proves it with an issue comment naming the commit that fixes it "
            "and the test node") in section
    assert "never with a tracked record or report file" in section


def test_planning_tail_commits_nothing_after_the_final_sweep():
    section = _practices()
    assert "work → closures → sweeps" in section
    assert "NOTHING is committed after the final sweep" in section
    assert "there is no record step" in section


def test_planning_has_no_freeze_and_no_probe_audit_rule():
    section = _practices()
    assert "FREEZE" not in section
    assert "record-only" not in section
    assert "probe-audit" not in section
    assert "true final sha" not in section


def test_planning_every_implementation_task_names_a_behaviour_failing_test():
    section = _practices()
    assert "Every implementation task names its own behaviour-failing test." \
        in section
    assert "name a test that fails if the behaviour is absent" in section
    assert "There are no separate audit tasks" in section


def test_planning_never_plans_a_paperwork_task():
    section = _practices()
    assert "Never plan paperwork." in section
    assert ("No task's deliverable may be a tracked report, record, close-out, "
            "audit, evidence or re-take document") in section
    assert "written under `/run/ralphd/artifacts` and never committed" in section
    assert "updated in the same task as the behaviour it describes" in section


def test_planning_sweep_output_goes_to_the_artifacts_dir_uncommitted():
    section = _practices()
    assert re.search(r"The sweep's output goes to the run's artifacts directory "
                     r"\(`/run/ralphd/artifacts`.{0,60}\) and is never committed",
                     section)


# ---- plan_review.md --------------------------------------------------------

def test_plan_review_objects_to_a_paperwork_task():
    text = _norm("plan_review.md")
    assert "**A paperwork task.**" in text
    assert ("A task whose deliverable is a tracked report, record, close-out, "
            "audit, evidence or re-take document") in text


def test_plan_review_objects_to_an_implementation_task_with_no_failing_test():
    text = _norm("plan_review.md")
    assert "**An implementation task with no behaviour-failing test.**" in text
    assert "name no test that fails if the behaviour is absent" in text
    assert "probe-audit" not in text


# ---- review.md / verdict_gate.md -------------------------------------------

def test_review_checks_the_spec_against_live_behaviour_as_blocking():
    text = _norm("review.md")
    assert "7. Check the product spec against the live code." in text
    assert ("a passage that contradicts the live code is a blocking finding"
            in text)


def test_review_other_prose_is_neither_evidence_nor_a_rejection_ground():
    text = _norm("review.md")
    assert ("Numbers and claims in any other prose — commit messages, notes, "
            "issue comments, run artifacts, any tracked report — are neither "
            "evidence nor grounds for rejection") in text
    assert "the tests you run in your identity-proven clone" in text
    assert "Cross-check every evidence artifact" not in text
    assert "un-derived number" not in text


def test_verdict_gate_applies_the_same_spec_rule():
    text = _norm("verdict_gate.md")
    assert "a contradiction is blocking" in text
    assert "are neither evidence nor grounds for rejection" in text


