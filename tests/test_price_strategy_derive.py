"""Deriving a cost from the built-in AWS Bedrock table (task 011, #14).

Task 009 shipped the table, task 010 shipped the `price_strategy` knob that
decides whether it may be consulted, and task 049 stopped believing the
gateway's implausible `$0`. This task connects the three: with
`price_strategy: aws`, a route the provider did not price -- *including* one
it "priced" with a zero next to half a million billed tokens, which is the
route this whole thread of work exists for -- is costed from the built-in
table and published as DERIVED money.

The invariants asserted here, in the order they matter:

* the operator's own `pricing:` map always wins. A rate an operator typed for
  THEIR gateway beats a shipped table's idea of the same id, so the two are
  composed as an ordered `PricingChain`, never merged into one rate dict --
  merging would lose which table answered.
* both triggers derive: an *absent* cost block (task 052's original case) and
  an *implausible zero* one (task 049 / steering 001). The zero-quote route is
  the only one the AIGW gateway actually produced, so a derivation that fired
  only on absence would never fire at all in production.
* an unknown model id under `aws` still reports `unavailable`. A neighbouring
  rate is a guess, and a guess is what `format_cost` exists to refuse.
* `price_strategy: none` is byte-identical to pre-v0.6 ralphd. Frozen usage
  payloads below pin that as bytes, not as a promise in a docstring.
* every surface can say WHICH table produced a rate: `PricingMap.describe()`
  and `PricingChain.describe()` name themselves, `table_for()` names the layer
  that answered, and `GET /config`'s `priceTables` lists them in precedence
  order (`"operator map, then builtin-aws-bedrock"`, or `"neither"`).
"""

from __future__ import annotations

import json

import pytest
from test_e2e import EngineProc

from ralphd.engine import pricing_aws as aws
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.pricing import (
    NO_TABLE,
    OPERATOR_TABLE,
    PricingChain,
    PricingMap,
    price_tables,
    resolve_pricing,
    wants_aws,
)
from ralphd.engine.runner import IterationResult, PiRunner
from ralphd.engine.state import COST_UNAVAILABLE, RunDir, cost_status, format_cost

# The verbatim iteration-1 usage of the v0.6 self-development run: a zero money
# quote next to 505 628 billed tokens (artifacts/reports/pricing-anomaly.md).
ITER1_ZERO_QUOTE = {"input": 32, "output": 18320, "cacheRead": 438945,
                    "cacheWrite": 48331, "totalTokens": 505628,
                    "cost": {"total": 0}}
# The same tokens with no `cost` block at all -- task 052's original case.
ITER1_NO_COST = {k: v for k, v in ITER1_ZERO_QUOTE.items() if k != "cost"}
# ...and the model id that produced them, in the gateway's own spelling.
GATEWAY_MODEL = "amazon-bedrock/eu.anthropic.claude-opus-5"
# What the built-in table says those tokens cost (us-east opus-5 rates + the
# EU premium; NOT collapsed to a region-less id -- see task 009).
AWS_DERIVED_USD = 1.077671

# An operator map covering the same route at deliberately different rates, so
# "whose number came out" is visible in the result rather than inferred.
OPERATOR_MAP = {"aliases": {"amazon-bedrock/*": "*"},
                "models": {"eu.anthropic.claude-opus-5": {
                    "input": 1.0, "output": 1.0,
                    "cacheRead": 0.1, "cacheWrite": 1.25}}}
OPERATOR_DERIVED_USD = 0.12266

# Frozen pre-v0.6 output for `price_strategy: none` (captured from the code as
# it stood before task 011). Byte equality against these strings is the whole
# assertion: the default path must not gain, lose or reorder a single key.
NONE_ZERO_QUOTE_NO_MAP = (
    '{"cacheRead": 438945, "cacheWrite": 48331, "costDerived": false, '
    '"costPriced": false, "costZeroQuoted": true, "input": 32, '
    '"output": 18320, "totalTokens": 505628}')
NONE_ZERO_QUOTE_WITH_MAP = (
    '{"cacheRead": 438945, "cacheWrite": 48331, "costDerived": true, '
    '"costDerivedUSD": 0.12266, "costPriced": false, "costZeroQuoted": true, '
    '"input": 32, "output": 18320, "totalTokens": 505628}')
NONE_NO_COST_NO_MAP = (
    '{"cacheRead": 438945, "cacheWrite": 48331, "costDerived": false, '
    '"costPriced": false, "input": 32, "output": 18320, "totalTokens": 505628}')


def _scan(usage: dict, source, model: str | None = GATEWAY_MODEL) -> dict:
    """One pi `message_end` event through the real runner scanner."""
    line = json.dumps({"type": "message_end", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "hi"}],
        "usage": usage}}).encode()
    result = IterationResult()
    PiRunner._scan_line(line, result, pricing=source, model=model)
    return result.usage


def _scan_json(usage: dict, source, model: str | None = GATEWAY_MODEL) -> str:
    return json.dumps(_scan(usage, source, model), sort_keys=True)


# --------------------------------------------------------------------------
# resolve_pricing: what a run's rate source actually is
# --------------------------------------------------------------------------

def test_the_default_strategy_resolves_to_the_operator_map_alone():
    # Not a chain, not a wrapper: literally what pre-v0.6 ralphd passed to the
    # runner, so the default path cannot behave differently by construction.
    assert resolve_pricing({}, "none") is None
    source = resolve_pricing(OPERATOR_MAP, "none")
    assert isinstance(source, PricingMap)
    assert source == PricingMap.from_config(OPERATOR_MAP)


def test_no_strategy_argument_at_all_still_means_none():
    assert resolve_pricing({}) is None
    assert isinstance(resolve_pricing(OPERATOR_MAP), PricingMap)


def test_aws_without_an_operator_map_resolves_to_the_builtin_table():
    source = resolve_pricing({}, "aws")
    assert isinstance(source, PricingChain)
    assert [layer.name for layer in source.layers] == [aws.TABLE_NAME]


def test_aws_with_an_operator_map_puts_the_operator_first():
    source = resolve_pricing(OPERATOR_MAP, "aws")
    assert isinstance(source, PricingChain)
    assert [layer.name for layer in source.layers] == [OPERATOR_TABLE,
                                                      aws.TABLE_NAME]
    assert source.name == f"{OPERATOR_TABLE}, then {aws.TABLE_NAME}"


@pytest.mark.parametrize("value", ["aws", "AWS", " aws ", "Aws"])
def test_the_strategy_name_is_matched_tolerantly(value):
    assert wants_aws(value) is True
    assert isinstance(resolve_pricing({}, value), PricingChain)


@pytest.mark.parametrize("value", [None, "", "none", "bedrock", "AWSX", 0])
def test_anything_but_aws_leaves_the_builtin_table_out(value):
    assert wants_aws(value) is False
    assert resolve_pricing({}, value) is None


def test_the_builtin_table_is_not_imported_until_it_is_asked_for(monkeypatch):
    # `none` must not even build the table (nor emit its staleness warning):
    # blow up if anything touches it on the default path.
    def explode():  # pragma: no cover - the point is that it is NOT called
        raise AssertionError("pricing_aws.pricing_map() consulted for none")

    monkeypatch.setattr(aws, "pricing_map", explode)
    assert resolve_pricing(OPERATOR_MAP, "none") is not None
    assert price_tables(OPERATOR_MAP, "none")["names"] == [OPERATOR_TABLE]


# --------------------------------------------------------------------------
# the derivation itself
# --------------------------------------------------------------------------

def test_an_implausible_zero_quote_is_derived_from_the_builtin_table():
    usage = _scan(ITER1_ZERO_QUOTE, resolve_pricing({}, "aws"))
    assert usage["costDerivedUSD"] == AWS_DERIVED_USD
    assert usage["costDerived"] is True
    # a derived number is never presented as money the provider quoted
    assert usage["costPriced"] is False
    assert usage["costZeroQuoted"] is True
    assert "costUSD" not in usage
    assert cost_status(usage) == "derived"
    rendered = format_cost(usage, decimals=4)
    assert rendered.startswith("~$") and rendered.endswith("derived")
    assert "$0.00" not in rendered


def test_an_absent_cost_block_is_derived_from_the_builtin_table():
    # Steering 001: the trigger must cover BOTH shapes, not only absence.
    usage = _scan(ITER1_NO_COST, resolve_pricing({}, "aws"))
    assert usage["costDerivedUSD"] == AWS_DERIVED_USD
    assert cost_status(usage) == "derived"
    assert "costZeroQuoted" not in usage


def test_the_operator_map_wins_over_the_builtin_table():
    source = resolve_pricing(OPERATOR_MAP, "aws")
    usage = _scan(ITER1_ZERO_QUOTE, source)
    assert usage["costDerivedUSD"] == OPERATOR_DERIVED_USD
    assert usage["costDerivedUSD"] != AWS_DERIVED_USD
    assert source.table_for(GATEWAY_MODEL) == OPERATOR_TABLE


def test_the_builtin_table_covers_what_the_operator_map_does_not():
    # One route pinned by the operator, another only the shipped table knows:
    # the chain answers both, from the right layer each time.
    source = resolve_pricing(OPERATOR_MAP, "aws")
    assert source.table_for(GATEWAY_MODEL) == OPERATOR_TABLE
    assert source.table_for("us.anthropic.claude-sonnet-5") == aws.TABLE_NAME
    assert source.table_for("mystery/model-9") == NO_TABLE


def test_an_unknown_model_under_aws_stays_unavailable():
    usage = _scan(ITER1_ZERO_QUOTE, resolve_pricing({}, "aws"),
                  model="mystery/model-9")
    assert "costDerivedUSD" not in usage
    assert usage["costDerived"] is False, "a missing rate must not read as covered"
    assert cost_status(usage) == "unknown"
    assert format_cost(usage, decimals=4) == COST_UNAVAILABLE


def test_a_provider_quoted_price_is_never_replaced_by_a_derived_one():
    usage = _scan({**ITER1_ZERO_QUOTE, "cost": {"total": 0.42}},
                  resolve_pricing({}, "aws"))
    assert usage["costUSD"] == 0.42
    assert usage["costPriced"] is True
    assert "costDerivedUSD" not in usage
    assert cost_status(usage) is None


def test_a_declared_free_route_is_still_free_under_aws():
    # `free:` is a declaration and outranks any rate table, including a
    # shipped one that happens to know the id (task 049).
    source = resolve_pricing({"free": ["amazon-bedrock/*"]}, "aws")
    assert source.is_free(GATEWAY_MODEL) is True
    usage = _scan(ITER1_ZERO_QUOTE, source)
    assert usage["costUSD"] == 0.0
    assert usage["costFree"] is True
    assert "costDerivedUSD" not in usage
    assert format_cost(usage) == "$0.00"


def test_the_chain_derives_from_exactly_one_layer_never_a_sum():
    source = resolve_pricing(OPERATOR_MAP, "aws")
    assert source.derive(ITER1_ZERO_QUOTE, GATEWAY_MODEL) == OPERATOR_DERIVED_USD
    assert source.derive(ITER1_ZERO_QUOTE, GATEWAY_MODEL) \
        != OPERATOR_DERIVED_USD + AWS_DERIVED_USD
    assert source.derive(ITER1_ZERO_QUOTE, "mystery/model-9") is None
    assert source.rate_for(GATEWAY_MODEL).input == 1.0


# --------------------------------------------------------------------------
# byte equality of the default path
# --------------------------------------------------------------------------

@pytest.mark.parametrize("usage,pricing_cfg,frozen", [
    (ITER1_ZERO_QUOTE, {}, NONE_ZERO_QUOTE_NO_MAP),
    (ITER1_ZERO_QUOTE, OPERATOR_MAP, NONE_ZERO_QUOTE_WITH_MAP),
    (ITER1_NO_COST, {}, NONE_NO_COST_NO_MAP),
])
def test_price_strategy_none_output_is_byte_identical_to_pre_v06(
        usage, pricing_cfg, frozen):
    assert _scan_json(usage, resolve_pricing(pricing_cfg, "none")) == frozen


def test_the_frozen_fixtures_really_are_what_the_old_code_path_produced():
    # The frozen strings above must equal what `PricingMap.from_config` alone
    # (the pre-011 call site, still exercised by tests/test_pricing_map.py)
    # produces -- otherwise the byte-equality test is only comparing task 011
    # with itself.
    for usage, cfg, frozen in ((ITER1_ZERO_QUOTE, {}, NONE_ZERO_QUOTE_NO_MAP),
                               (ITER1_ZERO_QUOTE, OPERATOR_MAP,
                                NONE_ZERO_QUOTE_WITH_MAP),
                               (ITER1_NO_COST, {}, NONE_NO_COST_NO_MAP)):
        assert _scan_json(usage, PricingMap.from_config(cfg)) == frozen


def test_aws_changes_that_output_only_by_adding_derived_money():
    # The complement of the byte-equality test: `aws` must differ from `none`
    # in the derived fields and NOTHING else (no token counter, no marker).
    off = _scan(ITER1_ZERO_QUOTE, resolve_pricing({}, "none"))
    on = _scan(ITER1_ZERO_QUOTE, resolve_pricing({}, "aws"))
    changed = {k for k in set(off) | set(on) if off.get(k) != on.get(k)}
    assert changed == {"costDerived", "costDerivedUSD"}


# --------------------------------------------------------------------------
# naming the table that answered
# --------------------------------------------------------------------------

def test_a_pricing_map_names_itself_in_describe():
    assert PricingMap.from_config(OPERATOR_MAP).describe()["table"] == OPERATOR_TABLE
    assert aws.pricing_map().describe()["table"] == aws.TABLE_NAME
    assert aws.pricing_map().name == aws.TABLE_NAME


def test_a_chain_describes_its_layers_in_order():
    described = resolve_pricing(OPERATOR_MAP, "aws").describe()
    assert described["table"] == f"{OPERATOR_TABLE}, then {aws.TABLE_NAME}"
    assert [t["name"] for t in described["tables"]] == [OPERATOR_TABLE,
                                                       aws.TABLE_NAME]
    assert json.dumps(described)  # it goes into an API payload


def test_price_tables_names_neither_when_nothing_can_price():
    tables = price_tables({}, "none")
    assert tables == {"names": [], "answers": NO_TABLE, "tables": []}


def test_price_tables_names_the_operator_map_alone():
    tables = price_tables(OPERATOR_MAP, "none")
    assert tables["names"] == [OPERATOR_TABLE]
    assert tables["answers"] == OPERATOR_TABLE
    assert tables["tables"][0]["models"] == 1


def test_price_tables_names_the_builtin_table_with_its_staleness():
    tables = price_tables({}, "aws")
    assert tables["names"] == [aws.TABLE_NAME]
    entry = tables["tables"][0]
    assert entry["asOf"] == aws.AS_OF and entry["asOfValid"] is True
    assert entry["stale"] is False
    assert entry["models"] == len(aws.RATES)


def test_price_tables_lists_both_in_precedence_order():
    tables = price_tables(OPERATOR_MAP, "aws")
    assert tables["names"] == [OPERATOR_TABLE, aws.TABLE_NAME]
    assert tables["answers"] == f"{OPERATOR_TABLE}, then {aws.TABLE_NAME}"


def test_effective_config_carries_the_price_tables(tmp_path):
    eff = JobConfig(run_id="r", price_strategy="aws").effective()
    assert eff["priceStrategy"] == "aws"
    assert eff["priceTables"]["names"] == [aws.TABLE_NAME]
    # the operator-map view keeps its exact pre-011 meaning: null when the
    # operator configured no map, whatever the strategy says
    assert eff["pricing"] is None
    assert JobConfig(run_id="r").effective()["priceTables"]["answers"] == NO_TABLE


# --------------------------------------------------------------------------
# engine wiring
# --------------------------------------------------------------------------

def test_the_supervisor_hands_the_resolved_chain_to_the_runner(tmp_path):
    run = RunDir(root=tmp_path / "run")
    aws_sup = LoopSupervisor(JobConfig(run_id="unit", price_strategy="aws"),
                             run, tmp_path)
    assert isinstance(aws_sup.runner.pricing, PricingChain)
    assert aws_sup.runner.pricing.table_for(GATEWAY_MODEL) == aws.TABLE_NAME
    plain = LoopSupervisor(JobConfig(run_id="unit"), run, tmp_path)
    assert plain.runner.pricing is None


@pytest.fixture
def price_engine(tmp_path):
    procs: list[EngineProc] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> EngineProc:
        defaults = {"run_id": "price-derive-e2e", "iterations": 6,
                    "max_approaches": 1, "on_complete": "exit",
                    "model": GATEWAY_MODEL}
        e = EngineProc(tmp_path, {**defaults, **(job or {})}, stub_env)
        procs.append(e)
        return e

    yield make
    for e in procs:
        e.stop()


def test_a_zero_quoting_run_over_a_gateway_id_reports_derived_money(price_engine):
    """The headline case: no operator pricing map at all, `price_strategy:
    aws`, a gateway-shaped Bedrock id and a gateway that quotes $0 for real
    tokens -- exactly the v0.6 self-development run's own route."""
    e = price_engine({"price_strategy": "aws"},
                     stub_env={"STUB_ZERO_COST_COUNT": "99"})
    assert e.proc.wait(timeout=60) == 0

    metas = [json.loads(p.read_text())
             for p in sorted(e.run_dir.glob("iterations/*/meta.json"))]
    assert metas
    for meta in metas:
        usage = meta["usage"]
        assert usage["costDerivedUSD"] > 0, meta
        assert usage["costDerived"] is True, meta
        assert usage["costPriced"] is False, meta
        assert "costUSD" not in usage, meta

    usage = json.loads((e.run_dir / "status.json").read_text())["usage"]
    assert usage["costStatus"] == "derived"
    assert usage["costDerivedUSD"] > 0
    assert "costUSD" not in usage
    rendered = format_cost(usage, decimals=4)
    assert rendered.startswith("~$") and rendered.endswith("derived")


def test_the_same_run_without_the_strategy_stays_unavailable(price_engine):
    e = price_engine(stub_env={"STUB_ZERO_COST_COUNT": "99"})
    assert e.proc.wait(timeout=60) == 0
    usage = json.loads((e.run_dir / "status.json").read_text())["usage"]
    assert usage["costStatus"] == "unknown"
    assert "costDerivedUSD" not in usage
    assert format_cost(usage, decimals=4) == COST_UNAVAILABLE


def test_get_config_names_the_table_that_may_price_a_live_run(price_engine):
    e = price_engine({"price_strategy": "aws", "on_complete": "idle"})
    e.wait_api()
    status, doc = e.api("GET", "/config")
    assert status == 200
    assert doc["priceTables"]["names"] == [aws.TABLE_NAME]
    assert doc["priceTables"]["answers"] == aws.TABLE_NAME
    assert doc["priceTables"]["tables"][0]["asOf"] == aws.AS_OF
