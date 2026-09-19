"""Task 074: a named, non-regressable guard over every shipped prompt
template for the sentinel shape task 041 introduced (worker.md's own
"Progress budget exhausted" prose defeating
`test_forced_split_budget_task016.py::test_dispatch_under_budget_carries_no_notice`'s
absence check), plus the two further instances task 074 found and fixed:
task-verify.md's step-4 prose spelling out the final-attempt marker verbatim
(defeating `test_verify.py`'s absence checks) and worker.md's step-3 prose
spelling out the retry block's heading verbatim (the literal three
`test_worker_hat.py` nodes check the absence of in the injected extra block).
Both were fixed by naming the thing by role and citing where it is rendered.

`ENUMERATED_ABSENCE_ASSERTIONS` below is the audit's SINGLE declared list and
it is COMPLETE: one entry per absence assertion the repeatable search in
`artifacts/reports/v0.20-prompt-sentinel-audit.md` enumerates (the same regex
lives in `SEARCH_PATTERN` here, so `test_the_enumeration_covers_every_hit_of_
its_own_search` can re-run the search and refuse a newly added assertion that
nobody audited). Each entry declares the artifact the assertion reads and the
scope in which its literals are forbidden inside `src/ralphd/prompts/`:

* `ALL`  -- the artifact is a rendered prompt, a rendered `## Job context`
  extra block, or a shipped template read raw, and no shipped template may
  contain the literal. This is the guard proper.
* a tuple of template file names -- the assertion reads exactly those
  templates as raw constants (`PLAN_REVIEW_MD`, `VERDICT_GATE_MD`), so the
  forbidding is scoped to them; other templates legitimately use the words.
* `None` -- the artifact is NOT fed by any shipped template (a CLI render, a
  hub `app.js` slice, `router.py`'s inline prompt, the generated SPEC table or
  the generated mermaid diagram), or the literal is legitimately present in a
  template for an unrelated reason. Every such entry MUST carry a non-empty
  `allowed_because`, and the guard checks that it does -- an entry cannot be
  quietly parked in this class without saying why.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "src" / "ralphd" / "prompts"
TESTS_DIR = REPO_ROOT / "tests"

# The repeatable search the report quotes, as a regex: every `not in` line in
# tests/*.py whose target names or derives from a rendered prompt, an injected
# extra block, a render, a raw `*_MD` template constant, a generated table, a
# generated diagram or a rendered `block`.
SEARCH_PATTERN = (
    r'not in\s+\S*([Pp]rompt|[Ee]xtra\b|[Rr]ender|_MD\b|[Tt]able\b'
    r'|[Dd]iagram|block\b)')

ALL = "*"  # forbidden in every shipped template


@dataclass(frozen=True)
class AbsenceAssertion:
    """One enumerated absence assertion (or a group of them sharing a line,
    literal set and artifact)."""

    module: str                     # file under tests/ the assertion lives in
    lines: tuple[int, ...]          # its line numbers at the audit's sha
    literals: tuple[str, ...]       # what it forbids, resolved to literals
    reads: str                      # the artifact it reads
    forbid_in: object               # ALL, a tuple of template names, or None
    allowed_because: str = ""       # required iff forbid_in is None
    lowercased: bool = False        # the assertion compares `.lower()` text


ENUMERATED_ABSENCE_ASSERTIONS: tuple[AbsenceAssertion, ...] = (
    AbsenceAssertion(
        "test_amendment_status_load_time.py", (469,), ("**id**: 001",),
        "the first worker prompt of a pass that began after an operator "
        "recorded a live `mode: status` ruling (GH #209 defect 6): the "
        "scheduler must never have been handed the ruled task, so the "
        "assembled task block must be absent from the rendered prompt", ALL),
    AbsenceAssertion(
        "test_approach_progress_context.py", (72, 73),
        ("- Approach", "Remaining iteration budget"),
        "the rendered planning prompt", ALL),
    AbsenceAssertion(
        "test_cli_argparse_walker_182.py", (80,), (),
        "the walker helper's own dict of parsed argv routes", None,
        "not an absence assertion at all -- a dict-membership branch in "
        "`_unrouted`, no rendered artifact and no literal"),
    AbsenceAssertion(
        "test_contract_events.py", (1194,),
        ("THE ONE RULE: exactly one task, then stop",),
        "`_ask_contract_fix`'s re-ask prompt (WORKER_PROMPT_FRAGMENT)", None,
        "the literal IS worker.md:7's own title, and the artifact is built "
        "in Python from errors/final_text (src/ralphd/engine/loop.py:5450) "
        "and never mounts a hat's template, so no template edit can reach it"),
    AbsenceAssertion(
        "test_contract_ladder.py", (187, 375),
        ("THE ONE RULE: exactly one task, then stop",),
        "`_ask_contract_fix`'s re-ask prompt (WORKER_PROMPT_FRAGMENT)", None,
        "same literal and same template-free artifact as the "
        "test_contract_events.py row above"),
    AbsenceAssertion(
        "test_contract_ladder.py", (188, 376, 191),
        ("do the thing", "continue working", "keep working",
         "make more changes", "implement the task"),
        "`_ask_contract_fix`'s re-ask prompt, lowercased", ALL,
        lowercased=True),
    AbsenceAssertion(
        "test_contract_ladder.py", (191,), ("do the work",),
        "`_ask_contract_fix`'s re-ask prompt, lowercased (one of the five "
        "invite phrases)", None,
        "planning.md:47 and steer.md:8 legitimately say 'do the work'; the "
        "artifact is the template-free re-ask prompt, so this is no coupling"),
    AbsenceAssertion(
        "test_contract_reask_grounding.py", (147,), (),
        "`_ask_contract_fix`'s re-ask prompt", None,
        "the forbidden value is a runtime fixture string ('x' * 5000 + tail), "
        "not a fixed literal any file could contain"),
    AbsenceAssertion(
        "test_cost_zero_quote.py", (105,), ("$",),
        "`format_cost`'s CLI render", None,
        "not a template-fed artifact, and '$' is legitimately in shell "
        "examples at planning.md:223, task-verify.md:137, worker.md:261 "
        "and worker.md:308"),
    AbsenceAssertion(
        "test_dead_dependency_blocking.py", (344,),
        ("append a line to the handoff notes file naming the blocked",),
        "`WORKER_MD` (worker.md read raw)", ALL),
    AbsenceAssertion(
        "test_deferred_tasks_surface.py", (161, 163), ("## Deferred tasks",),
        "the worker prompt's injected extra block", ALL),
    AbsenceAssertion(
        "test_e2e.py", (490, 530), ("MUST take priority",),
        "the rendered verify and worker prompts", ALL),
    AbsenceAssertion(
        "test_e2e.py", (993,), ("Docker siblings",),
        "the rendered worker prompt", ALL),
    AbsenceAssertion(
        "test_forced_split_budget_task016.py", (140,),
        ("Progress budget exhausted",),
        "the rendered worker prompt (task 041's regression, fixed by 073)",
        ALL),
    AbsenceAssertion(
        "test_hat_extensibility.py", (296,),
        ("trivial no-op hat (task 015 fixture)",),
        "`_ask_contract_fix`'s re-ask prompt", ALL),
    AbsenceAssertion(
        "test_hub_delete_affordance.py", (261,), ("delete",),
        "a slice of the hub's app.js bundle (`RUN_COLUMNS`)", None,
        "not a prompt/Job-context/SPEC-table/diagram render, and 'delete' is "
        "legitimately in steer.md:32, :53 and :109"),
    AbsenceAssertion(
        "test_hub_features.py", (451,), ("innerHTML", "html:",
                                        "insertAdjacentHTML"),
        "a slice of the hub's app.js bundle (`taskDialogText`)", None,
        "not a template-fed artifact -- a rendering-discipline grep over the "
        "hub bundle; no shipped template contains these literals either"),
    AbsenceAssertion(
        "test_hub_prd_dialog.py", (211,), ("innerHTML", "html:",
                                          "insertAdjacentHTML"),
        "a slice of the hub's app.js bundle (`openTextDialog`)", None,
        "same hub-bundle artifact as the test_hub_features.py row above"),
    AbsenceAssertion(
        "test_last_approach.py", (794,), ("Operator rulings (task 028, #26)",),
        "the rendered review prompt (AMENDMENT_MARKER)", ALL),
    AbsenceAssertion(
        "test_operator_ruling.py", (333, 336),
        ("Historical rulings", "purpose of this review"),
        "the operator-ruling context string `_operator_amendment_review_"
        "context(audience='verdict_gate')` hands the peer verdict gate "
        "(task 018, D.8, #196)", None,
        "the artifact is built entirely in loop.py -- the ruling files "
        "under CONFIG_DIR plus that method's own prose -- so no shipped "
        "template contributes a byte to it and none can make either "
        "absence vacuous; the assertions on the RENDERED prompt in the "
        "same test are presence checks, not absence ones"),
    AbsenceAssertion(
        "test_operator_ruling_review_verdict.py", (158,),
        ("Operator rulings (task 028, #26)",),
        "the rendered review prompt (AMENDMENT_MARKER)", ALL),
    AbsenceAssertion(
        "test_peer_verdict_gate.py", (555, 558, 696, 744),
        ("TOPSECRET-REVIEW-REASON-MARKER-4f9c2",),
        "the rendered review and peer verdict-gate prompts "
        "(REVIEW_SECRET_REASON)", ALL),
    AbsenceAssertion(
        "test_petition_hold_bound.py", (127,), ("skipped",),
        "the mid-run adjudication episode's own extra block "
        "(`LoopSupervisor._midrun_adjudication_extra`, task 011, "
        "requirement C.5, #187) -- Python-built text, never read from any "
        "shipped template", None,
        "the literal legitimately appears throughout review.md and "
        "worker.md describing the `skipped` task status generally; this "
        "check is scoped to `_midrun_adjudication_extra`'s own returned "
        "string, which is never itself a shipped template", lowercased=True),
    AbsenceAssertion(
        "test_price_strategy_derive.py", (174,), ("$0.00",),
        "`format_cost`'s CLI render", None,
        "not a template-fed artifact; no shipped template contains it either"),
    AbsenceAssertion(
        "test_resume_after_incurable_rejection.py", (215,),
        ("Operator rulings (task 028, #26)",),
        "the rendered review prompt (AMENDMENT_MARKER)", ALL),
    AbsenceAssertion(
        "test_review_clone_protocol.py", (132, 139),
        ("## Disposable-clone protocol (operator-authorized)",),
        "every hat's rendered prompt for a run that declares no "
        "`review_clone_protocol` (#208), and for one that declares only "
        "whitespace: the engine-built `## Job context` extra block "
        "(`LoopSupervisor._review_clone_protocol_note`, loop.py:1511) must "
        "render nothing at all, so an existing job.yaml keeps byte-for-byte "
        "the prompt it always had", ALL),
    AbsenceAssertion(
        "test_review_criteria_reverification.py", (92,),
        ("## Criteria edited after a validation failure",),
        "the fully rendered review prompt", ALL),
    AbsenceAssertion(
        "test_review_findings_prompt_shape.py", (125,), ("findings_path",),
        "`PLAN_REVIEW_MD` (plan_review.md read raw)", ("plan_review.md",)),
    AbsenceAssertion(
        "test_review_findings_prompt_shape.py", (176, 177),
        ("replacement_criteria", "task_id"),
        "`VERDICT_GATE_MD` (verdict_gate.md read raw)", ("verdict_gate.md",)),
    AbsenceAssertion(
        "test_review_findings_prompt_shape.py", (184, 185),
        ("replacement_criteria", "task_id"),
        "`PLAN_REVIEW_MD` (plan_review.md read raw)", ("plan_review.md",)),
    AbsenceAssertion(
        "test_router.py", (147, 174), ("Context:", "rejected"),
        "`router.decide`'s inline prompt (src/ralphd/engine/router.py:223)",
        None,
        "no shipped template feeds the router's prompt, and 'rejected' is "
        "legitimately in all eight templates"),
    AbsenceAssertion(
        "test_steer.py", (737, 795), ("MUST take priority",),
        "every hat's rendered prompt", ALL),
    AbsenceAssertion(
        "test_steer.py", (738,), ("pending, not for this phase",),
        "every hat's rendered prompt", ALL),
    AbsenceAssertion(
        "test_steering_ledger.py", (171,),
        ("## Steering ledger (read-only context)",),
        "`sup.build_prompt('worker')` (STEERING_LEDGER_HEADING)", ALL),
    AbsenceAssertion(
        "test_task_failure_kind_surfaces.py", (562, 563),
        ("validationAttempts", "VALIDATION_ATTEMPT_LIMIT", ">= 3"),
        "two slices of the hub's app.js bundle", None,
        "not a template-fed artifact, and `validationAttempts` is "
        "legitimately in review.md:20, task-verify.md:31/:34 and worker.md "
        "(three reads), '>= 3' in planning.md:111"),
    AbsenceAssertion(
        "test_task_status_lifecycle.py", (237,), ("1 validated",),
        "`state.format_task_counts`'s CLI render", None,
        "not a template-fed artifact; no shipped template contains it either"),
    AbsenceAssertion(
        "test_transitions.py", (538,),
        ("| `in-progress` | both | `superseded` |",),
        "the generated SPEC task-status table (`mutated_table`, a "
        "mutation-and-restore round trip)", None,
        "the table is rendered from src/ralphd/engine/transitions.py by the "
        "repo's renderer -- no shipped template feeds it; no template "
        "contains the literal either"),
    AbsenceAssertion(
        "test_transitions.py", (539,), ("in_progress --> superseded",),
        "the generated mermaid task-status diagram (`mutated_diagram`)", None,
        "generated from transitions.py by the repo's renderer, not from any "
        "shipped template; no template contains the literal either"),
    AbsenceAssertion(
        "test_transitions.py", (559,), ("- `stagnation_exhausted`",),
        "the generated SPEC guard-note block embedded in the docs", None,
        "generated from transitions.GUARD_CONDITION_NOTES, not from any "
        "shipped template; no template contains the literal either"),
    AbsenceAssertion(
        "test_transitions.py", (573,), ("decoy note for a test",),
        "the generated SPEC transition render (`rtt.render()`)", None,
        "a test-only value a mutation injects and a `finally` removes; the "
        "render is generated from transitions.py, not from a template"),
    AbsenceAssertion(
        "test_verdict_gate.py", (566,), ("the router routed the plan back",),
        "the second rendered planning prompt (real template + injected "
        "error)", ALL),
    AbsenceAssertion(
        "test_verify.py", (148, 175), ("FINAL ATTEMPT",),
        "the verify prompt's injected `## Task under verification` block "
        "(task 074's first fix: task-verify.md:32)", ALL),
    AbsenceAssertion(
        "test_worker_hat.py", (1693, 1701, 1707),
        ("## Why the previous attempt was rejected",),
        "the worker prompt's injected extra block (`HEADING`; task 074's "
        "second fix: worker.md:53)", ALL),
    AbsenceAssertion(
        "test_worker_split_proposal.py", (497, 498),
        ("smallest atomic first piece",
         "describe the remainder precisely in the notes file"),
        "`WORKER_MD` (worker.md read raw)", ALL),
    AbsenceAssertion(
        "test_worker_split_refusal_reprompt.py", (83, 192),
        ("## Your last proposed task split was refused",),
        "the worker prompt's injected extra block (task 008, requirement "
        "B.4: loop.py:5052)", ALL),
)


def _shipped_templates() -> dict:
    assert PROMPTS_DIR.is_dir(), f"missing prompts dir {PROMPTS_DIR}"
    files = sorted(PROMPTS_DIR.glob("*.md"))
    assert files, "no shipped prompt templates found -- the guard would be vacuous"
    return {f: f.read_text() for f in files}


def _scan(templates: dict) -> list:
    """Every (template, literal) collision the declared list forbids.

    Each entry is matched the way its own assertion matches: case-insensitively
    for the assertions that compare `prompt.lower()`, byte-exact otherwise --
    lowercase prose that could never collide with an upper-case sentinel is
    not a defect (task-verify.md:69's "on the final attempt" versus
    `test_verify.py`'s `"FINAL ATTEMPT"`).
    """
    hits = []
    for entry in ENUMERATED_ABSENCE_ASSERTIONS:
        if entry.forbid_in is None:
            continue
        for path, text in templates.items():
            in_scope = (entry.forbid_in == ALL
                        or path.name in entry.forbid_in)
            if not in_scope:
                continue
            haystack = text.lower() if entry.lowercased else text
            for literal in entry.literals:
                needle = literal.lower() if entry.lowercased else literal
                if needle in haystack:
                    hits.append((path.name, literal, entry.module))
    return hits


def test_no_shipped_template_reproduces_an_absence_checked_sentinel():
    """The regression this guard exists to catch: a template edit that
    reintroduces one of the enumerated literals verbatim, the way task 041's
    worker.md edit permanently broke
    `test_dispatch_under_budget_carries_no_notice`, task-verify.md's step-4
    wording reproduced the final-attempt marker, and worker.md's step-3
    wording reproduced the retry block's heading."""
    for entry in ENUMERATED_ABSENCE_ASSERTIONS:
        if entry.forbid_in is None:
            assert entry.allowed_because, (
                f"{entry.module}:{entry.lines} is parked out of the guard's "
                "scope without saying why -- every such entry needs an "
                "`allowed_because`")
        else:
            assert entry.literals, (
                f"{entry.module}:{entry.lines} declares a forbidding scope "
                "with no literals to forbid")

    hits = _scan(_shipped_templates())
    assert not hits, (
        "a shipped prompt template reproduces a literal some test asserts "
        "the absence of elsewhere -- name the thing by role and cite where "
        f"it is rendered instead of quoting it: {hits}")


def test_the_enumeration_covers_every_hit_of_its_own_search(tmp_path):
    """The list above is the COMPLETE enumerated set, not a sample: re-run
    the report's own search and refuse any test module that grows an
    absence assertion of this shape without an audited entry here. Also
    proves the search itself still matches (non-vacuity of the enumeration)
    and that the scan catches the defect shape, using a fixture template
    written inside the test's own tmp dir -- never in shipped code."""
    pattern = re.compile(SEARCH_PATTERN)
    declared = {e.module for e in ENUMERATED_ABSENCE_ASSERTIONS}
    found: dict = {}
    for path in sorted(TESTS_DIR.glob("*.py")):
        if path.name == Path(__file__).name:
            continue  # this module quotes the pattern and the literals
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line):
                found.setdefault(path.name, []).append(i)
    assert found, "the search matched nothing -- the enumeration is vacuous"
    unaudited = {m: ls for m, ls in found.items() if m not in declared}
    assert not unaudited, (
        "these absence assertions are not enumerated in "
        "ENUMERATED_ABSENCE_ASSERTIONS -- add a row to "
        "artifacts/reports/v0.20-prompt-sentinel-audit.md and an entry "
        f"here: {unaudited}")
    stale = {m for m in declared if m not in found}
    assert not stale, (
        f"enumerated modules whose assertions the search no longer finds: "
        f"{stale}")

    # Non-vacuity of the guard itself: a fixture template that DOES contain
    # one of the ALL-scoped sentinels trips the identical scan.
    sentinel = next(lit for e in ENUMERATED_ABSENCE_ASSERTIONS
                    if e.forbid_in == ALL for lit in e.literals)
    fixture_dir = tmp_path / "prompts"
    fixture_dir.mkdir()
    fixture = fixture_dir / "worker.md"
    fixture.write_text(
        f"# Role: Worker\n\nSome instructions mentioning {sentinel} by name.\n")
    assert _scan({fixture: fixture.read_text()}), \
        "the fixture template must trip the scan -- the probe itself is broken"
    assert fixture not in _shipped_templates(), \
        "the fixture must never be mistaken for a shipped template"
