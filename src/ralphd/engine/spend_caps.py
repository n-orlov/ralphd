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

from .config import MODEL_HATS
from .llm import _tier_leaf_ref

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
