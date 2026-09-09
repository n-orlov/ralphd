"""Task 010 (requirement C, #60 pillar 1, fact 19): re-prompt loop around
`contracts.validate_payload`.

Three ordinary failure shapes -- malformed JSON, an absent payload, and
syntactically valid JSON of the wrong shape -- must each show the harness
re-prompting with the *specific* validation error, `JobConfig`'s
`contract_retry_max` extra times, before scoring an agent-fault iteration.
Every violation is logged loudly to `events.jsonl` at the moment it is
discovered (fact 19's hazard), not just inferred from the final outcome.

No engine cutover is asserted here -- `_run_job_core` does not call any of
this yet (task 014's cutover); this module only proves the mechanism
itself, exactly as `tests/test_response_contracts.py` did for task 009.
"""

from __future__ import annotations

import json

import pytest

from ralphd.engine import contract_enforcement as ce
from ralphd.engine.config import JobConfig
from ralphd.engine.state import RunDir


# ---------------------------------------------------------------------------
# JobConfig knob: default, job.yaml, RALPHD_* override, GET /config view
# ---------------------------------------------------------------------------

def test_contract_retry_max_default_is_two():
    assert JobConfig().contract_retry_max == 2


def test_contract_retry_max_from_job_yaml(tmp_path):
    p = tmp_path / "job.yaml"
    p.write_text("contract_retry_max: 5\n")
    cfg = JobConfig.load(p)
    assert cfg.contract_retry_max == 5


def test_contract_retry_max_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("RALPHD_CONTRACT_RETRY_MAX", "4")
    cfg = JobConfig.load(tmp_path / "does-not-exist.yaml")
    assert cfg.contract_retry_max == 4


def test_contract_retry_max_in_effective_view():
    assert JobConfig().effective()["contracts"]["retryMax"] == 2
    assert JobConfig(contract_retry_max=7).effective()["contracts"]["retryMax"] == 7


# ---------------------------------------------------------------------------
# parse_response -- the three ordinary failure shapes, each with its own
# specific error text (what a re-prompt states verbatim to the hat)
# ---------------------------------------------------------------------------

def test_absent_payload_is_a_specific_error():
    payload, errors = ce.parse_response("worker", None)
    assert payload is None
    assert errors and "absent" in errors[0]


def test_blank_payload_is_treated_as_absent():
    payload, errors = ce.parse_response("worker", "   \n")
    assert payload is None
    assert errors and "absent" in errors[0]


def test_malformed_json_is_a_specific_error():
    payload, errors = ce.parse_response("worker", "{not json")
    assert payload is None
    assert errors and "malformed JSON" in errors[0]


def test_wrong_shape_json_reuses_validate_payload_errors():
    # valid JSON, but "verdict" is not one of worker's enum values.
    payload, errors = ce.parse_response("worker", json.dumps({"verdict": "maybe"}))
    assert payload is None
    assert errors == ["$.verdict: 'maybe' is not one of "
                       "['complete', 'no_verdict', 'unsatisfiable', "
                       "'plan_complete', 'deferred']"]


def test_a_valid_payload_comes_back_with_no_errors():
    raw = json.dumps({"verdict": "complete", "task_id": "001"})
    payload, errors = ce.parse_response("worker", raw)
    assert errors == []
    assert payload == {"verdict": "complete", "task_id": "001"}


# ---------------------------------------------------------------------------
# enforce_contract -- the re-prompt loop, one case per failure shape, plus
# recovery within the retry budget and exhaustion into an agent fault
# ---------------------------------------------------------------------------

class _StubSink:
    """A bare EventSink stub -- records every emit() call so a test can
    assert on the sequence without a real run directory."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, type_, **data):
        event = {"type": type_, **data}
        self.events.append(event)
        return event


def _always(raw):
    def respond(attempt, prior_errors):
        return raw
    return respond


@pytest.mark.parametrize("bad_raw, shape", [
    (None, "absent"),
    ("{this is not json", "malformed"),
    (json.dumps({"verdict": "maybe"}), "wrong-shape"),
])
def test_a_hat_that_never_recovers_is_reprompted_then_agent_faulted(bad_raw, shape):
    sink = _StubSink()
    result = ce.enforce_contract("worker", _always(bad_raw), max_retries=2, state=sink)

    assert result.ok is False
    assert result.agent_fault is True
    # first attempt + 2 retries = 3 total attempts.
    assert result.attempts == 3
    violation_events = [e for e in sink.events if e["type"] == ce.EVENT_VIOLATION]
    fault_events = [e for e in sink.events if e["type"] == ce.EVENT_AGENT_FAULT]
    assert len(violation_events) == 3, shape
    assert len(fault_events) == 1, shape
    assert all(e["hat"] == "worker" for e in violation_events + fault_events)


def test_the_specific_error_is_carried_back_into_the_next_attempt():
    """The harness re-prompts *with the specific validation error* -- prove
    `respond` actually receives it, not just that a second call happens."""
    seen_prior_errors = []

    def respond(attempt, prior_errors):
        seen_prior_errors.append(prior_errors)
        return "{still not json"

    ce.enforce_contract("worker", respond, max_retries=2, state=None)

    assert seen_prior_errors[0] is None  # nothing to carry on the first attempt
    assert seen_prior_errors[1] and "malformed JSON" in seen_prior_errors[1][0]
    assert seen_prior_errors[2] and "malformed JSON" in seen_prior_errors[2][0]


def test_recovery_within_the_retry_budget_needs_no_agent_fault():
    attempts_seen = []

    def respond(attempt, prior_errors):
        attempts_seen.append(attempt)
        if attempt < 2:
            return "not json at all"
        return json.dumps({"verdict": "complete"})

    sink = _StubSink()
    result = ce.enforce_contract("worker", respond, max_retries=2, state=sink)

    assert result.ok is True
    assert result.agent_fault is False
    assert result.attempts == 3
    assert result.payload == {"verdict": "complete"}
    assert not [e for e in sink.events if e["type"] == ce.EVENT_AGENT_FAULT]
    assert len([e for e in sink.events if e["type"] == ce.EVENT_VIOLATION]) == 2


def test_a_valid_first_attempt_never_reprompts_at_all():
    calls = []

    def respond(attempt, prior_errors):
        calls.append(attempt)
        return json.dumps({"verdict": "complete"})

    sink = _StubSink()
    result = ce.enforce_contract("worker", respond, max_retries=2, state=sink)

    assert result.ok is True
    assert result.attempts == 1
    assert calls == [0]
    assert sink.events == []


def test_zero_retries_means_exactly_one_attempt_then_agent_fault():
    sink = _StubSink()
    result = ce.enforce_contract("worker", _always(None), max_retries=0, state=sink)
    assert result.attempts == 1
    assert result.agent_fault is True
    assert len([e for e in sink.events if e["type"] == ce.EVENT_VIOLATION]) == 1


def test_negative_retries_is_a_programming_error():
    with pytest.raises(ValueError):
        ce.enforce_contract("worker", _always(None), max_retries=-1, state=None)


# ---------------------------------------------------------------------------
# The violation is logged loudly to events.jsonl at the moment it happens --
# a real run directory, not just the stub sink above, and asserted on the
# events actually written to disk, not on the returned outcome.
# ---------------------------------------------------------------------------

def test_violations_land_on_disk_in_events_jsonl_as_they_happen(tmp_path):
    run = RunDir(root=tmp_path)
    result = ce.enforce_contract("worker", _always("not json"), max_retries=1, state=run)
    assert result.agent_fault is True

    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    events = [json.loads(line) for line in lines]
    types = [e["type"] for e in events]
    assert types == [ce.EVENT_VIOLATION, ce.EVENT_VIOLATION, ce.EVENT_AGENT_FAULT]
    assert all(e["hat"] == "worker" for e in events)
    assert "malformed JSON" in events[0]["errors"][0]


def test_a_run_state_is_optional_and_silently_skipped():
    """No `state` argument -- the mechanism itself does not require an
    on-disk run directory to be exercised (unit-testable in isolation, per
    task 009's precedent)."""
    result = ce.enforce_contract("worker", _always(None), max_retries=1, state=None)
    assert result.agent_fault is True
