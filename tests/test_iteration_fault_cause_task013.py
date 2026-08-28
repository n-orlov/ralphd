"""v0.13 task 013 (#89, requirement H): `iteration_detail` derives the fault
CAUSE -- `reason` + `signature` -- for its OWN iteration, not only for the
run's last fault.

#89's own correction comment is the point this pins: the cause was never
missing from disk. `faults.explain_fault` is a pure function of fields
`meta.json` already carries, and `state.fault_explanation` already called it
that way -- but only for `_last_fault_iteration(root)`. Iteration #2's cause
became unreachable the moment #40 faulted, though #2's own meta held every
input `explain_fault` needs the whole time.

What is pinned here:

  * `iteration_detail` re-derives `reason`/`signature` for its own number
    through the exact same `faults.explain_fault` call `fault_explanation`
    uses -- one shared derivation, never a second copy of the ladder;
  * both keys live in `ITERATION_DERIVED_KEYS`, so a hand-edited/hostile
    meta.json cannot smuggle a fake cause straight through the passthrough
    (the `_with_approach_display`/`contract_fault_for_iteration` discipline,
    applied a third time);
  * `faultClass` is always the verdict the ENGINE recorded and acted on --
    never overwritten by the re-derivation; a disagreement is reported via
    `notices`/`FAULT_VERDICT_DIVERGED_NOTICE` instead of silently preferred;
  * a run dir with TWO faulted iterations of DIFFERENT causes: each
    iteration's own detail names its own cause, and the later fault does not
    overwrite the earlier one's -- the exact defect #89's correction comment
    describes.

Out of scope, deliberately (named in the PRD, requirement H): attributing a
historical `infra_retry`/`infra_wait` episode (attempt, backoff, outage
budget) to its iteration. Those events are episode-scoped, not
iteration-scoped, and keying them by iteration is a separable follow-on --
not filed as a new issue by this task (nothing here needed it); left for
whoever wants it to file.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine.faults import (
    FAULT_REASON_SIGNATURE,
    FAULT_REASON_WORK,
)
from ralphd.engine.state import (
    FAULT_VERDICT_DIVERGED_NOTICE,
    ITERATION_DERIVED_KEYS,
    iteration_detail,
)

DNS_ERROR = "request to https://aigw.internal/v1 failed, reason: getaddrinfo EAI_AGAIN aigw.internal"
WORK_ERROR = "Traceback (most recent call last):\n  ...\nAssertionError: task 5 criteria not met"


def _write_iteration(run_dir: Path, n: int, meta: dict) -> Path:
    d = run_dir / "iterations" / f"{n:04d}"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(meta))
    return d


def _meta(n: int, **over) -> dict:
    meta = {"number": n, "phase": "worker", "approach": 1,
            "startedAt": "2026-09-04T10:00:00Z", "endedAt": "2026-09-04T10:00:31Z",
            "exitCode": 0, "interrupted": False, "timedOut": False,
            "noTrafficTimeout": False, "sawComplete": False, "sawVerified": False,
            "error": None, "faultClass": None, "usage": {}}
    meta.update(over)
    return meta


def test_reason_and_signature_are_in_the_derived_keys_tuple():
    assert "reason" in ITERATION_DERIVED_KEYS
    assert "signature" in ITERATION_DERIVED_KEYS


def test_non_faulted_iteration_has_no_cause(tmp_path):
    _write_iteration(tmp_path, 1, _meta(1))
    d = iteration_detail(tmp_path, 1)
    assert d["faultClass"] is None
    assert d["reason"] is None
    assert d["signature"] is None
    assert d["notices"] == []


def test_two_faulted_iterations_of_different_causes_each_name_their_own(tmp_path):
    """The exact shape #89's correction comment describes: an infra
    signature fault, followed by a later, unrelated work fault -- neither
    iteration's cause is lost or overwritten by the other's."""
    _write_iteration(tmp_path, 2, _meta(
        2, error=DNS_ERROR, exitCode=0, faultClass="infra"))
    _write_iteration(tmp_path, 5, _meta(
        5, error=WORK_ERROR, exitCode=1, faultClass="work",
        usage={"totalTokens": 4000, "input": 3000, "output": 1000}))

    d2 = iteration_detail(tmp_path, 2)
    assert d2["faultClass"] == "infra"
    assert d2["reason"] == FAULT_REASON_SIGNATURE
    assert d2["signature"]["family"] == "dns"
    assert d2["notices"] == []

    # Reading iteration 5 (the LATER fault) does not touch iteration 2's own
    # cause -- re-reading #2 fresh still names its own cause, unmodified.
    d5 = iteration_detail(tmp_path, 5)
    assert d5["faultClass"] == "work"
    assert d5["reason"] == FAULT_REASON_WORK
    assert d5["signature"] is None
    assert d5["notices"] == []

    d2_again = iteration_detail(tmp_path, 2)
    assert d2_again["reason"] == FAULT_REASON_SIGNATURE
    assert d2_again["signature"]["family"] == "dns"


def test_divergence_between_recorded_and_rederived_is_reported_not_resolved(tmp_path):
    """The engine recorded `work` for an error that textually matches an
    infra signature (e.g. an abort/interrupt carve-out that predates this
    engine's own classifier) -- the recorded verdict stands, and the
    disagreement is said out loud via `notices`."""
    _write_iteration(tmp_path, 3, _meta(3, error=DNS_ERROR, exitCode=0,
                                        faultClass="work"))
    d = iteration_detail(tmp_path, 3)
    assert d["faultClass"] == "work", "never overwritten by the re-derivation"
    assert d["reason"] == FAULT_REASON_SIGNATURE
    assert d["signature"]["family"] == "dns"
    assert FAULT_VERDICT_DIVERGED_NOTICE in d["notices"]


def test_hostile_meta_cannot_smuggle_a_fake_cause(tmp_path):
    """A hand-edited (or hostile) meta.json that writes `reason`/`signature`
    directly, plus a `notices` list of its own invention, gets neither
    passed through nor merged with the real derivation -- the
    `ITERATION_DERIVED_KEYS` passthrough strips all three before this
    iteration's own cause is recomputed from its OTHER (raw) fields."""
    _write_iteration(tmp_path, 4, _meta(
        4, error=DNS_ERROR, exitCode=0, faultClass="infra",
        reason="forged: everything is fine",
        signature={"family": "forged", "description": "not real"},
        notices=["forged notice"]))
    d = iteration_detail(tmp_path, 4)
    assert d["reason"] == FAULT_REASON_SIGNATURE
    assert d["signature"]["family"] == "dns"
    assert d["notices"] == []

    # And a non-faulted iteration forging `faultClass` itself as a truthy
    # non-string sentinel does not fool the guard either -- only a real
    # string faultClass triggers re-derivation.
    _write_iteration(tmp_path, 6, _meta(6, faultClass="", reason="forged"))
    d6 = iteration_detail(tmp_path, 6)
    assert d6["reason"] is None
    assert d6["signature"] is None
