"""Task 006 (#60 pillar 4, requirement A): retirement of task 044's default.

Task 044 (#53) pinned `balanced` as the one shipped `model_strategy` default,
written in exactly one place (`engine/config.DEFAULT_MODEL_STRATEGY`) and
imported host-side so the engine and `ralphctl` could never disagree about
an unconfigured run's phase→tier mix. Task 006 removed the mechanism that
constant fed entirely: `model_strategy`, `fast_model` and
`JobConfig.STRATEGY_TIERS` are gone, replaced by task 005's per-hat
`models`/`model_overrides` table, which has no "current strategy" to default
-- an unconfigured job just resolves every hat to the bare `model` field
(`None` unless set), so there is no longer a "which preset ships" question
for a constant to answer.

What still needs guarding, now that the mechanism itself is retired:

* the three retired names are gone from `engine.config`'s public surface --
  nothing can accidentally import a live `DEFAULT_MODEL_STRATEGY` again and
  wire it back into a resolution rung;
* the migration that keeps a *pre-006* job.yaml's `model_strategy`/
  `fast_model` pair resolving to the exact models it always did is
  deterministic for both keys, never a silent drop into `extra` -- this is
  the one place either legacy name is still read at all
  (`_migrate_legacy_model_config`), and it must reproduce the retired
  `STRATEGY_TIERS` table exactly, including the "unrecognised strategy name
  degrades to `quality-first`, not to the shipped default" anti-downgrade
  rule task 044's own tests pinned;
* the CLI surfaces that used to accept a strategy name (`ralphctl start
  --model-strategy`, `ralphctl config set model_strategy`) are refused, not
  silently ignored -- covered in depth by `tests/test_cli_config.py`, so
  this module only pins that the retired names carry no default anywhere
  `ralphctl` could still hardcode one.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from ralphd.engine.config import MODEL_HATS, JobConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PY = REPO_ROOT / "src" / "ralphd" / "engine" / "config.py"
MAIN_PY = REPO_ROOT / "src" / "ralphd" / "cli" / "main.py"

PHASES = ("planning", "worker", "review", "verify", "reflect")

# The exact table `_LEGACY_STRATEGY_TIERS` must keep reproducing, copied here
# as an independent expectation (not read from the module) so a change to
# the legacy table's *values* -- not just its presence -- fails a test.
LEGACY_TIERS = {
    "quality-first": {p: "strong" for p in PHASES},
    "cost-optimized": {"planning": "strong", "worker": "fast", "review": "fast",
                       "verify": "fast", "reflect": "fast"},
    "balanced": {"planning": "strong", "worker": "fast", "review": "strong",
                 "verify": "fast", "reflect": "strong"},
}
STRONG = "strongco/strong-model"
FAST = "fastco/fast-model"


# --------------------------------------------------- the retired names are gone


def test_the_three_retired_names_are_not_on_the_config_module():
    import ralphd.engine.config as config_mod
    for name in ("DEFAULT_MODEL_STRATEGY", "MODEL_STRATEGIES"):
        assert not hasattr(config_mod, name), (
            f"engine.config.{name} is back -- task 006 retired the "
            "strategy-tier mechanism it fed; this must be gone, not merely "
            "unused")
    assert not hasattr(JobConfig, "STRATEGY_TIERS")
    assert not hasattr(JobConfig(), "model_strategy")
    assert not hasattr(JobConfig(), "fast_model")


def test_no_live_reference_to_the_retired_names_outside_migration_code():
    """`grep -rn 'STRATEGY_TIERS|model_strategy|fast_model' src/` must only
    turn up migration/compat code -- this pins the engine half of that by
    reading the AST: every remaining name in config.py is a `_LEGACY_*`
    identifier or lives inside `_migrate_legacy_model_config`."""
    tree = ast.parse(CONFIG_PY.read_text())
    migrate_fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef)
                      and n.name == "_migrate_legacy_model_config")
    migrate_lines = set(range(migrate_fn.lineno, migrate_fn.end_lineno + 1))
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in (
                "model_strategy", "fast_model", "STRATEGY_TIERS"):
            if node.lineno not in migrate_lines:
                bad.append((node.lineno, node.id))
    assert not bad, f"live (non-migration) reference(s) remain: {bad}"


def test_no_strategy_name_is_hardcoded_on_the_host_side():
    """`ralphctl` must not carry a strategy name as a default of its own --
    there is no rung left for one to feed."""
    literals = [(node.lineno, node.value)
                for node in ast.walk(ast.parse(MAIN_PY.read_text()))
                if isinstance(node, ast.Constant)
                and node.value in set(LEGACY_TIERS)]
    assert not literals, (
        f"cli/main.py still hardcodes a retired strategy name: {literals}")


# ------------------------------------------- a default JobConfig has no strategy


def test_a_default_jobconfig_resolves_every_hat_to_the_bare_model_field():
    cfg = JobConfig()
    assert {h: cfg.model_for(h) for h in MODEL_HATS} == {h: None for h in MODEL_HATS}
    cfg = JobConfig(model="only")
    assert {h: cfg.model_for(h) for h in MODEL_HATS} == \
        {h: "only" for h in MODEL_HATS}


def test_a_job_yaml_with_neither_legacy_key_never_reaches_the_migration(tmp_path, caplog):
    (tmp_path / "job.yaml").write_text("run_id: r\niterations: 3\n")
    with caplog.at_level(logging.WARNING):
        cfg = JobConfig.load(tmp_path / "job.yaml")
    assert cfg.model_overrides == {}
    assert not any("model_strategy" in r.message or "fast_model" in r.message
                  for r in caplog.records)


# --------------------------------------------- the migration: both legacy keys


def _migrated_models(**job_yaml_keys) -> dict[str, str | None]:
    from ralphd.engine.config import _migrate_legacy_model_config
    raw = _migrate_legacy_model_config(dict(job_yaml_keys))
    return raw.get("models", {})


@pytest.mark.parametrize("strategy", list(LEGACY_TIERS))
def test_model_strategy_alone_translates_with_the_fast_tier_falling_back_to_model(strategy):
    """The `model_strategy` key, with no `fast_model`: every hat -- whatever
    tier the retired table gave it -- lands on the bare `model` value,
    because the fast tier itself was always `fast_model or model` (task
    044's "no fast_model, no change" rule)."""
    models = _migrated_models(model_strategy=strategy, model=STRONG)
    assert models == {p: STRONG for p in PHASES}


@pytest.mark.parametrize("strategy", list(LEGACY_TIERS))
def test_fast_model_alone_translates_together_with_the_strategy(strategy):
    """Both legacy keys present: `fast_model` fills every fast-tier hat, the
    plain `model` every strong-tier hat -- exactly `_LEGACY_STRATEGY_TIERS`,
    never a silent drop of either key."""
    models = _migrated_models(model_strategy=strategy, model=STRONG, fast_model=FAST)
    tiers = LEGACY_TIERS[strategy]
    expected = {p: (STRONG if tiers[p] == "strong" else FAST) for p in PHASES}
    assert models == expected


def test_fast_model_with_no_strategy_key_defaults_to_balanced():
    """`model_strategy` omitted entirely defaults to the retired mechanism's
    own default (`balanced`), matching the pre-006 live behaviour exactly."""
    models = _migrated_models(model="S", fast_model="F")
    assert models == {"planning": "S", "worker": "F", "review": "S",
                      "verify": "F", "reflect": "S"}


def test_an_unrecognised_legacy_strategy_name_degrades_to_quality_first():
    """Task 044's anti-downgrade rule survives the migration verbatim: a
    typo must never quietly resolve as the cheap tier."""
    models = _migrated_models(model_strategy="qualtiy-first", model=STRONG,
                              fast_model=FAST)
    assert models == {p: STRONG for p in PHASES}


def test_an_explicit_models_table_beats_the_translated_one_per_hat():
    models = _migrated_models(model_strategy="cost-optimized", model=STRONG,
                              fast_model=FAST, models={"worker": "explicit"})
    assert models["worker"] == "explicit"
    assert models["planning"] == STRONG


def test_an_explicit_model_overrides_table_beats_the_translated_one_too():
    models = _migrated_models(model_strategy="cost-optimized", model=STRONG,
                              fast_model=FAST, model_overrides={"verify": "explicit"})
    assert models["verify"] == "explicit"


def test_the_migration_pops_both_legacy_keys_and_logs_a_warning(caplog):
    from ralphd.engine.config import _migrate_legacy_model_config
    with caplog.at_level(logging.WARNING):
        raw = _migrate_legacy_model_config(
            {"model_strategy": "balanced", "fast_model": "F", "model": "S"})
    assert "model_strategy" not in raw
    assert "fast_model" not in raw
    assert any("model_strategy" in r.message and "fast_model" in r.message
              for r in caplog.records), (
        "the migration must log so an operator finds out their job.yaml "
        "still names a retired key, rather than the translation happening "
        "invisibly")


def test_a_job_yaml_round_trip_resolves_every_hat_like_the_retired_live_code(tmp_path):
    """The end-to-end path: `JobConfig.load()` on a real pre-006 job.yaml
    resolves every hat exactly like the retired `STRATEGY_TIERS` rung did."""
    (tmp_path / "job.yaml").write_text(
        f"run_id: r\nmodel_strategy: cost-optimized\n"
        f"model: {STRONG}\nfast_model: {FAST}\n")
    cfg = JobConfig.load(tmp_path / "job.yaml")
    tiers = LEGACY_TIERS["cost-optimized"]
    for phase in PHASES:
        want = STRONG if tiers[phase] == "strong" else FAST
        assert cfg.model_for(phase) == want, phase
    # router never had a strategy-table row; the migration must not invent
    # one for it either.
    assert cfg.model_for("router") == STRONG
