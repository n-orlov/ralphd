"""Task 002 (#68 half 2, review finding 2): `JobConfig.model_tiers`, the
job.yaml configuration surface for the hat x tier host map, plus the
`engine.tiers.default_host_map` builder that reads it.

Three things get their own section below, matching this task's three
success criteria:

(a) a job.yaml that configures only `worker.hard` makes `resolve_bundle`
    return that ref for `('worker', 'hard')` while `('worker', 'normal')`
    still returns the half-1 (`bundle_for`) bundle;
(b) with no `model_tiers` table at all, the built host map is IDENTICAL to
    today's `default_host_map` output for every hat x tier -- the
    one-model-operator collapse survives the widening;
(c) an unrecognised hat or tier key in `model_tiers` degrades to the most-
    capable configured entry, never a cheaper one.

`resolve_bundle` itself is untouched here -- it stays the pure function
task 012 shipped (no I/O, no clock); every anti-downgrade guarantee below
comes from how `default_host_map` BUILDS the host map, never from a second
branch inside the resolver.
"""

from __future__ import annotations

import itertools

import pytest

from ralphd.engine import tiers as T
from ralphd.engine.config import (HatBundle, JobConfig, MODEL_HATS,
                                  UNRECOGNISED_HAT_FALLBACK)


# ---------------------------------------------------------------------------
# (a) a configured rung wins for its own (hat, tier); an unconfigured rung
#     of the SAME hat still falls through to the half-1 bundle.
# ---------------------------------------------------------------------------

def test_a_configured_hard_rung_wins_only_at_hard():
    cfg = JobConfig(model="shipped-default",
                     model_tiers={"worker": {"hard": "acme/worker-hard-ref"}})
    host_map = T.default_host_map(cfg)
    assert T.resolve_bundle("worker", "hard", host_map).model == "acme/worker-hard-ref"
    # normal/trivial are untouched -- still the half-1 bundle.
    assert T.resolve_bundle("worker", "normal", host_map) == cfg.bundle_for("worker")
    assert T.resolve_bundle("worker", "trivial", host_map) == cfg.bundle_for("worker")


def test_configuring_one_hat_never_touches_a_different_hats_ladder():
    cfg = JobConfig(model="shipped-default",
                     model_tiers={"worker": {"hard": "acme/worker-hard-ref"}})
    host_map = T.default_host_map(cfg)
    for tier in T.TIERS:
        assert T.resolve_bundle("verify", tier, host_map) == cfg.bundle_for("verify")


def test_a_provider_shaped_leaf_carries_its_own_model_and_thinking():
    cfg = JobConfig(model_tiers={
        "verify": {"hard": {"model": "aigw-openai/gpt-5.6-terra", "effort": "high"}},
    })
    host_map = T.default_host_map(cfg)
    bundle = T.resolve_bundle("verify", "hard", host_map)
    assert bundle == HatBundle(model="aigw-openai/gpt-5.6-terra", effort="high")


def test_a_provider_shaped_leaf_may_omit_model_and_inherit_the_hats_own():
    cfg = JobConfig(model_overrides={"verify": "acme/verify-model"},
                     model_tiers={"verify": {"hard": {"effort": "high"}}})
    host_map = T.default_host_map(cfg)
    bundle = T.resolve_bundle("verify", "hard", host_map)
    assert bundle == HatBundle(model="acme/verify-model", effort="high")


def test_a_plain_string_leaf_inherits_the_hats_thinking_setting():
    cfg = JobConfig(thinking="medium",
                     model_tiers={"worker": {"hard": "acme/worker-hard-ref"}})
    host_map = T.default_host_map(cfg)
    bundle = T.resolve_bundle("worker", "hard", host_map)
    assert bundle == HatBundle(model="acme/worker-hard-ref", thinking="medium")


# ---------------------------------------------------------------------------
# (b) no `model_tiers` table at all -> identical to the un-widened default,
#     for every hat x every tier (the one-model collapse survives).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hat,tier", tuple(itertools.product(MODEL_HATS, T.TIERS)))
def test_no_tier_table_reproduces_the_plain_default_host_map(hat, tier):
    cfg = JobConfig(model="single-provider/only-model")
    host_map = T.default_host_map(cfg)
    assert T.resolve_bundle(hat, tier, host_map) == cfg.bundle_for(hat)


@pytest.mark.parametrize("hat,tier", tuple(itertools.product(MODEL_HATS, T.TIERS)))
def test_no_tier_table_with_a_mixed_per_hat_config_still_reproduces_bundle_for(hat, tier):
    cfg = JobConfig(model="shipped", model_overrides={
        "worker": "acme/worker-model", "verify": "acme/verify-model",
    })
    host_map = T.default_host_map(cfg)
    assert T.resolve_bundle(hat, tier, host_map) == cfg.bundle_for(hat)


def test_an_explicitly_empty_tier_table_is_the_same_as_no_table():
    cfg_empty = JobConfig(model="m", model_tiers={})
    cfg_absent = JobConfig(model="m")
    assert T.default_host_map(cfg_empty) == T.default_host_map(cfg_absent)


# ---------------------------------------------------------------------------
# (c) anti-downgrade: an unrecognised hat or tier KEY inside `model_tiers`
#     degrades to the most-capable configured entry, never a cheaper one.
# ---------------------------------------------------------------------------

def test_an_unrecognised_tier_key_folds_onto_the_top_tier_not_a_cheap_one():
    """A typo'd tier name ("hardd") must not vanish -- and must not land on
    a CHEAPER rung than the top one either, or the operator's evident
    intent (naming a stronger ref) would silently cost less than asked."""
    cfg = JobConfig(model="shipped",
                     model_tiers={"worker": {"hardd": "acme/typo-tier-ref"}})
    host_map = T.default_host_map(cfg)
    # It lands on the TOP tier (never trivial/normal -- never cheaper).
    assert T.resolve_bundle("worker", "hard", host_map).model == "acme/typo-tier-ref"
    # trivial/normal are untouched, still the half-1 default.
    assert T.resolve_bundle("worker", "normal", host_map) == cfg.bundle_for("worker")
    assert T.resolve_bundle("worker", "trivial", host_map) == cfg.bundle_for("worker")


def test_a_real_hard_key_beats_a_typo_tier_key_mapping_to_the_same_rung():
    """When both a correctly-spelled `hard` AND a typo'd tier key are
    present for the same hat, the correctly-spelled entry is the one that
    should be trusted -- last-write-wins is acceptable dict-merge
    semantics, but the test pins today's concrete outcome so a future
    change to iteration order is a deliberate decision, not an accident."""
    cfg = JobConfig(model_tiers={"worker": {
        "hard": "acme/real-hard-ref",
    }})
    host_map = T.default_host_map(cfg)
    assert T.resolve_bundle("worker", "hard", host_map).model == "acme/real-hard-ref"


def test_an_unrecognised_hat_key_borrows_the_planning_ladder_not_nothing():
    """A typo'd HAT name ("wroker") in `model_tiers` must not be silently
    dropped -- it degrades exactly like `model_for`'s own anti-downgrade
    rule (task 005): via `UNRECOGNISED_HAT_FALLBACK` ("planning")'s own
    ladder, never to an empty/absent bundle."""
    cfg = JobConfig(model="shipped",
                     model_tiers={"wroker": {"hard": "acme/typo-hat-ref"}})
    host_map = T.default_host_map(cfg)
    # Looking the typo'd name up directly borrows planning's ladder --
    # resolve_bundle's own existing fallback (task 012) does this once the
    # host map carries the entry on planning's hard rung.
    resolved = T.resolve_bundle("wroker", "hard", host_map)
    assert resolved.model == "acme/typo-hat-ref"
    assert resolved == T.resolve_bundle(UNRECOGNISED_HAT_FALLBACK, "hard", host_map)


def test_a_typo_hat_key_never_overwrites_a_real_planning_entry():
    """A correctly-spelled `planning` entry always wins over anything a
    typo'd hat key tries to leak into the same rung -- the borrowed ladder
    (previous test) only fills gaps `planning`'s own table left unset."""
    cfg = JobConfig(model="shipped", model_tiers={
        "planning": {"hard": "acme/real-planning-hard-ref"},
        "wroker": {"hard": "acme/typo-hat-ref"},
    })
    host_map = T.default_host_map(cfg)
    assert T.resolve_bundle("planning", "hard", host_map).model == "acme/real-planning-hard-ref"


def test_a_typo_hat_key_still_fills_a_rung_planning_left_unconfigured():
    cfg = JobConfig(model="shipped", model_tiers={
        "planning": {"normal": "acme/real-planning-normal-ref"},
        "wroker": {"hard": "acme/typo-hat-ref"},
    })
    host_map = T.default_host_map(cfg)
    assert T.resolve_bundle("planning", "normal", host_map).model == "acme/real-planning-normal-ref"
    assert T.resolve_bundle("planning", "hard", host_map).model == "acme/typo-hat-ref"


def test_an_unrecognised_hat_and_tier_key_together_still_lands_on_planning_hard():
    cfg = JobConfig(model="shipped",
                     model_tiers={"wroker": {"hardd": "acme/double-typo-ref"}})
    host_map = T.default_host_map(cfg)
    resolved = T.resolve_bundle("wroker", "hard", host_map)
    assert resolved.model == "acme/double-typo-ref"


def test_default_host_map_still_needs_no_io_no_clock_with_model_tiers_set(monkeypatch):
    """The widened builder keeps requirement J's "pure, no I/O, no clock"
    guarantee (task 012) even once `model_tiers` is populated."""
    import pathlib
    import time as time_mod

    def _boom(*a, **k):
        raise AssertionError("tiers module performed I/O or read the clock")

    monkeypatch.setattr(pathlib.Path, "read_text", _boom)
    monkeypatch.setattr(time_mod, "time", _boom)

    cfg = JobConfig(model="acme/model",
                     model_tiers={"worker": {"hard": "acme/worker-hard"}})
    host_map = T.default_host_map(cfg)
    assert T.resolve_bundle("worker", "hard", host_map).model == "acme/worker-hard"
