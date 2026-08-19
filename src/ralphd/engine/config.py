"""Job configuration loaded from /config/job.yaml (or env fallbacks)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .pricing import PricingMap

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
    # model tiers: any pi "provider/model" ref; phase → tier via strategy
    model: str | None = None          # strong tier (pi default model when None)
    fast_model: str | None = None     # fast tier (falls back to model)
    model_strategy: str = "quality-first"
    model_overrides: dict = field(default_factory=dict)  # phase → model ref
    thinking: str | None = None       # pi --thinking level
    api_token: str | None = None
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
    pricing: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    # "reflect" (post-job self-reflection, PRD req 24) mirrors "review"'s tier
    # in every strategy -- it's the same kind of post-hoc analysis role.
    STRATEGY_TIERS = {
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

    @classmethod
    def load(cls, path: Path | None = None) -> JobConfig:
        path = path or CONFIG_DIR / "job.yaml"
        raw = {}
        if path.exists():
            raw = yaml.safe_load(path.read_text()) or {}
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        kwargs = {k: v for k, v in raw.items() if k in known}
        cfg = cls(**kwargs, extra={k: v for k, v in raw.items() if k not in known})
        if tok := os.environ.get("RALPHD_API_TOKEN"):
            cfg.api_token = tok
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
            },
            "flags": {
                "vigilant": self.vigilant,
                "onComplete": self.on_complete,
                "onCompleteCmd": self.on_complete_cmd,
                "reflect": self.reflect,
            },
            "model": {
                "strategy": self.model_strategy,
                "model": self.model,
                "fastModel": self.fast_model,
                "overrides": dict(self.model_overrides),
                "thinking": self.thinking,
            },
            # Task 052 (#10): the host-side rate table as resolved (null when
            # none is configured, and the shipped default IS none) -- so an
            # operator reading a derived cost can see which rates produced it.
            "pricing": (pm.describe()
                        if (pm := PricingMap.from_config(self.pricing)) else None),
        }

    def model_for(self, phase: str) -> str | None:
        """Resolve the pi model ref for a phase; None = pi's own default."""
        if override := self.model_overrides.get(phase):
            return override
        tiers = self.STRATEGY_TIERS.get(self.model_strategy,
                                        self.STRATEGY_TIERS["quality-first"])
        tier = tiers.get(phase, "strong")
        if tier == "fast":
            return self.fast_model or self.model
        return self.model
