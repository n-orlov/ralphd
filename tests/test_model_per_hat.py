"""Task 005 (#60 pillar 4, requirement A): the per-hat model table.

`model_overrides` (job.yaml key `model_overrides:` or its new alias
`models:`) becomes THE mechanism for a model per hat, covering the seven hats
`model_for` resolves: the six `STATE_HAT` roles (planning/worker/verify/
review/reflect/steer) plus the router. Four things must hold, and each gets
its own section below:

* an entirely absent table reproduces today's behaviour for a run that
  configures only `model` (and, separately, a run that configures nothing);
* a configured per-hat entry (via either spelling) wins for its own hat;
* a KNOWN hat with no entry in the table falls back to the single shipped
  default ref (`model` itself, or `DEFAULT_HAT_MODEL_REF`/`None` when that
  is unset too);
* an UNRECOGNISED hat name (a typo -- not one of the seven) resolves to the
  most capable CONFIGURED entry, never the cheap one, per
  `UNRECOGNISED_HAT_FALLBACK`.

Task 009 (PRD requirement G, #98) added `steer` as a real seventh entry --
the assertion below is strengthened (never weakened) accordingly: the old
claim was `set(MODEL_HATS) == {planning, worker, verify, review, reflect,
router}` (six members); it is now that set PLUS `steer`, because `steer` is
no longer an unrecognised name that would degrade through
`UNRECOGNISED_HAT_FALLBACK` -- it is one of `model_for`'s own known hats.

Task 018 (PRD requirement K, #101) strengthens the same assertion again:
`plan_review` (the opt-in peer plan-review hat) joins `MODEL_HATS` too, off
by default (`JobConfig.peer_plan_review`) but a real, known hat name
either way -- never an unrecognised one.
"""

from __future__ import annotations

import pytest

from ralphd.engine.config import (DEFAULT_HAT_MODEL_REF, MODEL_HATS,
                                  UNRECOGNISED_HAT_FALLBACK, JobConfig)

HATS = MODEL_HATS  # ("planning", "worker", "verify", "review", "reflect", "router", "steer", "plan_review")


# --------------------------------------------------------- the two constants


def test_the_two_constants_are_named_and_kept_apart():
    """Mirrors DEFAULT_MODEL_STRATEGY's own split: "what ships" vs "what an
    unrecognised name degrades to" must stay two different names, or a later
    change to one would silently retarget the other."""
    assert DEFAULT_HAT_MODEL_REF is None
    assert UNRECOGNISED_HAT_FALLBACK == "planning"
    assert UNRECOGNISED_HAT_FALLBACK in MODEL_HATS
    assert set(MODEL_HATS) == {"planning", "worker", "verify", "review",
                               "reflect", "router", "steer", "plan_review"}


# ------------------------------------------------- absent-table equivalence


def test_an_entirely_absent_table_with_nothing_configured_resolves_pi_default():
    cfg = JobConfig()
    for hat in HATS:
        assert cfg.model_for(hat) is DEFAULT_HAT_MODEL_REF is None


def test_an_entirely_absent_table_with_only_model_set_reproduces_today():
    """A one-model operator writes one line and every hat gets it -- the
    table's mere absence must never leave a hat unresolved."""
    cfg = JobConfig(model="only-model")
    for hat in HATS:
        assert cfg.model_for(hat) == "only-model"


# ------------------------------------------------------- per-hat resolution


def test_a_per_hat_entry_via_the_models_alias_wins_for_its_own_hat():
    cfg = JobConfig(models={
        "planning": "opus", "worker": "sonnet", "verify": "terra",
        "review": "r-model", "reflect": "f-model", "router": "rt-model",
    })
    assert cfg.model_for("planning") == "opus"
    assert cfg.model_for("worker") == "sonnet"
    assert cfg.model_for("verify") == "terra"
    assert cfg.model_for("review") == "r-model"
    assert cfg.model_for("reflect") == "f-model"
    assert cfg.model_for("router") == "rt-model"


def test_a_per_hat_entry_via_the_retained_model_overrides_name_still_works():
    """`model_overrides` is the RETAINED name and stays the mechanism --
    `models` is only an alias for it, never a replacement."""
    cfg = JobConfig(model_overrides={"worker": "W2"})
    assert cfg.model_for("worker") == "W2"


def test_the_worked_prd_example_is_reachable():
    """The PRD's own acceptance configuration (requirement A): an Opus
    planner, a Sonnet worker, a cross-family gpt-5.6-terra verifier."""
    cfg = JobConfig(models={
        "planning": "amazon-bedrock/eu.anthropic.claude-opus-5",
        "worker": "amazon-bedrock/eu.anthropic.claude-sonnet-5",
        "verify": "aigw-openai/openai.gpt-5.6-terra",
    })
    assert cfg.model_for("planning") == "amazon-bedrock/eu.anthropic.claude-opus-5"
    assert cfg.model_for("worker") == "amazon-bedrock/eu.anthropic.claude-sonnet-5"
    assert cfg.model_for("verify") == "aigw-openai/openai.gpt-5.6-terra"


def test_model_overrides_wins_over_the_models_alias_on_a_shared_key():
    """The pre-existing, more specific escape hatch wins when a job.yaml
    (implausibly) sets the same hat both ways."""
    cfg = JobConfig(models={"worker": "from-models"},
                    model_overrides={"worker": "from-overrides"})
    assert cfg.model_for("worker") == "from-overrides"


def test_the_models_alias_still_fills_in_keys_model_overrides_left_unset():
    cfg = JobConfig(models={"worker": "from-models", "planning": "from-models-2"},
                    model_overrides={"worker": "from-overrides"})
    assert cfg.model_for("worker") == "from-overrides"
    assert cfg.model_for("planning") == "from-models-2"


# --------------------------------------------------------- absent-hat default


def test_a_known_hat_absent_from_the_table_falls_back_to_the_shipped_model():
    cfg = JobConfig(model="shipped", models={"planning": "opus"})
    assert cfg.model_for("worker") == "shipped"
    assert cfg.model_for("verify") == "shipped"
    assert cfg.model_for("router") == "shipped"


def test_a_known_hat_absent_from_the_table_with_no_model_either_is_pi_default():
    cfg = JobConfig(models={"planning": "opus"})
    assert cfg.model_for("worker") is None
    assert cfg.model_for("verify") is None


# --------------------------------------------------- unknown-hat anti-downgrade


def test_an_unrecognised_hat_name_resolves_to_the_planning_entry_not_the_cheap_one():
    cfg = JobConfig(model="strong-default",
                    models={"worker": "cheap-worker", "planning": "opus"})
    # Never "cheap-worker" -- the anti-downgrade rule (never the cheapest).
    assert cfg.model_for("bogus-hat") == "opus"


def test_an_unrecognised_hat_name_falls_back_to_model_when_planning_is_unset():
    cfg = JobConfig(model="strong-default", models={"worker": "cheap-worker"})
    assert cfg.model_for("bogus-hat") == "strong-default"


def test_an_unrecognised_hat_name_with_nothing_configured_is_pi_default():
    cfg = JobConfig()
    assert cfg.model_for("bogus-hat") is None


@pytest.mark.parametrize("typo", ["planing", "Worker", "verifyer", "reveiw",
                                  " router", "reflekt"])
def test_a_family_of_typo_hat_names_all_take_the_anti_downgrade_path(typo):
    cfg = JobConfig(model="strong", models={"worker": "cheap", "planning": "opus"})
    assert cfg.model_for(typo) == "opus"
