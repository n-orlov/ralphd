"""Unit tests for the pure fault classifier (task 001a).

`classify_fault` is a pure function over an iteration's failure signal --
no engine/loop/runner state involved -- so these tests call it directly
with hand-built signal combinations.
"""

from __future__ import annotations

from ralphd.engine.faults import classify_fault


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


def test_interrupted_with_traffic_and_no_infra_text_classifies_work():
    assert classify_fault(
        error_text="",
        interrupted=True,
        produced_traffic=True,
    ) == "work"
