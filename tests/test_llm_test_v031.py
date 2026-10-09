"""v0.31 #286: `ralphctl llm test` resolves and judges as `start` does.

(a) `--hats` runs the engine's claude-code route check, with or without
    `--no-ping`; (b) a claude-code 404 / `model_not_found` ping is `refused`, as
    in a run (pi's text-only 404 keeps `infra`, as in a run);
(c) `--network` is resolved with `start`'s precedence and reaches the ping
container; (d) a runner that exits non-zero shows a scrubbed tail of its
stderr in the FAIL line.

Same harness as `test_llm_test_real_path.py` (recording stub docker in exec
mode); no network, no real Claude.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml

from tests.test_llm_test_real_path import (  # noqa: F401  (ctl is a fixture)
    PINNED_IMAGE, Ctl, _ping_args,
)


@pytest.fixture
def ctl(tmp_path, capsys):
    return Ctl(tmp_path, capsys)


def _network_of(argv: list[str]) -> str | None:
    return argv[argv.index("--network") + 1] if "--network" in argv else None


# -- (a) the claude-code route check ----------------------------------------

def test_hats_refuses_a_non_anthropic_ref_on_a_claude_code_hat_even_with_no_ping(ctl):
    ctl.write_profile("sub", {"description": "sub", "harness": "claude-code",
                              "model": "amazon-bedrock/eu.anthropic.claude-opus-5-5"})
    res = ctl.run("llm", "test", "sub", "--hats", "--no-ping")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "resolves OK" not in res.stdout
    assert ("hat 'planning' runs on the claude-code harness but is configured "
            "with model 'amazon-bedrock/eu.anthropic.claude-opus-5-5', whose "
            "provider 'amazon-bedrock' is not 'anthropic'; the claude-code "
            "harness only serves anthropic models") in res.stderr
    assert not ctl.runs()


def test_the_refusal_message_is_the_engines_own(ctl):
    from ralphd.engine.config import JobConfig
    from ralphd.engine.llm import UnroutableHatModel, check_hat_routes

    ctl.write_profile("sub", {"description": "sub", "harness": "claude-code",
                              "model": "aigw-openai/openai.gpt-6-sol"})
    res = ctl.run("llm", "test", "sub", "--hats", "--no-ping")
    from ralphd.engine.config import MODEL_HATS
    job = JobConfig(model="aigw-openai/openai.gpt-6-sol",
                    harnesses={h: "claude-code" for h in MODEL_HATS})
    with pytest.raises(UnroutableHatModel) as exc:
        check_hat_routes(job, home=ctl.tmp)
    assert res.returncode == 1
    assert str(exc.value) in res.stderr


def test_hats_refuses_before_pinging_a_bad_tier_ref_from_a_template_harness(ctl):
    ctl.write_profile("acme", {"description": "acme", "model": "acme/m1"})
    ctl.write_template("mixed", {
        "harnesses": {"worker": "claude-code"},
        "models": {"worker": "anthropic/claude-sonnet-5"},
        "model_tiers": {"worker": {"hard": "aigw-openai/gpt"}},
    })
    res = ctl.run("llm", "test", "acme", "--image", PINNED_IMAGE, "--hats",
                  "--template", "mixed")
    assert res.returncode == 1
    assert "hat 'worker' tier 'hard' runs on the claude-code harness" in res.stderr
    assert not ctl.runs()


def test_harness_for_flag_reaches_the_route_check(ctl):
    ctl.write_profile("acme", {"description": "acme", "model": "acme/m1"})
    res = ctl.run("llm", "test", "acme", "--hats", "--no-ping",
                  "--harness-for", "verify=claude-code")
    assert res.returncode == 1
    assert "hat 'verify' runs on the claude-code harness" in res.stderr


def test_the_route_check_only_applies_with_hats(ctl):
    ctl.write_profile("sub", {"description": "sub", "harness": "claude-code",
                              "model": "acme/m1"})
    res = ctl.run("llm", "test", "sub", "--no-ping")
    assert res.returncode == 0, res.stderr
    assert "resolves OK" in res.stdout


# -- (b) refusals are `refused` ---------------------------------------------

def test_a_pi_404_model_not_found_ping_stays_infra_like_the_loop(ctl):
    ctl.write_profile("acme", {"description": "acme", "model": "acme/m1"})
    res = ctl.run("llm", "test", "acme", "--image", PINNED_IMAGE, env={
        "STUB_INBAND_ERROR_COUNT": "1",
        "STUB_INBAND_ERROR_MESSAGE":
            '404 {"error":{"code":"model_not_found","message":"no such model"}}'})
    assert res.returncode == 1
    (line,) = res.stdout.splitlines()
    assert line.startswith("FAIL pi acme/m1  infra: ")


def test_judge_classifies_model_not_found_like_the_loop():
    from ralphd.engine.faults import explain_fault
    from ralphd.engine.llm_ping import judge
    from ralphd.engine.runner import IterationResult

    for harness, expected, result in (
            # pi's text-only gateway 404 is unchanged: infra, as in a run
            ("pi", "infra", IterationResult(
                exit_code=1, error_message="404 model_not_found")),
            ("claude-code", "refused", IterationResult(
                exit_code=1, http_status=404, harness="claude-code",
                error_message="API Error: 404 model_not_found: no such model")),
            ("pi", "refused", IterationResult(exit_code=0, error_message=(
                '404 {"type":"error","error":{"type":"not_found_error"}}')))):
        verdict = judge(harness, "m", result)
        loop_class = explain_fault(
            error_text=result.error_message, exit_code=result.exit_code,
            interrupted=False, timed_out=False, no_traffic_timeout=False,
            produced_traffic=False, http_status=result.http_status)["faultClass"]
        assert verdict["faultClass"] == loop_class == expected


# -- (c) --network -----------------------------------------------------------

def test_no_network_flag_keeps_the_default_bridge(ctl):
    ctl.write_profile("acme", {"description": "acme", "model": "acme/m1"})
    res = ctl.run("llm", "test", "acme", "--image", PINNED_IMAGE)
    assert res.returncode == 0, res.stderr
    (argv,) = ctl.runs()
    assert "--network" not in argv


def test_network_flag_reaches_the_ping_container(ctl):
    ctl.write_profile("acme", {"description": "acme", "model": "acme/m1"})
    res = ctl.run("llm", "test", "acme", "--image", PINNED_IMAGE, "--network", "host")
    assert res.returncode == 0, res.stderr
    (argv,) = ctl.runs()
    assert _network_of(argv) == "host"
    assert argv.index("--network") < argv.index("--entrypoint")


@pytest.mark.parametrize("template_net, registry_net, flag, expected", [
    (None, None, None, None),
    (None, "regnet", None, "regnet"),
    ("host", "regnet", None, "host"),
    ("host", "regnet", "flagnet", "flagnet"),
    (None, "regnet", "flagnet", "flagnet"),
])
def test_network_precedence_is_starts(ctl, template_net, registry_net, flag, expected):
    ctl.write_profile("acme", {"description": "acme", "model": "acme/m1"})
    job = {"network": template_net} if template_net else {}
    ctl.write_template("t", job)
    if registry_net:
        (ctl.registry / "config.yaml").write_text(yaml.safe_dump({"network": registry_net}))
    argv_in = ["llm", "test", "acme", "--image", PINNED_IMAGE, "--template", "t"]
    if flag:
        argv_in += ["--network", flag]
    res = ctl.run(*argv_in)
    assert res.returncode == 0, res.stderr
    (argv,) = ctl.runs()
    assert _network_of(argv) == expected


def test_network_applies_to_every_hats_ping(ctl):
    ctl.write_profile("acme", {"description": "acme", "model": "acme/m1"})
    ctl.write_template("t", {"network": "host", "models": {"review": "acme/m2"}})
    res = ctl.run("llm", "test", "acme", "--image", PINNED_IMAGE, "--hats",
                  "--template", "t")
    assert res.returncode == 0, res.stdout + res.stderr
    runs = ctl.runs()
    assert len(runs) == 2
    assert {_network_of(a) for a in runs} == {"host"}


# -- (d) the runner's stderr tail ---------------------------------------------

def _failing_pi(tmp_path, stderr_text: str):
    d = tmp_path / "failing-pi"
    d.mkdir()
    pi = d / "pi"
    pi.write_text("#!/bin/sh\ncat >/dev/null\n"
                  f"echo '{stderr_text}' >&2\nexit 1\n")
    pi.chmod(pi.stat().st_mode | stat.S_IEXEC)
    return d


def test_a_runner_that_exits_nonzero_shows_its_scrubbed_stderr_tail(ctl, tmp_path):
    secret = "ping-secret-value-0123456789"
    ctl.write_profile("acme", {"description": "acme", "model": "acme/m1",
                               "env": {"ACME_KEY": secret}})
    bad = _failing_pi(tmp_path, f"Error: getaddrinfo ENOTFOUND aigw.example.invalid {secret}")
    res = ctl.run("llm", "test", "acme", "--image", PINNED_IMAGE,
                  env={"PATH": os.pathsep.join([str(bad), ctl.path])})
    assert res.returncode == 1
    (line,) = res.stdout.splitlines()
    assert line.startswith("FAIL pi acme/m1  infra: the runner exited 1: ")
    assert "getaddrinfo ENOTFOUND aigw.example.invalid" in line
    assert secret not in res.stdout + res.stderr


def test_judge_appends_a_bounded_scrubbed_tail_only_for_a_nonzero_exit():
    from ralphd.engine.llm_ping import MESSAGE_LIMIT, STDERR_TAIL_LIMIT, judge
    from ralphd.engine.runner import IterationResult

    long_tail = "x" * 2000 + " ENOTFOUND-END"
    v = judge("pi", "m", IterationResult(exit_code=1), long_tail)
    assert v["message"].startswith("the runner exited 1: ")
    assert v["message"].endswith("ENOTFOUND-END")
    assert len(v["message"]) <= MESSAGE_LIMIT
    assert len(v["message"]) - len("the runner exited 1: ") <= STDERR_TAIL_LIMIT
    assert judge("pi", "m", IterationResult(exit_code=1))["message"] == "the runner exited 1"
    # a timeout keeps its own message
    assert judge("pi", "m", IterationResult(exit_code=1, timed_out=True),
                 "noise")["message"] == "the ping timed out"


def test_runner_stderr_tail_keeps_non_event_lines_and_scrubs(tmp_path):
    from ralphd.engine.llm_ping import runner_stderr_tail

    transcript = tmp_path / "ping.jsonl"
    transcript.write_text('{"type":"session"}\nfirst   line\n{"type":"x"}\n'
                          "last line\n")
    assert runner_stderr_tail(transcript) == "first line | last line"
    stderr_log = tmp_path / "ping.claude-scratch" / "stderr.log"
    stderr_log.parent.mkdir()
    stderr_log.write_text("wrapper boom\n")
    assert runner_stderr_tail(transcript).endswith("wrapper boom")
    assert runner_stderr_tail(tmp_path / "missing.jsonl") == ""


def test_runner_stderr_tail_includes_the_codex_wrapper_stderr_log(tmp_path):
    from ralphd.engine.llm_ping import runner_stderr_tail

    transcript = tmp_path / "ping.jsonl"
    transcript.write_text('{"type":"session"}\n')
    stderr_log = tmp_path / "ping.codex-scratch" / "stderr.log"
    stderr_log.parent.mkdir()
    stderr_log.write_text("codex   boom: getaddrinfo ENOTFOUND gw.example\n")
    assert runner_stderr_tail(transcript) == "codex boom: getaddrinfo ENOTFOUND gw.example"


def test_runner_stderr_tail_codex_log_is_scrubbed_bounded_and_keeps_the_end(tmp_path):
    from ralphd.engine.llm_ping import STDERR_TAIL_LIMIT, runner_stderr_tail

    transcript = tmp_path / "ping.jsonl"
    transcript.write_text("transcript line\n")
    stderr_log = tmp_path / "ping.codex-scratch" / "stderr.log"
    stderr_log.parent.mkdir()
    stderr_log.write_text("x" * 1000 + "\n{\"type\":\"event\"}\nLAST-codex-line\n")
    tail = runner_stderr_tail(transcript)
    assert len(tail) <= STDERR_TAIL_LIMIT
    assert tail.endswith("LAST-codex-line")
    assert "event" not in tail


def test_runner_stderr_tail_reads_both_wrapper_logs(tmp_path):
    from ralphd.engine.llm_ping import runner_stderr_tail

    transcript = tmp_path / "ping.jsonl"
    transcript.write_text("")
    for scratch, text in (("ping.claude-scratch", "claude-side"),
                          ("ping.codex-scratch", "codex-side")):
        (tmp_path / scratch).mkdir()
        (tmp_path / scratch / "stderr.log").write_text(text + "\n")
    tail = runner_stderr_tail(transcript)
    assert "claude-side" in tail and "codex-side" in tail


def test_build_runner_for_a_codex_pair_is_a_codex_runner(tmp_path):
    from ralphd.engine.llm_ping import build_runner
    from ralphd.engine.runner import ClaudeCodeRunner, CodexRunner, PiRunner

    assert type(build_runner("codex", tmp_path)) is CodexRunner
    assert type(build_runner("claude-code", tmp_path)) is ClaudeCodeRunner
    assert type(build_runner("pi", tmp_path)) is PiRunner


def test_spec_and_profile_docs_name_codexrunner_for_the_ping():
    root = Path(__file__).resolve().parent.parent
    spec = (root / "SPEC.md").read_text()
    row = next(l for l in spec.splitlines() if l.startswith("| `src/ralphd/engine/llm_ping.py`"))
    assert "`CodexRunner`" in row
    docs = (root / "docs" / "llm-profiles.md").read_text()
    section = docs.split("## Verifying a profile", 1)[1].split("\n## ", 1)[0]
    assert "CodexRunner" in section
    assert ".codex-scratch" in spec
