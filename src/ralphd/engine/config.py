"""Job configuration loaded from /config/job.yaml (or env fallbacks)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

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
    on_complete: str = "idle"  # idle | exit
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
            },
            "flags": {
                "vigilant": self.vigilant,
                "onComplete": self.on_complete,
                "reflect": self.reflect,
            },
            "model": {
                "strategy": self.model_strategy,
                "model": self.model,
                "fastModel": self.fast_model,
                "overrides": dict(self.model_overrides),
                "thinking": self.thinking,
            },
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
