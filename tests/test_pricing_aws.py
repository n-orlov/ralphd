"""Built-in AWS Bedrock rate table (task 009, #14).

Task 052 (#10) let an operator hand-write rates; task 049 stopped an
implausible `$0` from being reported as money. Neither gives a real number for
the route this project actually runs on -- an AIGW-style gateway that bills
Bedrock list price and quotes zero -- unless somebody maintains a private rate
table by hand, which nobody does. So the rates ship *with* ralphd.

What is under test here is the table as DATA plus its wiring discipline:

* it resolves through `PricingMap` (one resolver, not two);
* every documented gateway spelling resolves to a rate;
* region prefixes keep their own -- higher -- rate instead of collapsing onto
  us-east, because this project's own route is `eu.` and 10% of a $25 run is
  not a rounding error;
* an id the table does not know resolves to *nothing* (unavailable beats a
  guess);
* the mirror carries a machine-readable as-of date and a staleness signal, and
  a missing/unparseable date is a hard error rather than a shrug;
* the generated region really is generated: `tools/refresh_bedrock_rates.py`
  reproduces it from pi-ai's bundled provider data.

Nothing here decides *when* the table is consulted (`price_strategy`, task 010)
or how a derived cost is published (task 011).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import inspect
import json
from pathlib import Path

import pytest

from ralphd.engine import pricing_aws as aws
from ralphd.engine.pricing import PricingMap

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "refresh_bedrock_rates.py"

# The verbatim usage block of iteration 1 of the run that motivated #14:
# 505628 tokens, quoted cost 0 (see artifacts/reports/pricing-anomaly.md).
RUN_USAGE = {"input": 32, "output": 18320, "cacheRead": 438945, "cacheWrite": 48331}
RUN_MODEL = "amazon-bedrock/eu.anthropic.claude-opus-5"


def _tool():
    """Import the refresh script by path: it is a tool, not an installed module."""
    spec = importlib.util.spec_from_file_location("refresh_bedrock_rates", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# every documented gateway form resolves to a rate
# --------------------------------------------------------------------------

# (model id as pi/the gateway reports it, expected input rate USD/Mtok)
GATEWAY_FORMS = [
    # this run's own route, exactly as `iterations/*/meta.json` records it
    ("amazon-bedrock/eu.anthropic.claude-opus-5", 5.5),
    ("amazon-bedrock/us.anthropic.claude-sonnet-5", 2.0),
    ("amazon-bedrock/amazon.nova-pro-v1:0", 0.8),
    # the AIGW spellings from the pricing-anomaly report: same model, two
    # provider prefixes, one price
    ("aigw-openai/openai.gpt-5.6-sol", 4.4),
    ("bedrock-mantle/openai.gpt-5.6-sol", 4.4),
    # bare region-prefixed Bedrock ids (no provider segment at all)
    ("eu.anthropic.claude-sonnet-5", 2.2),
    ("us.anthropic.claude-opus-5", 5.0),
    ("global.anthropic.claude-opus-5", 5.0),
    ("jp.anthropic.claude-sonnet-5", 2.0),
    ("au.anthropic.claude-opus-4-6-v1", 16.5),
    # the slash spelling docs/api.md uses for alias targets
    ("anthropic/claude-sonnet-5", 2.0),
    ("openai/gpt-5.6-sol", 4.4),
    ("amazon-bedrock/anthropic/claude-sonnet-5", 2.0),
]


@pytest.mark.parametrize(("model", "input_rate"), GATEWAY_FORMS)
def test_every_documented_gateway_form_resolves_to_a_rate(model, input_rate):
    rate = aws.rate_for(model)
    assert rate is not None, f"{model} resolves to no rate"
    assert rate.input == pytest.approx(input_rate)
    assert rate.output > rate.input  # sanity: output is never cheaper


def test_every_provider_prefix_is_aliased_away_mechanically():
    """A new gateway provider is a tuple entry, not a new code path."""
    for provider in aws.PROVIDER_PREFIXES:
        rate = aws.rate_for(f"{provider}/eu.anthropic.claude-opus-5")
        assert rate is not None and rate.input == pytest.approx(5.5), provider


def test_the_run_that_motivated_the_issue_now_derives_real_money():
    """The zero-quote iteration of THIS project's run becomes a number.

    Not an assertion about the exact bill -- an assertion that the table plus
    the existing `derive()` arithmetic turn a 505k-token iteration from
    `unknown` into ~$1, i.e. ~$25 over a run of this size.
    """
    derived = aws.pricing_map().derive(RUN_USAGE, RUN_MODEL)
    assert derived is not None
    assert 0.5 < derived < 3.0, derived
    # every counter contributes at its own rate, cacheRead included
    expected = (
        32 * 5.5 + 18320 * 27.5 + 438945 * 0.55 + 48331 * 6.875
    ) / 1_000_000
    assert derived == pytest.approx(round(expected, 6))


def test_a_hand_computed_bill_matches_the_refreshed_gpt_5_6_sol_rate():
    """Task 020's pi-ai 0.85.1 refresh moved `openai.gpt-5.6-sol`'s input rate
    from 5.5 to 4.4 USD/Mtok (output 22.0, cacheRead 0.44, cacheWrite 5.5, all
    read off the shipped table itself below -- this pins the refreshed number,
    not the pre-refresh one).

    Hand computation for 1,000,000 input + 500,000 output + 200,000 cacheRead
    + 100,000 cacheWrite tokens, at $/Mtok input=4.4 output=22.0 cacheRead=0.44
    cacheWrite=5.5:

        input:      1,000,000 * 4.4  / 1_000_000 = 4.4
        output:       500,000 * 22.0 / 1_000_000 = 11.0
        cacheRead:    200,000 * 0.44 / 1_000_000 = 0.088
        cacheWrite:   100,000 * 5.5  / 1_000_000 = 0.55
        total = 4.4 + 11.0 + 0.088 + 0.55 = 16.038
    """
    rate = aws.rate_for("openai.gpt-5.6-sol")
    assert rate is not None
    assert (rate.input, rate.output, rate.cache_read, rate.cache_write) == (
        4.4, 22.0, 0.44, 5.5,
    )
    usage = {"input": 1_000_000, "output": 500_000,
             "cacheRead": 200_000, "cacheWrite": 100_000}
    derived = aws.pricing_map().derive(usage, "openai.gpt-5.6-sol")
    assert derived == pytest.approx(16.038)


# --------------------------------------------------------------------------
# regions are not collapsed, unknown ids are not guessed
# --------------------------------------------------------------------------


def test_region_prefixes_keep_their_own_rate():
    """EU is ~10% above us-east; `au.` opus-4-6 is >3x it. Collapsing lies."""
    eu = aws.rate_for("eu.anthropic.claude-opus-5")
    us = aws.rate_for("us.anthropic.claude-opus-5")
    assert eu.input > us.input
    assert eu.input == pytest.approx(us.input * 1.1)
    assert eu.output == pytest.approx(27.5)
    assert aws.rate_for("au.anthropic.claude-opus-4-6-v1").input > 3 * us.input


@pytest.mark.parametrize("model", [
    "aigw-openai/openai.gpt-9-does-not-exist",
    "amazon-bedrock/eu.anthropic.claude-opus-99",
    "anthropic/claude-opus-5",            # no region-less entry exists upstream
    "ollama/llama3",
    "my-gateway/big-model",
    "",
    None,
])
def test_an_unknown_id_resolves_to_nothing_rather_than_a_neighbour(model):
    assert aws.rate_for(model) is None


def test_the_table_never_prices_a_cache_counter_at_zero():
    """A `0` cache rate in the mirror is dropped, so it falls back to `input`."""
    for model, entry in aws.RATES.items():
        assert entry.get("cacheRead") != 0, model
        assert entry.get("cacheWrite") != 0, model
        assert entry.get("input", 0) > 0, model
        assert entry.get("output", 0) > 0, model
    # dropped, not stored: nova has no cacheWrite tier, so writes cost input
    rate = aws.rate_for("amazon.nova-pro-v1:0")
    assert rate.per_mtok("cacheWrite") == pytest.approx(rate.input)
    assert rate.per_mtok("cacheRead") == pytest.approx(0.2)


def test_table_keys_are_bedrock_ids_not_gateway_ids():
    """Keys carry no provider segment: that is what `ALIASES` is for."""
    for model in aws.RATES:
        assert "/" not in model, model
        assert "." in model, model
    assert len(aws.RATES) > 50


# --------------------------------------------------------------------------
# one resolver, not two
# --------------------------------------------------------------------------


def test_the_table_is_exposed_as_a_pricing_map():
    built = aws.pricing_map()
    assert isinstance(built, PricingMap)
    assert built is aws.pricing_map()  # built once, treated read-only
    assert built.models and built.aliases
    assert not built.free  # "free" is an operator declaration, never built in


def test_the_module_defines_no_second_resolver():
    """Matching rules live in `pricing.py`; this module is data plus wiring."""
    src = inspect.getsource(aws)
    for smell in ("def _match", "def canonical", "fnmatch", "re.match", "startswith("):
        assert smell not in src, f"pricing_aws.py should not implement matching: {smell}"
    assert "from .pricing import PricingMap" in src


def test_pricing_map_applies_the_documented_matching_rules():
    """Longest alias pattern wins, one hop, single trailing `*` -- as documented."""
    built = aws.pricing_map()
    # longest pattern first: the vendor-qualified alias beats the bare strip
    assert built.canonical("amazon-bedrock/anthropic/claude-sonnet-5") == "anthropic.claude-sonnet-5"
    assert built.canonical("amazon-bedrock/eu.anthropic.claude-opus-5") == "eu.anthropic.claude-opus-5"
    # one hop only: an id with no alias is returned unchanged
    assert built.canonical("eu.anthropic.claude-opus-5") == "eu.anthropic.claude-opus-5"


# --------------------------------------------------------------------------
# as-of date and staleness signal
# --------------------------------------------------------------------------


def test_the_table_carries_a_machine_readable_as_of_date():
    """A rate table with no as-of date is a future lie."""
    assert aws.AS_OF, "pricing_aws.AS_OF must be set"
    assert aws.as_of_date() == dt.date.fromisoformat(aws.AS_OF)
    assert aws.as_of_date() <= dt.datetime.now(dt.UTC).date()
    assert aws.STALE_AFTER_DAYS > 0


@pytest.mark.parametrize("bad", ["", "   ", "not-a-date", "2026-13-40", "today"])
def test_a_missing_or_unparseable_as_of_date_is_an_error(monkeypatch, bad):
    monkeypatch.setattr(aws, "AS_OF", bad)
    with pytest.raises(ValueError):
        aws.as_of_date()
    # ... and shows up as data, not a crash, on the reporting surface
    signal = aws.staleness()
    assert signal["asOfValid"] is False
    assert signal["stale"] is True          # unknown age is not fresh
    assert signal["ageDays"] is None
    assert aws.age_days() is None
    assert aws.is_stale() is True


def test_the_staleness_signal_flips_at_the_documented_threshold():
    as_of = aws.as_of_date()
    fresh = as_of + dt.timedelta(days=aws.STALE_AFTER_DAYS)
    stale = as_of + dt.timedelta(days=aws.STALE_AFTER_DAYS + 1)
    assert aws.age_days(fresh) == aws.STALE_AFTER_DAYS
    assert aws.is_stale(fresh) is False
    assert aws.is_stale(stale) is True
    assert aws.staleness(stale)["stale"] is True
    assert aws.staleness(fresh)["stale"] is False


def test_the_staleness_signal_names_its_provenance_and_refresh_path():
    signal = aws.staleness()
    assert signal["table"] == aws.TABLE_NAME == "builtin-aws-bedrock"
    assert signal["models"] == len(aws.RATES)
    assert "pi-ai" in signal["source"]
    assert signal["sourceUrl"].startswith("https://aws.amazon.com/bedrock/pricing")
    assert "@earendil-works/pi-ai@" in signal["sourceVersion"]
    # the refresh command is a real, runnable path in this repo
    script = signal["refresh"].split()[-1]
    assert (REPO / script).is_file(), signal["refresh"]
    described = aws.describe()
    assert described["table"] == aws.TABLE_NAME
    assert described["aliases"] == len(aws.ALIASES)
    assert json.dumps(described)  # JSON-serialisable: it goes into GET /config


def test_a_stale_table_warns_when_the_map_is_built(monkeypatch, caplog):
    monkeypatch.setattr(aws, "AS_OF", "2000-01-01")
    aws.pricing_map.cache_clear()
    try:
        with caplog.at_level("WARNING", logger="ralphd.pricing"):
            aws.pricing_map()
        assert any("stale" in r.getMessage() for r in caplog.records)
    finally:
        aws.pricing_map.cache_clear()


# --------------------------------------------------------------------------
# the generated region really is generated
# --------------------------------------------------------------------------


def test_the_rate_table_is_a_marked_generated_region():
    src = Path(inspect.getfile(aws)).read_text()
    assert src.count("# BEGIN GENERATED RATES") == 1
    assert src.count("# END GENERATED RATES") == 1
    tool = _tool()
    region = tool.generated_region(src)
    assert region, "generated region is empty"
    assert all(len(line) <= 100 for line in region)  # ruff line-length
    assert "do not hand-edit" in src.lower()


def test_the_refresh_tool_reproduces_the_shipped_table():
    """The documented refresh path, run against the exact mirror recorded in
    SOURCE_VERSION, is the one that produced what ships.

    Pinned to a checked-in copy of that pi-ai release's bundled provider data
    (`tests/data/amazon-bedrock-<version>.json`) rather than whatever pi-ai
    happens to be installed locally via `find_source()`'s autodetection: the
    sandbox's local pi-ai install and the mirror a refresh actually used are
    two different things (task 020 refreshed from an npm-packed pi-ai 0.85.1
    while this container's own pi-ai install stayed at 0.84.1), so coupling
    this assertion to whatever the ambient environment happens to have
    installed made it pass or fail on an accident of the sandbox image rather
    than on whether the shipped table is genuinely what the tool produces
    from its documented source.
    """
    tool = _tool()
    version_tag = aws.SOURCE_VERSION.rsplit("@", 1)[-1]
    fixture = REPO / "tests" / "data" / f"amazon-bedrock-{version_tag}.json"
    assert fixture.is_file(), f"no checked-in mirror for {aws.SOURCE_VERSION}: {fixture}"
    models = json.loads(fixture.read_text())[tool.API_KEY]
    rendered = tool.render_rates(models)
    shipped = tool.generated_region(Path(inspect.getfile(aws)).read_text())
    assert rendered == shipped, (
        "the shipped rate table no longer matches its source; "
        f"rerun {aws.REFRESH_CMD}"
    )


def test_the_check_guard_fails_against_a_deliberately_stale_table(tmp_path, monkeypatch):
    """The documented `--check` path is a GUARD, not an inspection: it must
    fail loudly on real drift, never skip.

    Task 021 (F.2). `test_the_refresh_tool_reproduces_the_shipped_table`
    above only proves `main(["--check"])` passes on the table this repo
    actually ships (the happy path); this node proves the other direction --
    that the same guard, run against a shipped file whose generated region
    has been deliberately mutated to disagree with its own mirror, exits
    non-zero rather than exiting 0 or being skipped for lack of an
    environment probe. `tool.TARGET` is monkeypatched to a throwaway file
    (never the real `pricing_aws.py`) so nothing in the live tree is
    touched.
    """
    tool = _tool()
    version_tag = aws.SOURCE_VERSION.rsplit("@", 1)[-1]
    fixture = REPO / "tests" / "data" / f"amazon-bedrock-{version_tag}.json"
    real = Path(inspect.getfile(aws)).read_text()
    stale = real.replace('"input": 4.4', '"input": 999.0', 1)
    assert stale != real, "the substring this test stales is no longer present"
    staged = tmp_path / "pricing_aws.py"
    staged.write_text(stale)
    monkeypatch.setattr(tool, "TARGET", staged)

    rc = tool.main(["--check", str(fixture)])

    assert rc == 1, "the guard must fail (non-zero exit), not pass or skip, on a stale table"


def test_the_check_guard_passes_with_no_positional_source_against_the_shipped_table(monkeypatch):
    """PRD F, Definition of done predicate 6: a source-less `--check` must
    exit 0 against the table this repo actually ships, in ANY sandbox --
    including one whose ambient pi-ai install disagrees with the version the
    shipped table was generated from (this project's own dev image ships
    pi-ai 0.84.1 while the shipped table is generated from 0.85.1).

    A source-less `--check` must validate against the checked-in fixture for
    the table's own recorded `SOURCE_VERSION`
    (`find_check_source()`), never against whatever `find_source()`'s ambient
    autodetection happens to turn up: `find_source` is monkeypatched here to
    raise, so a pass proves the fixture path actually ran rather than having
    gotten lucky because this sandbox's ambient install happened to agree.
    """
    tool = _tool()

    def _must_not_run():
        raise AssertionError(
            "find_source() (ambient autodetection) must not run for a source-less --check"
        )

    monkeypatch.setattr(tool, "find_source", _must_not_run)

    rc = tool.main(["--check"])

    assert rc == 0, "the shipped table must validate against its own recorded SOURCE_VERSION fixture"


def test_the_refresh_tool_drops_zero_cache_rates_and_wraps_long_lines():
    tool = _tool()
    lines = tool.render_rates({
        "vendor.no-cache-tier": {"cost": {"input": 1.0, "output": 2.0, "cacheWrite": 0}},
        "vendor.a-model-id-so-long-that-the-rendered-entry-cannot-fit-on-one-line-at-all":
            {"cost": {"input": 12.5, "output": 62.5, "cacheRead": 1.25, "cacheWrite": 15.625}},
        "vendor.unpriced": {"cost": {}},
    })
    body = "\n".join(lines)
    assert '"vendor.no-cache-tier": {"input": 1.0, "output": 2.0},' in body
    assert "cacheWrite" not in body.split("vendor.a-model")[0]
    assert "vendor.unpriced" not in body          # nothing to price with -> skipped
    assert all(len(line) <= 100 for line in lines)
    assert any(line.endswith(":") for line in lines)  # long entry wrapped


def test_the_refresh_tool_rewrites_only_the_generated_region(tmp_path):
    tool = _tool()
    original = Path(inspect.getfile(aws)).read_text()
    updated = tool.rewrite(original, ['    "vendor.only": {"input": 1.0, "output": 2.0},'],
                           "2099-01-02", "@earendil-works/pi-ai@9.9.9")
    assert 'AS_OF = "2099-01-02"' in updated
    assert 'SOURCE_VERSION = "@earendil-works/pi-ai@9.9.9"' in updated
    assert tool.generated_region(updated) == ['    "vendor.only": {"input": 1.0, "output": 2.0},']
    # everything outside the region survives verbatim
    for keep in ("def as_of_date", "PROVIDER_PREFIXES", "STALE_AFTER_DAYS", "def staleness"):
        assert keep in updated
    assert original.count("AS_OF = ") == updated.count("AS_OF = ") == 1


def test_the_refresh_path_is_documented_for_an_operator():
    cli_doc = (REPO / "docs" / "cli.md").read_text()
    assert "tools/refresh_bedrock_rates.py" in cli_doc
    assert "as-of" in cli_doc.lower()
    assert "builtin-aws-bedrock" in cli_doc
    # the doc points at the constant rather than repeating the date, so a
    # refresh cannot rot the prose
    assert aws.AS_OF not in cli_doc
