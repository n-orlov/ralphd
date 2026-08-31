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
