"""ralphd.engine.findings — the structured findings channel for the review
hat (task 001, requirement A, #110).

Today review's whole verdict is `{"verdict": "verified"}` or `{"verdict":
"rejected", "reason", "findings_path"}` (`transitions.HAT_SCHEMAS["review"]`),
with `findings_path` pointing at a free-markdown `review-findings.md`. That
markdown channel stays exactly as it is — human judgement in prose is still
the record a human reads first. This module adds a SECOND, machine-readable
channel beside it: a JSON sidecar (by convention, `review-findings.json`
next to `review-findings.md` — see `state.FINDINGS_JSON_FILE`/
`RunDir.findings_json_file`) holding a list of structured findings, each
carrying at minimum:

  - `severity` — `"blocking"` or `"residual"` (requirement A: the verdict a
    later task computes engine-side reads only the blocking set);
  - `curable` — a boolean (requirement B: would a fresh approach change this
    finding's truth value?);
  - `clause` — the PRD clause / DoD item the finding says is violated;
  - `evidence` — what was observed.

Nothing here changes `review`'s own contract payload, wires verdict
computation, or reads the file back into the loop — that is task 002
(verdict-from-blocking-only) and task 003 (the rubric default)'s scope. This
module only has to make the channel itself exist, correct and round-trippable
in isolation, exactly the same "build the layer, wire it later" split
`contracts.py`/`plan_ingestion.py` already used for tasks 009/011.

Per fact 16/#65 (`tests/test_response_contracts.py` enforces this across the
whole tree): there is exactly ONE schema-validation engine,
`ralphd.engine.contracts.validate_payload`/`validate_against`. This module
does not re-derive schema checking — `validate_finding`/`validate_findings`
below are thin callers of `contracts.validate_against`, the same pattern
`engine/router.py` already uses for its own non-hat payload shape.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ralphd.engine import contracts
from ralphd.engine.state import atomic_write_json, read_json

# The one schema a structured finding must satisfy. Deliberately flat and
# small, in the same restricted-JSON-Schema subset `contracts.py` documents
# it supports (type/properties/required/additionalProperties/enum) — no
# `items` sub-schema is needed here because each finding is validated one at
# a time by `validate_findings` below, not as a single array-typed payload.
FINDING_SCHEMA: dict = {
    "type": "object",
    "required": ["severity", "curable", "clause", "evidence"],
    "properties": {
        # Requirement A (#110): the verdict a later task computes reads
        # only findings whose severity is "blocking" — "residual" findings
        # are still recorded and surfaced, never dropped.
        "severity": {"type": "string", "enum": ["blocking", "residual"]},
        # Requirement B (#111): would a fresh approach change this
        # finding's truth value? Asserted by review only (no run-side
        # input reaches it) — this module only carries the bit, it does
        # not decide it.
        "curable": {"type": "boolean"},
        # The PRD clause / DoD item this finding says is violated.
        "clause": {"type": "string"},
        # What was observed — the concrete evidence behind the finding.
        "evidence": {"type": "string"},
        # Task 019 (requirement D.4, #125): which independent judging hat
        # produced this finding -- e.g. `"review"` or `"verdict_gate"`.
        # Deliberately OPTIONAL here (existing findings written before this
        # task, and every fixture in `tests/test_structured_findings.py`/
        # `tests/test_review_verdict_findings.py`, carry no such field and
        # must keep validating unchanged); `attribute_authorship` below is
        # the one place that actually FILLS it in, and it does so from the
        # engine's own knowledge of which fixed channel a findings list
        # came from, never from a value the hat itself wrote -- the same
        # "asserted by exactly one party" discipline `curable` documents
        # for itself, just with the engine as that one party instead of
        # the hat, because which channel a file IS is an engine-side fact,
        # not a judgement call.
        "author": {"type": "string"},
    },
    "additionalProperties": False,
}


# A private sentinel distinguishable from any real `read_json` default
# (including `None`, which a hand-edited file could legitimately contain at
# the top level and which must still be reported as invalid, not "absent").
_MISSING = object()


def validate_finding(payload: Any) -> list[str]:
    """Validate a single finding against `FINDING_SCHEMA`, through the one
    schema engine (`contracts.validate_against`). Returns a list of human
    readable error strings; empty means the finding is valid."""
    return contracts.validate_against(FINDING_SCHEMA, payload)


def validate_findings(payloads: Any) -> list[str]:
    """Validate a whole findings list: every element must independently
    satisfy `FINDING_SCHEMA`. A non-list/tuple top level is reported once,
    at `$`, the same shape `contracts.validate_payload` uses for a
    non-object top-level payload."""
    if not isinstance(payloads, (list, tuple)):
        return [f"$: expected array of findings, got {type(payloads).__name__}"]
    errors: list[str] = []
    for index, item in enumerate(payloads):
        for err in validate_finding(item):
            # Every error from `contracts._validate_against` starts with
            # the path it was called at ("$"); reattach the finding's own
            # index so a caller can tell which element failed.
            errors.append(f"$[{index}]{err[1:]}" if err.startswith("$")
                          else f"$[{index}]: {err}")
    return errors


def write_findings_json(path: Path, findings: list[dict]) -> None:
    """Write `findings` to `path` as the machine-readable sidecar, after
    validating every finding through the one schema engine. Raises
    `ValueError` — never writes a partially-invalid channel — when any
    finding is malformed; the caller (a future task's engine wiring) is
    expected to treat that the same way any other contract violation is
    treated, not to catch and ignore it."""
    errors = validate_findings(findings)
    if errors:
        raise ValueError(
            "refusing to write an invalid findings channel: " + "; ".join(errors))
    atomic_write_json(path, list(findings))


def attribute_authorship(items: list[dict], author: str) -> list[dict]:
    """Task 019 (requirement D.4, #125): return a COPY of `items` with
    every entry's `author` field set to `author`, unconditionally --
    overwriting whatever value (if any) the entry already carried.

    This is deliberately an overwrite, not a fill-only-if-absent: `author`
    records which of the run's independent judging hats produced a
    finding, and the ENGINE already knows that unambiguously from which
    fixed-path channel (`RunDir.findings_json_file` vs `RunDir.
    verdict_gate_findings_json_file`) the list was read from -- the same
    reason `_review_findings_channel`'s `duplicate_petition_findings`
    append is unconditional rather than "only if review didn't already
    say so": a fact the engine can derive on its own is never left to a
    hat's own (possibly wrong, possibly absent) say-so. Callers pass the
    channel-appropriate literal (`"review"` or `"verdict_gate"`), never a
    value read out of the payload itself.

    Never mutates the dicts in `items` -- each entry is shallow-copied
    first, so a caller holding a reference to the original list (as
    `_review_findings_channel`'s callers do) is unaffected.
    """
    return [dict(item, author=author) for item in items]


def flagged_criteria_findings(tasks: list[dict]) -> list[dict]:
    """Task 024 (requirement E.3, #116 section 2): one engine-detected
    `severity: "blocking"` finding, shaped exactly like `FINDING_SCHEMA`,
    per task carrying a truthy `criteriaEditedAfterValidationFailure`
    marker (`engine.loop.LoopSupervisor._check_criteria_edits`, task 008)
    -- the same unconditional-append discipline `engine.petitions.
    duplicate_petition_findings` already uses for its own structurally
    detected violation.

    A worker/planner rewrite of a task's own `successCriteria` has no
    authority: the ONE legal amendment path is the judge-authored one, on
    a CONFIRMED petition, through `engine.petitions.criteria_amendment_
    candidates` / `LoopSupervisor._apply_criteria_amendments` (task 006,
    requirement B.1/B.2, #114). Before this task, that fact was only
    SURFACED to the review model as a prompt instruction
    (`LoopSupervisor._flagged_criteria_review_context`, task 009) --
    review had to voluntarily obey it. This closes the engine-side gap:
    the finding this function returns is appended to the SAME findings
    list `LoopSupervisor._review_findings_channel` hands every caller, so
    a review payload claiming `verdict: "verified"` over a flagged task
    now contradicts its own (engine-augmented) findings channel and is
    refused by the identical `transitions.review_verdict_findings_errors`
    contradiction check every other engine-detected finding already rides
    -- never a new, parallel enforcement path.

    `curable` is `True`: nothing about the underlying fact (an
    unauthorized rewrite once happened) is incurable in the sense
    requirement B is about -- a fresh approach that never edits the
    criteria again does not repeat the violation. The
    `criteriaEditedAfterValidationFailure` marker itself is never cleared
    once set (`_check_criteria_edits`'s own documented contract), so a
    task that was ever tampered with keeps producing this finding on
    every later review pass too -- a disclosed, deliberately conservative
    consequence of that pre-existing "once set, never cleared" design,
    not something this function changes.
    """
    out = []
    for t in tasks:
        if not t.get("criteriaEditedAfterValidationFailure"):
            continue
        out.append({
            "severity": "blocking",
            "curable": True,
            "clause": (f"task {t.get('id')} successCriteria authority "
                       "(#116 section 2)"),
            "evidence": (
                f"task {t.get('id')}'s successCriteria was rewritten by a "
                "worker or planner with no amendment authority -- the ONE "
                "legal path is a judge-authored amendment on a CONFIRMED "
                "petition (docs/petition-format.md); current text: "
                f"{t.get('successCriteria', '')!r}"),
        })
    return out


def compute_verdict(findings: list[dict]) -> str:
    """Task 002 (requirement A.2, #110): the verdict the ENGINE computes
    from a findings list's severities ALONE -- `"rejected"` iff at least
    one finding's `severity` is `"blocking"`, `"verified"` otherwise
    (an all-residual list, same as an empty one, computes `"verified"`).
    This is the ONE place that rule lives; `engine/loop.py`'s
    `_review_verdict`/`_review_contract_ladder` call this rather than
    re-deriving the same `any(... == "blocking" ...)` check inline, so a
    later change to what counts as blocking only has to change here.

    Deliberately takes an already-validated findings list (the caller's
    job, via `read_findings_json`/`validate_findings`) -- this function
    itself does no schema checking; a malformed element's `.get("severity")`
    simply reads `None`, which is never `"blocking"`, so a caller that
    (incorrectly) fed it unvalidated data fails safe towards `"verified"`
    rather than crashing -- exactly why callers are expected to validate
    first, not a licence to skip it.
    """
    return "rejected" if any(
        f.get("severity") == "blocking" for f in findings) else "verified"


def all_blocking_findings_incurable(findings: list[dict]) -> bool:
    """Task 005 (requirement B, #111): are this rejection's BLOCKING
    findings ALL incurable? The one place this question is answered --
    a later task's `rejected_incurable` edge calls this rather than
    re-deriving the same scan inline.

    Reads ONLY the `severity`/`curable` fields of the `findings` list the
    caller hands it -- which itself came from `read_findings_json`/
    `_review_findings_channel` (review's own structured findings channel,
    `review-findings.json`, written by `write_findings_json`). Curability
    is asserted by REVIEW ONLY (PRD requirement B.4): this function takes
    NO other argument -- no run directory, no workspace path, no
    worker-writable file -- so there is no second input path through
    which anything on the run side (the worker's own writable workspace
    included) could reach the answer; the only way to change it is for
    review itself to write a different `curable` bit into the findings
    channel it alone controls.

    Residual findings never enter the scan -- only `severity: "blocking"`
    entries are inspected, so a residual finding's `curable` value
    (whatever it is set to) is irrelevant to the answer, exactly as it is
    irrelevant to `compute_verdict` above.

    An empty blocking set answers `True` (vacuously "all incurable" -- the
    same shape `all()` always gives an empty iterable), but that is not a
    licence to route on this answer alone: a caller must first confirm
    `compute_verdict(findings) == "rejected"` (i.e. that a blocking finding
    actually exists) before treating `True` here as "take the incurable
    edge" -- this function only answers the curability question once a
    rejection is already established, same division of labour
    `compute_verdict` documents for itself.

    Like `compute_verdict`, this function does no schema checking of its
    own -- a malformed finding missing `curable` (should never happen past
    `validate_findings`) reads as curable (only an explicit `curable is
    False` counts as incurable), which fails towards the ordinary replan
    path rather than towards pausing the run, the same
    fail-towards-today's-behaviour posture non-negotiable #2 already
    established for `compute_verdict`'s own None-safety.
    """
    blocking = [f for f in findings if f.get("severity") == "blocking"]
    return all(f.get("curable") is False for f in blocking)


def read_findings_json(path: Path) -> list[dict]:
    """Read the machine-readable sidecar back from `path`, re-validating
    every finding through the same schema engine on the way out — a file
    that was valid when written but corrupted or hand-edited since is
    caught here rather than trusted silently. Raises `ValueError` on a
    missing/malformed file or an invalid finding."""
    parsed = read_json(path, default=_MISSING)
    if parsed is _MISSING:
        raise ValueError(f"no findings channel at {path}")
    errors = validate_findings(parsed)
    if errors:
        raise ValueError(
            f"invalid findings channel at {path}: " + "; ".join(errors))
    return parsed


# ---------------------------------------------------------------------------
# Task 003 (requirement A.3, #110): the materiality rubric comes from the
# contract; the engine's own default when a contract supplies none is
# today's behaviour, unchanged -- every finding is blocking. Non-negotiable
# #2 names this default explicitly as something never to weaken.
# ---------------------------------------------------------------------------

# A markdown heading (any level "#" through "######") whose text is
# "materiality rubric", optionally followed by parenthesised commentary --
# exactly the shape this PRD's own `### Materiality rubric (review: compute
# your verdict from blocking findings only)` heading has. Matched
# case-insensitively so a future contract's own capitalisation choice still
# surfaces.
_RUBRIC_HEADING_RE = re.compile(
    r"^(#{1,6})[ \t]+materiality rubric\b.*$", re.IGNORECASE | re.MULTILINE)

# The explicit, quotable statement of the default this module falls back to
# when `extract_materiality_rubric` finds nothing -- reused verbatim by
# `materiality_rubric_context` below and by the pinning test, so the two
# never drift apart.
ALL_BLOCKING_DEFAULT_NOTICE = (
    "This contract supplies no materiality rubric. Default: every finding "
    "is blocking (today's behaviour, unchanged)."
)


def extract_materiality_rubric(contract_text: str) -> str | None:
    """Find the PRD's own materiality-rubric section inside `contract_text`
    (the run's PRD/composite-PRD text, i.e. "the contract") and return it
    verbatim -- heading line included, body up to (but not including) the
    next heading of equal or higher level, or end of text. Returns `None`
    when no such heading exists, which is the ordinary case for a contract
    written before this convention existed; the caller
    (`materiality_rubric_context`) is the one place that turns `None` into
    the explicit all-blocking default -- this function itself makes no
    default decision, it only answers "is one there".

    A restricted heading match (not a generic keyword search): the rubric
    is a *section* review must read as a whole, not a sentence that happens
    to mention the phrase, so only a line that IS a markdown heading titled
    (near enough) "materiality rubric" opens the section.
    """
    match = _RUBRIC_HEADING_RE.search(contract_text or "")
    if match is None:
        return None
    level = len(match.group(1))
    start = match.start()
    # The next heading of equal-or-higher level (fewer or equal "#"s) ends
    # the section; a deeper heading (more "#"s, a subsection) stays inside
    # it.
    next_heading = re.compile(
        r"^#{1," + str(level) + r"}[ \t]+\S.*$", re.MULTILINE)
    tail = next_heading.search(contract_text, match.end())
    end = tail.start() if tail else len(contract_text)
    return contract_text[start:end].strip()


def materiality_rubric_context(contract_text: str) -> str:
    """Render the review-prompt context block that hands review the
    contract's own materiality rubric -- or, when the contract supplies
    none, the explicit all-blocking default (`ALL_BLOCKING_DEFAULT_NOTICE`)
    instead. Requirement A.3 (#110): "review reads the PRD's materiality
    rubric; the engine defaults to all-blocking when none is present" --
    this is that hand-off, always present so review is never left to guess
    which case it is in.
    """
    rubric = extract_materiality_rubric(contract_text)
    lines = ["\n## Materiality rubric (task 003, requirement A.3, #110)\n"]
    if rubric is None:
        lines.append(f"\n{ALL_BLOCKING_DEFAULT_NOTICE}\n")
    else:
        lines.append(
            "\nThe contract below is this run's own materiality rubric "
            "(quoted verbatim from the PRD/composite PRD). Use it -- not "
            "any other source -- to decide each finding's `severity`.\n")
        lines.append(f"\n{rubric}\n")
    return "".join(lines)
