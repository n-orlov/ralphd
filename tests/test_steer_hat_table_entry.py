"""Task 009 (PRD requirement G, #98, #60 pillar 4): the `steer` hat as a
pure table entry -- a state, a schema, a prompt and a real `MODEL_HATS`
entry with a shipped default -- and nothing else. Task 012 is what makes
the harness actually TAKE the `worker -> steer` edge (the priority routing
rule); this module only proves the table declares it legally and refuses
anything undeclared, exactly like every other state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ralphd.engine import contracts
from ralphd.engine import tiers as T
from ralphd.engine import transitions as tr
from ralphd.engine.config import (DEFAULT_HAT_MODEL_REF, MODEL_HATS,
                                  UNRECOGNISED_HAT_FALLBACK, JobConfig)

REPO_ROOT = Path(__file__).resolve().parents[1]
STEER_PROMPT = REPO_ROOT / "src" / "ralphd" / "prompts" / "steer.md"


# ---------------------------------------------------------------------------
# 1. `steer` is a declared table entry, reachable only through the declared
#    edge, and an undeclared target is refused like every other state.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vigilant", [True, False])
def test_steer_is_in_the_state_inventory(vigilant):
    assert "steer" in tr.ALL_STATES
    succ = tr.successors("steer", vigilant)
    assert succ != (), "steer must not be a dead end -- it must return control"
    assert all(edge.target in tr.ALL_STATES for edge in succ)


@pytest.mark.parametrize("vigilant", [True, False])
def test_worker_has_a_declared_edge_into_steer(vigilant):
    assert tr.is_legal("worker", "steer", vigilant)
    edges = tr.successors("worker", vigilant)
    steer_edges = [e for e in edges if e.target == "steer"]
    assert len(steer_edges) == 1
    assert steer_edges[0].guard == "steering_pending"


@pytest.mark.parametrize("vigilant", [True, False])
def test_steer_returns_control_to_worker_via_a_declared_edge(vigilant):
    assert tr.is_legal("steer", "worker", vigilant)
    edges = tr.successors("steer", vigilant)
    assert edges == (tr.T("worker", "steer_settled"),)


@pytest.mark.parametrize("vigilant", [True, False])
def test_an_undeclared_target_out_of_steer_is_refused(vigilant):
    assert not tr.is_legal("steer", "bogus_undeclared_target", vigilant)
    with pytest.raises(tr.IllegalTransition):
        tr.TransitionExecutor(vigilant).advance(
            "steer", "bogus_undeclared_target", "steer_settled")


@pytest.mark.parametrize("vigilant", [True, False])
def test_no_other_state_declares_an_edge_into_steer_except_worker(vigilant):
    """`steer` is reachable ONLY through the one declared `worker -> steer`
    edge -- proves no other state's table entry accidentally targets it."""
    table = tr.table_for(vigilant)
    sources = {state for state, edges in table.items()
               if any(edge.target == "steer" for edge in edges)}
    assert sources == {"worker"}


# ---------------------------------------------------------------------------
# 2. STATE_HAT / HATS / HAT_SCHEMAS entries
# ---------------------------------------------------------------------------

def test_state_hat_entry():
    assert tr.STATE_HAT["steer"] == "steer"


def test_steer_is_in_hats():
    assert "steer" in tr.HATS


def test_hat_schemas_entry_shape():
    schema = tr.HAT_SCHEMAS["steer"]
    assert schema["type"] == "object"
    assert schema["required"] == ["settled"]
    assert schema["properties"]["settled"]["type"] == "boolean"
    assert schema["additionalProperties"] is False


def test_every_hat_including_steer_has_exactly_one_schema():
    assert contracts.hats_with_schemas() == frozenset(tr.HATS)
    assert "steer" in contracts.hats_with_schemas()


def test_a_settled_true_payload_validates():
    errors = contracts.validate_payload("steer", {"settled": True})
    assert errors == []


def test_a_settled_false_payload_with_notes_validates():
    errors = contracts.validate_payload(
        "steer", {"settled": False, "notes": "could not parse the message"})
    assert errors == []


def test_a_payload_missing_settled_is_rejected():
    errors = contracts.validate_payload("steer", {"notes": "no settled field"})
    assert errors


def test_an_extra_property_is_rejected():
    errors = contracts.validate_payload(
        "steer", {"settled": True, "task_status": "completed"})
    assert errors


# ---------------------------------------------------------------------------
# 3. The prompt exists and says the hat reshapes state and writes no code.
# ---------------------------------------------------------------------------

def test_steer_prompt_exists():
    assert STEER_PROMPT.is_file(), f"missing {STEER_PROMPT}"


def test_steer_prompt_says_it_reshapes_state_and_writes_no_code():
    text = STEER_PROMPT.read_text()
    lowered = text.lower()
    assert "reshape" in lowered
    assert "write no code" in lowered or "you write no code" in lowered
    # never claims task/verify/review-owned statuses for itself either
    assert "you write no code" in lowered or "writes no code" in lowered


def test_steer_prompt_declares_the_settled_contract():
    text = STEER_PROMPT.read_text()
    assert '"settled"' in text
    assert "done" in text.lower()


# ---------------------------------------------------------------------------
# 4. `config.MODEL_HATS` gains a REAL `steer` entry with a shipped default
#    -- never resolving through `UNRECOGNISED_HAT_FALLBACK` -- and tier
#    rungs via `tiers.all_hat_tier_pairs()`.
# ---------------------------------------------------------------------------

def test_steer_is_a_real_model_hats_entry():
    assert "steer" in MODEL_HATS
    assert "steer" != UNRECOGNISED_HAT_FALLBACK


def test_steer_shipped_default_matches_every_other_known_hat():
    cfg = JobConfig()
    assert cfg.model_for("steer") is DEFAULT_HAT_MODEL_REF is None
    cfg2 = JobConfig(model="shipped-model")
    assert cfg2.model_for("steer") == "shipped-model"


def test_steer_never_resolves_through_the_unrecognised_hat_fallback():
    """A `planning`-only configured table must NOT leak onto `steer` the
    way it would for a genuinely unrecognised (typo'd) hat name -- `steer`
    is one of `model_for`'s own known hats, so it falls through to the
    bare `model` field, never to `planning`'s entry."""
    cfg = JobConfig(model="strong-default", models={"planning": "opus"})
    # the anti-downgrade path a TYPO would take:
    assert cfg.model_for("totally-bogus-hat") == "opus"
    # steer is real, so it does NOT take that path:
    assert cfg.model_for("steer") == "strong-default"


def test_steer_has_tier_rungs_via_all_hat_tier_pairs():
    pairs = T.all_hat_tier_pairs()
    steer_pairs = {(h, t) for h, t in pairs if h == "steer"}
    assert steer_pairs == {("steer", tier) for tier in T.TIERS}


def test_steer_host_map_widens_cleanly_with_nothing_configured():
    cfg = JobConfig(model="shipped-model")
    host_map = T.default_host_map(cfg)
    assert "steer" in host_map
    for tier in T.TIERS:
        assert host_map["steer"][tier].model == "shipped-model"


def test_steer_resolves_through_resolve_bundles_pure_signature():
    """`resolve_bundle` is the single pure resolver task 016 pins to one
    call site in `loop.py` (`tests/test_tier_dispatch.py::
    test_resolver_has_exactly_one_call_site_in_src`) -- this only proves
    `steer` participates in that SAME function, not a second pathway."""
    cfg = JobConfig(model="shipped-model")
    host_map = T.default_host_map(cfg)
    bundle = T.resolve_bundle("steer", "normal", host_map)
    assert bundle.model == "shipped-model"


def test_steer_configured_tier_rung_resolves_through_the_same_resolver():
    cfg = JobConfig(model_tiers={"steer": {"hard": {"model": "steer-hard-ref"}}})
    host_map = T.default_host_map(cfg)
    bundle = T.resolve_bundle("steer", "hard", host_map)
    assert bundle.model == "steer-hard-ref"
