"""Unit tests for the pure fault classifier (task 001a).

`classify_fault` is a pure function over an iteration's failure signal --
no engine/loop/runner state involved -- so these tests call it directly
with hand-built signal combinations.
"""

from __future__ import annotations

import pytest

from ralphd.engine.faults import (
    FAULT_CLASS_RATE_LIMIT,
    FAULT_CLASS_SIGNAL,
    FAULT_REASON_RATE_LIMIT,
    classify_fault,
    effective_utilization,
    explain_fault,
    parse_resets_at,
    rate_limit_resets_at,
)


def test_success_is_not_a_failure():
    assert classify_fault(exit_code=0) is None


def test_success_with_traffic_is_not_a_failure():
    assert classify_fault(exit_code=0, produced_traffic=True) is None


def test_enotfound_error_text_classifies_infra():
    assert classify_fault(
        error_text="Error: getaddrinfo ENOTFOUND api.example.com",
        exit_code=1,
        produced_traffic=False,
    ) == "infra"


def test_enotfound_classifies_infra_even_with_some_traffic():
    # A gateway-level DNS/connection failure the agent still surfaced as an
    # assistant-visible error after partial traffic is still an infra fault,
    # not a work failure -- the text signature wins.
    assert classify_fault(
        error_text="getaddrinfo ENOTFOUND gateway.internal",
        exit_code=1,
        produced_traffic=True,
    ) == "infra"


def test_econnrefused_error_text_classifies_infra():
    assert classify_fault(
        error_text="connect ECONNREFUSED 10.0.0.1:443",
        exit_code=1,
        produced_traffic=False,
    ) == "infra"


def test_tls_handshake_failure_classifies_infra():
    assert classify_fault(
        error_text="TLS handshake failed while connecting to provider",
        exit_code=1,
        produced_traffic=False,
    ) == "infra"


def test_gateway_5xx_before_any_tokens_classifies_infra():
    assert classify_fault(
        error_text="upstream request failed: 502 Bad Gateway",
        exit_code=1,
        produced_traffic=False,
    ) == "infra"


def test_startup_window_no_traffic_timeout_is_always_infra():
    # The engine's own watchdog fired (task 001a's core new mechanism): no
    # error text captured at all (the process never printed anything), but
    # this is unconditionally infra.
    assert classify_fault(
        error_text="",
        exit_code=None,
        interrupted=True,
        no_traffic_timeout=True,
        produced_traffic=False,
    ) == "infra"


def test_unclassifiable_no_traffic_failure_defaults_to_infra():
    # Instant-exit case (task 059's pre-existing carve-out territory): no
    # traffic, no recognizable infra text, still classified infra by this
    # module -- loop.py's wrapper is what decides (via duration_s) whether
    # to route this to the old streak-based carve-out instead of the new
    # retry-with-backoff path; classify_fault itself just answers "is the
    # LLM endpoint the likely culprit", which for a no-traffic failure it
    # always is.
    assert classify_fault(
        error_text="",
        exit_code=1,
        produced_traffic=False,
    ) == "infra"


def test_work_exit_with_traffic_classifies_work():
    assert classify_fault(
        error_text="agent crashed after making changes",
        exit_code=1,
        produced_traffic=True,
    ) == "work"


def test_timed_out_with_traffic_and_no_infra_text_classifies_work():
    assert classify_fault(
        error_text="",
        timed_out=True,
        produced_traffic=True,
    ) == "work"


def test_interrupted_with_traffic_and_no_infra_text_classifies_signal():
    # RETARGETED by task 013 (#49), not deleted: this shape used to be `work`,
    # which charged the agent for a failure a signal committed. It is now its
    # own class -- see faults.FAULT_CLASS_SIGNAL. Everything else about the
    # ladder is unchanged, so this stays here beside the shapes it neighbours.
    verdict = classify_fault(
        error_text="",
        interrupted=True,
        produced_traffic=True,
    )
    assert verdict == FAULT_CLASS_SIGNAL
    assert verdict != "work", "the whole point of #49 part 1"
    assert verdict != "infra", "and it is not retried as an outage either"


# -- task 001 (#11): an error recorded at exit 0 is still a failure ---------
#
# pi can record an in-band agent error (`message_end` with
# `stopReason: "error"`) and *still* exit 0 -- the observed live shape was a
# gateway error surfaced as an assistant error message with zero token usage
# followed by a clean shutdown. The table below pins the full
# {exit 0, exit nonzero} x {traffic, no traffic} grid for an error-bearing
# result: every cell must classify as a fault (never None), with the class
# decided only by the error signature and whether traffic happened.
_ERROR_BEARING_GRID = [
    # (id, error_text, exit_code, produced_traffic, expected)
    ("infra-text/exit0/no-traffic", "connect ECONNRESET 10.0.0.1:443", 0, False, "infra"),
    ("infra-text/exit0/traffic", "connect ECONNRESET 10.0.0.1:443", 0, True, "infra"),
    ("infra-text/exit1/no-traffic", "connect ECONNRESET 10.0.0.1:443", 1, False, "infra"),
    ("infra-text/exit1/traffic", "connect ECONNRESET 10.0.0.1:443", 1, True, "infra"),
    ("gateway-dns/exit0/no-traffic",
     "getaddrinfo EAI_AGAIN gateway.internal", 0, False, "infra"),
    ("gateway-dns/exit0/traffic",
     "getaddrinfo EAI_AGAIN gateway.internal", 0, True, "infra"),
    # No recognized infra signature: a no-traffic failure is still infra
    # (nothing ever ran), a with-traffic one is the agent's own problem.
    ("opaque/exit0/no-traffic", "unknown agent error", 0, False, "infra"),
    ("opaque/exit0/traffic", "unknown agent error", 0, True, "work"),
    ("opaque/exit1/no-traffic", "unknown agent error", 1, False, "infra"),
    ("opaque/exit1/traffic", "unknown agent error", 1, True, "work"),
]


@pytest.mark.parametrize(
    "error_text,exit_code,produced_traffic,expected",
    [case[1:] for case in _ERROR_BEARING_GRID],
    ids=[case[0] for case in _ERROR_BEARING_GRID],
)
def test_error_bearing_result_is_always_a_fault(error_text, exit_code,
                                                produced_traffic, expected):
    verdict = classify_fault(
        error_text=error_text,
        exit_code=exit_code,
        produced_traffic=produced_traffic,
    )
    assert verdict is not None, "an iteration with an error recorded is a failure"
    assert verdict == expected


def test_zero_token_no_traffic_termination_with_error_is_a_fault():
    # The exact live shape: exit 0, zero tokens, zero assistant text, an
    # infra-shaped errorMessage recorded in-band.
    assert classify_fault(
        error_text="connect ECONNRESET 10.0.0.1:443",
        exit_code=0,
        produced_traffic=False,
    ) == "infra"


@pytest.mark.parametrize("error_text", ["", "   ", "\n"],
                        ids=["empty", "spaces", "newline"])
def test_error_free_exit_zero_is_still_not_a_failure(error_text):
    # The guard against over-reaching: a clean iteration (no error text, or
    # only whitespace) must keep returning None whether or not it produced
    # traffic, so successes are never retried/refunded.
    assert classify_fault(error_text=error_text, exit_code=0) is None
    assert classify_fault(error_text=error_text, exit_code=0,
                          produced_traffic=True) is None


# -- task 002 (#11): the infra text-signature table, family by family ------
#
# Every case below is passed with produced_traffic=True and exit_code=1, so
# the *only* thing that can make the verdict "infra" is the error text
# matching `faults.INFRA_SIGNATURES` (a no-traffic failure would be
# classified infra regardless, which would make these assertions vacuous).
# Strings are the real-world shapes seen from the gateway / Bedrock stack.
_INFRA_FAMILY_CASES = [
    # (id, error_text)
    ("dns-enotfound", "Error: getaddrinfo ENOTFOUND aigw.internal.example"),
    ("dns-eai-again",
     ("request to https://aigw.internal/v1 failed, "
      "reason: getaddrinfo EAI_AGAIN aigw.internal")),
    ("tcp-econnrefused", "connect ECONNREFUSED 127.0.0.1:8080"),
    ("tcp-econnreset", "read ECONNRESET"),
    ("tcp-etimedout", "connect ETIMEDOUT 10.4.2.9:443"),
    ("tcp-ehostunreach", "connect EHOSTUNREACH 10.4.2.9:443"),
    ("tcp-enetunreach", "connect ENETUNREACH 2600:1f18::1:443"),
    ("stream-epipe", "write EPIPE"),
    ("stream-socket-hang-up", "Error: socket hang up"),
    ("stream-premature-close", "Error: aborted: Premature close"),
    ("tls-handshake", "TLS handshake timeout talking to the gateway"),
    ("ssl-handshake", "SSL handshake failure (alert 40)"),
    ("tls-certificate", "unable to get local issuer certificate: certificate verify failed"),
    # The single most common live shape: the SDK's opaque transport error.
    ("sdk-connection-error", "Connection error."),
    ("http-502", "upstream request failed: 502 Bad Gateway"),
    ("http-504", "504 Gateway Timeout"),
    ("http-503", "503 Service Unavailable"),
    ("bedrock-serviceunavailable", "ServiceUnavailableException: Bedrock is unavailable"),
    ("http-500-internal", "API error: Internal server error"),
    ("backpressure-429", "429 Too Many Requests"),
    ("backpressure-529", "529 {\"type\":\"error\",\"error\":{\"type\":\"overloaded_error\"}}"),
    ("backpressure-rate-limit", "Rate limit reached for model eu.anthropic.claude-opus-5"),
    ("backpressure-ratelimit-oneword", "ratelimit_exceeded: slow down"),
    ("backpressure-throttling", "ThrottlingException: Too many requests, please wait"),
    ("backpressure-overloaded", "Overloaded"),
    ("backpressure-overloaded-error", "{\"type\":\"overloaded_error\",\"message\":\"Overloaded\"}"),
    ("bedrock-model-stream-error", "ModelStreamErrorException: stream terminated by upstream"),
    ("quota-exhausted", "You exceeded your current quota for this model"),
    ("capacity-exhausted", "No capacity available in this region, retry later"),
]


@pytest.mark.parametrize("error_text",
                        [case[1] for case in _INFRA_FAMILY_CASES],
                        ids=[case[0] for case in _INFRA_FAMILY_CASES])
def test_infra_signature_family_classifies_infra(error_text):
    assert classify_fault(
        error_text=error_text,
        exit_code=1,
        produced_traffic=True,
    ) == "infra", f"expected an infra signature match for: {error_text!r}"


def test_regression_deck_phase1_getaddrinfo_eai_again():
    # deck-phase1, worker iteration that hung the full iteration timeout on a
    # transient gateway-DNS glitch before dying: temp-failure DNS text with
    # traffic already observed must still be infra, never "work".
    assert classify_fault(
        error_text="request to https://aigw.internal/v1/messages failed, "
                   "reason: getaddrinfo EAI_AGAIN aigw.internal",
        exit_code=-2,
        timed_out=True,
        produced_traffic=True,
    ) == "infra"


def test_regression_deck_phase1_econnreset_exit0_zero_tokens():
    # deck-phase1, in-band error shape: pi recorded the gateway reset as an
    # assistant error and exited 0 with zero token usage.
    assert classify_fault(
        error_text="read ECONNRESET",
        exit_code=0,
        produced_traffic=False,
    ) == "infra"


def test_regression_deck_phase1_connection_error_exit0_zero_tokens():
    # Same shape, the other observed text: pi's bare "Connection error.".
    # Asserted with produced_traffic=True as well, so the verdict comes from
    # the signature table rather than from the no-traffic default.
    assert classify_fault(
        error_text="Connection error.",
        exit_code=0,
        produced_traffic=False,
    ) == "infra"
    assert classify_fault(
        error_text="Connection error.",
        exit_code=0,
        produced_traffic=True,
    ) == "infra"


_NON_INFRA_ERROR_TEXTS = [
    ("pytest-failure", "pytest exited 1: 3 failed, 12 passed in 41.2s"),
    ("assertion-failure",
     "AssertionError: expected task 004 status 'completed', got 'pending'"),
    ("agent-gave-up",
     "the agent could not reconcile the merge conflict in src/app.py and gave up"),
    ("ruff-failure", "ruff check failed: 2 errors (line-too-long, unused-import)"),
]


@pytest.mark.parametrize("error_text",
                        [case[1] for case in _NON_INFRA_ERROR_TEXTS],
                        ids=[case[0] for case in _NON_INFRA_ERROR_TEXTS])
def test_ordinary_agent_failure_text_is_not_infra(error_text):
    # The table must not swallow genuine work failures: with traffic
    # observed, an ordinary agent failure stays "work" so it keeps consuming
    # the approach/task-failure bookkeeping instead of being retried forever.
    assert classify_fault(
        error_text=error_text,
        exit_code=1,
        produced_traffic=True,
    ) == "work", f"unexpected infra signature match in: {error_text!r}"


# -- v0.30 F.1: the `rate_limit` class, from structured evidence only --------

_RL_ERROR = 'API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}'
_REJECTED = {"status": "rejected", "resetsAt": 1790936400,
             "rateLimitType": "five_hour",
             "unifiedWindows": {"five_hour": {"resetsAt": 1790936400}}}


@pytest.mark.parametrize("kwargs", [
    # a fresh rate-limit report with status rejected
    {"rate_limit": _REJECTED, "rate_limit_fresh": True},
    # the SDK's assistant error type
    {"error_type": "rate_limit"},
    {"error_type": "rate_limit_error"},
    # HTTP 429 together with an error type, a rateLimitType or the API's
    # own rate_limit_error in the text
    {"http_status": 429, "error_type": "server_error"},
    {"http_status": 429, "rate_limit": {"status": "allowed",
                                        "rateLimitType": "seven_day"}},
    {"http_status": 429},
], ids=["fresh-rejected", "error-type", "error-type-api", "429-type",
        "429-window", "429-text"])
def test_structured_rate_limit_evidence_classifies_rate_limit(kwargs):
    exp = explain_fault(error_text=_RL_ERROR, exit_code=1, **kwargs)
    assert exp["faultClass"] == FAULT_CLASS_RATE_LIMIT
    assert exp["reason"] == FAULT_REASON_RATE_LIMIT
    assert exp["rateLimit"]["evidence"]
    assert classify_fault(error_text=_RL_ERROR, exit_code=1,
                          **kwargs) == FAULT_CLASS_RATE_LIMIT


def test_rate_limit_wins_over_the_watchdog_and_the_signature_rungs():
    """Never infra: the rung sits before no_traffic_timeout and the
    `backpressure` signature that matches the same text."""
    assert classify_fault(error_text=_RL_ERROR, no_traffic_timeout=True,
                          error_type="rate_limit") == FAULT_CLASS_RATE_LIMIT
    assert classify_fault(error_text="429 rate limit", exit_code=1,
                          produced_traffic=True,
                          error_type="rate_limit") == FAULT_CLASS_RATE_LIMIT


def test_an_abort_is_never_waited_out_as_a_rate_limit():
    assert classify_fault(error_text="aborted", operator_abort=True,
                          error_type="rate_limit") == "work"


@pytest.mark.parametrize("error_text", [
    "429 Too Many Requests",
    "rate limit exceeded",
    _RL_ERROR,
])
def test_a_pi_text_429_stays_infra(error_text):
    """pi reports errors as text only, with no structured fields: its 429
    stays an infra `backpressure` fault, as before."""
    assert classify_fault(error_text=error_text, exit_code=0) == "infra"
    assert classify_fault(error_text=error_text, exit_code=1,
                          produced_traffic=True) == "infra"


def test_a_stale_or_unrejected_report_is_not_evidence():
    # carried over from an earlier iteration: not fresh
    assert classify_fault(error_text="boom", exit_code=1, produced_traffic=True,
                          rate_limit=_REJECTED,
                          rate_limit_fresh=False) == "work"
    # fresh but allowed
    allowed = dict(_REJECTED, status="allowed_warning")
    assert classify_fault(error_text="boom", exit_code=1, produced_traffic=True,
                          rate_limit=allowed, rate_limit_fresh=True) == "work"
    # a 5xx with an error type is not a rate limit
    assert classify_fault(error_text="API Error: 500", exit_code=1,
                          http_status=500, error_type="server_error") == "infra"


def test_a_rejected_report_on_a_clean_iteration_is_not_a_failure():
    assert classify_fault(exit_code=0, produced_traffic=True,
                          rate_limit=_REJECTED, rate_limit_fresh=True) is None


@pytest.mark.parametrize("value,expected", [
    (1790936400, 1790936400.0),
    (1790936400.5, 1790936400.5),
    ("1790936400", 1790936400.0),
    (1790936400000, 1790936400.0),           # milliseconds
    ("2026-10-02T15:00:00Z", 1790953200.0),
    ("2026-10-02T17:00:00+02:00", 1790953200.0),
    ("2026-10-02T15:00:00", 1790953200.0),   # no offset: UTC
    (None, None), ("", None), ("soon", None), (True, None), (-5, None),
])
def test_parse_resets_at_accepts_epoch_and_iso(value, expected):
    assert parse_resets_at(value) == expected


def test_the_window_for_the_reported_type_decides_reset_and_utilization():
    info = {"status": "allowed_warning", "resetsAt": 100, "rateLimitType": "five_hour",
            "unifiedWindows": {"five_hour": {"utilization": 0.96, "resetsAt": 200},
                               "seven_day": {"utilization": 0.4, "resetsAt": 300}}}
    assert rate_limit_resets_at(info) == 200.0
    assert effective_utilization(info) == 0.96
    # a top-level utilization wins
    assert effective_utilization(dict(info, utilization=0.5)) == 0.5
    # no window for the type: top-level reset, max over the windows
    other = dict(info, rateLimitType="opus")
    assert rate_limit_resets_at(other) == 100.0
    assert effective_utilization(other) == 0.96
    assert effective_utilization(None) is None
    assert rate_limit_resets_at({"status": "rejected"}) is None
