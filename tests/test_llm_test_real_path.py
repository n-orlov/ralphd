"""PRD v0.30 H: `ralphctl llm test` tests the real path.

`llm test` pings every distinct (harness, model) pair through the runner an
iteration uses -- `ralphd-engine --llm-ping` in a throwaway container from the
run image, as the agent uid -- and judges each pair by its `IterationResult`,
never by the container's exit status.

The CLI runs in-process (`call_ralphctl_in_process`) against the recording
stub docker in its `STUB_DOCKER_RUN_EXEC` mode, which execs the container's
entrypoint locally with the `-e` env merged in. That puts the real
`ralphd-engine --llm-ping` on the far side of the `docker run`, driving
stub-pi (first on PATH) or, through the real Agent SDK, stub-claude
(`RALPHD_CLAUDE_CLI_PATH`). No network, no real token.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.conftest import _resolve_engine, call_ralphctl_in_process

REPO = Path(__file__).resolve().parent.parent
STUB_DOCKER = REPO / "tests" / "stub-docker" / "docker"
STUB_PI_DIR = REPO / "tests" / "stub-pi"
STUB_CLAUDE = REPO / "tests" / "stub-claude" / "claude"
FAKE_OAUTH = "sk-ant-oat01-FAKE-FOR-TESTS-ONLY"
PINNED_IMAGE = "ralphd:llm-test-pin"


class Ctl:
    def __init__(self, tmp: Path, capsys):
        self.tmp = tmp
        self.capsys = capsys
        self.registry = tmp / "registry"
        self.registry.mkdir()
        self.log = tmp / "docker-argv.jsonl"
        self.stub_run = tmp / "stub-run"
        self.stub_run.mkdir()
        engine = _resolve_engine(Path(sys.executable).parent, os.environ)
        assert engine is not None
        self.path = os.pathsep.join([str(STUB_PI_DIR), str(engine.parent),
                                     os.environ.get("PATH", "")])

    def write_profile(self, name: str, doc: dict) -> None:
        d = self.registry / "llm-profiles"
        d.mkdir(exist_ok=True)
        (d / f"{name}.yaml").write_text(yaml.safe_dump(doc))

    def write_template(self, name: str, job: dict) -> None:
        d = self.registry / "templates" / name
        d.mkdir(parents=True)
        (d / "job.yaml").write_text(yaml.safe_dump(job))

    def run(self, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
        full_env = {
            **os.environ,
            "PATH": self.path,
            "RALPHD_DOCKER": str(STUB_DOCKER),
            "RALPHD_REGISTRY": str(self.registry),
            "STUB_DOCKER_LOG": str(self.log),
            "STUB_DOCKER_RUN_EXEC": "1",
            "STUB_RUN_DIR": str(self.stub_run),
            "RALPHD_CLAUDE_CLI_PATH": str(STUB_CLAUDE),
            **(env or {}),
        }
        return call_ralphctl_in_process(list(argv), env=full_env, capsys=self.capsys)

    def runs(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [a for a in (json.loads(line) for line in self.log.read_text().splitlines())
                if a[:1] == ["run"]]


@pytest.fixture
def ctl(tmp_path, capsys):
    return Ctl(tmp_path, capsys)


def _ping_args(argv: list[str]) -> list[str]:
    return argv[argv.index("--llm-ping"):]


def test_a_healthy_pair_prints_ok_and_pings_through_the_runner(ctl):
    ctl.write_profile("acme", {"description": "acme", "model": "acme/m1",
                               "env": {"ACME_KEY": "acme-literal-value"}})
    res = ctl.run("llm", "test", "acme", "--image", PINNED_IMAGE)
    assert res.returncode == 0, res.stderr
    assert res.stdout.splitlines() == ["ok  pi  acme/m1"]
    runs = ctl.runs()
    assert len(runs) == 1
    argv = runs[0]
    assert "--rm" in argv and "-i" in argv and "-d" not in argv
    assert argv[argv.index("--user") + 1] == "1000"
    assert argv[argv.index("--label") + 1] == "ralphd.llm-test=acme"
    assert argv[argv.index("--entrypoint") + 1] == "ralphd-engine"
    assert "ACME_KEY=acme-literal-value" in argv
    assert argv[argv.index("--llm-ping") - 1] == PINNED_IMAGE
    assert _ping_args(argv) == ["--llm-ping", "--harness", "pi", "--model", "acme/m1"]


def test_json_output_is_one_verdict_per_pair(ctl):
    ctl.write_profile("acme", {"description": "acme", "model": "acme/m1"})
    res = ctl.run("--json", "llm", "test", "acme", "--image", PINNED_IMAGE)
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    assert [(v["harness"], v["model"], v["ok"]) for v in doc] == [("pi", "acme/m1", True)]
    assert doc[0]["image"] == PINNED_IMAGE and doc[0]["faultClass"] is None


def test_an_inband_pi_error_with_exit_0_fails_with_its_class_and_message(ctl):
    """pi exits 0 on an in-band provider error; `llm test` used to pass it."""
    ctl.write_profile("acme", {"description": "acme", "model": "acme/m1"})
    res = ctl.run("llm", "test", "acme", "--image", PINNED_IMAGE,
                  env={"STUB_INBAND_ERROR_COUNT": "1"})
    assert res.returncode == 1
    assert res.stdout.splitlines() == ["FAIL pi acme/m1  infra: Connection error."]
    assert "1 of 1" in res.stderr


def test_an_inband_pi_refusal_fails_as_refused(ctl):
    ctl.write_profile("acme", {"description": "acme", "model": "acme/m1"})
    res = ctl.run("llm", "test", "acme", "--image", PINNED_IMAGE, env={
        "STUB_INBAND_ERROR_COUNT": "1",
        "STUB_INBAND_ERROR_MESSAGE": '401 {"type":"error","error":{"type":'
                                     '"authentication_error","message":"invalid x-api-key"}}'})
    assert res.returncode == 1
    (line,) = res.stdout.splitlines()
    assert line.startswith("FAIL pi acme/m1  refused: 401 ")
    assert "authentication_error" in line


def test_a_claude_code_refusal_fails_with_class_refused_and_a_scrubbed_message(ctl):
    ctl.write_profile("sub", {"description": "subscription",
                              "model": "anthropic/claude-sonnet-5",
                              "harness": "claude-code",
                              "env": {"CLAUDE_CODE_OAUTH_TOKEN": FAKE_OAUTH}})
    res = ctl.run("llm", "test", "sub", "--image", PINNED_IMAGE, env={
        "STUB_CLAUDE_SCENARIO": "refusal",
        "STUB_CLAUDE_ERROR_MESSAGE": f"Third-party apps now draw from your extra "
                                     f"usage, not your plan limits ({FAKE_OAUTH})"})
    assert res.returncode == 1, res.stdout
    (line,) = res.stdout.splitlines()
    assert line.startswith("FAIL claude-code anthropic/claude-sonnet-5  refused: ")
    assert "Third-party apps now draw from your extra usage" in line
    assert FAKE_OAUTH not in res.stdout + res.stderr
    assert "REDACTED" in line
    (argv,) = ctl.runs()
    assert _ping_args(argv) == ["--llm-ping", "--harness", "claude-code",
                                "--model", "anthropic/claude-sonnet-5"]


def test_a_healthy_claude_code_pair_prints_ok(ctl):
    ctl.write_profile("sub", {"description": "subscription",
                              "model": "anthropic/claude-sonnet-5",
                              "harness": "claude-code",
                              "env": {"CLAUDE_CODE_OAUTH_TOKEN": FAKE_OAUTH}})
    res = ctl.run("llm", "test", "sub", "--image", PINNED_IMAGE)
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.splitlines() == ["ok  claude-code  anthropic/claude-sonnet-5"]


def test_hats_pings_exactly_the_distinct_merged_pairs(ctl):
    """worker on claude-code, every other hat on pi, over two pi models: the
    registry, the template and the command-line flags merge as on `start`."""
    ctl.write_profile("acme", {"description": "acme", "model": "acme/m1"})
    (ctl.registry / "config.yaml").write_text(yaml.safe_dump(
        {"models": {"review": "acme/m2"}}))
    ctl.write_template("mixed", {
        "harnesses": {"worker": "claude-code"},
        "models": {"worker": "anthropic/claude-sonnet-5"},
        "model_tiers": {"worker": {"hard": "anthropic/claude-opus-5"}},
    })
    res = ctl.run("llm", "test", "acme", "--image", PINNED_IMAGE, "--hats",
                  "--template", "mixed", "--model-for", "verify=acme/m2",
                  "--harness-for", "steer=pi")
    assert res.returncode == 0, res.stdout + res.stderr
    expected = [("pi", "acme/m1"), ("claude-code", "anthropic/claude-sonnet-5"),
                ("claude-code", "anthropic/claude-opus-5"), ("pi", "acme/m2")]
    assert res.stdout.splitlines() == [f"ok  {h}  {m}" for h, m in expected]
    pinged = [tuple(_ping_args(a)[2:5:2]) for a in ctl.runs()]
    assert pinged == expected


def test_hats_harness_for_on_the_command_line_wins(ctl):
    ctl.write_profile("acme", {"description": "acme", "model": "acme/m1"})
    res = ctl.run("--json", "llm", "test", "acme", "--image", PINNED_IMAGE, "--hats",
                  "--harness-for", "worker=claude-code",
                  "--model-for", "worker=anthropic/claude-sonnet-5")
    assert res.returncode == 0, res.stdout + res.stderr
    pairs = [(v["harness"], v["model"]) for v in json.loads(res.stdout)]
    assert pairs == [("pi", "acme/m1"), ("claude-code", "anthropic/claude-sonnet-5")]


def test_hat_flags_without_hats_are_a_usage_error(ctl):
    ctl.write_profile("acme", {"description": "acme", "model": "acme/m1"})
    res = ctl.run("llm", "test", "acme", "--harness-for", "worker=claude-code")
    assert res.returncode == 2
    assert "--hats" in res.stderr
    assert not ctl.runs()


def test_the_image_default_is_the_run_image_not_ralphd_dev(ctl):
    from ralphd.cli import image
    from ralphd.cli.main import current_source_hash

    ctl.write_profile("acme", {"description": "acme", "model": "acme/m1"})
    env = {k: "" for k in ("RALPHD_IMAGE",)}
    res = ctl.run("llm", "test", "acme", env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    (argv,) = ctl.runs()
    used = argv[argv.index("--llm-ping") - 1]
    _, source_hash = current_source_hash()
    assert source_hash
    assert used == image.image_tag(source_hash)
    assert used != "ralphd:dev"


def test_a_registry_image_pin_is_the_run_image(ctl):
    ctl.write_profile("acme", {"description": "acme", "model": "acme/m1"})
    (ctl.registry / "config.yaml").write_text(yaml.safe_dump({"image": "acme:pinned"}))
    res = ctl.run("llm", "test", "acme", env={"RALPHD_IMAGE": ""})
    assert res.returncode == 0, res.stdout + res.stderr
    (argv,) = ctl.runs()
    assert argv[argv.index("--llm-ping") - 1] == "acme:pinned"


def test_a_container_that_prints_no_verdict_fails(ctl):
    """Without the exec mode the stub's `docker run` prints a container id
    and exits 0 -- the old pass condition. That is not a verdict."""
    ctl.write_profile("acme", {"description": "acme", "model": "acme/m1"})
    res = ctl.run("llm", "test", "acme", "--image", PINNED_IMAGE,
                  env={"STUB_DOCKER_RUN_EXEC": ""})
    assert res.returncode == 1
    (line,) = res.stdout.splitlines()
    assert line.startswith("FAIL pi acme/m1  no-verdict: the ping container printed "
                           "no verdict (exit 0)")


def test_the_host_profile_stages_the_pi_dir_as_start_does(ctl, tmp_path, monkeypatch):
    """`host` mounts every host pi file `start --llm host` copies, with a
    `!command` apiKey resolved on the host and a template `llm_header`
    merged in -- read back from the staged files before they are removed."""
    from ralphd.cli import main as cli_main

    home = tmp_path / "home"
    pi_dir = home / ".pi" / "agent"
    pi_dir.mkdir(parents=True)
    (pi_dir / "models.json").write_text(json.dumps({"providers": {"gw": {
        "baseUrl": "https://gw.example.invalid", "apiKey": "!echo resolved-key-0123"}}}))
    (pi_dir / "settings.json").write_text("{}")
    ctl.write_template("hdr", {"llm_header": ["X-Ticket: T-1"]})
    staged: dict[str, str] = {}
    real_stage = cli_main._llm_test_stage_pi

    def capture(resolved, headers, stage):
        mounts = real_stage(resolved, headers, stage)
        for f in (stage / "pi").iterdir():
            staged[f.name] = f.read_text()
        return mounts

    monkeypatch.setattr(cli_main, "_llm_test_stage_pi", capture)
    res = ctl.run("llm", "test", "host", "--image", PINNED_IMAGE, "--template", "hdr",
                  env={"HOME": str(home)})
    assert res.returncode == 0, res.stdout + res.stderr
    assert sorted(staged) == ["models.json", "settings.json"]
    provider = json.loads(staged["models.json"])["providers"]["gw"]
    assert provider["apiKey"] == "resolved-key-0123"
    assert provider["headers"] == {"X-Ticket": "T-1"}
    (argv,) = ctl.runs()
    targets = sorted(argv[i + 1].split(":")[1] for i, a in enumerate(argv) if a == "-v")
    assert targets == ["/home/agent/.pi/agent/models.json",
                       "/home/agent/.pi/agent/settings.json"]
    assert not any(Path(argv[i + 1].split(":")[0]).exists()
                   for i, a in enumerate(argv) if a == "-v")
    assert _ping_args(argv) == ["--llm-ping", "--harness", "pi"]


def test_the_ping_verdict_reads_the_iteration_result_not_the_exit_code():
    """`llm_ping.judge` over results the runner can hand back: blank text
    with exit 0 fails; a fresh rejected rate-limit report is `rate_limit`."""
    from ralphd.engine.llm_ping import judge
    from ralphd.engine.runner import IterationResult

    blank = judge("pi", "acme/m1", IterationResult(exit_code=0, final_text="  "))
    assert blank["ok"] is False and blank["faultClass"] == "work"
    assert blank["message"] == "the model returned no text"

    limited = judge("claude-code", None, IterationResult(
        exit_code=1, error_message="429 rate_limit: API Error: 429",
        http_status=429, error_type="rate_limit", harness="claude-code",
        rate_limit={"status": "rejected", "rateLimitType": "five_hour",
                    "resetsAt": 1790936400},
        rate_limit_fresh=True))
    assert limited["ok"] is False and limited["faultClass"] == "rate_limit"

    healthy = judge("pi", "acme/m1", IterationResult(exit_code=0, final_text="ok"))
    assert healthy == {"harness": "pi", "model": "acme/m1", "ok": True,
                       "faultClass": None, "reason": None, "message": "",
                       "exitCode": 0}
