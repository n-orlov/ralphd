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


@dataclass
class JobConfig:
    run_id: str = "unnamed-run"
    iterations: int = 25
    max_approaches: int = 3
    vigilant: bool = False
    on_complete: str = "idle"  # idle | exit
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

    STRATEGY_TIERS = {
        "quality-first": {"planning": "strong", "worker": "strong",
                          "review": "strong", "verify": "strong"},
        "cost-optimized": {"planning": "strong", "worker": "fast",
                           "review": "fast", "verify": "fast"},
        "balanced": {"planning": "strong", "worker": "fast",
                     "review": "strong", "verify": "fast"},
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
