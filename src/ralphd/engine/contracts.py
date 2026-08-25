"""ralphd.engine.contracts — the one validator a hat's response contract
goes through (task 009, requirement C, #60 pillar 1).

Fact 4 is why this module exists at all: `pi` has no `--response-format`,
no `--schema`, no tool-forcing, so a response contract is not
provider-enforced — this is the harness-side enforcement, and re-prompting
on a failure (task 010) is the mechanism that makes the absence of
provider enforcement survivable.

Fact 16 / #65 is why there must be exactly one of these. #65 shipped as one
correct rule in `tests/conftest.py` and 19 independent, each-slightly-wrong
re-derivations of it scattered through the modules that needed the answer;
fixing the shared copy reached 25 of 45 call sites and left the rest broken
in the exact layout the fix was for. "A shared fix with a live second copy
is not a fix" (PRD fact 16). So: `validate_payload` below is the *only*
function in this tree that checks a payload against a hat's schema. No
other module may grow a second one, however differently shaped or named —
`tests/test_response_contracts.py` scans the whole `src/ralphd` tree for
anything schema-validator-shaped and fails the suite if it finds a second
one, exactly as `tests/test_ralphctl_resolution.py` does for #65's console
script paths.

This module implements a deliberately small subset of JSON Schema — only
what `transitions.HAT_SCHEMAS` actually uses: `type` (object/string/
integer/number/boolean/array), `properties`, `required`,
`additionalProperties` (bool) and per-property `enum`. It is not a general
JSON Schema engine and must not grow into one; if a hat's schema ever needs
more, that is a decision to make explicitly, not a feature to add quietly
to satisfy one new field.

Nothing in `_run_job_core` calls this yet — task 010 wires re-prompting on
a validation failure, task 011 wires the plan-proposal ingestion path. This
module only has to exist and be correct in isolation for task 009.
"""

from __future__ import annotations

from typing import Any

from ralphd.engine.transitions import HAT_SCHEMAS, HATS

_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
}


def _check_type(value: Any, expected: str, path: str) -> list[str]:
    checkers = _TYPE_CHECKS.get(expected)
    if checkers is None:  # pragma: no cover - schemas are authored, not user input
        return [f"{path}: unknown schema type {expected!r}"]
    # bool is a subclass of int in Python; do not let a bool satisfy
    # "integer"/"number" or vice versa -- a schema author who wrote
    # "boolean" meant it.
    if isinstance(value, bool) and expected in ("integer", "number"):
        return [f"{path}: expected {expected}, got boolean"]
    if not isinstance(value, checkers):
        return [f"{path}: expected {expected}, got {type(value).__name__}"]
    return []


def _validate_against(schema: dict, payload: Any, path: str) -> list[str]:
    errors: list[str] = []

    expected_type = schema.get("type")
    if expected_type is not None:
        errors.extend(_check_type(payload, expected_type, path))
        if errors:
            return errors  # wrong type -- nothing below is meaningful

    if expected_type == "object" or (expected_type is None and isinstance(payload, dict)):
        if not isinstance(payload, dict):
            return errors  # already reported above via _check_type
        properties: dict = schema.get("properties", {})
        required: list[str] = schema.get("required", [])
        for name in required:
            if name not in payload:
                errors.append(f"{path}: missing required field {name!r}")
        additional_allowed = schema.get("additionalProperties", True)
        if not additional_allowed:
            extra = sorted(set(payload) - set(properties))
            if extra:
                errors.append(f"{path}: unexpected field(s) {extra!r}")
        for name, sub_schema in properties.items():
            if name in payload:
                errors.extend(
                    _validate_against(sub_schema, payload[name], f"{path}.{name}")
                )

    enum = schema.get("enum")
    if enum is not None and payload not in enum:
        errors.append(f"{path}: {payload!r} is not one of {enum!r}")

    return errors


def validate_payload(hat: str, payload: Any) -> list[str]:
    """Validate `payload` (already-parsed JSON) against `hat`'s declared
    schema (`transitions.HAT_SCHEMAS[hat]`). Returns a list of human
    readable error strings; empty means the payload is valid. Raises
    `KeyError` for a hat with no declared schema -- that is a programming
    error (task 009's own enumeration test), not an ordinary-case input to
    handle gracefully.

    This is the sole entry point response-contract validation goes
    through, from re-prompting (task 010) to plan-proposal ingestion
    (task 011) -- see the module docstring for why there must not be a
    second one.
    """
    schema = HAT_SCHEMAS[hat]
    if not isinstance(payload, dict):
        return [f"$: expected object, got {type(payload).__name__}"]
    return _validate_against(schema, payload, "$")


def validate_against(schema: dict, payload: Any) -> list[str]:
    """Validate `payload` against an arbitrary schema in this module's
    restricted subset, for a caller that has no `transitions.HAT_SCHEMAS`
    entry to key off (task 018's `engine.router`: the router's own answer
    shape -- `{"next_step": ..., "task_id": ...}` -- is not a per-hat
    response contract, so it has no HATS entry, but its answer still MUST
    go through this module's one schema engine (fact 16/#65), not a
    second, hand-rolled one living in `router.py`.

    A thin wrapper over the same `_validate_against` that `validate_payload`
    itself calls -- there is still exactly one engine; this only widens
    which schema dict it may be pointed at.
    """
    return _validate_against(schema, payload, "$")


def hats_with_schemas() -> frozenset[str]:
    """The hats that currently have a declared schema -- used by the
    enumeration test to prove every hat in `transitions.HATS` has one, and
    that nothing has been declared for a hat the table does not know."""
    return frozenset(HAT_SCHEMAS)


assert hats_with_schemas() == frozenset(HATS), (
    "HAT_SCHEMAS and transitions.HATS have drifted apart -- every hat needs "
    "a schema and every schema needs a live hat"
)
