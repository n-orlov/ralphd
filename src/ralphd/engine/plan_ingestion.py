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
# clause itself survives in a tracked file, so nothing here has to be
# reconstructed -- `tests/test_stranding_lints.py` asserts the recovered
# string is a verbatim (whitespace-flattened) substring of that tracked
# record, and only the minimal criterion frame around it is this module's.
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
# ordinary evidence citations. `tests/test_seventh_lint_task013.py` is this
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

_UNSATISFIABLE_SHAPE_CHECKS: tuple[tuple[re.Pattern, str], ...] = (
    (
        _LATER_FINAL_LAST_COMMIT_RE,
        "names a later/final/last commit or its sha, which cannot be "
        'known until that commit lands -- name "the code sha this task '
        'lands" once it exists, or a past sha this task can already read',
    ),
    (
        _PREDECLARED_EVENT_STATE_RE,
        "predeclares an event state (a petition/approach outcome) that a "
        "later event in this same run can falsify -- require the record "
        "to state whatever the TRUE outcome turns out to be, not a "
        "specific outcome named in advance",
    ),
    (
        _RECORD_CITES_OWN_WRITING_COMMIT_RE,
        "requires a record to cite the sha of the commit that writes it, "
        "which is impossible by construction (the hash is computed from "
        "the tree the commit produces) -- cite the record's own "
        "report-state sha (its commit's parent) instead, the convention "
        "`docs/sweep-records.md` already documents",
    ),
    (
        _EMPTY_GIT_RANGE_RE,
        "requires a git log/diff range to be empty -- name the specific "
        "paths and the sha the range is measured from instead of "
        "demanding an empty range as a standing invariant",
    ),
    (
        _EXACTLY_ONCE_COMMIT_COUNT_RE,
        'demands an "exactly once" commit count -- require the '
        "deliverable to land, verified by its own evidence, not a count "
        "of how many commits it took",
    ),
    (
        _NOTIFICATION_OR_HTTP_STATUS_RE,
        "requires a per-task notification ledger entry or HTTP-status "
        "record -- sends are engine-recorded and best-effort; no "
        "criterion may mention a send, a ledger entry or an HTTP status",
    ),
)


def _unsatisfiable_shape_clause(criteria: str) -> tuple[str, str] | None:
    """None unless some clause of `criteria` matches one of the six banned
    shapes above; otherwise `(clause, satisfiable_alternative_text)` for the
    FIRST shape (in the order above) that matches the FIRST clause it
    matches -- the offending clause is quoted and a satisfiable alternative
    is named, exactly like every other lint in this module."""
    for clause in _CLAUSE_SPLIT_RE.split(criteria):
        stripped = clause.strip()
        if not stripped:
            continue
        for pattern, alternative in _UNSATISFIABLE_SHAPE_CHECKS:
            if pattern.search(stripped):
                return stripped, alternative
    return None


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
#       way.
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


def _unenumerated_quantifier_clause(criteria: str) -> str | None:
    """None unless some clause carries a universal quantifier
    (every/each/all/"no ... anywhere") over a set that is neither
    enumerated nor bounded by a single named defining command -- must-catch
    fixtures: v0.16 task 023's "each shipped piece", tasks 012/013's "every
    rephrasing of X" (both recovered verbatim in
    `tests/test_criteria_bound_lint.py`); must-pass sibling: "every test in
    tests/test_x.py passes"."""
    for clause in _CLAUSE_SPLIT_RE.split(criteria):
        stripped = clause.strip()
        if not stripped:
            continue
        if not (
            _UNIVERSAL_QUANTIFIER_WORD_RE.search(stripped)
            or _NO_QUANTIFIER_ANYWHERE_RE.search(stripped)
        ):
            continue
        if not _is_bounded_quantifier_clause(stripped):
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
# `tests/test_command_form_lint_task009.py` asserts direct EQUALITY between
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


def lint_proposal(payload: dict) -> list[str]:
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
    same process's captured status -- see the
    block comments immediately above."""
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
            errors.append(
                f"task {tid!r}: successCriteria quantifies over an "
                f"unenumerated set ({quantifier_clause!r}) -- an "
                "every/each/all/\"no ... anywhere\" clause must either name "
                "every member explicitly (e.g. \"each of tasks 001, 002 and "
                "003\") or name the single defining command/file that "
                'bounds the set (e.g. "every test in tests/test_x.py '
                'passes"), never an open-ended quantifier with the members '
                "left unnamed"
            )

        overrun = _clause_count_overrun(criteria)
        if overrun is not None:
            clause_count, overflow_clause = overrun
            errors.append(
                f"task {tid!r}: successCriteria carries {clause_count} "
                f"checkable clauses, past the ~{_MAX_CHECKABLE_CLAUSES}-"
                f"clause bound (first clause past the bound: "
                f"{overflow_clause!r}) -- split this task into smaller "
                "ones, or move any wave-wide obligation this criteria "
                "repeats (a boundary check, a notification-log entry, an "
                "operator discipline) into the Standing Rules block once "
                "instead of copying it per task"
            )

        shape_match = _unsatisfiable_shape_clause(criteria)
        if shape_match is not None:
            offending_clause, alternative = shape_match
            errors.append(
                f"task {tid!r}: successCriteria is structurally "
                f"unsatisfiable ({offending_clause!r}) -- this clause "
                f"{alternative}"
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
