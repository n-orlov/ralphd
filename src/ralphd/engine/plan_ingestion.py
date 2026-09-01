"""ralphd.engine.plan_ingestion — ingest the planner's file-borne plan
proposal (task 011, requirement C, #60 pillar 1).

Per #60's decided channel split (fact 4, `transitions.HAT_SCHEMAS["planning"]`):
the planner's final output is only a pointer-plus-summary
(`{"proposal_path": ..., "summary": ...}`), validated exactly like any other
hat's response (`contracts.validate_payload`, via
`contract_enforcement.parse_response` — reused here, not re-derived). The
plan itself is file-borne: the planner drafts `plan-proposal.json` in its
writable scratch area, and the harness treats *that file* as untrusted
input, in three ordered steps:

  1. schema validation of the pointer payload (task 010's mechanism, as-is)
  2. structural shape validation of the proposal file's own JSON  — ad hoc
     `isinstance` checks below, deliberately NOT written through a second
     schema-shaped validator. `tests/test_response_contracts.py` scans the
     whole tree for any function that treats "required"/"properties"/"type"
     as string constants together and fails the suite if it finds one
     outside `engine/contracts.py` (fact 16/#65's lesson) — this module's
     shape check earns that by construction, not by exemption.
  3. semantic lint: `dependsOn` refs resolve, no cycles, no duplicate ids,
     no empty/boilerplate `successCriteria`, and — the enforcement point for
     requirement C's rule that a proposal "carrying a status field is
     rejected, not honoured" — no task may smuggle a `status` of its own.
     Requirement H's own-terminal-state rule (task 012, #63's incident) is
     also enforced at this step, by `lint_own_terminal_state` alongside
     `lint_proposal` — kept as a separate, separately-tested function rather
     than folded into `lint_proposal` itself.

Only after all three steps pass does `ingest_proposal` build the harness's
own `tasks.json` document, initialising every task to `pending` regardless
of anything the proposal said (belt-and-suspenders: step 3 already refuses
a proposal that tries).

A lint failure — like any other contract violation — re-prompts the planner
with the *specific* error, under the same N-then-agent-fault policy task 010
built (`JobConfig.contract_retry_max`, `EVENT_VIOLATION`/`EVENT_AGENT_FAULT`
emitted to `events.jsonl` at the moment each attempt fails, not batched).
`enforce_plan_ingestion` below is that loop, reusing
`contract_enforcement.parse_response` for step 1 rather than re-implementing
it.

The exact same function serves an approach-boundary replan: nothing here is
first-plan-only, and nothing keys off approach number (approach-scoped
namespacing of *validated/verified* task state is task 020's concern, not
ingestion's).

Nothing in `_run_job_core` calls this yet — task 014's executor cutover
decides where in the real loop a planning iteration's proposal reaches this
module. This module only has to be correct and observable in isolation,
exactly as `contracts.py` and `contract_enforcement.py` were for tasks 009
and 010.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ralphd.engine.contract_enforcement import (
    EVENT_AGENT_FAULT,
    EVENT_VIOLATION,
    EventSink,
    RespondFn,
    parse_response,
)
from ralphd.engine.tiers import DEFAULT_TIER, TOP_TIER, is_tier

# A task field carrying exactly this text is the unfilled placeholder from
# `prompts/planning.md`'s own schema example -- copy-pasted rather than
# written, which makes it "boilerplate" rather than a real criterion.
BOILERPLATE_SUCCESS_CRITERIA = (
    "<natural language, independently checkable by someone who did not do "
    "the work>"
)

# Fields a proposal's task is allowed to carry through to the harness's own
# tasks.json verbatim. Deliberately excludes "status" -- see the module
# docstring's step 3.
# Task 013 (requirement F): `tier` is passed through beside `priority`.
# Ingestion is the ONLY place a task's `tier` is ever set or normalised --
# nothing else in the harness may annotate it -- so a missing/unrecognised
# tier is defaulted here (`normalize_task_tiers` below), never left for a
# later stage to invent a value for.
_TASK_PASSTHROUGH_FIELDS: tuple[str, ...] = (
    "id",
    "title",
    "successCriteria",
    "dependsOn",
    "priority",
    "tier",
)


# ---------------------------------------------------------------------------
# Step 2: structural shape validation (deliberately not schema-shaped)
# ---------------------------------------------------------------------------

def validate_proposal_shape(payload: Any) -> list[str]:
    """Ad hoc structural check on the parsed `plan-proposal.json` document:
    an object with a `tasks` array of objects, each carrying non-blank
    `id`/`title`/`successCriteria` strings and, if present, a `dependsOn`
    that is itself a list. Nothing here treats a schema as data -- see the
    module docstring for why that distinction matters to the test suite."""
    if not isinstance(payload, dict):
        return [f"$: expected an object, got {type(payload).__name__}"]
    errors: list[str] = []
    tasks = payload.get("tasks")
    if tasks is None:
        return ["$.tasks: missing"]
    if not isinstance(tasks, list):
        return [f"$.tasks: expected an array, got {type(tasks).__name__}"]
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            errors.append(f"$.tasks[{i}]: expected an object, got {type(t).__name__}")
            continue
        tid = t.get("id")
        for field_name in ("id", "title", "successCriteria"):
            value = t.get(field_name)
            if not isinstance(value, str) or not value.strip():
                errors.append(
                    f"$.tasks[{i}] (id={tid!r}): missing or blank {field_name!r}"
                )
        if "dependsOn" in t and not isinstance(t["dependsOn"], list):
            errors.append(f"$.tasks[{i}] (id={tid!r}): 'dependsOn' must be an array")
    return errors


# ---------------------------------------------------------------------------
# Step 3: semantic lint -- structural checks only, plan *quality* is not
# the lint's business (requirement C).
# ---------------------------------------------------------------------------

def _is_boilerplate(criteria: str) -> bool:
    stripped = criteria.strip()
    if stripped == BOILERPLATE_SUCCESS_CRITERIA:
        return True
    # Any leftover "<...>" placeholder shape is boilerplate whether or not
    # it is exactly the example text above -- a worker who half-filled the
    # template still left a template.
    return stripped.startswith("<") and stripped.endswith(">")


def _detect_cycles(tasks: list[dict]) -> list[str]:
    graph: dict[Any, list[Any]] = {t.get("id"): list(t.get("dependsOn") or []) for t in tasks}
    color: dict[Any, int] = {tid: 0 for tid in graph}  # 0 white, 1 gray, 2 black
    errors: list[str] = []
    reported: set[frozenset] = set()

    def visit(node: Any, path: list[Any]) -> None:
        color[node] = 1
        for dep in graph.get(node, []):
            if dep not in graph:
                continue  # unresolved dep is reported separately
            if color.get(dep) == 1:
                cycle = path[path.index(dep):] + [dep]
                key = frozenset(cycle)
                if key not in reported:
                    reported.add(key)
                    errors.append(
                        f"task {cycle[0]!r}: dependsOn cycle: "
                        + " -> ".join(repr(c) for c in cycle)
                    )
            elif color.get(dep) == 0:
                visit(dep, path + [dep])
        color[node] = 2

    for tid in graph:
        if color[tid] == 0:
            visit(tid, [tid])
    return errors


# ---------------------------------------------------------------------------
# Task 012 (requirement D, #114): the first three of the plan's own five
# stranding lints (PRD contract rule 3, `docs/prds/v0.16-a-petition-is-not-a-postmortem.md:79-84`):
# relational sha anchors, single-commit demands on a deliverable whose own
# criteria also mandate a separate disclosure/notification/record commit,
# and forward citation of a task ordered later in the same plan. Each
# mirrors `lint_own_terminal_state`'s #86/#76 discipline (below,
# `_depends_on_own_terminal_state`): matched against ONE clause
# (`_CLAUSE_SPLIT_RE`), never the whole criteria string, so a criterion that
# merely *mentions* a sha/commit/count outside the banned shape is never
# flagged -- every lint below ships at least one must-pass sibling proving
# exactly that in `tests/test_stranding_lints.py`. Task 013 adds the
# remaining two lints (unscoped self-growing counts, scratch-state format).
#
# The must-catch fixture named by task 012's own success criteria -- "the
# real v0.15 approach-3 task 004 `HEAD~1` clause" -- cannot be lifted
# byte-for-byte: that run's own `tasks.json` lived only in its own run
# directory, torn down once the run finished, and was never committed here
# (the exact same loss `TASK_042_SUCCESS_CRITERIA` above documents for v0.8's
# task 042). What survives, verifiably, in THIS repository's tracked history
# is this PRD's own description of that clause
# (`docs/prds/v0.16-a-petition-is-not-a-postmortem.md` lines 14 and 204,
# checked directly by `tests/test_stranding_lints.py` against the tracked
# file rather than asserted in a comment): "verify with `HEAD~1`" as the
# generic pattern, and "approach 3 task 004's `HEAD~1` clause" naming this
# exact instance. `TASK_004_HEAD_ANCHOR_CRITERIA` below is a reconstruction
# built from that surviving description, not a recovered original file.
TASK_004_HEAD_ANCHOR_CRITERIA = (
    "This task's deliverable is verified by running `git diff HEAD~1 -- "
    "src/ralphd/prompts/review.md` and confirming only the intended change "
    "appears."
)

_RELATIONAL_SHA_RE = re.compile(
    r"HEAD~\d+|HEAD\^+|this commit'?s parent|the parent of this commit",
    re.IGNORECASE,
)

# "in a single commit"/"as one commit"/"exactly one commit" etc -- a real
# demand that the whole deliverable land as exactly one commit. A bare
# mention like "at least one commit" or "the commit's sha" must NOT match
# (must-pass sibling in `tests/test_stranding_lints.py`).
_SINGLE_COMMIT_DEMAND_RE = re.compile(
    r"\b(?:in|as|within)\s+(?:a\s+)?single\s+commit\b"
    r"|\bexactly\s+one\s+commit\b"
    r"|\bsingle[- ]commit\s+(?:deliverable|demand)\b",
    re.IGNORECASE,
)

# A mandate, elsewhere in the SAME criteria, that a disclosure/notification/
# record land as its own, separate, commit -- the contradiction that strands
# a single-commit demand.
_SEPARATE_RECORD_COMMIT_RE = re.compile(
    r"\b(?:separate|second|another|distinct|its\s+own)\s+"
    r"(?:disclosure|notification|record)\s+commit\b",
    re.IGNORECASE,
)

# "task 013's commit", "commit sha of task 013", "cites task 013's commit" --
# a reference to ANOTHER task's commit sha, by numeric id. A bare "task 013"
# with no commit/sha language, or a reference to THIS task's own commit,
# must not match.
_FORWARD_TASK_COMMIT_REF_RE = re.compile(
    r"task\s+(\d+)(?:'s|\u2019s)?\s+(?:own\s+)?commit(?:\s+sha)?"
    r"|commit(?:\s+sha)?\s+of\s+task\s+(\d+)"
    r"|cites?\s+task\s+(\d+)(?:'s|\u2019s)\s+commit",
    re.IGNORECASE,
)


def _first_clause_matching(criteria: str, pattern: re.Pattern) -> str | None:
    """Return the first clause of `criteria` (split by `_CLAUSE_SPLIT_RE`,
    stripped) that matches `pattern`, or None. Scoping to the clause, not
    the whole criteria string, is the #76 discipline
    `_depends_on_own_terminal_state` already applies below."""
    for clause in _CLAUSE_SPLIT_RE.split(criteria):
        stripped = clause.strip()
        if stripped and pattern.search(stripped):
            return stripped
    return None


def _relational_sha_clause(criteria: str) -> str | None:
    return _first_clause_matching(criteria, _RELATIONAL_SHA_RE)


def _single_commit_conflict_clause(criteria: str) -> str | None:
    """None unless BOTH a single-commit demand clause is present AND some
    clause of the same criteria separately mandates a disclosure/
    notification/record commit of its own -- the contradiction, not either
    half alone."""
    demand_clause = _first_clause_matching(criteria, _SINGLE_COMMIT_DEMAND_RE)
    if demand_clause is None:
        return None
    if _SEPARATE_RECORD_COMMIT_RE.search(criteria):
        return demand_clause
    return None


def _forward_task_commit_refs(criteria: str) -> list[tuple[str, str]]:
    """Return `[(clause, referenced_task_id), ...]` for every clause of
    `criteria` naming another task's commit sha."""
    results: list[tuple[str, str]] = []
    for clause in _CLAUSE_SPLIT_RE.split(criteria):
        stripped = clause.strip()
        if not stripped:
            continue
        for m in _FORWARD_TASK_COMMIT_REF_RE.finditer(stripped):
            ref = next(g for g in m.groups() if g)
            results.append((stripped, ref))
    return results


def lint_proposal(payload: dict) -> list[str]:
    """Semantic lint over an already shape-valid proposal. Every error
    names the offending task's id. This function does NOT re-check
    requirement H's own-terminal-state rule -- that lint is task 012's
    (kept separate, `lint_own_terminal_state`). It DOES enforce task 012's
    OWN three stranding lints (relational sha anchors, single-commit
    demands on a deliverable with a mandated second commit, forward
    citation of a later task's commit sha) -- see the block comment
    immediately above."""
    errors: list[str] = []
    tasks = payload.get("tasks", [])
    seen: dict[Any, int] = {}
    id_to_index: dict[Any, int] = {t.get("id"): i for i, t in enumerate(tasks)}
    for i, t in enumerate(tasks):
        tid = t.get("id")
        if tid in seen:
            errors.append(f"task {tid!r}: duplicate id (also at position {seen[tid]})")
        else:
            seen[tid] = i
        if "status" in t:
            errors.append(
                f"task {tid!r}: carries a 'status' field -- the harness "
                "initialises every ingested task to pending; a proposal "
                "must not smuggle one in"
            )
        criteria = t.get("successCriteria", "")
        if not isinstance(criteria, str) or not criteria.strip() or _is_boilerplate(criteria):
            errors.append(f"task {tid!r}: successCriteria is empty or boilerplate")
            continue

        sha_clause = _relational_sha_clause(criteria)
        if sha_clause is not None:
            errors.append(
                f"task {tid!r}: successCriteria anchors verification to a "
                f"relational sha ({sha_clause!r}) -- name the absolute sha "
                "this task's own commit lands at once it exists (e.g. "
                '"at commit `<full-sha>`"), never a relational anchor like '
                "`HEAD~1`/`HEAD^`/\"this commit's parent\", which means a "
                "different commit every time another commit lands"
            )

        commit_clause = _single_commit_conflict_clause(criteria)
        if commit_clause is not None:
            errors.append(
                f"task {tid!r}: successCriteria demands a single commit "
                f"({commit_clause!r}) while also mandating a separate "
                "disclosure/notification/record commit elsewhere in the "
                "same criteria -- require the deliverable's own commit AND "
                "its separately-committed disclosure/notification/record to "
                "each land on their own, not a single-commit demand"
            )

        for ref_clause, ref_id in _forward_task_commit_refs(criteria):
            ref_index = id_to_index.get(ref_id)
            if ref_index is not None and ref_index > i:
                errors.append(
                    f"task {tid!r}: successCriteria requires the commit sha "
                    f"of task {ref_id!r} ({ref_clause!r}), which is ordered "
                    "LATER in this same plan -- cite only a task ordered "
                    "earlier (already committed by the time this task "
                    f"runs), or move the citation into task {ref_id!r}'s "
                    "own criteria instead"
                )

    known_ids = set(seen)
    for t in tasks:
        tid = t.get("id")
        for dep in (t.get("dependsOn") or []):
            if dep not in known_ids:
                errors.append(f"task {tid!r}: dependsOn references unknown task {dep!r}")

    errors.extend(_detect_cycles(tasks))
    return errors


# ---------------------------------------------------------------------------
# Requirement H's ingestion-time half (#63's incident, fact 13, task 012):
# reject a task whose successCriteria makes THIS run's own terminal state a
# precondition of the task's own completion. v0.8's task 042 is the incident
# this exists for: approach 1's 43rd (and last) task made its closing report
# conditional on `status.json` having already left `state: running`, which
# no iteration executing *inside* that same run can ever observe -- three
# worker iterations correctly refused to fabricate it and the approach
# starved on "no task progress in 3 iterations" (docs/prds/README.md's
# settled row for `selfdev-v08-auditable`, and this repo's own commit
# `5f6b6d9` before that row was rewritten, are where that account survives).
#
# The literal task 042 JSON no longer exists anywhere reachable from this
# repo: it lived only inside that run's own run directory, which this job's
# harness tore down once the run finished, and it was never committed here.
# `TASK_042_SUCCESS_CRITERIA` below is therefore reconstructed, not lifted
# byte-for-byte from a surviving file. What IS verifiable is narrower than
# earlier commit messages here claimed (see the fix-up in task 012's second
# pass): commit `5f6b6d9^`'s copy of `docs/prds/README.md` (the parent of
# the commit that rewrote that row -- `git show 5f6b6d9^:docs/prds/README.md`)
# contains, as prose reflowed across a hard line wrap, the clause
#     required `status.json` to leave `state: running` before its closing
#     report could send
# This clause is embedded byte-for-byte (after collapsing the source's own
# whitespace-only line wrap, which changes no word) as a literal substring
# of `TASK_042_SUCCESS_CRITERIA` below -- not merely quoted in this comment.
# `tests/test_plan_ingestion.py` asserts that substring relationship directly
# against the git blob, so the claim is checked, not just written down; it
# also names the limitation that the fixture as a whole is a reconstruction,
# not a recovered original file.
TASK_042_SUCCESS_CRITERIA = (
    "Task 042's own success criteria required `status.json` to leave "
    "`state: running` before its closing report could send, which makes "
    "this run's own terminal state a precondition of this task's own "
    "success criteria being observed to hold."
)

# Cue phrases naming THIS run's own terminal state (not terminal states in
# general -- `finishing` as a non-terminal *run state* in some other task's
# criteria, e.g. task 008's, must not trip this). Matched literally against
# lowercased text, deliberately not as one giant regex, so each cue stays
# readable and auditable on its own.
_OWN_TERMINAL_STATE_CUES: tuple[str, ...] = (
    "terminal state",
    "run is terminal",
    "run has terminated",
    "run's own terminal state",
    "own terminal state",
    "run being terminal",
    "run becomes terminal",
)

# A leave/leaves/left/leaving `state: running` clause is on its own already
# the own-terminal-state trap (v0.8 task 042's exact shape), independent of
# whether the literal words "terminal state" also appear.
_LEAVES_RUNNING_RE = re.compile(
    r"le(?:ave|aves|ft|aving)\s+`?state:\s*running`?", re.IGNORECASE,
)

# Words that turn a mere mention of terminal-state vocabulary into a
# precondition on it -- "before the run is terminal" is the trap; "the run
# terminating cleanly is described in docs/architecture.md" is not.
_PRECONDITION_CUES: tuple[str, ...] = (
    "before", "until", "once", "depends on", "depend on", "requires",
    "require", "cannot", "can not", "can only", "only once", "after",
)

# #76: the two cue sets above used to be matched independently over the
# *whole* criteria string, so any criterion whose subject is terminal-state
# behaviour tripped the guard as soon as one of twelve extremely common
# precondition words appeared *anywhere else* in it -- a criterion long
# enough to be useful (~1000-1400 chars) essentially guarantees one. That is
# what rejected `selfdev-v010-not-a-verdict`'s satisfiable task 006 attempt 2
# twice: it named terminal-state vocabulary as its *subject*, not as a
# precondition of its own completion. Scope the match to one clause -- a
# terminal cue and a precondition cue only trip the guard when they occur in
# the same sentence/clause, not merely the same criterion.
_CLAUSE_SPLIT_RE = re.compile(r"[.!?;]+")


def _depends_on_own_terminal_state(criteria: str) -> bool:
    """True if `criteria` gates the task's own completion on this run
    having already reached a terminal state -- the #63 trap. A criterion
    that merely mentions "status" or a terminal state as a *fact about the
    system* (not a precondition of this task's own success) is not flagged;
    both directions have their own test in `tests/test_plan_ingestion.py`.
    The `_OWN_TERMINAL_STATE_CUES`/`_PRECONDITION_CUES` pair only trips the
    guard when both occur inside the SAME clause (#76) -- a criterion that
    merely discusses terminal-state behaviour in one sentence and uses an
    unrelated precondition word in another is not flagged."""
    lowered = criteria.lower()
    if _LEAVES_RUNNING_RE.search(lowered):
        return True
    for clause in _CLAUSE_SPLIT_RE.split(lowered):
        has_terminal_cue = any(cue in clause for cue in _OWN_TERMINAL_STATE_CUES)
        if not has_terminal_cue:
            continue
        if any(cue in clause for cue in _PRECONDITION_CUES):
            return True
    return False


def lint_own_terminal_state(payload: dict) -> list[str]:
    """Requirement H's plan-time lint (task 012): reject any task whose
    `successCriteria` depends on this run's own terminal state, naming the
    offending task and quoting the criterion. Kept separate from
    `lint_proposal` (see that function's own docstring) -- the two are
    always run together by `_parse_proposal`, but each is independently
    testable and each names its offending task the same way."""
    errors: list[str] = []
    for t in payload.get("tasks", []):
        criteria = t.get("successCriteria", "")
        if isinstance(criteria, str) and _depends_on_own_terminal_state(criteria):
            errors.append(
                f"task {t.get('id')!r}: successCriteria depends on this run's "
                f"own terminal state, which no iteration executing inside the "
                f"run can ever observe (#63's incident, v0.8 task 042): "
                f"{criteria!r}"
            )
    return errors


def lint_worker_split(proposed_tasks: Any, *, assigned_task_id: str | None,
                       existing_ids: set) -> list[str]:
    """Requirement N (#102): the semantic + shape lint for a worker's
    optional `proposed_tasks` contract field
    (`transitions.HAT_SCHEMAS["worker"]`) -- reuses the SAME three
    functions `_parse_proposal` already runs for a planner's file-borne
    proposal (`validate_proposal_shape`, `lint_proposal`,
    `lint_own_terminal_state`) rather than writing a second lint (fact
    16/#65's lesson).

    Two rules on top of that reused pipeline are what make this a SPLIT of
    the task actually assigned THIS iteration rather than unbounded new
    scope:

    * `assigned_task_id` must be real -- nothing to split if the harness
      handed this worker no task at all this pass.
    * `lint_proposal`'s own `dependsOn` resolution is already scoped to
      exactly the ids present in `proposed_tasks` itself (see that
      function's `known_ids`) -- a split cannot silently wire itself to
      unrelated plan work, which is the coherence check requirement N asks
      for. The one thing that reused check has no way to know is whether a
      proposed id collides with one already LIVE in the plan (it only ever
      sees the proposed batch) -- checked here against `existing_ids`,
      the caller's live `tasks.json` id set.

    Called from `LoopSupervisor._apply_worker_proposed_tasks`
    (`engine/loop.py`) -- this module stays engine-loop-agnostic, exactly
    like `lint_proposal`/`lint_own_terminal_state` already are; it does not
    read or write `tasks.json` itself.
    """
    if not assigned_task_id:
        return ["proposed_tasks: no task was assigned this iteration -- a "
                "split proposal requires a real task to split"]
    payload = {"tasks": proposed_tasks}
    shape_errors = validate_proposal_shape(payload)
    if shape_errors:
        return shape_errors
    errors = lint_proposal(payload) + lint_own_terminal_state(payload)
    collisions = sorted({
        t.get("id") for t in payload["tasks"]
        if isinstance(t, dict) and t.get("id") in existing_ids
    })
    if collisions:
        errors.append(
            f"proposed_tasks: id(s) already exist in the plan: {collisions}"
        )
    return errors


def normalize_task_tiers(payload: dict) -> list[str]:
    """Requirement F, task 013: normalise every task's optional `tier`
    annotation IN PLACE and return a list of WARNING strings -- a channel
    entirely distinct from `validate_proposal_shape`'s / `lint_proposal`'s
    error lists. Nothing here ever rejects a proposal.

    - A task with no `tier` key, or a `tier` that is not one of
      `tiers.TIERS`, is defaulted to `tiers.DEFAULT_TIER` ("normal") and
      earns one warning naming the task and the default applied. This is
      also what keeps an old, tier-naive proposal ingesting exactly as it
      always did -- `ingest_proposal` itself never invents a `tier` key on
      its own (a task that arrives here with no `tier` at all, i.e. this
      function was never run over it, still passes through with no `tier`
      key at all -- see that function's own docstring); this function is
      the ONLY place a missing/unrecognised annotation is ever turned into
      an explicit value.
    - A plan whose every task ends up at `tiers.TOP_TIER` ("hard") is a
      degenerate distribution -- annotating everything as the most
      expensive tier defeats the point of tiering at all -- and earns
      exactly one additional warning for the whole plan, not one per task.
    """
    warnings: list[str] = []
    tasks = [t for t in payload.get("tasks", []) if isinstance(t, dict)]
    for t in tasks:
        tier = t.get("tier")
        if tier is None:
            warnings.append(
                f"task {t.get('id')!r}: no tier annotated; defaulted to "
                f"{DEFAULT_TIER!r}"
            )
            t["tier"] = DEFAULT_TIER
        elif not is_tier(tier):
            warnings.append(
                f"task {t.get('id')!r}: unrecognised tier {tier!r}; "
                f"defaulted to {DEFAULT_TIER!r}"
            )
            t["tier"] = DEFAULT_TIER
    if tasks and all(t.get("tier") == TOP_TIER for t in tasks):
        warnings.append(
            "plan: every task is annotated tier 'hard' -- a degenerate "
            "distribution that defeats the point of tiering (every task "
            "costs as much as the most expensive one)"
        )
    return warnings


def _parse_proposal(text: str | None) -> tuple[dict | None, list[str], list[str]]:
    """Turn the raw text of `plan-proposal.json` into
    `(payload, errors, warnings)`, running steps 2 and 3 in order, then
    tier normalisation (task 013). Mirrors
    `contract_enforcement.parse_response`'s three-shapes shape, for the
    file channel instead of the inline one. `warnings` is always `[]`
    whenever `errors` is non-empty -- warnings are only ever produced for
    an already-accepted proposal."""
    if text is None or not text.strip():
        return None, ["proposal: no plan-proposal.json produced (absent)"], []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, [f"proposal: malformed JSON ({exc.msg} at line {exc.lineno} column {exc.colno})"], []
    shape_errors = validate_proposal_shape(payload)
    if shape_errors:
        return None, shape_errors, []
    lint_errors = lint_proposal(payload) + lint_own_terminal_state(payload)
    if lint_errors:
        return None, lint_errors, []
    warnings = normalize_task_tiers(payload)
    return payload, [], warnings


# ---------------------------------------------------------------------------
# Ingestion proper: the ONLY place a proposal becomes the harness's own plan
# ---------------------------------------------------------------------------

def ingest_proposal(payload: dict) -> dict:
    """Build the harness-owned `tasks.json` document from a validated,
    linted proposal. Ids/titles/successCriteria/dependsOn/priority survive
    verbatim -- nothing is renumbered or invented. Every task's `status` is
    set to `pending` unconditionally; lint (step 3) already refuses a
    proposal that tries to carry one of its own, so this is
    belt-and-suspenders, not the enforcement point."""
    tasks = []
    for t in payload.get("tasks", []):
        new_task = {k: t[k] for k in _TASK_PASSTHROUGH_FIELDS if k in t}
        new_task["status"] = "pending"
        tasks.append(new_task)
    return {
        "version": payload.get("version", 1),
        "goal": payload.get("goal", ""),
        "scope": payload.get("scope", {}),
        "repositories": payload.get("repositories", []),
        "tasks": tasks,
        "discovered": payload.get("discovered", {}),
    }


@dataclass
class PlanIngestionResult:
    """The outcome of `enforce_plan_ingestion` for one planning (or replan)
    invocation."""

    ok: bool
    tasks_doc: dict | None
    attempts: int
    agent_fault: bool
    errors: list[str] = field(default_factory=list)
    # Task 107 (#60 pillar 3): the planning contract's optional
    # `standing_rules` field, carried through so `LoopSupervisor._step_planning`
    # can pin it (`RunDir.write_standing_rules`) without re-parsing the raw
    # response itself. `""` (never `None`) when the pointer omitted the
    # field or ingestion failed -- the same "absent means nothing to pin"
    # reading `RunDir.read_standing_rules()` already uses.
    standing_rules: str = ""
    # Task 013 (requirement F): tier-normalisation warnings for THIS
    # attempt's proposal -- a channel distinct from `errors`. Always `[]`
    # when `ok` is `False` (warnings are only ever produced for an
    # already-accepted proposal; see `_parse_proposal`'s own docstring).
    # Never causes a re-prompt.
    warnings: list[str] = field(default_factory=list)


def file_reader(base_dir: Path | str) -> Callable[[str], str | None]:
    """A convenience `read_proposal` for the ordinary case: the proposal
    lives at `<base_dir>/<proposal_path>`. Returns `None` (never raises) for
    anything unreadable, so a missing/garbled file is the same ordinary
    "absent" case `_parse_proposal` already handles."""
    base = Path(base_dir)

    def _read(proposal_path: str) -> str | None:
        try:
            return (base / proposal_path).read_text()
        except OSError:
            return None

    return _read


def enforce_plan_ingestion(
    respond: RespondFn,
    *,
    max_retries: int,
    read_proposal: Callable[[str], str | None],
    state: EventSink | None = None,
) -> PlanIngestionResult:
    """Validate a planning hat's response end to end -- pointer schema,
    then the proposal file's shape and lint -- re-prompting on any failure.

    Same shape as `contract_enforcement.enforce_contract`: up to
    `max_retries + 1` calls to `respond(attempt, prior_errors)`, the
    specific error carried into the next call, every failed attempt
    `emit()`ed as `EVENT_VIOLATION` the moment it is discovered, and one
    `EVENT_AGENT_FAULT` if every attempt fails. The same function serves an
    approach-boundary replan -- nothing here is first-plan-only."""
    if max_retries < 0:
        raise ValueError("max_retries must be >= 0")

    prior_errors: list[str] | None = None
    errors: list[str] = []
    for attempt in range(max_retries + 1):
        raw = respond(attempt, prior_errors)
        pointer, pointer_errors = parse_response("planning", raw)
        if pointer_errors:
            errors = pointer_errors
        else:
            proposal_text = read_proposal(pointer["proposal_path"])
            proposal, errors, warnings = _parse_proposal(proposal_text)
            if not errors:
                return PlanIngestionResult(
                    ok=True, tasks_doc=ingest_proposal(proposal),
                    attempts=attempt + 1, agent_fault=False, errors=[],
                    standing_rules=pointer.get("standing_rules") or "",
                    warnings=warnings,
                )
        if state is not None:
            state.emit(EVENT_VIOLATION, hat="planning", attempt=attempt + 1,
                        maxAttempts=max_retries + 1, errors=list(errors))
        prior_errors = errors

    if state is not None:
        state.emit(EVENT_AGENT_FAULT, hat="planning", attempts=max_retries + 1,
                    errors=list(errors))
    return PlanIngestionResult(ok=False, tasks_doc=None, attempts=max_retries + 1,
                                agent_fault=True, errors=errors)
