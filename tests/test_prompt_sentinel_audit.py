"""Task 074: a named, non-regressable guard over every shipped prompt
template for the sentinel shape task 041 introduced (worker.md's own
"Progress budget exhausted" prose defeating
`test_forced_split_budget_task016.py::test_dispatch_under_budget_carries_no_notice`'s
absence check) and task 074 found a second instance of
(task-verify.md's own step-4 prose spelling out `**FINAL ATTEMPT**`
verbatim, defeating `test_verify.py`'s absence checks -- fixed in this same
commit by naming the marker by role and citing `_verify_task`,
`src/ralphd/engine/loop.py:11028`, instead).

`SENTINELS_FORBIDDEN_IN_ANY_SHIPPED_TEMPLATE` below is the audit's own
declared list: every literal `artifacts/reports/v0.20-prompt-sentinel-audit.md`
recorded as "no template contains it today" for a rendered-prompt / Job-context
absence assertion, where a future template edit reproducing the literal would
be the exact same defect shape. It is deliberately NOT every literal audited
-- some (`WORKER_PROMPT_FRAGMENT`, `## Why the previous attempt was rejected`,
`task_id`, `replacement_criteria`, `findings_path`) are legitimately present in
one or more shipped templates today (worker.md's own title, a heading a
DIFFERENT test requires worker.md to name, and contract field names review.md
and task-verify.md must describe) and would make this guard permanently red
for no defect at all -- the report explains each exclusion.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "src" / "ralphd" / "prompts"

# One literal per absence assertion the audit enumerated and confirmed absent
# from every shipped template at the sha the report names. Keep this list and
# the report's rows in the same order this file lists them.
SENTINELS_FORBIDDEN_IN_ANY_SHIPPED_TEMPLATE = [
    "Progress budget exhausted",           # test_forced_split_budget_task016.py
    "FINAL ATTEMPT",                       # test_verify.py
    "MUST take priority",                  # test_e2e.py, test_steer.py
    "Docker siblings",                     # test_e2e.py, test_*sibling*.py
    "the router routed the plan back",     # test_verdict_gate.py
    "## Criteria edited after a validation failure",  # test_review_criteria_reverification.py
    "Operator rulings (task 028, #26)",    # test_last_approach.py and peers (AMENDMENT_MARKER)
    "## Steering ledger (read-only context)",  # test_steering_ledger.py (STEERING_LEDGER_HEADING)
    "## Deferred tasks",                   # test_deferred_tasks_surface.py
    "pending, not for this phase",         # test_steer.py
    "- Approach",                          # test_approach_progress_context.py
    "Remaining iteration budget",          # test_approach_progress_context.py
    "do the thing",                        # test_contract_ladder.py
    "continue working",                    # test_contract_ladder.py
    "keep working",                        # test_contract_ladder.py
    "make more changes",                   # test_contract_ladder.py
    "implement the task",                  # test_contract_ladder.py
    "trivial no-op hat (task 015 fixture)",  # test_hat_extensibility.py
    "append a line to the handoff notes file naming the blocked",  # test_dead_dependency_blocking.py
    "smallest atomic first piece",         # test_worker_split_proposal.py
    "describe the remainder precisely in the notes file",  # test_worker_split_proposal.py
]


def _shipped_templates() -> dict:
    assert PROMPTS_DIR.is_dir(), f"missing prompts dir {PROMPTS_DIR}"
    files = sorted(PROMPTS_DIR.glob("*.md"))
    assert files, "no shipped prompt templates found -- the guard would be vacuous"
    return {f: f.read_text() for f in files}


def test_no_shipped_template_reproduces_an_absence_checked_sentinel():
    """The regression this guard exists to catch: a template edit that
    reintroduces one of the enumerated literals verbatim, the same way
    task 041's worker.md edit permanently broke
    `test_dispatch_under_budget_carries_no_notice` and task 074's
    task-verify.md wording permanently would have broken
    `test_the_final_attempt_marker_is_absent_at_zero_attempts` had its
    absence check not already been scoped past the instructions section."""
    templates = _shipped_templates()
    hits = []
    for path, text in templates.items():
        for sentinel in SENTINELS_FORBIDDEN_IN_ANY_SHIPPED_TEMPLATE:
            if sentinel in text:
                hits.append((path.name, sentinel))
    assert not hits, (
        "a shipped prompt template reproduces a literal some test asserts "
        f"the absence of elsewhere: {hits}")


def test_the_guard_is_not_vacuous(tmp_path):
    """Mutation case, in the test's own tmp dir -- never in shipped code
    (task 074's own success criteria): a fixture template that DOES contain
    one of the enumerated sentinels must fail the same scan the real guard
    above runs, proving `test_no_shipped_template_reproduces_an_absence_checked_sentinel`
    can actually catch the defect shape it is named for."""
    fixture_dir = tmp_path / "prompts"
    fixture_dir.mkdir()
    sentinel = SENTINELS_FORBIDDEN_IN_ANY_SHIPPED_TEMPLATE[0]
    (fixture_dir / "worker.md").write_text(
        f"# Role: Worker\n\nSome instructions mentioning {sentinel} by name.\n")

    hits = []
    for path in sorted(fixture_dir.glob("*.md")):
        text = path.read_text()
        for needle in SENTINELS_FORBIDDEN_IN_ANY_SHIPPED_TEMPLATE:
            if needle in text:
                hits.append((path.name, needle))
    assert hits, "the fixture template must trip the scan -- the probe itself is broken"
    assert (fixture_dir / "worker.md") not in _shipped_templates(), \
        "the fixture must never be mistaken for a shipped template"
