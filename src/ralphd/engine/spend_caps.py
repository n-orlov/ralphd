"""Spend caps (#321): the shape, the validation and the unpriceable check.

A run's caps live in `job.yaml` under `spend_caps:`, two maps -- `overall`
and `per_approach` -- each from a hat name (or the pseudo-hat `total`) to a
positive USD amount. `ralphctl start` resolves them per hat and per level
(registry `config.yaml` < template `job.yaml` < `--spend-cap` /
`--spend-cap-approach`) and records each entry with the layer that set it:

    spend_caps:
      overall:      {review: {usd: 20.0, source: flag}}
      per_approach: {total: {usd: 5.0, source: registry}}

The registry and template layers may spell an entry as a bare number
(`review: 20`) or in the recorded `{usd, source}` form; the engine reads the
recorded form, and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import state
from .config import MODEL_HATS
from .llm import _tier_leaf_ref
from .pricing import RATE_KEYS

TOTAL = "total"
CAP_KEYS: tuple[str, ...] = (*MODEL_HATS, TOTAL)
LEVELS: tuple[str, ...] = ("overall", "per_approach")
SOURCES: tuple[str, ...] = ("registry", "template", "flag", "budget")
UNPRICEABLE_REASON = "no price in the pricing chain"


class SpendCapError(ValueError):
    """A cap that does not validate; the message names the offender."""


def _amount(where: str, key: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpendCapError(
            f"{where}: spend cap for {key!r} must be a positive USD amount, "
            f"got {value!r}")
    if not value > 0:
        raise SpendCapError(
            f"{where}: spend cap for {key!r} must be a positive USD amount, "
            f"got {value!r} (to remove a cap, leave it out)")
    return float(value)


def check_key(where: str, key: object) -> str:
    if key not in CAP_KEYS:
        raise SpendCapError(
            f"{where}: unknown spend cap key {key!r} "
            f"(valid names: {', '.join(CAP_KEYS)})")
    return key  # type: ignore[return-value]


def parse_level_map(where: str, raw: object) -> dict[str, float]:
    """One level's map, `{hat: number}` (or `{hat: {usd, source}}`), to
    `{hat: usd}`. Unknown keys and non-positive amounts raise."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SpendCapError(f"{where}: must be a mapping of hat name to USD")
    out: dict[str, float] = {}
    for key, value in raw.items():
        check_key(where, key)
        if isinstance(value, dict):
            value = value.get("usd")
        out[key] = _amount(where, key, value)
    return out


def parse_block(where: str, raw: object) -> dict[str, dict[str, float]]:
    """A whole `spend_caps:` block to `{level: {hat: usd}}`, empty levels
    kept out. Anything but `overall` and `per_approach` is refused."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SpendCapError(
            f"{where}: spend_caps must be a mapping with "
            f"{' and '.join(LEVELS)}")
    out: dict[str, dict[str, float]] = {}
    for level, body in raw.items():
        if level not in LEVELS:
            raise SpendCapError(
                f"{where}: unknown spend_caps level {level!r} "
                f"(valid levels: {', '.join(LEVELS)})")
        parsed = parse_level_map(f"{where} spend_caps.{level}", body)
        if parsed:
            out[level] = parsed
    return out


def parse_flag(flag: str, spec: str) -> tuple[str, float]:
    """`HAT=USD` from `--spend-cap` / `--spend-cap-approach`."""
    hat, sep, amount = str(spec).partition("=")
    hat = hat.strip()
    if not sep or not hat or not amount.strip():
        raise SpendCapError(
            f"{flag} {spec!r} must be HAT=USD, e.g. review=20 "
            f"(valid names: {', '.join(CAP_KEYS)})")
    check_key(flag, hat)
    try:
        value: object = float(amount)
    except ValueError:
        raise SpendCapError(
            f"{flag} {spec!r}: {amount.strip()!r} is not a number") from None
    return hat, _amount(flag, hat, value)


def resolve(registry: object, template: object, flag_overall: list[str] | None,
            flag_approach: list[str] | None) -> dict[str, dict[str, dict]]:
    """Merge the layers, weakest first, per hat and per level, into the
    recorded form `{level: {hat: {usd, source}}}`. A level with no entry is
    left out, so a run with no caps resolves to `{}`."""
    resolved: dict[str, dict[str, dict]] = {}
    layers = (("registry", parse_block("registry config.yaml", registry)),
              ("template", parse_block("template job.yaml", template)))
    for source, block in layers:
        for level, entries in block.items():
            for hat, usd in entries.items():
                resolved.setdefault(level, {})[hat] = {"usd": usd, "source": source}
    for level, flag, specs in (("overall", "--spend-cap", flag_overall),
                               ("per_approach", "--spend-cap-approach", flag_approach)):
        for spec in specs or []:
            hat, usd = parse_flag(flag, spec)
            resolved.setdefault(level, {})[hat] = {"usd": usd, "source": "flag"}
    return resolved


def normalize_recorded(raw: object) -> dict[str, dict[str, dict]]:
    """The engine's read of `job.yaml`'s `spend_caps:` -- the recorded form
    `{level: {hat: {usd, source}}}`. Validates like `parse_block`; an entry
    written without a source reads as source `unknown`."""
    block = parse_block("job.yaml", raw)
    sources: dict[str, dict[str, str]] = {}
    if isinstance(raw, dict):
        for level, body in raw.items():
            if isinstance(body, dict):
                sources[level] = {k: v.get("source") for k, v in body.items()
                                  if isinstance(v, dict) and v.get("source")}
    return {level: {hat: {"usd": usd,
                          "source": sources.get(level, {}).get(hat, "unknown")}
                    for hat, usd in entries.items()}
            for level, entries in block.items()}


def hat_models(job, hat: str) -> list[str | None]:
    """Every model ref `hat` may run on under `job`: its plain model, and for
    a tiered hat each configured tier leaf. Order is first seen."""
    plain = job.model_for(hat)
    refs: list[str | None] = [plain]
    tiers = job.model_tiers.get(hat) if isinstance(job.model_tiers, dict) else None
    if isinstance(tiers, dict):
        refs += [_tier_leaf_ref(leaf, plain) for leaf in tiers.values()]
    return list(dict.fromkeys(refs))


def unpriceable_hats(job, pricing, hats) -> list[tuple[str, str | None]]:
    """`(hat, model)` for each of `hats` whose resolved model has no rate in
    `pricing` (an engine `PricingSource` or None) and is not declared free."""
    out: list[tuple[str, str | None]] = []
    for hat in hats:
        for model in hat_models(job, hat):
            if pricing is not None and (pricing.rate_for(model) is not None
                                        or pricing.is_free(model)):
                continue
            out.append((hat, model))
    return out


def unpriceable_refusal(caps: dict[str, dict], job, pricing) -> str | None:
    """The refusal text for `caps` (`{level: {hat: ...}}`, any value shape),
    or None when every capped hat can be priced. A `total` cap needs every
    hat of the lineup priceable, and names each that is not."""
    capped = {hat for entries in caps.values() for hat in entries}
    hats = list(MODEL_HATS) if TOTAL in capped else [h for h in MODEL_HATS if h in capped]
    bad = unpriceable_hats(job, pricing, hats)
    if not bad:
        return None
    lines = [f"hat {hat!r} (model {model or 'unset'}): {UNPRICEABLE_REASON}"
             for hat, model in bad]
    head = ("a spend cap on total needs every hat priceable"
            if TOTAL in capped else "a spend cap needs a priceable hat")
    return head + ":\n  " + "\n  ".join(lines) + (
        "\n  (add the model to `pricing:` in the registry config.yaml, or set "
        "price_strategy, or drop the cap)")


# ---------------------------------------------------------------------------
# Enforcement (#321): which cap is spent, and what an unpriced iteration counts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SpentCap:
    """One cap whose spend has reached it. `hat` is the cap's key (a hat
    name or `total`), `level` is `overall` or `per_approach`."""

    hat: str
    level: str
    cap: float
    spend: float
    source: str = "unknown"

    @property
    def reason(self) -> str:
        """The `exhausted` reason this cap ends a run with."""
        return exhausted_reason(self.hat, self.level)


def exhausted_reason(hat: str, level: str) -> str:
    """`spend:<hat>` for an overall cap (`spend:total` for the run total),
    `spend:<hat>:approach` for a per-approach cap on the last approach."""
    return f"spend:{hat}" + (":approach" if level == "per_approach" else "")


def is_spend_reason(value: object) -> bool:
    return isinstance(value, str) and value.startswith("spend:")


def _cap_usd(entry: object) -> float | None:
    usd = entry.get("usd") if isinstance(entry, dict) else entry
    if isinstance(usd, bool) or not isinstance(usd, (int, float)) or usd <= 0:
        return None
    return float(usd)


def has_cap(caps: object, hat: str) -> bool:
    """True iff `hat` or `total` carries a cap at either level."""
    if not isinstance(caps, dict):
        return False
    return any(isinstance(entries, dict) and (hat in entries or TOTAL in entries)
               for entries in caps.values())


def spent_cap(caps: object, spend: object, approach: object,
              hat: str, *, level: str | None = None) -> SpentCap | None:
    """The first cap `hat` is blocked by, or None. The hat's own caps and
    `total`'s are checked at both levels, overall first (an overall cap ends
    the run whatever the approach says); a cap is spent once the counter
    reaches it. `level` restricts the check to one level."""
    if not isinstance(caps, dict) or not caps:
        return None
    doc = spend if isinstance(spend, dict) else {}
    overall = doc.get("overall") if isinstance(doc.get("overall"), dict) else {}
    by_approach = (doc.get("byApproach")
                   if isinstance(doc.get("byApproach"), dict) else {})
    key = str(approach if isinstance(approach, int)
              and not isinstance(approach, bool) and approach > 0 else 1)
    bucket = by_approach.get(key) if isinstance(by_approach.get(key), dict) else {}
    for lvl, counters in (("overall", overall), ("per_approach", bucket)):
        if level is not None and lvl != level:
            continue
        entries = caps.get(lvl)
        if not isinstance(entries, dict):
            continue
        for name in dict.fromkeys((hat, TOTAL)):
            usd = _cap_usd(entries.get(name))
            spent = counters.get(name) or 0
            if usd is not None and spent >= usd:
                entry = entries.get(name)
                source = entry.get("source") if isinstance(entry, dict) else None
                return SpentCap(name, lvl, usd, float(spent), source or "unknown")
    return None


def is_unpriced(usage: object) -> bool:
    """True when `usage` billed tokens that nothing priced: a stamped
    iteration with `listEquivalentUnpricedTokens`, a route that quoted no
    price (or an implausible zero) and was not derived, or a subscription
    iteration that never got its list-equivalent figure. A route the operator
    declared free is priced at $0 by declaration."""
    if not isinstance(usage, dict) or not state.billable_tokens(usage):
        return False
    if state._list_unpriced_tokens(usage) > 0:
        return True
    if usage.get("costFree") is True:
        return False
    if ((usage.get("costPriced") is False or state.is_zero_quote(usage))
            and usage.get("costDerived") is not True):
        return True
    return (usage.get("costBasis") == state.COST_BASIS_SUBSCRIPTION
            and not state._is_number(usage.get("listEquivalentUSD")))


def rate_dict(rate: object) -> dict[str, float]:
    return {key: rate.per_mtok(key) for key in RATE_KEYS}  # type: ignore[attr-defined]


def estimate_usd(usage: dict, rate: object) -> float:
    """What `usage`'s tokens cost at `rate` (USD per million tokens). When
    the iteration reported only a total, every token is priced at the rate's
    dearest counter -- an upper bound, never a $0."""
    total = 0.0
    seen = False
    for key in RATE_KEYS:
        tokens = usage.get(key)
        if state._is_number(tokens) and tokens > 0:
            seen = True
            total += tokens * rate.per_mtok(key) / 1_000_000  # type: ignore[attr-defined]
    if not seen:
        tokens = state._token_total(usage) or 0
        top = max(rate.per_mtok(key) for key in RATE_KEYS)  # type: ignore[attr-defined]
        total = tokens * top / 1_000_000
    return round(total, 6)


def counted_spend(usage: object, model: str | None,
                  pricing: object) -> tuple[float, dict | None]:
    """`(amount, unpriced)`: what an iteration counts against a cap. A priced
    iteration counts `state.iteration_spend`, with `unpriced` None. An
    unpriced one counts the dearer of that and its tokens at the highest rate
    `pricing` knows for `model`'s family -- never $0 -- and `unpriced` names
    the rate used (`None` when the chain holds no rate at all)."""
    base = state.iteration_spend(usage)
    if not is_unpriced(usage):
        return base, None
    rate = pricing.family_rate(model) if pricing is not None else None
    if rate is None:
        return base, {"rate": None, "estimateUSD": base}
    estimate = estimate_usd(usage, rate)  # type: ignore[arg-type]
    return max(base, estimate), {"rate": rate_dict(rate), "estimateUSD": estimate}
