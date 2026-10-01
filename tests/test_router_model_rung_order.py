"""Task 003 (#278 B.6): `JobConfig.model_for("router")`'s four-rung
resolution order -- `router_model` > `model_overrides["router"]`/
`models.router` > the worker hat's own `trivial` tier
(`model_tiers["worker"]["trivial"]`, when configured) > the bare `model`
field.

Each test below configures ALL FOUR rungs with a distinct, recognisable
value and asserts the one rung it names wins -- so each one fails equally
if its own rung were skipped (the resolver would fall through to a lower
rung's value) or if the rungs were reordered (a different rung's value
would win instead).
"""

from __future__ import annotations

from ralphd.engine.config import JobConfig

ROUTER_MODEL = "rung-1-router-model"
MODELS_ROUTER = "rung-2-models-router"
WORKER_TRIVIAL = "rung-3-worker-trivial"
BARE_MODEL = "rung-4-bare-model"


def _cfg(**overrides) -> JobConfig:
    kw = dict(router_model=None, model_overrides={}, model_tiers={},
              model=BARE_MODEL)
    kw.update(overrides)
    return JobConfig(**kw)


def test_rung_1_router_model_wins_over_every_other_rung():
    cfg = _cfg(router_model=ROUTER_MODEL,
                model_overrides={"router": MODELS_ROUTER},
                model_tiers={"worker": {"trivial": WORKER_TRIVIAL}})
    assert cfg.model_for("router") == ROUTER_MODEL


def test_rung_2_models_router_wins_when_router_model_is_unset():
    cfg = _cfg(router_model=None,
                model_overrides={"router": MODELS_ROUTER},
                model_tiers={"worker": {"trivial": WORKER_TRIVIAL}})
    assert cfg.model_for("router") == MODELS_ROUTER


def test_rung_3_worker_trivial_tier_wins_when_the_first_two_are_unset():
    cfg = _cfg(router_model=None, model_overrides={},
                model_tiers={"worker": {"trivial": WORKER_TRIVIAL}})
    assert cfg.model_for("router") == WORKER_TRIVIAL


def test_rung_4_bare_model_wins_when_nothing_else_is_configured():
    cfg = _cfg(router_model=None, model_overrides={}, model_tiers={})
    assert cfg.model_for("router") == BARE_MODEL
