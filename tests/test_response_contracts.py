"""Task 009 (requirement C, #60 pillar 1): each hat's response-contract
schema, declared beside its table entry in `transitions.HAT_SCHEMAS`, and
`contracts.validate_payload` as the *only* code path that checks a payload
against one.

No engine cutover is asserted here -- `_run_job_core` does not call any of
this yet (task 010 re-prompts on a failure, task 011 ingests the plan
proposal). This module only proves the contract layer itself is correct,
complete against the table, and not duplicated.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ralphd.engine import contracts, transitions as tr

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "ralphd"
CONTRACTS_PATH = SRC_ROOT / "engine" / "contracts.py"


# ---------------------------------------------------------------------------
# Every hat in the table has a schema, and vice versa
# ---------------------------------------------------------------------------

def test_every_hat_in_the_table_has_a_declared_schema():
    assert set(tr.HATS) == set(tr.HAT_SCHEMAS)
    assert set(tr.HATS) == set(contracts.hats_with_schemas())


def test_a_hat_missing_from_the_schema_map_is_caught():
    """Mutation case for the enumeration check above: a hats tuple with one
    extra hat that has no schema must fail the same comparison the real
    test makes -- proving the check is doing real work, not vacuously
    passing because both sides happen to be empty or equal by accident."""
    hats_with_an_orphan = tr.HATS + ("router",)
    assert set(hats_with_an_orphan) != set(tr.HAT_SCHEMAS)


@pytest.mark.parametrize("hat", tr.HATS)
def test_every_schema_is_an_object_schema_with_required_fields(hat):
    schema = tr.HAT_SCHEMAS[hat]
    assert schema["type"] == "object"
    assert schema["required"], f"{hat}'s schema declares no required fields"
    assert schema["additionalProperties"] is False


def test_the_planning_schema_carries_no_status_field():
    """Requirement C: the plan schema has no status field; the harness
    initialises every ingested task to pending. That is task 011's
    ingestion path, but the planning *hat's* own final-output schema (the
    pointer-plus-summary, not the proposal file) must not smuggle one in
    either."""
    assert "status" not in tr.HAT_SCHEMAS["planning"]["properties"]


# ---------------------------------------------------------------------------
# validate_payload: acceptance and rejection, per hat
# ---------------------------------------------------------------------------

def test_a_well_formed_worker_payload_validates_clean():
    errors = contracts.validate_payload(
        "worker", {"verdict": "complete", "task_id": "001"}
    )
    assert errors == []


def test_a_missing_required_field_is_reported_by_name():
    errors = contracts.validate_payload("verify", {"verdict": "verified"})
    assert any("task_id" in e for e in errors)


def test_a_value_outside_the_enum_is_reported():
    errors = contracts.validate_payload(
        "worker", {"verdict": "maybe", "task_id": "001"}
    )
    assert any("maybe" in e for e in errors)


def test_an_unexpected_field_is_reported_not_silently_dropped():
    errors = contracts.validate_payload(
        "reflect",
        {"report_written": True, "suggestions_written": True,
         "typo_field": "oops"},
    )
    assert any("typo_field" in e for e in errors)


def test_the_wrong_top_level_shape_is_reported():
    errors = contracts.validate_payload("planning", ["not", "an", "object"])
    assert errors and "object" in errors[0]


def test_a_valid_payload_for_every_hat_validates_clean():
    samples = {
        "planning": {"proposal_path": "plan-proposal.json", "summary": "x"},
        "worker": {"verdict": "complete", "task_id": "001"},
        "verify": {"task_id": "001", "verdict": "verified"},
        "review": {"verdict": "verified"},
        "reflect": {"report_written": True, "suggestions_written": True},
    }
    for hat, payload in samples.items():
        assert contracts.validate_payload(hat, payload) == [], (hat, payload)


def test_an_unknown_hat_is_a_programming_error_not_a_validation_result():
    with pytest.raises(KeyError):
        contracts.validate_payload("no_such_hat", {})


# ---------------------------------------------------------------------------
# Structural audit: exactly one validator code path (fact 16 / #65's lesson)
# ---------------------------------------------------------------------------
#
# #65 shipped as one correct rule and 19 independent re-derivations of the
# broken one; fixing the shared copy alone left most of the tree still
# broken. The same failure mode applies here: a second, hand-rolled schema
# checker anywhere in src/ralphd would silently diverge from this one the
# first time either got a fix the other did not. Two structural rules,
# mirroring tests/test_ralphctl_resolution.py's shape:
#
#   A. shape-based: any function elsewhere whose body treats "required",
#      "properties" and "type" as string constants together is doing what
#      a schema validator does, whatever it is named.
#   B. name-based: only one function named `validate_payload` may exist in
#      the whole tree, and it must be this module's.

_SCHEMA_SHAPE_MARKERS = frozenset({"required", "properties", "type"})


def _string_constants(node: ast.AST) -> set[str]:
    return {
        n.value for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _schema_validator_shaped_functions(tree: ast.AST) -> list[ast.FunctionDef]:
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _SCHEMA_SHAPE_MARKERS.issubset(_string_constants(node)):
                hits.append(node)
    return hits


def _named_validate_payload(tree: ast.AST) -> list[ast.FunctionDef]:
    return [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "validate_payload"
    ]


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return path.name  # planted fixture outside the repo (mutation cases)


def _offences(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offences = []
    for fn in _schema_validator_shaped_functions(tree):
        offences.append(
            f"{_display_path(path)}:{fn.lineno}: {fn.name}() looks "
            "like a second schema validator (references required/"
            "properties/type together) -- use "
            "ralphd.engine.contracts.validate_payload instead")
    for fn in _named_validate_payload(tree):
        offences.append(
            f"{_display_path(path)}:{fn.lineno}: a second "
            "validate_payload() -- only engine/contracts.py may define one")
    return offences


def test_no_module_other_than_contracts_defines_a_schema_validator():
    modules = sorted(SRC_ROOT.rglob("*.py"))
    assert len(modules) > 10, "the glob under src/ralphd found suspiciously little"

    offences = [
        o for p in modules if p != CONTRACTS_PATH for o in _offences(p)
    ]
    assert offences == [], (
        "a second response-contract validator has appeared (fact 16/#65's "
        "lesson) -- route through ralphd.engine.contracts.validate_payload "
        "instead:\n  " + "\n  ".join(offences))


def test_contracts_module_itself_is_the_one_exempted_copy():
    # Sanity check on the scan itself: contracts.py obviously *is*
    # shape-marked and *does* define validate_payload -- if scanning it
    # too produced zero offences, the shape/name detectors below would be
    # vacuous.
    tree = ast.parse(CONTRACTS_PATH.read_text(encoding="utf-8"))
    assert _schema_validator_shaped_functions(tree)
    assert _named_validate_payload(tree)


def test_the_audit_catches_a_reintroduced_shape_based_duplicate(tmp_path):
    """Mutation case, rule A: a differently-named function that still walks
    required/properties/type must be caught."""
    planted = tmp_path / "test_regressed_shape.py"
    planted.write_text(
        "def _my_own_checker(schema, payload):\n"
        "    required = schema.get('required', [])\n"
        "    properties = schema.get('properties', {})\n"
        "    t = schema.get('type')\n"
        "    return required, properties, t\n")
    offences = _offences(planted)
    assert len(offences) == 1
    assert "_my_own_checker" in offences[0]


def test_the_audit_catches_a_reintroduced_name_based_duplicate(tmp_path):
    """Mutation case, rule B: a same-named function anywhere else, even one
    that does nothing schema-shaped, must be caught."""
    planted = tmp_path / "test_regressed_name.py"
    planted.write_text(
        "def validate_payload(hat, payload):\n"
        "    return []\n")
    offences = _offences(planted)
    assert len(offences) == 1
    assert "second validate_payload" in offences[0]


def test_a_clean_module_produces_no_offences(tmp_path):
    planted = tmp_path / "test_clean.py"
    planted.write_text(
        "from ralphd.engine.contracts import validate_payload\n"
        "\n\n"
        "def test_uses_the_shared_validator():\n"
        "    assert validate_payload('worker', {'verdict': 'complete'}) == []\n")
    assert _offences(planted) == []
