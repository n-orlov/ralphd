"""Task 012 (#68 half 2, requirements E/J): the three-tier vocabulary and
the pure hat x tier resolver.

`ralphd.engine.tiers` defines exactly three ordinal tiers, ordering helpers
(`escalate_tier` capped at `hard`, `step_down_tier` floored at `trivial`),
and `resolve_bundle` -- a pure `(hat, effective tier, host map) -> bundle`
function with no I/O, no clock, and no config-file read inside it.

These tests also guard requirement J's "the transition table stays about
*what happens next*; provisioning is *how*" by asserting `transitions.TABLE`
carries no model/tier/thinking keys at all.
"""

from __future__ import annotations

import itertools

import pytest

from ralphd.engine import tiers as T
from ralphd.engine import transitions as tr
from ralphd.engine.config import HatBundle, JobConfig, MODEL_HATS


# ---------------------------------------------------------------------------
# The vocabulary itself
# ---------------------------------------------------------------------------

def test_exactly_three_ordinal_tiers_in_order():
    assert T.TIERS == ("trivial", "normal", "hard")
    assert T.tier_rank("trivial") < T.tier_rank("normal") < T.tier_rank("hard")


def test_default_tier_is_normal():
    assert T.DEFAULT_TIER == "normal"


def test_unrecognised_tier_raises_not_silently_normalised():
    with pytest.raises(ValueError):
        T.tier_rank("medium")


@pytest.mark.parametrize("tier,expected", [
    ("trivial", "normal"),
    ("normal", "hard"),
    ("hard", "hard"),          # capped -- no fourth rung
])
def test_escalate_tier_is_plus_one_capped_at_hard(tier, expected):
    assert T.escalate_tier(tier) == expected


@pytest.mark.parametrize("tier,expected", [
    ("hard", "normal"),
    ("normal", "trivial"),
    ("trivial", "trivial"),    # floored -- no rung below trivial
])
def test_step_down_tier_is_minus_one_floored_at_trivial(tier, expected):
    assert T.step_down_tier(tier) == expected


def test_is_tier_helper():
    for name in T.TIERS:
        assert T.is_tier(name)
    assert not T.is_tier("medium")
    assert not T.is_tier(None)


# ---------------------------------------------------------------------------
# The pure resolver: fully-specified map, every hat x every tier
# ---------------------------------------------------------------------------

def _fully_specified_map():
    """A hand-built host map giving every hat its own distinct bundle per
    tier -- proves each hat's ladder is genuinely independent (decision 3):
    the verifier's `hard` rung names a different family than the worker's."""
    host_map = {}
    for hat in MODEL_HATS:
        host_map[hat] = {
            tier: HatBundle(model=f"{hat}-provider/{tier}-model",
                             thinking=f"{tier}-thinking")
            for tier in T.TIERS
        }
    return host_map


@pytest.mark.parametrize("hat,tier", tuple(itertools.product(MODEL_HATS, T.TIERS)))
def test_resolver_is_pure_and_exhaustive_over_fully_specified_map(hat, tier):
    host_map = _fully_specified_map()
    bundle = T.resolve_bundle(hat, tier, host_map)
    assert bundle == HatBundle(model=f"{hat}-provider/{tier}-model",
                                thinking=f"{tier}-thinking")


def test_all_hat_tier_pairs_matches_the_product():
    assert set(T.all_hat_tier_pairs()) == set(itertools.product(MODEL_HATS, T.TIERS))


def test_a_different_hat_never_shares_another_hats_rung():
    host_map = _fully_specified_map()
    worker_hard = T.resolve_bundle("worker", "hard", host_map)
    verify_hard = T.resolve_bundle("verify", "hard", host_map)
    assert worker_hard.model != verify_hard.model


# ---------------------------------------------------------------------------
# The defaulted map, built from a JobConfig -- every hat x every tier
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hat,tier", tuple(itertools.product(MODEL_HATS, T.TIERS)))
def test_default_host_map_covers_every_hat_x_every_tier(hat, tier):
    cfg = JobConfig(model_overrides={
        "worker": "acme/worker-model",
        "verify": "acme/verify-model",
    })
    host_map = T.default_host_map(cfg)
    resolved = T.resolve_bundle(hat, tier, host_map)
    assert resolved == cfg.bundle_for(hat)


@pytest.mark.parametrize("hat,tier", tuple(itertools.product(MODEL_HATS, T.TIERS)))
def test_one_model_configuration_collapses_every_rung_onto_it(hat, tier):
    """Requirement E's defaultability rule: an operator who configures one
    model gets ALL rungs of ALL hats collapsed onto it -- tiering becomes a
    no-op, not an error."""
    cfg = JobConfig(model="single-provider/only-model")
    host_map = T.default_host_map(cfg)
    resolved = T.resolve_bundle(hat, tier, host_map)
    assert resolved.model == "single-provider/only-model"


def test_default_host_map_needs_no_io_no_clock(monkeypatch):
    """`default_host_map`/`resolve_bundle` never touch a config file or the
    clock -- poison `Path.read_text`/`time.time` for the duration and prove
    both functions still work from an in-memory JobConfig alone."""
    import pathlib
    import time as time_mod

    def _boom(*a, **k):
        raise AssertionError("tiers module performed I/O or read the clock")

    monkeypatch.setattr(pathlib.Path, "read_text", _boom)
    monkeypatch.setattr(time_mod, "time", _boom)

    cfg = JobConfig(model="acme/model")
    host_map = T.default_host_map(cfg)
    assert T.resolve_bundle("worker", "hard", host_map).model == "acme/model"


def test_resolver_falls_back_to_unrecognised_hat_ladder():
    """An incomplete map -- missing a hat entirely -- degrades via the same
    anti-downgrade rung task 005 already established (`planning`'s own
    ladder), never to the cheap tier and never by erroring."""
    host_map = {"planning": {"hard": HatBundle(model="acme/planner-strong")}}
    resolved = T.resolve_bundle("worker", "hard", host_map)
    assert resolved.model == "acme/planner-strong"


def test_resolver_returns_empty_bundle_when_nothing_at_all_resolves():
    resolved = T.resolve_bundle("worker", "hard", {})
    assert resolved == HatBundle()


# ---------------------------------------------------------------------------
# transitions.TABLE gains no provisioning policy (requirement J)
# ---------------------------------------------------------------------------

def test_transition_table_carries_no_provisioning_policy():
    forbidden = ("model", "tier", "thinking", "effort")
    for state, edges in tr.TABLE.items():
        for edge in edges:
            assert isinstance(edge, tr.Transition)
            # `Transition` is `__slots__`-based (target, guard only) -- this
            # both proves it structurally and pins the behaviour so a future
            # edit adding a stray attribute is caught here, not discovered
            # downstream.
            assert edge.__slots__ == ("guard", "target")
            for key in forbidden:
                assert not hasattr(edge, key), (
                    f"{state} edge {edge!r} carries a provisioning key {key!r}")
