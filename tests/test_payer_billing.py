"""#298 (v0.32.1): cost grouped by payer / billing channel.

A profile declares who pays (`billing: {payer, basis}`); `ralphctl start`
inlines the run's billing block into job.yaml; the engine stamps `payer` and
`basis` onto every iteration's (and router/contract call's) usage at call
time; status.json's `usage.byPayer` groups by whatever payer labels exist,
with one `byModel` row per requested model ref; every surface renders the
one server-worded `state.payer_view`.

What is proven here:

  * profile and registry validation (`resolve_billing`), the job.yaml
    normaliser (`normalize_billing`) and the per-harness stamp
    (`JobConfig.billing_for`);
  * `ralphctl start` writes the `billing` key (named profile, host, flags),
    never for `--llm none`, and never into llm-wiring.json;
  * `state.stamp_billing` RELOCATES a zero-priced route's money evidence and
    drops nothing; an unpriceable subscription iteration is never a silent
    $0 on any per-iteration surface (issue point 6);
  * `state.accumulate_payer_usage` keeps the byPhase sum invariant;
  * the issue's own deck-phase4k numbers, synthesized and replayed from the
    real run's iteration records (tests/fixtures/payer_deck_phase4k.json);
  * a run recorded before 0.32.1 (no byPayer) renders byte for byte as it
    did, on every surface;
  * the engine end to end, the hub, the run record and the CLI.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path

import pytest
import yaml

from ralphd.cli import llm_profiles
from ralphd.cli.llm_profiles import ProfileError, resolve_billing
from ralphd.engine import config as C
from ralphd.engine import loop as loop_module
from ralphd.engine import run_record, state
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import call_ralphctl_in_process
from tests.test_cli_llm_profiles import Ctl

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "payer_deck_phase4k.json"
APP_JS = REPO / "src" / "ralphd" / "cli" / "web" / "app.js"

SUB_PAYER, GW_PAYER = "Claude subscription", "AIGW gateway"

# A pi route quoting part of its money and deriving the rest.
PI_QUOTED_DERIVED = {"input": 100, "output": 50, "cacheRead": 850,
                     "totalTokens": 1000, "costUSD": 0.25, "costPriced": True,
                     "costDerived": True, "costDerivedUSD": 0.75}
# A pi route nothing could price.
PI_UNPRICEABLE = {"input": 100, "output": 50, "totalTokens": 150, "costUSD": 0,
                  "costPriced": False, "costDerived": False}
# The AIGW shape of deck-phase4k: $0 quoted for billed tokens, rate derived.
PI_ZERO_QUOTE = {"input": 22, "output": 4320, "cacheRead": 246596,
                 "cacheWrite": 35233, "totalTokens": 286171, "costUSD": 0,
                 "costPriced": False, "costZeroQuoted": True,
                 "costDerived": True, "costDerivedUSD": 0.171586}
PI_PRICED = {"input": 100, "output": 50, "totalTokens": 150, "costUSD": 1.23,
             "costPriced": True}
CC_SUB = {"input": 10, "output": 5, "totalTokens": 15, "costUSD": 0.0,
          "costFree": True, "costBasis": "subscription",
          "listEquivalentUSD": 0.4}
# A claude-code iteration that ended before the SDK's ResultMessage.
CC_SUB_INTERRUPTED = {"input": 10, "output": 5, "totalTokens": 15,
                      "costUSD": 0.0, "costFree": True,
                      "costBasis": "subscription"}

COST_FIELDS = ("costUSD", "costPriced", "costDerived", "costDerivedUSD",
               "costZeroQuoted", "costFree", "costBasis", "listEquivalentUSD")


def _numeric(d: dict) -> dict:
    return {k: v for k, v in d.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}


def _assert_sum_invariant(total: dict) -> None:
    by_payer = total["byPayer"]
    for field, value in _numeric(total).items():
        summed = sum(b.get(field, 0) for b in by_payer.values())
        assert summed == pytest.approx(value, abs=1e-5), field
    for bucket in by_payer.values():
        for field, value in _numeric(bucket).items():
            summed = sum(m.get(field, 0) for m in bucket["byModel"].values())
            assert summed == pytest.approx(value, abs=1e-5), field


def _fold(total: dict, usage: dict, model: str | None = None) -> dict:
    """What `LoopSupervisor._accumulate_usage` does to the total, minus the
    status.json round-trip."""
    LoopSupervisor._merge_usage(total, usage)
    state.accumulate_payer_usage(total, usage, model=model)
    return total


def _dead_run(tmp_path: Path, usage: dict, run_id: str = "payer") -> Path:
    registry = tmp_path / "registry"
    run_dir = registry / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps(
        {"runId": run_id, "state": "succeeded", "iterationsUsed": 2,
         "usage": usage}))
    return registry


def _profile_reg(tmp_path: Path, name: str, doc: dict) -> Path:
    reg = tmp_path / "reg"
    (reg / "llm-profiles").mkdir(parents=True, exist_ok=True)
    (reg / "llm-profiles" / f"{name}.yaml").write_text(yaml.safe_dump(doc))
    return reg


# ------------------------------------------------------- 1. resolve_billing


def test_a_profile_without_billing_falls_back_to_its_name(tmp_path):
    reg = _profile_reg(tmp_path, "gw", {"description": "d", "model": "m"})
    assert llm_profiles.resolve_profile("gw", reg, host_env={})["billing"] == {
        "payer": "gw", "basis": "metered"}


def test_a_claude_code_profile_without_billing_bills_the_subscription(tmp_path):
    reg = _profile_reg(tmp_path, "sub", {"description": "d", "harness": "claude-code",
                                         "model": "anthropic/claude-sonnet-5"})
    assert llm_profiles.resolve_profile("sub", reg, host_env={})["billing"] == {
        "payer": "sub", "basis": "subscription"}


def test_an_explicit_billing_block_is_literal(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYER_PROBE", "resolved-value")
    reg = _profile_reg(tmp_path, "gw", {
        "description": "d", "model": "m",
        "billing": {"payer": "${env:PAYER_PROBE}", "basis": "chargeback"}})
    resolved = llm_profiles.resolve_profile("gw", reg, host_env=dict(os.environ))
    assert resolved["billing"] == {"payer": "${env:PAYER_PROBE}",
                                   "basis": "chargeback"}
    redacted = llm_profiles.resolve_profile("gw", reg, host_env=dict(os.environ),
                                            redact=True)
    assert redacted["billing"]["payer"] == "${env:PAYER_PROBE}"


def test_a_basis_is_case_and_whitespace_tolerant():
    out = resolve_billing({"basis": "Subscription "}, label="p", harness="pi",
                          default_payer="p")
    assert out == {"payer": "p", "basis": "subscription"}


@pytest.mark.parametrize("raw, harness, words", [
    (["payer"], "pi", "must be a mapping"),
    ({"payer": "x", "colour": "red"}, "pi", "unknown key"),
    ({"subscription_payer": "x"}, "pi", "unknown key"),
    ({"payer": "  "}, "pi", "non-blank"),
    ({"payer": 7}, "pi", "non-blank"),
    ({"payer": "x\nspend:     $999.00 (metered)"}, "pi", "control characters"),
    ({"payer": "x\ty"}, "pi", "control characters"),
    ({"basis": "prepaid"}, "pi", "is not one of"),
    ({"basis": "metered"}, "claude-code", "contradicts harness"),
])
def test_a_bad_billing_block_is_a_profile_error(raw, harness, words):
    with pytest.raises(ProfileError) as e:
        resolve_billing(raw, label="llm profile 'p'", harness=harness,
                        default_payer="p")
    assert str(e.value).startswith("llm profile 'p': billing")
    assert words in str(e.value)


def test_a_profile_billing_error_surfaces_from_resolve_profile(tmp_path):
    reg = _profile_reg(tmp_path, "gw", {"description": "d", "model": "m",
                                        "billing": {"basis": "nope"}})
    with pytest.raises(ProfileError, match="llm profile 'gw': billing basis"):
        llm_profiles.resolve_profile("gw", reg, host_env={})


def test_a_label_with_only_surrounding_whitespace_is_stripped_not_refused():
    out = resolve_billing({"payer": "Team\n"}, label="p", harness="pi",
                          default_payer="p")
    assert out["payer"] == "Team"


def test_a_control_character_in_subscription_payer_is_refused():
    with pytest.raises(ProfileError, match="control characters"):
        resolve_billing({"subscription_payer": "Max\rplan"},
                        label="registry config.yaml", harness=C.DEFAULT_HARNESS,
                        default_payer=C.HOST_BILLING_PAYER,
                        extra_keys=("subscription_payer",))


def test_the_registry_block_validates_subscription_payer_as_an_extra_key():
    out = resolve_billing(
        {"payer": "Home", "basis": "subscription", "subscription_payer": " Max "},
        label="registry config.yaml", harness=C.DEFAULT_HARNESS,
        default_payer=C.HOST_BILLING_PAYER, extra_keys=("subscription_payer",))
    assert out == {"payer": "Home", "basis": "subscription",
                   "subscription_payer": "Max"}
    assert resolve_billing(None, label="r", harness="pi",
                           default_payer=C.HOST_BILLING_PAYER,
                           extra_keys=("subscription_payer",)) == {
        "payer": C.HOST_BILLING_PAYER, "basis": "metered"}


# ----------------------------------------------------------------- 2. config


def test_normalize_billing_degrades_without_raising():
    n = C.normalize_billing
    assert n(None) is None
    assert n(["not", "a", "dict"]) is None
    assert n({"payer": "x", "basis": "prepaid", "harness": "pi"})["basis"] == "metered"
    forced = n({"payer": "x", "basis": "metered", "harness": "claude-code"})
    assert forced["basis"] == "subscription"
    assert n({"payer": " ", "profile": "gw", "basis": "metered"})["payer"] == "gw"
    assert n({"payer": None, "basis": "metered"})["payer"] == C.BILLING_UNLABELLED_PAYER
    full = n({"payer": "x", "basis": "chargeback", "profile": "gw", "harness": "pi"})
    assert full == {"payer": "x", "basis": "chargeback", "profile": "gw",
                    "harness": "pi", "subscription_payer": "claude-code subscription"}
    assert C.DEFAULT_SUBSCRIPTION_PAYER == "claude-code subscription"
    assert n({"payer": "x", "harness": "bogus"})["harness"] == C.DEFAULT_HARNESS
    # A hand-edited label cannot forge a line in `ralphctl cost`/the record.
    forged = n({"payer": "x\nspend:     $999.00", "basis": "metered",
                "subscription_payer": "Max\r\n plan"})
    assert forged["payer"] == "x spend:     $999.00"
    assert forged["subscription_payer"] == "Max  plan"


def test_billing_for_under_a_pi_profile():
    cfg = JobConfig(run_id="r", billing={
        "payer": GW_PAYER, "basis": "chargeback", "profile": "gw",
        "harness": "pi", "subscription_payer": "Max plan"})
    assert cfg.billing_for("pi") == (GW_PAYER, "chargeback")
    assert cfg.billing_for("claude-code") == ("Max plan", "subscription")


def test_billing_for_under_a_claude_code_profile():
    cfg = JobConfig(run_id="r", billing={
        "payer": SUB_PAYER, "basis": "subscription", "profile": "claude-sub",
        "harness": "claude-code"})
    assert cfg.billing_for("claude-code") == (SUB_PAYER, "subscription")
    assert cfg.billing_for("pi") == ("claude-sub", "metered")


def test_billing_for_a_legacy_run_is_none():
    assert JobConfig(run_id="r").billing_for("pi") is None
    assert JobConfig(run_id="r").billing_for("claude-code") is None


def test_effective_config_carries_the_normalised_billing():
    assert JobConfig(run_id="r").effective()["billing"] is None
    cfg = JobConfig(run_id="r", billing={"payer": "x", "basis": "FREE",
                                          "profile": "p", "harness": "pi"})
    assert cfg.effective()["billing"] == {
        "payer": "x", "basis": "free", "profile": "p", "harness": "pi",
        "subscription_payer": C.DEFAULT_SUBSCRIPTION_PAYER}


def test_the_forced_basis_matches_the_claude_code_runners_cost_basis():
    """HARNESS_FORCED_BASIS is the data twin of `_mark_subscription`."""
    from ralphd.engine.runner import ClaudeCodeRunner
    assert set(C.HARNESS_FORCED_BASIS) <= set(C.HARNESSES)
    result = IterationResult(exit_code=0)
    result.usage = {"input": 1, "output": 1, "totalTokens": 2}
    ClaudeCodeRunner._mark_subscription(result)
    assert (result.usage["costBasis"]
            == C.HARNESS_FORCED_BASIS[C.CLAUDE_CODE_HARNESS])
    assert set(state.PAYER_ZERO_PRICED_BASES) == set(C.ZERO_PRICED_BASES)
    assert C.HARNESS_FORCED_BASIS[C.CLAUDE_CODE_HARNESS] in C.ZERO_PRICED_BASES


# ---------------------------------------------------------- 3. ralphctl start


@pytest.fixture
def ctl(tmp_path, capsys):
    return Ctl(tmp_path, capsys)


def _job_billing(ctl: Ctl, run_id: str):
    return yaml.safe_load((ctl.config_dir(run_id) / "job.yaml").read_text()).get(
        "billing", "ABSENT")


def test_start_writes_the_profiles_billing(ctl):
    ctl.write_profile("gw", {"model": "gw/m", "env": {},
                             "billing": {"payer": GW_PAYER, "basis": "chargeback"}})
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "gw", "--run-id", "b-gw")
    assert res.returncode == 0, res.stderr
    assert _job_billing(ctl, "b-gw") == {
        "payer": GW_PAYER, "basis": "chargeback", "profile": "gw",
        "harness": "pi", "subscription_payer": C.DEFAULT_SUBSCRIPTION_PAYER}
    text = (ctl.config_dir("b-gw") / "job.yaml").read_text()
    assert any(ln.startswith("billing: {") for ln in text.splitlines())
    wiring = ctl.config_dir("b-gw") / "llm-wiring.json"
    if wiring.exists():
        assert "billing" not in wiring.read_text()
        assert GW_PAYER not in wiring.read_text()


def test_start_on_host_writes_the_host_fallback(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--run-id", "b-host")
    assert res.returncode == 0, res.stderr
    assert _job_billing(ctl, "b-host") == {
        "payer": C.HOST_BILLING_PAYER, "basis": "metered", "profile": "host",
        "harness": "pi", "subscription_payer": C.DEFAULT_SUBSCRIPTION_PAYER}


def test_the_registry_billing_block_reaches_a_host_run(ctl):
    (ctl.registry / "config.yaml").write_text(yaml.safe_dump({"billing": {
        "payer": "Home OAuth", "basis": "subscription",
        "subscription_payer": "Max plan"}}))
    res = ctl.run("start", "--prd", str(ctl.prd), "--run-id", "b-reg")
    assert res.returncode == 0, res.stderr
    assert _job_billing(ctl, "b-reg") == {
        "payer": "Home OAuth", "basis": "subscription", "profile": "host",
        "harness": "pi", "subscription_payer": "Max plan"}


def test_a_bad_registry_billing_block_exits_2(ctl):
    (ctl.registry / "config.yaml").write_text(yaml.safe_dump(
        {"billing": {"basis": "prepaid"}}))
    res = ctl.run("start", "--prd", str(ctl.prd), "--run-id", "b-badreg")
    assert res.returncode == 2
    assert "registry config.yaml: billing basis" in res.stderr


def test_the_billing_flags_override_the_profile(ctl):
    ctl.write_profile("gw", {"model": "gw/m", "env": {},
                             "billing": {"payer": GW_PAYER, "basis": "chargeback"}})
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "gw", "--run-id", "b-flag",
                  "--billing-payer", "Team budget", "--billing-basis", "metered")
    assert res.returncode == 0, res.stderr
    b = _job_billing(ctl, "b-flag")
    assert (b["payer"], b["basis"], b["profile"]) == ("Team budget", "metered", "gw")


def test_a_claude_code_profile_refuses_a_metered_basis_flag(ctl):
    ctl.write_profile("sub", {"model": "anthropic/claude-sonnet-5",
                              "harness": "claude-code", "env": {}})
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "sub", "--run-id", "b-cc",
                  "--billing-basis", "metered")
    assert res.returncode == 2
    assert "contradicts harness 'claude-code'" in res.stderr


def test_a_blank_billing_payer_flag_exits_2(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--run-id", "b-blank",
                  "--billing-payer", "  ")
    assert res.returncode == 2


def test_a_billing_payer_flag_with_a_newline_exits_2(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--run-id", "b-nl",
                  "--billing-payer", "x\nspend:     $999.00 (metered)")
    assert res.returncode == 2
    assert "control characters" in res.stderr
    assert not (ctl.config_dir("b-nl") / "job.yaml").exists()


def test_llm_none_writes_no_billing_and_refuses_the_flags(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none", "--run-id", "b-none")
    assert res.returncode == 0, res.stderr
    text = (ctl.config_dir("b-none") / "job.yaml").read_text()
    assert "billing" not in text
    assert _job_billing(ctl, "b-none") == "ABSENT"
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "b-none2", "--billing-payer", "x")
    assert res.returncode == 2
    assert "--billing-payer/--billing-basis need --llm <profile> or host" in res.stderr


def test_llm_show_prints_the_billing_line(ctl):
    ctl.write_profile("gw", {"model": "gw/m", "env": {},
                             "billing": {"payer": GW_PAYER, "basis": "chargeback"}})
    res = ctl.run("llm", "show", "gw")
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    i = next(i for i, ln in enumerate(lines) if ln.startswith("price_strategy:"))
    assert lines[i + 1] == f"billing: {GW_PAYER} (chargeback)"
    doc = json.loads(ctl.run("--json", "llm", "show", "gw").stdout)
    assert doc["billing"] == {"payer": GW_PAYER, "basis": "chargeback"}


# ------------------------------------------------------- 4. state.stamp_billing


def test_a_pi_subscription_iteration_relocates_its_money():
    before = dict(PI_QUOTED_DERIVED)
    u = state.stamp_billing(dict(PI_QUOTED_DERIVED), SUB_PAYER, "subscription")
    assert (u["payer"], u["basis"]) == (SUB_PAYER, "subscription")
    assert u["costUSD"] == 0.0 and isinstance(u["costUSD"], float)
    assert u["costFree"] is True
    assert u["costBasis"] == "subscription"
    assert u["listEquivalentQuotedUSD"] == before["costUSD"]
    assert u["listEquivalentDerivedUSD"] == before["costDerivedUSD"]
    assert u["listEquivalentUSD"] == pytest.approx(1.0)
    assert u["listEquivalentPriced"] is True
    assert u["listEquivalentDerived"] is True
    for gone in ("costPriced", "costDerived", "costDerivedUSD"):
        assert gone not in u
    assert "listEquivalentUnpricedTokens" not in u
    # the counters never move
    for k in ("input", "output", "cacheRead", "totalTokens"):
        assert u[k] == before[k]
    assert state.format_list_equivalent(u) == "~$1.0000"


def test_an_unpriceable_subscription_iteration_is_never_a_silent_zero(tmp_path):
    u = state.stamp_billing(dict(PI_UNPRICEABLE), SUB_PAYER, "subscription")
    assert u["costUSD"] == 0.0
    assert u["listEquivalentUnpricedTokens"] == 150
    assert "listEquivalentUSD" not in u
    assert u["listEquivalentPriced"] is False
    assert u["listEquivalentDerived"] is False
    assert state.format_list_equivalent(u) == "unavailable"
    assert state.format_cost(u, 4) == "$0.0000 (list-equivalent unavailable)"

    # the `ralphctl logs` footer
    import contextlib
    import io
    from ralphd.cli import log_render
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        log_render._render_boundary({"event": "end", "number": 1, "usage": u}, False)
    assert "cost=$0.0 (list-equivalent unavailable)" in buf.getvalue()

    # `ralphctl iteration` / the hub iteration table
    root = tmp_path / "run"
    itdir = root / "iterations" / "0001"
    itdir.mkdir(parents=True)
    (itdir / "meta.json").write_text(json.dumps(
        {"number": 1, "phase": "worker", "assignedTaskId": "001", "approach": 1,
         "usage": u}))
    detail = state.iteration_detail(root, 1)
    assert detail["costDisplay"] == "$0.0000 (list-equivalent unavailable)"

    # the hub tasks table
    totals = state.task_totals([detail])
    assert totals["1:001"]["costDisplay"].endswith(state.COST_LIST_UNAVAILABLE_SUFFIX)


def test_a_zero_quote_iteration_is_relocated_and_no_longer_an_anomaly():
    assert state.is_zero_quote(PI_ZERO_QUOTE)
    u = state.stamp_billing(dict(PI_ZERO_QUOTE), SUB_PAYER, "subscription")
    assert u["listEquivalentZeroQuoted"] is True
    assert "costZeroQuoted" not in u
    assert not state.is_zero_quote(u)
    assert u["listEquivalentDerivedUSD"] == 0.171586
    assert "listEquivalentQuotedUSD" not in u
    assert u["listEquivalentUSD"] == 0.171586
    assert "listEquivalentUnpricedTokens" not in u


def test_a_pi_free_stamp_declares_free_without_a_subscription_basis():
    u = state.stamp_billing(dict(PI_PRICED), "Lab box", "free")
    assert u["costFree"] is True and u["costUSD"] == 0.0
    assert "costBasis" not in u
    assert u["listEquivalentQuotedUSD"] == 1.23
    assert state.format_cost(u, 4) == "$0.0000"


@pytest.mark.parametrize("basis", ["metered", "chargeback"])
@pytest.mark.parametrize("usage", [PI_QUOTED_DERIVED, PI_UNPRICEABLE,
                                   PI_ZERO_QUOTE, PI_PRICED])
def test_a_metered_or_chargeback_stamp_only_labels(usage, basis):
    before = copy.deepcopy(usage)
    u = state.stamp_billing(copy.deepcopy(usage), GW_PAYER, basis)
    assert u == {**before, "payer": GW_PAYER, "basis": basis}


def test_an_empty_usage_stays_empty():
    assert state.stamp_billing({}, SUB_PAYER, "subscription") == {}
    assert state.stamp_billing({}, GW_PAYER, "metered") == {}


def test_a_claude_code_stamp_keeps_its_cost_fields():
    u = state.stamp_billing(dict(CC_SUB), SUB_PAYER, "subscription")
    assert {k: u.get(k) for k in COST_FIELDS} == {k: CC_SUB.get(k) for k in COST_FIELDS}
    assert "listEquivalentUnpricedTokens" not in u
    cut = state.stamp_billing(dict(CC_SUB_INTERRUPTED), SUB_PAYER, "subscription")
    assert cut["listEquivalentUnpricedTokens"] == 15
    assert {k: cut.get(k) for k in COST_FIELDS} == {
        k: CC_SUB_INTERRUPTED.get(k) for k in COST_FIELDS}


# ----------------------------------------------- 5. accumulate_payer_usage


def test_three_payers_give_three_rows_with_the_sum_invariant():
    total: dict = {}
    _fold(total, state.stamp_billing(dict(CC_SUB), SUB_PAYER, "subscription"),
          "anthropic/claude-sonnet-5")
    _fold(total, state.stamp_billing(dict(PI_QUOTED_DERIVED), GW_PAYER, "chargeback"),
          "aigw-openai/gpt-x")
    _fold(total, state.stamp_billing(dict(PI_PRICED), "Team card", "metered"),
          "openai/gpt-y")
    _fold(total, state.stamp_billing(dict(PI_ZERO_QUOTE), GW_PAYER, "chargeback"),
          "aigw-openai/gpt-x")
    assert set(total["byPayer"]) == {SUB_PAYER, GW_PAYER, "Team card"}
    _assert_sum_invariant(total)
    pv = state.payer_view(total)
    assert [r["key"] for r in pv["rows"]] == [GW_PAYER, "Team card", SUB_PAYER]
    assert pv["headline"] == (
        "spend:     ~$2.40 (chargeback, metered, estimated); "
        "subscription: $0.00 (would be $0.40 at list)")


def test_one_payer_under_two_bases_turns_mixed():
    total: dict = {}
    _fold(total, state.stamp_billing(dict(PI_PRICED), "Shared", "metered"))
    _fold(total, state.stamp_billing(dict(CC_SUB), "Shared", "subscription"))
    bucket = total["byPayer"]["Shared"]
    assert bucket["basis"] == state.BILLING_BASIS_MIXED
    assert bucket["byModel"][state.PAYER_DEFAULT_MODEL]["basis"] == "mixed"
    pv = state.payer_view(total)
    row = pv["rows"][0]
    assert row["listEquivalentDisplay"] == "$0.4000"
    # The headline agrees with the row: the mixed payer's list-equivalent is
    # in the $0 clause, and "mixed" is never printed as a basis. Its single
    # model row is mixed too, so no concrete basis can be named.
    assert pv["headline"] == ("spend:     $1.23; zero-priced: $0.00 "
                              "(would be $0.40 at list)")


def test_a_mixed_payer_headline_names_its_model_rows_bases():
    total: dict = {}
    _fold(total, state.stamp_billing(dict(PI_PRICED), "Shared", "metered"),
          "gw/big")
    _fold(total, state.stamp_billing(dict(CC_SUB), "Shared", "subscription"),
          "anthropic/claude-sonnet-5")
    _fold(total, state.stamp_billing(dict(PI_QUOTED_DERIVED), GW_PAYER, "chargeback"),
          "gw/big")
    assert total["byPayer"]["Shared"]["basis"] == state.BILLING_BASIS_MIXED
    pv = state.payer_view(total)
    assert pv["headline"] == (
        "spend:     ~$2.23 (chargeback, metered, estimated); "
        "subscription: $0.00 (would be $0.40 at list)")
    assert "mixed" not in pv["headline"]


def test_a_payer_mixing_two_zero_priced_bases_spends_nothing():
    total: dict = {}
    _fold(total, state.stamp_billing(dict(CC_SUB), "Home", "subscription"), "a/x")
    _fold(total, state.stamp_billing(dict(PI_QUOTED_DERIVED), "Home", "free"), "b/y")
    bucket = total["byPayer"]["Home"]
    assert (bucket["basis"], bucket.get("costFree")) == ("mixed", True)
    assert state.payer_view(total)["headline"] == (
        "spend:     $0.00; free/subscription: $0.00 "
        "(would be ~$1.40 at list)")


def test_by_model_keys_on_the_requested_ref():
    total: dict = {}
    for resolved in ("eu.anthropic.claude-opus-5", "anthropic.claude-opus-5-v1"):
        u = state.stamp_billing(dict(PI_PRICED, model=resolved), GW_PAYER, "chargeback")
        _fold(total, u, "amazon-bedrock/claude-opus-5")
    _fold(total, state.stamp_billing(dict(PI_PRICED), GW_PAYER, "chargeback"), None)
    _fold(total, state.stamp_billing(dict(PI_PRICED), GW_PAYER, "chargeback"), "  ")
    by_model = total["byPayer"][GW_PAYER]["byModel"]
    assert set(by_model) == {"amazon-bedrock/claude-opus-5", state.PAYER_DEFAULT_MODEL}
    assert by_model["amazon-bedrock/claude-opus-5"]["totalTokens"] == 300
    assert by_model[state.PAYER_DEFAULT_MODEL]["totalTokens"] == 300


# A pi route quoting real money and an AIGW route that returned no cost block
# at all (the host rate table derived it): the review finding's two usages.
PI_SUB_QUOTED = {"input": 100, "output": 50, "totalTokens": 150,
                 "costUSD": 0.25, "costPriced": True}
GW_DERIVED_ONLY = {"input": 22, "output": 4320, "cacheRead": 246596,
                   "totalTokens": 250938, "costPriced": False,
                   "costDerived": True, "costDerivedUSD": 0.171586}


def _breakdown(tmp_path: Path, usage: dict) -> dict:
    run = tmp_path / "zq-run"
    run.mkdir()
    (run / "status.json").write_text(json.dumps({"usage": usage}))
    return state.cost_breakdown(run)


def test_a_stamped_zero_priced_route_beside_a_derived_one_is_no_zero_quote(tmp_path):
    total: dict = {}
    _fold(total, state.stamp_billing(dict(PI_SUB_QUOTED), "Sub", "subscription"),
          "anthropic/x")
    _fold(total, state.stamp_billing(dict(GW_DERIVED_ONLY), "GW", "chargeback"),
          "aigw-openai/y")
    # The stamped $0.0 survives the fold without `costFree` (the derived
    # sibling is paid), yet it is the subscription's declared $0, not a quote.
    assert total["costUSD"] == 0.0 and "costFree" not in total
    assert state.is_zero_quote(total) is False
    bd = _breakdown(tmp_path, total)
    assert bd["notices"] == []
    assert not any(ln.startswith("!!") for ln in state.cost_breakdown_lines(bd))
    # The stamped $0 is not shown as a quoted part next to the derived money.
    assert bd["costDisplay"] == "~$0.1716 derived"
    assert state.payer_view(total)["headline"] == (
        "spend:     ~$0.17 (chargeback, estimated); "
        "subscription: $0.00 (would be $0.25 at list)")


def test_the_same_usages_unstamped_render_as_in_0_32_0(tmp_path):
    legacy: dict = {}
    LoopSupervisor._merge_usage(legacy, dict(PI_SUB_QUOTED))
    LoopSupervisor._merge_usage(legacy, dict(GW_DERIVED_ONLY))
    assert state.is_zero_quote(legacy) is False
    bd = _breakdown(tmp_path, legacy)
    assert bd["notices"] == []
    assert bd["costDisplay"] == "$0.2500 + ~$0.1716 derived"


def test_only_stamped_keys_explain_a_zero():
    # A pre-v0.6 zero quote is still the anomaly, and so is any unstamped
    # bucket: the exception reads keys only `stamp_billing` writes, so a run
    # recorded before 0.32.1 is classified exactly as before.
    assert state.is_zero_quote({"costUSD": 0, "totalTokens": 5}) is True
    assert state.is_zero_quote({"costUSD": 0.0, "totalTokens": 5,
                                "listEquivalentUSD": 0.4}) is True
    for key in state.COST_STAMPED_ZERO_PRICED_KEYS:
        assert state.is_zero_quote({"costUSD": 0.0, "totalTokens": 5,
                                    key: 3}) is False
        assert state.is_zero_quote({"costUSD": 0.0, "totalTokens": 5,
                                    key: 0}) is True


def test_an_unstamped_usage_leaves_the_total_untouched():
    total = {"totalTokens": 5}
    state.accumulate_payer_usage(total, dict(PI_PRICED), model="m")
    assert total == {"totalTokens": 5}


# -------------------------------------- 6. regression: the issue's own numbers


def _issue_usage() -> dict:
    sub = {"input": 400_000, "output": 1_000_000, "cacheRead": 56_000_000,
           "totalTokens": 57_400_000, "costUSD": 0.0, "costFree": True,
           "costBasis": "subscription", "listEquivalentUSD": 27.1744,
           "basis": "subscription"}
    sub["byModel"] = {"anthropic/claude-sonnet-5-5": {
        k: v for k, v in sub.items() if k != "byModel"}}
    gw = {"input": 300_000, "output": 500_000, "cacheRead": 48_000_000,
          "totalTokens": 48_800_000, "costUSD": 1.214, "costDerivedUSD": 57.4329,
          "costStatus": "derived", "basis": "chargeback"}
    astra = {"input": 200_000, "output": 300_000, "cacheRead": 41_200_000,
             "totalTokens": 41_700_000, "costUSD": 0, "costDerivedUSD": 57.4329,
             "costStatus": "derived", "basis": "chargeback"}
    sonnet = {"input": 100_000, "output": 200_000, "cacheRead": 6_800_000,
              "totalTokens": 7_100_000, "costUSD": 1.214, "basis": "chargeback"}
    gw["byModel"] = {"aigw-openai/openai.gpt-6-astra": astra,
                     "amazon-bedrock/eu.anthropic.claude-sonnet-5-5": sonnet}
    total = {"input": 700_000, "output": 1_500_000, "cacheRead": 104_000_000,
             "totalTokens": 106_200_000, "costUSD": 1.214,
             "costDerivedUSD": 57.4329, "costStatus": "derived",
             "costBasis": "mixed", "listEquivalentUSD": 27.1744,
             "byPhase": {"worker": {"totalTokens": 106_200_000, "costUSD": 1.214}},
             "byPayer": {SUB_PAYER: sub, GW_PAYER: gw}}
    return total


ISSUE_HEADLINE = ("spend:     ~$58.65 (chargeback, estimated); "
                  "subscription: $0.00 (would be $27.17 at list)")


def test_the_issues_numbers_give_its_headline_and_rows():
    pv = state.payer_view(_issue_usage())
    assert pv["headline"] == ISSUE_HEADLINE
    assert pv["columns"] == ["payer", "basis", "tokens", "cost", "list-equivalent"]
    rows = {r["key"]: r for r in pv["rows"]}
    gw, sub = rows[GW_PAYER], rows[SUB_PAYER]
    assert gw["costDisplay"] == "~$58.6469"
    assert gw["footnote"] == "$1.2140 quoted + ~$57.4329 derived"
    assert gw["listEquivalentDisplay"] is None
    assert sub["costDisplay"] == "$0.0000"
    assert sub["listEquivalentDisplay"] == "$27.1744"
    assert sub["footnote"] is None
    models = {m["key"]: m for m in gw["models"]}
    assert models["aigw-openai/openai.gpt-6-astra"]["costDisplay"] == "~$57.4329"
    assert models["amazon-bedrock/eu.anthropic.claude-sonnet-5-5"]["costDisplay"] == "$1.2140"
    assert [m["key"] for m in gw["models"]][0] == "aigw-openai/openai.gpt-6-astra"


def test_the_issues_numbers_in_cost_breakdown_lines(tmp_path):
    registry = _dead_run(tmp_path, _issue_usage())
    bd = state.cost_breakdown(registry / "runs" / "payer")
    lines = bd["summaryLines"]
    assert lines[0] == ISSUE_HEADLINE
    assert lines[1].startswith("cost:      ")
    assert lines[2] == "list-equiv: $27.1744" + state.COST_LIST_EQUIV_ZERO_PRICED_NOTE
    i = lines.index(state.PAYER_GROUP_LABEL)
    assert lines.index("by phase:") > i
    assert state.PAYER_ALTERNATIVES_NOTE in lines
    gw_line = next(ln for ln in lines if ln.startswith(f"  {GW_PAYER}"))
    tokens = next(r["tokensTotalDisplay"] for r in bd["payerView"]["rows"]
                  if r["key"] == GW_PAYER)
    assert gw_line.index(tokens) < gw_line.index("~$58.6469")
    assert gw_line.endswith("($1.2140 quoted + ~$57.4329 derived)")
    sub_line = next(ln for ln in lines if ln.startswith(f"  {SUB_PAYER}"))
    assert sub_line.endswith("list-equiv: $27.1744")
    assert any(ln.startswith("    aigw-openai/openai.gpt-6-astra") for ln in lines)


# ------------------------------------- 7. regression: replay of deck-phase4k


def _replay(extra: list[dict] = ()) -> dict:
    rows = json.loads(FIXTURE.read_text())["iterations"] + list(extra)
    total: dict = {}
    for r in rows:
        u = copy.deepcopy(r["usage"])
        if r["harness"] == "claude-code":
            state.stamp_billing(u, SUB_PAYER, "subscription")
        else:
            state.stamp_billing(u, GW_PAYER, "chargeback")
        _fold(total, u, r["model"])
    return total


def test_the_fixture_holds_only_usage_numbers():
    rows = json.loads(FIXTURE.read_text())["iterations"]
    assert len(rows) == 74
    for r in rows:
        assert set(r) == {"harness", "model", "usage"}
        for k, v in r["usage"].items():
            assert isinstance(v, (int, float, bool)) or k == "costBasis", k


def test_the_deck_phase4k_replay():
    total = _replay()
    assert set(total["byPayer"]) == {SUB_PAYER, GW_PAYER}
    _assert_sum_invariant(total)
    sub = total["byPayer"][SUB_PAYER]
    assert "listEquivalentUnpricedTokens" not in sub
    assert sub["costUSD"] == 0
    pv = state.payer_view(total)
    assert pv["headline"] is not None
    assert pv["headline"].startswith("spend:     ~$")
    assert "(chargeback, estimated); subscription: $0.00 (would be $" in pv["headline"]
    assert pv["notices"] == []


def test_an_interrupted_claude_code_iteration_reads_partial_at_list():
    total = _replay([{"harness": "claude-code", "model": "anthropic/claude-sonnet-5-5",
                      "usage": dict(CC_SUB_INTERRUPTED)}])
    _assert_sum_invariant(total)
    sub = total["byPayer"][SUB_PAYER]
    assert sub["listEquivalentUnpricedTokens"] == 15
    pv = state.payer_view(total)
    row = next(r for r in pv["rows"] if r["key"] == SUB_PAYER)
    expected = state.format_money(sub["listEquivalentUSD"], 4) + state.COST_PARTIAL_SUFFIX
    assert row["listEquivalentDisplay"] == expected
    assert "at list, rest unavailable" in pv["headline"]


# ------------------------------------------------------------ 8. legacy runs

PI_DERIVED = {"input": 100, "output": 50, "totalTokens": 150, "costUSD": 0,
              "costPriced": False, "costZeroQuoted": True, "costDerived": True,
              "costDerivedUSD": 0.5}


def _legacy_usage() -> dict:
    total, phases = {}, {}
    for ph, u in (("worker", PI_PRICED), ("verify", PI_DERIVED), ("review", CC_SUB)):
        LoopSupervisor._merge_usage(total, dict(u))
        phases[ph] = LoopSupervisor._merge_usage({}, dict(u))
    total["byPhase"] = phases
    return total


# Captured from the v0.32.0 tree (HEAD before #298) over `_legacy_usage()`.
LEGACY_SUMMARY = [
    "cost:      $1.2300 + ~$0.5000 derived",
    "list-equiv: $0.4000  (subscription list price, not billed)",
    "tokens:    315 total (in 210, out 105)",
    "!! provider quoted $0 for billable tokens; treated as an unpriced route "
    "(declare its rates, or declare it free, in `pricing:` to settle it)",
    "by phase:",
    "  worker  $1.2300           150 tokens",
    "  verify  ~$0.5000 derived  150 tokens",
    "  review  $0.0000            15 tokens  list-equiv: $0.4000",
    "legend:    a bare amount was quoted by the provider; ~ marks money derived "
    "from the host-side rate table; unavailable means tokens were billed that "
    "nothing priced",
]


def test_a_legacy_run_renders_as_before(tmp_path, capsys):
    usage = _legacy_usage()
    assert state.payer_view(usage) is None
    registry = _dead_run(tmp_path, usage, "legacy")
    bd = state.cost_breakdown(registry / "runs" / "legacy")
    assert "payerView" not in bd
    assert bd["summaryLines"] == LEGACY_SUMMARY

    from ralphd.cli.ui_server import _with_cost_display
    hub = _with_cost_display({"usage": usage})["usage"]
    assert "payerView" not in hub
    assert hub["listEquivalentDisplay"] == "$0.4000"
    assert hub["byPhase"]["review"]["listEquivalentDisplay"] == "$0.4000"

    text = "\n".join(run_record._usage_lines(usage))
    assert "byPayer" not in text
    assert "- listEquivalentUSD: 0.4" in text

    for u, want in ((PI_PRICED, "$1.2300"), (PI_DERIVED, "~$0.5000 derived"),
                    (CC_SUB, "$0.0000"), (usage, "$1.2300 + ~$0.5000 derived")):
        assert state.format_cost(u, 4) == want

    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    res = call_ralphctl_in_process(["status", "legacy"], env=env, capsys=capsys)
    assert res.returncode == 0, res.stderr
    out = res.stdout.splitlines()
    assert "list-equiv: $0.4000  (subscription list price, not billed)" in out
    assert not any(ln.startswith(("spend:", "!! by payer")) for ln in out)


def test_an_unattributed_run_gets_a_notice_not_a_headline():
    usage = _issue_usage()
    usage["totalTokens"] += 1_234_567
    pv = state.payer_view(usage)
    assert pv["headline"] is None
    assert pv["notices"] == [state.PAYER_UNATTRIBUTED_NOTICE.format(
        attributed=106_200_000, total=107_434_567)]
    assert "106,200,000 of 107,434,567" in pv["notices"][0]


def test_a_payer_row_without_tokens_does_not_crash_the_sort():
    usage = {"totalTokens": 150, "byPayer": {
        "a": {"costUSD": 0.1, "basis": "metered"},
        "b": dict(PI_PRICED, basis="metered")}}
    pv = state.payer_view(usage)
    assert [r["key"] for r in pv["rows"]] == ["b", "a"]


# ------------------------------------------------------ 9. engine end to end


def _make_sup(registry_root, run_id, **cfg_kw):
    root = registry_root / "runs" / run_id
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [
        {"id": "001", "title": "one", "status": "pending", "successCriteria": "c"}]})
    kw = {"iterations": 5, "max_approaches": 1, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          "route_task_selection": False, **cfg_kw}
    cfg = JobConfig(run_id=run_id, **kw)
    return loop_module.LoopSupervisor(cfg, run, root), run


class _ScriptedAgent:
    def __init__(self, usages: list[dict]):
        self._usages = list(usages)

    def interrupt(self):
        return False

    async def run(self, prompt, transcript, **kw):
        r = IterationResult(exit_code=0)
        r.final_text = json.dumps({"task_id": "001", "verdict": "complete"})
        r.duration_s = 0.05
        r.usage = dict(self._usages.pop(0))
        return r


def _drive(tmp_path, billing):
    sup, run = _make_sup(tmp_path / "registry", "e2e", billing=billing)
    sup.runner = _ScriptedAgent([PI_QUOTED_DERIVED, PI_PRICED, PI_PRICED])
    asyncio.run(sup._step_worker())
    asyncio.run(sup._ask_router("pick one"))
    asyncio.run(sup._ask_contract_fix("worker", ["bad payload"]))
    metas = sorted(run.root.glob("iterations/*/meta.json"))
    assert metas
    return json.loads(metas[0].read_text()), run.read_status()["usage"]


def test_the_engine_stamps_iterations_router_and_contract(tmp_path):
    meta, usage = _drive(tmp_path, {"payer": GW_PAYER, "basis": "chargeback",
                                    "profile": "gw", "harness": "pi"})
    assert meta["usage"]["payer"] == GW_PAYER
    assert meta["usage"]["basis"] == "chargeback"
    assert set(usage["byPayer"]) == {GW_PAYER}
    assert usage["byPayer"][GW_PAYER]["totalTokens"] == usage["totalTokens"] == 1300
    assert set(usage["byPhase"]) >= {"router", "contract"}
    _assert_sum_invariant(usage)
    assert state.payer_view(usage)["headline"] == (
        "spend:     ~$3.46 (chargeback, estimated)")


def test_an_engine_run_without_billing_is_never_stamped(tmp_path):
    meta, usage = _drive(tmp_path, None)
    assert "payer" not in meta["usage"]
    assert "basis" not in meta["usage"]
    assert "byPayer" not in usage


# ----------------------------------------------- 10. hub, record and CLI


def test_the_hub_view_carries_the_payer_view():
    from ralphd.cli.ui_server import _with_cost_display
    out = _with_cost_display({"usage": _issue_usage()})["usage"]
    assert out["payerView"]["headline"] == ISSUE_HEADLINE
    assert out["byPayer"] == _issue_usage()["byPayer"]


def test_render_usage_places_server_strings_as_text_nodes():
    src = APP_JS.read_text()
    region = src[src.index("function renderUsage"):src.index("function statCard")]
    assert region.count("innerHTML") == 1
    # the two #298 blocks: the headline before the grid, the table after it
    payer = (region[region.index("usage.payerView"):region.index("const grid")]
             + region[region.index("if (pv && Array.isArray(pv.rows))"):
                      region.index('["by phase", "byPhase"]')])
    assert "toFixed" not in payer
    assert "innerHTML" not in payer
    assert "pv.columns" in payer


def test_the_run_record_has_a_by_payer_section_only_when_stamped():
    stamped = "\n".join(run_record._usage_lines(_issue_usage()))
    assert "- byPayer:" in stamped
    assert f"  - headline: {ISSUE_HEADLINE}" in stamped
    assert (f"  - `{GW_PAYER}` (chargeback): 48,800,000 tokens, ~$58.6469 "
            "($1.2140 quoted + ~$57.4329 derived)") in stamped
    assert f"  - `{SUB_PAYER}` (subscription): 57,400,000 tokens, $0.0000, list-equiv $27.1744" in stamped
    assert "    - `aigw-openai/openai.gpt-6-astra` (chargeback)" in stamped
    assert "- byPayer:" not in "\n".join(run_record._usage_lines(_legacy_usage()))


def test_ralphctl_status_prints_the_spend_line_for_a_stamped_run(tmp_path, capsys):
    registry = _dead_run(tmp_path, _issue_usage())
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    res = call_ralphctl_in_process(["status", "payer"], env=env, capsys=capsys)
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    i = lines.index("list-equiv: $27.1744" + state.COST_LIST_EQUIV_ZERO_PRICED_NOTE)
    assert lines[i + 1] == ISSUE_HEADLINE
    cost = call_ralphctl_in_process(["cost", "payer"], env=env, capsys=capsys)
    assert cost.returncode == 0, cost.stderr
    out = cost.stdout.splitlines()
    assert out.index(ISSUE_HEADLINE) + 1 == next(
        i for i, ln in enumerate(out) if ln.startswith("cost:      "))
    assert state.PAYER_GROUP_LABEL in out
