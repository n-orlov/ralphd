"""v0.39 (#321 D): `ralphctl budget <run> --spend / --spend-approach` and the
`resume` rule for a run `exhausted` by spend.

The CLI half drives the real `ralphctl` through tests/test_cli_docker.py's
`Ctl` (stub docker, temp registry); the live half drives the real
`PATCH /config/budget` route against a real supervisor, and the resume half
runs the engine twice over one run dir with a scripted runner (fake hats,
injected costs), so no test spends a dollar or waits out a clock."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import yaml
from test_cli_budget import _StubEngine
from test_cli_docker import Ctl
from test_spend_enforcement import _caps, _plan, _work

from ralphd.engine import spend_caps
from ralphd.engine.api import create_app
from ralphd.engine.config import JobConfig
from ralphd.engine.state import RunDir
from tests.conftest import make_supervisor, scripted_supervisor

pytestmark = [pytest.mark.smoke, pytest.mark.fast]

PRICED = "prov/priced-model"
PRICING = "pricing:\n  models:\n    \"prov/*\": {input: 1.0, output: 2.0}\n"


@pytest.fixture
def ctl(tmp_path, capsys):
    c = Ctl(tmp_path, capsys)
    (c.registry / "config.yaml").write_text(PRICING)
    return c


def _job_path(ctl: Ctl, run_id: str):
    matches = list(ctl.tmp.glob(f"**/configs/{run_id}/job.yaml"))
    assert len(matches) == 1, matches
    return matches[0]


def _job(ctl: Ctl, run_id: str) -> dict:
    return yaml.safe_load(_job_path(ctl, run_id).read_text())


def _docker_runs(ctl: Ctl) -> list[list[str]]:
    return [a for a in ctl.recorded() if a[:2] == ["run", "-d"]]


def _start(ctl: Ctl, run_id: str, *extra: str, model: str = PRICED):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", run_id, "--model", model, *extra)
    assert res.returncode == 0, res.stderr
    return res


def _entry(usd: float, source: str) -> dict:
    return {"usd": usd, "source": source}


# -- the spec grammar, at each level -----------------------------------------

@pytest.mark.parametrize("level,flag", [("overall", "--spend"),
                                        ("per_approach", "--spend-approach")])
@pytest.mark.parametrize("spec,expected", [
    ("review=+5", _entry(25.0, "budget")),      # top-up
    ("review=12.5", _entry(12.5, "budget")),    # set
    ("review=0", None),                          # remove
    ("review=none", None),                       # remove
])
def test_each_form_at_each_level_is_persisted_in_job_yaml_with_source_budget(
        ctl, level, flag, spec, expected):
    cap_flag = "--spend-cap" if level == "overall" else "--spend-cap-approach"
    _start(ctl, "bud-forms", cap_flag, "review=20", cap_flag, "worker=7")
    res = ctl.run("budget", "bud-forms", flag, spec)
    assert res.returncode == 0, res.stderr
    caps = _job(ctl, "bud-forms")["spend_caps"][level]
    if expected is None:
        assert "review" not in caps
    else:
        assert caps["review"] == expected
    assert caps["worker"] == _entry(7.0, "flag")     # an untouched cap keeps its source
    assert "review" in res.stdout


def test_removing_the_last_cap_leaves_no_spend_caps_key(ctl):
    _start(ctl, "bud-last", "--spend-cap", "review=20")
    assert ctl.run("budget", "bud-last", "--spend", "review=none").returncode == 0
    assert "spend_caps" not in _job(ctl, "bud-last")


def test_both_flags_are_repeatable_and_combine_in_one_call(ctl):
    _start(ctl, "bud-many")
    res = ctl.run("budget", "bud-many", "--spend", "review=20", "--spend", "total=90",
                  "--spend-approach", "worker=5", "--spend-approach", "review=4")
    assert res.returncode == 0, res.stderr
    assert _job(ctl, "bud-many")["spend_caps"] == {
        "overall": {"review": _entry(20.0, "budget"), "total": _entry(90.0, "budget")},
        "per_approach": {"worker": _entry(5.0, "budget"), "review": _entry(4.0, "budget")}}


def test_a_top_up_of_a_cap_that_is_not_there_is_refused(ctl):
    _start(ctl, "bud-nocap")
    res = ctl.run("budget", "bud-nocap", "--spend", "review=+5")
    assert res.returncode == 2
    assert "no overall cap to top up" in res.stderr
    assert "spend_caps" not in _job(ctl, "bud-nocap")


@pytest.mark.parametrize("spec", ["review", "review=", "review=abc", "review=-3",
                                  "review=+0", "bogus=5", "review=nan"])
def test_a_malformed_spec_is_refused_before_anything_is_written(ctl, spec):
    _start(ctl, "bud-bad", "--spend-cap", "review=20")
    before = _job_path(ctl, "bud-bad").read_text()
    res = ctl.run("budget", "bud-bad", "--spend", spec)
    assert res.returncode == 2, res.stderr
    assert _job_path(ctl, "bud-bad").read_text() == before


# -- combined with the existing levers, against a live engine ----------------

def test_spend_combines_with_the_other_levers_in_one_request_and_is_recorded(ctl):
    engine = _StubEngine(200, {
        "iterations": 40, "previous": 25, "iterationsUsed": 3,
        "spendCaps": {"overall": {"review": _entry(25.0, "budget")}},
        "previousSpendCaps": {"overall": {"review": _entry(20.0, "flag")}}})
    try:
        _start(ctl, "bud-live", "--spend-cap", "review=20")
        _seed_engine_run_port(ctl, "bud-live", engine)
        res = ctl.run("budget", "bud-live", "+15", "--spend", "review=+5")
        assert res.returncode == 0, res.stderr
        [req] = engine.requests
        assert (req["method"], req["path"]) == ("PATCH", "/config/budget")
        assert req["body"] == {"iterations": "+15",
                               "spendCaps": {"overall": ["review=+5"]}}
        assert "spend cap review: $20 -> $25" in res.stdout
        assert _job(ctl, "bud-live")["spend_caps"] == {
            "overall": {"review": _entry(25.0, "budget")}}
    finally:
        engine.close()


def _seed_engine_run_port(ctl: Ctl, run_id: str, engine: _StubEngine) -> None:
    rdir = next(ctl.tmp.glob(f"**/runs/{run_id}"))
    meta = json.loads((rdir / "host.json").read_text())
    meta["port"] = engine.port
    meta["apiUrl"] = f"http://127.0.0.1:{engine.port}"
    (rdir / "host.json").write_text(json.dumps(meta))


def test_a_live_container_gets_the_spend_flags_alone_through_the_api(ctl):
    engine = _StubEngine(200, {
        "spendCaps": {"per_approach": {"total": _entry(9.0, "budget")}},
        "previousSpendCaps": {}})
    try:
        _start(ctl, "bud-live2")
        _seed_engine_run_port(ctl, "bud-live2", engine)
        res = ctl.run("budget", "bud-live2", "--spend-approach", "total=9", env={
            "STUB_DOCKER_CONTAINERS": "ralphd-bud-live2",
            "STUB_DOCKER_RUNNING": "ralphd-bud-live2"})
        assert res.returncode == 0, res.stderr
        assert engine.requests[0]["body"] == {
            "spendCaps": {"per_approach": ["total=9"]}}
        assert _job(ctl, "bud-live2")["spend_caps"] == {
            "per_approach": {"total": _entry(9.0, "budget")}}
    finally:
        engine.close()


# -- A.4: the unpriceable refusal ---------------------------------------------

@pytest.mark.parametrize("flag", ["--spend", "--spend-approach"])
def test_a_cap_on_an_unpriceable_hat_is_refused_at_budget(ctl, flag):
    _start(ctl, "bud-unpriced", "--model-for", "review=other/unknown",
           "--spend-cap", "worker=5")
    before = _job_path(ctl, "bud-unpriced").read_text()
    res = ctl.run("budget", "bud-unpriced", flag, "review=5")
    assert res.returncode == 2, res.stderr
    assert "review" in res.stderr and "other/unknown" in res.stderr
    assert "no price in the pricing chain" in res.stderr
    assert _job_path(ctl, "bud-unpriced").read_text() == before


def test_a_total_cap_at_budget_names_every_unpriceable_hat(ctl):
    _start(ctl, "bud-unpriced-total", "--model-for", "review=other/unknown-r",
           "--model-for", "verify=other/unknown-v")
    res = ctl.run("budget", "bud-unpriced-total", "--spend", "total=50")
    assert res.returncode == 2
    assert "other/unknown-r" in res.stderr and "other/unknown-v" in res.stderr


def test_removing_a_cap_is_never_refused_as_unpriceable(ctl):
    _start(ctl, "bud-unpriced-rm", "--model", "other/unknown", "--model-for",
           f"review={PRICED}", "--spend-cap", "review=5")
    (ctl.registry / "config.yaml").write_text("")        # nothing is priced now
    cfg = _job_path(ctl, "bud-unpriced-rm")
    job = yaml.safe_load(cfg.read_text())
    job["pricing"] = None
    job["models"] = {"review": "other/unknown"}
    cfg.write_text("".join(f"{k}: {json.dumps(v)}\n" for k, v in job.items()
                           if v is not None))
    assert ctl.run("budget", "bud-unpriced-rm", "--spend", "review=5").returncode == 2
    assert ctl.run("budget", "bud-unpriced-rm", "--spend", "review=none").returncode == 0


# -- live: the change applies at the next boundary and survives a resume ------

def _live_supervisor(tmp_path, caps, **status):
    return make_supervisor(tmp_path, **{
        "run_id": "unit", "cfg_defaults": False, "iterations": 25,
        "spend_caps": caps,
        "status": {"state": "running", "approach": 1, **status}})


def _client(sup) -> httpx.AsyncClient:
    app = create_app(sup.cfg, sup.run, sup)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://engine")


@pytest.mark.asyncio
@pytest.mark.parametrize("body,expected", [
    ({"overall": ["worker=+4"]}, {"overall": {"worker": _entry(5.0, "budget")}}),
    ({"overall": ["worker=9"]}, {"overall": {"worker": _entry(9.0, "budget")}}),
    ({"overall": ["worker=none"]}, {}),
    ({"overall": ["worker=0"]}, {}),
])
async def test_a_live_run_applies_each_form_at_its_next_boundary(tmp_path, body, expected):
    sup = _live_supervisor(tmp_path, _caps(overall={"worker": 1.0}))
    sup.run.record_spend(1, "worker", {"costUSD": 2.0, "totalTokens": 1}, 1)
    # the worker is spent: the boundary refuses it
    with pytest.raises(BaseException) as spent:
        sup._enforce_spend_caps("worker")
    assert type(spent.value).__name__ == "_SpendCapSpent"
    async with _client(sup) as client:
        resp = await client.patch("/config/budget", json={"spendCaps": body})
    assert resp.status_code == 200, resp.text
    assert resp.json()["spendCaps"] == expected
    assert sup.cfg.spend_caps == expected
    if expected.get("overall", {}).get("worker", {}).get("usd", 0) < 3.0:
        # $2 spent: a $5 / $9 cap or no cap lets the worker dispatch again
        sup._enforce_spend_caps("worker")
    events = [json.loads(x) for x in (sup.run.root / "events.jsonl").read_text().splitlines()]
    [changed] = [e for e in events if e["type"] == "budget_changed"]
    assert changed["field"] == "spendCaps" and changed["source"] == "api"


@pytest.mark.asyncio
async def test_a_live_per_approach_top_up_applies_to_the_current_approach(tmp_path):
    sup = _live_supervisor(tmp_path, _caps(per_approach={"total": 1.0}), approach=2)
    sup.run.record_spend(2, "worker", {"costUSD": 1.5, "totalTokens": 1}, 1)
    async with _client(sup) as client:
        resp = await client.patch("/config/budget", json={
            "spendCaps": {"per_approach": ["total=+5"]}})
    assert resp.status_code == 200, resp.text
    assert sup.cfg.spend_caps == {"per_approach": {"total": _entry(6.0, "budget")}}
    sup._enforce_spend_caps("worker")        # no longer spent


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"overall": ["review=+5"]},           # nothing to top up
    {"overall": ["bogus=5"]},             # unknown key
    {"overall": ["worker=-1"]},           # not a positive figure
    {"sideways": ["worker=5"]},           # unknown level
    {"overall": "worker=5"},              # not a list
])
async def test_the_engine_refuses_a_bad_spend_request_and_changes_nothing(tmp_path, body):
    caps = _caps(overall={"worker": 1.0})
    sup = _live_supervisor(tmp_path, caps)
    async with _client(sup) as client:
        resp = await client.patch("/config/budget", json={"spendCaps": body})
    assert resp.status_code == 422, resp.text
    assert sup.cfg.spend_caps == caps


def test_a_budget_change_is_still_in_force_after_resume(ctl):
    """The change lands in job.yaml with source `budget`, which is what the
    engine loads at the resumed container's start."""
    _start(ctl, "bud-persist", "--spend-cap", "worker=1")
    assert ctl.run("budget", "bud-persist", "--spend", "worker=+4",
                   "--spend-approach", "total=3").returncode == 0
    res = ctl.run("resume", "bud-persist")
    assert res.returncode == 0, res.stderr
    cfg = JobConfig.load(_job_path(ctl, "bud-persist"))
    assert cfg.spend_caps == {"overall": {"worker": _entry(5.0, "budget")},
                              "per_approach": {"total": _entry(3.0, "budget")}}


# -- resume of a spend-exhausted run -------------------------------------------

def _exhaust(ctl: Ctl, run_id: str, reason: str, spend: dict, approach: int = 1):
    rdir = next(ctl.tmp.glob(f"**/runs/{run_id}"))
    (rdir / "status.json").write_text(json.dumps({
        "state": "exhausted", "exhaustedBudget": reason, "approach": approach,
        "spend": spend}))


def test_resume_of_a_spend_exhausted_run_names_each_still_spent_cap(ctl):
    _start(ctl, "bud-resume", "--spend-cap", "worker=1.5", "--spend-cap", "total=3",
           "--spend-cap-approach", "review=0.75", "--spend-cap", "verify=50")
    _exhaust(ctl, "bud-resume", "spend:worker", {
        "overall": {"worker": 1.8, "verify": 2.0, "total": 3.4},
        "byApproach": {"1": {"review": 0.8}}})
    started = len(_docker_runs(ctl))
    res = ctl.run("resume", "bud-resume")
    assert res.returncode == 2, res.stderr
    assert "overall cap on worker: $1.80 spent of $1.50" in res.stderr
    assert "overall cap on total: $3.40 spent of $3.00" in res.stderr
    assert "per approach cap on review: $0.80 spent of $0.75" in res.stderr
    assert "verify" not in res.stderr                 # not spent: not named
    assert len(_docker_runs(ctl)) == started, "a refused resume starts no container"


def test_resume_is_still_refused_while_one_cap_stays_spent_then_accepted(ctl):
    _start(ctl, "bud-resume2", "--spend-cap", "worker=1.5", "--spend-cap", "total=3")
    _exhaust(ctl, "bud-resume2", "spend:worker", {
        "overall": {"worker": 1.8, "total": 3.4}})
    assert ctl.run("budget", "bud-resume2", "--spend", "worker=+2").returncode == 0
    started = len(_docker_runs(ctl))
    res = ctl.run("resume", "bud-resume2")
    assert res.returncode == 2
    assert "cap on total" in res.stderr and "cap on worker" not in res.stderr
    assert ctl.run("budget", "bud-resume2", "--spend", "total=none").returncode == 0
    res = ctl.run("resume", "bud-resume2")
    assert res.returncode == 0, res.stderr
    assert len(_docker_runs(ctl)) == started + 1


def test_a_per_approach_exhaustion_resumes_after_a_per_approach_top_up(ctl):
    _start(ctl, "bud-resume3", "--spend-cap-approach", "worker=1")
    _exhaust(ctl, "bud-resume3", "spend:worker:approach", {
        "overall": {"worker": 9.0}, "byApproach": {"2": {"worker": 1.2}}}, approach=2)
    res = ctl.run("resume", "bud-resume3")
    assert res.returncode == 2
    assert "per approach cap on worker: $1.20 spent of $1.00" in res.stderr
    assert "--spend-approach worker=+USD" in res.stderr
    assert ctl.run("budget", "bud-resume3", "--spend-approach",
                   "worker=+1").returncode == 0
    assert ctl.run("resume", "bud-resume3").returncode == 0


def test_the_capped_hat_is_dispatched_again_once_the_cap_is_lifted(tmp_path):
    """Engine side of the resume: the second process reads the lifted caps
    (as `JobConfig.load` does from job.yaml) and dispatches the worker."""
    sup_for = scripted_supervisor(
        run_id="spend-resume", iterations=60, vigilant=False, on_complete="exit",
        infra_outage_budget_s=1000.0)
    root = tmp_path / "run"
    first = sup_for(root, {"planning": [_plan(0.1)], "worker": [_work(0.6)]},
                    spend_caps=_caps(overall={"worker": 1.0}), max_approaches=3)
    RunDir(root=root).update_status(state="starting")
    assert asyncio.run(asyncio.wait_for(first._run_job_core(), 120)) == "exhausted"
    assert first.runner.calls["worker"] == 2
    status = json.loads((root / "status.json").read_text())
    assert status["exhaustedBudget"] == "spend:worker"

    lifted = spend_caps.apply_budget_specs(
        first.cfg.spend_caps, {"overall": [("worker", "add", 1.0)]})
    assert lifted["overall"]["worker"] == _entry(2.0, "budget")
    second = sup_for(root, {"worker": [_work(0.6)]}, spend_caps=lifted,
                     max_approaches=3)
    final = asyncio.run(asyncio.wait_for(second._run_job_core(), 120))
    assert second.runner.calls["worker"] >= 1, "the capped hat was not dispatched"
    status = json.loads((root / "status.json").read_text())
    assert status["spend"]["overall"]["worker"] > 1.2
    assert final == "exhausted" and status["exhaustedBudget"] == "spend:worker"


def test_the_flags_and_the_resume_rule_are_documented():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    cli = (root / "docs" / "cli.md").read_text()
    spec = (root / "SPEC.md").read_text()
    for text in (cli, spec):
        assert "--spend-approach" in text and "--spend HAT" in text.replace(
            "--spend <HAT>", "--spend HAT")
    assert "still spent" in cli
