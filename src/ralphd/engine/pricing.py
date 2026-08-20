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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger("ralphd.pricing")

# The usage counters a rate may be quoted for, in the order they are summed.
RATE_KEYS = ("input", "output", "cacheRead", "cacheWrite")


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

    @classmethod
    def from_config(cls, raw: object) -> PricingMap | None:
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
            for name, entry in models_raw.items():
                rate = ModelRate.parse(entry)
                if rate is None:
                    log.warning("pricing: ignoring unusable rate for %r", name)
                    continue
                models[str(name)] = rate
        if not models and not free:
            return None
        return cls(models=models, aliases=aliases, free=free)

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

    def describe(self) -> dict:
        """Non-secret summary for `GET /config` -- the rates as configured,
        so an operator can see *which* table produced a derived cost."""
        return {
            "models": {name: {k: rate.per_mtok(k) for k in RATE_KEYS}
                       for name, rate in sorted(self.models.items())},
            "aliases": dict(sorted(self.aliases.items())),
            "free": sorted(self.free),
        }
