"""Task 010 (#79, PRD requirement O): the resolved hat -> model mapping is
shown on all three surfaces that render a run's status -- the hub's
run-header card (`ui_server.run_detail`), `ralphctl status`'s plain text,
and `ralphctl status --json` -- and all three agree because all three call
the ONE shared formatter (`engine.state.format_provisioning_lines`) over
the ONE shared resolver (`JobConfig.provisioning_table`), never a second
implementation.

Unlike the per-iteration `model:`/`modelRaw` fields (which only exist once
some iteration has actually dispatched), this mapping comes from the run's
own job.yaml alone -- proven here on a run whose status.json records zero
iterations and whose API is unreachable (no engine container was ever
started for it, matching `#79`'s own repro: "no way to see the mapping
before iteration 1").
"""

from __future__ import annotations

import json
from pathlib import Path

from test_cli_docker import Ctl
from test_cli_ui import _write_dead_run

from ralphd.cli.ui_server import run_detail
from ralphd.engine.config import JobConfig, MODEL_HATS
from ralphd.engine.state import format_provisioning_lines

# The PRD's worked configuration, and #79's own reproducer: an Opus planner,
# a Sonnet worker, and a gpt-5.6-terra cross-family verifier.
PLANNER = "amazon-bedrock/eu.anthropic.claude-opus-5"
WORKER = "amazon-bedrock/eu.anthropic.claude-sonnet-5"
VERIFIER = "aigw-openai/openai.gpt-5.6-terra"


def _write_job_yaml(config_dir: Path, models: dict) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "job.yaml").write_text("models: " + json.dumps(models) + "\n")


def test_provisioning_table_resolves_the_prd_worked_configuration():
    """Unit: `JobConfig.provisioning_table` resolves the exact PLANNER/
    WORKER/VERIFIER refs, one row per `MODEL_HATS` entry in that tuple's
    order -- the resolved mapping, not the raw `models:` knob."""
    cfg = JobConfig(model_overrides={
        "planning": PLANNER, "worker": WORKER, "verify": VERIFIER})
    table = cfg.provisioning_table()
    assert [row["hat"] for row in table] == list(MODEL_HATS)
    by_hat = {row["hat"]: row["model"] for row in table}
    assert by_hat["planning"] == PLANNER
    assert by_hat["worker"] == WORKER
    assert by_hat["verify"] == VERIFIER
    # Unconfigured hats resolve to pi's own default (None), never a
    # made-up ref, and every row is a plain (model, thinking, effort)
    # dict -- never the retired `strategy`/`fastModel` vocabulary.
    assert by_hat["review"] is None
    assert by_hat["reflect"] is None
    assert by_hat["router"] is None
    assert all(set(row) == {"hat", "model", "thinking", "effort", "harness"} for row in table)


def test_format_provisioning_lines_has_no_strategy_wording_and_no_none():
    table = JobConfig(model_overrides={"planning": PLANNER}).provisioning_table()
    lines = format_provisioning_lines(table)
    assert len(lines) == len(MODEL_HATS)
    joined = "\n".join(lines)
    assert PLANNER in joined
    assert "None" not in joined
    assert "strategy" not in joined.lower()
    assert "balanced" not in joined.lower()
    assert "pi default" in joined  # unconfigured hats, worded honestly


def test_provisioning_table_collapses_a_one_model_configuration(tmp_path=None):
    """Task 006 (#68): with no `model_tiers` table at all, every hat's
    three tiers resolve to the SAME `bundle_for` answer -- `default_host_
    map`'s own no-op-widening guarantee (task 002) -- so `provisioning_
    table` collapses every row back down to task 010's original
    single-rung shape (`{"hat", "model", "thinking", "effort", "harness"}`), never a
    `"rungs"` list, for a one-model configuration."""
    cfg = JobConfig(model_overrides={
        "planning": PLANNER, "worker": WORKER, "verify": VERIFIER})
    table = cfg.provisioning_table()
    assert all("rungs" not in row for row in table)
    assert all(set(row) == {"hat", "model", "thinking", "effort", "harness"} for row in table)
    lines = format_provisioning_lines(table)
    assert any(line.startswith("worker ") for line in lines)
    assert not any("." in line.split()[0] for line in lines)


def _write_job_yaml_with_worker_hard_tier(config_dir: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "job.yaml").write_text(
        "models: " + json.dumps({"worker": WORKER}) + "\n"
        "model_tiers: " + json.dumps({"worker": {"hard": VERIFIER}}) + "\n")


def test_the_two_surfaces_agree_on_a_distinct_worker_hard_rung(tmp_path, capsys):
    """Task 006 (#68): a job.yaml that configures a distinct `worker.hard`
    rung (different from `worker`'s other two tiers) makes BOTH the hub's
    `run_detail` payload and `ralphctl status`'s plain text carry that
    third rung, and both derive it from the identical
    `provisioning_table`/`format_provisioning_lines` pair -- no second
    formatter anywhere."""
    ctl = Ctl(tmp_path, capsys)
    run_id = "hat-status-tiers"
    _write_dead_run(ctl.registry, run_id, iterationsUsed=0, iterationsBudget=25)
    cdir = ctl.registry / "configs" / run_id
    _write_job_yaml_with_worker_hard_tier(cdir)

    # Surface 1: the hub's run-detail payload.
    detail = run_detail(ctl.registry, run_id)
    assert detail is not None
    hub_table = detail["status"]["provisioning"]
    hub_lines = detail["status"]["provisioningLines"]
    by_hat = {row["hat"]: row for row in hub_table}
    worker_row = by_hat["worker"]
    assert "rungs" in worker_row
    by_tier = {rung["tier"]: rung["model"] for rung in worker_row["rungs"]}
    assert by_tier["trivial"] == WORKER
    assert by_tier["normal"] == WORKER
    assert by_tier["hard"] == VERIFIER
    assert "worker.hard" in "\n".join(hub_lines)

    # Surface 2: `ralphctl status` plain text -- the hub's exact strings.
    text_res = ctl.run("status", run_id)
    assert text_res.returncode == 0, text_res.stderr
    for line in hub_lines:
        assert line in text_res.stdout

    # Surface 3: `ralphctl status --json` -- the hub's exact structured
    # data, unchanged -- proving all three come from the SAME function.
    json_res = ctl.run("--json", "status", run_id)
    assert json_res.returncode == 0, json_res.stderr
    doc = json.loads(json_res.stdout)
    assert doc["provisioning"] == hub_table
    assert doc["provisioningLines"] == hub_lines


def test_the_three_surfaces_agree_before_any_iteration_has_run(tmp_path, capsys):
    ctl = Ctl(tmp_path, capsys)
    run_id = "hat-status-surfaces"
    _write_dead_run(ctl.registry, run_id, iterationsUsed=0, iterationsBudget=25)
    cdir = ctl.registry / "configs" / run_id
    _write_job_yaml(cdir, {"planning": PLANNER, "worker": WORKER,
                           "verify": VERIFIER})

    # Surface 1: the hub's run-detail payload (no live container -- proxy
    # falls back, exactly like `ralphctl status` below).
    detail = run_detail(ctl.registry, run_id)
    assert detail is not None
    hub_table = detail["status"]["provisioning"]
    hub_lines = detail["status"]["provisioningLines"]
    assert hub_lines  # present -- not gated on any iteration having run
    by_hat = {row["hat"]: row["model"] for row in hub_table}
    assert by_hat["planning"] == PLANNER
    assert by_hat["worker"] == WORKER
    assert by_hat["verify"] == VERIFIER

    # Surface 2: `ralphctl status` plain text -- the hub's exact strings,
    # unchanged.
    text_res = ctl.run("status", run_id)
    assert text_res.returncode == 0, text_res.stderr
    assert "provisioning:" in text_res.stdout
    for line in hub_lines:
        assert line in text_res.stdout

    # Surface 3: `ralphctl status --json` -- the hub's exact structured
    # data, unchanged.
    json_res = ctl.run("--json", "status", run_id)
    assert json_res.returncode == 0, json_res.stderr
    doc = json.loads(json_res.stdout)
    assert doc["provisioning"] == hub_table
    assert doc["provisioningLines"] == hub_lines
    # Present before any iteration has run.
    assert doc.get("iterationsUsed") == 0


def test_provisioning_lines_end_with_each_hats_harness():
    """Every hat line, collapsed or per-rung, names the hat's harness."""
    from ralphd.engine.state import format_provisioning_lines
    cfg = JobConfig(
        model_overrides={"worker": WORKER},
        harnesses={"worker": "claude-code"},
        model_tiers={"verify": {"hard": VERIFIER}})
    lines = format_provisioning_lines(cfg.provisioning_table())
    assert any(l.startswith("worker") and l.endswith("[claude-code]") for l in lines)
    assert any(l.startswith("planning") and l.endswith("[pi]") for l in lines)
    assert all(l.endswith("]") for l in lines)
    assert any(l.startswith("verify.hard") and l.endswith("[pi]") for l in lines)


def test_the_hub_provisioning_lines_show_each_hats_harness(tmp_path, capsys):
    """v0.31 (#285): a run whose job.yaml puts hats on claude-code shows
    `[claude-code]` / `[pi]` per hat -- tier rungs included -- in the hub's
    own `provisioningLines`, the strings app.js renders unchanged."""
    ctl = Ctl(tmp_path, capsys)
    run_id = "hat-status-harness"
    _write_dead_run(ctl.registry, run_id, iterationsUsed=0, iterationsBudget=25)
    cdir = ctl.registry / "configs" / run_id
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "job.yaml").write_text(
        "models: " + json.dumps({"worker": WORKER}) + "\n"
        "harnesses: " + json.dumps({"worker": "claude-code"}) + "\n"
        "model_tiers: " + json.dumps({"worker": {"hard": VERIFIER}}) + "\n")
    detail = run_detail(ctl.registry, run_id)
    lines = detail["status"]["provisioningLines"]
    worker = [l for l in lines if l.startswith("worker.")]
    assert len(worker) == 3 and all(l.endswith("[claude-code]") for l in worker)
    assert any(l.startswith("planning") and l.endswith("[pi]") for l in lines)
