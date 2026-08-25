"""Task 018 (requirement E, #60 pillar 2): `engine.router` in isolation --
no engine, no subprocess, no run directory. Every model call is a plain
async stub so these tests run in milliseconds and exercise the decision
logic directly: deterministic-first (zero calls when unambiguous),
validate-then-retry-then-fallback, and the timeout budget.

No engine cutover is asserted here (that is `_step_generic_hat`'s business,
covered by tests/test_router_engine.py) -- this module only proves the
mechanism itself, exactly as tests/test_response_contracts.py did for
task 009 and tests/test_contract_enforcement.py did for task 010.
"""

from __future__ import annotations

import asyncio

import pytest

from ralphd.engine import router
from ralphd.engine import transitions as tr

AMBIGUOUS = "task018_ambiguous_test_state"


@pytest.fixture
def two_edges(monkeypatch):
    """A synthetic state with two legal edges and a declared fallback --
    the smallest fixture that makes a state genuinely router-ambiguous,
    mirroring task 015's no-op-hat registration technique."""
    assert AMBIGUOUS not in tr.TABLE, "fixture name collided with a real state"
    tr.TABLE[AMBIGUOUS] = (tr.T("worker", "picked_worker"),
                           tr.T("review", "picked_review"))
    tr.ROUTER_FALLBACK[AMBIGUOUS] = "picked_worker"
    try:
        yield
    finally:
        tr.TABLE.pop(AMBIGUOUS, None)
        tr.ROUTER_FALLBACK.pop(AMBIGUOUS, None)


def _never_ask(prompt: str):
    raise AssertionError("router.decide() called `ask` for an unambiguous state")


# ---------------------------------------------------------------------------
# Deterministic-first: 0 or 1 legal edge never reaches the model.
# ---------------------------------------------------------------------------

SINGLE_EDGE_STATE = "task018_single_edge_test_state"


@pytest.fixture
def one_edge(monkeypatch):
    assert SINGLE_EDGE_STATE not in tr.TABLE, "fixture name collided"
    tr.TABLE[SINGLE_EDGE_STATE] = (tr.T("planning", "only_edge"),)
    try:
        yield
    finally:
        tr.TABLE.pop(SINGLE_EDGE_STATE, None)


def test_a_state_with_exactly_one_legal_edge_never_calls_ask(one_edge):
    """A state with only one legal edge is unambiguous by construction --
    `decide()` never reaches `ask`, whatever the caller passes it. This is
    what makes task 015's single-edge generic-hat path (still the common
    case: every real, currently-shipped state either has one of the four
    hand-written `_step_*` guards computing its edge directly from disk
    state, or -- like this fixture -- exactly one legal edge) provably free
    of router calls; the engine-level test in test_router_engine.py proves
    the SAME property end to end through a real dispatch loop, over the
    real, shipped table (planning/worker/review/replan), where the guard is
    computed from state rather than the edge count -- both properties are
    real and are asserted where each is actually true."""
    outcome = asyncio.run(router.decide(
        SINGLE_EDGE_STATE, False, ask=_never_ask, fallback_guard=None,
        retry_max=2, timeout_s=5.0))
    assert outcome.used_router is False
    assert outcome.attempts == 0
    assert outcome.target == "planning"
    assert outcome.guard == "only_edge"


def test_decide_raises_for_a_terminal_state():
    with pytest.raises(ValueError, match="terminal state"):
        asyncio.run(router.decide("succeeded", False, ask=_never_ask,
                                  fallback_guard=None, retry_max=2,
                                  timeout_s=5.0))


# ---------------------------------------------------------------------------
# The genuine-choice path: validate, re-prompt, fall back.
# ---------------------------------------------------------------------------

def test_decide_accepts_a_valid_first_answer(two_edges):
    async def ask(prompt):
        return '{"next_step": "review"}', {"totalTokens": 12}

    outcome = asyncio.run(router.decide(
        AMBIGUOUS, False, ask=ask, fallback_guard="picked_worker",
        retry_max=2, timeout_s=5.0))
    assert outcome.target == "review"
    assert outcome.guard == "picked_review"
    assert outcome.used_router is True
    assert outcome.used_fallback is False
    assert outcome.attempts == 1
    assert outcome.usage == {"totalTokens": 12}


@pytest.mark.parametrize("bad_raw", [
    None, "", "not json", "42", '{"next_step": "not_a_legal_target"}',
    '{"wrong_field": "worker"}', '{"next_step": "worker", "extra": 1}',
])
def test_an_invalid_answer_re_prompts_then_falls_back(two_edges, bad_raw):
    prompts_seen = []

    async def ask(prompt):
        prompts_seen.append(prompt)
        return bad_raw, {}

    outcome = asyncio.run(router.decide(
        AMBIGUOUS, False, ask=ask, fallback_guard="picked_worker",
        retry_max=2, timeout_s=5.0))
    assert outcome.attempts == 3  # 1 + retry_max
    assert outcome.used_router is True
    assert outcome.used_fallback is True
    # the declared fallback -- never an illegal transition, however many
    # attempts failed.
    assert outcome.target == "worker"
    assert outcome.guard == "picked_worker"
    assert tr.is_legal(AMBIGUOUS, outcome.target, False)
    # every re-prompt after the first carries the specific validation error
    assert len(prompts_seen) == 3
    assert "rejected" not in prompts_seen[0]
    assert "rejected" in prompts_seen[1]
    assert "rejected" in prompts_seen[2]


def test_recovery_within_the_retry_budget_needs_no_fallback(two_edges):
    calls = {"n": 0}

    async def ask(prompt):
        calls["n"] += 1
        if calls["n"] < 2:
            return "not json", {}
        return '{"next_step": "worker"}', {}

    outcome = asyncio.run(router.decide(
        AMBIGUOUS, False, ask=ask, fallback_guard="picked_worker",
        retry_max=2, timeout_s=5.0))
    assert outcome.used_fallback is False
    assert outcome.attempts == 2
    assert outcome.target == "worker"


def test_an_illegal_transition_is_never_accepted_even_after_n_failures(two_edges):
    """A router answer naming a target that IS a real state but NOT one of
    THIS state's own legal edges must never be honoured, however many
    attempts it survives across -- the fallback fires instead."""
    async def ask(prompt):
        return '{"next_step": "planning"}', {}  # a real state, wrong edge

    outcome = asyncio.run(router.decide(
        AMBIGUOUS, False, ask=ask, fallback_guard="picked_worker",
        retry_max=1, timeout_s=5.0))
    assert outcome.used_fallback is True
    assert tr.is_legal(AMBIGUOUS, outcome.target, False)


# ---------------------------------------------------------------------------
# Latency budget: a hanging call cannot stall the loop.
# ---------------------------------------------------------------------------

def test_a_hanging_router_call_times_out_and_falls_back(two_edges):
    async def ask(prompt):
        await asyncio.sleep(3600)  # would hang the loop forever if awaited raw
        return '{"next_step": "worker"}', {}

    outcome = asyncio.run(router.decide(
        AMBIGUOUS, False, ask=ask, fallback_guard="picked_worker",
        retry_max=0, timeout_s=0.05))
    assert outcome.used_fallback is True
    assert outcome.target == "worker"


# ---------------------------------------------------------------------------
# An ambiguous state with no declared fallback is a programming error, not
# a runtime hazard an operator can hit silently.
# ---------------------------------------------------------------------------

def test_an_ambiguous_state_with_no_declared_fallback_raises(two_edges):
    tr.ROUTER_FALLBACK.pop(AMBIGUOUS, None)  # undeclared, on purpose

    async def ask(prompt):
        return '{"next_step": "worker"}', {}

    with pytest.raises(AssertionError, match="no transitions.ROUTER_FALLBACK entry|ROUTER_FALLBACK entry"):
        asyncio.run(router.decide(AMBIGUOUS, False, ask=ask,
                                  fallback_guard=tr.fallback_guard(AMBIGUOUS),
                                  retry_max=1, timeout_s=5.0))


def test_a_fallback_guard_naming_a_foreign_edge_raises(two_edges):
    async def ask(prompt):
        return "still not json", {}

    with pytest.raises(AssertionError, match="not one of its own legal edges"):
        asyncio.run(router.decide(AMBIGUOUS, False, ask=ask,
                                  fallback_guard="no_such_guard",
                                  retry_max=0, timeout_s=5.0))


def test_validate_answer_directly():
    tr.TABLE[AMBIGUOUS] = (tr.T("worker", "picked_worker"),
                           tr.T("review", "picked_review"))
    try:
        payload, errors = router.validate_answer(
            '{"next_step": "worker"}', AMBIGUOUS, False)
        assert errors == []
        assert payload == {"next_step": "worker"}
        _, errors = router.validate_answer(None, AMBIGUOUS, False)
        assert "absent output" in errors[0]
        _, errors = router.validate_answer("{bad", AMBIGUOUS, False)
        assert "malformed JSON" in errors[0]
    finally:
        tr.TABLE.pop(AMBIGUOUS, None)


# ---------------------------------------------------------------------------
# Fact 16/#65: still exactly one schema-validator code path. router.py must
# not have grown a second one -- it must route through
# `contracts.validate_against`.
# ---------------------------------------------------------------------------

def test_router_module_has_no_second_schema_validator():
    import ast
    from pathlib import Path
    path = Path(router.__file__)
    tree = ast.parse(path.read_text(), filename=str(path))
    markers = frozenset({"required", "properties", "type"})
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            consts = {
                n.value for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            }
            assert not markers.issubset(consts), (
                f"{node.name}() in router.py looks like a second schema "
                "validator -- use ralphd.engine.contracts.validate_against")


# ---------------------------------------------------------------------------
# JobConfig knobs: default, job.yaml, RALPHD_* override, GET /config view,
# and `model_for("router")`'s resolution order (requirement E's "one new
# configurable model key ... through the existing model_overrides
# mechanism").
# ---------------------------------------------------------------------------

def test_router_model_default_is_none_and_falls_back_to_fast_then_strong():
    from ralphd.engine.config import JobConfig
    cfg = JobConfig()
    assert cfg.router_model is None
    assert cfg.model_for("router") is None  # nothing configured at all
    cfg2 = JobConfig(model="strong-ref")
    assert cfg2.model_for("router") == "strong-ref"
    cfg3 = JobConfig(model="strong-ref", fast_model="fast-ref")
    assert cfg3.model_for("router") == "fast-ref"
    cfg4 = JobConfig(model="strong-ref", fast_model="fast-ref",
                      router_model="router-ref")
    assert cfg4.model_for("router") == "router-ref"


def test_router_model_override_via_model_overrides_wins_over_router_model():
    from ralphd.engine.config import JobConfig
    cfg = JobConfig(router_model="router-ref",
                     model_overrides={"router": "override-ref"})
    assert cfg.model_for("router") == "override-ref"


def test_router_model_from_job_yaml(tmp_path):
    from ralphd.engine.config import JobConfig
    p = tmp_path / "job.yaml"
    p.write_text("router_model: yaml-router-ref\n")
    cfg = JobConfig.load(p)
    assert cfg.router_model == "yaml-router-ref"


def test_router_model_env_override(tmp_path, monkeypatch):
    from ralphd.engine.config import JobConfig
    monkeypatch.setenv("RALPHD_ROUTER_MODEL", "env-router-ref")
    cfg = JobConfig.load(tmp_path / "does-not-exist.yaml")
    assert cfg.router_model == "env-router-ref"


def test_router_timeout_default_and_env_override(tmp_path, monkeypatch):
    from ralphd.engine.config import JobConfig
    assert JobConfig().router_timeout_s == 60.0
    monkeypatch.setenv("RALPHD_ROUTER_TIMEOUT_S", "12.5")
    cfg = JobConfig.load(tmp_path / "does-not-exist.yaml")
    assert cfg.router_timeout_s == 12.5


def test_router_knobs_in_effective_view():
    from ralphd.engine.config import JobConfig
    eff = JobConfig(router_model="r", router_timeout_s=9.0).effective()
    assert eff["model"]["routerModel"] == "r"
    assert eff["budgets"]["routerTimeoutS"] == 9.0
    # unset default is visible too, not just the non-default case.
    assert JobConfig().effective()["model"]["routerModel"] is None
