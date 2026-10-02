"""Task 009 (PRD requirement D, #60 pillar 4): "Fail fast at start when a
configured hat's model has no route in the profile."

`ralphd.engine.llm.check_hat_routes` resolves every `MODEL_HATS` entry's ref
(`JobConfig.model_for`) and checks its provider key against this profile's
own `~/.pi/agent/models.json` (`provider_keys()`) -- never a live gateway
call. `main.amain()` runs it before `LoopSupervisor.run_job()` is ever
scheduled, so an unroutable ref aborts the process before iteration 1 can
run, and a fully routable table is unaffected.

Both tests drive a real `amain()` with a stubbed `run_job`/`uvicorn.Server.
serve` -- the same black-box pattern as
tests/test_engine_budget_seed.py::_run_amain_capturing_cfg.
"""

from __future__ import annotations

import json
import socket

import pytest
import uvicorn

from ralphd.engine.loop import LoopSupervisor

GOOD_PROVIDER = "good-provider"
BAD_REF = "bogus-provider/some-model"
GOOD_VERIFY_REF = f"{GOOD_PROVIDER}/verify-model"
GOOD_PLANNING_REF = f"{GOOD_PROVIDER}/planning-model"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _events(run_dir):
    f = run_dir / "events.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text().splitlines() if line]


def _write_models_json(home_dir, providers: dict) -> None:
    agent_dir = home_dir / ".pi" / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "models.json").write_text(
        json.dumps({"providers": providers}))


async def _run_amain(tmp_path, monkeypatch, *, models_yaml_block: str,
                      providers: dict, llm_wiring: dict | None = None):
    from ralphd.engine import config as config_mod
    from ralphd.engine import main as main_mod
    from ralphd.engine import skills as skills_mod

    cfg_dir, run_dir, ws, home_dir = (
        tmp_path / "cfg", tmp_path / "run", tmp_path / "ws", tmp_path / "home")
    for d in (cfg_dir, run_dir, ws, home_dir):
        d.mkdir()
    (cfg_dir / "prd.md").write_text("# PRD\n")
    (cfg_dir / "job.yaml").write_text(
        "run_id: hat-route-check\niterations: 5\non_complete: exit\n"
        + models_yaml_block)
    _write_models_json(home_dir, providers)
    if llm_wiring is not None:
        (cfg_dir / "llm-wiring.json").write_text(json.dumps(llm_wiring))

    monkeypatch.setattr(config_mod, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(main_mod, "CONFIG_DIR", cfg_dir)
    # A pre-existing regression (task 002/003, #83's `place_skills` guard,
    # unrelated to this task): `skills.py` imports its own `CONFIG_DIR`
    # name at module-load time, so patching `config_mod`/`main_mod` alone
    # leaves `place_skills`'s internal check comparing against the real
    # `/config` -- patch it here too so this test can reach `amain()` at
    # all. See notes.md for the report; not this task's to fix.
    monkeypatch.setattr(skills_mod, "CONFIG_DIR", cfg_dir)
    monkeypatch.setenv("RALPHD_RUN_DIR", str(run_dir))
    monkeypatch.setenv("RALPHD_WORKSPACE_DIR", str(ws))
    monkeypatch.setenv("RALPHD_PORT", str(_free_port()))
    monkeypatch.setenv("HOME", str(home_dir))

    run_job_calls: list[str] = []

    async def fake_serve(self):          # no socket, no port bound
        return None

    async def fake_run_job(self):
        run_job_calls.append(self.cfg.run_id)
        return "succeeded"

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    monkeypatch.setattr(LoopSupervisor, "run_job", fake_run_job)

    rc = await main_mod.amain()
    return rc, run_dir, run_job_calls, main_mod


@pytest.mark.asyncio
async def test_unroutable_hat_model_aborts_before_iteration_1(
        tmp_path, monkeypatch):
    rc, run_dir, run_job_calls, main_mod = await _run_amain(
        tmp_path, monkeypatch,
        models_yaml_block=f"models:\n  verify: {BAD_REF}\n",
        providers={GOOD_PROVIDER: {"baseUrl": "https://example.invalid"}})

    assert rc == main_mod.EXIT_UNROUTABLE_HAT_MODEL
    # the loop never reached iteration 1 -- run_job was never scheduled
    assert run_job_calls == []

    logs = [e for e in _events(run_dir)
            if e["type"] == "log" and e.get("level") == "error"]
    assert logs, _events(run_dir)
    message = logs[-1]["message"]
    assert "verify" in message           # the hat
    assert BAD_REF in message            # the offending ref
    assert GOOD_PROVIDER in message      # the provider keys that ARE available

    # status.json never advanced to "running" -- the loop truly never started
    status_path = run_dir / "status.json"
    if status_path.exists():
        assert json.loads(status_path.read_text()).get("state") != "running"


@pytest.mark.asyncio
async def test_unroutable_plan_review_hat_model_aborts_before_iteration_1(
        tmp_path, monkeypatch):
    """Task 019 (requirement M): the peer plan-review gate is opt-in and
    off by default (`JobConfig.peer_plan_review`), but a misconfigured
    `plan_review` model ref must fail exactly like any other hat's --
    never silently degrade to 'no gate' just because the gate itself
    happens to be off in this job.yaml. `check_hat_routes` (task 009/005)
    already walks every `config.MODEL_HATS` entry unconditionally
    (`plan_review` is one, task 018), so this pins that this real hat's
    entry is genuinely covered rather than merely present in the tuple --
    mirroring `test_unroutable_hat_model_aborts_before_iteration_1` above,
    for a different hat and with the gate left off."""
    rc, run_dir, run_job_calls, main_mod = await _run_amain(
        tmp_path, monkeypatch,
        models_yaml_block=f"models:\n  plan_review: {BAD_REF}\n",
        providers={GOOD_PROVIDER: {"baseUrl": "https://example.invalid"}})

    assert rc == main_mod.EXIT_UNROUTABLE_HAT_MODEL
    # the loop never reached iteration 1 -- run_job was never scheduled,
    # exactly as for any other unroutable hat, gate on or off.
    assert run_job_calls == []

    logs = [e for e in _events(run_dir)
            if e["type"] == "log" and e.get("level") == "error"]
    assert logs, _events(run_dir)
    message = logs[-1]["message"]
    assert "plan_review" in message      # the hat
    assert BAD_REF in message            # the offending ref
    assert GOOD_PROVIDER in message      # the provider keys that ARE available

    status_path = run_dir / "status.json"
    if status_path.exists():
        assert json.loads(status_path.read_text()).get("state") != "running"


@pytest.mark.asyncio
async def test_unroutable_tier_rung_aborts_with_exact_message(
        tmp_path, monkeypatch):
    """Task 005 (#68): a typo'd `model_tiers` rung -- not just a bare
    per-hat `models:` entry -- must be caught before iteration 1, and the
    error must name the hat, the tier, the offending ref, and the
    provider keys that ARE available, in exactly this wording."""
    rc, run_dir, run_job_calls, main_mod = await _run_amain(
        tmp_path, monkeypatch,
        models_yaml_block=(
            f"models:\n  verify: {GOOD_VERIFY_REF}\n"
            f"model_tiers:\n  verify:\n    hard: {BAD_REF}\n"),
        providers={GOOD_PROVIDER: {"baseUrl": "https://example.invalid"}})

    assert rc == main_mod.EXIT_UNROUTABLE_HAT_MODEL
    assert run_job_calls == []

    logs = [e for e in _events(run_dir)
            if e["type"] == "log" and e.get("level") == "error"]
    assert logs, _events(run_dir)
    message = logs[-1]["message"]
    assert message == (
        f"hat 'verify' tier 'hard' is configured with model {BAD_REF!r}, "
        f"whose provider 'bogus-provider' is not one of the providers "
        f"this profile declares ({GOOD_PROVIDER})")

    status_path = run_dir / "status.json"
    if status_path.exists():
        assert json.loads(status_path.read_text()).get("state") != "running"


@pytest.mark.asyncio
async def test_routable_tier_rung_does_not_raise(tmp_path, monkeypatch):
    """Task 005 (#68): a `model_tiers` rung whose ref IS routable must not
    trip the check, even alongside a bare per-hat `models:` entry -- the
    widened check must not turn a previously-fine table into a failure."""
    rc, run_dir, run_job_calls, main_mod = await _run_amain(
        tmp_path, monkeypatch,
        models_yaml_block=(
            f"models:\n  verify: {GOOD_VERIFY_REF}\n"
            f"model_tiers:\n  verify:\n"
            f"    hard: {GOOD_PROVIDER}/verify-hard-model\n"),
        providers={GOOD_PROVIDER: {"baseUrl": "https://example.invalid"}})

    assert rc == 0
    assert run_job_calls == ["hat-route-check"]

    logs = [e for e in _events(run_dir)
            if e["type"] == "log" and e.get("level") == "error"]
    assert logs == []


@pytest.mark.asyncio
async def test_fully_routable_table_starts_normally_no_network_call(
        tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("network call attempted during startup validation")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket.socket, "connect_ex", _boom)

    rc, run_dir, run_job_calls, main_mod = await _run_amain(
        tmp_path, monkeypatch,
        models_yaml_block=(
            f"models:\n  planning: {GOOD_PLANNING_REF}\n"
            f"  verify: {GOOD_VERIFY_REF}\n"),
        providers={GOOD_PROVIDER: {"baseUrl": "https://example.invalid"}})

    assert rc == 0
    assert run_job_calls == ["hat-route-check"]   # the loop DID reach iteration 1

    logs = [e for e in _events(run_dir)
            if e["type"] == "log" and e.get("level") == "error"]
    assert logs == []


# ---------------------------------------------- per-harness routes (A.5, I.1)
async def _message(tmp_path, monkeypatch, **kw):
    rc, run_dir, calls, main_mod = await _run_amain(tmp_path, monkeypatch, **kw)
    logs = [e for e in _events(run_dir)
            if e["type"] == "log" and e.get("level") == "error"]
    return rc, calls, main_mod, (logs[-1]["message"] if logs else "")


@pytest.mark.asyncio
async def test_claude_code_hat_with_non_anthropic_ref_is_refused_empty_models_json(
        tmp_path, monkeypatch):
    rc, calls, main_mod, message = await _message(
        tmp_path, monkeypatch,
        models_yaml_block=("harnesses:\n  worker: claude-code\n"
                           "models:\n  worker: aigw-openai/x\n"),
        providers={})
    assert rc == main_mod.EXIT_UNROUTABLE_HAT_MODEL
    assert calls == []
    assert "'worker'" in message and "aigw-openai/x" in message
    assert "claude-code" in message


@pytest.mark.asyncio
async def test_claude_code_tier_leaf_with_non_anthropic_ref_is_refused(
        tmp_path, monkeypatch):
    rc, calls, main_mod, message = await _message(
        tmp_path, monkeypatch,
        models_yaml_block=("harnesses:\n  verify: claude-code\n"
                           "model_tiers:\n  verify:\n    hard: aigw-openai/x\n"),
        providers={})
    assert rc == main_mod.EXIT_UNROUTABLE_HAT_MODEL
    assert "'verify'" in message and "'hard'" in message
    assert "aigw-openai/x" in message and "claude-code" in message


@pytest.mark.asyncio
async def test_claude_code_hat_with_anthropic_or_no_ref_starts(
        tmp_path, monkeypatch):
    rc, calls, _m, message = await _message(
        tmp_path, monkeypatch,
        models_yaml_block=("harnesses:\n  worker: claude-code\n"
                           "  review: claude-code\n"
                           "models:\n  worker: anthropic/claude-sonnet-5\n"),
        providers={})
    assert rc == 0, message
    assert calls == ["hat-route-check"]


@pytest.mark.asyncio
async def test_builtin_providers_make_anthropic_pi_hat_routable(
        tmp_path, monkeypatch):
    block = "models:\n  worker: anthropic/claude-sonnet-5\n"
    providers = {GOOD_PROVIDER: {"baseUrl": "https://example.invalid"}}
    rc, calls, main_mod, message = await _message(
        tmp_path, monkeypatch, models_yaml_block=block, providers=providers,
        llm_wiring={"mode": "p", "env": {}, "mounts": [],
                    "builtinProviders": ["anthropic"]})
    assert rc == 0, message
    assert calls == ["hat-route-check"]


@pytest.mark.asyncio
async def test_without_builtin_providers_anthropic_pi_hat_is_refused(
        tmp_path, monkeypatch):
    rc, calls, main_mod, message = await _message(
        tmp_path, monkeypatch,
        models_yaml_block="models:\n  worker: anthropic/claude-sonnet-5\n",
        providers={GOOD_PROVIDER: {"baseUrl": "https://example.invalid"}})
    assert rc == main_mod.EXIT_UNROUTABLE_HAT_MODEL
    assert "anthropic" in message and GOOD_PROVIDER in message
