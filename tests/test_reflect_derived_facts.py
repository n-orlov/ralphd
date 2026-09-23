"""Task 007 (PRD C.2/C.3/C.4, #211): `report_written`/`suggestions_written`
retired from the `reflect` response contract (`transitions.HAT_SCHEMAS
["reflect"]`) -- the engine derives both facts itself from
`artifacts/reflection/report.md` and `artifacts/reflection/suggestions.diff`
instead of trusting an agent-supplied claim.

Three things pinned here, over `LoopSupervisor._reflect_derived_facts()`:

1. it tracks the two files across all four present/absent combinations
   (neither, report only, suggestions only, both);
2. `_reflect_failure()` reads the SAME derivation for `report_written` --
   a missing report is still a DEGRADED reflect failure even with a
   contract payload that no longer says anything about it either way;
3. a historical payload carrying both retired booleans still parses and
   validates without error (PRD C.4) -- the schema-level half of the same
   guarantee, `tests/test_response_contracts.py`'s own dedicated test, is
   exercised again here end to end through `_reflect_verdict`/
   `_reflect_failure` so a stale claim from an *earlier* engine cannot
   turn a perfectly good reflect into a failure either.
"""

from __future__ import annotations

import json

import pytest

from ralphd.engine.runner import IterationResult
from tests.conftest import make_supervisor


def _ok_result(final_text: str = "{}") -> IterationResult:
    return IterationResult(
        final_text=final_text, exit_code=0,
        interrupted=False, timed_out=False, no_traffic_timeout=False,
        error_message="",
    )


@pytest.mark.parametrize(
    "write_report, write_suggestions",
    [(False, False), (True, False), (False, True), (True, True)],
)
def test_derived_facts_track_the_two_files_across_present_absent_combinations(
    tmp_path, write_report, write_suggestions
):
    sup = make_supervisor(tmp_path)
    outdir = sup.run.artifacts_dir / "reflection"
    outdir.mkdir(parents=True, exist_ok=True)
    if write_report:
        (outdir / sup.REFLECT_REPORT).write_text("# Report\n\nit went fine.\n")
    if write_suggestions:
        (outdir / sup.REFLECT_SUGGESTIONS).write_text("--- a/x\n+++ b/x\n")

    facts = sup._reflect_derived_facts()

    assert facts == {"report_written": write_report,
                      "suggestions_written": write_suggestions}


def test_a_missing_report_is_still_a_degraded_failure_with_an_empty_contract(tmp_path):
    """Nothing in the contract claims report_written any more, but a
    missing report.md is still caught -- derived from disk, not from the
    (now silent) payload."""
    sup = make_supervisor(tmp_path)
    error, fault_class = sup._reflect_failure(_ok_result(json.dumps({})))
    assert error is not None and "report.md" in error
    assert fault_class == sup.REFLECT_FAULT_DEGRADED


def test_a_report_on_disk_with_an_empty_contract_is_not_a_failure(tmp_path):
    sup = make_supervisor(tmp_path)
    outdir = sup.run.artifacts_dir / "reflection"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / sup.REFLECT_REPORT).write_text("# Report\n\nfine.\n")

    error, fault_class = sup._reflect_failure(_ok_result(json.dumps({})))

    assert (error, fault_class) == (None, None)


def test_a_historical_payload_carrying_both_retired_booleans_still_reads_clean(tmp_path):
    """PRD C.4: an extra field in a historical record is not a contract
    violation. A reflect payload from before task 007 -- both booleans
    present and true -- must not turn a perfectly good report into a
    failure."""
    sup = make_supervisor(tmp_path)
    outdir = sup.run.artifacts_dir / "reflection"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / sup.REFLECT_REPORT).write_text("# Report\n\nfine.\n")
    historical = json.dumps({"report_written": True,
                              "suggestions_written": True,
                              "notes": "from an earlier engine"})

    payload, contract_errors = sup._reflect_verdict(_ok_result(historical))
    error, fault_class = sup._reflect_failure(_ok_result(historical))

    assert contract_errors == []
    assert payload["report_written"] is True  # still readable, just unused
    assert (error, fault_class) == (None, None)
