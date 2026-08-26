"""Job configuration loaded from /config/job.yaml (or env fallbacks)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .pricing import PricingMap, price_tables

log = logging.getLogger("ralphd.config")

CONFIG_DIR = Path(os.environ.get("RALPHD_CONFIG_DIR", "/config"))
RUN_DIR = Path(os.environ.get("RALPHD_RUN_DIR", "/run/ralphd"))
WORKSPACE_DIR = Path(os.environ.get("RALPHD_WORKSPACE_DIR", "/workspace"))
PROMPTS_BUILTIN = Path(os.environ.get("RALPHD_PROMPTS_DIR",
                                      str(Path(__file__).parent.parent / "prompts")))

# Container-local writable overlay for runtime config mutations (PRD req 11).
# `/config` is mounted read-only from the host in real containers, so any
# config CRUD driven by the API (skills/creds/prompts/llm PUTs) must land
# somewhere else that's actually writable inside the container -- never under
# the mounted /config path, and never under the run dir (which is
# host-visible history, and where creds must never appear). Defaults under
# $HOME so it lives entirely inside the container's own filesystem layer and
# is gone when the container is removed.
OVERLAY_DIR = Path(os.environ.get(
    "RALPHD_CONFIG_OVERLAY_DIR",
    str(Path(os.environ.get("HOME") or os.path.expanduser("~")) /
        ".ralphd" / "config-overlay")))


# Task 003 (#62 candidate 2, requirement A): the coordinates that locate
# and authenticate to the engine's own control-plane HTTP API. Defence in
# depth alongside requirement A's route split (task 004) -- `PiRunner.run`
# strips these from the base `os.environ` before spawning `pi`, so a
# spawned iteration never inherits the port it could probe, the bind
# address, or the bearer token that would let it succeed once it did. Fact
# 5's probe (read your own environment, then hit the API) used exactly
# these two names; `RALPHD_BIND` is the third "locates the API" coordinate
# (host-network jobs only, `engine/main.py`'s `bind`) and is stripped for
# the same reason even though fact 5 didn't need it. Every other RALPHD_*
# var (RALPHD_RUN_DIR, RALPHD_WORKSPACE_DIR, RALPHD_SELF_CONTAINER_ID, the
# RALPHD_HOST_* docker-sibling coordinates, ...) is deliberately NOT here:
# an iteration legitimately needs those to do its job or to label sibling
# containers, and none of them locates or authenticates to the API.
CONTROL_PLANE_ENV_VARS = frozenset({
    "RALPHD_PORT",
    "RALPHD_BIND",
    "RALPHD_API_TOKEN",
    # Task 004 (#62 candidate 1): the mutating-only gate's secret
    # (`JobConfig.control_token`) locates/authenticates to the control
    # plane exactly like `RALPHD_API_TOKEN` does, so it is stripped from a
    # spawned iteration's environment for the same reason.
    "RALPHD_CONTROL_TOKEN",
})


# Task 004 (#62 candidate 1, requirement A): the per-route mutating/
# observation classification, ONE canonical copy (fact from the PRD's own
# harness lesson: "a shared fix with a live second copy is not a fix") --
# `engine/api.py`'s auth gate, `tests/test_route_inventory.py`'s inventory
# test and `cli/main.py`'s control-token attach point all import it from
# here (not `engine/api.py`, which pulls in FastAPI -- `cli/main.py` is
# deliberately stdlib-only) rather than each keeping a private frozenset
# that could drift out of sync with the others.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
OBSERVATION_METHODS = frozenset({"GET", "HEAD"})


def classify_method(method: str) -> str:
    """'observation' (GET/HEAD), 'mutating' (POST/PUT/PATCH/DELETE), or
    'unclassified' for anything else -- callers must treat 'unclassified'
    as a failure, never default it either way (see
    tests/test_route_inventory.py's mutation case)."""
    if method in OBSERVATION_METHODS:
        return "observation"
    if method in MUTATING_METHODS:
        return "mutating"
    return "unclassified"


def overlay_or_config(rel: str) -> Path:
    """Resolve a `/config`-relative path, preferring a runtime overlay entry
    (written via the API) over the corresponding path under the (possibly
    read-only) mounted `/config`. Callers still need to check `.exists()` and
    fall back further (e.g. to a builtin default) themselves."""
    overlay = OVERLAY_DIR / rel
    return overlay if overlay.exists() else CONFIG_DIR / rel


def overlay_write_path(rel: str) -> Path:
    """Path under the writable overlay for a config-relative name, creating
    its parent directory. Never write under CONFIG_DIR (read-only mount) or
    RUN_DIR (host-visible run state) for config mutations."""
    dest = OVERLAY_DIR / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


# Task 006 (#5): the default infra-fault backoff schedule, in seconds, with
# the LAST value repeating for every further attempt (capped at
# infra_retry_backoff_max_s). Fast at the start on purpose: a 30-second
# gateway blip should cost seconds of delay, not the minute-plus the old
# [60, 300, 900] schedule imposed on the very first retry.
DEFAULT_INFRA_RETRY_BACKOFF_S = [2.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0]
# Longest single backoff wait; the repeating tail of the schedule is clamped
# to it so a long outage keeps polling at a sane 5-minute cadence.
DEFAULT_INFRA_RETRY_BACKOFF_MAX_S = 300.0
# Wall-clock budget for ONE fault episode: retries continue while the
# cumulative wait for this episode stays under it (4 hours by default --
# long enough to sit out a real provider outage), and the episode clock
# resets on any successful iteration.
DEFAULT_INFRA_OUTAGE_BUDGET_S = 4 * 3600.0

# Task 010 (#14): how a cost is derived when the provider quotes none (or
# quotes an implausible zero -- see state.is_zero_quote). `none` is the
# shipped default: without an explicit opt-in ralphd never puts a number it
# invented next to a run, exactly as before v0.6. `aws` engages the built-in
# AWS Bedrock rate table (engine/pricing_aws.py); an operator `pricing:` map
# is independent of this knob and always applies.
PRICE_STRATEGIES = ("none", "aws")
DEFAULT_PRICE_STRATEGY = "none"


def normalize_price_strategy(value: object) -> str:
    """Coerce a configured price strategy to a known name.

    An unknown/misspelled value degrades to `none` with a warning rather than
    killing the engine: a typo in an optional cost annotation must never stop
    a job from running, and falling back to `none` can only ever *withhold* a
    derived number, never invent one. The effective value is visible in
    `GET /config` (`priceStrategy`), so the fallback is observable.
    """
    if value is None or value == "":
        return DEFAULT_PRICE_STRATEGY
    name = str(value).strip().lower()
    if name in PRICE_STRATEGIES:
        return name
    log.warning("price_strategy %r is not one of %s -- using %r",
                value, "|".join(PRICE_STRATEGIES), DEFAULT_PRICE_STRATEGY)
    return DEFAULT_PRICE_STRATEGY


# Phase names with a builtin prompt (see src/ralphd/prompts/); the only names
# accepted by the prompts CRUD API (PRD req 10).
PROMPT_NAMES = ("planning", "worker", "review", "task-verify")


def prompt_source(name: str) -> str:
    """Effective origin for a phase prompt: 'api' (runtime overlay PUT) >
    'mounted' (/config/prompts/{name}.md, operator-provided) > 'builtin'."""
    if (OVERLAY_DIR / f"prompts/{name}.md").exists():
        return "api"
    if (CONFIG_DIR / f"prompts/{name}.md").exists():
        return "mounted"
    return "builtin"


def list_prompts() -> list[dict]:
    """Every known phase prompt with its effective source (PRD req 10)."""
    return [{"name": n, "source": prompt_source(n)} for n in PROMPT_NAMES]


# ---------------------------------------------------------------------------
# Task 006 (#60 pillar 4, requirement A cleanup): MIGRATION-ONLY. Tasks 043/044
# gave every job a `model_strategy` (a name into `_LEGACY_STRATEGY_TIERS`
# below) plus an optional `fast_model` second ref; task 005 replaced that
# whole mechanism with the per-hat `models`/`model_overrides` table
# `model_for` reads directly. Neither name is a live field or a live
# resolution rung any more -- `_migrate_legacy_model_config` below is the
# ONLY place either name is read, and it runs exactly once, at `load()` time,
# to translate an old job.yaml into the new table so an existing run keeps
# behaving identically instead of the keys silently landing in `extra` and
# doing nothing. Nothing downstream of that call ever sees `model_strategy`
# or `fast_model` again.
_LEGACY_DEFAULT_MODEL_STRATEGY = "balanced"

# The preset table itself, verbatim from the retired `JobConfig.STRATEGY_TIERS`
# class attribute -- kept ONLY so the migration above can reproduce exactly
# the same phase->tier choice an old job.yaml used to get live. Never
# consulted by `model_for`.
_LEGACY_STRATEGY_TIERS = {
    "quality-first": {"planning": "strong", "worker": "strong",
                      "review": "strong", "verify": "strong",
                      "reflect": "strong"},
    "cost-optimized": {"planning": "strong", "worker": "fast",
                       "review": "fast", "verify": "fast",
                       "reflect": "fast"},
    "balanced": {"planning": "strong", "worker": "fast",
                 "review": "strong", "verify": "fast",
                 "reflect": "strong"},
}

# The names `_LEGACY_STRATEGY_TIERS` recognises -- used only to decide whether
# a `model_strategy` value degrades to the migration's own "unrecognised name"
# fallback (`quality-first`, matching the retired live behaviour exactly).
_LEGACY_MODEL_STRATEGIES = tuple(_LEGACY_STRATEGY_TIERS)


def _migrate_legacy_model_config(raw: dict) -> dict:
    """Task 006: fold a job.yaml's legacy `model_strategy`/`fast_model` keys
    into the per-hat `models` table `model_for` actually reads, deterministically
    and exactly once, so an old job.yaml (or one hand-written / carried over
    from a template written before this task) keeps resolving to the exact
    same model per phase it always did -- never a silent drop into `extra`.

    Runs on the raw dict `load()` just parsed, before the known/extra split,
    and returns a new dict with both legacy keys removed and `models` filled
    in for any of the five strategy-tiered hats an explicit `models`/
    `model_overrides` entry did not already claim (those always win).
    """
    if "model_strategy" not in raw and "fast_model" not in raw:
        return raw
    raw = dict(raw)
    strategy = raw.pop("model_strategy", _LEGACY_DEFAULT_MODEL_STRATEGY)
    fast_model = raw.pop("fast_model", None)
    model = raw.get("model")
    tiers = _LEGACY_STRATEGY_TIERS.get(
        strategy, _LEGACY_STRATEGY_TIERS["quality-first"])
    translated = {hat: (fast_model or model if tiers.get(hat) == "fast" else model)
                  for hat in ("planning", "worker", "verify", "review", "reflect")}
    explicit = dict(raw.get("models") or {})
    explicit.update(raw.get("model_overrides") or {})
    merged = {**translated, **explicit}
    merged = {k: v for k, v in merged.items() if v is not None}
    log.warning(
        "job.yaml's `model_strategy`/`fast_model` keys are retired (task "
        "006) -- translated deterministically into an equivalent `models:` "
        "table naming every hat: %s. Update job.yaml to spell this directly "
        "as `models:` (or `model_overrides:`).", merged)
    raw["models"] = merged
    return raw
# ---------------------------------------------------------------------------


# Task 005 (#60 pillar 4, requirement A): the per-hat model table's own two
# constants, preserving the exact split `DEFAULT_MODEL_STRATEGY`'s comment
# above states for strategy names -- "what ships" and "what an unrecognised
# name degrades to" stay two separate decisions here too, for the identical
# reason: a typo in a hat name must never quietly make a run cheaper, even
# though the shipped default is free to be the cheap/absent choice.
#
# All six hats `model_for` resolves: the five `STATE_HAT` roles
# (transitions.py) plus the router, which is not state-tiered but still gets
# a per-hat table entry (`router_model`/`models["router"]`).
MODEL_HATS: tuple[str, ...] = (
    "planning", "worker", "verify", "review", "reflect", "router")

# "What ships": the ref a KNOWN hat resolves to when neither the per-hat
# table (`models:`/`model_overrides:`) nor the bare `model` field configures
# anything for it -- i.e. what a job that configures nothing at all gets.
# `None` means "pi's own default model", exactly like the bare `model` field's
# `None` already means today; this is also `model`'s own field default below,
# so the two can never drift apart the way a second hardcoded literal would.
DEFAULT_HAT_MODEL_REF: str | None = None

# "What an unrecognised name degrades to": kept as its OWN constant, never
# collapsed into `DEFAULT_HAT_MODEL_REF` above, for the same reason
# `DEFAULT_MODEL_STRATEGY`'s comment gives for strategy names -- collapsing
# them would mean a future default flip silently retargets the anti-downgrade
# fallback too. Names the hat whose own configured entry `model_for` borrows
# for an unrecognised hat name: `planning` (never `worker`/`verify`/`router`),
# because every preset and every per-hat table seen so far reserves the
# strongest model for the planner -- so a typo can degrade AT WORST to "as
# capable as the planner", never to the cheap/mechanical tier.
UNRECOGNISED_HAT_FALLBACK = "planning"


@dataclass
class JobConfig:
    run_id: str = "unnamed-run"
    iterations: int = 25
    max_approaches: int = 3
    vigilant: bool = False
    on_complete: str = "exit"  # idle | exit (idle is an explicit debugging opt-in)
    on_complete_cmd: str | None = None  # shell hook run once at terminal state
    reflect: bool = False  # run one extra 'reflect' iteration after terminal state
    job_timeout_s: int = 8 * 3600
    iteration_timeout_s: int = 45 * 60
    # any pi "provider/model" ref; the plain fallback every hat resolves to
    # when neither `model_overrides`/`models` nor (for `router`) `router_model`
    # names anything for it. `fast_model`/`model_strategy` (a phase->tier
    # strategy table) are RETIRED (task 006) -- see `_migrate_legacy_model_config`
    # above for the one place a job.yaml carrying either key is still honoured.
    model: str | None = DEFAULT_HAT_MODEL_REF  # pi's own default model when None
    # Task 005 (#60 pillar 4, requirement A): `model_overrides` is the
    # RETAINED name for the per-hat table and stays THE mechanism `model_for`
    # consults first -- `models` (below) is accepted as its alias for at
    # least this release (merged into this dict in `__post_init__`, never
    # silently dropped) so a job.yaml can spell either `model_overrides:` or
    # `models:` and land in the exact same table. Covers all six
    # `MODEL_HATS` (phase/hat name → model ref); an absent entry falls
    # through to `model_for`'s other rungs, never to a second copy of this
    # dict.
    model_overrides: dict = field(default_factory=dict)  # phase → model ref
    # The new canonical spelling from #60 pillar 4's own `models:` example.
    # Kept as a SEPARATE field (not just a `model_overrides` rename) so
    # `JobConfig.load()`'s existing `raw key in known-fields` filter picks it
    # up for free from a `models:` job.yaml key with zero extra parsing code,
    # then `__post_init__` folds it into `model_overrides` once, in one place.
    models: dict = field(default_factory=dict)  # alias for model_overrides
    thinking: str | None = None       # pi --thinking level
    api_token: str | None = None
    # Task 004 (#62 candidate 1, requirement A): a second, mandatory-by-
    # default secret gating *only* the mutating half of the API (see this
    # module's `MUTATING_METHODS`/`classify_method`, above). Independent
    # of `api_token`: `api_token` is an operator opt-in that (when set)
    # gates every route, unit tests that build a JobConfig directly never
    # set it, and mutating routes stay reachable exactly as before for
    # them. `control_token` is what `ralphctl start`/`resume` provision
    # unconditionally (task 004), so a real deployment closes the mutating
    # surface even when the operator never passed `--api-token` at all.
    # Checked against the `X-Ralphd-Control-Token` header, never
    # `Authorization`, so both gates can be satisfied independently in one
    # request.
    control_token: str | None = None
    # Infra-fault fail-fast/retry knobs (task 001a). Startup window: how long
    # a planning/worker iteration may run with ZERO observed LLM traffic
    # (no parseable pi NDJSON event at all) before the engine kills it as an
    # infra fault rather than waiting for the full iteration_timeout_s.
    # Backoff schedule: escalating sleep between retries of the SAME
    # phase/iteration after an infra-classified failure (task 006/#5:
    # 2/5/15/30/60/120/300s, last value repeating up to
    # infra_retry_backoff_max_s).
    #
    # infra_outage_budget_s is the real stopping rule: retries continue
    # while the cumulative wait for one fault episode stays under it.
    # infra_retry_max is a back-compat ATTEMPT CAP that is honoured ONLY
    # when set explicitly (job.yaml key or RALPHD_INFRA_RETRY_MAX) -- the
    # default `None` sentinel means "no attempt cap, retry as long as the
    # outage budget allows". Existing job.yaml files and tests that pin a
    # cap keep their old behaviour; nothing new should introduce one.
    #
    # All of these are overridable via env vars
    # (RALPHD_INFRA_STARTUP_TIMEOUT, RALPHD_INFRA_RETRY_BACKOFF_S,
    # RALPHD_INFRA_RETRY_BACKOFF_MAX_S, RALPHD_INFRA_RETRY_MAX,
    # RALPHD_INFRA_OUTAGE_BUDGET_S) so tests (and operators who want a
    # tighter/looser policy) don't need a job.yaml edit for every run.
    infra_startup_timeout_s: float = 150.0
    infra_retry_backoff_s: list = field(
        default_factory=lambda: list(DEFAULT_INFRA_RETRY_BACKOFF_S))
    infra_retry_backoff_max_s: float = DEFAULT_INFRA_RETRY_BACKOFF_MAX_S
    infra_retry_max: int | None = None
    infra_outage_budget_s: float = DEFAULT_INFRA_OUTAGE_BUDGET_S
    # Task 052 (#10): optional host-side pricing map (see engine/pricing.py),
    # normally inlined into job.yaml from `<registry>/config.yaml`'s
    # `pricing:` key by `ralphctl start`. Used ONLY when the provider quotes
    # no price; the derived cost is published separately and marked derived,
    # never merged into provider-reported `costUSD`. Overridable per run with
    # RALPHD_PRICING (a JSON object of the same shape).
    # Task 010 (requirement C, #60 pillar 1, fact 19): a hat's final output
    # is a response contract, not provider-enforced (fact 4) -- a malformed,
    # absent or wrong-shape payload is an ordinary case the harness
    # re-prompts, carrying the specific validation error back to the hat.
    # `contract_retry_max` is how many RE-PROMPTS are granted after the
    # first attempt (so `contract_retry_max + 1` total attempts) before the
    # harness gives up and scores the iteration as an agent fault against
    # the budget, continuing per the table rather than hanging forever on a
    # hat that never recovers. Overridable with RALPHD_CONTRACT_RETRY_MAX;
    # see `engine/contract_enforcement.py` for the mechanism itself (not
    # wired into `_run_job_core` yet -- that is task 014's cutover).
    contract_retry_max: int = 2
    # Task 018 (requirement E, #60 pillar 2): the router's own model and
    # latency budget. `router_model` is one new key through the EXISTING
    # `model_overrides` mechanism (`model_overrides["router"]` still wins
    # when set, see `model_for`) -- this is deliberately NOT pillar 4's
    # per-hat model roles (Non-goals): one key, not a config rewrite.
    # Unset (`None`), it resolves to the fast tier (a one-question
    # classification never needs the strong model) via `model_for`.
    # Overridable with RALPHD_ROUTER_MODEL / RALPHD_ROUTER_TIMEOUT_S.
    # `router_retry_max` reuses `contract_retry_max`'s N-then-fallback shape
    # rather than adding a second retry-count knob (task 010's policy,
    # requirement E's acceptance: "the loop ... falls back to the
    # deterministic choice the table declares" after N failures) -- see
    # `LoopSupervisor._step_generic_hat`.
    router_model: str | None = None
    router_timeout_s: float = 60.0
    pricing: dict = field(default_factory=dict)
    # Task 010 (#14): `none` (default) | `aws`. When `aws`, a route the
    # provider did not price (or priced with an implausible zero) is costed
    # from the built-in AWS Bedrock table; the result is still published as
    # *derived*, never as provider-quoted `costUSD`. Overridable per run with
    # RALPHD_PRICE_STRATEGY, and settable from an LLM profile / `ralphctl
    # start --price-strategy` (both land in job.yaml on the host side).
    price_strategy: str = DEFAULT_PRICE_STRATEGY
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Normalise here, not only in `load`, so a JobConfig built directly
        # (tests, the API, future callers) can never carry an unknown
        # strategy that a downstream `== "aws"` check would silently treat as
        # "not aws" with no warning.
        self.price_strategy = normalize_price_strategy(self.price_strategy)
        # Task 005: fold the `models:` alias into `model_overrides` -- the one
        # table `model_for` actually reads -- exactly once, here, so a
        # JobConfig built directly (tests, the API) behaves identically to
        # one loaded from job.yaml. `model_overrides` wins on a key both name,
        # since it is the pre-existing, more specific escape hatch; `models`
        # only fills in keys `model_overrides` left unset.
        if self.models:
            merged = dict(self.models)
            merged.update(self.model_overrides)
            self.model_overrides = merged

    @classmethod
    def load(cls, path: Path | None = None) -> JobConfig:
        path = path or CONFIG_DIR / "job.yaml"
        raw = {}
        if path.exists():
            raw = yaml.safe_load(path.read_text()) or {}
        # Task 006: translate any legacy `model_strategy`/`fast_model` keys
        # into `models` BEFORE the known/extra split below, so they land as
        # a real per-hat override rather than silently in `extra`.
        raw = _migrate_legacy_model_config(raw)
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        kwargs = {k: v for k, v in raw.items() if k in known}
        cfg = cls(**kwargs, extra={k: v for k, v in raw.items() if k not in known})
        if tok := os.environ.get("RALPHD_API_TOKEN"):
            cfg.api_token = tok
        if tok := os.environ.get("RALPHD_CONTROL_TOKEN"):
            cfg.control_token = tok
        if v := os.environ.get("RALPHD_INFRA_STARTUP_TIMEOUT"):
            cfg.infra_startup_timeout_s = float(v)
        if v := os.environ.get("RALPHD_INFRA_RETRY_BACKOFF_S"):
            cfg.infra_retry_backoff_s = [float(x) for x in v.split(",") if x]
        if v := os.environ.get("RALPHD_INFRA_RETRY_BACKOFF_MAX_S"):
            cfg.infra_retry_backoff_max_s = float(v)
        if v := os.environ.get("RALPHD_INFRA_RETRY_MAX"):
            cfg.infra_retry_max = int(v)
        if v := os.environ.get("RALPHD_INFRA_OUTAGE_BUDGET_S"):
            cfg.infra_outage_budget_s = float(v)
        if v := os.environ.get("RALPHD_PRICING"):
            try:
                parsed = json.loads(v)
            except json.JSONDecodeError:
                log.warning("RALPHD_PRICING is not valid JSON -- ignored")
            else:
                if isinstance(parsed, dict):
                    cfg.pricing = parsed
        if v := os.environ.get("RALPHD_PRICE_STRATEGY"):
            cfg.price_strategy = normalize_price_strategy(v)
        if v := os.environ.get("RALPHD_CONTRACT_RETRY_MAX"):
            cfg.contract_retry_max = int(v)
        if v := os.environ.get("RALPHD_ROUTER_MODEL"):
            cfg.router_model = v
        if v := os.environ.get("RALPHD_ROUTER_TIMEOUT_S"):
            cfg.router_timeout_s = float(v)
        return cfg

    def effective(self) -> dict:
        """Effective job config for `GET /config` (PRD req 10): budgets,
        flags, model strategy, plus (via helpers below, composed by the
        caller) prompt sources and skills/creds *names*. Deliberately
        excludes `api_token` and `extra` (operator-supplied, may contain
        arbitrary/secret-shaped values) -- only the well-known, non-secret
        fields are surfaced."""
        return {
            "runId": self.run_id,
            "budgets": {
                "iterations": self.iterations,
                "maxApproaches": self.max_approaches,
                "jobTimeoutS": self.job_timeout_s,
                "iterationTimeoutS": self.iteration_timeout_s,
                "infraStartupTimeoutS": self.infra_startup_timeout_s,
                "infraRetryBackoffS": list(self.infra_retry_backoff_s),
                "infraRetryBackoffMaxS": self.infra_retry_backoff_max_s,
                # null = no explicit attempt cap (retry within the budget).
                "infraRetryMax": self.infra_retry_max,
                "infraOutageBudgetS": self.infra_outage_budget_s,
                # Task 018 (requirement E): the router's latency budget --
                # a call that hangs cannot stall the loop; see
                # `LoopSupervisor._ask_router` / `engine.router.decide`.
                "routerTimeoutS": self.router_timeout_s,
            },
            "flags": {
                "vigilant": self.vigilant,
                "onComplete": self.on_complete,
                "onCompleteCmd": self.on_complete_cmd,
                "reflect": self.reflect,
            },
            # Task 010: the response-contract re-prompt policy (requirement
            # C). `retryMax` is re-prompts granted after the first attempt
            # -- `retryMax + 1` total attempts before an agent fault.
            "contracts": {
                "retryMax": self.contract_retry_max,
            },
            "model": {
                "model": self.model,
                # Task 018 (requirement E): the router's own model key,
                # surfaced next to the per-hat table it borrows a fallback
                # from -- `model_for("router")` resolves this, then `model`,
                # unless `overrides["router"]` is set (checked first).
                "routerModel": self.router_model,
                "overrides": dict(self.model_overrides),
                "thinking": self.thinking,
            },
            # Task 052 (#10): the host-side rate table as resolved (null when
            # none is configured, and the shipped default IS none) -- so an
            # operator reading a derived cost can see which rates produced it.
            "pricing": (pm.describe()
                        if (pm := PricingMap.from_config(self.pricing)) else None),
            # Task 010 (#14): which built-in rate table (if any) may derive a
            # cost for an unpriced route -- always the *effective* value, so
            # an unknown configured name shows up here as the `none` it
            # actually behaves as.
            "priceStrategy": self.price_strategy,
            # Task 011 (#14): every table that may derive a cost, in the order
            # they are consulted, so "which table produced this rate" is
            # answerable from the API alone: `names`/`answers` name the
            # operator map, the built-in AWS table, both, or "neither", and
            # the built-in table's entry carries its as-of date + staleness.
            "priceTables": price_tables(self.pricing, self.price_strategy),
        }

    def model_for(self, phase: str) -> str | None:
        """Resolve the pi model ref for a phase; None = pi's own default.

        Task 006 retired the strategy-tiered rung (`STRATEGY_TIERS`/
        `model_strategy`/`fast_model`) entirely: the only rungs left are the
        per-hat table and the bare `model` field. An old job.yaml that still
        names a strategy never reaches here with one -- `load()` already
        translated it into `model_overrides` via `_migrate_legacy_model_config`.
        """
        if override := self.model_overrides.get(phase):
            return override
        # Task 005 (#60 pillar 4, requirement A): a hat name that is not one
        # of the six `MODEL_HATS` -- a typo, not a real role -- degrades via
        # `UNRECOGNISED_HAT_FALLBACK` ("planning"'s own configured entry),
        # never via a second mechanism. Falls through to the bare `model`
        # field only when even that borrowed entry is unset, so a table with
        # nothing configured at all still behaves like today (None = pi's
        # own default).
        if phase not in MODEL_HATS:
            return (self.model_overrides.get(UNRECOGNISED_HAT_FALLBACK)
                    or self.model)
        # Task 018 (requirement E): the router's own `router_model` knob,
        # then the plain `model` field -- no implicit fast tier without an
        # explicit `model_overrides`/`models` entry (task 006).
        if phase == "router":
            return self.router_model or self.model
        # Every other known hat with no per-hat entry falls through to the
        # bare `model` field -- there is no second, strategy-driven rung any
        # more (task 006).
        return self.model

