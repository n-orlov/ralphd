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

Task 031 (PRD requirement E.2, #171) adds two more, both optional at the
schema level: `cured_when` (a curable finding's own cure specification --
required, as a BUSINESS rule checked by `curable_findings_missing_cured_
when`/`transitions.review_cured_when_errors` rather than by the schema
itself, whenever `curable` is `True`) and `invalidates` (zero or more
in-checkout paths the finding's cure invalidates, always optional).

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
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ralphd.engine import contracts
from ralphd.engine.state import atomic_write_json, read_json


class UnrecognisedFindingFieldWarning(UserWarning):
    """Task 014 (requirement E.1, #190): the warning category validation
    raises for a finding carrying a key `FINDING_SCHEMA` does not declare.
    A distinct category (rather than a bare `UserWarning`) so a caller --
    or a test -- can filter for exactly this one, and so the fact that an
    unrecognised key was TOLERATED is never confused with a validation
    error: the finding is still valid, the warning is the record that a
    key was ignored."""


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
        # Task 031 (PRD requirement E.2, #171): the SPECIFICATION a curable
        # finding is -- required (by `curable_findings_missing_cured_when`
        # below, a business rule, not this schema, because "required only
        # when curable is True" is a conditional this restricted schema
        # subset cannot express) whenever `curable` is `True`; optional and
        # ignored otherwise. `state.cure_task_from_finding` uses this text
        # verbatim as the cure task's `successCriteria` once present.
        "cured_when": {"type": "string"},
        # Task 031 (PRD requirement E.2, #171): zero or more in-checkout
        # paths this finding's cure invalidates -- a later task (#167's
        # requirement E.3) reads this to insert a re-take task per path.
        # Deliberately optional on every finding, curable or not.
        "invalidates": {"type": "array"},
    },
    # Task 014 (requirement E.1, #190): deliberately True, not False.
    # `additionalProperties: False` used to make an unrecognised extra key
    # (e.g. a workspace on a newer engine version writing a field this
    # engine's own schema does not yet know) fail the WHOLE finding, which
    # in turn failed `validate_findings`/`read_findings_json` for a
    # findings list that was otherwise perfectly valid -- "the findings
    # channel self-locking the next wave" the section header names. An
    # unrecognised key is now tolerated and ignored by the schema itself;
    # `unexpected_finding_fields`/`finding_warnings` below still name it,
    # via a WARNING rather than a validation error, so nothing about the
    # key is silently lost -- it is surfaced, just not treated as fatal.
    # Every field actually listed in `required` above is still required:
    # this change adds no newly-required field and removes none.
    "additionalProperties": True,
}


# A private sentinel distinguishable from any real `read_json` default
# (including `None`, which a hand-edited file could legitimately contain at
# the top level and which must still be reported as invalid, not "absent").
_MISSING = object()


def validate_finding(payload: Any) -> list[str]:
    """Validate a single finding against `FINDING_SCHEMA`, through the one
    schema engine (`contracts.validate_against`). Returns a list of human
    readable error strings; empty means the finding is valid.

    Task 014 (requirement E.1, #190): a finding carrying one or more keys
    `FINDING_SCHEMA` does not declare is VALID (this returns `[]` for it,
    same as a finding with no extra keys at all) -- `additionalProperties`
    is `True` on the schema itself now. Every field the schema's `required`
    list actually names is still required and still fails validation when
    missing; nothing about that half changed.

    Validation is also where the "ignored, but NAMED" half of E.1 is
    delivered: every unrecognised key `finding_warnings` reports is
    surfaced here as it is tolerated -- as EXACTLY ONE
    `UnrecognisedFindingFieldWarning` per key, through the stdlib
    `warnings` machinery and nothing else. One key means one emitted
    warning: this deliberately does NOT also log the same text through a
    module logger (an earlier revision did, and "one warning that names
    it" then arrived twice -- two stderr lines for one unrecognised key).
    A process that wants these in its log routes them there once, with
    `logging.captureWarnings(True)`, rather than having validation emit
    the same fact on two channels. Callers that only want the text
    without emitting anything call `finding_warnings` directly -- that
    function stays side-effect free."""
    for message in finding_warnings(payload):
        warnings.warn(message, UnrecognisedFindingFieldWarning, stacklevel=2)
    return contracts.validate_against(FINDING_SCHEMA, payload)


def unexpected_finding_fields(payload: Any) -> list[str]:
    """Task 014 (requirement E.1, #190): the sorted list of keys in
    `payload` that `FINDING_SCHEMA["properties"]` does not declare -- the
    same set `additionalProperties: False` used to reject outright, now
    only reported, never itself a validation error. A non-dict payload
    (already reported as a type error by `validate_finding`) has no keys
    to report, so this returns `[]` for one rather than raising."""
    if not isinstance(payload, dict):
        return []
    return sorted(set(payload) - set(FINDING_SCHEMA["properties"]))


def finding_warnings(payload: Any) -> list[str]:
    """Task 014 (requirement E.1, #190): one human-readable warning per
    unrecognised key `unexpected_finding_fields` finds on `payload`, each
    naming the key by name -- the "ignored, but named" half of E.1. Empty
    when the finding carries no key outside `FINDING_SCHEMA`'s own
    properties.

    Pure: this only builds the text. `validate_finding` above is the one
    place that EMITS it, as one `UnrecognisedFindingFieldWarning` per key
    and on no second channel, so every validation path -- `validate_
    findings`, `write_findings_json`, `read_findings_json` -- warns
    exactly once per key without any of them re-deriving the wording."""
    return [
        f"finding carries unrecognised field {key!r}; ignoring it, not "
        "rejecting the finding"
        for key in unexpected_finding_fields(payload)
    ]


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


# Task 039 (operator steer 002): the ONE literal that identifies a
# criteria-tamper finding as the engine's own. `flagged_criteria_findings`
# below builds every `clause` it emits from this marker, and
# `any_criteria_tamper_finding` recognises one by it -- so the producer and
# the recogniser can never drift apart into two hand-typed copies of the
# same wording.
CRITERIA_TAMPER_CLAUSE_MARKER = "successCriteria authority (#116 section 2)"


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
    list `LoopSupervisor._review_findings_channel` hands every caller (and
    persisted into that same channel on disk by `_record_flagged_criteria_
    findings`, so the blocking finding is a record and not just a return
    value), so a review payload claiming `verdict: "verified"` over a
    flagged task now contradicts its own (engine-augmented) findings
    channel and is refused by the identical `transitions.review_verdict_
    findings_errors` contradiction check every other engine-detected
    finding already rides -- never a new, parallel enforcement path.

    The rewrite ITSELF is undone at detection time, not here:
    `_check_criteria_edits` restores `successCriteria` from the verbatim
    `criteriaOfRecord` copy the engine keeps beside the fingerprint. This
    function is the record of the fact that it happened, which is why the
    marker it reads is deliberately sticky.

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

    Task 031 (PRD requirement E.2, #171) adds `cured_when`: every curable
    blocking finding now needs one, engine-authored findings included --
    there is no carve-out in the rule for a finding review never wrote
    itself. This one's specification names the authorized escape hatch
    already documented above: the ONE legal amendment path, a
    judge-authored replacement on a CONFIRMED petition, never a second
    unauthorized rewrite.
    """
    out = []
    for t in tasks:
        if not t.get("criteriaEditedAfterValidationFailure"):
            continue
        out.append({
            "severity": "blocking",
            "curable": True,
            "clause": (f"task {t.get('id')} "
                       f"{CRITERIA_TAMPER_CLAUSE_MARKER}"),
            "evidence": (
                f"task {t.get('id')}'s successCriteria was rewritten by a "
                "worker or planner with no amendment authority -- the ONE "
                "legal path is a judge-authored amendment on a CONFIRMED "
                "petition (docs/petition-format.md); current text: "
                f"{t.get('successCriteria', '')!r}"),
            "cured_when": (
                f"task {t.get('id')}'s successCriteria is replaced only "
                "through a judge-authored amendment on a CONFIRMED "
                "petition (docs/petition-format.md), never by a further "
                "unauthorized worker or planner rewrite"),
        })
    return out


def any_criteria_tamper_finding(findings: list[dict]) -> bool:
    """Task 039 (operator steer 002): does this findings list carry at least
    one criteria-tamper finding -- the engine-authored finding
    `flagged_criteria_findings` emits for a task whose `successCriteria` a
    worker or planner rewrote with no amendment authority?

    Recognised by `CRITERIA_TAMPER_CLAUSE_MARKER` appearing in a finding's
    `clause`, the same literal the producer above builds its clause from.

    Deliberately severity-BLIND, unlike `compute_verdict`/`all_blocking_
    findings_curable`: a caller asks this question in order to withhold a
    remedy (the cure pass, see `engine.loop.LoopSupervisor._review_cure_
    pass_available`), so the safe direction is to answer `True` whenever the
    fact appears at all -- including a copy that some other rule has
    downgraded to `"residual"`. Answering only for `"blocking"` copies would
    make a downgrade a way to buy back the cure pass over a tampered
    approach, which is exactly the authority boundary #116 section 2 exists
    to hold.
    """
    return any(
        CRITERIA_TAMPER_CLAUSE_MARKER in (f.get("clause") or "")
        for f in findings)


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


def has_residual_findings(findings: list[dict]) -> bool:
    """Task 012 (requirement D.3): does `findings` carry at least one
    `severity: "residual"` entry? The one place that question is answered
    -- `engine/loop.py`'s success-verdict labelling (`_review_success_
    verdict_label`) calls this rather than re-deriving the same scan
    inline, the same discipline `compute_verdict`/`all_blocking_findings_
    incurable` already document for themselves.

    Deliberately does not care about `severity: "blocking"` entries at
    all -- a caller must already know the list computes `"verified"`
    (via `compute_verdict`) before this answer means anything; a
    blocking-and-residual mix is `"rejected"` and never reaches the
    caller this function serves in the first place.
    """
    return any(f.get("severity") == "residual" for f in findings)


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


def all_blocking_findings_curable(findings: list[dict]) -> bool:
    """Task 009 (PRD requirement C.1, #141): are this rejection's BLOCKING
    findings ALL curable? The strict complement of
    `all_blocking_findings_incurable` above -- that function asks "is
    every blocking finding's `curable` explicitly `False`", this one asks
    "is every blocking finding's `curable` explicitly `True`" -- the two
    are not simply negations of each other precisely because a malformed
    or absent `curable` value must fail towards the SAME default both
    ways: towards the ordinary replan path, never towards a run-changing
    edge (a pause on this function's caller's opposite number, a cure
    pass here) on a finding whose curability was never actually asserted.
    A finding missing `curable` (should never happen past `validate_
    findings`) therefore reads as NOT curable here (only an explicit
    `curable is True` counts), the mirror image of how it reads as
    curable (not incurable) in `all_blocking_findings_incurable`.

    Reads ONLY `severity`/`curable`, exactly like `all_blocking_findings_
    incurable` -- curability stays asserted by review alone, no run-side
    input reaches this function either. Residual findings never enter the
    scan. An empty blocking set answers `True` (vacuously "all curable",
    the same shape `all()` gives any empty iterable) -- same caveat as
    `all_blocking_findings_incurable`'s own empty-set case: a caller must
    first confirm `compute_verdict(findings) == "rejected"` before acting
    on this answer.
    """
    blocking = [f for f in findings if f.get("severity") == "blocking"]
    return all(f.get("curable") is True for f in blocking)


def blocking_finding_clauses(findings: list[dict]) -> set[str]:
    """Task 030 (PRD requirement E.1, #167): the set of `clause` values
    carried by this findings list's BLOCKING entries only -- residual
    findings never enter the set, the same blocking-only scan every other
    function in this module already restricts itself to. This is the
    comparison unit `LoopSupervisor._review_cure_pass_available` uses to
    decide whether a SECOND (or third) rejection in the same approach is
    a superset of the immediately preceding cure pass's own blocking set
    -- "the same wall, unmoved" -- or a genuinely smaller/different set
    that deserves another cure pass. A missing/non-string `clause` on an
    otherwise-blocking entry (should never happen past `validate_
    findings`) is dropped from the set rather than raising, so a malformed
    entry fails towards "looks like progress" (a smaller set, i.e. the
    cure pass is granted) rather than towards silently refusing a budget
    the PRD says is owed.
    """
    blocking = [f for f in findings if f.get("severity") == "blocking"]
    return {f["clause"] for f in blocking if isinstance(f.get("clause"), str)}


def curable_findings_missing_cured_when(findings: list[dict]) -> list[dict]:
    """Task 031 (PRD requirement E.2, #171): every BLOCKING, curable
    (`curable is True`) finding in `findings` whose `cured_when` is
    missing, non-string or blank -- the set `transitions.review_cured_
    when_errors` reports as a review contract violation. `cured_when` is
    the specification `state.cure_task_from_finding` uses as a cure
    task's own `successCriteria` once a cure pass is granted
    (`_review_cure_pass_available` already requires `all_blocking_
    findings_curable` before a pass is ever taken), so a curable blocking
    finding reaching that point with no `cured_when` would otherwise leave
    the cure task with nothing but the old clause+evidence fallback --
    exactly the gap this business rule closes at review time instead, the
    same review-time-not-cure-time posture `all_blocking_findings_curable`
    already keeps for the sibling `curable` bit.

    A residual finding, or a blocking finding whose `curable` is not
    explicitly `True`, never enters the scan -- the same restriction
    `all_blocking_findings_curable` places on itself, so this function
    only ever asks the question of a finding a cure pass could actually
    act on.
    """
    out = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        if f.get("severity") != "blocking" or f.get("curable") is not True:
            continue
        cured_when = f.get("cured_when")
        if not isinstance(cured_when, str) or not cured_when.strip():
            out.append(f)
    return out


def curable_findings_with_overrun_cured_when(
        findings: list[dict]) -> list[tuple[dict, tuple[int, str]]]:
    """Task 031 (PRD requirement E.2, #171): every BLOCKING, curable
    finding in `findings` whose OWN `cured_when` text overruns the plan's
    bounded-checkable-clause lint (`plan_ingestion.clause_count_overrun`,
    the exact same ~5-clause bound `lint_proposal` holds a task's own
    `successCriteria` to), paired with that overrun's own `(clause_count,
    overflow_clause)` result -- `cured_when` BECOMES a cure task's
    `successCriteria` (`state.cure_task_from_finding`), so it is held to
    the identical bound, never a second, looser one. A finding missing
    `cured_when` entirely is not reported here -- that is `curable_
    findings_missing_cured_when`'s own question; this function only scans
    findings that DO carry one.
    """
    from ralphd.engine import plan_ingestion
    out = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        if f.get("severity") != "blocking" or f.get("curable") is not True:
            continue
        cured_when = f.get("cured_when")
        if not isinstance(cured_when, str) or not cured_when.strip():
            continue
        overrun = plan_ingestion.clause_count_overrun(cured_when)
        if overrun is not None:
            out.append((f, overrun))
    return out


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
# Task 003/004 (requirements A.3/A.2, #110): the materiality rubric comes
# from the contract; when a contract supplies none, the engine's own
# default is no longer "every finding is blocking" -- it is this fragment's
# own product-blocking/residual boundary (`docs/review-economics.md`'s
# "## Materiality rubric" section), kept here as the single copy in the
# source tree so review always has a real boundary to classify against,
# never a placeholder that collapses every finding to blocking.
# ---------------------------------------------------------------------------

# A markdown heading (any level "#" through "######") whose text is
# "materiality rubric", optionally followed by parenthesised commentary --
# exactly the shape this PRD's own `### Materiality rubric (review: compute
# your verdict from blocking findings only)` heading has. Matched
# case-insensitively so a future contract's own capitalisation choice still
# surfaces.
_RUBRIC_HEADING_RE = re.compile(
    r"^(#{1,6})[ \t]+materiality rubric\b.*$", re.IGNORECASE | re.MULTILINE)

# The explicit, quotable statement handed to review whenever no rubric is
# found -- reused verbatim by `materiality_rubric_context` below, by
# `LoopSupervisor._review_materiality_rubric_context`, and by the pinning
# test, so the three never drift apart.
NO_RUBRIC_NOTICE = "no rubric supplied; the engine default applies"

# The engine's own default materiality rubric (task 004, requirement A.2,
# #139): the product-blocking/residual boundary text of
# `docs/review-economics.md`'s "## Materiality rubric" section, quoted here
# as the single copy in the source tree. This is a REAL rubric -- review
# still classifies each finding blocking or residual against it -- not the
# old collapse-everything-to-blocking placeholder the engine used to fall
# back to (removed by task 004). A citing PRD may
# still narrow this boundary explicitly (an inline `## Materiality rubric`
# heading, or a reference resolved by `resolve_materiality_rubric` below);
# this constant is only what applies when a contract supplies neither.
DEFAULT_RUBRIC = (
    "## Materiality rubric\n"
    "\n"
    "Review computes its verdict from **blocking** findings only; "
    "**residual** findings are recorded and surfaced but never reject.\n"
    "\n"
    "**Blocking** \u2014 any of: a red test at the shipped sha; an unmet "
    "requirement of the contract; a false claim in a tracked artifact "
    "that changes what a reader believes landed or its result (a wrong "
    "commit attributed for a change that shipped differently than "
    "described, a test result misstated, an issue state misreported, an "
    "invented notification); a missing required declaration (e.g. a "
    "sweep's `sweep-sha`); an issue closed without evidence; a secret "
    "exposed; a stated Definition-of-done predicate evaluating false.\n"
    "\n"
    "**Residual** \u2014 everything else, including: imprecision or "
    "infelicity in prose; a stale snapshot that is labelled as a "
    "snapshot; a missing cross-reference; formatting; an inaccuracy in a "
    "record *about another record* whose correction would not change "
    "what a reader believes shipped; a notification gap that has been "
    "cured under the late-cure rule.\n"
)


def extract_materiality_rubric(contract_text: str) -> str | None:
    """Find the PRD's own materiality-rubric section inside `contract_text`
    (the run's PRD/composite-PRD text, i.e. "the contract") and return it
    verbatim -- heading line included, body up to (but not including) the
    next heading of equal or higher level, or end of text. Returns `None`
    when no such heading exists, which is the ordinary case for a contract
    written before this convention existed; the caller
    (`materiality_rubric_context`) is the one place that turns `None` into
    the engine's own default rubric (`DEFAULT_RUBRIC`) -- this function
    itself makes no default decision, it only answers "is one there".

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
    none, the engine's own default rubric (`DEFAULT_RUBRIC`) plus the
    explicit `NO_RUBRIC_NOTICE` sentence, so review is told which case it
    is in and still receives a real blocking/residual boundary to classify
    findings against. Requirement A.3/A.2 (#110/#139): "review reads the
    PRD's materiality rubric; the engine defaults to its own
    product-blocking/residual boundary when none is present" -- this is
    that hand-off, always present so review is never left to guess.
    """
    rubric = extract_materiality_rubric(contract_text)
    lines = ["\n## Materiality rubric (task 003, requirement A.3, #110)\n"]
    if rubric is None:
        lines.append(f"\n{NO_RUBRIC_NOTICE}\n\n{DEFAULT_RUBRIC}\n")
    else:
        lines.append(
            "\nThe contract below is this run's own materiality rubric "
            "(quoted verbatim from the PRD/composite PRD). Use it -- not "
            "any other source -- to decide each finding's `severity`.\n")
        lines.append(f"\n{rubric}\n")
    return "".join(lines)


# ---------------------------------------------------------------------------
# Task 002 (requirement A.1, #110), resolver half: a contract need not carry
# its materiality rubric inline -- it may instead ADOPT ONE BY REFERENCE,
# naming another in-checkout document (typically `docs/review-economics.md`,
# the reusable fragment) whose own `## Materiality rubric` section applies
# here. Task 003 (wiring half) is the one that makes
# `LoopSupervisor._review_materiality_rubric_context` call the resolver
# below instead of `extract_materiality_rubric` directly; this module only
# has to make the resolution itself exist, correct and self-contained.
#
# Two reference shapes are recognised, both requiring a backticked path:
#   - a PROSE line: "...the materiality rubric ... of `<path>` applies..."
#     or "...apply..." (both verb forms accepted -- review finding 1,
#     approach 02 task 001: a contract subject may be singular "rubric"
#     or plural, so either "apply" or "applies" must resolve), case-
#     insensitive, one line -- `.` does not cross a newline by
#     default, which is exactly the restriction a *sentence* needs);
#   - an explicit HTML-comment MARKER: `<!-- rubric: <path> -->`, for a
#     contract that would rather not rely on prose-matching at all.
# An INLINE `## Materiality rubric` heading in the contract text always
# wins over either reference shape -- `resolve_materiality_rubric` checks
# `extract_materiality_rubric` first and only falls through to reference
# resolution when that returns `None`.
# ---------------------------------------------------------------------------

_RUBRIC_MARKER_RE = re.compile(
    r"<!--\s*rubric:\s*(\S+?)\s*-->", re.IGNORECASE)

_RUBRIC_PROSE_REFERENCE_RE = re.compile(
    r"the materiality rubric\b.{0,200}?\bof\s+`([^`]+)`.{0,200}?\bappl(?:y|ies)\b",
    re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class RubricResolution:
    """The result of `resolve_materiality_rubric` below.

    `rubric` is the resolved section text (verbatim, heading included) or
    `None` when the contract supplies no rubric at all -- inline or by
    reference -- or names a reference this checkout cannot resolve.
    `source_path` is the reference string as it appeared in the contract
    (e.g. `"docs/review-economics.md"`) when -- and only when -- the rubric
    text came from a resolved reference; it stays `None` for an inline
    rubric and for the no-rubric case alike, so a caller can tell "this text
    is the contract's own" apart from "this text was adopted from
    elsewhere" without re-parsing anything. `log_events` carries zero or
    more dicts shaped as `run.emit("log", **event)` kwargs (`level`,
    `message`) -- this module has no `RunDir` to emit through itself, so it
    hands the event back for the caller (task 003's engine call site) to
    emit; the ordinary case (no reference, or a reference that resolved) is
    an empty tuple.
    """

    rubric: str | None
    source_path: str | None = None
    log_events: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def _find_rubric_reference(contract_text: str) -> str | None:
    """Return the referenced path named by either recognised reference
    shape (marker checked first, purely because it is the more explicit,
    less ambiguous of the two -- both are equally authoritative), or
    `None` when neither appears."""
    marker = _RUBRIC_MARKER_RE.search(contract_text or "")
    if marker is not None:
        return marker.group(1).strip()
    prose = _RUBRIC_PROSE_REFERENCE_RE.search(contract_text or "")
    if prose is not None:
        return prose.group(1).strip()
    return None


def resolve_materiality_rubric(
        contract_text: str, workspace_root: str | Path) -> RubricResolution:
    """Resolve THE materiality rubric that applies to `contract_text`,
    inline or by reference, resolving any referenced path against
    `workspace_root` (the checkout root).

    Precedence, in order:
      1. an inline `## Materiality rubric` heading in `contract_text` --
         via `extract_materiality_rubric`, unchanged -- always wins;
      2. failing that, a reference (prose or marker, see above) naming
         another in-checkout document, whose own `## Materiality rubric`
         section (again via `extract_materiality_rubric`, run against the
         REFERENCED document's text) is inlined here;
      3. failing that -- no inline heading and no reference, or a
         reference naming a path that does not resolve to a readable file
         under `workspace_root`, or that resolves but itself carries no
         rubric heading -- the no-rubric result (`rubric=None`), same as
         `extract_materiality_rubric` returning `None` today. A reference
         that fails to resolve additionally carries one `log` event naming
         the unresolved path, so the run's own event log records *why*
         review ends up with no rubric rather than leaving that silent.
    """
    inline = extract_materiality_rubric(contract_text)
    if inline is not None:
        return RubricResolution(rubric=inline)

    reference = _find_rubric_reference(contract_text)
    if reference is None:
        return RubricResolution(rubric=None)

    root = Path(workspace_root)
    candidate = root / reference
    try:
        referenced_text = candidate.read_text()
    except OSError:
        return RubricResolution(
            rubric=None,
            log_events=(
                {
                    "level": "warning",
                    "message": (
                        "materiality rubric reference "
                        f"{reference!r} did not resolve to a readable "
                        f"file under {root}"
                    ),
                },
            ),
        )

    resolved = extract_materiality_rubric(referenced_text)
    if resolved is None:
        return RubricResolution(
            rubric=None,
            log_events=(
                {
                    "level": "warning",
                    "message": (
                        f"materiality rubric reference {reference!r} "
                        f"resolved to {candidate} but that document has "
                        "no '## Materiality rubric' section"
                    ),
                },
            ),
        )
    return RubricResolution(rubric=resolved, source_path=reference)
