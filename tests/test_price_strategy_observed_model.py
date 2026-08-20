"""Pricing an UNPINNED run from the model pi reported (task 050, #14).

Task 011 made `price_strategy: aws` derive money for an unpriced route, and
task 012 started recording the model pi actually resolved. Between the two sat
the defect this module pins: the rate lookup keyed on the ref the *engine
requested*, and an operator who pins nothing (`cfg.model_for(phase)` is None,
pi picks its own model) requests nothing -- so on the only route the whole #14
thread exists for, `aws` could never fire and every surface said `unavailable`
while `status.json` cheerfully named `amazon-bedrock/eu.anthropic.claude-opus-5`
two lines up.

The rule, asserted below:

* nothing pinned -> price against the id pi reported on its own
  `message_end` (task 012's observation, now used and not merely recorded);
* something pinned -> the pinned ref decides, even when it resolves to no
  rate at all. An operator naming a ref is choosing which rate applies, and
  an unknown pinned ref must stay `unavailable` rather than quietly borrowing
  the rate of whatever the gateway happened to route to;
* nothing pinned and nothing reported -> still `unavailable`, byte-identical
  to the pre-050 output (ignorance is not a third pricing mode).

The frozen byte-equality fixtures of `test_price_strategy_derive.py` are
re-asserted here through the no-model-named path, because the fallback lives on
exactly the line those fixtures pass through.
"""

from __future__ import annotations

import json

import pytest
from test_e2e import EngineProc
from test_price_strategy_derive import (
    AWS_DERIVED_USD,
    GATEWAY_MODEL,
    ITER1_NO_COST,
    ITER1_ZERO_QUOTE,
    NONE_ZERO_QUOTE_NO_MAP,
    OPERATOR_DERIVED_USD,
    OPERATOR_MAP,
)

from ralphd.engine import pricing_aws as aws
from ralphd.engine.pricing import resolve_pricing
from ralphd.engine.runner import IterationResult, PiRunner
from ralphd.engine.state import COST_UNAVAILABLE, format_cost, model_ids

# The gateway-shaped pair pi reports for this project's own route: the two
# halves that `state.model_ids` joins into GATEWAY_MODEL.
OBSERVED_PROVIDER, OBSERVED_MODEL = GATEWAY_MODEL.split("/", 1)
# A second Bedrock id in the built-in table with deliberately different rates,
# so "which id was priced" is visible in the number rather than inferred.
OTHER_MODEL = "amazon.nova-lite-v1:0"
UNKNOWN_REF = "mystery/model-9"


def _scan(usage: dict, source, model: str | None = None,
          provider: str | None = OBSERVED_PROVIDER,
          reported: str | None = OBSERVED_MODEL) -> dict:
    """One pi `message_end` through the real scanner.

    `model` is what the ENGINE requested (None for an unpinned run);
    `provider`/`reported` are what pi says it actually used.
    """
    message = {"role": "assistant", "content": [{"type": "text", "text": "hi"}],
               "usage": usage}
    if provider is not None:
        message["provider"] = provider
    if reported is not None:
        message["model"] = reported
    line = json.dumps({"type": "message_end", "message": message}).encode()
    result = IterationResult()
    PiRunner._scan_line(line, result, pricing=source, model=model)
    return result.usage


# --------------------------------------------------------------------------
# the observed id prices an unpinned run
# --------------------------------------------------------------------------

def test_the_pair_pi_reports_is_the_route_this_run_actually_used():
    # guard the fixture itself: the two halves must join into the id the
    # built-in table knows, or the tests below would pass vacuously
    assert model_ids(OBSERVED_PROVIDER, OBSERVED_MODEL) == (GATEWAY_MODEL,
                                                            OBSERVED_MODEL)
    assert aws.pricing_map().rate_for(GATEWAY_MODEL) is not None


def test_an_unpinned_zero_quoting_run_derives_from_the_observed_id():
    """The headline defect: nothing pinned, `aws` on, gateway quotes $0."""
    usage = _scan(ITER1_ZERO_QUOTE, resolve_pricing({}, "aws"), model=None)
    assert usage["costDerivedUSD"] == AWS_DERIVED_USD
    assert usage["costDerived"] is True
    assert usage["costPriced"] is False
    assert usage["costZeroQuoted"] is True
    assert "costUSD" not in usage


def test_an_unpinned_run_with_no_cost_block_derives_from_the_observed_id():
    # task 052's original absent-cost shape takes the same fallback
    usage = _scan(ITER1_NO_COST, resolve_pricing({}, "aws"), model=None)
    assert usage["costDerivedUSD"] == AWS_DERIVED_USD
    assert usage["costDerived"] is True
    assert "costZeroQuoted" not in usage


def test_the_operator_map_still_wins_over_the_observed_id():
    usage = _scan(ITER1_ZERO_QUOTE, resolve_pricing(OPERATOR_MAP, "aws"),
                  model=None)
    assert usage["costDerivedUSD"] == OPERATOR_DERIVED_USD


def test_a_route_the_observed_id_declares_free_keeps_its_honest_zero():
    free = {"free": [GATEWAY_MODEL]}
    usage = _scan(ITER1_ZERO_QUOTE, resolve_pricing(free, "aws"), model=None)
    assert usage["costUSD"] == 0
    assert usage["costPriced"] is True
    assert usage["costFree"] is True
    assert "costDerivedUSD" not in usage


# --------------------------------------------------------------------------
# a pinned ref still decides
# --------------------------------------------------------------------------

def test_a_pinned_ref_outranks_the_observed_id():
    source = resolve_pricing({}, "aws")
    other = source.derive(ITER1_ZERO_QUOTE, OTHER_MODEL)
    assert other is not None and other != AWS_DERIVED_USD  # the fixture is a fork
    # pi reports the cheap model, the operator pinned the expensive one: the
    # pinned ref is the one that gets priced
    usage = _scan(ITER1_ZERO_QUOTE, source, model=GATEWAY_MODEL,
                  reported=OTHER_MODEL, provider="amazon-bedrock")
    assert usage["costDerivedUSD"] == AWS_DERIVED_USD


def test_a_pinned_but_unknown_ref_stays_unavailable():
    """No silent fallback to the observed id: the operator asked for a ref
    nothing can price, and `unavailable` is the honest answer."""
    usage = _scan(ITER1_ZERO_QUOTE, resolve_pricing({}, "aws"),
                  model=UNKNOWN_REF)
    assert usage["costDerived"] is False
    assert "costDerivedUSD" not in usage
    assert format_cost(usage, decimals=4) == COST_UNAVAILABLE


def test_a_pinned_ref_declared_free_is_not_overridden_by_the_observed_id():
    free = {"free": [UNKNOWN_REF]}
    usage = _scan(ITER1_ZERO_QUOTE, resolve_pricing(free, "aws"),
                  model=UNKNOWN_REF)
    assert usage["costFree"] is True
    assert usage["costUSD"] == 0


# --------------------------------------------------------------------------
# nothing observed changes nothing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("strategy", ["none", "aws"])
def test_a_message_naming_no_model_is_byte_identical_to_pre_050(strategy):
    """The fallback lives on the line the frozen fixtures pass through, so it
    has to be a no-op when there is nothing to fall back to."""
    usage = _scan(ITER1_ZERO_QUOTE, resolve_pricing({}, strategy), model=None,
                  provider=None, reported=None)
    assert json.dumps(usage, sort_keys=True) == NONE_ZERO_QUOTE_NO_MAP


def test_the_second_message_of_an_iteration_reuses_the_observed_id():
    """pi names the model on the messages that have one; a later message
    without one must still be priced against what this iteration observed."""
    source = resolve_pricing({}, "aws")
    result = IterationResult()
    for message in ({"role": "assistant", "provider": OBSERVED_PROVIDER,
                     "model": OBSERVED_MODEL, "usage": ITER1_ZERO_QUOTE},
                    {"role": "assistant", "usage": ITER1_ZERO_QUOTE}):
        PiRunner._scan_line(
            json.dumps({"type": "message_end", "message": message}).encode(),
            result, pricing=source, model=None)
    assert result.usage["costDerivedUSD"] == round(2 * AWS_DERIVED_USD, 6)
    assert result.usage["costDerived"] is True


# --------------------------------------------------------------------------
# end to end: an unpinned engine run
# --------------------------------------------------------------------------

@pytest.fixture
def observed_engine(tmp_path):
    procs: list[EngineProc] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> EngineProc:
        # NO `model` key at all: exactly the job.yaml of a run that lets pi
        # choose, which is where the defect lived.
        defaults = {"run_id": "observed-model-e2e", "iterations": 6,
                    "max_approaches": 1, "on_complete": "exit",
                    "price_strategy": "aws"}
        env = {"STUB_ZERO_COST_COUNT": "99",
               "STUB_MODEL_PROVIDER": OBSERVED_PROVIDER,
               "STUB_MODEL": OBSERVED_MODEL, **(stub_env or {})}
        e = EngineProc(tmp_path, {**defaults, **(job or {})}, env)
        procs.append(e)
        return e

    yield make
    for e in procs:
        e.stop()


def test_an_unpinned_engine_run_reports_derived_money(observed_engine):
    e = observed_engine()
    assert e.proc.wait(timeout=60) == 0

    metas = [json.loads(p.read_text())
             for p in sorted(e.run_dir.glob("iterations/*/meta.json"))]
    assert metas
    for meta in metas:
        assert meta["model"] is None, meta          # nothing was requested
        assert meta["modelResolved"] == GATEWAY_MODEL, meta
        assert meta["usage"]["costDerivedUSD"] > 0, meta
        assert meta["usage"]["costDerived"] is True, meta
        assert meta["usage"]["costPriced"] is False, meta

    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["model"] == GATEWAY_MODEL
    usage = status["usage"]
    assert usage["costStatus"] == "derived"
    assert usage["costDerivedUSD"] > 0
    assert "costUSD" not in usage
    rendered = format_cost(usage, decimals=4)
    assert rendered.startswith("~$") and rendered.endswith("derived")


def test_an_engine_run_pinning_an_unknown_ref_stays_unavailable(observed_engine):
    e = observed_engine({"model": UNKNOWN_REF})
    assert e.proc.wait(timeout=60) == 0
    status = json.loads((e.run_dir / "status.json").read_text())
    # the observed id is recorded (task 012) but deliberately NOT priced
    assert status["model"] == GATEWAY_MODEL
    usage = status["usage"]
    assert usage["costStatus"] == "unknown"
    assert "costDerivedUSD" not in usage
    assert format_cost(usage, decimals=4) == COST_UNAVAILABLE
