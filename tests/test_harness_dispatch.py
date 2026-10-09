"""Harness dispatch (v0.30 T02c): a hat has a harness, the bundle carries it,
and the loop holds one runner per harness and dispatches on it. `pi` and
`claude-code` are registered by default; these tests replace both with fakes."""

from __future__ import annotations

import asyncio
import json

import pytest

from ralphd.engine.config import (DEFAULT_HARNESS, HARNESSES, MODEL_HATS,
                                  HatBundle, JobConfig)
from ralphd.engine.runner import IterationResult
from ralphd.engine.tiers import TIERS, default_host_map, resolve_bundle
from tests.conftest import make_supervisor

_CFG = dict(run_id="t02c", status={"state": "running"}, iterations=5,
            max_approaches=1, vigilant=False, on_complete="exit",
            infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
            infra_outage_budget_s=1000.0)


class _Fake:
    def __init__(self, name):
        self.name = name
        self.calls: list[dict] = []
        self.running = False
        self.interrupts = 0

    def interrupt(self) -> bool:
        self.interrupts += 1
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.calls.append(kw)
        r = IterationResult(exit_code=0)
        r.final_text = "not json"
        r.duration_s = 0.01
        r.usage = {}
        return r


def _sup(tmp_path, **cfg):
    sup = make_supervisor(tmp_path / "run", **{**_CFG, **cfg})
    pi, cc = _Fake("pi"), _Fake("claude-code")
    sup.runner = pi
    sup._runners = {"pi": pi, "claude-code": cc}
    return sup, pi, cc


def test_harness_constants():
    # codex is the third harness; its exact tuple is pinned by
    # tests/test_codex_harness_registration.py::test_codex_is_the_third_harness.
    assert HARNESSES == ("pi", "claude-code", "codex")
    assert DEFAULT_HARNESS == "pi"
    assert HatBundle().harness == "pi"


def test_no_harnesses_key_resolves_every_hat_to_pi():
    cfg = JobConfig(run_id="x")
    assert cfg.harnesses == {}
    for hat in MODEL_HATS:
        assert cfg.harness_for(hat) == "pi"
        assert cfg.bundle_for(hat).harness == "pi"


def test_configured_hat_harness_reaches_bundle_and_every_tier_rung():
    cfg = JobConfig(
        run_id="x", harnesses={"worker": "claude-code"},
        model_tiers={"worker": {"trivial": "p/small",
                                "hard": {"model": "p/big", "thinking": "high"}}})
    assert cfg.bundle_for("worker").harness == "claude-code"
    assert cfg.bundle_for("verify").harness == "pi"
    host_map = default_host_map(cfg)
    for tier in TIERS:
        assert resolve_bundle("worker", tier, host_map).harness == "claude-code"
        assert resolve_bundle("verify", tier, host_map).harness == "pi"
    assert resolve_bundle("worker", "hard", host_map).model == "p/big"


def test_unknown_hat_follows_planning():
    cfg = JobConfig(run_id="x", harnesses={"planning": "claude-code"})
    assert cfg.harness_for("no-such-hat") == "claude-code"
    assert cfg.harness_for("worker") == "pi"
    assert JobConfig(run_id="x").harness_for("no-such-hat") == "pi"


def test_harnesses_key_loads_from_job_yaml_dict():
    cfg = JobConfig(**{"run_id": "x", "harnesses": {"review": "claude-code"}})
    assert cfg.harness_for("review") == "claude-code"


def test_iteration_goes_to_the_runner_of_its_harness_and_meta_records_it(tmp_path):
    sup, pi, cc = _sup(tmp_path, harnesses={"worker": "claude-code"})
    # `sup.runner` is the pi fake and registered under "pi", so dispatch is real.
    asyncio.run(sup.run_iteration("worker"))
    asyncio.run(sup.run_iteration("planning"))
    assert len(cc.calls) == 1 and len(pi.calls) == 1
    meta1 = json.loads((sup.run.iteration_dir(1) / "meta.json").read_text())
    meta2 = json.loads((sup.run.iteration_dir(2) / "meta.json").read_text())
    assert meta1["harness"] == "claude-code"
    assert meta2["harness"] == "pi"


def test_router_and_contract_repair_use_the_hats_harness(tmp_path):
    sup, pi, cc = _sup(tmp_path, harnesses={"router": "claude-code",
                                            "verify": "claude-code"})
    asyncio.run(sup._ask_router("which way?"))
    assert len(cc.calls) == 1 and pi.calls == []
    asyncio.run(sup._ask_contract_fix("verify", ["bad"], final_text=None))
    assert len(cc.calls) == 2 and pi.calls == []
    asyncio.run(sup._ask_contract_fix("worker", ["bad"], final_text=None))
    assert len(pi.calls) == 1 and len(cc.calls) == 2


def test_a_scripted_sup_runner_overrides_every_harness(tmp_path):
    sup, pi, cc = _sup(tmp_path, harnesses={"worker": "claude-code"})
    double = _Fake("double")
    sup.runner = double          # the test seam: not the registered pi runner
    asyncio.run(sup.run_iteration("worker"))
    asyncio.run(sup.run_iteration("planning"))
    assert len(double.calls) == 2 and pi.calls == [] and cc.calls == []


def test_unregistered_harness_raises_a_clear_error(tmp_path):
    """The raise happens inside `_run_iteration_once`'s containment boundary,
    so it becomes an ordinary failed iteration rather than an engine crash."""
    sup, pi, cc = _sup(tmp_path)
    del sup._runners["claude-code"]
    with pytest.raises(RuntimeError, match="no runner registered for harness"):
        sup._runner_for("claude-code")
    assert sup._runner_for("pi") is pi


def test_interrupt_reaches_a_running_claude_code_runner(tmp_path):
    sup, pi, cc = _sup(tmp_path)
    cc.running = True
    assert sup.interrupt() is True
    assert cc.interrupts == 1 and pi.interrupts == 1
    cc.running = False
    assert sup.interrupt() is False
