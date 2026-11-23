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

from collections.abc import Callable
from dataclasses import dataclass

from . import state
from .config import HARNESS_FORCED_BASIS, MODEL_HATS, ZERO_PRICED_BASES
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


# ---------------------------------------------------------------------------
# `ralphctl budget --spend` / `--spend-approach` (#321 D)
# ---------------------------------------------------------------------------

BUDGET_ADD = "add"
BUDGET_SET = "set"
BUDGET_REMOVE = "remove"
_REMOVE_WORDS = ("0", "none")


def parse_budget_spec(flag: str, spec: str) -> tuple[str, str, float | None]:
    """`HAT=+USD` (top up), `HAT=USD` (set) or `HAT=0` / `HAT=none`
    (remove) from `budget --spend` / `--spend-approach`, as
    `(hat, kind, usd)` with `usd` None for a removal."""
    hat, sep, amount = str(spec).partition("=")
    hat, amount = hat.strip(), amount.strip()
    if not sep or not hat or not amount:
        raise SpendCapError(
            f"{flag} {spec!r} must be HAT=USD, HAT=+USD or HAT=none, e.g. "
            f"review=+5 (valid names: {', '.join(CAP_KEYS)})")
    check_key(flag, hat)
    if amount.lower() in _REMOVE_WORDS:
        return hat, BUDGET_REMOVE, None
    kind = BUDGET_ADD if amount.startswith("+") else BUDGET_SET
    try:
        value: object = float(amount.lstrip("+"))
    except ValueError:
        raise SpendCapError(
            f"{flag} {spec!r}: {amount!r} is not a number "
            "(expected e.g. +5, 20, 0 or none)") from None
    if not isinstance(value, float) or value != value or value in (
            float("inf"), float("-inf")):
        raise SpendCapError(f"{flag} {spec!r}: {amount!r} is not a finite number")
    if not value > 0:
        raise SpendCapError(
            f"{flag} {spec!r}: the amount must be a positive USD figure "
            "(to remove a cap, pass HAT=0 or HAT=none)")
    return hat, kind, value


def apply_budget_specs(caps: object, specs: dict[str, list[tuple[str, str, float | None]]],
                       ) -> dict[str, dict[str, dict]]:
    """The recorded caps after `specs` (`{level: [(hat, kind, usd), ...]}`,
    in order). A top-up of a hat with no cap at that level is refused; every
    entry a spec touches is recorded with source `budget`, the rest keep
    their source. Empty levels are left out, so removing the last cap leaves
    `{}`. `caps` is not modified."""
    out = {level: {hat: dict(entry) for hat, entry in entries.items()}
           for level, entries in (caps if isinstance(caps, dict) else {}).items()
           if isinstance(entries, dict)}
    for level, items in specs.items():
        flag = "--spend" if level == "overall" else "--spend-approach"
        entries = out.setdefault(level, {})
        for hat, kind, usd in items:
            if kind == BUDGET_REMOVE:
                entries.pop(hat, None)
                continue
            if kind == BUDGET_ADD:
                current = _cap_usd(entries.get(hat))
                if current is None:
                    raise SpendCapError(
                        f"{flag} {hat}=+{usd:g}: {hat!r} has no "
                        f"{level.replace('_', '-')} cap to top up "
                        f"(set one with {flag} {hat}=USD)")
                usd = round(current + float(usd or 0), 6)
            entries[hat] = {"usd": float(usd or 0), "source": "budget"}
    return {level: entries for level, entries in out.items() if entries}


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


WARN_FRACTION = 0.8


def warning_key(cap: SpentCap, approach: object) -> str:
    """What marks `cap` as already warned about: its level, the approach (a
    per-approach cap warns once in each), its key and its amount, so a cap
    raised past the warning point warns again at its new 80 %."""
    where = cap.level
    if cap.level == "per_approach":
        where += f":{approach if isinstance(approach, int) else 1}"
    return f"{where}:{cap.hat}:{cap.cap:g}"


def warning_due(spend: float, usd: float) -> bool:
    """True once `spend` has reached 80 % of `usd`."""
    return spend >= usd * WARN_FRACTION


def warnings_reached(caps: object, spend: object, approach: object,
                     hat: str) -> list[SpentCap]:
    """Every cap `hat` is held against (its own and `total`'s, at both
    levels) whose counter has reached 80 % of it, as `SpentCap`s carrying
    the counter. Dedup is the caller's (`warning_key`)."""
    found: list[SpentCap] = []
    if not isinstance(caps, dict) or not caps:
        return found
    doc = spend if isinstance(spend, dict) else {}
    overall = doc.get("overall") if isinstance(doc.get("overall"), dict) else {}
    by_approach = (doc.get("byApproach")
                   if isinstance(doc.get("byApproach"), dict) else {})
    key = str(approach if isinstance(approach, int)
              and not isinstance(approach, bool) and approach > 0 else 1)
    bucket = by_approach.get(key) if isinstance(by_approach.get(key), dict) else {}
    for lvl, counters in (("overall", overall), ("per_approach", bucket)):
        entries = caps.get(lvl)
        if not isinstance(entries, dict):
            continue
        for name in dict.fromkeys((hat, TOTAL)):
            usd = _cap_usd(entries.get(name))
            spent = float(counters.get(name) or 0)
            if usd is not None and warning_due(spent, usd):
                entry = entries.get(name)
                source = entry.get("source") if isinstance(entry, dict) else None
                found.append(SpentCap(name, lvl, usd, spent, source or "unknown"))
    return found


def spent_caps(caps: object, spend: object, approach: object) -> list[SpentCap]:
    """Every cap, at either level, whose counter has reached it -- what a
    `resume` of a spend-exhausted run names while it stays refused. The
    per-approach counters are the bucket of `approach`."""
    found: list[SpentCap] = []
    if not isinstance(caps, dict):
        return found
    for level in LEVELS:
        entries = caps.get(level)
        if not isinstance(entries, dict):
            continue
        for name in entries:
            hit = spent_cap({level: {name: entries[name]}}, spend, approach,
                            name, level=level)
            if hit is not None:
                found.append(hit)
    return found


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
    # A subscription iteration is always `costFree`, but only its SDK list
    # price counts; until that figure arrives (a live iteration, or one killed
    # before its result) its tokens are unpriced.
    if usage.get("costBasis") == state.COST_BASIS_SUBSCRIPTION:
        return not state._is_number(usage.get("listEquivalentUSD"))
    if usage.get("costFree") is True:
        return False
    return bool((usage.get("costPriced") is False or state.is_zero_quote(usage))
                and usage.get("costDerived") is not True)


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


# ---------------------------------------------------------------------------
# Mid-iteration interrupt (#321, C): the running cost of a live iteration
# ---------------------------------------------------------------------------

def running_spend(usage: object, model: str | None, pricing: object) -> float:
    """What a live iteration has spent so far, from the usage a runner folded
    after its latest message. A priced iteration is exactly
    `state.iteration_spend` (pi and codex fold a quoted or derived price per
    message). An unpriced one -- which is every claude-code iteration until
    the final SDK result arrives with the list price -- is its tokens so far
    at the rate the chain holds for `model` (the highest rate of its family
    when the chain holds none for that very id), never $0. The boundary
    count of an unpriced iteration uses the family's highest rate, so a cap
    this figure crosses is a cap the boundary sees crossed as well."""
    base = state.iteration_spend(usage)
    if not is_unpriced(usage) or pricing is None:
        return base
    rate = pricing.rate_for(model) or pricing.family_rate(model)  # type: ignore[attr-defined]
    if rate is None:
        return base
    return max(base, estimate_usd(usage, rate))  # type: ignore[arg-type]


@dataclass
class SpendWatch:
    """The caps one live iteration is held against. A runner folds each
    message's cost into the iteration's usage, then calls `observe` with the
    running cost; when the hat's prior spend plus that cost reaches a cap the
    watch records the crossing, emits `spend.interrupted` once and the runner
    ends the iteration through its `iteration_timeout_s` kill path.

    `caps` is `job.yaml`'s recorded `{level: {hat: {usd, source}}}`; `prior`
    the counters before this iteration (`{"overall": {name: usd},
    "per_approach": {name: usd}}`, the current approach's bucket for the
    second); `emit(event, **fields)` is the run's event writer."""

    caps: dict
    hat: str
    prior: dict
    emit: Callable[..., object] | None = None
    # `warn(cap)` is told of every cap the running spend has brought to 80 %
    # (the callee dedups); None for a watch that does not warn.
    warn: Callable[[SpentCap], object] | None = None
    crossed: SpentCap | None = None
    overshoot: float = 0.0

    def observe(self, running: float) -> SpentCap | None:
        """The first cap `prior + running` reaches, or None. Once a cap is
        crossed the watch keeps that crossing and emits nothing more."""
        if self.crossed is not None:
            return self.crossed
        for level in LEVELS:
            entries = self.caps.get(level) if isinstance(self.caps, dict) else None
            if not isinstance(entries, dict):
                continue
            counters = self.prior.get(level) if isinstance(self.prior, dict) else None
            counters = counters if isinstance(counters, dict) else {}
            for name in dict.fromkeys((self.hat, TOTAL)):
                usd = _cap_usd(entries.get(name))
                if usd is None:
                    continue
                spend = round(float(counters.get(name) or 0) + running, 6)
                entry = entries.get(name)
                source = entry.get("source") if isinstance(entry, dict) else None
                if self.warn is not None and warning_due(spend, usd):
                    self.warn(SpentCap(name, level, usd, spend,
                                       source or "unknown"))
                if spend >= usd:
                    self.crossed = SpentCap(name, level, usd, spend,
                                            source or "unknown")
                    self.overshoot = round(spend - usd, 6)
                    if self.emit is not None:
                        self.emit("spend.interrupted", hat=name, level=level,
                                  cap=usd, spend=spend,
                                  overshoot=self.overshoot,
                                  source=self.crossed.source,
                                  iterationHat=self.hat)
                    return self.crossed
        return None


# ---------------------------------------------------------------------------
# Display (#321 E): spend against caps on status, GET /status and cost
# ---------------------------------------------------------------------------

NOTIONAL_NOTE = "notional, list-equivalent"


def notional_hats(job) -> set[str]:
    """The cap keys whose counted spend is notional under `job`: a hat whose
    route bills nothing per token (a subscription, or a route declared free)
    counts its list-equivalent price, so its figure is never a bill. `total`
    is notional when any hat is."""
    out: set[str] = set()
    for hat in MODEL_HATS:
        harness = job.harness_for(hat)
        basis = HARNESS_FORCED_BASIS.get(harness)
        if basis is None:
            billing = job.billing_for(harness)
            basis = billing[1] if billing else None
        if basis in ZERO_PRICED_BASES:
            out.add(hat)
    if out:
        out.add(TOTAL)
    return out


def _counters(spend: object, approach: object) -> tuple[dict, dict]:
    doc = spend if isinstance(spend, dict) else {}
    overall = doc.get("overall") if isinstance(doc.get("overall"), dict) else {}
    by_approach = (doc.get("byApproach")
                   if isinstance(doc.get("byApproach"), dict) else {})
    key = str(approach if isinstance(approach, int)
              and not isinstance(approach, bool) and approach > 0 else 1)
    bucket = by_approach.get(key) if isinstance(by_approach.get(key), dict) else {}
    return overall, bucket


def _level_cell(entries: object, name: str, counters: dict) -> dict | None:
    usd = _cap_usd(entries.get(name)) if isinstance(entries, dict) else None
    if usd is None:
        return None
    entry = entries.get(name)
    source = entry.get("source") if isinstance(entry, dict) else None
    return {"cap": usd, "spend": round(float(counters.get(name) or 0), 6),
            "source": source or "unknown"}


def _ordered_keys(caps: dict) -> list[str]:
    named = {name for level in LEVELS if isinstance(caps.get(level), dict)
             for name in caps[level]}
    return [k for k in CAP_KEYS if k in named]


def format_cap_usd(usd: float) -> str:
    """`$150` for a whole cap, `$7.50` otherwise."""
    return f"${usd:.0f}" if float(usd).is_integer() else f"${usd:.2f}"


def format_cell(cell: dict) -> str:
    return f"${cell['spend']:.2f} / {format_cap_usd(cell['cap'])}"


def status_entries(caps: object, spend: object, approach: object,
                   notional: set[str] | frozenset[str] = frozenset()) -> list[dict]:
    """One entry per capped hat (and `total`) for `status`, `status --json`
    and `GET /status`: `{hat, notional, overall, perApproach, line}`, each
    level `{cap, spend, source}` or null when that level carries no cap;
    `perApproach` is the current approach's bucket. A run with no caps gives
    `[]`, so the surfaces print nothing and publish no key."""
    if not isinstance(caps, dict) or not any(
            isinstance(caps.get(level), dict) and caps[level] for level in LEVELS):
        return []
    overall, bucket = _counters(spend, approach)
    entries = []
    for name in _ordered_keys(caps):
        entries.append({
            "hat": name,
            "notional": name in notional,
            "overall": _level_cell(caps.get("overall"), name, overall),
            "perApproach": _level_cell(caps.get("per_approach"), name, bucket),
        })
    width = max((len(e["hat"]) for e in entries), default=0)
    for e in entries:
        e["line"] = format_status_line(e, width)
    return entries


def format_status_line(entry: dict, width: int = 0) -> str:
    """`review  $38.10 / $150 overall · $12.40 / $50 this approach`, with a
    trailing ` (notional, list-equivalent)` on a notional hat. A level with
    no cap is left out."""
    parts = []
    if entry.get("overall"):
        parts.append(f"{format_cell(entry['overall'])} overall")
    if entry.get("perApproach"):
        parts.append(f"{format_cell(entry['perApproach'])} this approach")
    line = f"{entry['hat']:<{max(width, len(entry['hat']))}}  " + " · ".join(parts)
    if entry.get("notional"):
        line += f"  ({NOTIONAL_NOTE})"
    return line


def bar_entries(entries: object) -> list[dict]:
    """One bar per cap for the hub's run detail, from `status_entries`' shape
    (what `GET /status` serves as `spendCaps`): `{hat, level, label, cap,
    spend, percent, notional, text}`. `level` is `overall` or `per_approach`;
    `percent` is spend over cap clamped to 0..100; `text` is the figure line
    (`$38.10 / $150`, marked notional on a notional hat). No entries give
    `[]`, so a run with no caps draws no bar."""
    bars: list[dict] = []
    if not isinstance(entries, list):
        return bars
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for level, key, label in (("overall", "overall", "overall"),
                                  ("per_approach", "perApproach", "this approach")):
            cell = entry.get(key)
            if not isinstance(cell, dict) or not cell.get("cap"):
                continue
            notional = bool(entry.get("notional"))
            text = f"{format_cell(cell)} {label}"
            if notional:
                text += f" ({NOTIONAL_NOTE})"
            bars.append({
                "hat": entry.get("hat"), "level": level,
                "label": f"{entry.get('hat')} {label}",
                "cap": cell["cap"], "spend": cell["spend"],
                "percent": round(min(100.0, max(
                    0.0, 100.0 * float(cell["spend"]) / float(cell["cap"]))), 1),
                "notional": notional, "text": text,
            })
    return bars


def cost_entries(caps: object, spend: object,
                 notional: set[str] | frozenset[str] = frozenset()) -> list[dict]:
    """One entry per capped hat for `ralphctl cost`: `{hat, notional,
    overall, approaches}`, `overall` as in `status_entries` and `approaches`
    a list of `{approach, cap, spend, source}` for every approach with
    counters (or the first, when none ran) -- empty when the hat has no
    per-approach cap. No caps gives `[]`."""
    if not isinstance(caps, dict) or not any(
            isinstance(caps.get(level), dict) and caps[level] for level in LEVELS):
        return []
    doc = spend if isinstance(spend, dict) else {}
    overall = doc.get("overall") if isinstance(doc.get("overall"), dict) else {}
    by_approach = (doc.get("byApproach")
                   if isinstance(doc.get("byApproach"), dict) else {})
    numbers = sorted(int(k) for k in by_approach if str(k).isdigit()) or [1]
    out = []
    for name in _ordered_keys(caps):
        approaches = []
        for n in numbers:
            bucket = by_approach.get(str(n))
            cell = _level_cell(caps.get("per_approach"), name,
                               bucket if isinstance(bucket, dict) else {})
            if cell is not None:
                approaches.append({"approach": n, **cell})
        out.append({"hat": name, "notional": name in notional,
                    "overall": _level_cell(caps.get("overall"), name, overall),
                    "approaches": approaches})
    return out


def run_entries(job, status: object, *, cost: bool = False) -> list[dict]:
    """The display entries for a run from its `JobConfig` and status
    document: `status_entries`, or `cost_entries` with `cost=True`."""
    status = status if isinstance(status, dict) else {}
    caps = job.spend_caps
    notional = notional_hats(job)
    if cost:
        return cost_entries(caps, status.get("spend"), notional)
    return status_entries(caps, status.get("spend"), status.get("approach"), notional)
