"""ralphd.engine.tiers -- the three-tier vocabulary and the pure hat x tier
resolver (task 012, #68 half 2, requirements E/J).

Two things live here, and nothing else:

1. The tier vocabulary itself: exactly three ordinal tiers, `trivial <
   normal < hard` (decision 1/E), plus the ordering helpers a caller needs
   to move along that ladder -- `escalate_tier` (the +1-step-capped-at-hard
   rule, decision 6/G) and `step_down_tier` (the one-step-down-floored-at-
   trivial rule verify provisioning uses, decision 7/H). Three tiers only
   (decision/non-goal: "tuning the tier count" is explicitly out of scope);
   nothing here makes that a configurable number.

2. `resolve_bundle`: the PURE resolver of `(hat, effective_tier, host_map)
   -> HatBundle` that requirement J asks for -- "the resolver is a pure
   function ... with no I/O and no clock". It does exactly one thing: look
   the (hat, tier) pair up in the host map it is handed and fall back
   sensibly when a rung is missing. It never reads `JobConfig`, never
   touches a file, never calls `time`/`datetime`, and never imports
   anything that could. `default_host_map` (below) is the ONE place a
   `JobConfig` is consulted, and it does so by delegating to
   `JobConfig.bundle_for` (task 007's existing, already-tested resolution
   order) -- never a second model-resolution pathway.

`transitions.TABLE` is deliberately untouched by this module (requirement
J: "the transition table stays about *what happens next*; provisioning is
*how*") -- see `tests/test_tiers.py::test_transition_table_carries_no_
provisioning_policy` for the guard that keeps it that way.
"""

from __future__ import annotations

import dataclasses
import itertools
from typing import Mapping

from ralphd.engine.config import HatBundle, JobConfig, MODEL_HATS, UNRECOGNISED_HAT_FALLBACK

# ---------------------------------------------------------------------------
# The tier vocabulary (requirement E, decision 1)
# ---------------------------------------------------------------------------
# Exactly three tiers, ordinal, in this order. This tuple's order IS the
# ordering -- `_TIER_RANK` below is derived from it, never hand-maintained
# as a second source of truth.
TIERS: tuple[str, ...] = ("trivial", "normal", "hard")

# Requirement F: an absent/unrecognised annotation degrades to `normal`,
# never to `trivial` (which would silently cheapen unannotated work) and
# never to `hard` (which would silently make every old, tier-naive plan
# expensive). Also the tier a steering-injected task starts at (decision 9).
DEFAULT_TIER: str = "normal"

TOP_TIER: str = TIERS[-1]     # "hard" -- the escalation ceiling (decision 6)
BOTTOM_TIER: str = TIERS[0]   # "trivial" -- the verify-provisioning floor (H)

_TIER_RANK: dict[str, int] = {name: i for i, name in enumerate(TIERS)}


def is_tier(name: object) -> bool:
    """True iff `name` is one of the three declared tier strings."""
    return name in _TIER_RANK


def tier_rank(tier: str) -> int:
    """This tier's ordinal position (0 = trivial .. 2 = hard). Raises
    `ValueError` on anything not in `TIERS` -- callers at the plan-ingestion
    boundary (task 013) are expected to normalise an unrecognised string to
    `DEFAULT_TIER` themselves and warn, exactly as requirement F specifies;
    this helper does not silently absorb that decision."""
    try:
        return _TIER_RANK[tier]
    except KeyError:
        raise ValueError(f"not one of the three declared tiers: {tier!r}") from None


def escalate_tier(tier: str) -> str:
    """+1 step, capped at `hard` (decision 6/requirement G): a
    validation-failed re-entry bumps the task's effective tier one rung,
    and a task already at `hard` stays at `hard` -- there is no fourth rung
    to escalate into and de-escalation does not exist (requirement G)."""
    idx = tier_rank(tier)
    return TIERS[min(idx + 1, len(TIERS) - 1)]


def step_down_tier(tier: str) -> str:
    """One step down, floored at `trivial` (requirement H): per-task verify
    runs at `max(effective_tier - 1, trivial)` on verify's OWN ladder --
    this is that `max(..., trivial)` as a named, reusable step rather than
    inline arithmetic at each call site."""
    idx = tier_rank(tier)
    return TIERS[max(idx - 1, 0)]


# ---------------------------------------------------------------------------
# The host map: hat x tier -> HatBundle (requirement E, decision 2)
# ---------------------------------------------------------------------------
# A hat's three rungs are its OWN ladder (decision 3) -- nothing here
# assumes any relationship between one hat's `hard` rung and another's.
HostMap = Mapping[str, Mapping[str, HatBundle]]


def _bundle_from_leaf(leaf: object, fallback: HatBundle) -> HatBundle:
    """Task 002: turn one `model_tiers[hat][tier]` leaf into a `HatBundle`,
    inheriting from `fallback` (that hat's plain `bundle_for` answer)
    whatever the leaf does not spell out explicitly, so a tier rung that
    only wants to swap the model does not have to also repeat the hat's
    ordinary thinking setting, and vice versa.

    - a plain string names the model ref only; thinking/effort are
      inherited from `fallback` unchanged.
    - a provider-shaped mapping may name `model` (defaulting to
      `fallback.model` when omitted -- a rung that only wants to change
      the thinking/effort setting need not repeat the model ref) and
      EITHER `thinking` OR `effort` (never both, mirroring `HatBundle`'s
      own discipline); a mapping naming neither inherits `fallback`'s
      thinking/effort too, exactly like the plain-string case.
    - anything else (a leaf that is neither a string nor a mapping) is
      malformed and degrades to `fallback` unchanged, never to a made-up
      value.
    """
    if isinstance(leaf, str):
        return HatBundle(model=leaf, thinking=fallback.thinking,
                          effort=fallback.effort, harness=fallback.harness)
    if isinstance(leaf, Mapping):
        model = leaf.get("model", fallback.model)
        if "thinking" in leaf or "effort" in leaf:
            return HatBundle(model=model, thinking=leaf.get("thinking"),
                              effort=leaf.get("effort"),
                              harness=fallback.harness)
        return HatBundle(model=model, thinking=fallback.thinking,
                          effort=fallback.effort, harness=fallback.harness)
    return fallback


def _apply_tier_table(ladder: dict[str, HatBundle], tier_table: Mapping,
                       fallback: HatBundle) -> None:
    """Task 002: write `tier_table`'s leaves into `ladder` in place, one
    rung at a time. A `tier_table` key that is not one of the three
    declared `TIERS` is a typo, not a fourth rung -- it is folded onto
    `TOP_TIER` ("hard"), never onto a cheaper rung, so a mis-spelled tier
    name never silently degrades that leaf's intended capability down to
    `bundle_for`'s plain default the way dropping it would."""
    for tier_key, leaf in tier_table.items():
        tier = tier_key if tier_key in _TIER_RANK else TOP_TIER
        ladder[tier] = _bundle_from_leaf(leaf, fallback)


def default_host_map(cfg: JobConfig) -> dict[str, dict[str, HatBundle]]:
    """Widen task 005/007's per-hat table (`JobConfig.bundle_for`, one
    bundle per hat) into a hat x tier host map, by giving every one of a
    hat's three rungs that hat's single half-1 bundle, THEN layering
    task 002's own `cfg.model_tiers` job.yaml table on top of that base for
    whichever rungs it actually configures.

    This is the ONE place in this module a `JobConfig` is read, and it
    reads it through the existing, already-tested `bundle_for` -- never a
    second model-resolution pathway (requirement J's "one call site, not a
    new pathway" applies just as much to how the map is BUILT as to where
    it is later consulted). `model_tiers` is read nowhere else.

    This is also exactly what makes a one-model configuration collapse
    cleanly (requirement E's "defaultability... survives the widening"):
    with nothing configured beyond the bare `model` field (and no
    `model_tiers` table at all -- the default, empty dict), every hat's
    `bundle_for` already returns that same model, so every rung of every
    hat's ladder ends up naming it too -- tiering becomes a no-op rather
    than an error, with no special-case branch anywhere in this function.

    `model_tiers` anti-downgrade (task 002 criterion c): this function
    never lets an unrecognised hat or tier KEY in the configured table
    quietly vanish into a cheaper rung than what IS configured.

    - a `model_tiers` key spelled exactly like one of `MODEL_HATS` always
      wins for that hat, applied first (below), so a correctly-spelled
      hat's own table is never overridden by anything that follows.
    - a `model_tiers` key that is NOT one of `MODEL_HATS` (a typo) folds
      its (tier-normalised) leaves into `UNRECOGNISED_HAT_FALLBACK`
      ("planning")'s own ladder -- the exact same borrowed-ladder reasoning
      `model_for`/`resolve_bundle` already use for an unrecognised hat NAME
      at lookup time (task 005) -- but only into rungs the correctly-
      spelled `planning` entry (if any) left unset, so a real planning
      config always wins over a stray typo'd one rather than being
      overwritten by it.
    - within either pass, an unrecognised TIER key inside a hat's own
      sub-table folds onto `TOP_TIER` (see `_apply_tier_table`), never onto
      `trivial`/`normal`.
    """
    host_map: dict[str, dict[str, HatBundle]] = {
        hat: {tier: cfg.bundle_for(hat) for tier in TIERS} for hat in MODEL_HATS
    }
    raw_table = cfg.model_tiers if isinstance(cfg.model_tiers, Mapping) else {}

    # Pass 1: hats spelled exactly like a `MODEL_HATS` member -- these
    # always win, including over anything a typo'd hat key (pass 2) might
    # otherwise want to leak into the same hat.
    for hat, tier_table in raw_table.items():
        if hat in MODEL_HATS and isinstance(tier_table, Mapping):
            _apply_tier_table(host_map[hat], tier_table, cfg.bundle_for(hat))

    # Pass 2: everything left -- an unrecognised hat key -- borrows
    # `UNRECOGNISED_HAT_FALLBACK`'s ladder, filling only the rungs pass 1
    # (a correctly-spelled `planning` entry, if configured) left untouched.
    fallback_hat = UNRECOGNISED_HAT_FALLBACK
    fallback_default = cfg.bundle_for(fallback_hat)
    protected_tiers = set(raw_table.get(fallback_hat) or {}) & set(_TIER_RANK)
    for hat, tier_table in raw_table.items():
        if hat in MODEL_HATS or not isinstance(tier_table, Mapping):
            continue
        for tier_key, leaf in tier_table.items():
            tier = tier_key if tier_key in _TIER_RANK else TOP_TIER
            if tier in protected_tiers:
                continue
            host_map[fallback_hat][tier] = _bundle_from_leaf(leaf, fallback_default)

    return host_map


def resolve_bundle(hat: str, tier: str, host_map: HostMap) -> HatBundle:
    """The pure resolver requirement J asks for: `(hat, effective tier,
    host map) -> HatBundle`. No I/O, no clock, no config file read -- the
    host map is the caller's problem (`default_host_map` above, or a
    fully hand-specified one in a test); this function only ever indexes
    into whatever mapping it is handed.

    Fallback order, mirroring `JobConfig.model_for`'s own anti-downgrade
    rule (task 005) rather than inventing a second one:

    - an exact `(hat, tier)` rung wins;
    - a hat not in the map at all, or a hat missing this specific tier's
      rung, borrows `UNRECOGNISED_HAT_FALLBACK`'s ("planning") ladder at
      the SAME tier, same reasoning as task 005's per-hat fallback: a
      typo or a partially-specified map degrades at worst to "as capable
      as the planner", never to the cheap tier;
    - if even that is missing, an empty `HatBundle()` -- pi's own default
      model, the same `None` sentinel every other rung of this module
      speaks for "nothing configured".
    """
    ladder = host_map.get(hat)
    if ladder is not None and tier in ladder:
        return ladder[tier]
    fallback_ladder = host_map.get(UNRECOGNISED_HAT_FALLBACK)
    if fallback_ladder is not None and tier in fallback_ladder:
        return fallback_ladder[tier]
    return HatBundle()


def all_hat_tier_pairs() -> tuple[tuple[str, str], ...]:
    """Every `(hat, tier)` combination this module's vocabulary defines --
    `MODEL_HATS x TIERS` -- as a tuple so a table-driven test can iterate
    it without re-deriving the product itself."""
    return tuple(itertools.product(MODEL_HATS, TIERS))


# ---------------------------------------------------------------------------
# One step down the thinking ladder (the judge retry, #300)
# ---------------------------------------------------------------------------

#: The thinking/effort vocabulary, lowest first (the one pi's `--thinking`
#: speaks and `HatBundle` documents).
THINKING_LEVELS: tuple[str, ...] = (
    "off", "minimal", "low", "medium", "high", "xhigh", "max")


def lower_thinking_level(level: str | None) -> str | None:
    """`level` one step down `THINKING_LEVELS` (`high` -> `medium`). The
    lowest level, a level this vocabulary does not name and `None` (nothing
    configured, so nothing to lower) come back unchanged."""
    if level in THINKING_LEVELS:
        return THINKING_LEVELS[max(0, THINKING_LEVELS.index(level) - 1)]
    return level


def lower_bundle(bundle: HatBundle) -> HatBundle:
    """`bundle` with its thinking and effort levels each one step lower,
    everything else (model, harness) untouched. Pure."""
    return dataclasses.replace(
        bundle, thinking=lower_thinking_level(bundle.thinking),
        effort=lower_thinking_level(bundle.effort))
