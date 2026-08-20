"""Built-in AWS Bedrock rate table: the rates ralphd already knows (task 009, #14).

Task 052 (#10) gave operators a host-side `pricing:` map so an unpriced route
could still produce a number, and task 049 (v0.6) made an *implausible zero* --
a quoted `costUSD == 0` next to hundreds of thousands of billable tokens --
report as `unknown` instead of `$0.00`. Both are honest, and both leave the
common case unsolved: the AIGW-style gateway route this project runs on bills
exactly Bedrock list price, so the rate IS knowable without an operator
hand-writing a table they will never remember to update. Requiring one is why
`pricing:` is unset in practice, and why real runs cost "unknown".

So ralphd ships the table. This module is data plus wiring only; nothing here
decides *when* to use it (that is `price_strategy`, task 010) and nothing here
computes money (that is `PricingMap.derive`, task 011). A cost derived from this
table is still **derived**: it lands in `costDerivedUSD` with the `~... derived`
marker, never in `costUSD`, because a built-in table is not a provider quote.

Deliberate design choices, each with a test in `tests/test_pricing_aws.py`:

* **One resolver, not two.** The table is exposed as a `PricingMap`
  (`pricing_map()`), so gateway ids resolve through the exact rules operators
  already read about in `docs/cli.md`: one alias hop, exact key beats wildcard,
  longest wildcard prefix wins, single trailing `*` only, cache rates fall back
  to the input rate. A second matcher living here would drift from that one.
* **Provider prefixes are aliased away; region prefixes are NOT.** pi reports
  ids like `amazon-bedrock/eu.anthropic.claude-opus-5` and
  `aigw-openai/openai.gpt-5.6-sol`; the leading `<provider>/` segment is a pi/
  gateway artefact and carries no pricing information, so `ALIASES` strips it.
  The `eu.`/`us.`/`jp.`/... segment DOES carry pricing information -- EU sits
  ~10% above us-east and `au.anthropic.claude-opus-4-6-v1` is >3x its us-east
  twin -- so region-prefixed ids keep their own entry. Collapsing them onto the
  region-less id (the shape sketched in the PRD) would have silently mispriced
  this very project's EU route by 10%.
* **An unknown id resolves to nothing.** `rate_for` returning None is the point:
  the caller keeps the cost `unknown` rather than guessing from a neighbouring
  model or an unpriced region.
* **A zero cache rate is never stored.** The mirror carries `cacheWrite: 0` for
  models with no caching tier; storing that would price real cached tokens at
  $0, the exact lie #10/#14 exist to remove. The refresh script drops them, so
  `ModelRate.per_mtok` falls back to the input rate (overstating is recoverable;
  a silent $0 is not).
* **An as-of date, machine-readable, plus a staleness signal.** AWS changes
  prices. `AS_OF` is parsed (not just printed) by `as_of_date()`, and
  `staleness()` reports `ageDays`/`stale` so surfaces can say the table is old
  instead of quietly deriving 2026 money in 2028. A rate table with no as-of
  date is a future lie.

## Provenance and refresh

`RATES` is GENERATED -- do not hand-edit the region between the
`BEGIN/END GENERATED RATES` markers. It mirrors `pi-ai`'s bundled Bedrock
provider data (`@earendil-works/pi-ai/dist/providers/data/amazon-bedrock.json`,
key `bedrock-converse-stream`, `cost` blocks in USD per MILLION tokens), which
is what pi itself prices a request with, cross-checkable against
<https://aws.amazon.com/bedrock/pricing/>. Refresh with:

    python tools/refresh_bedrock_rates.py          # rewrites RATES + AS_OF
    python tools/refresh_bedrock_rates.py --check   # non-zero if out of date

`SOURCE_VERSION` records which pi-ai the numbers came from, `AS_OF` when they
were mirrored.
"""

from __future__ import annotations

import datetime as dt
import functools
import logging

from .pricing import PricingMap

log = logging.getLogger("ralphd.pricing")

# Name used wherever a surface has to say WHICH table produced a rate
# (`pricing.describe()`, `GET /config`); the operator map's name is
# "operator map", neither is "the pricing table".
TABLE_NAME = "builtin-aws-bedrock"

# Machine-readable, ISO-8601, and parsed by `as_of_date()` -- a missing or
# malformed value is a hard error, not a shrug (tests/test_pricing_aws.py).
AS_OF = "2026-08-20"
# How long a mirror of somebody else's price list stays credible.
STALE_AFTER_DAYS = 180

SOURCE = (
    "pi-ai bundled Bedrock provider data (@earendil-works/pi-ai/dist/providers/"
    "data/amazon-bedrock.json, bedrock-converse-stream cost blocks), mirroring "
    "AWS Bedrock on-demand list price"
)
SOURCE_VERSION = "@earendil-works/pi-ai@0.84.1"
SOURCE_URL = "https://aws.amazon.com/bedrock/pricing/"
REFRESH_CMD = "python tools/refresh_bedrock_rates.py"

# `<provider>/` segments pi and the gateways put in front of a Bedrock model id.
# Aliased away because they carry no pricing information: the same
# `openai.gpt-5.6-sol` costs the same whether pi calls the provider
# `aigw-openai` or `bedrock-mantle` (see artifacts/reports/pricing-anomaly.md,
# where one run reached that id through both).
PROVIDER_PREFIXES = (
    "amazon-bedrock",
    "bedrock",
    "aigw-openai",
    "aigw-anthropic",
    "aigw-bedrock",
    "bedrock-mantle",
)

# Vendor segments, so the slash-separated spelling of a canonical id
# (`anthropic/claude-opus-5`, the form `docs/api.md` uses for alias targets)
# resolves to the dotted Bedrock spelling this table is keyed on.
VENDOR_SEGMENTS = (
    "amazon",
    "anthropic",
    "deepseek",
    "google",
    "meta",
    "minimax",
    "mistral",
    "moonshot",
    "moonshotai",
    "nvidia",
    "openai",
    "qwen",
    "writer",
    "xai",
    "zai",
)


def _build_aliases() -> dict[str, str]:
    """Gateway/provider spellings -> the ids `RATES` is keyed on.

    Mechanical, so a new provider prefix is one tuple entry rather than a new
    branch. Every value is a `PricingMap` alias in its documented single-
    trailing-`*` form, resolved by `PricingMap.canonical` (longest pattern
    first, one hop) -- there is no second resolver here.
    """
    aliases: dict[str, str] = {}
    for provider in PROVIDER_PREFIXES:
        # `amazon-bedrock/eu.anthropic.claude-opus-5` -> `eu.anthropic.claude-opus-5`
        aliases[f"{provider}/*"] = "*"
        for vendor in VENDOR_SEGMENTS:
            # `aigw-openai/openai/gpt-5.6-sol` -> `openai.gpt-5.6-sol`
            # (longer pattern, so it wins over the bare provider strip above)
            aliases[f"{provider}/{vendor}/*"] = f"{vendor}.*"
    for vendor in VENDOR_SEGMENTS:
        # `anthropic/claude-opus-5` -> `anthropic.claude-opus-5`
        aliases[f"{vendor}/*"] = f"{vendor}.*"
    return aliases


ALIASES = _build_aliases()

# USD per MILLION tokens, keyed exactly as Bedrock (and pi) name the model,
# region prefix included where the rate differs by region.
RATES: dict[str, dict[str, float]] = {
    # BEGIN GENERATED RATES
    "amazon.nova-2-lite-v1:0": {"input": 0.33, "output": 2.75},
    "amazon.nova-lite-v1:0": {"input": 0.06, "output": 0.24, "cacheRead": 0.015},
    "amazon.nova-micro-v1:0": {"input": 0.035, "output": 0.14, "cacheRead": 0.00875},
    "amazon.nova-pro-v1:0": {"input": 0.8, "output": 3.2, "cacheRead": 0.2},
    "anthropic.claude-fable-5":
        {"input": 10.0, "output": 50.0, "cacheRead": 1.0, "cacheWrite": 12.5},
    "anthropic.claude-haiku-4-5-20251001-v1:0":
        {"input": 1.0, "output": 5.0, "cacheRead": 0.1, "cacheWrite": 1.25},
    "anthropic.claude-opus-4-1-20250805-v1:0":
        {"input": 15.0, "output": 75.0, "cacheRead": 1.5, "cacheWrite": 18.75},
    "anthropic.claude-opus-4-5-20251101-v1:0":
        {"input": 5.0, "output": 25.0, "cacheRead": 0.5, "cacheWrite": 6.25},
    "anthropic.claude-opus-4-6-v1":
        {"input": 5.0, "output": 25.0, "cacheRead": 0.5, "cacheWrite": 6.25},
    "anthropic.claude-opus-4-7":
        {"input": 5.0, "output": 25.0, "cacheRead": 0.5, "cacheWrite": 6.25},
    "anthropic.claude-opus-4-8":
        {"input": 5.0, "output": 25.0, "cacheRead": 0.5, "cacheWrite": 6.25},
    "anthropic.claude-sonnet-4-5-20250929-v1:0":
        {"input": 3.0, "output": 15.0, "cacheRead": 0.3, "cacheWrite": 3.75},
    "anthropic.claude-sonnet-4-6":
        {"input": 3.0, "output": 15.0, "cacheRead": 0.3, "cacheWrite": 3.75},
    "anthropic.claude-sonnet-5":
        {"input": 2.0, "output": 10.0, "cacheRead": 0.2, "cacheWrite": 2.5},
    "au.anthropic.claude-haiku-4-5-20251001-v1:0":
        {"input": 1.0, "output": 5.0, "cacheRead": 0.1, "cacheWrite": 1.25},
    "au.anthropic.claude-opus-4-6-v1":
        {"input": 16.5, "output": 82.5, "cacheRead": 1.65, "cacheWrite": 20.625},
    "au.anthropic.claude-opus-4-8":
        {"input": 5.0, "output": 25.0, "cacheRead": 0.5, "cacheWrite": 6.25},
    "au.anthropic.claude-opus-5":
        {"input": 5.0, "output": 25.0, "cacheRead": 0.5, "cacheWrite": 6.25},
    "au.anthropic.claude-sonnet-4-5-20250929-v1:0":
        {"input": 3.0, "output": 15.0, "cacheRead": 0.3, "cacheWrite": 3.75},
    "au.anthropic.claude-sonnet-4-6":
        {"input": 3.3, "output": 16.5, "cacheRead": 0.33, "cacheWrite": 4.125},
    "au.anthropic.claude-sonnet-5":
        {"input": 2.0, "output": 10.0, "cacheRead": 0.2, "cacheWrite": 2.5},
    "deepseek.r1-v1:0": {"input": 1.35, "output": 5.4},
    "deepseek.v3-v1:0": {"input": 0.58, "output": 1.68},
    "deepseek.v3.2": {"input": 0.62, "output": 1.85},
    "eu.anthropic.claude-fable-5":
        {"input": 11.0, "output": 55.0, "cacheRead": 1.1, "cacheWrite": 13.75},
    "eu.anthropic.claude-haiku-4-5-20251001-v1:0":
        {"input": 1.1, "output": 5.5, "cacheRead": 0.11, "cacheWrite": 1.375},
    "eu.anthropic.claude-opus-4-5-20251101-v1:0":
        {"input": 5.5, "output": 27.5, "cacheRead": 0.55, "cacheWrite": 6.875},
    "eu.anthropic.claude-opus-4-6-v1":
        {"input": 5.5, "output": 27.5, "cacheRead": 0.55, "cacheWrite": 6.875},
    "eu.anthropic.claude-opus-4-7":
        {"input": 5.5, "output": 27.5, "cacheRead": 0.55, "cacheWrite": 6.875},
    "eu.anthropic.claude-opus-4-8":
        {"input": 5.5, "output": 27.5, "cacheRead": 0.55, "cacheWrite": 6.875},
    "eu.anthropic.claude-opus-5":
        {"input": 5.5, "output": 27.5, "cacheRead": 0.55, "cacheWrite": 6.875},
    "eu.anthropic.claude-sonnet-4-5-20250929-v1:0":
        {"input": 3.3, "output": 16.5, "cacheRead": 0.33, "cacheWrite": 4.125},
    "eu.anthropic.claude-sonnet-4-6":
        {"input": 3.3, "output": 16.5, "cacheRead": 0.33, "cacheWrite": 4.125},
    "eu.anthropic.claude-sonnet-5":
        {"input": 2.2, "output": 11.0, "cacheRead": 0.22, "cacheWrite": 2.75},
    "global.anthropic.claude-fable-5":
        {"input": 10.0, "output": 50.0, "cacheRead": 1.0, "cacheWrite": 12.5},
    "global.anthropic.claude-haiku-4-5-20251001-v1:0":
        {"input": 1.0, "output": 5.0, "cacheRead": 0.1, "cacheWrite": 1.25},
    "global.anthropic.claude-opus-4-5-20251101-v1:0":
        {"input": 5.0, "output": 25.0, "cacheRead": 0.5, "cacheWrite": 6.25},
    "global.anthropic.claude-opus-4-6-v1":
        {"input": 5.0, "output": 25.0, "cacheRead": 0.5, "cacheWrite": 6.25},
    "global.anthropic.claude-opus-4-7":
        {"input": 5.0, "output": 25.0, "cacheRead": 0.5, "cacheWrite": 6.25},
    "global.anthropic.claude-opus-4-8":
        {"input": 5.0, "output": 25.0, "cacheRead": 0.5, "cacheWrite": 6.25},
    "global.anthropic.claude-opus-5":
        {"input": 5.0, "output": 25.0, "cacheRead": 0.5, "cacheWrite": 6.25},
    "global.anthropic.claude-sonnet-4-5-20250929-v1:0":
        {"input": 3.0, "output": 15.0, "cacheRead": 0.3, "cacheWrite": 3.75},
    "global.anthropic.claude-sonnet-4-6":
        {"input": 3.0, "output": 15.0, "cacheRead": 0.3, "cacheWrite": 3.75},
    "global.anthropic.claude-sonnet-5":
        {"input": 2.0, "output": 10.0, "cacheRead": 0.2, "cacheWrite": 2.5},
    "google.gemma-3-27b-it": {"input": 0.12, "output": 0.2},
    "google.gemma-3-4b-it": {"input": 0.04, "output": 0.08},
    "jp.anthropic.claude-haiku-4-5-20251001-v1:0":
        {"input": 1.0, "output": 5.0, "cacheRead": 0.1, "cacheWrite": 1.25},
    "jp.anthropic.claude-opus-4-7":
        {"input": 5.0, "output": 25.0, "cacheRead": 0.5, "cacheWrite": 6.25},
    "jp.anthropic.claude-opus-4-8":
        {"input": 5.0, "output": 25.0, "cacheRead": 0.5, "cacheWrite": 6.25},
    "jp.anthropic.claude-opus-5":
        {"input": 5.0, "output": 25.0, "cacheRead": 0.5, "cacheWrite": 6.25},
    "jp.anthropic.claude-sonnet-4-5-20250929-v1:0":
        {"input": 3.0, "output": 15.0, "cacheRead": 0.3, "cacheWrite": 3.75},
    "jp.anthropic.claude-sonnet-4-6":
        {"input": 3.0, "output": 15.0, "cacheRead": 0.3, "cacheWrite": 3.75},
    "jp.anthropic.claude-sonnet-5":
        {"input": 2.0, "output": 10.0, "cacheRead": 0.2, "cacheWrite": 2.5},
    "meta.llama3-1-70b-instruct-v1:0": {"input": 0.72, "output": 0.72},
    "meta.llama3-1-8b-instruct-v1:0": {"input": 0.22, "output": 0.22},
    "meta.llama3-3-70b-instruct-v1:0": {"input": 0.72, "output": 0.72},
    "meta.llama4-maverick-17b-instruct-v1:0": {"input": 0.24, "output": 0.97},
    "meta.llama4-scout-17b-instruct-v1:0": {"input": 0.17, "output": 0.66},
    "minimax.minimax-m2": {"input": 0.3, "output": 1.2},
    "minimax.minimax-m2.1": {"input": 0.3, "output": 1.2},
    "minimax.minimax-m2.5": {"input": 0.3, "output": 1.2},
    "mistral.devstral-2-123b": {"input": 0.4, "output": 2.0},
    "mistral.magistral-small-2509": {"input": 0.5, "output": 1.5},
    "mistral.ministral-3-14b-instruct": {"input": 0.2, "output": 0.2},
    "mistral.ministral-3-3b-instruct": {"input": 0.1, "output": 0.1},
    "mistral.ministral-3-8b-instruct": {"input": 0.15, "output": 0.15},
    "mistral.mistral-large-3-675b-instruct": {"input": 0.5, "output": 1.5},
    "mistral.pixtral-large-2502-v1:0": {"input": 2.0, "output": 6.0},
    "mistral.voxtral-mini-3b-2507": {"input": 0.04, "output": 0.04},
    "mistral.voxtral-small-24b-2507": {"input": 0.15, "output": 0.35},
    "moonshot.kimi-k2-thinking": {"input": 0.6, "output": 2.5},
    "moonshotai.kimi-k2.5": {"input": 0.6, "output": 3.0},
    "nvidia.nemotron-nano-12b-v2": {"input": 0.2, "output": 0.6},
    "nvidia.nemotron-nano-3-30b": {"input": 0.06, "output": 0.24},
    "nvidia.nemotron-nano-9b-v2": {"input": 0.06, "output": 0.23},
    "nvidia.nemotron-super-3-120b": {"input": 0.15, "output": 0.65},
    "openai.gpt-5.4": {"input": 2.75, "output": 16.5, "cacheRead": 0.275},
    "openai.gpt-5.5": {"input": 5.5, "output": 33.0, "cacheRead": 0.55},
    "openai.gpt-5.6-luna":
        {"input": 0.22, "output": 1.32, "cacheRead": 0.022, "cacheWrite": 0.275},
    "openai.gpt-5.6-sol": {"input": 5.5, "output": 33.0, "cacheRead": 0.55, "cacheWrite": 6.88},
    "openai.gpt-5.6-terra": {"input": 2.2, "output": 13.2, "cacheRead": 0.22, "cacheWrite": 2.75},
    "openai.gpt-oss-120b": {"input": 0.15, "output": 0.6},
    "openai.gpt-oss-120b-1:0": {"input": 0.15, "output": 0.6},
    "openai.gpt-oss-20b": {"input": 0.07, "output": 0.3},
    "openai.gpt-oss-20b-1:0": {"input": 0.07, "output": 0.3},
    "openai.gpt-oss-safeguard-120b": {"input": 0.15, "output": 0.6},
    "openai.gpt-oss-safeguard-20b": {"input": 0.07, "output": 0.2},
    "qwen.qwen3-235b-a22b-2507-v1:0": {"input": 0.22, "output": 0.88},
    "qwen.qwen3-32b-v1:0": {"input": 0.15, "output": 0.6},
    "qwen.qwen3-coder-30b-a3b-v1:0": {"input": 0.15, "output": 0.6},
    "qwen.qwen3-coder-480b-a35b-v1:0": {"input": 0.22, "output": 1.8},
    "qwen.qwen3-coder-next": {"input": 0.22, "output": 1.8},
    "qwen.qwen3-next-80b-a3b": {"input": 0.14, "output": 1.4},
    "qwen.qwen3-vl-235b-a22b": {"input": 0.3, "output": 1.5},
    "us.anthropic.claude-fable-5":
        {"input": 10.0, "output": 50.0, "cacheRead": 1.0, "cacheWrite": 12.5},
    "us.anthropic.claude-haiku-4-5-20251001-v1:0":
        {"input": 1.0, "output": 5.0, "cacheRead": 0.1, "cacheWrite": 1.25},
    "us.anthropic.claude-opus-4-1-20250805-v1:0":
        {"input": 15.0, "output": 75.0, "cacheRead": 1.5, "cacheWrite": 18.75},
    "us.anthropic.claude-opus-4-5-20251101-v1:0":
        {"input": 5.0, "output": 25.0, "cacheRead": 0.5, "cacheWrite": 6.25},
    "us.anthropic.claude-opus-4-6-v1":
        {"input": 5.0, "output": 25.0, "cacheRead": 0.5, "cacheWrite": 6.25},
    "us.anthropic.claude-opus-4-7":
        {"input": 5.0, "output": 25.0, "cacheRead": 0.5, "cacheWrite": 6.25},
    "us.anthropic.claude-opus-4-8":
        {"input": 5.0, "output": 25.0, "cacheRead": 0.5, "cacheWrite": 6.25},
    "us.anthropic.claude-opus-5":
        {"input": 5.0, "output": 25.0, "cacheRead": 0.5, "cacheWrite": 6.25},
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0":
        {"input": 3.0, "output": 15.0, "cacheRead": 0.3, "cacheWrite": 3.75},
    "us.anthropic.claude-sonnet-4-6":
        {"input": 3.0, "output": 15.0, "cacheRead": 0.3, "cacheWrite": 3.75},
    "us.anthropic.claude-sonnet-5":
        {"input": 2.0, "output": 10.0, "cacheRead": 0.2, "cacheWrite": 2.5},
    "us.deepseek.r1-v1:0": {"input": 1.35, "output": 5.4},
    "us.meta.llama4-maverick-17b-instruct-v1:0": {"input": 0.24, "output": 0.97},
    "us.meta.llama4-scout-17b-instruct-v1:0": {"input": 0.17, "output": 0.66},
    "writer.palmyra-x4-v1:0": {"input": 2.5, "output": 10.0},
    "writer.palmyra-x5-v1:0": {"input": 0.6, "output": 6.0},
    "xai.grok-4.3": {"input": 1.25, "output": 2.5, "cacheRead": 0.2},
    "zai.glm-4.7": {"input": 0.6, "output": 2.2},
    "zai.glm-4.7-flash": {"input": 0.07, "output": 0.4},
    "zai.glm-5": {"input": 1.0, "output": 3.2},
    # END GENERATED RATES
}


# -- as-of date and staleness ---------------------------------------------


def as_of_date() -> dt.date:
    """`AS_OF` as a real date.

    Raises `ValueError` when it is missing or unparseable, deliberately: the
    only thing worse than a stale rate table is one that cannot say how stale
    it is. Callers that must not crash use `staleness()`, which reports the
    breakage as data.
    """
    raw = (AS_OF or "").strip()
    if not raw:
        raise ValueError("pricing_aws.AS_OF is empty: the rate table must carry an as-of date")
    return dt.date.fromisoformat(raw)


def age_days(today: dt.date | None = None) -> int | None:
    """Days since the table was mirrored, or None if `AS_OF` is unusable."""
    try:
        as_of = as_of_date()
    except ValueError:
        return None
    return ((today or dt.datetime.now(dt.UTC).date()) - as_of).days


def is_stale(today: dt.date | None = None) -> bool:
    """True once the mirror is older than `STALE_AFTER_DAYS`.

    An unusable `AS_OF` counts as stale: unknown age is not fresh.
    """
    age = age_days(today)
    return True if age is None else age > STALE_AFTER_DAYS


def staleness(today: dt.date | None = None) -> dict:
    """The staleness signal surfaces render (`GET /config`, `doctor`).

    Never raises -- a broken `AS_OF` shows up as `asOfValid: false` with
    `stale: true`, so an operator sees the breakage instead of a crash.
    """
    valid = True
    try:
        as_of_date()
    except ValueError:
        valid = False
    return {
        "table": TABLE_NAME,
        "asOf": AS_OF,
        "asOfValid": valid,
        "ageDays": age_days(today),
        "staleAfterDays": STALE_AFTER_DAYS,
        "stale": is_stale(today),
        "models": len(RATES),
        "source": SOURCE,
        "sourceVersion": SOURCE_VERSION,
        "sourceUrl": SOURCE_URL,
        "refresh": REFRESH_CMD,
    }


# -- the table as a PricingMap -------------------------------------------


@functools.lru_cache(maxsize=1)
def pricing_map() -> PricingMap:
    """The built-in table as a `PricingMap` (built once, treated read-only).

    Reusing `PricingMap` is the point: gateway ids resolve through the same
    alias/exact/longest-wildcard rules as an operator map, and `derive()` is
    the same arithmetic, so "which rules applied" has one answer.
    """
    built = PricingMap.from_config({"aliases": ALIASES, "models": RATES})
    if built is None:  # pragma: no cover - only reachable if RATES is emptied
        raise RuntimeError("built-in AWS Bedrock rate table is empty")
    if is_stale():
        log.warning(
            "built-in AWS Bedrock rate table is stale (as of %s, %s days old); refresh with %r",
            AS_OF, age_days(), REFRESH_CMD,
        )
    return built


def rate_for(model: str | None):
    """Rate for a raw/gateway model id, or None when the table cannot price it."""
    return pricing_map().rate_for(model)


def describe(today: dt.date | None = None) -> dict:
    """Non-secret summary for `GET /config`: what this table is, not every rate.

    The full 100+ rate dump belongs in the module, not in a status payload; what
    an operator needs from an API is *which* table answered and whether it is
    still credible.
    """
    return {**staleness(today), "aliases": len(ALIASES)}
