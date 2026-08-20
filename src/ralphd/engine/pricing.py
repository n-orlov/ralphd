"""Optional host-side pricing map: derive a cost the provider never quoted.

Task 052 (#10). Some gateways bill plenty of tokens and report no `cost`
block at all -- task 049 stopped lying about that (`unknown`, not `$0`), but
"unknown" is still not a number an operator can budget with, and for a
gateway alias like `aigw-openai/gpt-5` NO upstream pricing table can ever
know the rate: the model id is local to that gateway. So ralphd accepts a
host-side map of per-million-token rates and, *only* when the provider
reported nothing, derives a cost from it.

A derived cost is never conflated with a provider-reported one. It lands in
its own `costDerivedUSD` field (never in `costUSD`), carries its own marker
(`costDerived`), and every surface renders it with a `~ ... derived` marker
(`state.format_cost`). Provider-reported `costUSD` therefore keeps its exact
pre-0.5 meaning: money the provider itself quoted.

Config shape (`<registry>/config.yaml`'s `pricing:` key, inlined into the
run's `job.yaml` by `ralphctl start`; also settable directly in a job.yaml or
via `RALPHD_PRICING` as JSON):

    pricing:
      aliases:
        "aigw-openai/*": "openai/*"          # trailing-* keeps the tail
        "eu.anthropic.claude-opus-5": "anthropic/claude-opus-5"
      models:
        "openai/gpt-5": {input: 1.25, output: 10.0, cacheRead: 0.125}
        "anthropic/*":  {input: 3.0,  output: 15.0}
      free:
        - "ollama/*"                          # this route really costs nothing

Rates are USD per MILLION tokens, keyed exactly like the usage counters pi
reports (`input`, `output`, `cacheRead`, `cacheWrite`); an absent cache rate
falls back to the input rate rather than to zero, since silently pricing
cached tokens at $0 is the same class of lie this task exists to remove. A
malformed entry is ignored rather than fatal -- an operator's typo in an
optional cost annotation must never stop a job from running.

`free:` (task 049, v0.6) is the ONLY way a $0 becomes credible for a route
that billed tokens: it is a declaration by the operator, matched with the same
rules as the rate table. Never infer "free" from a provider quoting zero --
that is exactly the implausible-zero anomaly `state.is_zero_quote` exists to
catch (`artifacts/reports/pricing-anomaly.md`).

Task 011 (#14, v0.6) adds a SECOND possible source of rates -- the built-in
AWS Bedrock table (`engine/pricing_aws.py`), engaged by
`price_strategy: aws`. The two are composed, never merged: `resolve_pricing`
returns a `PricingChain` with the operator map first, so an operator rate
always wins and every derived number can still name the table it came from
(`table_for`, `price_tables` -> `GET /config`). With the shipped default
`price_strategy: none`, `resolve_pricing` returns the operator map itself and
behaviour is byte-identical to pre-v0.6.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger("ralphd.pricing")

# The usage counters a rate may be quoted for, in the order they are summed.
RATE_KEYS = ("input", "output", "cacheRead", "cacheWrite")

# Task 011 (#14): the names every surface uses to say WHICH table produced a
# rate. The operator's own `pricing:` map is "operator map"; a built-in table
# names itself (`pricing_aws.TABLE_NAME`); "neither" means nothing could price
# the route at all, which is why such a cost stays `unavailable`.
OPERATOR_TABLE = "operator map"
NO_TABLE = "neither"
# The one strategy name that engages the built-in AWS Bedrock table.
# `config.PRICE_STRATEGIES` validates the knob; this is what acts on it.
AWS_STRATEGY = "aws"


@dataclass(frozen=True)
class ModelRate:
    """Per-million-token USD rates for one canonical model id."""

    input: float = 0.0
    output: float = 0.0
    cache_read: float | None = None
    cache_write: float | None = None

    def per_mtok(self, key: str) -> float:
        """Rate for one usage counter. Cache rates fall back to `input`."""
        if key == "input":
            return self.input
        if key == "output":
            return self.output
        if key == "cacheRead":
            return self.input if self.cache_read is None else self.cache_read
        if key == "cacheWrite":
            return self.input if self.cache_write is None else self.cache_write
        return 0.0

    @classmethod
    def parse(cls, raw: object) -> ModelRate | None:
        """Build a rate from a config mapping, or None if unusable."""
        if not isinstance(raw, dict):
            return None
        vals: dict[str, float] = {}
        for key, field_name in (("input", "input"), ("output", "output"),
                                ("cacheRead", "cache_read"),
                                ("cacheWrite", "cache_write")):
            if key not in raw or raw[key] is None:
                continue
            try:
                vals[field_name] = float(raw[key])
            except (TypeError, ValueError):
                return None
        if "input" not in vals and "output" not in vals:
            return None  # nothing to price with
        return cls(**vals)


def _match(pattern: str, model: str) -> str | None:
    """Match `model` against a config key, returning the wildcard tail.

    Exact key -> `""`. A key ending in `*` matches by prefix and returns the
    matched tail, so `"aigw-openai/*"` -> `"openai/*"` rewrites
    `aigw-openai/gpt-5` to `openai/gpt-5`. Deliberately only this one
    wildcard form: full glob/regex in a cost table buys nothing and makes
    "which rate priced this run" hard to answer.
    """
    if pattern == model:
        return ""
    if pattern.endswith("*") and model.startswith(pattern[:-1]):
        return model[len(pattern) - 1:]
    return None


@dataclass
class PricingMap:
    """A resolved host-side pricing map. Empty maps are never constructed --
    `from_config` returns None instead, so `pricing is None` reads as "no map
    configured" at every call site."""

    models: dict[str, ModelRate] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    # Task 049 (v0.6): model-id patterns the operator DECLARES cost nothing,
    # matched exactly like `models` keys (after aliasing). The only way a $0
    # with billable tokens is believed.
    free: tuple[str, ...] = ()
    # Task 011 (#14): what this table is called wherever a surface has to say
    # which one answered. Defaults to the operator map -- the only table an
    # operator configures; `pricing_aws.pricing_map()` passes its TABLE_NAME.
    name: str = OPERATOR_TABLE

    @classmethod
    def from_config(cls, raw: object, *,
                    name: str = OPERATOR_TABLE) -> PricingMap | None:
        """Parse the `pricing:` config value. None when absent/unusable."""
        if not isinstance(raw, dict) or not raw:
            return None
        aliases_raw = raw.get("aliases") or {}
        free_raw = raw.get("free") or ()
        if "models" in raw:
            models_raw = raw.get("models") or {}
        else:
            # flat form: the mapping IS the model table
            models_raw = {k: v for k, v in raw.items()
                          if k not in ("aliases", "free")}
        aliases = {str(k): str(v) for k, v in aliases_raw.items()
                   if isinstance(aliases_raw, dict) and v is not None}
        if isinstance(free_raw, str):
            free_raw = [free_raw]
        free = tuple(str(p) for p in free_raw if p) \
            if isinstance(free_raw, (list, tuple)) else ()
        models: dict[str, ModelRate] = {}
        if isinstance(models_raw, dict):
            for model_id, entry in models_raw.items():
                rate = ModelRate.parse(entry)
                if rate is None:
                    log.warning("pricing: ignoring unusable rate for %r", model_id)
                    continue
                models[str(model_id)] = rate
        if not models and not free:
            return None
        return cls(models=models, aliases=aliases, free=free, name=name)

    # -- lookup -----------------------------------------------------------

    def canonical(self, model: str) -> str:
        """Apply the alias table (one hop) to a raw/gateway model id."""
        for pattern, target in sorted(self.aliases.items(),
                                      key=lambda kv: -len(kv[0])):
            tail = _match(pattern, model)
            if tail is None:
                continue
            return target[:-1] + tail if target.endswith("*") else target
        return model

    def rate_for(self, model: str | None) -> ModelRate | None:
        """The rate for a (possibly aliased) model id, or None if unknown.

        Exact model keys win over wildcard ones, and among wildcards the
        longest (most specific) prefix wins -- so a family default
        (`anthropic/*`) can coexist with a pinned per-model rate.
        """
        if not model:
            return None
        name = self.canonical(str(model))
        if name in self.models:
            return self.models[name]
        for pattern in sorted(self.models, key=len, reverse=True):
            if _match(pattern, name) is not None:
                return self.models[pattern]
        return None

    def is_free(self, model: str | None) -> bool:
        """True when the operator DECLARED this route free (task 049, v0.6).

        Matched on the canonical (aliased) id with the same exact-then-longest-
        wildcard rules as `rate_for`, so `free: ["ollama/*"]` covers a family
        and a pinned id covers exactly one route. A declared-free route keeps
        rendering `$0.00`; an *undeclared* zero quote is an anomaly, not a
        price (`state.is_zero_quote`).
        """
        if not model or not self.free:
            return False
        name = self.canonical(str(model))
        return any(_match(pattern, name) is not None
                   for pattern in sorted(self.free, key=len, reverse=True))

    def derive(self, usage: dict, model: str | None) -> float | None:
        """Derived USD for one message's token counters, or None when this
        model has no rate at all (the caller then keeps cost *unknown*)."""
        rate = self.rate_for(model)
        if rate is None:
            return None
        total = 0.0
        for key in RATE_KEYS:
            try:
                tokens = int(usage.get(key) or 0)
            except (TypeError, ValueError):
                tokens = 0
            if tokens:
                total += tokens * rate.per_mtok(key) / 1_000_000
        return round(total, 6)

    def table_for(self, model: str | None) -> str:
        """Which table can price `model`: this map's `name`, or `NO_TABLE`."""
        return self.name if self.rate_for(model) is not None else NO_TABLE

    def describe(self) -> dict:
        """Non-secret summary for `GET /config` -- the rates as configured,
        so an operator can see *which* table produced a derived cost."""
        return {
            "table": self.name,
            "models": {model: {k: rate.per_mtok(k) for k in RATE_KEYS}
                       for model, rate in sorted(self.models.items())},
            "aliases": dict(sorted(self.aliases.items())),
            "free": sorted(self.free),
        }

    def summary(self) -> dict:
        """One line about this table for the `priceTables` list (task 011):
        its name and how much it knows, without dumping every rate twice."""
        return {"name": self.name, "models": len(self.models),
                "aliases": len(self.aliases), "free": len(self.free)}


@dataclass(frozen=True)
class PricingChain:
    """Several rate tables consulted in order, most specific first (task 011).

    Built by `resolve_pricing` when `price_strategy` engages a built-in table:
    the operator's own `pricing:` map is always layer 0, so a rate an operator
    typed for THEIR gateway always beats a shipped table's idea of the same id.
    It is a duck-typed stand-in for a single `PricingMap` (`rate_for`,
    `is_free`, `derive`, `table_for`, `describe`), so `PiRunner` and
    `_accumulate_cost` need no idea whether one table or three answered.

    Deliberately NOT a merged dict of rates: merging would lose which table a
    number came from, and "which table priced this run" is the question this
    whole feature exists to keep answerable.
    """

    layers: tuple[PricingMap, ...]

    @property
    def name(self) -> str:
        return ", then ".join(layer.name for layer in self.layers) or NO_TABLE

    def _layer_for(self, model: str | None) -> PricingMap | None:
        """The first layer with a rate for `model` -- the one that answers."""
        for layer in self.layers:
            if layer.rate_for(model) is not None:
                return layer
        return None

    # Deliberately NO chain-level `canonical()`: each layer aliases with its
    # own table, so "the canonical id" is a per-layer answer, not a chain one.

    def rate_for(self, model: str | None) -> ModelRate | None:
        layer = self._layer_for(model)
        return layer.rate_for(model) if layer else None

    def is_free(self, model: str | None) -> bool:
        """True when ANY layer declares the route free. Only an operator map
        ever carries `free:` patterns -- a shipped table has no business
        declaring someone else's route free -- so in practice this is the
        operator's declaration, honoured whichever layer holds the rate."""
        return any(layer.is_free(model) for layer in self.layers)

    def derive(self, usage: dict, model: str | None) -> float | None:
        """Derived USD from the FIRST layer that can price `model`, or None.

        Never a sum and never an average across layers: exactly one table
        prices a given message, and it is the most specific one.
        """
        layer = self._layer_for(model)
        return layer.derive(usage, model) if layer else None

    def table_for(self, model: str | None) -> str:
        layer = self._layer_for(model)
        return layer.name if layer else NO_TABLE

    def describe(self) -> dict:
        return {"table": self.name,
                "tables": [layer.summary() for layer in self.layers]}

    def summary(self) -> dict:
        return {"name": self.name, "tables": [layer.summary()
                                              for layer in self.layers]}


# A resolved rate source: one table, a chain of them, or nothing configured.
PricingSource = PricingMap | PricingChain


def wants_aws(price_strategy: object) -> bool:
    """True when `price_strategy` selects the built-in AWS Bedrock table.

    Tolerant of casing/whitespace exactly like `config.normalize_price_strategy`
    (which has already normalised it in every real code path) so a directly
    built JobConfig or a hand-written test value cannot silently mean `none`.
    """
    return str(price_strategy or "").strip().lower() == AWS_STRATEGY


def resolve_pricing(pricing_cfg: object,
                    price_strategy: object = None) -> PricingSource | None:
    """The effective rate source for a run (task 011, #14).

    * `price_strategy` not `aws` -> exactly what pre-v0.6 ralphd used: the
      operator's map, or None when there is none. Byte-identical behaviour is
      the point, and `tests/test_price_strategy_derive.py` asserts it on a
      fixture rather than trusting this sentence.
    * `price_strategy: aws` -> the built-in AWS Bedrock table
      (`pricing_aws.pricing_map()`), behind the operator's map when one is
      configured, so an operator rate always wins.

    Imported lazily: `pricing_aws` imports this module, and a run that never
    opts in should not pay for building the table (nor see its staleness
    warning).
    """
    operator = PricingMap.from_config(pricing_cfg)
    if not wants_aws(price_strategy):
        return operator
    from . import pricing_aws
    layers = [layer for layer in (operator, pricing_aws.pricing_map())
              if layer is not None]
    return PricingChain(tuple(layers)) if layers else None


def price_tables(pricing_cfg: object,
                 price_strategy: object = None) -> dict:
    """What may derive a cost, in precedence order, for `GET /config`.

    Answers "which table produced this rate" *before* anything is derived:
    `names` in precedence order, `answers` as one human string (`"operator
    map, then builtin-aws-bedrock"`, or `"neither"` when nothing is
    configured), and one summary per table -- including the built-in table's
    as-of date and staleness, which is the part an operator must be able to
    distrust.
    """
    tables: list[dict] = []
    operator = PricingMap.from_config(pricing_cfg)
    if operator is not None:
        tables.append(operator.summary())
    if wants_aws(price_strategy):
        from . import pricing_aws
        tables.append(pricing_aws.describe())
    names = [t["name"] for t in tables]
    return {"names": names,
            "answers": ", then ".join(names) or NO_TABLE,
            "tables": tables}
