"""Unknown (unpriced) iteration cost instead of a silent $0 (task 049, #10).

Unit tests drive `PiRunner._scan_line` over the exact NDJSON shapes pi emits;
the black-box test runs the real engine with the stub `pi` reporting tokens
and no `cost` block (STUB_UNPRICED_COUNT) and reads only run-dir files.
"""

from __future__ import annotations

import json

from test_e2e import engine_factory

from ralphd.engine.runner import IterationResult, PiRunner

__all__ = ["engine_factory"]


def _line(usage: dict | None, *, text: str = "hi", stop: str | None = None) -> bytes:
    msg: dict = {"role": "assistant", "content": [{"type": "text", "text": text}]}
    if stop:
        msg["stopReason"] = stop
        msg["errorMessage"] = "Connection error."
    if usage is not None:
        msg["usage"] = usage
    return json.dumps({"type": "message_end", "message": msg}).encode()


def _scan(*lines: bytes) -> dict:
    r = IterationResult()
    for line in lines:
        PiRunner._scan_line(line, r)
    return r.usage


# --- priced ---------------------------------------------------------------

def test_priced_iteration_records_cost_and_marks_it_priced():
    usage = _scan(_line({"input": 100, "output": 10, "totalTokens": 110,
                         "cost": {"total": 0.01}}))
    assert usage["costUSD"] == 0.01
    assert usage["costPriced"] is True
    assert usage["totalTokens"] == 110


def test_provider_reported_zero_price_is_still_priced():
    """An explicit 0.0 from the provider means free, not unknown."""
    usage = _scan(_line({"input": 100, "output": 10, "totalTokens": 110,
                         "cost": {"total": 0.0}}))
    assert usage["costUSD"] == 0.0
    assert usage["costPriced"] is True


# --- unpriced with tokens -------------------------------------------------

def test_unpriced_iteration_with_tokens_has_no_cost_and_is_marked_unpriced():
    usage = _scan(_line({"input": 100, "output": 10, "totalTokens": 110}))
    assert "costUSD" not in usage, "a missing price must not be recorded as $0"
    assert usage["costPriced"] is False
    assert usage["totalTokens"] == 110


def test_empty_cost_block_with_tokens_is_unpriced():
    usage = _scan(_line({"input": 5, "output": 1, "totalTokens": 6, "cost": {}}))
    assert "costUSD" not in usage
    assert usage["costPriced"] is False


def test_mixed_messages_keep_the_priced_subtotal_flagged_as_partial():
    usage = _scan(
        _line({"input": 100, "output": 10, "totalTokens": 110, "cost": {"total": 0.02}}),
        _line({"input": 100, "output": 10, "totalTokens": 110}),
    )
    assert usage["costUSD"] == 0.02
    assert usage["costPriced"] is False, "a partial total must not read as priced"
    assert usage["totalTokens"] == 220


def test_unpriced_first_then_priced_still_partial():
    usage = _scan(
        _line({"input": 100, "output": 10, "totalTokens": 110}),
        _line({"input": 100, "output": 10, "totalTokens": 110, "cost": {"total": 0.02}}),
    )
    assert usage["costUSD"] == 0.02
    assert usage["costPriced"] is False


# --- no traffic: byte-for-byte unchanged ---------------------------------

def test_no_traffic_at_all_records_no_usage():
    r = IterationResult()
    assert PiRunner._scan_line(b"not json", r) is False
    assert r.usage == {}


def test_zero_token_inband_error_keeps_the_historical_zero_cost():
    """pi zero-fills `usage` on an in-band error: nothing was billed, so $0
    is the truth and the recorded value stays exactly what it always was
    (int 0, no unpriced marker)."""
    usage = _scan(_line({"input": 0, "output": 0, "totalTokens": 0},
                        text="", stop="error"))
    assert usage == {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
                     "totalTokens": 0, "costUSD": 0}
    assert json.dumps(usage["costUSD"]) == "0"


def test_message_without_a_usage_block_keeps_zero_cost():
    usage = _scan(_line(None))
    assert usage["costUSD"] == 0
    assert "costPriced" not in usage


# --- black box: the real engine over the real runner ---------------------

def test_unpriced_run_records_unknown_cost_in_iteration_meta(engine_factory):
    e = engine_factory(job={"on_complete": "exit", "iterations": 6},
                       stub_env={"STUB_UNPRICED_COUNT": "99"})
    assert e.proc.wait(timeout=60) == 0

    metas = [json.loads(p.read_text())
             for p in sorted(e.run_dir.glob("iterations/*/meta.json"))]
    assert metas
    for meta in metas:
        usage = meta["usage"]
        assert usage["totalTokens"] > 0, meta
        assert usage.get("costPriced") is False, meta
        assert "costUSD" not in usage, meta

    # The run-level total is not silently inflated by a fake $0 either.
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["usage"].get("costUSD", 0) == 0
    assert status["usage"]["totalTokens"] > 0


def test_priced_run_still_records_cost_in_iteration_meta(engine_factory):
    e = engine_factory(job={"on_complete": "exit", "iterations": 6})
    assert e.proc.wait(timeout=60) == 0

    metas = [json.loads(p.read_text())
             for p in sorted(e.run_dir.glob("iterations/*/meta.json"))]
    assert metas
    for meta in metas:
        assert meta["usage"]["costUSD"] > 0, meta
        assert meta["usage"]["costPriced"] is True, meta
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["usage"]["costUSD"] > 0
