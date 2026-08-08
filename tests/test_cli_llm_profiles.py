"""Black-box tests for named LLM profiles (PRD req 13, resolution +
`ralphctl start --llm <name>` wiring).

Each test invokes the real `ralphctl` executable as a subprocess with
RALPHD_DOCKER pointing at the recording stub (tests/stub-docker/docker) and
RALPHD_REGISTRY at a tmp dir holding `llm-profiles/<name>.yaml`, then
asserts on the recorded `docker run` argv and the config dir the CLI
staged. No CLI internals are imported for the profile-selection assertions
(pure subprocess + filesystem); the reference-resolution edge cases are
additionally exercised directly against `ralphd.cli.llm_profiles` since
that module has no host-mutating side effects for those inputs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).parent.parent
STUB_DOCKER = REPO / "tests" / "stub-docker" / "docker"
RALPHCTL = Path(sys.executable).parent / "ralphctl"

sys.path.insert(0, str(REPO / "src"))
from ralphd.cli import llm_profiles


class Ctl:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.registry = tmp / "registry"
        self.registry.mkdir()
        self.log = tmp / "docker-argv.jsonl"
        self.prd = tmp / "prd.md"
        self.prd.write_text("# Test PRD\n\nDo the thing.\n")

    def profiles_dir(self) -> Path:
        d = self.registry / "llm-profiles"
        d.mkdir(exist_ok=True)
        return d

    def write_profile(self, name: str, doc: dict) -> Path:
        p = self.profiles_dir() / f"{name}.yaml"
        p.write_text(yaml.safe_dump(doc))
        return p

    def run(self, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
        full_env = {
            **os.environ,
            "RALPHD_DOCKER": str(STUB_DOCKER),
            "RALPHD_REGISTRY": str(self.registry),
            "STUB_DOCKER_LOG": str(self.log),
            **(env or {}),
        }
        return subprocess.run([str(RALPHCTL), *argv], env=full_env,
                              capture_output=True, text=True, timeout=60)

    def recorded(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def config_dir(self, run_id: str) -> Path:
        return self.registry / "configs" / run_id


@pytest.fixture
def ctl(tmp_path):
    return Ctl(tmp_path)


def docker_run_argv(ctl: Ctl) -> list[str]:
    runs = [a for a in ctl.recorded() if a[:2] == ["run", "-d"]]
    assert len(runs) == 1, f"expected one docker run, got: {ctl.recorded()}"
    return runs[0]


def env_vars(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]


def mount_specs(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]


# ---------------------------------------------------------- resolve_profile
def test_resolve_all_three_reference_forms(tmp_path, monkeypatch):
    reg = tmp_path / "reg"
    (reg / "llm-profiles").mkdir(parents=True)
    keyfile = tmp_path / "key.txt"
    keyfile.write_text("file-secret-value\n")
    (reg / "llm-profiles" / "acme.yaml").write_text(yaml.safe_dump({
        "description": "test profile",
        "model": "acme/big",
        "fast_model": "acme/small",
        "env": {
            "FROM_HOST": "${env:PROBE_HOST_VAR}",
            "FROM_FILE": f"${{file:{keyfile}}}",
            "FROM_CMD": "${cmd:echo -n cmd-secret-value}",
        },
        "pi": {
            "providers": {
                "acme": {
                    "baseUrl": "https://gw.example.com",
                    "apiKey": "${env:FROM_CMD}",
                }
            }
        },
        "mounts": ["~/.acme:/home/agent/.acme:ro"],
    }))
    monkeypatch.setenv("PROBE_HOST_VAR", "host-secret-value")
    resolved = llm_profiles.resolve_profile("acme", reg)
    assert resolved["model"] == "acme/big"
    assert resolved["fast_model"] == "acme/small"
    assert resolved["env"] == {
        "FROM_HOST": "host-secret-value",
        "FROM_FILE": "file-secret-value",
        "FROM_CMD": "cmd-secret-value",
    }
    # pi.providers.acme.apiKey references the *profile's own* resolved env
    # (not just host env), per docs/llm-profiles.md
    assert resolved["pi"]["providers"]["acme"]["apiKey"] == "cmd-secret-value"
    assert resolved["mounts"] == [f"{Path.home()}/.acme:/home/agent/.acme:ro"]


def test_resolve_unset_env_ref_raises_clear_error(tmp_path, monkeypatch):
    reg = tmp_path / "reg"
    (reg / "llm-profiles").mkdir(parents=True)
    (reg / "llm-profiles" / "bad.yaml").write_text(yaml.safe_dump({
        "env": {"KEY": "${env:TOTALLY_UNSET_VAR_XYZ}"},
    }))
    monkeypatch.delenv("TOTALLY_UNSET_VAR_XYZ", raising=False)
    with pytest.raises(llm_profiles.ProfileError) as ei:
        llm_profiles.resolve_profile("bad", reg)
    msg = str(ei.value)
    assert "bad" in msg
    assert "TOTALLY_UNSET_VAR_XYZ" in msg
    assert "env.KEY" in msg


def test_resolve_missing_file_ref_raises_clear_error(tmp_path):
    reg = tmp_path / "reg"
    (reg / "llm-profiles").mkdir(parents=True)
    (reg / "llm-profiles" / "bad.yaml").write_text(yaml.safe_dump({
        "env": {"KEY": "${file:/no/such/file/anywhere}"},
    }))
    with pytest.raises(llm_profiles.ProfileError) as ei:
        llm_profiles.resolve_profile("bad", reg)
    assert "/no/such/file/anywhere" in str(ei.value)


def test_resolve_failing_cmd_ref_raises_clear_error(tmp_path):
    reg = tmp_path / "reg"
    (reg / "llm-profiles").mkdir(parents=True)
    (reg / "llm-profiles" / "bad.yaml").write_text(yaml.safe_dump({
        "env": {"KEY": "${cmd:exit 7}"},
    }))
    with pytest.raises(llm_profiles.ProfileError) as ei:
        llm_profiles.resolve_profile("bad", reg)
    assert "exited 7" in str(ei.value)


def test_resolve_missing_profile_file_raises_clear_error(tmp_path):
    reg = tmp_path / "reg"
    with pytest.raises(llm_profiles.ProfileError) as ei:
        llm_profiles.resolve_profile("nope", reg)
    assert "nope" in str(ei.value)
    assert "not found" in str(ei.value)


# --------------------------------------------------------------- CLI start
def test_start_llm_named_profile_wires_env_mounts_pi(ctl):
    ctl.write_profile("acme", {
        "description": "acme profile",
        "model": "acme/big-model",
        "fast_model": "acme/small-model",
        "env": {
            "ACME_KEY": "${cmd:echo -n resolved-acme-key}",
        },
        "pi": {
            "providers": {
                "acme": {
                    "baseUrl": "https://gw.example.com",
                    "apiKey": "${env:ACME_KEY}",
                }
            }
        },
        "mounts": ["~/.acmecreds:/home/agent/.acmecreds:ro"],
    })
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "acme",
                  "--run-id", "tst-acme")
    assert res.returncode == 0, res.stderr
    argv = docker_run_argv(ctl)

    ev = env_vars(argv)
    assert "ACME_KEY=resolved-acme-key" in ev

    mv = mount_specs(argv)
    assert any(m.endswith(":/home/agent/.acmecreds:ro") for m in mv)

    # pi fragment landed at the config dir's pi/models.json, mode 0600,
    # with the resolved apiKey (never the ${env:...} literal)
    models_json = ctl.config_dir("tst-acme") / "pi" / "models.json"
    doc = json.loads(models_json.read_text())
    assert doc["providers"]["acme"]["apiKey"] == "resolved-acme-key"
    mode = oct(models_json.stat().st_mode)[-3:]
    assert mode == "600"

    # job.yaml picked up the profile's model/fast_model (no --model flag)
    job_yaml = (ctl.config_dir("tst-acme") / "job.yaml").read_text()
    assert 'model: "acme/big-model"' in job_yaml
    assert 'fast_model: "acme/small-model"' in job_yaml


def test_start_llm_explicit_model_flag_overrides_profile(ctl):
    ctl.write_profile("acme", {"model": "acme/big-model", "env": {}})
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "acme",
                  "--model", "override/model", "--run-id", "tst-override")
    assert res.returncode == 0, res.stderr
    job_yaml = (ctl.config_dir("tst-override") / "job.yaml").read_text()
    assert 'model: "override/model"' in job_yaml
    assert "acme/big-model" not in job_yaml


def test_start_llm_unresolvable_profile_exits_nonzero_before_docker_run(ctl):
    ctl.write_profile("broken", {"env": {"KEY": "${env:NEVER_SET_ANYWHERE_9}"}})
    assert "NEVER_SET_ANYWHERE_9" not in os.environ
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "broken",
                  "--run-id", "tst-broken")
    assert res.returncode != 0
    assert "NEVER_SET_ANYWHERE_9" in res.stderr
    assert docker_run_never_happened(ctl)


def test_start_llm_unknown_profile_name_exits_2(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "no-such-profile",
                  "--run-id", "tst-unknown")
    assert res.returncode == 2
    assert "no-such-profile" in res.stderr
    assert "not found" in res.stderr
    assert docker_run_never_happened(ctl)


def docker_run_never_happened(ctl: Ctl) -> bool:
    return not any(a[:2] == ["run", "-d"] for a in ctl.recorded())


def test_start_llm_host_unaffected(ctl):
    """`--llm host` (the default) must behave exactly as before: no
    profile lookup, no crash even though no llm-profiles/ dir exists."""
    res = ctl.run("start", "--prd", str(ctl.prd), "--run-id", "tst-host")
    assert res.returncode == 0, res.stderr
    argv = docker_run_argv(ctl)
    assert "--label" in argv


def test_start_llm_none_unaffected(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tst-none")
    assert res.returncode == 0, res.stderr
    argv = docker_run_argv(ctl)
    ev = env_vars(argv)
    assert not any(v.startswith("ACME_") for v in ev)
    assert not (ctl.config_dir("tst-none") / "pi").exists()


# ------------------------------------------------------ resolve_profile redact
def test_resolve_profile_redact_masks_env_and_ref_derived_pi_fields(tmp_path, monkeypatch):
    reg = tmp_path / "reg"
    (reg / "llm-profiles").mkdir(parents=True)
    (reg / "llm-profiles" / "acme.yaml").write_text(yaml.safe_dump({
        "model": "acme/big",
        "env": {
            "LITERAL_KEY": "literal-value-not-a-secret-but-masked-anyway",
            "FROM_ENV": "${env:PROBE_REDACT_VAR}",
        },
        "pi": {
            "providers": {
                "acme": {
                    "baseUrl": "https://gw.example.com",  # literal -- stays visible
                    "apiKey": "${env:FROM_ENV}",          # ref-derived -- masked
                }
            }
        },
    }))
    monkeypatch.setenv("PROBE_REDACT_VAR", "super-secret-value-xyz")

    unredacted = llm_profiles.resolve_profile("acme", reg)
    assert unredacted["env"]["FROM_ENV"] == "super-secret-value-xyz"
    assert unredacted["pi"]["providers"]["acme"]["apiKey"] == "super-secret-value-xyz"

    redacted = llm_profiles.resolve_profile("acme", reg, redact=True)
    assert redacted["env"]["LITERAL_KEY"] == llm_profiles.MASK
    assert redacted["env"]["FROM_ENV"] == llm_profiles.MASK
    assert redacted["pi"]["providers"]["acme"]["apiKey"] == llm_profiles.MASK
    # literal, non-ref pi field is preserved for diagnosis
    assert redacted["pi"]["providers"]["acme"]["baseUrl"] == "https://gw.example.com"
    dumped = json.dumps(redacted)
    assert "super-secret-value-xyz" not in dumped
    assert "literal-value-not-a-secret-but-masked-anyway" not in dumped


# ----------------------------------------------------------- CLI llm profiles/show
def test_cli_llm_profiles_lists_builtins_and_files(ctl):
    ctl.write_profile("acme", {"model": "acme/big", "env": {}})
    ctl.write_profile("zeta", {"model": "zeta/small", "env": {}})
    res = ctl.run("llm", "profiles")
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    names = [line.split()[0] for line in lines]
    assert names == ["host", "none", "acme", "zeta"]
    assert "builtin" in lines[0] and "builtin" in lines[1]
    assert "builtin" not in lines[2]


def test_cli_llm_profiles_json_stable_shape(ctl):
    ctl.write_profile("acme", {"model": "acme/big", "env": {}})
    res = ctl.run("--json", "llm", "profiles")
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    assert doc == [
        {"name": "host", "builtin": True},
        {"name": "none", "builtin": True},
        {"name": "acme", "builtin": False},
    ]


def test_cli_llm_show_masks_secret_and_exposes_literals(ctl):
    ctl.write_profile("acme", {
        "description": "acme profile",
        "model": "acme/big-model",
        "fast_model": "acme/small-model",
        "env": {"ACME_KEY": "${cmd:echo -n top-secret-cli-value}"},
        "pi": {
            "providers": {
                "acme": {
                    "baseUrl": "https://gw.example.com",
                    "apiKey": "${env:ACME_KEY}",
                }
            }
        },
        "mounts": ["~/.acmecreds:/home/agent/.acmecreds:ro"],
    })
    res = ctl.run("llm", "show", "acme")
    assert res.returncode == 0, res.stderr
    assert "top-secret-cli-value" not in res.stdout
    assert "***" in res.stdout
    assert "acme/big-model" in res.stdout
    assert "https://gw.example.com" in res.stdout  # literal field stays visible

    res_json = ctl.run("--json", "llm", "show", "acme")
    assert res_json.returncode == 0, res_json.stderr
    assert "top-secret-cli-value" not in res_json.stdout
    doc = json.loads(res_json.stdout)
    assert doc["name"] == "acme"
    assert doc["model"] == "acme/big-model"
    assert doc["env"]["ACME_KEY"] == llm_profiles.MASK
    assert doc["pi"]["providers"]["acme"]["apiKey"] == llm_profiles.MASK
    assert doc["pi"]["providers"]["acme"]["baseUrl"] == "https://gw.example.com"


def test_cli_llm_show_unknown_profile_exits_3(ctl):
    res = ctl.run("llm", "show", "totally-unknown-profile")
    assert res.returncode == 3
    assert "totally-unknown-profile" in res.stderr


def test_cli_llm_show_builtin_profiles(ctl):
    for name in ("host", "none"):
        res = ctl.run("llm", "show", name)
        assert res.returncode == 0, res.stderr
        assert "built-in" in res.stdout


# ---------------------------------------------------------------- llm test

NO_DOCKER = {"RALPHD_DOCKER": "/nonexistent-docker-binary-for-tests"}


def test_llm_test_resolvable_profile_passes_without_docker(ctl):
    ctl.write_profile("good", {"description": "d", "model": "acme/big",
                               "env": {"FOO": "literal-value"}})
    res = ctl.run("llm", "test", "good", env=NO_DOCKER)
    assert res.returncode == 0, res.stderr
    assert "resolves OK" in res.stdout
    assert "docker unavailable" in res.stdout
    assert not ctl.recorded()  # never even got to a `docker run`


def test_llm_test_unresolvable_profile_fails_with_diagnostic_no_docker_needed(ctl):
    ctl.write_profile("bad", {"description": "d",
                              "env": {"FOO": "${env:TOTALLY_UNSET_VAR_XYZ}"}})
    res = ctl.run("llm", "test", "bad", env=NO_DOCKER)
    assert res.returncode == 1
    assert "TOTALLY_UNSET_VAR_XYZ" in res.stderr
    assert "bad" in res.stderr
    assert not ctl.recorded()  # dies before ever touching docker


def test_llm_test_missing_profile_exits_3(ctl):
    res = ctl.run("llm", "test", "no-such-profile", env=NO_DOCKER)
    assert res.returncode == 3
    assert "no-such-profile" in res.stderr


def test_llm_test_builtins_resolve_trivially_without_docker(ctl):
    for name in ("host", "none"):
        res = ctl.run("llm", "test", name, env=NO_DOCKER)
        assert res.returncode == 0, res.stderr
        assert "resolves OK" in res.stdout


def test_llm_test_docker_ping_invocation_shape(ctl):
    ctl.write_profile("acme", {
        "description": "acme",
        "model": "acme/big-model",
        "env": {"ACME_KEY": "top-secret-value"},
        "pi": {"providers": {"acme": {"baseUrl": "https://gw.example.com"}}},
    })
    res = ctl.run("llm", "test", "acme")  # stub docker present via ctl.run()
    assert res.returncode == 0, res.stderr
    assert "ping succeeded" in res.stdout
    runs = [a for a in ctl.recorded() if a[:1] == ["run"]]
    assert len(runs) == 1, ctl.recorded()
    argv = runs[0]
    assert "--rm" in argv
    assert "-d" not in argv  # never detached -- this is a throwaway ping
    assert "--label" in argv
    label_i = argv.index("--label")
    assert argv[label_i + 1] == "ralphd.llm-test=acme"
    assert "--entrypoint" in argv
    ep_i = argv.index("--entrypoint")
    assert argv[ep_i + 1] == "pi"
    assert "-e" in argv
    assert "ACME_KEY=top-secret-value" in env_vars(argv)
    assert argv[-1] == "-p" or "--model" in argv  # pi's own args tail the argv
    assert "acme/big-model" in argv


def test_llm_test_no_ping_flag_skips_container_even_when_docker_available(ctl):
    ctl.write_profile("acme", {"description": "acme", "env": {"FOO": "bar"}})
    res = ctl.run("llm", "test", "acme", "--no-ping")
    assert res.returncode == 0, res.stderr
    assert "--no-ping" in res.stdout
    assert not [a for a in ctl.recorded() if a[:1] == ["run"]]
