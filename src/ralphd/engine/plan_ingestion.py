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
  3. semantic lint: `dependsOn` refs resolve, every ordering-only
     declaration names one of the task's own `dependsOn` ids
     (`ORDERING_ONLY_FIELD`), no cycles, no duplicate ids,
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

# GH #209 defect 3's residual: the one name of the optional per-task field a
# PLANNER uses to declare which of its OWN `dependsOn` entries are pure
# ORDERING edges -- sequencing only, producing none of the evidence that
# task's `successCriteria` reports on.
#
# Declared, never guessed. A review cure whose finding `invalidates` a path
# re-opens the plan's own evidence record for that path AND every task that
# record depends on (`state._lane_evidence_record_chain`/`insert_cure_tasks`,
# requirement E.3), because an evidence record's dependencies are usually the
# steps that PRODUCED the evidence. Measured in `selfdev-v022`: task `013`'s
# criteria named an invalidated module, `013.dependsOn == ['012']` was a plain
# ordering edge, and `012` -- already `validated`, its work landed and
# nightly-green -- was dragged back to `pending` and re-worked for it. The
# engine cannot tell the two edge kinds apart by reading criteria text (every
# textual discriminator tried either broke the pinned E.3 fixture or replaced
# a correct re-open with thinner evidence), and only the planner that wrote
# the edge knows which kind it is -- so it says so, here.
ORDERING_ONLY_FIELD = "orderingOnly"


def ordering_only_dependencies(task: dict) -> set[str]:
    """The ids in `task`'s own `dependsOn` that the plan declares are pure
    ORDERING edges (`ORDERING_ONLY_FIELD` above), as a set of strings. The
    ONE reader of that field's shape: `state._lane_evidence_record_chain` is
    its only consumer today, and `lint_proposal` below checks the RAW value
    rather than duplicating this tolerance.

    BACKWARD COMPATIBILITY -- the load-bearing safety property of the whole
    field (GH #209 defect 3's residual): an absent, non-list or empty
    declaration returns the EMPTY set, so every edge counts as
    evidence-producing. That is the conservative reading and it is exactly
    what a plan written without this field already gets, so no existing run,
    fixture or test loses a correct re-open; a plan that never names the
    field behaves identically either way.

    The result is INTERSECTED with the row's live `dependsOn`, which is what
    makes every OTHER `dependsOn` writer safe without any of them knowing
    this field exists: an edge that was re-pointed onto something else
    (`state.apply_worker_dependency_repoints` from a worker's own
    `proposed_task_updates`, a steer `task-update`, `state._repoint_
    dependents_onto_cure_task`, or the cure pass's own appended cure edge)
    was declared by nobody and therefore defaults to evidence-producing,
    while a declaration left behind for an id that is no longer a dependency
    at all is inert rather than silently applying to whatever replaced it.
    """
    raw = task.get(ORDERING_ONLY_FIELD) if isinstance(task, dict) else None
    if not isinstance(raw, list) or not raw:
        return set()
    depends = task.get("dependsOn")
    if not isinstance(depends, list):
        return set()
    live = {str(d) for d in depends}
    return {str(entry) for entry in raw if str(entry) in live}


# Fields a proposal's task is allowed to carry through to the harness's own
# tasks.json verbatim. Deliberately excludes "status" -- see the module
# docstring's step 3.
# Task 013 (requirement F): `tier` is passed through beside `priority`.
# Ingestion is the ONLY place a task's `tier` is ever set or normalised --
# nothing else in the harness may annotate it -- so a missing/unrecognised
# tier is defaulted here (`normalize_task_tiers` below), never left for a
# later stage to invent a value for.
# GH #209 defect 3's residual: `ORDERING_ONLY_FIELD` rides through beside
# `dependsOn`, since it is a declaration ABOUT those ids and is worthless to
# the evidence walk that reads it off `tasks.json` if ingestion drops it.
_TASK_PASSTHROUGH_FIELDS: tuple[str, ...] = (
    "id",
    "title",
    "successCriteria",
    "dependsOn",
    ORDERING_ONLY_FIELD,
    "priority",
    "tier",
    "estimatedIterations",
    # PRD v0.29 requirement B.1 (#280): a plan-proposal task's optional
    # boolean marker that its deliverable is a whole-tier or CI-lane run
    # over the final tree. Carried through unchanged -- never renamed,
    # never defaulted here -- so `state.insert_cure_tasks` can read it
    # straight off the ingested task; a plan with no `sweep` task behaves
    # exactly as before this field existed.
    "sweep",
)


# ---------------------------------------------------------------------------
# Step 2: structural shape validation (deliberately not schema-shaped)
# ---------------------------------------------------------------------------

def validate_proposal_shape(payload: Any) -> list[str]:
    """Ad hoc structural check on the parsed `plan-proposal.json` document:
    an object with a `tasks` array of objects, each carrying non-blank
    `id`/`title`/`successCriteria` strings and, if present, a `dependsOn`
    (and its ordering-only companion, `ORDERING_ONLY_FIELD`) that is itself
    a list. Nothing here treats a schema as data -- see the module docstring
    for why that distinction matters to the test suite."""
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
        # GH #209 defect 3's residual: the ordering-only declaration is an
        # array of ids like `dependsOn` itself, and is refused at shape time
        # for the same reason that one is -- a bare string here would read as
        # a set of one-character ids, which `ordering_only_dependencies`
        # would then quietly intersect down to nothing.
        if ORDERING_ONLY_FIELD in t and not isinstance(t[ORDERING_ONLY_FIELD], list):
            errors.append(
                f"$.tasks[{i}] (id={tid!r}): {ORDERING_ONLY_FIELD!r} must be an array")
        # Requirement D.1 (#157): `estimatedIterations`, when the planner
        # supplies one at all, must be an integer >= 1 -- a missing field
        # is fine here (that is `normalize_task_estimated_iterations`'s job,
        # step 3, to default), but a present-and-wrong one is refused at
        # shape time, exactly like `dependsOn`'s type check just above.
        # `bool` is deliberately excluded even though it is an `int`
        # subclass in Python -- `True`/`False` are not iteration counts.
        if "estimatedIterations" in t:
            value = t["estimatedIterations"]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                errors.append(
                    f"$.tasks[{i}] (id={tid!r}): 'estimatedIterations' must "
                    f"be an integer >= 1, got {value!r}"
                )
        # PRD v0.29 requirement B.1 (#280): `sweep`, when a task carries it
        # at all, must be a plain boolean -- `True`/`False`, never a string
        # (`"true"`), an int, or anything else a planner might write
        # expecting it to be read as truthy. A non-boolean value is refused
        # here, by shape, exactly where `estimatedIterations`'s own
        # present-but-wrong case is refused just above, with a message
        # naming the field.
        if "sweep" in t and not isinstance(t["sweep"], bool):
            errors.append(
                f"$.tasks[{i}] (id={tid!r}): 'sweep' must be a boolean, "
                f"got {t['sweep']!r}"
            )
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


def _find_cycles(graph: dict[Any, list[Any]]) -> list[list[Any]]:
    """The graph-only half of `_detect_cycles` below: a plain DFS over
    `graph` (id -> its own `dependsOn` list; a dep not itself a key in
    `graph` is ignored here -- unresolved deps are a separate lint) that
    returns one cycle per cycle actually found, as a plain node list
    closing back on its own first element, in first-discovery order and
    de-duplicated by node set (the same cycle reached from two different
    starting points is reported once).

    Extracted (task 001, PRD requirement D, #206) so a caller that needs
    the raw cycle shape -- `state.py`'s three `dependsOn` writers that skip
    the ingestion-time lint entirely (`insert_cure_tasks`,
    `steer_task_update`, the `task-insert` steer tool) -- can ask "does
    this candidate edge close a cycle back to this task" without
    re-parsing `_detect_cycles`'s own formatted strings. `_detect_cycles`
    itself is unchanged in behaviour: same messages, same order, same
    dedup -- this is a pure refactor of its internals, not a change to
    what it says (D.5).
    """
    color: dict[Any, int] = {tid: 0 for tid in graph}  # 0 white, 1 gray, 2 black
    cycles: list[list[Any]] = []
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
                    cycles.append(cycle)
            elif color.get(dep) == 0:
                visit(dep, path + [dep])
        color[node] = 2

    for tid in graph:
        if color[tid] == 0:
            visit(tid, [tid])
    return cycles


def _detect_cycles(tasks: list[dict]) -> list[str]:
    graph: dict[Any, list[Any]] = {t.get("id"): list(t.get("dependsOn") or []) for t in tasks}
    return [
        f"task {cycle[0]!r}: dependsOn cycle: " + " -> ".join(repr(c) for c in cycle)
        for cycle in _find_cycles(graph)
    ]


def dependency_cycles(tasks: list[dict]) -> list[list[str]]:
    """Task 002 (PRD requirement E, #206): the raw cycle shape `_detect_
    cycles` above formats into strings and `validate_dependency_edges`
    checks edge-by-edge, exposed here for a caller OUTSIDE this module --
    `LoopSupervisor._unschedulable_cycle_fault` (`loop.py`) -- that needs
    the node list itself to name the offending edge(s) on an event
    payload, applied at SELECTION time (every pass nothing is selectable)
    as a THIRD safety net beside this module's own ingestion-time lint and
    `validate_dependency_edges`'s write-time guard: an edge that reached
    `tasks.json` despite both (#206's own incident -- a cure task's
    `dependsOn` naming its own id, written before task 001's validator
    existed on that path, or before it applied to that path at all) is
    still caught here, on the very first pass nothing is schedulable,
    rather than never.

    Same graph construction `_detect_cycles` uses, same `_find_cycles`
    DFS -- a pure re-exposure of the shape that already existed, not new
    detection logic. Ids are stringified and non-dict/blank-id entries
    skipped, matching `state.task_blocked_by`'s own defensiveness, so a
    caller never has to guess whether a raw `tasks.json` id (which may be
    any JSON scalar) compares consistently or crashes on a malformed
    entry.
    """
    graph: dict[str, list[str]] = {}
    for t in tasks:
        if not isinstance(t, dict) or t.get("id") in (None, ""):
            continue
        tid = str(t["id"])
        raw = t.get("dependsOn")
        graph[tid] = [str(d) for d in raw] if isinstance(raw, list) else []
    return _find_cycles(graph)


def validate_dependency_edges(tasks: list[dict], task_id: str,
                              candidate_depends_on: list) -> tuple[list, list[dict]]:
    """Task 001 (PRD requirement D, #206): the one validator the three
    writers into `tasks.json` that skip `lint_proposal`/`_detect_cycles`
    entirely -- `state.insert_cure_tasks`, `state.steer_task_update`'s
    `dependsOn` merge, and the `task-insert` steer tool -- run a candidate
    `dependsOn` list for `task_id` through before it is ever written.

    Edges are validated one at a time, in `candidate_depends_on` order,
    against the rest of `tasks` (which is never mutated here) plus every
    edge already accepted earlier in this same call -- so two candidate
    edges that only close a cycle together (each fine alone) are both
    caught once the second is checked against the first. Two refusal
    shapes, both structural:

    - the id does not name any task actually present in `tasks` (an
      unresolved dependency);
    - the edge would close a path back to `task_id` -- built on
      `_find_cycles`, the exact DFS `_detect_cycles`'s own ingestion-time
      lint uses, so this is the SAME cycle detection, not a second copy of
      the rule. A self-loop (`task_id` naming itself) is the length-one
      case of this and is caught by the identical check, not a special
      case bolted on beside it.

    Returns `(accepted, rejected)`: `accepted` is `candidate_depends_on`
    with every bad edge dropped, order preserved; `rejected` is one
    `{"edge": <id>, "reason": <text>}` dict per dropped edge, same order,
    so a caller can turn each into its own named refusal event (PRD D.3)
    while still writing the accepted remainder -- a validation failure
    here costs exactly the bad edge, never the whole write, and never the
    task the edge was attached to.
    """
    ids_present = {t.get("id") for t in tasks
                  if isinstance(t, dict) and t.get("id")}
    accepted: list = []
    rejected: list[dict] = []
    for dep in candidate_depends_on or []:
        if dep == task_id:
            rejected.append({
                "edge": dep,
                "reason": (
                    f"dependsOn refused: task {task_id!r} names itself -- "
                    "a self-loop is a dependsOn cycle of length one"),
            })
            continue
        if dep not in ids_present:
            rejected.append({
                "edge": dep,
                "reason": (
                    f"dependsOn refused: task {task_id!r} names unknown "
                    f"task {dep!r} -- not in the current plan"),
            })
            continue
        graph = {
            t.get("id"): list(t.get("dependsOn") or [])
            for t in tasks
            if isinstance(t, dict) and t.get("id") and t.get("id") != task_id
        }
        graph[task_id] = accepted + [dep]
        if any(task_id in cycle for cycle in _find_cycles(graph)):
            rejected.append({
                "edge": dep,
                "reason": (
                    f"dependsOn refused: task {task_id!r} -> {dep!r} would "
                    "close a dependsOn cycle"),
            })
            continue
        accepted.append(dep)
    return accepted, rejected


# ---------------------------------------------------------------------------
# Task 012 (requirement D, #114): the first three of the plan's own five
# stranding lints (PRD contract rule 3, `docs/prds/v0.16-a-petition-is-not-a-postmortem.md:79-84`):
# relational sha anchors, single-commit demands on a deliverable whose own
# criteria also mandate a separate disclosure/notification/record commit,
# and forward citation of a task ordered later in the same plan. Task 013
# (this block's continuation, below `_forward_task_commit_refs`) adds the
# remaining two: an unscoped write-time occurrence count of a string the
# task's own mandated output contains, and a criterion conditioning on the
# content or format of untracked run state. Each mirrors
# `lint_own_terminal_state`'s #86/#76 discipline (below,
# `_depends_on_own_terminal_state`): matched against ONE clause
# (`_CLAUSE_SPLIT_RE`), never the whole criteria string, so a criterion that
# merely *mentions* a sha/commit/count outside the banned shape is never
# flagged -- every lint below ships at least one must-pass sibling proving
# exactly that in `tests/test_stranding_lints.py`.
#
# The must-catch fixture named by task 012's own success criteria -- "the
# real v0.15 approach-3 task 004 `HEAD~1` clause". Task 013 (third pass,
# closing task 012's residual gap, see 012's `validationNotes`) replaces the
# earlier RECONSTRUCTION of that clause with the real one, RECOVERED
# verbatim from this repository's own tracked v0.15 records: the approach-3
# task 004 entry of `artifacts/reports/v0.15-operator-notifications.md`
# carries the relational-sha verification clause below word for word (its
# `## Task 004 (approach 3, requirement F)` section). The v0.15 run's own
# `tasks.json` is indeed gone with its run directory (the loss
# `TASK_042_SUCCESS_CRITERIA` above documents for v0.8's task 042), but the
# clause itself survives here as a literal string, so nothing here has to be
# reconstructed -- `tests/test_stranding_lints.py` verified (task 013) that
# the recovered string was a verbatim (whitespace-flattened) substring of
# that tracked record before N.1 (task 061) deleted `artifacts/reports/`
# wholesale; only the minimal criterion frame around it is this module's.
V015_TASK_004_RECOVERED_RELATIONAL_SHA_CLAUSE = (
    "`git diff --stat HEAD~1 HEAD` showing only additions to that file"
)

V015_TASK_004_RECOVERED_CLAUSE_SOURCE = (
    "artifacts/reports/v0.15-operator-notifications.md"
)

TASK_004_HEAD_ANCHOR_CRITERIA = (
    "This task's deliverable is verified by "
    + V015_TASK_004_RECOVERED_RELATIONAL_SHA_CLAUSE
    + "."
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
# Task 013 third pass (task 012's residual gap (a), see 012's
# `validationNotes`): this cue used to be word-order sensitive -- it only
# understood "a separate notification commit", so the commonest wording of
# all, "The notification must land in a separate commit", slipped through.
# Both orders now count: modifier-then-noun AND noun-then-modifier with the
# verb phrase in between.
_SEPARATE_RECORD_COMMIT_RE = re.compile(
    r"\b(?:separate|second|another|distinct|its\s+own)\s+"
    r"(?:disclosure|notification|record|report)\s+commit\b"
    r"|\b(?:disclosure|notification|record|report)\b"
    r"(?:\W+\w+){0,8}?\W+"
    r"(?:separate|second|another|distinct|own)\s+commit\b",
    re.IGNORECASE,
)

# "task 013's commit", "commit sha of task 013", "cites task 013's commit" --
# a reference to ANOTHER task's commit sha. Task 013 third pass (task 012's
# residual gap (b), see 012's `validationNotes`): the id was matched as
# `\d+`, so a plan with non-numeric ids (`alpha` citing a later `beta`)
# escaped the lint entirely. Any id token now matches; the reference is only
# ever REPORTED when the token resolves to a task ordered later in the same
# plan, so widening the token cannot invent a finding. A bare "task 013"
# with no commit/sha language, or a reference to THIS task's own commit,
# still must not match.
_TASK_ID_TOKEN = r"[A-Za-z0-9_][A-Za-z0-9_.\-]*"

_FORWARD_TASK_COMMIT_REF_RE = re.compile(
    rf"task\s+`?({_TASK_ID_TOKEN})`?(?:'s|\u2019s)?\s+(?:own\s+)?commit(?:\s+sha)?"
    rf"|commit(?:\s+sha)?\s+of\s+task\s+`?({_TASK_ID_TOKEN})`?"
    rf"|cites?\s+task\s+`?({_TASK_ID_TOKEN})`?(?:'s|\u2019s)\s+commit",
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


def _resolve_task_index(id_to_index: dict, ref_id: str) -> int | None:
    """Map a task id token lifted out of a criteria clause onto its position
    in this same proposal. Ids arrive as strings from the text; a plan may
    carry them as ints (`13`) or in another case (`Beta`), so all three
    resolutions are tried before giving up. Returns None when the token
    names no task in this plan -- the lint then stays silent, which is why
    widening `_TASK_ID_TOKEN` to non-numeric ids cannot invent a finding."""
    if ref_id in id_to_index:
        return id_to_index[ref_id]
    if ref_id.isdigit():
        try:
            numeric = int(ref_id)
        except ValueError:  # pragma: no cover - isdigit already guarantees it
            numeric = None
        if numeric is not None and numeric in id_to_index:
            return id_to_index[numeric]
    lowered = ref_id.lower()
    for known, index in id_to_index.items():
        if isinstance(known, str) and known.lower() == lowered:
            return index
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


# ---------------------------------------------------------------------------
# Task 013 (requirement D, #114): the remaining two of the plan's own five
# stranding lints -- an unscoped write-time occurrence count of a string
# the task's own mandated output contains (PRD lines 15-17: "a write-time
# occurrence count of a sha that the task's own mandated additions
# increment"), and a criterion conditioning on the content or format of
# untracked run state (contract rule 1, PRD lines 71-74; the real v0.15
# case is approach 4 task 001's notes.md demand, PRD lines 18-20 and 204).
#
# Neither fixture below is a reconstruction any more. Task 013 third pass
# (closing the residual gaps validation named in task 012's and this task's
# own `validationNotes`) anchors both on text RECOVERED verbatim from this
# repository's tracked files:
#   * `V015_TASK_001_RECOVERED_NOTES_CONTENT_CLAUSE` is the operative clause
#     of the real v0.15 approach-4 task-001 incident, quoted word for word
#     out of the tracked PRD (`docs/prds/v0.16-a-petition-is-not-a-postmortem.md`,
#     the paragraph at lines 18-20); only the minimal criterion frame around
#     it belongs to this module.
#   * `V015_RECOVERED_WRITE_TIME_COUNT_PATTERN` is the tracked PRD's own
#     word-for-word statement of the count shape that stranded v0.15 (no
#     individual v0.15 task number survives for this one, the PRD records
#     the shape); `UNSCOPED_SHA_OCCURRENCE_CRITERIA` instantiates exactly
#     that recovered shape.
# `tests/test_stranding_lints.py` asserts each recovered string is a verbatim
# (whitespace-flattened) substring of the tracked file named beside it, so
# the recovery is checked, not claimed.
V015_TASK_001_RECOVERED_NOTES_CONTENT_CLAUSE = (
    "`/run/ralphd/notes.md` *pointed to* the artifact holding 19 stale-sha "
    "pairs instead of inlining them"
)

V015_RECOVERED_WRITE_TIME_COUNT_PATTERN = (
    "a write-time occurrence count of a sha that the task's own mandated "
    "additions increment"
)

V015_RECOVERED_CLAUSE_SOURCE_PRD = (
    "docs/prds/v0.16-a-petition-is-not-a-postmortem.md"
)

TASK_001_NOTES_INLINE_CRITERIA = (
    "This task's landed fix is not accepted while "
    + V015_TASK_001_RECOVERED_NOTES_CONTENT_CLAUSE
    + "."
)

UNSCOPED_SHA_OCCURRENCE_CRITERIA = (
    "Once this task's own commit lands, the record must show that the sha "
    "`abc1234` appears exactly 4 times, even though this same task's own "
    "mandated additions to that record append further occurrences of that "
    "sha."
)

# Task 013 second pass -- the first pass got BOTH directions of this lint
# wrong and validation caught it: it fired on any "contains exactly N"
# clause (a STATIC count like "the API response fixture must contain
# exactly 4 entries" is perfectly verifiable and must pass), and it missed
# a self-growing count written with a spelled-out number ("four
# occurrences"). The criterion the contract actually bans is narrower than
# "a count": it is *an unscoped write-time count of a string the task's own
# mandated output contains*. So this lint now requires all three of:
#   1. a count-of-a-string shape (digits OR a spelled-out number),
#   2. no sha scope on that clause (`at \`<sha>\`: N` is the passing form),
#   3. the criteria themselves declaring that the task's OWN output grows
#      what is being counted -- an ownership cue and a growth-of-the-
#      counted-thing verb inside one clause.
# Without (3) the count is static and no re-write can strand it, which is
# exactly why the static-count sibling must pass.

# A number, in either notation. "all" counts too ("all 19 occurrences").
_COUNT_NUM = (
    r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
    r"|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)"
)

_COUNTED_NOUN = (
    r"(?:occurrences?|instances?|entries|entry|pairs?|times|matches|copies"
    r"|mentions?|appearances?)"
)

# "N occurrences/instances/entries/...", "appears exactly N times",
# "contains exactly N occurrences", "a count of N occurrences" -- a count of
# how many times a string shows up. A bare mention of a count with no
# occurrence-of-a-string language ("19 tasks remain") must not match.
_OCCURRENCE_COUNT_RE = re.compile(
    rf"\b(?:all\s+)?{_COUNT_NUM}\s+{_COUNTED_NOUN}\b"
    rf"|\b(?:appears?|appear|occurs?|shows?\s+up)\s+(?:exactly\s+)?"
    rf"{_COUNT_NUM}\s+times?\b"
    rf"|\bcount\s+of\s+{_COUNT_NUM}\s+{_COUNTED_NOUN}\b",
    re.IGNORECASE,
)

# The task claiming the counted output as its OWN.
_OWN_OUTPUT_CUE_RE = re.compile(
    r"\bthis (?:same )?task(?:'s)?\b"
    r"|\bthe task's own\b"
    r"|\bits own\b"
    r"|\bown (?:commit|additions|output|mandated|entry|entries|record)\b",
    re.IGNORECASE,
)

# ...growing the very thing being counted ("appends further occurrences",
# "grow that count", "increments the number of entries"). A growth verb
# with any other object ("this task's own commit adds the fixture") is not
# self-growth of a count and must not make a static count trip.
_GROWTH_OF_COUNTED_RE = re.compile(
    r"\b(?:append\w*|add\w*|grow\w*|increment\w*|extend\w*|bump\w*)\b"
    r"(?:\W+\w+){0,6}?\W+"
    r"(?:occurrences?|counts?|entries|entry|instances?|pairs?|matches|times"
    r"|figures?|lines?|mentions?|further|more)\b",
    re.IGNORECASE,
)

# The sha-scoped form the standing rules mandate for every figure in a
# tracked record ("at `<sha>`: N") -- a clause carrying this marker is
# already order-honest and must never trip `_OCCURRENCE_COUNT_RE` above.
_SHA_SCOPED_FIGURE_RE = re.compile(
    r"\bat\s+(?:commit\s+)?`?(?:[0-9a-f]{7,40}|<sha>)`?\s*:\s*\d+",
    re.IGNORECASE,
)


def _declares_self_growing_output(criteria: str) -> bool:
    """True if these criteria themselves say the task's OWN mandated output
    grows the thing being counted -- the write-time half of lint (d). Both
    cues must land in the same clause, so an unrelated "this task's own
    commit" sentence elsewhere cannot turn a static count into a false
    positive (must-pass sibling in `tests/test_stranding_lints.py`)."""
    for clause in _CLAUSE_SPLIT_RE.split(criteria):
        stripped = clause.strip()
        if not stripped:
            continue
        if _OWN_OUTPUT_CUE_RE.search(stripped) and _GROWTH_OF_COUNTED_RE.search(stripped):
            return True
    return False


# Task 013 third pass. Validation caught the two-cue test above missing the
# plainest shape of all: the count clause names the counted STRING
# ("four occurrences of `abc123`") and a NEIGHBOURING clause says the task's
# own mandated output carries that same string ("this task's mandated output
# is a new line in that report containing `abc123`") -- self-growth stated
# without a growth verb anywhere near a counted noun, so
# `_GROWTH_OF_COUNTED_RE` never fired. The token-identity route below covers
# it: the counted string must literally reappear in a clause that both
# claims the output as the task's OWN and says that output carries/produces
# it. A static count stays legitimate, because a static count names no
# string the task's own output contains (must-pass siblings in
# `tests/test_stranding_lints.py`).
_COUNTED_STRING_TOKEN_RE = re.compile(
    r"`([^`\n]{2,80})`" r"|'([^'\n]{2,80})'" r"|\"([^\"\n]{2,80})\""
)

# How far from the count phrase a backticked/quoted literal may sit and still
# be read as the string being counted.
_COUNTED_TOKEN_WINDOW = 60

_OWN_OUTPUT_CARRIES_RE = re.compile(
    r"\b(?:contain\w*|includ\w*|carr(?:y|ies|ying)|mention\w*|nam(?:e|es|ing)"
    r"|append\w*|add\w*|writ\w*|record\w*|grow\w*|increment\w*|repeat\w*"
    r"|further|another|additional)\b"
    r"|\bnew\s+(?:line|lines|entry|entries|row|rows|occurrence|occurrences"
    r"|mention|mentions|section)\b",
    re.IGNORECASE,
)


def _counted_string_tokens(clause: str) -> list[str]:
    """Every backticked or quoted literal sitting NEXT TO a count phrase in
    `clause` -- the candidate strings whose occurrences are being counted.
    Proximity matters: a criterion long enough to be useful also names
    directories, modules and shas that have nothing to do with the count
    (`artifacts/reports/`, `tests/foo.py`), and treating those as the
    counted string turned an ordinary sha-scoped criterion into a false
    positive when its boilerplate notification clause happened to mention
    the same directory. Only literals within
    `_COUNTED_TOKEN_WINDOW` characters of the count phrase count, and a
    literal that is plainly a directory prefix (trailing `/`) never does."""
    tokens: list[str] = []
    windows = [
        (max(0, m.start() - _COUNTED_TOKEN_WINDOW), m.end() + _COUNTED_TOKEN_WINDOW)
        for m in _OCCURRENCE_COUNT_RE.finditer(clause)
    ]
    if not windows:
        return tokens
    for m in _COUNTED_STRING_TOKEN_RE.finditer(clause):
        token = next((g for g in m.groups() if g), "").strip()
        if not token or token.endswith("/"):
            continue
        if any(m.start() < end and m.end() > start for start, end in windows):
            tokens.append(token)
    return tokens


def _counted_token_is_in_own_output(criteria: str, count_clause: str) -> bool:
    """True if a string counted by `count_clause` reappears in a clause of
    the same criteria that claims the task's OWN output and says that output
    carries or produces it -- "a count of a string the task's own mandated
    output contains", stated without any growth verb."""
    tokens = _counted_string_tokens(count_clause)
    if not tokens:
        return False
    for clause in _CLAUSE_SPLIT_RE.split(criteria):
        stripped = clause.strip()
        if not stripped:
            continue
        if not _OWN_OUTPUT_CUE_RE.search(stripped):
            continue
        if not _OWN_OUTPUT_CARRIES_RE.search(stripped):
            continue
        lowered = stripped.lower()
        if any(token.lower() in lowered for token in tokens):
            return True
    return False


def _unscoped_occurrence_count_clause(criteria: str) -> str | None:
    """None unless some clause demands an unscoped write-time occurrence
    count of a string the task's own mandated output grows or contains. A
    clause already carrying the sha-scoped ``at `<sha>`: N`` form is
    legitimate, and so is a STATIC count of something the task does not
    grow -- both have must-pass siblings in
    `tests/test_stranding_lints.py`."""
    count_clauses = [
        stripped
        for stripped in (c.strip() for c in _CLAUSE_SPLIT_RE.split(criteria))
        if stripped
        and _OCCURRENCE_COUNT_RE.search(stripped)
        and not _SHA_SCOPED_FIGURE_RE.search(stripped)
    ]
    if not count_clauses:
        return None
    if _declares_self_growing_output(criteria):
        return count_clauses[0]
    for clause in count_clauses:
        if _counted_token_is_in_own_output(criteria, clause):
            return clause
    return None


# Untracked run state this contract's scratch-state rule names by example
# (`/run/ralphd/notes.md`, handoff notes) or by category (anything outside
# the repository tree, "scratch state"). Task 013 second pass: validation
# caught this cue set accepting explicit content demands on other concrete
# out-of-tree paths (`/tmp/handoff.txt`, `/run/ralphd/tasks.json`), so any
# absolute path rooted in a filesystem directory that cannot be inside the
# repository tree now counts as untracked run state on its own. Task 013
# THIRD pass: validation caught the fixed root list still missing roots it
# never enumerated (`/srv/ralphd-handoff.txt`, `/data/run-handoff.txt`) and
# missing parent-escape relative paths (`../run-handoff.txt`) entirely. Two
# general cues replace the enumeration's role: ANY absolute path whose last
# component looks like a file (a dotted extension), and ANY `../` escape --
# neither can name a tracked path inside this repository. `/workspace/...`,
# the checkout itself, is deliberately excluded, and a URL path (`GET
# /repos/n-orlov/ralphd/issues/114`, `POST .../notify`) matches nothing here:
# it carries no file extension and its leading slash is preceded by a word
# character or is a bare single component.
_UNTRACKED_PATH_CUE_RE = re.compile(
    r"/run/ralphd/notes\.md"
    r"|handoff notes"
    r"|\bnotes\.md\b"
    r"|\bscratch[- ]state\b"
    r"|\buntracked run state\b"
    r"|\brun (?:state )?director(?:y|ies)\b"
    r"|outside (?:the )?repo(?:sitory)?(?:\s+tree)?"
    r"|(?<![\w./])/(?!workspace/)(?:run|tmp|var|etc|home|root|proc|sys|dev"
    r"|mnt|opt|usr|srv|data|media|lib|boot)/"
    r"|(?<![\w./])/(?!workspace/)[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]*"
    r"[A-Za-z0-9_-]\.[A-Za-z0-9]{1,6}\b"
    r"|(?<![\w.])\.\./[A-Za-z0-9_./-]+",
    re.IGNORECASE,
)

# Words that turn a mere mention of untracked run state into a content/
# format demand on it -- "inline"/"verbatim"/"must show" are the trap; a
# clause that only asks the path to point at, or the artifact to exist,
# never uses one of these (must-pass sibling in
# `tests/test_stranding_lints.py`).
# Task 013 second pass: validation caught the modal list here as far too
# short -- "The handoff notes must list all 19 stale sha pairs" is exactly
# the banned shape and "must list" was not in it. Every modal + content
# verb pairing now counts; existence and pointer wording ("must exist",
# "need only point to") deliberately does not.
# Task 013 THIRD pass: validation caught a FORMAT condition slipping through
# whole -- "The handoff notes are required to use valid JSON." -- because
# neither "use" nor any format-language noun was a cue. Format verbs
# (use/follow/conform/be valid/be written in/parse) and explicit
# format-language ("valid JSON", "JSON-formatted") are cues now, since
# contract rule 1 bans conditioning on the FORMAT of untracked run state
# just as squarely as on its content.
_CONTENT_OR_FORMAT_DEMAND_RE = re.compile(
    r"\binlin\w*\b"
    r"|\bverbatim\b"
    r"|\b(?:must|shall|should|has to|have to|needs? to|is required to"
    r"|are required to|only once)\s+(?:also\s+|itself\s+|already\s+|still\s+)?"
    r"(?:show|read|contain|contains|list|record|include|name|state|enumerate"
    r"|say|mention|carry|spell|match|reproduce|duplicate|quote|end|begin"
    r"|start|use|uses|reuse|employ|adopt|follow|conform|adhere|parse"
    r"|hold|store|reflect|restate|repeat|copy|mirror|summari[sz]e"
    r"|be\s+valid\w*|be\s+written|be\s+in|be\s+parse?able|be\s+formatted)\b"
    r"|\bcontains?\b"
    r"|\bformat\w*\b"
    r"|\bvalid\s+(?:JSON|YAML|TOML|CSV|markdown|ND?JSON)\b"
    r"|\b(?:JSON|YAML|TOML|CSV)[- ]formatted\b"
    r"|\bduplicat\w*\b"
    r"|\bquote\w*\b",
    re.IGNORECASE,
)


def _scratch_state_content_clause(criteria: str) -> str | None:
    """None unless some clause both names untracked run state AND demands
    something about its content or format, not merely its existence or a
    pointer to a declared artifact (contract rule 1) -- the must-pass
    sibling in `tests/test_stranding_lints.py`."""
    for clause in _CLAUSE_SPLIT_RE.split(criteria):
        stripped = clause.strip()
        if not stripped:
            continue
        if _UNTRACKED_PATH_CUE_RE.search(stripped) and _CONTENT_OR_FORMAT_DEMAND_RE.search(stripped):
            return stripped
    return None


# ---------------------------------------------------------------------------
# Task 013 (requirement E, #143): the plan's SEVENTH lint -- criteria that
# cannot be satisfied by construction, the class the operator's own
# completion analysis (#139-#147) found responsible for 37 of 118 blocking
# findings across 11 failed selfdev/deck runs (issue #143's own body, fetched
# from the tracked GitHub issue rather than reconstructed prose). Six shapes,
# each scoped to ONE clause via `_CLAUSE_SPLIT_RE`, never the whole criteria
# string (the #86 lesson every earlier lint in this module already follows):
#
#   (h) a reference to a later/final/last commit or its sha -- recovered
#       verbatim from this repository's own tracked PRDs: v0.14's non-
#       negotiable 12 ('"The last commit is followed by requirement B's
#       guard subset", green, recorded.'), v0.15's/v0.17's identical
#       requirement-list bullet ('"The final guard subset at the true final
#       sha", recorded as the last commit,'), the exact clause
#       `artifacts/reports/v0.17-a3-closing-report.md`'s Finding 1 quotes
#       from the v0.17 PRD when it rejected approach 3 on it.
#   (i) a predeclared event state ('none newly filed', 'no approach
#       rejected', 'all petitions REFUTED') -- recovered verbatim from
#       `artifacts/reports/v0.17-a3-closing-report.md` (and reprinted
#       identically in the a4 report): the task-014 successCriteria clause
#       2 parenthetical "its petition state (both prior petitions REFUTED,
#       none newly filed)", later CONFIRMED unsatisfiable by that same run's
#       own mid-run adjudication (#143's own account of why this shape is
#       banned, not a claim invented here).
#   (j) a record required to cite the commit that writes it -- the exact
#       impossibility `artifacts/reports/v0.17-a3-closing-report.md` states
#       in so many words ("A file cannot contain the sha of the commit that
#       writes it -- the hash is computed from the tree the commit
#       produces"); the concrete recurrence issue #143 attributes to
#       `deck-3i-2` (x2) is used VERBATIM as the issue itself renders that
#       run's clause -- "a record row must cite the commit that writes the
#       row" -- pinned against `tests/data/issue-143-body.md`, the issue
#       body tracked verbatim in this repository for exactly that purpose
#       (the deck runs' own directories belong to a different product and
#       are not reachable from this sandbox, so #143's text IS the
#       verbatim source of record for them).
#   (k) a git range required to be empty -- issue #143's verbatim clause
#       for `deck-3h` (x2) / `3i-2` (x4) / `spike` (x3): "a protected-path
#       audit range that begins before the PRD/plan commit itself", used
#       verbatim as a must-catch fixture and pinned the same way, beside
#       the spelled-out `git log`/`git diff` form of the same demand.
#   (l) an "exactly once" commit count -- issue #143's verbatim clause for
#       `deck-spike` (x4): "commit exactly once" combined with "correct the
#       report the review faulted", both halves used verbatim and pinned
#       the same way.
#   (m) a notification or HTTP-status clause -- recovered verbatim from
#       `artifacts/reports/v0.17-a4-closing-report.md` Finding 2's own
#       quoted clause: "every task appends endpoint, substance, and
#       observed HTTP status" (the missing approach-03 task-004
#       notification ledger entry finding).
#
# Two must-pass shapes recur across all six: an existing tracked artifact
# (a path, a record) named as evidence, and "the code sha this task lands"
# -- neither states a later event or an impossible relation, both are
# ordinary evidence citations. `tests/test_ingestion_lints.py` is this
# task's new fixture module, in both directions for every shape.
#
# Scope note for (m): the PRD's prohibition is absolute -- "no criterion
# may mention a send, a ledger entry or an HTTP status" -- so an "observed
# HTTP status" is refused whether or not a modal demands it, and two
# older must-pass fixtures in `tests/test_stranding_lints.py` (ids 029,
# 035) that quoted one had their assertions narrowed to their OWN lint's
# route rather than global emptiness. Their fixture text is unchanged.

_LATER_FINAL_LAST_COMMIT_RE = re.compile(
    r"\b(?:the\s+)?(?:last|final|later|next|subsequent)\s+commit\b"
    r"|\bcommit\s+(?:that\s+)?(?:comes|lands|is\s+made)\s+after\b"
    # ... and the same reference made through the *sha* instead of the
    # commit: "the final sha", "the true final sha", "the run's last sha",
    # "the sha of the final commit", "the last commit's sha". Task 013's
    # first two validation rounds faulted exactly this gap: `The report
    # must contain the final sha.` linted clean because only the word
    # "commit" was cued.
    r"|\b(?:true\s+)?(?:last|final|later|next|subsequent)\s+sha\b"
    r"|\bsha\s+of\s+the\s+(?:true\s+)?(?:last|final|later|next|subsequent)\s+commit\b"
    r"|\b(?:last|final)\s+commit'?s\s+sha\b",
    re.IGNORECASE,
)

_PREDECLARED_EVENT_STATE_RE = re.compile(
    r"none\s+newly\s+filed"
    r"|no\s+approach\s+(?:was\s+|were\s+)?rejected"
    r"|(?:both|all)\s+(?:prior\s+)?petitions?\s+(?:were\s+)?REFUTED"
    r"|(?:both|all)\s+(?:prior\s+)?petitions?\s+(?:were\s+)?CONFIRMED",
    re.IGNORECASE,
)

_RECORD_CITES_OWN_WRITING_COMMIT_RE = re.compile(
    r"cites?\s+the\s+(?:sha\s+of\s+the\s+)?commit\s+that\s+writes\s+it"
    r"|the\s+commit\s+that\s+writes\s+(?:it|this|the\s+row|that\s+row)"
    r"|names?\s+the\s+(?:sha\s+of\s+the\s+)?commit\s+that\s+(?:writes|adds)\s+it",
    re.IGNORECASE,
)

_EMPTY_GIT_RANGE_RE = re.compile(
    r"git\s+(?:log|diff)\b[^.;!?]{0,80}\b(?:empty|no\s+commits?|zero\s+commits?)\b"
    r"|\ban?\s+empty\s+(?:git\s+)?range\b"
    r"|\brange\b[^.;!?]{0,40}\b(?:must\s+be|is|shows?)\s+empty\b"
    # The concrete recurrence issue #143 records for `deck-3h`/`3i-2`/
    # `spike` is this same shape worded as an audit: "a protected-path
    # audit range that begins before the PRD/plan commit itself" -- a
    # range whose emptiness is the standing invariant. The PRD's own
    # prohibition list names "a protected-path clause" and "an audit
    # range" for exactly this reason.
    r"|\baudit\s+range\b"
    r"|\bprotected[-\s]paths?\b"
    r"|\brange\b[^.;!?]{0,60}\bbegins?\s+before\b",
    re.IGNORECASE,
)

_EXACTLY_ONCE_COMMIT_COUNT_RE = re.compile(
    r"commit(?:s|ted)?\s+exactly\s+once\b"
    r"|exactly\s+once\b[^.;!?]{0,40}\bcommit",
    re.IGNORECASE,
)

_NOTIFICATION_OR_HTTP_STATUS_RE = re.compile(
    r"\bnotification\s+ledger\b"
    r"|\bledger\s+entry\b"
    r"|\bnotification\s+inventory\b"
    # Any mention of an HTTP status at all, modal or not: the verbatim
    # v0.17-a4 clause "every task appends endpoint, substance, and
    # observed HTTP status" carries no modal, and task 013's validation
    # rounds faulted the modal-only form as materially underinclusive.
    r"|\bHTTP\s+status\b"
    # ... and a *send* obligation, however worded. Scoped to a sending
    # verb beside the notification, so the bare factual mention "The
    # notification is logged." (a filler clause several existing
    # must-pass fixtures rely on) still lints clean.
    r"|\b(?:sends?|sending|sent|posts?|posted|deliver(?:s|ed)?"
    r"|dispatch(?:es|ed)?|emits?|emitted)\b[^.;!?]{0,40}\bnotification\b"
    r"|\bnotification\b[^.;!?]{0,40}\b(?:is|are|was|were|must\s+be"
    r"|has\s+been|have\s+been)\s+(?:sent|posted|delivered|dispatched|emitted)\b"
    r"|\bnotif(?:y|ies|ied)\s+the\s+operator\b",
    re.IGNORECASE,
)

# Task 005 (PRD requirement B.1, #162): each shape above also carries a
# legal PREDICATE and a default SUBJECT, from which `_legal_rewrite` below
# builds a CONCRETE, clause-specific suggestion: the offending clause's own
# subject (the words in front of the illegal span, trimmed of modals) is
# kept and the illegal demand is replaced wholesale by the shape's legal
# predicate, so the suggestion is a whole sentence rather than a fragment
# spliced into the middle of a demand that is still illegal around it.
#
# Task 005, second attempt (the first attempt's `validationNotes`): the
# earlier version substituted the fragment for the matched span alone and
# left the rest of the clause standing, which produced suggestions that
# were themselves REFUSED when fed back through `lint_proposal` (`The
# audit range must be empty.` became `The the specific paths and the sha
# the range is measured from must be empty`, still an empty-range demand
# and ungrammatical besides). Every suggestion this module emits is now
# checked against every clause-scoped lint in this module before it is
# emitted (`_clause_lint_refusals`), and the candidate ladder in
# `_legal_rewrite` falls back until one lints clean -- so the suggested
# rewrite is always a legal rewrite, which
# `tests/test_legal_rewrite_reprompt.py` asserts by feeding each emitted
# suggestion straight back into `lint_proposal`.
#
# The suggestion stays derived from the offending clause: two clauses that
# hit the same shape but carry different subjects -- "The report must
# contain the final sha." vs. "This is the final commit of the wave." --
# yield two different suggestions, because the subject each one names
# ("report", "wave") survives into its own rewrite.
_UNSATISFIABLE_SHAPE_CHECKS: tuple[tuple[re.Pattern, str, str, str], ...] = (
    (
        _LATER_FINAL_LAST_COMMIT_RE,
        "names a later/final/last commit or its sha, which cannot be "
        'known until that commit lands -- name "the code sha this task '
        'lands" once it exists, or a past sha this task can already read',
        "is recorded at the code sha this task lands",
        "The report",
    ),
    (
        _PREDECLARED_EVENT_STATE_RE,
        "predeclares an event state (a petition/approach outcome) that a "
        "later event in this same run can falsify -- require the record "
        "to state whatever the TRUE outcome turns out to be, not a "
        "specific outcome named in advance",
        "records whatever the true outcome turns out to be when it is checked",
        "The record",
    ),
    (
        _RECORD_CITES_OWN_WRITING_COMMIT_RE,
        "requires a record to cite the sha of the commit that writes it, "
        "which is impossible by construction (the hash is computed from "
        "the tree the commit produces) -- cite the record's own "
        "report-state sha (its commit's parent) instead, the convention "
        "`docs/sweep-records.md` already documents",
        "cites its own report-state sha (the parent of its own commit)",
        "The row",
    ),
    (
        _EMPTY_GIT_RANGE_RE,
        "requires a git log/diff range to be empty -- name the specific "
        "paths and the sha the range is measured from instead of "
        "demanding an empty range as a standing invariant",
        "is checked over the files this task names, at the sha it names",
        "The check",
    ),
    (
        _EXACTLY_ONCE_COMMIT_COUNT_RE,
        'demands an "exactly once" commit count -- require the '
        "deliverable to land, verified by its own evidence, not a count "
        "of how many commits it took",
        "lands, committed and verified by its own evidence",
        "The deliverable",
    ),
    (
        _NOTIFICATION_OR_HTTP_STATUS_RE,
        "requires a per-task notification ledger entry or HTTP-status "
        "record -- sends are engine-recorded and best-effort; no "
        "criterion may mention a send, a ledger entry or an HTTP status",
        "is left to the engine's own best-effort record",
        "The operator contact",
    ),
)

# Words a candidate subject may not END on -- a modal, a copula, a bare
# article/preposition or a transitive verb whose object the rewrite drops.
# Trimmed from the right, repeatedly, so "The report must contain the"
# becomes "The report" and the shape's predicate attaches to a real noun
# phrase instead of a dangling verb.
_REWRITE_SUBJECT_TRIM_WORDS = frozenset(
    """a an the this that these those its their his her our your it
    is are was were be been being am
    must shall should will would can could may might has have had does do did
    contains contained carries carried cites cited names named
    shows showed shown states stated records recorded reports reported
    lists listed includes included verifies verified appends appended
    and or nor but of at in on to for from by with as into over under
    before after than then not no only own same which who whose where
    when while there here""".split()
)

# Bare verb forms that double as singular nouns ("the record", "to record"):
# kept OUT of the trim set above so "The record" survives as a subject, but
# popped together with the modal in front of them, so "The report must
# contain" reduces to "The report" rather than dangling a transitive verb.
_REWRITE_BARE_VERB_WORDS = frozenset(
    """contain carry cite name show state record report list include
    verify land check append hold read write""".split()
)

_REWRITE_MODAL_WORDS = frozenset(
    """must shall should will would can could may might does do did to""".split()
)

# Words that cannot head a subject noun phrase however they are trimmed --
# adjectives, participles and adverbs the banned shapes are full of. Without
# this list "The audit range must be empty." would rewrite to "The empty
# ..." (legal, but not a sentence).
_REWRITE_NON_NOUN_WORDS = frozenset(
    """empty final last later next subsequent prior newly filed protected
    exactly once observed single separate true verbatim refuted confirmed
    present absent clean green red done above below per still already ever
    never just zero many few more less each every all any both none""".split()
)

_REWRITE_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'`./_-]*")

# Punctuation a word carries into a subject phrase but must not keep.
_REWRITE_STRIP_CHARS = "`'\"(),;:./_- "


def _is_subject_noun(word: str) -> bool:
    """True if `word`, lifted out of an offending clause, can head a
    singular subject noun phrase ("The <word> ...") in a rewrite: not a
    trimmed function word, not an adjective/participle from the banned
    shapes' own vocabulary, and not plural (the shapes' predicates below
    are all singular)."""
    bare = word.strip(_REWRITE_STRIP_CHARS).lower()
    if len(bare) < 3:
        return False
    if bare in _REWRITE_SUBJECT_TRIM_WORDS or bare in _REWRITE_NON_NOUN_WORDS:
        return False
    if bare.endswith("s") and not bare.endswith(("ss", "us", "is")):
        return False
    return True


def _clause_lint_refusals(clause: str) -> list[str]:
    """Every clause-scoped lint in this module, run against ONE candidate
    clause. Task 005 (PRD requirement B.1, second attempt): this is what
    makes a suggested rewrite a *legal* rewrite -- a candidate that any of
    these lints would refuse is never emitted as a suggestion, so feeding
    the suggestion back into `lint_proposal` cannot produce a fresh
    refusal. Returns the names of the lints that refuse `clause` (empty
    list = legal)."""
    refusals: list[str] = []
    checks: tuple[tuple[str, Callable[[str], Any]], ...] = (
        ("unsatisfiable-shape", _unsatisfiable_shape_match),
        ("relational-sha", _relational_sha_clause),
        ("single-commit-conflict", _single_commit_conflict_clause),
        ("unscoped-occurrence-count", _unscoped_occurrence_count_clause),
        ("scratch-state-content", _scratch_state_content_clause),
        ("unenumerated-quantifier", _unenumerated_quantifier_clause),
        ("clause-count-overrun", _clause_count_overrun),
        ("command-form", _command_form_shape_clause),
        ("launch-form-with-status", _launch_form_with_captured_status_clause),
    )
    for name, check in checks:
        if check(clause) is not None:
            refusals.append(name)
    if _declares_self_growing_output(clause):
        refusals.append("self-growing-output")
    if _depends_on_own_terminal_state(clause):
        refusals.append("own-terminal-state")
    return refusals


def _tidy_sentence(text: str) -> str:
    """One-line, single-spaced, terminated with a period."""
    collapsed = re.sub(r"\s+", " ", text).strip().strip(",;:").strip()
    if collapsed and not collapsed.endswith((".", "!", "?")):
        collapsed += "."
    return collapsed


def _trimmed_subject(text: str) -> str:
    """`text` reduced to a noun phrase usable as a sentence subject: no
    trailing modal/copula/verb/preposition, no dangling modal-plus-bare-verb
    pair, no trailing punctuation."""
    words = re.sub(r"\s+", " ", text).strip().strip(",;:.").split(" ")

    def _bare(index: int) -> str:
        return words[index].strip(_REWRITE_STRIP_CHARS).lower()

    changed = True
    while changed:
        changed = False
        while words and (not words[-1] or not _bare(-1)
                         or _bare(-1) in _REWRITE_SUBJECT_TRIM_WORDS):
            words.pop()
            changed = True
        if (
            len(words) >= 2
            and _bare(-1) in _REWRITE_BARE_VERB_WORDS
            and _bare(-2) in _REWRITE_MODAL_WORDS
        ):
            words.pop()
            words.pop()
            changed = True
    return " ".join(words).strip(",;:. ")


def _subject_candidates(clause: str, match: re.Match, default_subject: str) -> list[str]:
    """Subjects to try for the rewrite of `clause`, best first: the
    clause's own subject (the words in front of the illegal span,
    progressively shortened from the right), then a noun lifted from what
    followed the span, then a noun lifted from the span itself (last word
    first -- "audit range" contributes "range"), and finally the shape's
    own default subject. Every candidate is still lint-checked by
    `_legal_rewrite`; this function only decides the ORDER in which the
    clause's own words get their chance to survive into the suggestion."""
    candidates: list[str] = []

    def _add(text: str) -> None:
        cleaned = _trimmed_subject(text)
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)

    prefix = _trimmed_subject(clause[: match.start()])
    while prefix:
        _add(prefix)
        prefix = _trimmed_subject(" ".join(prefix.split(" ")[:-1]))

    for word in _REWRITE_WORD_RE.findall(clause[match.end():]):
        if _is_subject_noun(word):
            _add(f"The {word.strip(_REWRITE_STRIP_CHARS)}")

    for word in reversed(_REWRITE_WORD_RE.findall(match.group(0))):
        if _is_subject_noun(word):
            _add(f"The {word.strip(_REWRITE_STRIP_CHARS)}")

    _add(default_subject)
    return candidates


def _legal_rewrite(clause: str, match: re.Match, predicate: str,
                   default_subject: str) -> str:
    """The concrete legal rewrite for one offending `clause`: the clause's
    own subject (or the closest thing to one its words offer) followed by
    the shape's legal `predicate`, and -- crucially -- the FIRST such
    candidate that `_clause_lint_refusals` accepts. Task 005 (PRD
    requirement B.1): clause-specific, because the subject comes out of
    the offending clause itself, and legal, because nothing this function
    returns is a string the lints would refuse."""
    for subject in _subject_candidates(clause, match, default_subject):
        candidate = _tidy_sentence(f"{subject} {predicate}")
        if not _clause_lint_refusals(candidate):
            return candidate
    # Unreachable in practice: the shape's own default subject plus its own
    # predicate is lint-clean by construction (asserted for all six shapes
    # in `tests/test_legal_rewrite_reprompt.py`). Kept so a future shape
    # added without that check degrades to the generic advice rather than
    # emitting an illegal suggestion.
    return _tidy_sentence(f"The evidence this task names {predicate}")


def _unsatisfiable_shape_match(criteria: str) -> tuple[str, int, re.Match] | None:
    """The raw match behind `_unsatisfiable_shape_clause`: `(clause,
    shape_index, match)` for the FIRST shape (in `_UNSATISFIABLE_SHAPE_
    CHECKS` order) that matches the FIRST clause it matches, else None.
    Split out from `_unsatisfiable_shape_clause` so `_clause_lint_refusals`
    can ask "is this text legal?" without building a rewrite for it --
    which would recurse forever, since building a rewrite asks exactly
    that question about its own candidates."""
    for clause in _CLAUSE_SPLIT_RE.split(criteria):
        stripped = clause.strip()
        if not stripped:
            continue
        for index, (pattern, _alt, _pred, _subj) in enumerate(
            _UNSATISFIABLE_SHAPE_CHECKS
        ):
            match = pattern.search(stripped)
            if match:
                return (stripped, index, match)
    return None


def _unsatisfiable_shape_clause(criteria: str) -> tuple[str, str, str] | None:
    """None unless some clause of `criteria` matches one of the six banned
    shapes above; otherwise `(clause, satisfiable_alternative_text,
    legal_rewrite_text)` for the FIRST shape (in the order above) that
    matches the FIRST clause it matches -- the offending clause is quoted,
    a satisfiable alternative is described, and a concrete rewrite of that
    is named, exactly like every other lint in this module."""
    found = _unsatisfiable_shape_match(criteria)
    if found is None:
        return None
    stripped, index, match = found
    _pattern, alternative, predicate, default_subject = (
        _UNSATISFIABLE_SHAPE_CHECKS[index]
    )
    return (stripped, alternative,
            _legal_rewrite(stripped, match, predicate, default_subject))


# ---------------------------------------------------------------------------
# Task 022 (requirement E.1, #115 lint half): the plan's SIXTH lint -- the
# criteria-maximalism bound `plan_review.md` already states for the peer
# reviewer ("a `successCriteria` clause that quantifies over an open set
# without enumerating it", "more than roughly 5 checkable clauses") now
# also gates ingestion itself, so a proposal never even reaches a worker
# before a human -- or the peer gate -- has to catch it by hand. Two
# independent checks, both #86-disciplined (scoped to ONE clause via
# `_CLAUSE_SPLIT_RE`, never the whole criteria string, exactly like every
# lint above):
#
#   (f) an unenumerated universal quantifier -- "every/each/all/no ...
#       anywhere" ranging over a set whose members are never named and no
#       single defining command/file bounds it. v0.16's real casualties,
#       both recovered verbatim from tracked files rather than
#       reconstructed: task 023's "each shipped piece" (the exact phrase
#       `plan_review.md:31`'s own "over-constrained criteria" rubric line
#       names by example), and tasks 012/013's "every rephrasing of X"
#       (`planning.md:165`'s own "an unenumerable aspiration" example,
#       `plan_review.md:30`'s "quantifies over an open set" example). The
#       must-pass sibling this
#       task's own criteria names by example -- "every test in
#       tests/test_x.py passes" -- is bounded because it names the single
#       file that DEFINES the set being quantified over; naming the members
#       explicitly ("each of tasks 001, 002 and 003") bounds it the other
#       way. Task 006 (PRD requirement B.2, #135) adds a third, narrower
#       escape: a clause whose quantifier points BACKWARD with an explicit
#       anaphor ("each of THEM", "all of THOSE", "every one of THESE",
#       "the above", "the aforementioned", "such") is bounded if some
#       OTHER clause of the SAME criteria is itself bounded on its own
#       merits -- see `_is_anaphorically_bound_elsewhere` below. A bare new
#       noun phrase with no anaphor ("each shipped piece") is unaffected by
#       this escape and stays refused even when some unrelated clause
#       elsewhere happens to be bounded.
#   (g) a clause-count overrun -- more than `_MAX_CHECKABLE_CLAUSES` (~5)
#       checkable clauses in one task's criteria, the other half of the
#       same `plan_review.md` rubric line.
#
# `tests/test_criteria_bound_lint.py` is this task's new module.

# Quantifier words that range over a SET rather than naming one thing --
# deliberately NOT "any"/"some", which do not claim universality.
_UNIVERSAL_QUANTIFIER_WORD_RE = re.compile(r"\b(?:every|each|all)\b", re.IGNORECASE)

# The PRD's fourth quantifier shape is two words apart: "no ... anywhere" --
# matched only within one clause (never across a `_CLAUSE_SPLIT_RE`
# boundary, since the split already happens before this pattern runs).
_NO_QUANTIFIER_ANYWHERE_RE = re.compile(
    r"\bno\b[^.!?;]*\banywhere\b", re.IGNORECASE,
)

# What bounds a quantified set well enough to pass: a single defining
# command/file (a backticked span, or a bare path with a file extension --
# "tests/test_x.py", "src/foo/bar.py") named IN THE SAME CLAUSE as the
# quantifier. `pytest` alone also counts -- it is itself a single defining
# command with no path required.
_DEFINING_COMMAND_RE = re.compile(
    r"`[^`\n]{1,80}`"
    r"|\b[\w.-]+(?:/[\w.-]+)+\.[A-Za-z0-9]{1,8}\b"
    r"|\bpytest\b",
    re.IGNORECASE,
)

# What bounds a quantified set the OTHER way: the members are enumerated
# right there in the clause -- an explicit id/number list ("tasks 001, 002
# and 003"), a parenthetical/braced list, or an enumeration-announcing word.
_ENUMERATED_SET_RE = re.compile(
    r"\bnamely\b"
    r"|\bspecifically\b"
    r"|\benumerated\b"
    r"|\bsuch as\b"
    r"|\bthe following\b"
    r"|[({][^()]*,[^()]*[)}]"
    r"|\b\d{1,4}\s*(?:,\s*(?:and\s+)?\d{1,4}\s*)+(?:and\s+\d{1,4})?\b"
    r"|\b\d{1,4}\s+and\s+\d{1,4}\b",
    re.IGNORECASE,
)


def _is_bounded_quantifier_clause(clause: str) -> bool:
    """True if `clause` names either a single defining command/file that
    bounds the quantified set, or enumerates the set's members explicitly
    -- either escape the PRD's rule allows."""
    return bool(_DEFINING_COMMAND_RE.search(clause) or _ENUMERATED_SET_RE.search(clause))


# Task 006 (PRD requirement B.2, #135): the "elsewhere-bounded-quantifier"
# rule -- v0.20's real casualty was a clause whose own text carries no
# bound at all because the bound sits in an EARLIER clause of the SAME
# criteria ("This commit touches `src/a.py` and `src/b.py`. Each of them
# gets a new docstring."): `_is_bounded_quantifier_clause` above only ever
# looks inside the one clause it is given, so that second clause was a
# hard refusal even though the set it quantifies over is plainly bounded
# one sentence earlier. The rule stays narrow on purpose, so it cannot be
# read as disabling the lint (B.2's own text): it fires ONLY when the
# flagged clause's quantifier points BACKWARD with an explicit anaphor --
# "them"/"those"/"these"/"the above"/"the aforementioned"/"such" -- never
# for a bare new noun phrase ("each shipped piece" has no anaphor and stays
# refused no matter what else the criteria bounds elsewhere). The limit:
# at most this one other-clause lookup, scoped to the SAME task's criteria
# string only (never another task's, never the PRD) -- reusing the exact
# same `_is_bounded_quantifier_clause` test every self-bounded clause
# already passes, so "bounded" means the identical thing in both places.
_ANAPHORIC_QUANTIFIER_RE = re.compile(
    r"\b(?:every|each|all)\b(?:[^.!?;]{0,40}\bof\b)?[^.!?;]{0,40}\b"
    r"(?:them|those|these|the\s+above|the\s+aforementioned|such)\b",
    re.IGNORECASE,
)


def _is_anaphorically_bound_elsewhere(clause: str, other_clauses: list[str]) -> bool:
    """True only if `clause` quantifies with a backward-pointing anaphor
    (them/those/these/the above/the aforementioned/such) AND some OTHER
    clause of the same criteria is itself bounded on its own merits
    (`_is_bounded_quantifier_clause`) -- the anaphor is what proves the
    referent is the SAME already-bounded set rather than a coincidental
    bound somewhere unrelated in the string."""
    if not _ANAPHORIC_QUANTIFIER_RE.search(clause):
        return False
    return any(_is_bounded_quantifier_clause(other) for other in other_clauses)


def _unenumerated_quantifier_clause(criteria: str) -> str | None:
    """None unless some clause carries a universal quantifier
    (every/each/all/"no ... anywhere") over a set that is neither
    enumerated nor bounded by a single named defining command, nor
    anaphorically bound by an earlier/later clause of the SAME criteria
    (`_is_anaphorically_bound_elsewhere`, PRD requirement B.2) -- must-catch
    fixtures: v0.16 task 023's "each shipped piece", tasks 012/013's "every
    rephrasing of X" (both recovered verbatim in
    `tests/test_criteria_bound_lint.py`); must-pass siblings: "every test in
    tests/test_x.py passes", and "Each of them" following an earlier clause
    that names the members."""
    clauses = [c.strip() for c in _CLAUSE_SPLIT_RE.split(criteria) if c.strip()]
    for index, stripped in enumerate(clauses):
        if not (
            _UNIVERSAL_QUANTIFIER_WORD_RE.search(stripped)
            or _NO_QUANTIFIER_ANYWHERE_RE.search(stripped)
        ):
            continue
        if _is_bounded_quantifier_clause(stripped):
            continue
        other_clauses = clauses[:index] + clauses[index + 1 :]
        if _is_anaphorically_bound_elsewhere(stripped, other_clauses):
            continue
        return stripped
    return None


# The PRD's own "more than roughly 5 checkable clauses" bound
# (`plan_review.md:32`, `docs/prds/v0.17-a-slip-is-not-a-verdict.md:274`).
_MAX_CHECKABLE_CLAUSES = 5


def _checkable_clauses(criteria: str) -> list[str]:
    """Every non-empty clause of `criteria`, split exactly like every other
    lint's per-clause scan (`_CLAUSE_SPLIT_RE`) -- the same granularity, so
    a criterion this lint counts at N clauses is the same N clauses the
    quantifier lint above and the five stranding lints each scan one at a
    time."""
    return [c.strip() for c in _CLAUSE_SPLIT_RE.split(criteria) if c.strip()]


def _clause_count_overrun(criteria: str) -> tuple[int, str] | None:
    """None unless `criteria` carries more than `_MAX_CHECKABLE_CLAUSES`
    checkable clauses; otherwise the total count and the first clause past
    the bound (the offending clause named in the refusal text)."""
    clauses = _checkable_clauses(criteria)
    if len(clauses) <= _MAX_CHECKABLE_CLAUSES:
        return None
    return len(clauses), clauses[_MAX_CHECKABLE_CLAUSES]


# Task 031 (PRD requirement E.2, #171): a PUBLIC alias for the checkable-
# clause bound above and the function that checks it -- a plan-authored
# task's `successCriteria` is not the only text this bound now applies to.
# `engine.transitions.review_cured_when_errors` holds a curable finding's
# own `cured_when` (a review-authored specification that BECOMES a cure
# task's `successCriteria`, `state.cure_task_from_finding`) to the exact
# same bound, through this same function -- never a second, hand-copied
# clause-counting rule that could drift from this one.
MAX_CHECKABLE_CLAUSES = _MAX_CHECKABLE_CLAUSES


def clause_count_overrun(criteria: str) -> tuple[int, str] | None:
    """Public wrapper for `_clause_count_overrun` above -- same contract,
    exposed for callers outside this module (task 031, `transitions.
    review_cured_when_errors`) that hold some OTHER piece of text to the
    identical ~`MAX_CHECKABLE_CLAUSES`-clause bound `lint_proposal` already
    enforces on a task's own `successCriteria`."""
    return _clause_count_overrun(criteria)


# Task 002 (requirement B): the two lexical ingestion refusals above --
# unenumerated-quantifier (f) and clause-count-overrun (g) -- now carry a
# concrete LEGAL rewrite beside their advice, exactly like the six
# structurally-unsatisfiable shapes already do (`_unsatisfiable_shape_
# clause`, `_legal_rewrite`). Both reuse the SAME `_legal_rewrite`/
# `_clause_lint_refusals` machinery those six shapes proved lint-clean --
# a candidate this module's own lints would refuse is never emitted, so
# feeding either suggestion straight back through `lint_proposal` as a
# whole task's `successCriteria` draws no fresh refusal
# (`tests/test_legal_rewrite_reprompt.py`).

# Task 002, second attempt (the first attempt's `validationNotes`): the
# first version built the quantifier rewrite the way the six
# structurally-unsatisfiable shapes are built -- the clause's own subject
# plus a canned predicate -- which THREW AWAY the clause's own demand
# ("Every widget works." became "The widget is the one this task's own
# criteria bounds ...", with `works` gone) and produced ungrammatical
# output whenever the subject trim cut into a verb ("The review model must
# reject is the one ..."). Both defects come from replacing the clause
# rather than BOUNDING it. This version keeps the offending clause
# verbatim -- subject, verb and demand all survive -- and adds the one
# thing the lint is actually missing: a named bounding file or command. A
# quantifier word is legal beside such a bound (`_is_bounded_quantifier_
# clause`, and the must-pass fixture "every test in tests/test_x.py
# passes"), so nothing has to be deleted to reach legality. Only the "no
# ... anywhere" shape drops one word, `anywhere` itself, which is the
# unbounded scope the lint objects to and carries no demand of its own.
#
# Task cure-01-01 (PRD requirement B.1, #135): the FIRST cure of this same
# gap left the bound as a backticked fill-in slot --
# `` `<the one file or command that lists the widget set>` `` -- which is
# advice, not a rewrite: it still asks the planner to go supply the very
# file/command the refusal needed, and the shipped
# `test_the_quantifier_rewrite_keeps_the_clauses_own_demand` passed while
# asserting that placeholder verbatim. This version names a CONCRETE,
# already-runnable command instead -- bare `pytest`, which
# `_DEFINING_COMMAND_RE` accepts as a single defining command with no path
# required (the PRD's own standing rule: "pytest alone also counts") --
# so the suggestion is directly usable with nothing left for the planner
# to fill in.


# Words that end a quantified noun phrase: a preposition, relative,
# determiner, copula or modal after the quantifier means the head noun is
# already behind us ("every rephrasing OF X" -> "rephrasing").
_QUANTIFIER_NOUN_STOP_WORDS = frozenset(
    """of that which who whose whom in on at to for from by with within
    and or but nor is are was were be been being am
    must shall should will would can could may might
    has have had does do did the a an this these those such them there
    it its their his her our your when while where after before""".split()
)


def _quantified_noun_phrase(text: str) -> str:
    """The head noun phrase of a quantified set, lifted out of `text` (what
    follows the quantifier word in the offending clause): up to three
    leading words, stopping at the first function word or at the first word
    that cannot be part of a singular noun phrase (`_is_subject_noun`, so
    the verb in "every widget WORKS" is not mistaken for part of the noun).
    Empty string when the clause offers nothing usable ("each of them")."""
    words: list[str] = []
    for word in _REWRITE_WORD_RE.findall(text):
        bare = word.strip(_REWRITE_STRIP_CHARS).lower()
        if bare in _QUANTIFIER_NOUN_STOP_WORDS or not _is_subject_noun(word):
            break
        words.append(word.strip(_REWRITE_STRIP_CHARS))
        if len(words) == 3:
            break
    return " ".join(words)


def _quantifier_bound_phrase(noun: str) -> str:
    """The bounding tail added to an offending clause: a concrete,
    already-runnable check -- bare `pytest`, which `_DEFINING_COMMAND_RE`
    accepts as a single defining command with no path required (the PRD's
    own standing rule: "pytest alone also counts") -- never a fill-in slot
    that only tells the planner to go name a bound itself (cure-01-01,
    PRD requirement B.1: that was the first cure's own gap). `noun` -- the
    clause's own quantified noun -- is folded into the bound so the
    suggestion still says WHICH set is being checked, not just that some
    check exists."""
    target = f"every {noun}" if noun else "the full set this clause quantifies over"
    return f", verified by rerunning pytest against {target}"


# Unreachable in practice (asserted lint-clean in
# `tests/test_legal_rewrite_reprompt.py`, same discipline as
# `_legal_rewrite`'s own fallback): used only if bounding the clause
# itself somehow still leaves it refused by another lint in this module.
# Cure-01-01: no fill-in slot here either -- `pytest` is itself the bound.
_QUANTIFIER_REWRITE_FALLBACK = (
    "The members this task checks are the ones verified by rerunning pytest."
)


def _unenumerated_quantifier_rewrite(criteria: str) -> tuple[str, str] | None:
    """None unless `_unenumerated_quantifier_clause` flags a clause of
    `criteria`; otherwise `(clause, legal_rewrite)` -- the offending clause
    kept VERBATIM (its subject, verb and demand intact) with a concrete,
    already-runnable bound (`pytest`) appended, which is exactly what the
    lint asks for and all it asks for -- never a backticked fill-in slot
    the planner would still have to complete. Every candidate is checked
    by `_clause_lint_refusals` before it is returned, so the suggestion
    draws no fresh refusal when fed back through `lint_proposal`."""
    clause = _unenumerated_quantifier_clause(criteria)
    if clause is None:
        return None
    word_match = _UNIVERSAL_QUANTIFIER_WORD_RE.search(clause)
    if word_match is not None:
        body = clause
        noun = _quantified_noun_phrase(clause[word_match.end():])
    else:
        anywhere_match = _NO_QUANTIFIER_ANYWHERE_RE.search(clause)
        if anywhere_match is None:
            return None
        # "anywhere" IS the unbounded scope: drop that one adverb (it
        # carries no demand) and keep the rest of the clause as written.
        body = re.sub(r"\s*\banywhere\b", "", clause, count=1,
                      flags=re.IGNORECASE)
        no_match = re.search(r"\bno\b", clause, re.IGNORECASE)
        noun = (
            _quantified_noun_phrase(clause[no_match.end():])
            if no_match is not None else ""
        )
    stem = body.rstrip(" .")
    for bound_noun in (noun, ""):
        candidate = _tidy_sentence(stem + _quantifier_bound_phrase(bound_noun))
        if not _clause_lint_refusals(candidate):
            return (clause, candidate)
    return (clause, _QUANTIFIER_REWRITE_FALLBACK)


# Task 002, second attempt (the first attempt's `validationNotes`): the
# first version's clause-count rewrite kept the criteria's first
# `_MAX_CHECKABLE_CLAUSES` clauses and DELETED the rest, so a six-clause
# "A exists. ... F exists." criteria came back with `F exists` simply
# gone -- a deletion dressed as a rewrite, and one that quietly drops a
# deliverable. This version FOLDS instead: the criteria's own leading
# clauses stay verbatim and every remaining clause is joined into ONE
# comma-separated final clause, so the count comes under the bound with
# nothing dropped at all. `_clause_lint_refusals` (already generic over a
# multi-clause string -- every check it runs does its own `_CLAUSE_SPLIT_
# RE` split) is the same legality gate the quantifier rewrite above uses,
# so a fold this module's own lints would refuse is never emitted; the
# ladder folds progressively more clauses together before giving up on
# the fixed fallback below (never observed for any fixture in
# `tests/test_legal_rewrite_reprompt.py`).
_OVERRUN_REWRITE_FALLBACK = (
    "The remaining work lands, committed and verified by its own evidence."
)

# First words that get lowercased when their clause is folded into the
# middle of a longer sentence -- determiners and pronouns only, so an id,
# an acronym or a proper noun ("GitHub", "F") keeps its own case.
_OVERRUN_FOLD_LOWERCASE_WORDS = frozenset(
    """the a an this that these those it its they he she there""".split()
)


def _folded_clauses(clauses: list[str]) -> str:
    """`clauses` joined into ONE clause: "A, B, and C" -- no sentence
    punctuation between them, so the whole fold counts as a single
    checkable clause under `_CLAUSE_SPLIT_RE`, and no clause's content is
    lost."""
    parts: list[str] = []
    for index, clause in enumerate(clauses):
        text = clause.strip().strip(",;: ")
        if index and text:
            head, _, rest = text.partition(" ")
            if head.lower() in _OVERRUN_FOLD_LOWERCASE_WORDS:
                text = (head.lower() + " " + rest).strip()
        if text:
            parts.append(text)
    if len(parts) <= 1:
        return parts[0] if parts else ""
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


def _clause_count_overrun_rewrite(criteria: str) -> tuple[int, str, str] | None:
    """None unless `_clause_count_overrun` flags `criteria`; otherwise
    `(clause_count, overflow_clause, legal_rewrite)` -- the rewrite keeps
    the criteria's leading clauses verbatim and folds the remaining ones
    into a single final clause, dropping nothing, so the result is under
    the `_MAX_CHECKABLE_CLAUSES` bound and lint-clean."""
    overrun = _clause_count_overrun(criteria)
    if overrun is None:
        return None
    clause_count, overflow_clause = overrun
    clauses = _checkable_clauses(criteria)
    for kept in range(min(_MAX_CHECKABLE_CLAUSES, len(clauses)) - 1, -1, -1):
        folded = _folded_clauses(clauses[kept:])
        if not folded:
            continue
        candidate = _tidy_sentence(". ".join([*clauses[:kept], folded]))
        if (_clause_count_overrun(candidate) is None
                and not _clause_lint_refusals(candidate)):
            return clause_count, overflow_clause, candidate
    return clause_count, overflow_clause, _OVERRUN_REWRITE_FALLBACK


# ---------------------------------------------------------------------------
# #209 defect 1: a refusal is not a repair
# ---------------------------------------------------------------------------
# Both lexical rewrites above already compute a LEGAL, lint-clean rewrite of
# the criteria they object to -- and until this change that rewrite was used
# for exactly one thing: advice text spliced into `lint_proposal`'s error
# string (`-- suggested rewrite: ...`), after which the whole proposal was
# REFUSED and the planning hat re-asked. Issue #209 §1 measured what that
# costs: `selfdev-v022` burnt planning's entire 3-attempt ladder to
# `contract.agent_fault` on `clause-count-overrun` alone, hit the same lint
# again after replanning, and spent 10 planning phases producing 3
# approaches. The harness knew the answer on attempt 1 and asked anyway.
#
# `repair_criteria` below APPLIES the rewrite instead, on the two lexical
# lints only, and records what it did (`CRITERIA_REPAIRED_EVENT`). It is a
# PRE-PASS: `lint_proposal` itself is untouched, because every candidate
# either rewrite returns is already gated through `_clause_lint_refusals`,
# so a repaired criteria simply never reaches those two branches while an
# un-repairable one still does -- the refusal stays alive exactly where no
# legal rewrite exists, and the ~100 test call sites of `lint_proposal` keep
# their meaning.
#
# THE ONE RULE THAT MUST NOT BE RELAXED (#209 §1, SD-2). Neither rewrite can
# return `None` once its own lint has fired: each ends its candidate ladder
# on a fixed fallback constant, and `_OVERRUN_REWRITE_FALLBACK` REPLACES THE
# WHOLE CRITERIA with a generic sentence. As advice for a human reader that
# is harmless; APPLIED it is precisely "a deletion dressed as a rewrite ...
# one that quietly drops a deliverable" -- the verdict recorded in task
# 002's own comment above `_OVERRUN_REWRITE_FALLBACK`, which exists because
# the first attempt at that rewrite did exactly this. So `repair_criteria`
# returns `None` -- i.e. falls back to today's refusal, which is safe and
# merely wasteful -- whenever the computed rewrite IS either fallback.
CRITERIA_REPAIRED_EVENT = "task.criteria_repaired"


def repair_criteria(
    criteria: str,
    *,
    lints: tuple[str, ...] = ("unenumerated-quantifier",
                              "clause-count-overrun"),
) -> tuple[list[str], str] | None:
    """`(lints_applied, repaired_criteria)` when the two lexical lints'
    own already-computed rewrites turn `criteria` into text
    `_clause_lint_refusals` accepts; `None` when they cannot, which leaves
    the caller's existing refusal exactly as it was. `lints_applied` names
    each lint that acted exactly ONCE, in the order they first acted --
    it is the set of names the `CRITERIA_REPAIRED_EVENT` publishes, not a
    per-clause operation count (see the dedupe at the return below).

    `lints` names which of the two lexical lints may act -- both by
    default (plan ingestion, where the text IS a task's own
    `successCriteria`), and `("clause-count-overrun",)` from
    `state.insert_cure_tasks`, whose subject is a finding's `cured_when`.
    See that call site's own comment for why the quantifier lint has no
    jurisdiction there.

    `None` is returned for a non-`str`/blank input, when nothing changed,
    when the result is still refused by some OTHER clause lint in this
    module, and -- mandatory, see the block comment above -- whenever the
    computed rewrite is one of the two criterion-destroying fallback
    constants.

    The candidate loop is bounded by the criteria's own clause count plus
    one (at most one bounding per clause, plus one fold), so there is no
    new tuning constant; a pathological criteria that never converges
    leaves the loop mid-repair and is then caught by the trailing
    `_clause_lint_refusals` guard, so the worst case is today's refusal,
    never a half-repaired criterion.
    """
    if not isinstance(criteria, str) or not criteria.strip():
        return None
    current = criteria
    applied: list[str] = []
    for _ in range(len(_checkable_clauses(criteria)) + 1):
        if "unenumerated-quantifier" in lints:
            quant = _unenumerated_quantifier_rewrite(current)
            if quant is not None:
                clause, rewrite = quant
                if rewrite == _QUANTIFIER_REWRITE_FALLBACK:
                    return None
                clauses = _checkable_clauses(current)
                if clause not in clauses:
                    # Defensive: both come from the same `_CLAUSE_SPLIT_RE`
                    # split, so the offending clause is always one of them.
                    return None
                clauses[clauses.index(clause)] = rewrite.rstrip(" .")
                # The `". ".join` rebuild normalises `;`/`!` clause
                # delimiters to `.` -- the same normalisation
                # `_clause_count_overrun_rewrite` already performs on its
                # own fold, not a new liberty taken here.
                current = _tidy_sentence(". ".join(clauses))
                applied.append("unenumerated-quantifier")
                continue
        if "clause-count-overrun" in lints:
            overrun = _clause_count_overrun_rewrite(current)
            if overrun is not None:
                _count, _overflow, rewrite = overrun
                if rewrite == _OVERRUN_REWRITE_FALLBACK:
                    return None
                current = rewrite
                applied.append("clause-count-overrun")
                continue
        break
    if current == criteria or _clause_lint_refusals(current):
        return None
    # The loop above bounds ONE clause per pass, so a criteria with three
    # bare quantifier clauses appends that lint's name three times -- but
    # what this list becomes is the `lints` field of
    # `CRITERIA_REPAIRED_EVENT`, which `SPEC.md` §5.4 and `docs/api.md`
    # both publish as "the lint names applied": which lints acted, not how
    # many clauses each one touched (#209 defect 1, unit-2 review).
    # `dict.fromkeys` keeps the order they first acted in.
    return list(dict.fromkeys(applied)), current


def repair_task_criteria(payload: dict) -> list[dict]:
    """#209 defect 1: apply `repair_criteria` to every task's
    `successCriteria` IN PLACE and return one record per repair --
    `{"taskId", "oldCriteria", "newCriteria", "lints"}`, the payload of the
    `CRITERIA_REPAIRED_EVENT` its caller emits. Non-dict entries and
    non-`str` criteria are skipped exactly the way `normalize_task_tiers`
    skips them.

    One thing separates this from its two sibling in-place normalisers
    (`normalize_task_tiers`, `normalize_task_estimated_iterations`): those
    run AFTER the lints, over an already-accepted proposal; this runs
    BEFORE them, because the whole point is to pre-empt a refusal this
    module can already repair.

    Deliberately NOT wired into `lint_worker_split` (a worker's
    `proposed_tasks` batch). Its callers write those tasks straight to
    `tasks.json`, and the lint function has no event sink in reach there --
    repairing would be a SILENT harness rewrite of a criterion, the one
    thing `LoopSupervisor._apply_criteria_amendments`' own discipline ("the
    engine emits, never silently mutates") forbids. #209 §1 records no cost
    on that path; it stays refusing.
    """
    repairs: list[dict] = []
    for t in payload.get("tasks", []):
        if not isinstance(t, dict):
            continue
        criteria = t.get("successCriteria")
        if not isinstance(criteria, str):
            continue
        repaired = repair_criteria(criteria)
        if repaired is None:
            continue
        lints_applied, new_criteria = repaired
        t["successCriteria"] = new_criteria
        repairs.append({"taskId": t.get("id"), "oldCriteria": criteria,
                        "newCriteria": new_criteria, "lints": lints_applied})
    return repairs


# ---------------------------------------------------------------------------
# Task 009 (requirement A.5, #154): the plan's EIGHTH lint -- issue #154's
# own account of `deck-phase3k` (0.18.0) approach 2's task 205 ("Run the
# ten-run stability gate at the new final code sha"): the worker measured
# 10/10 passed, exit 0 three times and verify rejected all three, never on
# the measurement, only on the launch form -- attempt 1 wrapped the launch
# in `bash -c '... ; echo $? > log.exitstatus'` instead of the criterion's
# own literal `nohup timeout 7200 ci/stability.sh 10 > log 2>&1 &`; attempt
# 3 added a `sleep 3; ps -p` liveness check the criterion never asked for.
# Issue #154's own text calls the criterion itself "self-contradictory (a
# bare backgrounded `nohup … &` cannot hand back its exit status without a
# wrapper, yet the criterion demands both the literal line and the
# captured status), which is a plan-time lint gap too". This wave's PRD
# (`docs/prds/v0.19-a-note-is-not-a-rejection.md`, "What this PRD
# deliberately does not ask") names "the deck-3k task 205 criterion" as
# the VERBATIM must-catch fixture, quoting an abbreviated form of it
# ("launched as \`nohup timeout … &\` and polled with \`sleep 120\` and
# nothing else" *and* "the captured exit status"). `TASK_205_SUCCESS_
# CRITERIA` below is therefore not that abbreviation but the criterion
# ITSELF, recovered byte-for-byte from the `deck-phase3k` run's own
# `tasks.json` (task 205, `status: skipped`, `validationAttempts: 3`) and
# tracked in this repo at `tests/data/deck-phase3k-task-205.json` -- the
# same fetch-and-track discipline task 013 uses for
# `tests/data/issue-143-body.md`. Recovery path (the run directories are a
# host-side sibling of this run's own, not on this container's filesystem):
#     docker run --rm --user 1000:1000 \
#       -v <host runs dir>:/hostruns:ro python:3.12-alpine python -c \
#       'import json;d=json.load(open("/hostruns/deck-phase3k/tasks.json"))
#        ;print([t for t in d["tasks"] if t["id"]=="205"][0]["successCriteria"])'
# `tests/test_ingestion_lints.py` asserts direct EQUALITY between
# the constant and that tracked record's `successCriteria`, so the
# "verbatim" claim is checkable without the host run dir.
#
# Four cue shapes, each scoped to ONE clause (`_CLAUSE_SPLIT_RE`), the same
# #86/#76 discipline every lint above follows:
#   (a) the literal text of a shell command -- "launched as `...`", "the
#       literal `...`", "run as `...`", "invoked as `...`", plus the
#       exactness forms "execute `...` exactly", "run `...` verbatim",
#       "exactly as `...`", "the launch line is `...`";
#   (b) a wrapper pinned by name -- "wrapped in/with/using `...`",
#       "wrapped in bash -c", "the wrapper is `...`";
#   (c) a polling cadence -- "polled with `...`", "with a polling
#       cadence", "the polling cadence is ...", "poll/polled/polling every
#       N seconds/minutes", "checked every N seconds", "every N seconds
#       until ..." (the bare words "polling cadence" are deliberately NOT
#       a cue on their own: a criterion may legitimately talk ABOUT the
#       ban, as task 009's own criterion in this very wave does);
#   (d) a "nothing else" exclusivity clause.
# A fifth, independent rule (#154's own "self-contradictory" diagnosis)
# refuses a criterion that names a launch form (any of the cues above, or
# the bare word "launched as"/"nohup"/"backgrounded") AND ALSO demands the
# same process's captured status -- the combination is what makes task
# 205's own criterion unsatisfiable by a bare backgrounded command,
# independent of whether it also happens to use one of the four
# literal-text cues above. This one rule deliberately reaches ACROSS
# clauses of the same criterion (while still reporting the two offending
# clauses individually): the real task 205 splits the two halves over two
# sentences -- the launch form in "`ci/stability.sh 10` is launched as
# `nohup ... &` ...", the captured status in "summary.log.exitstatus
# holding the script's own captured exit status" -- and a per-clause-only
# check would let exactly the must-catch shape through.

_RUN_VERB = r"(?:execute|executes|executed|run|runs|invoke|invokes|invoked|launch|launches|launched|call|calls)"
_EXACTNESS = (
    r"(?:exactly|verbatim|literally|as[-\s]written|character[-\s]for"
    r"[-\s]character|byte[-\s]for[-\s]byte)"
)

_LAUNCH_FORM_LITERAL_RE = re.compile(
    r"\b(?:launched|run|invoked|executed|started)\s+as\s+`[^`\n]{1,160}`"
    r"|\bthe\s+literal\s+`[^`\n]{1,160}`"
    # "execute `...` exactly" / "run `...` verbatim" / "runs `...` as written"
    rf"|\b{_RUN_VERB}\s+(?:it\s+|the\s+[\w.\-/]+\s+)?`[^`\n]{{1,160}}`[\s,]*{_EXACTNESS}\b"
    # "exactly as `...`" / "verbatim as `...`"
    rf"|\b{_EXACTNESS}\s+as\s+`[^`\n]{{1,160}}`"
    # "the launch line is (exactly) `...`"
    r"|\b(?:command(?:\s+line)?|invocation|launch\s+line|shell\s+line)\s+"
    rf"(?:is|must\s+be|reads)\s+(?:{_EXACTNESS}\s+)?`[^`\n]{{1,160}}`",
    re.IGNORECASE,
)

_LAUNCH_WRAPPER_CLAUSE_RE = re.compile(
    r"\bwrapp?ed\s+(?:in|with|using)\s+`[^`\n]{1,80}`"
    r"|\bwrapp?ed\s+(?:in|with|using)\s+(?:a\s+|the\s+)?"
    r"(?:bash|sh|nohup|timeout|setsid|script|subshell|wrapper)\b"
    r"|\bwrapp?(?:ed|ing|s)?\s+(?:a|the|its)\s+launch\s+(?:in|with|using)\b"
    r"|\bwrapper\s+(?:is|must\s+be)\s+`[^`\n]{1,80}`",
    re.IGNORECASE,
)

_CADENCE_UNIT = r"(?:s|sec|secs|second|seconds|m|min|mins|minute|minutes)"

_POLLING_CADENCE_CLAUSE_RE = re.compile(
    r"\bpoll(?:s|ed|ing)?\s+(?:it\s+|them\s+)?with\s+`[^`\n]{1,40}`"
    r"|\bpolling\s+cadence\s+(?:is|of|must|remains)\b"
    r"|\b(?:at|with|using|on)\s+(?:a|the|its)\s+polling\s+cadence\b"
    rf"|\bpoll(?:s|ed|ing)?\s+(?:it\s+|them\s+|the\s+[\w.\-/]+\s+)?"
    rf"every\s+\d+\s*{_CADENCE_UNIT}?\b"
    rf"|\b(?:check|checks|checked|sleep|sleeps|slept)\s+every\s+\d+\s*{_CADENCE_UNIT}\b"
    rf"|\bevery\s+\d+\s*{_CADENCE_UNIT}\s+until\b",
    re.IGNORECASE,
)

_NOTHING_ELSE_CLAUSE_RE = re.compile(r"\band\s+nothing\s+else\b", re.IGNORECASE)

_COMMAND_FORM_SHAPE_CHECKS: tuple[tuple[re.Pattern, str], ...] = (
    (
        _LAUNCH_FORM_LITERAL_RE,
        "pins the literal text of a shell command -- name the observable "
        "the command must produce instead (a file, an exit-status file, a "
        "tally line, a test node passing) and let the worker choose the "
        "launch form",
    ),
    (
        _LAUNCH_WRAPPER_CLAUSE_RE,
        "pins the literal wrapper a launch is required to use -- name the "
        "observable the wrapped process must produce instead and let the "
        "worker choose the wrapper",
    ),
    (
        _POLLING_CADENCE_CLAUSE_RE,
        "pins a polling cadence -- name the observable the polled process "
        "must eventually produce, never how often or with what command "
        "the worker checked for it",
    ),
    (
        _NOTHING_ELSE_CLAUSE_RE,
        "carries a \"nothing else\" exclusivity clause -- name the "
        "observable the command must produce and drop the exclusivity "
        "demand, which rules out every wrapper or cadence a worker might "
        "otherwise choose",
    ),
)


def _command_form_shape_clause(criteria: str) -> tuple[str, str] | None:
    """None unless some clause of `criteria` pins the literal text of a
    shell command, a wrapper, a polling cadence or a "nothing else"
    clause; otherwise `(clause, satisfiable_alternative_text)` for the
    FIRST shape (in the order above) that matches the FIRST clause it
    matches -- must-catch fixture: `TASK_205_SUCCESS_CRITERIA` below;
    must-pass sibling: "`summary.log.exitstatus` holds the script's exit
    status" (an observable, no launch form, no cadence, no exclusivity)."""
    for clause in _CLAUSE_SPLIT_RE.split(criteria):
        stripped = clause.strip()
        if not stripped:
            continue
        for pattern, alternative in _COMMAND_FORM_SHAPE_CHECKS:
            if pattern.search(stripped):
                return stripped, alternative
    return None


# The combo rule (#154's "self-contradictory" diagnosis): a clause naming a
# launch form (looser than the four literal-text cues above -- also catches
# bare "launched as"/"invoked as"/"wrapped in" without a backtick, and the
# bare word "nohup") AND demanding the same process's captured status.
_LAUNCH_FORM_CUE_RE = re.compile(
    r"\blaunched\s+as\b"
    r"|\binvoked\s+as\b"
    r"|\brun\s+as\s+`"
    r"|\bthe\s+literal\s+`"
    r"|\bwrapp?ed\s+in\b"
    r"|\bbackgrounded\s+(?:as|via|with)\b"
    r"|\bnohup\b",
    re.IGNORECASE,
)

_CAPTURED_STATUS_CUE_RE = re.compile(
    r"\bcaptured\s+(?:exit\s+)?status\b"
    r"|\bcapture(?:s|d)?\s+(?:the\s+)?(?:process'?s?\s+)?(?:exit\s+)?status\b",
    re.IGNORECASE,
)


def _launch_form_with_captured_status_clause(criteria: str) -> tuple[str, str] | None:
    """None unless `criteria` both names a launch form and demands that
    same process's captured status -- the shape `#154` calls
    "self-contradictory": a bare backgrounded launch cannot hand back its
    own exit status without a wrapper, so pinning the literal launch form
    AND the captured status cannot both hold. Returns `(launch_clause,
    status_clause)`, the two offending clauses (the same string twice when
    one clause carries both). Unlike every other check in this module this
    one spans the whole criterion rather than one clause: the must-catch
    fixture `TASK_205_SUCCESS_CRITERIA` puts the launch form in its first
    sentence and the captured status in its second, so a per-clause-only
    rule would miss the very shape it exists for. Must-pass sibling:
    naming the file that holds the result (`summary.log.exitstatus` holds
    the script's exit status) never trips this, since it names no launch
    form at all."""
    launch_clause: str | None = None
    status_clause: str | None = None
    for clause in _CLAUSE_SPLIT_RE.split(criteria):
        stripped = clause.strip()
        if not stripped:
            continue
        has_launch = bool(_LAUNCH_FORM_CUE_RE.search(stripped))
        has_status = bool(_CAPTURED_STATUS_CUE_RE.search(stripped))
        if has_launch and has_status:
            return stripped, stripped
        if has_launch and launch_clause is None:
            launch_clause = stripped
        if has_status and status_clause is None:
            status_clause = stripped
    if launch_clause is not None and status_clause is not None:
        return launch_clause, status_clause
    return None


# Task 009's own must-catch fixture: the `deck-phase3k` task 205
# `successCriteria` itself, byte-for-byte, recovered from that run's own
# `tasks.json` and tracked at `tests/data/deck-phase3k-task-205.json` (see
# the block comment above for the recovery command and the equality test
# that keeps the two in step). Not a reconstruction and not the PRD's
# abbreviation of it.
TASK_205_SUCCESS_CRITERIA = (
    "From a clean tree whose HEAD is the final code sha or a "
    "docs-only descendant of it, `ci/stability.sh 10` is launched as "
    "`nohup timeout 7200 ci/stability.sh 10 > log 2>&1 &` and polled "
    "with `sleep 120` and nothing else (no other test run in the same "
    "iteration). docs/reports/phase3k-205-stability10/ holds "
    "summary.log copied from the script's output dir, "
    "summary.log.exitstatus holding the script's own captured exit "
    "status, a poll log, and a README quoting summary.log's final "
    "tally verbatim. If that tally is below 10/10 it is published "
    "as-is and never re-run to improve it: the README lists the run "
    "numbers reported as failing by `grep -n -i fail summary.log` and "
    "the per-run log of each run number that command prints is copied "
    "into docs/reports/phase3k-205-stability10/. The README states "
    "the final code sha from `git log -1 --format=%H -- '*.go' "
    "'*.feature'`."
)


# Task 026 (PRD requirement D.2, #157): a task the planner estimates above
# this many iterations is refused at ingestion unless THE PROPOSAL
# decomposes it into staged TASKS carrying their own measurable criteria --
# exactly the shape `plan_review.md`'s peer-review checklist (item 8,
# below) asks a human-shaped peer to look for when the peer gate is ON;
# this lint is what enforces the SAME rule when that gate is OFF
# (`plan_review` never runs at all), and it is never skipped either way --
# ingestion's three steps run for every proposal regardless of
# `peer_plan_review`.
#
# The decomposition the PRD asks for is across TASKS, not inside one task's
# prose: "unless the proposal decomposes it into staged tasks with their own
# measurable criteria". Attempt 2 of this task read `Stage N` headings
# inside the oversized task's own `successCriteria` as the staging, which
# both refused a genuinely decomposed proposal (an oversized parent beside
# two measurable stage tasks) and accepted a monolith that had merely
# written headings into one flat criterion -- the exact inversion the
# verifier rejected. `_staged_decomposition_gap()` below now reads the
# proposal's task list.
_MAX_ESTIMATED_ITERATIONS_WITHOUT_STAGES = 3

# A stage marker: "Stage 1", "Stage 2" -- deliberately only the digit form,
# since that is what a worker/verifier can count mechanically without
# guessing at spelled-out numbers.
_STAGE_MARKER_RE = re.compile(r"\bstage\s+(\d+)\b", re.IGNORECASE)

# A named module group: a NAME, never a bare noun. The bare word "module"
# names nothing a worker could open, so "the module works" is NOT a module
# group (attempt 1 of this task accepted it, and the verifier rejected the
# attempt for exactly that): a group has to be identified by a path or a
# backticked identifier -- `tests/test_x.py`, `tests/records/`,
# docs/architecture.md, `plan_ingestion.lint_proposal`.
_MODULE_GROUP_TOKEN_RE = re.compile(
    # a backticked file/identifier: `tests/test_x.py`, `some_module.py`,
    # `plan_ingestion.lint_proposal`
    r"`[^`\s]*[\w-]+\.[A-Za-z][\w-]{0,15}`"
    # a backticked path or directory group: `tests/records/`, `src/ralphd`
    r"|`[^`\s]*/[^`]*`"
    # a bare path down to a file: docs/architecture.md, tests/test_x.py
    r"|\b[\w-]+(?:/[\w.-]+)*/[\w-]+\.[A-Za-z]{1,4}\b"
    # a bare directory group written with its trailing slash: tests/records/
    r"|\b[\w-]+(?:/[\w-]+)*/(?=[\s,.;:)]|$)"
)

# Reference noise stripped from a stage's clause BEFORE it is scanned: an
# issue number, a task id, a GitHub reference, a version string. These are
# identifiers, not measurements -- "Stage 1: investigate issue 42" states no
# intermediate target at all, and attempt 1 of this task credited its bare
# `42` as one (the verifier's first counterexample).
_REFERENCE_NOISE_RE = re.compile(
    r"#\d+"
    r"|\bGH-\d+"
    r"|\b(?:issues?|tasks?|PRs?|pull requests?|requirements?)\s+#?\d+"
    r"|\bv?\d+\.\d+(?:\.\d+)*",
    re.IGNORECASE,
)

# A unit a number can be a target OF. A number with no unit and no
# comparator is not an intermediate numeric target, it is just a digit.
_MEASURED_UNIT_RE = (
    r"(?:tests?|nodes?|modules?|cases?|assertions?|clauses?|checks?|guards?"
    r"|rows?|lines?|files?|entries?|records?|events?|fields?|keys?|routes?"
    r"|examples?|scenarios?|steps?|iterations?|passes|failures?|errors?"
    r"|warnings?|seconds?|minutes?|ms|s|%|percent|items?|paths?|columns?"
    r"|jobs?|lanes?|commits?|callers?|call sites?|occurrences?)"
)

# An intermediate numeric target: a number bound to a unit ("12 nodes"), a
# number bound to a comparator ("at least 12", ">= 12"), or a fraction of a
# whole ("12 of 15", "12/15"). Scoped to ONE stage's own clause below,
# never the whole criteria string, so a number elsewhere in the criteria
# never credits an unrelated stage.
_NUMERIC_TARGET_TOKEN_RE = re.compile(
    r"(?:\b(?:at least|at most|no fewer than|no more than|not fewer than"
    r"|not more than|minimum of|maximum of|up to|down to|from)\s+|[<>]=?\s*)"
    r"\d+\b"
    rf"|\b\d+\s*(?:\+\s*)?{_MEASURED_UNIT_RE}\b"
    r"|\b\d+\s*(?:of|/|->|to)\s*\d+\b",
    re.IGNORECASE,
)


def _measurable_criteria_gap(criteria: str) -> str | None:
    """`None` if `criteria` states something measurable -- an intermediate
    numeric target (a number bound to a unit or a comparator,
    `_NUMERIC_TARGET_TOKEN_RE`) or a named module group (a path or a
    backticked identifier, `_MODULE_GROUP_TOKEN_RE`) -- otherwise a short
    description of what is missing. Identifier noise (issue numbers, task
    ids, versions) is stripped BEFORE the scan, since an identifier is not
    a measurement: `investigate issue 42` measures nothing.
    """
    scannable = _REFERENCE_NOISE_RE.sub(" ", criteria)
    if _MODULE_GROUP_TOKEN_RE.search(scannable) or _NUMERIC_TARGET_TOKEN_RE.search(
        scannable
    ):
        return None
    return (
        "no intermediate numeric target (a number bound to a unit or a "
        "comparator, not an issue number or a task id) and no named module "
        "group (a module path or a backticked module name, not the bare "
        "word 'module')"
    )


def _normalised_clause(text: str) -> str:
    """`text` with identifier noise stripped, case folded and whitespace
    collapsed -- the form two stage tasks' criteria are compared in, so a
    stage restated verbatim is caught however it was spaced."""
    scannable = _REFERENCE_NOISE_RE.sub(" ", text)
    return " ".join(scannable.lower().split()).strip(" .,;:-")


def _stage_number(task: dict) -> int | None:
    """The `Stage N` label this task declares (digits only -- "Stage one" is
    not a marker), read from its `title` first and its `successCriteria`
    second, or `None` if it declares none."""
    for field in ("title", "successCriteria"):
        value = task.get(field)
        if isinstance(value, str):
            match = _STAGE_MARKER_RE.search(value)
            if match is not None:
                return int(match.group(1))
    return None


def _is_stage_task_of(stage: dict, parent: dict) -> bool:
    """`True` if `stage` is bound to `parent` -- by `dependsOn` in EITHER
    direction (the stages may depend on the oversized task or the oversized
    task may depend on its stages; the PRD names neither direction, so both
    are accepted) or by naming the parent's id in its own title/criteria.
    """
    parent_id = parent.get("id")
    stage_id = stage.get("id")
    stage_deps = stage.get("dependsOn") or []
    parent_deps = parent.get("dependsOn") or []
    if isinstance(stage_deps, list) and parent_id in stage_deps:
        return True
    if isinstance(parent_deps, list) and stage_id in parent_deps:
        return True
    if isinstance(parent_id, str) and parent_id:
        token = re.compile(rf"(?<![\w-]){re.escape(parent_id)}(?![\w-])")
        for field in ("title", "successCriteria"):
            value = stage.get(field)
            if isinstance(value, str) and token.search(value):
                return True
    return False


def _staged_decomposition_gap(task: dict, tasks: list) -> str | None:
    """`None` if the PROPOSAL decomposes the oversized `task` into staged
    tasks with their own measurable criteria -- otherwise a short,
    human-readable description of what is missing, for the caller to fold
    into its own error message naming the task.

    A **stage task** is a SEPARATE task in the same proposal that (a)
    carries a `Stage N` marker (digits) in its `title` or its
    `successCriteria` and (b) is bound to the oversized task, either by
    `dependsOn` in either direction or by naming its id
    (`_is_stage_task_of`) -- or is bound the same way to another stage task
    of it, so a descent written as a chain (`Stage 2` depends only on
    `Stage 1`, which depends on the parent) counts as one group.
    A genuine decomposition means all four of:

    1. at least two stage tasks bound to the oversized task -- headings
       written inside the oversized task's own criteria are NOT a
       decomposition, because they leave the work in one task of the same
       size;
    2. their `Stage N` labels, in proposal order, are the sequence 1..N --
       two `Stage 1`s, a skipped number or a descending pair is a list of
       restatements, not a descent, and proposal order is what the
       scheduler breaks priority ties by, so the descent is written in it;
    3. each stage task's own `successCriteria` is measurable
       (`_measurable_criteria_gap`): an intermediate numeric target or a
       named module group;
    4. no two stage tasks carry the same criteria text.

    This is a cross-task read, unlike every other lint in this function:
    that is what requirement D.2 asks for in so many words ("decomposes it
    into staged tasks with their own measurable criteria"), and it is the
    fault attempt 2 was rejected for -- it read the headings inside one
    task instead, refusing a decomposed proposal and accepting a monolith.
    Rule 3's tightened token patterns are attempt 2's own surviving work:
    `investigate issue 42` (a bare identifier), `the module works` (a bare
    noun) and duplicate labels are each still refused, each with its own
    message and its own node in
    `tests/test_estimated_iterations_staging.py`.
    """
    parent_id = task.get("id")
    candidates: list[tuple[int, dict]] = []
    for other in tasks:
        if not isinstance(other, dict) or other is task:
            continue
        if other.get("id") == parent_id:
            continue
        number = _stage_number(other)
        if number is not None:
            candidates.append((number, other))

    # Bound directly to the oversized task, then transitively through any
    # stage already in the group: a chain (`Stage 2` dependsOn `Stage 1`
    # dependsOn the parent) is one descent, not two strangers.
    stage_ids: set = set()
    grew = True
    while grew:
        grew = False
        for _, candidate in candidates:
            if candidate.get("id") in stage_ids:
                continue
            anchors = [
                other for _, other in candidates if other.get("id") in stage_ids
            ]
            if _is_stage_task_of(candidate, task) or any(
                _is_stage_task_of(candidate, anchor)
                or _is_stage_task_of(anchor, candidate)
                for anchor in anchors
            ):
                stage_ids.add(candidate.get("id"))
                grew = True
    stages = [
        (number, other)
        for number, other in candidates
        if other.get("id") in stage_ids
    ]

    if len(stages) < 2:
        return (
            "the proposal holds fewer than two stage tasks for it -- a stage "
            "task is a SEPARATE task in the same proposal carrying a "
            "'Stage N' marker (digits) in its title or successCriteria and "
            f"bound to task {parent_id!r} by dependsOn (either direction), by "
            "naming its id, or by the same link to another of its stage "
            "tasks; 'Stage N' headings written inside this task's "
            "own criteria decompose nothing, since the work stays in one "
            "task of the same size"
        )

    numbers = [number for number, _ in stages]
    if numbers != list(range(1, len(numbers) + 1)):
        return (
            f"the stage tasks' 'Stage N' labels {numbers}, in proposal "
            f"order, are not the sequence {list(range(1, len(numbers) + 1))} "
            "-- duplicated, skipped or out-of-order stage numbers are a list "
            "of restatements, not a staged descent"
        )

    seen_clauses: dict[str, int] = {}
    for number, stage in stages:
        criteria = stage.get("successCriteria")
        criteria = criteria if isinstance(criteria, str) else ""
        measurable_gap = _measurable_criteria_gap(criteria)
        if measurable_gap is not None:
            return (
                f"stage task {stage.get('id')!r} (stage {number}) names "
                f"{measurable_gap}"
            )
        normalised = _normalised_clause(criteria)
        if normalised in seen_clauses:
            return (
                f"stage tasks {seen_clauses[normalised]} and {number} carry "
                "the same successCriteria text -- a stage repeated verbatim "
                "decomposes nothing"
            )
        seen_clauses[normalised] = number
    return None


# Task 027 (PRD requirement D.3, #157): "A numeric target far from its
# baseline is a smell." A criterion of the shape `<metric> under <target>`
# whose plan-quoted baseline is more than 2x the target is refused unless
# the plan names the measurement task that re-measures the baseline at the
# launch sha and stages the descent. This is D.2's own neighbour in the
# PRD's requirement D list and reuses D.2's cross-task linkage primitive
# (`_is_stage_task_of`) rather than inventing a second one: the measurement
# task is bound to the offending task exactly the way a D.2 stage task is
# bound to its oversized parent (`dependsOn` in either direction, or by
# naming the offending task's id).
#
# The target: a number immediately following the word "under" ("... stays
# under 3000", "get the count under 3000"). The baseline: a number quoted
# in the SAME criteria text behind one of the cue phrases below
# ("currently ~8000", "baseline of 8000", "measured at 8000") -- a number
# with no such cue is not a "plan-quoted baseline", it is just a digit, and
# a criterion with no quoted baseline at all states no smell to catch (a
# bare "<metric> under <target>" with nothing to compare it to passes
# untouched, same as one already within 2x).
_UNDER_TARGET_RE = re.compile(r"\bunder\s+(\d[\d,]*)\b", re.IGNORECASE)

_BASELINE_CUE_RE = re.compile(
    r"(?:baseline(?:\s+of|\s+is)?|currently|presently|today|now at|sits at"
    r"|stands at|measured at|quoted at|starting at|existing count of)\s*"
    r"[:\-]?\s*(?:~|about|approx(?:imately)?\.?\s*)?(\d[\d,]*)\b",
    re.IGNORECASE,
)

# A measurement task's own wording: it must say it RE-MEASURES the
# baseline, anchor that re-measurement to the "launch sha" (the term the
# PRD itself uses), and stage the descent -- a `Stage N` marker
# (`_STAGE_MARKER_RE`, the same convention D.2's own stage tasks use) is
# what "stages" means here, rather than a second, parallel vocabulary for
# the same idea.
_REMEASURE_VERB_RE = re.compile(r"\bre-?measur\w*\b", re.IGNORECASE)
_LAUNCH_SHA_RE = re.compile(r"\blaunch sha\b", re.IGNORECASE)

# Those three cues alone say only that SOME baseline is re-measured at the
# launch sha in SOME stage -- attempt 1 of task 027 shipped exactly that
# and validation refused it, because a linked task titled "Stage 1:
# re-measure a DIFFERENT metric at the launch sha" whose criteria read
# "Record the slow tier latency; no descent plan is defined." satisfied all
# three. The requirement names two more things, and both are read off the
# OFFENDING criterion rather than off generic vocabulary:
#
#   * "re-measures THE BASELINE" -- the measurement task must identify the
#     offending metric, either by quoting the offending baseline number
#     itself or by naming at least `_MIN_SHARED_METRIC_WORDS` of the
#     offending clause's own distinctive metric words (the words before
#     "under", minus the stopwords below). Re-measuring the slow tier's
#     latency is not re-measuring the fast tier's collected test count.
#   * "and stages THE DESCENT" -- the descent has an endpoint, and the
#     endpoint is the offending target, so the measurement task must quote
#     that target number. A stage that measures and stops describes no
#     descent at all.
_METRIC_WORD_RE = re.compile(r"[a-z][a-z'-]{2,}", re.IGNORECASE)
_METRIC_STOPWORDS = frozenset(
    {
        "and",
        "are",
        "but",
        "every",
        "for",
        "from",
        "has",
        "have",
        "its",
        "must",
        "not",
        "remain",
        "remains",
        "stay",
        "stays",
        "than",
        "that",
        "the",
        "their",
        "then",
        "this",
        "under",
        "was",
        "were",
        "which",
        "with",
    }
)
_MIN_SHARED_METRIC_WORDS = 2


def _number_token(value: int) -> re.Pattern:
    """A pattern matching `value` written either bare (`8000`) or with
    thousands separators (`8,000`), and never as part of a longer number."""
    digits = str(value)
    grouped = f"{value:,}"
    alternatives = "|".join(
        sorted({re.escape(digits), re.escape(grouped)}, key=len, reverse=True)
    )
    return re.compile(rf"(?<![\d,])(?:{alternatives})(?![\d,])")


def _metric_words(text: str) -> set:
    """The distinctive words of a metric phrase: lowercased alphabetic words
    of three characters or more, minus `_METRIC_STOPWORDS`. Possessives are
    folded (`tier's` -> `tier`) so "the fast tier's collected test count"
    and "the fast tier count" share their metric words."""
    words = set()
    for match in _METRIC_WORD_RE.finditer(text):
        word = match.group(0).lower()
        word = re.sub(r"'s$|s'$", "", word)
        if len(word) >= 3 and word not in _METRIC_STOPWORDS:
            words.add(word)
    return words


def _quoted_baseline_target_gap(task: dict, tasks: list) -> str | None:
    """`None` if this `task`'s `successCriteria` states no `<metric> under
    <target>` clause with a plan-quoted baseline at all, or if that
    baseline is within 2x the target, or if the plan names a linked
    measurement task that re-measures THAT baseline at the launch sha and
    stages the descent toward THAT target -- otherwise a short,
    human-readable description of the missing measurement task, for the
    caller to fold into its own error message naming the offending task.

    A measurement task qualifies only when its own title/successCriteria
    carries all five of:

    1. a re-measure verb (`_REMEASURE_VERB_RE`);
    2. the phrase "launch sha" (`_LAUNCH_SHA_RE`);
    3. a `Stage N` marker (`_STAGE_MARKER_RE`), D.2's own staging
       convention;
    4. the offending metric's identity -- the offending baseline number
       quoted, or at least `_MIN_SHARED_METRIC_WORDS` of the offending
       clause's distinctive metric words (`_metric_words`);
    5. the descent's endpoint -- the offending target number quoted.

    4 and 5 are what attempt 1 lacked: with only 1-3, a stage task that
    re-measures some OTHER metric and defines no descent was accepted
    (validation's own counter-example, quoted in the block comment above).
    """
    criteria = task.get("successCriteria")
    criteria = criteria if isinstance(criteria, str) else ""
    target_match = _UNDER_TARGET_RE.search(criteria)
    if target_match is None:
        return None
    baseline_match = _BASELINE_CUE_RE.search(criteria)
    if baseline_match is None:
        return None
    target = int(target_match.group(1).replace(",", ""))
    baseline = int(baseline_match.group(1).replace(",", ""))
    if target <= 0 or baseline <= 2 * target:
        return None

    # The metric phrase is what stands before "under" in the offending
    # clause (clause boundaries: `;` and the sentence enders, so a second
    # sentence about something else contributes no metric words).
    clause_start = max(
        criteria.rfind(sep, 0, target_match.start()) + 1
        for sep in (";", ".", "!", "?", "\n")
    )
    metric_words = _metric_words(criteria[clause_start : target_match.start()])
    baseline_token = _number_token(baseline)
    target_token = _number_token(target)

    task_id = task.get("id")
    for other in tasks:
        if not isinstance(other, dict) or other is task:
            continue
        if not _is_stage_task_of(other, task):
            continue
        other_criteria = other.get("successCriteria")
        other_criteria = other_criteria if isinstance(other_criteria, str) else ""
        other_title = other.get("title")
        other_title = other_title if isinstance(other_title, str) else ""
        combined = f"{other_title}\n{other_criteria}"
        if not (
            _REMEASURE_VERB_RE.search(combined)
            and _LAUNCH_SHA_RE.search(combined)
            and _STAGE_MARKER_RE.search(combined)
        ):
            continue
        names_this_baseline = bool(
            baseline_token.search(combined)
        ) or len(metric_words & _metric_words(combined)) >= _MIN_SHARED_METRIC_WORDS
        if not names_this_baseline:
            continue
        if not target_token.search(combined):
            continue
        return None

    return (
        f"the baseline ({baseline}) is more than 2x the target ({target}) "
        "and the plan names no measurement task bound to task "
        f"{task_id!r} (by dependsOn in either direction or by naming its "
        "id) whose own title or successCriteria does all of: re-measures "
        "at the launch sha (a 're-measure'/'remeasure' verb AND the phrase "
        "'launch sha'), stages the work (a 'Stage N' marker), identifies "
        f"THIS baseline (quotes {baseline}, or names at least "
        f"{_MIN_SHARED_METRIC_WORDS} of this criterion's own metric words "
        f"{sorted(metric_words)}) and names THIS descent's endpoint (quotes "
        f"the target {target}) -- re-measuring some other metric, or "
        "measuring this one with no descent toward the target, is neither "
        "the measurement nor the staging the requirement asks for"
    )


def lint_proposal(
    payload: dict,
    *,
    extra_known_ids: set | None = None,
    warnings: list[str] | None = None,
) -> list[str]:
    """Semantic lint over an already shape-valid proposal. Every error
    names the offending task's id. This function does NOT re-check
    requirement H's own-terminal-state rule -- that lint is task 012's
    (kept separate, `lint_own_terminal_state`). It DOES enforce all SIX of
    the contract's plan-time lints (relational sha anchors; single-commit
    demands on a deliverable with a mandated second commit; forward
    citation of a later task's commit sha; unscoped write-time occurrence
    counts; scratch-state content/format conditions; unenumerated universal
    quantifiers and clause-count overruns) plus the SEVENTH (task 013,
    requirement E, #143): the six structurally-unsatisfiable shapes --
    later/final/last commit references, predeclared event states, a record
    citing the commit that writes it, an empty git range, an "exactly
    once" commit count, and a notification/HTTP-status clause -- and the
    EIGHTH (task 009, requirement A.5, #154): a command-form clause (the
    literal text of a shell command, a wrapper, a polling cadence, a
    "nothing else" clause) or a launch form paired with a demand for the
    same process's captured status -- and the NINTH (task 026, requirement
    D.2, #157): a task whose `estimatedIterations` is past
    `_MAX_ESTIMATED_ITERATIONS_WITHOUT_STAGES` and no staged decomposition
    into stage TASKS anywhere in the same proposal -- and the TENTH (task
    027, requirement D.3, #157): a `<metric> under <target>` clause whose
    plan-quoted baseline is more than 2x the target with no linked
    measurement task that re-measures the baseline at the launch sha and
    stages the descent -- see the block comments immediately above.

    `extra_known_ids` (task 017, PRD requirement B.2, #162): the ordinary
    planner call site never passes this -- a planner's file-borne proposal
    has no "live plan" outside itself, so `dependsOn` stays scoped to
    exactly the ids the proposal itself declares (`known_ids` below, byte
    for byte the same set it has always been). `plan_ingestion.
    lint_worker_split` is the one caller that passes it: a worker's split
    batch lives ALONGSIDE the current `tasks.json`, not instead of it, so
    a child naming the parent (or any other already-live task) in its own
    `dependsOn` is not an "unknown task" -- it is a real dependency this
    lint has no reason to refuse. `known_ids` becomes the UNION of the
    batch's own ids and this extra set; a dep in neither is still refused,
    with the same message, unchanged for every existing caller that leaves
    this at its default `None` (treated as empty).

    `warnings` (task 023, PRD requirement F.2, #222): an optional OUT list,
    a channel entirely distinct from the `errors` this function returns --
    a warning never rejects a proposal, so it is never folded into the
    return value. Every existing caller leaves this at its default `None`
    (this function makes its own throwaway list and the warning is simply
    never read back) so nothing about the accepted-or-refused verdict for
    ANY existing caller changes. `_parse_proposal` is the one caller that
    passes a real list, to carry the `orderingOnly`-over-declaration
    warning below onto `PlanIngestionResult.warnings` -- the channel task
    022 (requirement F.1) made a runtime consumer surface at all."""
    if warnings is None:
        warnings = []
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
            ref_index = _resolve_task_index(id_to_index, ref_id)
            if ref_index is not None and ref_index > i:
                errors.append(
                    f"task {tid!r}: successCriteria requires the commit sha "
                    f"of task {ref_id!r} ({ref_clause!r}), which is ordered "
                    "LATER in this same plan -- cite only a task ordered "
                    "earlier (already committed by the time this task "
                    f"runs), or move the citation into task {ref_id!r}'s "
                    "own criteria instead"
                )

        occurrence_clause = _unscoped_occurrence_count_clause(criteria)
        if occurrence_clause is not None:
            errors.append(
                f"task {tid!r}: successCriteria requires an unscoped "
                f"write-time occurrence count ({occurrence_clause!r}) -- a "
                "count of a string the task's own mandated output contains "
                "changes the moment that output is written, so an unscoped "
                'figure is stale by construction; write the sha-scoped '
                'form instead ("at `<sha>`: N")'
            )

        scratch_clause = _scratch_state_content_clause(criteria)
        if scratch_clause is not None:
            errors.append(
                f"task {tid!r}: successCriteria conditions on the content "
                f"or format of untracked run state ({scratch_clause!r}) -- "
                "no criterion may condition on the content or format of "
                "/run/ralphd/notes.md, handoff notes, or any path outside "
                "the repository tree beyond the declared existence of an "
                "artifact; require the artifact to exist and let a pointer "
                "to it stand as sufficient evidence of its content instead"
            )

        quantifier_clause = _unenumerated_quantifier_clause(criteria)
        if quantifier_clause is not None:
            quant_rewrite = _unenumerated_quantifier_rewrite(criteria)
            errors.append(
                f"task {tid!r}: successCriteria quantifies over an "
                f"unenumerated set ({quantifier_clause!r}) [unenumerated-"
                "quantifier] -- an every/each/all/\"no ... anywhere\" "
                "clause must either name every member explicitly (e.g. "
                '"each of tasks 001, 002 and 003") or name the single '
                'defining command/file that bounds the set (e.g. "every '
                'test in tests/test_x.py passes"), never an open-ended '
                "quantifier with the members left unnamed -- suggested "
                f"rewrite: {quant_rewrite[1]!r}"
            )

        overrun = _clause_count_overrun(criteria)
        if overrun is not None:
            clause_count, overflow_clause = overrun
            overrun_rewrite = _clause_count_overrun_rewrite(criteria)
            errors.append(
                f"task {tid!r}: successCriteria carries {clause_count} "
                f"checkable clauses, past the ~{_MAX_CHECKABLE_CLAUSES}-"
                "clause bound [clause-count-overrun] (first clause past "
                f"the bound: {overflow_clause!r}) -- split this task into "
                "smaller ones, or move any wave-wide obligation this "
                "criteria repeats (a boundary check, a notification-log "
                "entry, an operator discipline) into the Standing Rules "
                "block once instead of copying it per task -- suggested "
                f"rewrite: {overrun_rewrite[2]!r}"
            )

        shape_match = _unsatisfiable_shape_clause(criteria)
        if shape_match is not None:
            offending_clause, alternative, rewrite = shape_match
            errors.append(
                f"task {tid!r}: successCriteria is structurally "
                f"unsatisfiable ({offending_clause!r}) -- this clause "
                f"{alternative} -- suggested rewrite: {rewrite!r}"
            )

        command_form_match = _command_form_shape_clause(criteria)
        if command_form_match is not None:
            offending_clause, alternative = command_form_match
            errors.append(
                f"task {tid!r}: successCriteria pins a command form "
                f"({offending_clause!r}) -- this clause {alternative}"
            )

        launch_status_match = _launch_form_with_captured_status_clause(criteria)
        if launch_status_match is not None:
            launch_clause, status_clause = launch_status_match
            cited = (
                repr(launch_clause)
                if launch_clause == status_clause
                else f"{launch_clause!r} + {status_clause!r}"
            )
            errors.append(
                f"task {tid!r}: successCriteria both pins a launch form "
                f"and demands the same process's captured status "
                f"({cited}) -- a bare backgrounded launch "
                "cannot hand back its own exit status without a wrapper, so "
                "the two demands cannot both hold; name the file that holds "
                "the captured result and let the worker choose the launch "
                "form"
            )

        estimated_iterations = t.get("estimatedIterations")
        if (
            isinstance(estimated_iterations, int)
            and not isinstance(estimated_iterations, bool)
            and estimated_iterations > _MAX_ESTIMATED_ITERATIONS_WITHOUT_STAGES
        ):
            gap = _staged_decomposition_gap(t, tasks)
            if gap is not None:
                errors.append(
                    f"task {tid!r}: estimatedIterations is "
                    f"{estimated_iterations}, past the "
                    f"{_MAX_ESTIMATED_ITERATIONS_WITHOUT_STAGES}-iteration "
                    f"bound, and the proposal does not decompose it into "
                    f"staged tasks with their own measurable criteria "
                    f"({gap}) -- either lower the estimate to "
                    f"{_MAX_ESTIMATED_ITERATIONS_WITHOUT_STAGES} or fewer, "
                    "or split it into separate tasks titled 'Stage 1: ...', "
                    "'Stage 2: ...' (labels numbered 1..N in proposal order, "
                    "no repeats), each bound to this task by dependsOn or by "
                    "naming its id, and each carrying its own measurable "
                    "successCriteria: an intermediate numeric target (a "
                    "number bound to a unit or a comparator -- an issue "
                    "number or task id is not a target) or a named module "
                    "group (a module path or backticked module name -- the "
                    "bare word 'module' is not a group), with no two stage "
                    "tasks repeating the same criteria"
                )

        baseline_gap = _quoted_baseline_target_gap(t, tasks)
        if baseline_gap is not None:
            errors.append(
                f"task {tid!r}: successCriteria states a numeric target "
                f"with a plan-quoted baseline that is a smell ({baseline_gap}) "
                "-- either lower the plan-quoted baseline claim, drop the "
                "baseline quote so the criterion states only the target, or "
                "add a measurement task bound to this task (by dependsOn or "
                "by naming its id) whose own title or successCriteria "
                "re-measures THIS metric's baseline at the launch sha (naming "
                "the baseline number or this criterion's own metric words) "
                "and stages the descent toward THIS target with a 'Stage N' "
                "marker and the target number"
            )

    known_ids = set(seen) | (extra_known_ids or set())
    for t in tasks:
        tid = t.get("id")
        for dep in (t.get("dependsOn") or []):
            if dep not in known_ids:
                if extra_known_ids is not None:
                    errors.append(
                        f"task {tid!r}: dependsOn references unknown task "
                        f"{dep!r} -- not in the proposed batch "
                        f"{sorted(str(i) for i in seen)!r} and not in the "
                        f"current plan {sorted(str(i) for i in extra_known_ids)!r}"
                    )
                else:
                    errors.append(f"task {tid!r}: dependsOn references unknown task {dep!r}")
        # GH #209 defect 3's residual: an ordering-only entry that is not one
        # of THIS task's own `dependsOn` ids declares nothing at all -- the
        # field marks a SUBSET of the edges the task actually has. Refused
        # rather than ignored, because the plausible planner mistake is
        # naming the right task in the wrong place (a sibling's id, or an id
        # it meant to depend on and never did), and an inert declaration
        # reads to its author as an ordering edge that the cure pass then
        # drags back through anyway.
        #
        # This is the ONLY half of the field a lint can check. The semantic
        # claim -- that this dependency really produces no evidence this task
        # reports on -- is exactly the judgement the engine cannot make (it is
        # why the field is planner-declared in the first place), so a wrong
        # declaration is not caught here and the conservative default is what
        # bounds it: see `ordering_only_dependencies` above and
        # `state._lane_evidence_record_chain`, which still re-opens and
        # re-takes the evidence RECORD itself on every cure pass, so review
        # never judges stale evidence for the invalidated path however the
        # edges around it are annotated.
        declared = t.get(ORDERING_ONLY_FIELD)
        if isinstance(declared, list) and declared:
            raw_depends = t.get("dependsOn")
            own = ({str(d) for d in raw_depends}
                   if isinstance(raw_depends, list) else set())
            all_entries_valid = True
            for entry in declared:
                if str(entry) not in own:
                    all_entries_valid = False
                    errors.append(
                        f"task {tid!r}: {ORDERING_ONLY_FIELD} names {entry!r}, "
                        f"which is not one of its own dependsOn ids "
                        f"{sorted(own)!r} -- the field marks a SUBSET of this "
                        "task's dependencies as pure ordering, so every entry "
                        "must name an edge the task actually has")
            # Task 023 (PRD requirement F.2, #222): the SHAPE half of the
            # field is validated above (every entry names a real edge); this
            # is the one MECHANICAL check the degenerate case admits --
            # `orderingOnly` naming the task's WHOLE `dependsOn` set, not a
            # proper subset. That is never refused (the semantic claim behind
            # a correct full declaration is not this lint's to judge -- see
            # the block comment above), but it is exactly the shape
            # `state._lane_evidence_record_chain`'s filter turns into an
            # EMPTY verification list for every re-opened record: a cure pass
            # over this task re-takes no upstream evidence at all, which is
            # the loss the conservative default exists to prevent. A WARNING
            # only -- never folded into `errors` -- naming the task so a
            # planner (or an operator reading `PlanIngestionResult.warnings`,
            # task 022's surfaced channel) can see the over-declaration and
            # confirm it is intended.
            if all_entries_valid and own and {str(d) for d in declared} == own:
                warnings.append(
                    f"task {tid!r}: {ORDERING_ONLY_FIELD} names its WHOLE "
                    f"dependsOn set {sorted(own)!r} -- every dependency is "
                    "declared pure ordering, so a cure pass over this task "
                    "will re-take no upstream evidence at all; confirm that "
                    "is intended")

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
#
# Task 013: the lookahead restricts a split to a `.`/`!`/`?`/`;` that is
# followed by whitespace or the end of the string -- a genuine sentence
# boundary in ordinary prose. Without it, a literal dotted token inside a
# single clause (`/run/ralphd/notes.md`, `notes.md`) would itself get cut
# in two, which would silently defeat the two scratch-state/occurrence-
# count lints below (both name `notes.md` in their own cue text) despite
# never touching any existing fixture: every clause boundary already in
# use elsewhere in this module is a real `. `/`.$`-style sentence end
# (`status.json leaves` has no space after its dot either, and is matched
# by `_LEAVES_RUNNING_RE` over the whole string, never through this split
# at all), so this widens what stays inside one clause without narrowing
# it -- the #86/#76 regression tests below are unaffected.
_CLAUSE_SPLIT_RE = re.compile(r"[.!?;]+(?=\s|$)")


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
    * `lint_proposal`'s own `dependsOn` resolution now accepts an id from
      EITHER of two sets (task 017, PRD requirement B.2, #162): the ids
      present in `proposed_tasks` itself, or `existing_ids`, the caller's
      live `tasks.json` id set -- passed through as `lint_proposal`'s
      `extra_known_ids`. A child chaining itself behind a sibling in the
      same batch, a child inheriting the parent's own pre-existing
      `dependsOn` (`LoopSupervisor._fold_inherited_dependsOn`, task 016),
      and a child that simply names the parent (`assigned_task_id`, which
      `existing_ids` always contains -- it is a live task in the plan
      being split) all resolve cleanly; an id in NEITHER set is still
      refused, and `lint_proposal` now names both sets in that refusal's
      own text so the caller can see exactly what was and was not
      considered known.

      The one thing that widened check still has no way to know is
      whether a proposed id COLLIDES with one already LIVE in the plan
      (`lint_proposal` only ever sees the proposed batch's own ids as
      candidates for a NEW task) -- checked here, unchanged, against
      `existing_ids` directly.

    Called from `LoopSupervisor._apply_worker_proposed_tasks` and
    `LoopSupervisor._apply_forced_split_report`
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
    errors = (lint_proposal(payload, extra_known_ids=existing_ids)
              + lint_own_terminal_state(payload))
    collisions = sorted({
        t.get("id") for t in payload["tasks"]
        if isinstance(t, dict) and t.get("id") in existing_ids
    })
    if collisions:
        errors.append(
            f"proposed_tasks: id(s) already exist in the plan: {collisions}"
        )
    return errors


# #209 defect 7: the two statuses a `proposed_task_updates` re-point may
# DROP a `dependsOn` edge onto -- duplicated from
# `state.TASK_DEPENDENCY_DEAD_STATUSES` rather than imported, because this
# module must not depend on `state` (the real dependency runs the other way:
# `state` imports `plan_ingestion`), the same module-boundary duplication
# `state._WORKER_SPLIT_PASSTHROUGH_FIELDS` already makes in the opposite
# direction for this module's own `_TASK_PASSTHROUGH_FIELDS`. The two tuples
# are pinned equal by `tests/test_worker_split_proposal.py` so the copy
# cannot drift.
_REPOINTABLE_DEAD_STATUSES = ("failed", "skipped")


def lint_worker_task_updates(proposed_task_updates: Any, *,
                             assigned_task_id: str | None,
                             tasks: list[dict],
                             doomed_ids: set) -> list[str]:
    """#209 defect 7: the lint for a worker's optional
    `proposed_task_updates` re-point (`transitions.HAT_SCHEMAS["worker"]`),
    the sibling of `lint_worker_split` just above -- and the whole of what
    makes that channel safe, since the one thing it writes is an EXISTING
    task's `dependsOn`.

    Why the channel exists at all: `tasks.json` is harness-owned, and a
    worker's only structural lever was `proposed_tasks`, which can add a
    row but can never re-point one. The engine already re-points dependents
    itself on two paths -- `state._repoint_dependents_onto_cure_task` from
    `insert_cure_tasks`, and `petitions.apply_confirmed_petition_
    adjudications` for a CONFIRMED petition -- so this is the RESIDUAL
    case, and only that: a doomed dependent for which no cure task exists
    to be re-pointed onto (the observed incident re-pointed a `pending`
    task off a `failed` ancestor onto an already-`validated` sibling).
    Anything either engine path can clear still clears without this field.

    `tasks` and `doomed_ids` are passed IN by the caller for exactly the
    reason `lint_worker_split`'s `existing_ids` is: this module stays
    engine-loop-agnostic and never reads `tasks.json` itself.
    `doomed_ids` is `state.task_blocked_by`'s own derived map, so the
    standing to rewrite a graph is the HARNESS's judgement that the task is
    unschedulable, never the worker's opinion of it.

    The two refusals at the top return alone; past them every ENTRY that
    fails reports its failure(s) and the walk carries on to the next, so one
    call names every bad entry rather than only the first. Plural for one
    entry in exactly one place, deliberately: a blank `reason` is recorded
    and the SAME entry is then still checked for its `dependsOn` shape,
    because both are things the worker has to fix and one round trip per
    refusal is the cost of splitting them. Either way the WHOLE array is
    refused if any entry fails -- there is no partial application, and the
    working-copy fold below is therefore read by nothing but the cycle check
    of a batch that is already doomed to be refused. The guards, in order:

    * an assigned task is required, the same standing rule
      `lint_worker_split` opens with: this is a repair a worker makes in
      passing while holding real work, not a plan-editing mandate;
    * each entry is a dict carrying a non-blank `task_id`, a `dependsOn`
      list of non-blank strings and a non-blank `reason` -- all three
      mandatory, one entry per `task_id` so the batch has one meaning;
    * the target must be a task actually present in `tasks`, and its
      status must be exactly `pending`: narrower than
      `state.TASK_CLAIMED_STATUSES` on purpose, because an `in-progress`/
      `validation-failed` target carries the #184 hazard
      `state._depends_on_claimed_refusal` names (never rewrite the graph
      under a live attempt) and a terminal one has nothing left to unblock;
    * the target must be a key of `doomed_ids`;
    * every DROPPED id must name a task whose status is in
      `_REPOINTABLE_DEAD_STATUSES` -- the anti-abuse guard: a dependency a
      worker merely does not want to wait for is alive, and dropping it is
      refused;
    * NO id in the replacement list may be dead, so a re-point cannot mint
      a fresh deadlock ("a failed dependency never satisfies"). Stated over
      the whole replacement rather than only over the ids it ADDS -- which
      is stricter than "adds none", deliberately: an entry that KEPT a dead
      edge it already had would be accepted while leaving the target
      exactly as doomed as it found it, which is a re-point that achieves
      nothing except a free stagnation reset. This spelling also makes the
      rule one the prompt can state in one clause. `petitioned` is
      deliberately allowed -- a hold can still resolve, which is exactly
      why `task_blocked_by` keeps `petitioned` out of its own doomed set;
    * the replacement must DROP at least one edge (#209 defect 7, unit 6
      review). An entry whose list is byte-identical to the current one, or
      that only ADDS, is not a repair and is refused: past the two guards
      above such a target holds no dead edge of its own, so it is doomed
      only TRANSITIVELY -- some live dependency of its own is the doomed one
      -- and adding edges can only constrain it further. Accepting one would
      write the row, file a `task.dependency_repointed` claiming a re-point
      that did not happen, and hand back a free progress diff (so a free
      `stagnant` reset), REPEATABLY, since the target stays in `doomed_ids`.
      A transitively doomed task therefore has standing here but no legal
      repair -- only its blocking parent's own re-point (or a replan)
      clears it, which is what the refusal text says;
    * the candidate list goes through `validate_dependency_edges`, the one
      self-loop/unresolved-id/cycle detector every other `dependsOn`
      writer already shares. Its contract is "a bad edge costs itself,
      never the write" -- for THIS caller that would silently produce the
      outcome the drop-only-dead guard above forbids (an edge quietly
      vanishing from the replacement list), so here a single rejected edge
      refuses the whole batch instead. The refusal text is the validator's
      own reason string, verbatim.

    Entries are folded onto a working copy of the graph as they validate
    clean, so two entries that only close a cycle TOGETHER are caught --
    the same discipline `validate_dependency_edges` already applies edge by
    edge within one call. The working copy is a list of shallow row copies
    with a FRESH `dependsOn` list assigned, never a deep copy: nothing here
    mutates a nested value, so the caller's own `tasks` (and the rows
    inside it) are untouched.

    `doomed_ids` is deliberately the caller's PRE-batch snapshot and is
    never recomputed as entries fold. Standing can only be LOST by an
    accepted fold, never gained (a fold only ever removes a dead edge, and
    the replacement may name none), and an entry naming a target an earlier
    fold already un-doomed has no dead edge left to drop, so the
    drop-at-least-one guard above refuses it anyway -- re-deriving the map
    per entry would refuse the same entries for a different reason.
    """
    if not assigned_task_id:
        return ["proposed_task_updates: no task was assigned this iteration "
                "-- a dependsOn re-point requires a real assignment to ride "
                "alongside"]
    if not isinstance(proposed_task_updates, list) or not proposed_task_updates:
        return ["proposed_task_updates: expected a non-empty array, got "
                f"{type(proposed_task_updates).__name__}"]
    working = [dict(t) for t in tasks if isinstance(t, dict)]
    by_id = {str(t.get("id")): t for t in working if t.get("id")}
    errors: list[str] = []
    seen: set[str] = set()
    for i, entry in enumerate(proposed_task_updates):
        where = f"$.proposed_task_updates[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{where}: expected an object, got "
                          f"{type(entry).__name__}")
            continue
        raw_id = entry.get("task_id")
        where = f"{where} (task_id={raw_id!r})"
        if not isinstance(raw_id, str) or not raw_id.strip():
            errors.append(f"{where}: missing or blank 'task_id'")
            continue
        task_id = raw_id.strip()
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"{where}: missing or blank 'reason' -- a re-point "
                          "must say which dead edge it drops and what carries "
                          "that work instead")
        candidate = entry.get("dependsOn")
        if not isinstance(candidate, list) or any(
                not isinstance(d, str) or not d.strip() for d in candidate):
            errors.append(f"{where}: 'dependsOn' must be an array of "
                          "non-blank strings (the FULL replacement list, "
                          "not a delta)")
            continue
        if task_id in seen:
            errors.append(f"{where}: duplicate task_id -- one entry per task, "
                          "so the batch has exactly one meaning for each")
            continue
        seen.add(task_id)
        row = by_id.get(task_id)
        if row is None:
            errors.append(f"{where}: names unknown task {task_id!r} -- not in "
                          "the current plan")
            continue
        status = row.get("status", "pending")
        if status != "pending":
            errors.append(
                f"{where}: task is {status!r}, not 'pending' -- a re-point "
                "rewrites the graph a claimed task's own attempt is already "
                "executing against, and a terminal task has nothing left to "
                "unblock")
            continue
        if task_id not in {str(d) for d in doomed_ids}:
            errors.append(
                f"{where}: the harness does not derive task {task_id!r} as "
                "blocked (its taskBlockedBy map is the standing for a "
                "re-point, not your own reading of the plan)")
            continue
        old = [str(d) for d in (row.get("dependsOn") or [])]
        new = [d.strip() for d in candidate]
        dropped = [d for d in old if d not in new]
        live_drops = sorted({
            d for d in dropped
            if (by_id.get(d) or {}).get("status", "pending")
            not in _REPOINTABLE_DEAD_STATUSES})
        if live_drops:
            errors.append(
                f"{where}: refuses to drop live dependency(ies) {live_drops} "
                "-- only an edge onto a 'failed' or 'skipped' task may be "
                "dropped, and a dependency you merely do not want to wait "
                "for is not a dead one")
            continue
        dead_kept = sorted({
            d for d in new
            if (by_id.get(d) or {}).get("status", "pending")
            in _REPOINTABLE_DEAD_STATUSES})
        if dead_kept:
            errors.append(
                f"{where}: refuses dependency(ies) {dead_kept} onto a "
                "'failed'/'skipped' task -- a failed dependency never "
                "satisfies, so that is a deadlock, not a repair")
            continue
        # #209 defect 7 (unit 6 review): AFTER the two guards above, so a
        # replacement that names a dead task still reads the specific
        # refusal about that edge rather than this generic one.
        if not dropped:
            errors.append(
                f"{where}: drops no 'failed'/'skipped' edge, so it repairs "
                "nothing -- this task is blocked only THROUGH a live "
                "dependency of its own, and only re-pointing THAT dependency "
                "(or a replan) clears it")
            continue
        accepted, rejected = validate_dependency_edges(working, task_id, new)
        if rejected:
            errors.extend(f"{where}: {r.get('reason')}" for r in rejected)
            continue
        row["dependsOn"] = list(accepted)
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


# Requirement D.1 (#157): the estimate `LoopSupervisor._task_progress_
# budget` (`engine/loop.py`) already reads as `task.get("estimatedIterations",
# 1)` -- a runtime fallback that covers a `tasks.json` task with no such key
# at all. This constant is the SAME default, named once, applied at
# ingestion time instead of left to that runtime fallback, so a task that
# went through ingestion always carries an explicit, already-validated
# value rather than relying on the loop's own absent-key default to agree
# with it by coincidence.
DEFAULT_ESTIMATED_ITERATIONS = 1


def normalize_task_estimated_iterations(payload: dict) -> list[str]:
    """Requirement D.1, task 025 (#157): default every task's missing
    `estimatedIterations` to `DEFAULT_ESTIMATED_ITERATIONS` IN PLACE and
    return a list of WARNING strings recording that it defaulted -- the
    exact same channel and shape `normalize_task_tiers` above uses for
    `tier`, and run alongside it. A task that already carries a valid
    integer >= 1 (`validate_proposal_shape` has already refused anything
    else by the time this runs -- see `_parse_proposal`) passes through
    verbatim with no warning. Nothing here ever rejects a proposal; a
    refusal is `validate_proposal_shape`'s job, at shape time, before this
    function ever runs."""
    warnings: list[str] = []
    for t in payload.get("tasks", []):
        if not isinstance(t, dict):
            continue
        if "estimatedIterations" not in t:
            warnings.append(
                f"task {t.get('id')!r}: no estimatedIterations annotated; "
                f"defaulted to {DEFAULT_ESTIMATED_ITERATIONS!r}"
            )
            t["estimatedIterations"] = DEFAULT_ESTIMATED_ITERATIONS
    return warnings


def _parse_proposal(
    text: str | None,
) -> tuple[dict | None, list[str], list[str], list[dict]]:
    """Turn the raw text of `plan-proposal.json` into
    `(payload, errors, warnings, repairs)`, running steps 2 and 3 in order,
    then tier normalisation (task 013) and estimatedIterations
    normalisation (task 025). Mirrors `contract_enforcement.parse_response`'s
    three-shapes shape, for the file channel instead of the inline one.
    `warnings` is always `[]` whenever `errors` is non-empty -- warnings are
    only ever produced for an already-accepted proposal.

    #209 defect 1: `repairs` (`repair_task_criteria`'s records, run BEFORE
    the lints and mutating `payload` in place) deliberately does NOT share
    that guarantee. A repair is a fact about what the harness did to the
    text, and it happened whether or not some OTHER lint then refused the
    proposal -- so the fourth element is populated on the lint-error path
    too, and only the paths where no task was ever read (absent text,
    malformed JSON, a shape refusal) return `[]`."""
    if text is None or not text.strip():
        return None, ["proposal: no plan-proposal.json produced (absent)"], [], []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, [f"proposal: malformed JSON ({exc.msg} at line {exc.lineno} column {exc.colno})"], [], []
    shape_errors = validate_proposal_shape(payload)
    if shape_errors:
        return None, shape_errors, [], []
    repairs = repair_task_criteria(payload)
    lint_warnings: list[str] = []
    lint_errors = (lint_proposal(payload, warnings=lint_warnings)
                   + lint_own_terminal_state(payload))
    if lint_errors:
        return None, lint_errors, [], repairs
    warnings = list(lint_warnings)
    warnings += normalize_task_tiers(payload)
    warnings += normalize_task_estimated_iterations(payload)
    return payload, [], warnings, repairs


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
    # #209 defect 1: one record per criteria the two lexical lints' own
    # rewrites REPAIRED on the ACCEPTED attempt (`repair_task_criteria`'s
    # `{"taskId", "oldCriteria", "newCriteria", "lints"}` shape), so a
    # caller holding `tasks_doc` can say which of its criteria the harness
    # authored rather than the hat. Never causes a re-prompt. The repairs a
    # REFUSED attempt made are not carried here -- there is no `tasks_doc`
    # for them to describe -- but they are not lost either: every one is
    # emitted as `CRITERIA_REPAIRED_EVENT` the moment it is discovered, on
    # the failed attempt as much as the accepted one (see
    # `_parse_proposal`'s own docstring on why that channel differs from
    # `warnings` directly above).
    repairs: list[dict] = field(default_factory=list)


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
            proposal, errors, warnings, repairs = _parse_proposal(proposal_text)
            # #209 defect 1: emit the moment it is discovered, the same
            # discipline the `EVENT_VIOLATION` emit below uses, and BEFORE
            # the accept/refuse branch -- a criteria the harness rewrote is
            # recorded even when some other lint then refused the same
            # proposal, so `events.jsonl` never shows a task whose criteria
            # silently changed between the hat's file and `tasks.json`.
            # `hat`/`attempt` are copied from that same emit.
            if state is not None:
                for repair in repairs:
                    state.emit(CRITERIA_REPAIRED_EVENT, hat="planning",
                               attempt=attempt + 1, source="plan-ingestion",
                               taskId=repair["taskId"],
                               oldCriteria=repair["oldCriteria"],
                               newCriteria=repair["newCriteria"],
                               lints=list(repair["lints"]))
            if not errors:
                return PlanIngestionResult(
                    ok=True, tasks_doc=ingest_proposal(proposal),
                    attempts=attempt + 1, agent_fault=False, errors=[],
                    standing_rules=pointer.get("standing_rules") or "",
                    warnings=warnings, repairs=repairs,
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
