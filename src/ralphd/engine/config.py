"""Job configuration loaded from /config/job.yaml (or env fallbacks)."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import privsep
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


# Task 001 (PRD requirement A, #269): the hard cap on any text this engine
# hands its own notifier, everywhere -- the #269 incident was a 45,876-
# character `operator_input_needed` escalation body the notifier's own
# 16,000-character cap silently refused (422) and dropped, never seen by
# the operator. Set well below that 16,000-character notifier cap so this
# engine-side bound is always the one that fires first, with headroom for
# whatever transport wrapping (chat client quoting, etc.) sits between this
# engine and the notifier.
MAX_NOTIFY_CHARS = 3500

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

# v0.30 F.3/F.4: the ceiling on a run's total rate-limit waiting (a day), and
# the utilization of an `allowed_warning` window at which a `claude-code`
# iteration waits for the reset instead of starting.
DEFAULT_RATE_LIMIT_MAX_WAIT_S = 86400
DEFAULT_RATE_LIMIT_PREEMPT_UTILIZATION = 0.95

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


def normalize_billing(value: object) -> dict | None:
    """Coerce a job.yaml `billing:` value to its canonical dict (#298).

    Never raises: only a non-mapping degrades to `None` (a legacy, unstamped
    run); every other defect is repaired with a warning, so a typo never
    turns a stamped run into an unstamped one. The effective value is visible
    in `GET /config` (`billing`), so a degraded hand-edit is observable.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        log.warning("billing %r is not a mapping -- run is not stamped", value)
        return None

    def _text(v: object) -> str | None:
        if not isinstance(v, str):
            return None
        clean = BILLING_LABEL_CONTROL_RE.sub(" ", v).strip()
        if clean and clean != v.strip():
            log.warning("billing label %r holds control characters -- "
                        "using %r", v, clean)
        return clean or None

    harness = str(value.get("harness") or "").strip()
    if harness not in HARNESSES:
        harness = DEFAULT_HARNESS
    profile = _text(value.get("profile"))
    payer = _text(value.get("payer"))
    if payer is None:
        payer = profile or BILLING_UNLABELLED_PAYER
        log.warning("billing payer %r is not a non-blank string -- using %r",
                    value.get("payer"), payer)
    basis = str(value.get("basis") or "").strip().lower()
    if basis not in BILLING_BASES:
        log.warning("billing basis %r is not one of %s -- using %r",
                    value.get("basis"), "|".join(BILLING_BASES),
                    DEFAULT_BILLING_BASIS)
        basis = DEFAULT_BILLING_BASIS
    forced = HARNESS_FORCED_BASIS.get(harness)
    if forced and basis != forced:
        log.warning("billing basis %r contradicts harness %r (always %r) -- "
                    "using %r", basis, harness, forced, forced)
        basis = forced
    if basis in HARNESS_REFUSED_BASES.get(harness, ()):
        log.warning("billing basis %r contradicts harness %r (never %r) -- "
                    "using %r", basis, harness, basis, DEFAULT_BILLING_BASIS)
        basis = DEFAULT_BILLING_BASIS
    return {"payer": payer, "basis": basis, "profile": profile,
            "harness": harness,
            "subscription_payer": (_text(value.get("subscription_payer"))
                                   or DEFAULT_SUBSCRIPTION_PAYER)}


# Phase names with a builtin prompt (see src/ralphd/prompts/); the only names
# accepted by the prompts CRUD API (PRD req 10).
PROMPT_NAMES = ("planning", "worker", "review", "task-verify")


def prompt_source(name: str) -> str:
    """Effective origin for a phase prompt: 'api' (runtime overlay PUT) >
    'mounted' (/config/prompts/{name}.md, operator-provided) > 'builtin'.

    Task 024 (#180 class): the mounted-path check below reads under
    `CONFIG_DIR`, exactly like `JobConfig.load()` (task 023, E.1) -- same
    borrow, same reason (a uid mismatch between the invoking host user and
    the engine's own effective uid otherwise makes this raise EACCES
    instead of returning a plain False).
    """
    if (OVERLAY_DIR / f"prompts/{name}.md").exists():
        return "api"
    with privsep.root_for_write():
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
# All seven hats `model_for` resolves: the six `STATE_HAT` roles
# (transitions.py) plus the router, which is not state-tiered but still gets
# a per-hat table entry (`router_model`/`models["router"]`).
#
# `steer` (task 009, PRD requirement G, #98): a REAL entry here, resolved
# through the exact same `model_for`/`bundle_for` machinery every other
# known hat uses -- never through `UNRECOGNISED_HAT_FALLBACK`, which is
# reserved for a hat name that is NOT one of this tuple's members (a typo).
# Its shipped default, like every other hat's, is simply "whatever `model`
# resolves to when nothing configures it explicitly" -- `DEFAULT_HAT_MODEL_
# REF`/`None`, same as the rest.
#
# `plan_review` (task 018, PRD requirement K, #101): the peer plan-review
# hat joins the SAME way -- a real entry, a real shipped default, never the
# unrecognised-hat fallback, whether or not `JobConfig.peer_plan_review`
# (the opt-in gate, off by default) is actually turned on for a given run.
#
# `verdict_gate` (task 020, PRD requirement L, #101): the peer VERDICT gate
# joins on the SAME axis, a real entry either way, gated by
# `JobConfig.peer_verdict_gate` (also off by default) for reachability only
# -- never for whether `check_hat_routes`'s start-time fail-fast
# (`tests/test_hat_model_routes.py`) covers a misconfigured ref here, per
# requirement M's own note that an opt-in gate's unroutable model must fail
# exactly like any other hat's, never silently degrade to "no gate".
MODEL_HATS: tuple[str, ...] = (
    "planning", "worker", "verify", "review", "reflect", "router", "steer",
    "plan_review", "verdict_gate")

# The agent harnesses a hat can run on. `pi` is the default and the only one
# every hat has always used; `claude-code` runs the hat on the Claude Code
# CLI; `codex` runs an OpenAI-family hat on the Codex CLI. A hat's harness is resolved by `JobConfig.harness_for` and carried on
# its `HatBundle`.
HARNESSES: tuple[str, ...] = ("pi", "claude-code", "codex")
DEFAULT_HARNESS = "pi"
# The harness that reports rate-limit state (v0.30 F): only its hats wait
# on a rate limit or are preempted near one.
CLAUDE_CODE_HARNESS = "claude-code"
CODEX_HARNESS = "codex"

# #298: who pays for a hat's tokens. `basis` is one of BILLING_BASES; the
# zero-priced ones (subscription/free) bill nothing per token, so their
# money is reported as a list-equivalent instead of a cost.
BILLING_BASES: tuple[str, ...] = ("metered", "subscription", "chargeback", "free")
DEFAULT_BILLING_BASIS = "metered"
ZERO_PRICED_BASES: tuple[str, ...] = ("subscription", "free")
# The ONE engine-side statement that the claude-code runner always bills the
# subscription. The profile validator and `JobConfig.billing_for` both read
# it. It is the data twin of `runner.ClaudeCodeRunner._mark_subscription`
# (the cost-side code); a test pins the two together.
HARNESS_FORCED_BASIS: dict[str, str] = {CLAUDE_CODE_HARNESS: "subscription"}
# The codex harness is deliberately NOT in `HARNESS_FORCED_BASIS`: its hats
# bill `metered` by default and a profile may still label them `chargeback`
# or `free`, but no codex hat ever bills a subscription. A basis listed here
# for a harness is refused by the profile validator and `ralphctl start`, and
# repaired to `metered` by `normalize_billing` and `JobConfig.billing_for`.
HARNESS_REFUSED_BASES: dict[str, tuple[str, ...]] = {
    CODEX_HARNESS: ("subscription",)}
# Derived from the harness name so no vendor/product name is spelled here;
# the operator renames it through the registry `billing:` knob.
DEFAULT_SUBSCRIPTION_PAYER = f"{CLAUDE_CODE_HARNESS} subscription"
HOST_BILLING_PAYER = "host models.json"
BILLING_UNLABELLED_PAYER = "(unlabelled payer)"
# A payer label is printed into line-oriented output (`ralphctl cost`/
# `status`, the run record), so a control character -- a newline above all --
# would forge extra lines there. Profiles and flags refuse one; a hand-edited
# job.yaml has each run of them collapsed to a space.
BILLING_LABEL_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")

# The minimum Claude Code CLI version required to run iterations on the
# claude-code harness. The wrapper checks this at startup and refuses to run
# below it (requirement J.2).
CLAUDE_CODE_MIN_VERSION = "2.1.280"

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


def _model_family(ref: str | None) -> str:
    """Coarse model-family classifier for requirement C's provider-shaped
    thinking bundle (task 007). Looks at the ref text itself (a pi
    `provider/model` string, e.g. `amazon-bedrock/eu.anthropic.claude-opus-5`
    or `aigw-openai/openai.gpt-5.6-terra`) rather than a provider registry, so
    it needs no network access and works before requirement D's startup
    route check runs.

    Only two buckets exist: `"openai"` for a ref naming that family (its own
    differently-spelled `reasoning_effort` knob, PRD requirement C) and
    `"native"` for every other family, including Anthropic/Bedrock -- the
    vocabulary `pi --thinking` already speaks natively, so there is nothing
    to translate for them.
    """
    if not ref:
        return "native"
    return "openai" if "openai" in ref.lower() else "native"


@dataclass(frozen=True)
class HatBundle:
    """A hat's own (model ref, thinking/reasoning setting) bundle (task 007,
    PRD requirement C): what `JobConfig.bundle_for` resolves and what
    `loop.py`'s dispatch now hands to `runner.PiRunner.run`, replacing the
    single global `cfg.thinking` string it used to pass unconditionally to
    every hat's invocation.

    Provider-shaped: at most ONE of `thinking` (pi's native `--thinking`
    vocabulary -- off/minimal/low/medium/high/xhigh/max, spoken by every
    family this table ships a default for today, Anthropic/Bedrock
    included) or `effort` (the OpenAI family's own differently-spelled
    reasoning-effort knob) is ever populated on a bundle `bundle_for`
    produces -- never both -- so an OpenAI-family hat is never handed an
    Anthropic thinking level it did not ask for, and vice versa (see
    `bundle_for`'s docstring for exactly when each is chosen).
    """
    model: str | None = None
    thinking: str | None = None
    effort: str | None = None
    # The agent harness the hat runs on (`HARNESSES`). A hat's tiers share
    # it: a tier rung never carries its own harness.
    harness: str = DEFAULT_HARNESS


@dataclass
class JobConfig:
    run_id: str = "unnamed-run"
    iterations: int = 25
    max_approaches: int = 3
    vigilant: bool = False
    on_complete: str = "exit"  # idle | exit (idle is an explicit debugging opt-in)
    on_complete_cmd: str | None = None  # shell hook run once at terminal state
    reflect: bool = False  # run one extra 'reflect' iteration after terminal state
    # Task 014 (PRD requirement A.1, #224): the pause-before-archive gate.
    # ON by default (#224's design decision 2, PRD "Enabled by default"):
    # a rejection that would otherwise archive the approach (whether the
    # next stop is a fresh `replan` or the terminal `rejected_no_approach_
    # left` tail) instead pauses the run with `pauseReason:
    # "operator_input_needed"` -- `LoopSupervisor._step_review`'s own new
    # `_pause_and_escalate_operator_input_needed` sibling on the existing
    # pause seam, checked after every other rejection edge (incurable,
    # cure-pass-available, last-approach-curable, repeated-signature) keeps
    # its own precedence exactly as before, but strictly BEFORE `_archive_
    # approach`/`_write_composite_prd` ever run. `ralphctl start
    # --no-operator-input-needed-pause` sets this False and writes
    # `operator_input_needed_pause: false` to the run's `job.yaml` --
    # required, not optional (a job.yaml-only key would force the `start`
    # -> `docker stop` -> edit -> `resume` dance this wave removes), so an
    # unattended run that wants the pre-014 behaviour back can still ask
    # for it at launch. This changes behaviour for every EXISTING job.yaml
    # on upgrade and every unattended run that never sets the flag -- the
    # operator's explicit ruling, and requirement A.7's notifier retry plus
    # listing marker (a later task) are what make that safe.
    operator_input_needed_pause: bool = True
    # Task 018 (PRD requirement K, #101): the opt-in peer plan-review gate.
    # OFF by default -- `transitions.table_for(vigilant)` (no third
    # argument) is byte-identical to today's table whenever this stays
    # False (`tests/test_peer_plan_review_gate.py`). When True, `planning`'s
    # `planning_produced_tasks` edge targets `plan_review` instead of
    # `worker`, and a peer model either approves the plan or objects,
    # routing back to `planning` for a revision -- bounded by
    # `peer_plan_review_max_revisions` below, never "until it agrees".
    peer_plan_review: bool = False
    # An explicit NUMBER (decision recorded in `artifacts/reports/
    # task-018-requirement-k-budget-decision.md` and this task's own commit
    # message): the maximum number of `planning -> plan_review -> planning`
    # revision round trips ONE approach may spend on an objecting peer
    # before the plan proceeds to `worker` regardless
    # (`plan_review_revision_bound_reached`). Reset to 0 at the start of
    # every approach (`LoopSupervisor._record_plan_review_revision`).
    peer_plan_review_max_revisions: int = 2
    # Task 021 (PRD requirement N, #102): an explicit NUMBER -- the maximum
    # count of NEW tasks a worker's optional `proposed_tasks` split
    # (`transitions.HAT_SCHEMAS["worker"]`) may add to the plan across ONE
    # approach. Without a stated cap this is a plan-growth loop -- the same
    # reasoning `peer_plan_review_max_revisions` above states for its own
    # bound. Tracked in `status.json`'s `workerSplitProposals[str(approach)]`
    # (accepted-task count, not proposal-batch count), the same disk-backed,
    # per-approach-keyed shape `planReviewRevisions` already uses so it
    # survives a resumed process. A proposal that would push the running
    # total over this budget is refused whole (no partial insertion) --
    # see `LoopSupervisor._apply_worker_proposed_tasks`.
    worker_split_budget_per_approach: int = 3
    # #209 defect 7: an explicit NUMBER -- the maximum count of EXISTING
    # tasks a worker's optional `proposed_task_updates` re-point
    # (`transitions.HAT_SCHEMAS["worker"]`) may move across ONE approach.
    # Tracked in `status.json`'s `workerRepointUpdates[str(approach)]`, the
    # same disk-backed, per-approach-keyed shape `workerSplitProposals`
    # above and `planReviewRevisions` already use, so it survives a resumed
    # process; a batch that would push the running total past this budget is
    # refused whole (no partial re-point) -- see `LoopSupervisor._apply_
    # worker_task_updates`. Deliberately its OWN knob rather than sharing
    # `worker_split_budget_per_approach`: that counter is documented (SPEC
    # §5.2, `docs/api.md`) as the running total of tasks a split INSERTED,
    # and a re-point inserts nothing, so sharing it would let three
    # re-points silently forbid a needed split and make a shipped status
    # field's documented meaning wrong.
    #
    # The bound is also the stagnation answer, and that is not incidental.
    # A re-point mutates an EXISTING row, so unlike an inserted split task
    # it is NOT filtered out of the pass's progress diff by
    # `state.tasks_doc_excluding_new_entries` (which drops whole brand-new
    # worker-origin ROWS) and so it DOES read as progress and reset
    # `stagnant`. This number is what keeps that to at most three free
    # resets per approach instead of an unbounded escape from the
    # 3-iteration no-progress guard.
    worker_repoint_budget_per_approach: int = 3
    # Task 020 (PRD requirement L, #101): the opt-in peer VERDICT gate.
    # OFF by default -- `transitions.table_for(vigilant)` (no extra
    # arguments) stays byte-identical to today's table whenever this
    # stays False (`tests/test_peer_verdict_gate.py`). When True,
    # `review`'s own `verified` edge (once every OTHER terminal-success
    # blocker -- `_terminal_success_blockers()` -- is already clear)
    # targets `verdict_gate` instead of `finishing` directly: a SECOND,
    # genuinely BLIND peer model re-verifies the completed work -- see
    # `prompts/verdict_gate.md` for exactly what it is and is not handed --
    # before the run may reach terminal success. A `rejected` verdict
    # folds the peer's findings into a FRESH approach (`replan`), never a
    # targeted worker fix-up -- the decided rejection semantics, recorded
    # in this task's own commit message and `artifacts/reports/task-020-
    # requirement-l-rejection-semantics.md`.
    peer_verdict_gate: bool = False
    # Task 022 (PRD requirement K.2, #169): the mid-plan review episode's
    # own opt-in flag -- OFF by default, same "gate off -> today's engine
    # unchanged" shape `peer_plan_review`/`peer_verdict_gate` above already
    # carry. When True, `LoopSupervisor._maybe_run_mid_plan_review_episode`
    # (called from the same `_step_worker` seam the mid-run petition-
    # adjudication episode already uses, `loop.py`) runs one bounded review
    # iteration per approach, scoped to whichever tasks have already
    # reached this run's terminal-success status (`validated` under
    # vigilant, the only variant that ever produces one before the plan is
    # done), once the declared fraction `loop.MID_PLAN_REVIEW_FRACTION` of
    # the plan's own tasks has reached it. Its findings open cure tasks
    # through the SAME `state.insert_cure_tasks` path an ordinary
    # run-ending review's cure pass already uses -- no new cure mechanism,
    # no new FSM state (unlike the two peer gates above, this episode
    # never advances through `transitions.TABLE` at all, exactly like the
    # mid-run adjudication episode it is modelled on). Never wired into
    # `table_for`/`successors`/`is_legal` for that reason.
    mid_plan_review: bool = False
    # Task 006 (PRD requirement B.1, #122): an explicit NUMBER -- the
    # maximum number of times ONE steering message may be redelivered
    # (unconsumed and handed back to a following steer iteration) after a
    # clean iteration honestly claims `settled: false` for it, before the
    # message stops being redelivered and is instead marked
    # settled-by-exhaustion in the steering ledger. Counted per message
    # name in status.json's `steerRedeliveries[<name>]`, the same
    # disk-backed, survives-a-resume shape `peer_plan_review_max_revisions`
    # /`planReviewRevisions` above already uses for its own bounded loop.
    # `settled: true` never touches this counter at all -- it consumes
    # exactly once, as before this task.
    steer_redelivery_max: int = 3
    # Task 002 (PRD requirement A.2, #192): an explicit NUMBER -- the
    # ceiling on how many cure passes ONE RUN may take in total, summed
    # across every approach the run schedules AND across both cure
    # channels this engine has (review's own `curePasses` and the peer
    # verdict gate's `gateCurePasses`, task 001). The two counters stay
    # per-approach on status.json for the record, but the ceiling is
    # compared against their RUN-WIDE total (`LoopSupervisor._cure_
    # passes_for_run`), which is what makes A.2's claim true: a run
    # cannot loop cure -> reject -> cure indefinitely by starting a fresh
    # approach, because a fresh approach no longer resets the budget.
    # Read from configuration at the rejection site
    # (`LoopSupervisor._review_cure_pass_available`/`_gate_cure_pass_
    # available`, both now `self.cfg.cure_pass_ceiling`) rather than from
    # the module-level `CURE_PASS_CEILING` literal in `engine/loop.py`
    # both channels read directly before this task, so a job.yaml (or a
    # future `start`/`resume` flag) can widen or narrow it per run
    # without an engine change. Default `3`, unchanged from the literal
    # it replaces -- see `docs/api.md`'s `curePasses`/`gateCurePasses`
    # rows for the default stated where an operator surface reads it.
    cure_pass_ceiling: int = 3
    # #208: the operator-authorized disposable-clone protocol for the judging
    # hats (`prompts/review.md`, `prompts/verdict_gate.md`), as a path to an
    # executable inside the workspace. Those prompts require every measured
    # figure to come from a throwaway clone that has PROVEN it is measuring
    # itself rather than the live tree or the engine's own installed copy;
    # the `pip install -e .` / `ralphd.__file__` recipe they spell out is one
    # ecosystem's way to establish that, and it is unsatisfiable by
    # construction on a product with no `pyproject.toml`. A run pointed at a
    # Go module therefore had three consecutive reviews execute zero tests
    # and reject `curable: false`, because a hat honouring its prompt
    # legitimately cannot measure anything once the bootstrap fails -- and
    # correctly refused a Go analogue the run had written for itself, since
    # nothing authorized substituting it.
    #
    # Naming the script here is that authorization, granted at launch where
    # it is auditable, instead of through a mid-run `amend --wave`. `None`
    # (the default) leaves the prompts' own Python recipe as the protocol,
    # so every existing job.yaml behaves exactly as before. The value is a
    # command line, not merely a bare path (#265): a script that itself
    # requires an argument (e.g. `ci/review.sh --smoke`) is named through
    # this same field, and the whole value is rendered verbatim -- the
    # engine never introduces a shell, never parses it into argv, and
    # invents no quoting convention of its own. The engine does not run,
    # read or validate it either way: it only tells the judging hats that
    # this command line is the sanctioned protocol, and the hat runs it, as
    # written, and requires exit 0 before it may cite a measurement.
    # Rendered into every hat's `## Job context` by
    # `LoopSupervisor._review_clone_protocol_note`.
    review_clone_protocol: str | None = None
    # Task 022 (PRD requirement B.3, #140): the endpoint the engine-owned
    # `notify` hat tool (`engine/hat_tools.py`) POSTs to, from the engine
    # process itself -- available to the worker and steer hats through the
    # same request/response spool the steer hat's task-table tools already
    # use. `None` (the default: no `notifier_url:` in job.yaml and no
    # `RALPHD_NOTIFIER_URL`) means unconfigured -- the tool still answers
    # (and still records a `notification.failed` event -- task 009, PRD
    # requirement B.1) rather than raising, it just reports "no notifier
    # URL configured" and never sends. Kept
    # OUT of `effective()`/`GET /config`, the same way `api_token` is: an
    # operator-supplied endpoint, not a secret exactly, but not part of
    # this task's scope to also surface on the control-plane API.
    notifier_url: str | None = None
    # Cure task cure-01-04 (PRD requirement A.7, F04): the origin an
    # `operator_escalation`'s `hubLink` is built against -- `None` (the
    # default: no `hub_base_url:` in job.yaml and no `RALPHD_HUB_BASE_URL`)
    # means unconfigured, exactly like `notifier_url` just above; the
    # engine has no channel to the port `ralphctl ui --port` chose (it
    # defaults to a free ephemeral one, docs/cli.md), so an operator who
    # wants the emitted link to actually resolve sets this to the address
    # their own running hub is reachable at (e.g. `http://127.0.0.1:8080`
    # after `ralphctl ui --port 8080`). See `LoopSupervisor._hub_deep_
    # link` for the unconfigured fallback. Kept OUT of `effective()`/
    # `GET /config`, the same way `notifier_url` is.
    hub_base_url: str | None = None
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
    thinking: str | None = None       # pi --thinking level, every hat's default
    # Task 007 (PRD requirement C): the per-hat thinking/reasoning table,
    # same shape family as `model_overrides` (phase -> setting) but never
    # merged into it -- a model ref and its thinking setting are two
    # different knobs even though they travel together in `HatBundle`. An
    # entry is EITHER a plain string (pi's native `--thinking` vocabulary,
    # exactly what the bare `thinking` field above already speaks) OR a
    # one-key mapping naming which field of `HatBundle` it fills explicitly
    # -- `{"thinking": "high"}` or `{"effort": "medium"}` -- so a hat whose
    # model is OpenAI-family can carry that family's own differently-spelled
    # effort knob instead of a level word that means nothing to it. See
    # `bundle_for` for the resolution order.
    thinking_overrides: dict = field(default_factory=dict)
    # Task 002 (#68 half 2, review finding 2): the hat x tier host map's OWN
    # job.yaml configuration surface -- a per-(hat, tier) table, one leaf per
    # rung a job.yaml wants to name explicitly. Shape: `{hat: {tier: leaf}}`
    # where `hat`/`tier` are meant to be `MODEL_HATS`/`tiers.TIERS` members
    # (an unrecognised spelling of either is NOT rejected here -- see
    # `engine/tiers.default_host_map`, the one builder that reads this field,
    # for the anti-downgrade rule an unrecognised key degrades through) and
    # `leaf` is either a plain model ref string, or a provider-shaped mapping
    # naming `model`/`thinking`/`effort` explicitly -- the same shape family
    # `thinking_overrides` already speaks, widened with an optional `model`
    # key since a tier rung usually exists to name a DIFFERENT model, not
    # just a different thinking level. A hat/tier this table leaves unset
    # falls through to `bundle_for`'s existing half-1 answer for that hat --
    # nothing here is a second model-resolution pathway; `default_host_map`
    # is still the only place a `JobConfig` is read for this and `bundle_for`
    # is still the only method it calls to do so.
    model_tiers: dict = field(default_factory=dict)  # hat -> tier -> leaf
    # hat -> harness name (`HARNESSES`); an absent hat runs on `pi`.
    harnesses: dict = field(default_factory=dict)
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
    # v0.30 F.3: the ceiling on a run's TOTAL rate-limit waiting, in seconds.
    # A wait that would take the run past it does not start; the run pauses
    # `operator_input_needed` (`pauseDetail.reason == "rate_limit_ceiling"`)
    # instead, and an unpause resets the count. Settable in flight with
    # `PATCH /config/budget` `rateLimitMaxWaitS` (`ralphctl budget
    # --rate-limit-wait`).
    rate_limit_max_wait_s: int = DEFAULT_RATE_LIMIT_MAX_WAIT_S
    # v0.30 F.4: a `claude-code` iteration is not started while the run's last
    # rate-limit report for that harness is `allowed_warning` at or above
    # this utilization (0-1); the loop waits for that window's reset first.
    rate_limit_preempt_utilization: float = DEFAULT_RATE_LIMIT_PREEMPT_UTILIZATION
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
    # How many FRESH, uncharged re-dispatches a judging hat (today the peer
    # `verdict_gate`) is granted when its reply was cut off by the provider
    # (`output_truncated`) or came back empty after the contract ladder
    # (`empty_reply`), before the engine falls back to its no-verdict
    # handling. 0 disables the retry. Negative or non-integer values are
    # refused at construction. Overridable with RALPHD_JUDGE_RETRY_MAX.
    judge_retry_max: int = 2
    # Task 011 (v0.19 PRD requirement B.2, #153) added an explicit NUMBER --
    # how many CONSECUTIVE worker iterations may be spent on the SAME task,
    # each landing real commits but no completion signal, before the engine
    # forced `awaiting-validation` and routed to `verify` on the worker's
    # own behalf (`LoopSupervisor._record_task_absorption_streak`).
    #
    # Task 004 (v0.20.1 PRD requirement B.1, gate finding VG-002):
    # THIS FIELD STOPS HAVING THAT EFFECT rather than going away outright.
    # `_record_task_absorption_streak` and its call site are deleted --
    # nothing anywhere reads `task_absorption_max_iterations` any more, so
    # no consecutive-commit streak can ever force `awaiting-validation` or
    # dispatch `verify` again; the pre-existing progress-budget/forced-split
    # path (task 016, `PROGRESS_BUDGET_STATUS_KEY`, `estimatedIterations +
    # 2`) already governs exhaustion independently and is what B.3/B.4
    # (tasks 005/006) pin next. The field itself (and its env override) is
    # kept, inert, purely so two frozen v0.20 tests unrelated to this task
    # (`tests/test_progress_budget_task013.py`,
    # `tests/test_progress_pin_task015.py`) that pass
    # `task_absorption_max_iterations=3` as a `JobConfig` kwarg keep
    # constructing without a `TypeError` -- deleting the field would widen
    # this task's blast radius onto tests outside its own scope for no
    # behavioural gain, since neither test's assertions depend on the
    # value doing anything. Overridable (inertly) with
    # RALPHD_TASK_ABSORPTION_MAX_ITERATIONS.
    task_absorption_max_iterations: int = 3
    # Task 018 (requirement E, #60 pillar 2): the router's own model and
    # latency budget. `router_model` is one new key through the EXISTING
    # `model_overrides` mechanism, but task 003 (#278 B.6) corrected the
    # rung order to the PRD's own: `router_model` wins OVER
    # `model_overrides["router"]` now (the reverse of this field's own
    # pre-003 claim), then the worker hat's `trivial` tier, then `model` --
    # see `model_for` for the live order. This is deliberately NOT pillar
    # 4's per-hat model roles (Non-goals): one key, not a config rewrite.
    # Overridable with RALPHD_ROUTER_MODEL / RALPHD_ROUTER_TIMEOUT_S.
    # `router_retry_max` reuses `contract_retry_max`'s N-then-fallback shape
    # rather than adding a second retry-count knob (task 010's policy,
    # requirement E's acceptance: "the loop ... falls back to the
    # deterministic choice the table declares" after N failures) -- see
    # `LoopSupervisor._step_generic_hat`.
    router_model: str | None = None
    router_timeout_s: float = 60.0
    # Task 002 (#278 B.1/B.4/B.7) introduced this field False so the pre-
    # existing flow tests' task picks stayed fully deterministic while that
    # task landed; task 003 (PRD B.5) now turns the DEFAULT ON -- the router
    # is asked on `select_next_task`'s pending step whenever `scheduler.
    # pending_candidates` finds 2+ runnable tasks, unless `job.yaml`'s own
    # `route_task_selection: false` or `ralphctl start --no-route-tasks` (the
    # CLI opt-out, `cli/main.py`'s `_TEMPLATE_SCALAR_FIELDS`/`start`
    # argparser) says otherwise. `ralphctl resume` never re-derives this --
    # job.yaml is only rewritten for an explicit budget top-up flag -- so the
    # value a run started with (on by default, or either opt-out) survives
    # every later resume unchanged. Read straight off `JobConfig.load()`'s
    # known/extra split like every other plain job.yaml key -- no env
    # override (task selection routing is a planning-time choice, not an
    # operational knob like the router's latency budget just above).
    route_task_selection: bool = True
    pricing: dict = field(default_factory=dict)
    # Task 010 (#14): `none` (default) | `aws`. When `aws`, a route the
    # provider did not price (or priced with an implausible zero) is costed
    # from the built-in AWS Bedrock table; the result is still published as
    # *derived*, never as provider-quoted `costUSD`. Overridable per run with
    # RALPHD_PRICE_STRATEGY, and settable from an LLM profile / `ralphctl
    # start --price-strategy` (both land in job.yaml on the host side).
    price_strategy: str = DEFAULT_PRICE_STRATEGY
    # #298: who pays for this run's tokens -- `{payer, basis, profile,
    # harness, subscription_payer}`, inlined by `ralphctl start`. `None` (every
    # run recorded before 0.32.1, and `--llm none`) is never stamped and keeps
    # the legacy usage layout. Read through `billing_for`.
    billing: dict | None = None
    # Task 029 (PRD requirement I.2, #210): the task-boundary git sink.
    # `redact.scrub_text` scrubs known secret VALUES out of everything the
    # engine persists or serves; a commit the run's own worker writes is
    # neither, so nothing ever ran a commit's message or diff through the
    # redaction map. ON by default (a leak-prevention gate that ships off
    # prevents nothing): at every worker task boundary the engine scans the
    # commits THIS iteration created -- `head_before..head_after`, the
    # bracketing `_step_worker` already takes -- message and diff, for
    # actual redaction-map values, and on a hit refuses the push, emits a
    # `commit_secret_scan` event and records `commitSecretScanHit` in
    # status.json (what task 030's fault + `operator_input_needed` pause
    # reads). Set False only to get the pre-029 behaviour back.
    commit_secret_scan: bool = True
    # The push half of that same gate, and the one place in the engine that
    # may start a `git push` subprocess at all (`commit_scan.push_if_clean`
    # -- scan first, push only when the scan came back clean). `None` (the
    # default) keeps today's division of labour exactly as it is: the WORKER
    # pushes its own commit from its own shell, and the engine's boundary
    # only scans and records. A job.yaml that names a remote here hands the
    # push to the engine instead, where the scan can gate it mechanically
    # rather than by asking the agent nicely.
    workspace_push_remote: str | None = None
    workspace_push_branch: str = "main"
    extra: dict = field(default_factory=dict)

    def _validate_judge_retry_max(self) -> None:
        v = self.judge_retry_max
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError(
                f"judge_retry_max must be an integer >= 0, got {v!r}")

    def __post_init__(self) -> None:
        # Normalise here, not only in `load`, so a JobConfig built directly
        # (tests, the API, future callers) can never carry an unknown
        # strategy that a downstream `== "aws"` check would silently treat as
        # "not aws" with no warning.
        self.price_strategy = normalize_price_strategy(self.price_strategy)
        self.billing = normalize_billing(self.billing)
        self._validate_judge_retry_max()
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
        # Task 023 (#180, requirement E.1): `/config` is a host directory
        # `ralphctl start` chmod's 0700 and leaves owned by whichever host
        # uid ran it -- only ever 1000 by accident, on a single-user desktop
        # where that happens to be the first account. Under the uid boundary
        # (`main()` -> `privsep.separate_engine_identity()`), the engine's
        # *effective* uid is the agent's (1000) while its real/saved uid stays
        # root, so a host uid other than 1000 leaves the effective-uid check
        # on the 0700 mount an EACCES and the run could never start at all.
        # Borrowing `privsep.root_for_write()` -- the same momentary
        # effective-uid-to-root raise `write_root_owned_json` uses for a
        # write, and `petitions._read_stored_key` already reuses for a read
        # -- makes the read succeed without widening `/config` itself: no
        # mode or ownership bit on the mount changes, and the agent's own
        # effective uid is back in force before this method returns. A
        # no-op (this whole block behaves exactly as before) when there is
        # no boundary to raise into -- the test suite's unprivileged engine,
        # or `docker run --user 1000` with no root start.
        with privsep.root_for_write():
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
        if v := os.environ.get("RALPHD_JUDGE_RETRY_MAX"):
            cfg.judge_retry_max = int(v)
            cfg._validate_judge_retry_max()
        if v := os.environ.get("RALPHD_TASK_ABSORPTION_MAX_ITERATIONS"):
            cfg.task_absorption_max_iterations = int(v)
        if v := os.environ.get("RALPHD_ROUTER_MODEL"):
            cfg.router_model = v
        if v := os.environ.get("RALPHD_ROUTER_TIMEOUT_S"):
            cfg.router_timeout_s = float(v)
        if v := os.environ.get("RALPHD_NOTIFIER_URL"):
            cfg.notifier_url = v
        if v := os.environ.get("RALPHD_HUB_BASE_URL"):
            cfg.hub_base_url = v
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
                # v0.30 F.3/F.4: the rate-limit wait ceiling and the
                # preempt threshold.
                "rateLimitMaxWaitS": self.rate_limit_max_wait_s,
                "rateLimitPreemptUtilization": self.rate_limit_preempt_utilization,
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
                # Task 017 (D.3, #117 side finding): both opt-in peer gates
                # (task 018/020, PRD requirements K/L) surfaced next to the
                # flags they sit beside on `JobConfig` and are launched
                # alongside from `ralphctl start` -- so `GET /config` shows
                # an operator whether either gate is live for this run,
                # not just `vigilant`.
                "peerPlanReview": self.peer_plan_review,
                "peerVerdictGate": self.peer_verdict_gate,
                # Task 014 (PRD requirement A.1, #224): ON by default,
                # surfaced here for the same reason the two peer gates
                # above are -- an operator can see whether this run would
                # pause for a ruling before an approach is archived
                # without reading job.yaml.
                "operatorInputNeededPause": self.operator_input_needed_pause,
                # Task 022 (PRD requirement K.2, #169): surfaced the same
                # way the two peer gates above are -- an operator can see
                # whether this run would ever run a scoped mid-plan review
                # episode without reading job.yaml.
                "midPlanReview": self.mid_plan_review,
                # Task 003 (#278 B.5): surfaced the same way the flags
                # above are -- an operator can see whether this run's
                # task-selection pending step is ever routed through the
                # router without reading job.yaml.
                "routeTaskSelection": self.route_task_selection,
            },
            # Task 010: the response-contract re-prompt policy (requirement
            # C). `retryMax` is re-prompts granted after the first attempt
            # -- `retryMax + 1` total attempts before an agent fault.
            "contracts": {
                "retryMax": self.contract_retry_max,
                "judgeRetryMax": self.judge_retry_max,
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
                # Task 007 (requirement C): the per-hat thinking table
                # alongside the per-hat model table it travels with in
                # `HatBundle` -- see `bundle_for`.
                "thinkingOverrides": dict(self.thinking_overrides),
                # The per-hat agent harness (`HARNESSES`) the table above
                # runs on; every hat in `MODEL_HATS` is listed.
                "harnesses": {h: self.harness_for(h) for h in MODEL_HATS},
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
            # #298: the effective (normalised) billing block, null for a
            # legacy run, so a degraded hand-edit is observable here.
            "billing": dict(self.billing) if self.billing else None,
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
        `model_strategy`/`fast_model`) entirely. Task 003 (#278 B.6)
        corrected `router`'s own rung order to match the PRD: `router_model`
        now wins over `model_overrides`/`models`'s `router` entry (the
        reverse of the pre-003 order), with the worker hat's own `trivial`
        tier (`model_tiers['worker']['trivial']`, when configured) as a new
        third rung between that and the bare `model` field. Every other hat
        keeps the one rung family it always had: the per-hat table, then
        the bare `model` field. An old job.yaml that still names a strategy
        never reaches here with one -- `load()` already translated it into
        `model_overrides` via `_migrate_legacy_model_config`.
        """
        if phase == "router":
            if self.router_model:
                return self.router_model
            if override := self.model_overrides.get("router"):
                return override
            worker_tiers = self.model_tiers.get("worker") \
                if isinstance(self.model_tiers, dict) else None
            if isinstance(worker_tiers, dict):
                leaf = worker_tiers.get("trivial")
                if isinstance(leaf, str) and leaf:
                    return leaf
                if isinstance(leaf, dict) and leaf.get("model"):
                    return leaf["model"]
            return self.model
        if override := self.model_overrides.get(phase):
            return override
        # Task 005 (#60 pillar 4, requirement A): a hat name that is not one
        # of the seven `MODEL_HATS` -- a typo, not a real role -- degrades via
        # `UNRECOGNISED_HAT_FALLBACK` ("planning"'s own configured entry),
        # never via a second mechanism. Falls through to the bare `model`
        # field only when even that borrowed entry is unset, so a table with
        # nothing configured at all still behaves like today (None = pi's
        # own default).
        if phase not in MODEL_HATS:
            return (self.model_overrides.get(UNRECOGNISED_HAT_FALLBACK)
                    or self.model)
        # Every other known hat with no per-hat entry falls through to the
        # bare `model` field -- there is no second, strategy-driven rung any
        # more (task 006).
        return self.model

    def harness_for(self, hat: str) -> str:
        """The harness `hat` runs on: its `harnesses` entry for a known hat,
        planning's entry for an unrecognised name (the same degradation
        `model_for` applies), `pi` when nothing is configured."""
        table = self.harnesses if isinstance(self.harnesses, dict) else {}
        key = hat if hat in MODEL_HATS else UNRECOGNISED_HAT_FALLBACK
        return table.get(key) or DEFAULT_HARNESS

    def billing_for(self, harness: str) -> tuple[str, str] | None:
        """#298: the `(payer, basis)` stamped on a `harness` hat's usage, or
        `None` for a legacy / `--llm none` run (never stamped). The
        claude-code runner always bills the subscription, so a hat on it under
        a non-claude-code profile is stamped with `subscription_payer`."""
        b = self.billing
        if not b:
            return None
        forced = HARNESS_FORCED_BASIS.get(harness)
        if forced:
            return (b["payer"] if b["harness"] == harness
                    else b["subscription_payer"], forced)
        if harness == b["harness"]:
            if b["basis"] in HARNESS_REFUSED_BASES.get(harness, ()):
                return (b["payer"], DEFAULT_BILLING_BASIS)
            return (b["payer"], b["basis"])
        return (b["profile"] or BILLING_UNLABELLED_PAYER, DEFAULT_BILLING_BASIS)

    def bundle_for(self, phase: str) -> HatBundle:
        """Task 007 (PRD requirement C): the (model, thinking) bundle
        `loop.py`'s dispatch hands to `runner.PiRunner.run`, replacing the
        single global `cfg.thinking` string it used to pass unconditionally
        to every hat's invocation.

        `model_for` supplies the model half unchanged -- this method adds
        nothing to that resolution order. The thinking half:

        - an explicit `thinking_overrides[phase]` wins, and is
          provider-shaped: a one-key mapping (`{"thinking": ...}` or
          `{"effort": ...}`) names its own `HatBundle` field explicitly; a
          plain string is auto-placed by `_model_family(model)` -- `.effort`
          for an OpenAI-family hat, `.thinking` for every other family --
          so an operator overriding a hat's setting need not know its
          provider's own vocabulary to spell it correctly. Either way the
          global `self.thinking` (an Anthropic-shaped level) never lands on
          an overridden hat too -- the two `HatBundle` fields are mutually
          exclusive on every bundle this method returns.
        - absent an override, the old global `self.thinking` becomes this
          hat's default bundle setting (PRD requirement C's back-compat
          rule) -- landed into `.thinking` unconditionally, never guessed
          into `.effort`, because the global field has always spoken pi's
          native vocabulary and never the OpenAI one.
        """
        model = self.model_for(phase)
        harness = self.harness_for(phase)
        override = self.thinking_overrides.get(phase)
        if override is None:
            return HatBundle(model=model, thinking=self.thinking,
                              harness=harness)
        if isinstance(override, dict):
            return HatBundle(model=model, thinking=override.get("thinking"),
                              effort=override.get("effort"), harness=harness)
        if _model_family(model) == "openai":
            return HatBundle(model=model, effort=override, harness=harness)
        return HatBundle(model=model, thinking=override, harness=harness)

    def provisioning_table(self) -> list[dict]:
        """Task 010 (#79, PRD requirement O), widened by task 006 (#68): the
        resolved hat -> model mapping every surface (hub header, `ralphctl
        status`, `status --json`) renders from -- one row per `MODEL_HATS`
        entry, in that tuple's order, each row now resolved through EVERY
        one of that hat's three tiers, never just the one `bundle_for`
        would collapse a configured `model_tiers` rung down to and hide
        from every surface that renders this table.

        Deliberately does NOT call `tiers.default_host_map`/`resolve_
        bundle`: `tests/test_tier_dispatch.py::
        test_resolver_has_exactly_one_call_site_in_src` pins those two to
        `loop.py`'s dispatch call site alone (task 003), the same
        constraint task 005 already respected for `engine/llm.py`'s route
        check by mirroring `_bundle_from_leaf`'s per-leaf resolution
        rather than calling it through `default_host_map`. This method
        follows the identical precedent: it reuses `tiers._bundle_from_
        leaf`/`tiers._apply_tier_table` directly (private helpers the
        guard does not name) to widen `bundle_for(hat)`'s single bundle
        into that hat's own three-tier ladder using ONLY a correctly-
        spelled `MODEL_HATS` key in `self.model_tiers` (`default_host_
        map`'s own "pass 1") -- the typo'd-hat-key borrowing its "pass 2"
        additionally performs is a rare edge case this display-only table
        deliberately leaves unrendered rather than duplicating that
        second pass here too.

        `tiers` is imported here, lazily, rather than at module level:
        `tiers.py` itself imports `JobConfig`/`HatBundle` from this module,
        so a top-level `import ralphd.engine.tiers` here would be circular.

        Deliberately structured, not worded: this is each hat's actual
        per-tier answer, never the raw `models:`/`model_overrides:`/
        `thinking_overrides:`/`model_tiers:` knobs those answers were
        built from (the distinction #79 asked for) --
        `state.format_provisioning_lines` turns each row into the display
        string(s) both `ralphctl status` and the hub render unchanged.

        A hat whose three tiers all resolve to the SAME bundle (a
        one-model configuration, or any hat with no `model_tiers` entry --
        `default_host_map`'s own no-op-widening guarantee, task 002)
        collapses to the single-rung form task 010 shipped: `{"hat": str,
        "model": str | None, "thinking": str | None, "effort": str |
        None}`. A hat with at least one tier's bundle DIFFERENT from the
        others (a configured `model_tiers` rung) instead carries a
        `"rungs"` list, one entry per `tiers.TIERS`, each `{"tier": str,
        "model": str | None, "thinking": str | None, "effort": str |
        None}` -- so a caller can always tell, from the row's own shape,
        whether this hat has one rung or three, without re-resolving
        anything itself. `thinking`/`effort` stay mutually exclusive on
        every bundle, the same discipline `HatBundle` itself enforces.
        """
        from ralphd.engine import tiers as _tiers

        raw_table = self.model_tiers if isinstance(self.model_tiers, dict) else {}
        rows = []
        for hat in MODEL_HATS:
            fallback = self.bundle_for(hat)
            ladder = {tier: fallback for tier in _tiers.TIERS}
            tier_table = raw_table.get(hat)
            if isinstance(tier_table, dict):
                _tiers._apply_tier_table(ladder, tier_table, fallback)
            bundles = {tier: ladder[tier] for tier in _tiers.TIERS}
            distinct = set(bundles.values())
            if len(distinct) == 1:
                bundle = next(iter(distinct))
                rows.append({
                    "hat": hat,
                    "model": bundle.model,
                    "thinking": bundle.thinking,
                    "effort": bundle.effort,
                    "harness": bundle.harness,
                })
            else:
                rows.append({
                    "hat": hat,
                    "harness": self.harness_for(hat),
                    "rungs": [
                        {"tier": tier, "model": bundle.model,
                         "thinking": bundle.thinking, "effort": bundle.effort}
                        for tier, bundle in bundles.items()
                    ],
                })
        return rows

