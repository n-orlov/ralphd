"""`spend_caps` configuration (#321 A): the shape, the per-hat precedence, the
`job.yaml` record with each entry's source, and the unpriceable refusal at
`ralphctl start`. Drives the real `ralphctl` through tests/test_cli_docker.py's
`Ctl` (stub docker, temp registry), as tests/test_cli_model_per_hat.py does."""

from __future__ import annotations

import pytest
import yaml
from test_cli_docker import Ctl

from ralphd.engine.config import JobConfig
from ralphd.engine.spend_caps import CAP_KEYS

pytestmark = [pytest.mark.smoke, pytest.mark.fast]

PRICED = "prov/priced-model"
PRICING = "pricing:\n  models:\n    \"prov/*\": {input: 1.0, output: 2.0}\n"


@pytest.fixture
def ctl(tmp_path, capsys):
    c = Ctl(tmp_path, capsys)
    (c.registry / "config.yaml").write_text(PRICING)   # `prov/*` is priced
    return c


def _job_path(ctl: Ctl, run_id: str):
    matches = list(ctl.tmp.glob(f"**/configs/{run_id}/job.yaml"))
    assert len(matches) == 1, matches
    return matches[0]


def _job(ctl: Ctl, run_id: str) -> dict:
    return yaml.safe_load(_job_path(ctl, run_id).read_text())


def _start(ctl: Ctl, run_id: str, *extra: str):
    return ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                   "--run-id", run_id, "--model", PRICED, *extra)


def _registry(ctl: Ctl, caps: str) -> None:
    (ctl.registry / "config.yaml").write_text(PRICING + caps)


def _template(ctl: Ctl, caps: str) -> None:
    tdir = ctl.registry / "templates" / "caps"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "job.yaml").write_text(caps)


def test_the_valid_keys_are_the_nine_model_hats_and_total():
    assert set(CAP_KEYS) == {"planning", "worker", "verify", "review", "plan_review",
                             "reflect", "router", "steer", "verdict_gate", "total"}
    assert len(CAP_KEYS) == 10


@pytest.mark.parametrize("flag", ["--spend-cap", "--spend-cap-approach"])
def test_an_unknown_key_is_refused_with_the_valid_names(ctl, flag):
    res = _start(ctl, "caps-unknown", flag, "reviewer=5")
    assert res.returncode == 2, res.stderr
    assert "'reviewer'" in res.stderr
    for name in CAP_KEYS:
        assert name in res.stderr
    assert not list(ctl.tmp.glob("**/configs/caps-unknown"))


def test_an_unknown_key_in_a_template_or_the_registry_is_refused_with_the_names(ctl):
    _template(ctl, "spend_caps:\n  overall:\n    bogus: 3\n")
    res = _start(ctl, "caps-tpl-unknown", "--template", "caps")
    assert res.returncode == 2, res.stderr
    assert "bogus" in res.stderr and "verdict_gate" in res.stderr
    _registry(ctl, "spend_caps:\n  per_approach:\n    nope: 3\n")
    res = _start(ctl, "caps-reg-unknown")
    assert res.returncode == 2, res.stderr
    assert "nope" in res.stderr and "verdict_gate" in res.stderr


@pytest.mark.parametrize("amount", ["0", "-1", "-0.5"])
@pytest.mark.parametrize("flag", ["--spend-cap", "--spend-cap-approach"])
def test_a_zero_or_negative_amount_is_refused(ctl, flag, amount):
    res = _start(ctl, "caps-nonpositive", flag, f"review={amount}")
    assert res.returncode == 2, res.stderr
    assert "positive" in res.stderr and "review" in res.stderr


def test_a_zero_amount_in_a_template_is_refused(ctl):
    _registry(ctl, "")
    _template(ctl, "spend_caps:\n  overall:\n    review: 0\n")
    res = _start(ctl, "caps-tpl-zero", "--template", "caps")
    assert res.returncode == 2, res.stderr
    assert "positive" in res.stderr


def test_a_malformed_flag_is_refused(ctl):
    res = _start(ctl, "caps-malformed", "--spend-cap", "review")
    assert res.returncode == 2, res.stderr
    assert "HAT=USD" in res.stderr


LEVELS = [("overall", "--spend-cap"), ("per_approach", "--spend-cap-approach")]


@pytest.mark.parametrize("level,flag", LEVELS)
def test_registry_is_weaker_than_the_template(ctl, level, flag):
    _registry(ctl, f"spend_caps:\n  {level}:\n    review: 10\n")
    _template(ctl, f"spend_caps:\n  {level}:\n    review: 4\n")
    res = _start(ctl, "caps-reg-tpl", "--template", "caps")
    assert res.returncode == 0, res.stderr
    assert _job(ctl, "caps-reg-tpl")["spend_caps"] == {
        level: {"review": {"usd": 4.0, "source": "template"}}}


@pytest.mark.parametrize("level,flag", LEVELS)
def test_template_is_weaker_than_the_flag(ctl, level, flag):
    _template(ctl, f"spend_caps:\n  {level}:\n    review: 4\n")
    res = _start(ctl, "caps-tpl-flag", "--template", "caps", flag, "review=2.5")
    assert res.returncode == 0, res.stderr
    assert _job(ctl, "caps-tpl-flag")["spend_caps"] == {
        level: {"review": {"usd": 2.5, "source": "flag"}}}


@pytest.mark.parametrize("level,flag", LEVELS)
def test_registry_is_weaker_than_the_flag(ctl, level, flag):
    _registry(ctl, f"spend_caps:\n  {level}:\n    review: 10\n")
    res = _start(ctl, "caps-reg-flag", flag, "review=1")
    assert res.returncode == 0, res.stderr
    assert _job(ctl, "caps-reg-flag")["spend_caps"] == {
        level: {"review": {"usd": 1.0, "source": "flag"}}}


def test_a_flag_replaces_only_its_own_hats_entry_and_flags_repeat(ctl):
    _registry(ctl, "spend_caps:\n  overall:\n    review: 10\n    worker: 30\n"
                   "  per_approach:\n    review: 5\n")
    _template(ctl, "spend_caps:\n  overall:\n    verify: 7\n")
    res = _start(ctl, "caps-merge", "--template", "caps",
                 "--spend-cap", "review=2", "--spend-cap", "total=90",
                 "--spend-cap-approach", "worker=4", "--spend-cap-approach", "verify=1")
    assert res.returncode == 0, res.stderr
    assert _job(ctl, "caps-merge")["spend_caps"] == {
        "overall": {"review": {"usd": 2.0, "source": "flag"},
                    "worker": {"usd": 30.0, "source": "registry"},
                    "verify": {"usd": 7.0, "source": "template"},
                    "total": {"usd": 90.0, "source": "flag"}},
        "per_approach": {"review": {"usd": 5.0, "source": "registry"},
                         "worker": {"usd": 4.0, "source": "flag"},
                         "verify": {"usd": 1.0, "source": "flag"}}}


def test_the_recorded_caps_load_into_the_engine_job_config(ctl):
    res = _start(ctl, "caps-load", "--spend-cap", "review=2")
    assert res.returncode == 0, res.stderr
    cfg = JobConfig.load(_job_path(ctl, "caps-load"))
    assert cfg.spend_caps == {"overall": {"review": {"usd": 2.0, "source": "flag"}}}


def test_a_run_with_no_caps_writes_no_spend_caps_key(ctl):
    res = _start(ctl, "caps-none")
    assert res.returncode == 0, res.stderr
    assert "spend_caps" not in _job_path(ctl, "caps-none").read_text()
    assert JobConfig.load(_job_path(ctl, "caps-none")).spend_caps == {}


def test_a_cap_on_an_unpriceable_hat_is_refused_naming_hat_and_model(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "caps-unpriced", "--model", "other/unknown",
                  "--model-for", f"review={PRICED}", "--spend-cap", "worker=5")
    assert res.returncode == 2, res.stderr
    assert "worker" in res.stderr and "other/unknown" in res.stderr
    assert "no price in the pricing chain" in res.stderr
    assert not list(ctl.tmp.glob("**/caps-unpriced*"))


def test_a_cap_on_a_priceable_hat_is_accepted_even_when_another_hat_is_not(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "caps-partial", "--model", "other/unknown",
                  "--model-for", f"review={PRICED}", "--spend-cap-approach", "review=5")
    assert res.returncode == 0, res.stderr
    assert list(ctl.tmp.glob("**/caps-partial*")), "a started run leaves a directory"
    assert _job(ctl, "caps-partial")["spend_caps"] == {
        "per_approach": {"review": {"usd": 5.0, "source": "flag"}}}


def test_a_total_cap_names_every_unpriceable_hat_of_the_lineup(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "caps-total", "--model", PRICED,
                  "--model-for", "worker=other/unpriced-w",
                  "--model-for", "verify=other/unpriced-v",
                  "--spend-cap", "total=50")
    assert res.returncode == 2, res.stderr
    assert "worker" in res.stderr and "other/unpriced-w" in res.stderr
    assert "verify" in res.stderr and "other/unpriced-v" in res.stderr
    assert "no price in the pricing chain" in res.stderr
    assert "hat 'planning'" not in res.stderr
    assert not list(ctl.tmp.glob("**/caps-total*"))


def test_a_total_cap_is_accepted_when_the_whole_lineup_is_priced(ctl):
    res = _start(ctl, "caps-total-ok", "--spend-cap", "total=50")
    assert res.returncode == 0, res.stderr
    assert _job(ctl, "caps-total-ok")["spend_caps"] == {
        "overall": {"total": {"usd": 50.0, "source": "flag"}}}


def test_a_hat_declared_free_counts_as_priced(ctl):
    (ctl.registry / "config.yaml").write_text("pricing:\n  free: [\"prov/*\"]\n")
    res = _start(ctl, "caps-free", "--spend-cap", "total=50")
    assert res.returncode == 0, res.stderr


def test_the_flags_appear_in_start_help(ctl):
    res = ctl.run("start", "--help")
    assert "--spend-cap HAT=USD" in res.stdout
    assert "--spend-cap-approach HAT=USD" in res.stdout
