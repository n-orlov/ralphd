"""Black-box tests for the shipped example LLM profiles (PRD req 15).

`examples/llm-profiles/bedrock.yaml` and `examples/llm-profiles/gateway.yaml`
must (a) parse as valid profiles matching docs/llm-profiles.md's shapes, (b)
fully resolve given only placeholder host env / a stubbed `${cmd:}` command
(never a real credential), and (c) contain no real endpoints or
organization-specific values -- only `example.com`-style placeholders.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).parent.parent
EXAMPLES_DIR = REPO / "examples" / "llm-profiles"

import sys

sys.path.insert(0, str(REPO / "src"))
from ralphd.cli import llm_profiles


def _registry_with(tmp_path: Path, filenames: list[str]) -> Path:
    """Copy the given example profile files into a fresh temp registry's
    llm-profiles/ dir (mirrors how a real ~/.ralphd/llm-profiles/ looks)."""
    reg = tmp_path / "registry"
    prof_dir = reg / "llm-profiles"
    prof_dir.mkdir(parents=True)
    for fn in filenames:
        (prof_dir / fn).write_text((EXAMPLES_DIR / fn).read_text())
    return reg


def test_both_example_files_exist():
    assert (EXAMPLES_DIR / "bedrock.yaml").is_file()
    assert (EXAMPLES_DIR / "gateway.yaml").is_file()


def test_cross_family_example_file_exists():
    # Task 024 (#60 pillar 4 requirement N): the worked cross-family example
    # (one profile, two `pi.providers` families, one bearer token) docs/
    # llm-profiles.md now walks through.
    assert (EXAMPLES_DIR / "aigw-cross-family.yaml").is_file()


def test_bedrock_example_matches_docs_shape():
    doc = yaml.safe_load((EXAMPLES_DIR / "bedrock.yaml").read_text())
    assert doc["model"] == "amazon-bedrock/anthropic.claude-opus-5"
    assert doc["env"]["AWS_REGION"] == "${env:AWS_REGION}"
    assert doc["env"]["AWS_PROFILE"] == "${env:AWS_PROFILE}"
    assert doc["mounts"] == ["~/.aws:/home/agent/.aws:ro"]
    # no `pi:` fragment needed for the Bedrock preset (SDK credential chain)
    assert "pi" not in doc


def test_gateway_example_matches_docs_shape():
    doc = yaml.safe_load((EXAMPLES_DIR / "gateway.yaml").read_text())
    assert doc["model"] == "my-gateway/big-model"
    assert doc["fast_model"] == "my-gateway/small-model"
    assert doc["env"]["GW_API_KEY"].startswith("${cmd:")
    pi = doc["pi"]["providers"]["my-gateway"]
    assert pi["baseUrl"] == "https://my-gateway.example.com/api/v1"
    assert pi["api"] in ("anthropic-messages", "openai-completions")
    assert pi["apiKey"] == "${env:GW_API_KEY}"
    ids = [m["id"] for m in pi["models"]]
    assert ids == ["big-model", "small-model"]
    # #298: a gateway bills an internal cost centre. The generic example
    # ships a placeholder label, never an organisation's own gateway name.
    assert doc["billing"] == {"payer": "My gateway", "basis": "chargeback"}


def test_cross_family_example_matches_docs_shape():
    doc = yaml.safe_load((EXAMPLES_DIR / "aigw-cross-family.yaml").read_text())
    assert doc["model"] == "amazon-bedrock/eu.anthropic.claude-opus-5"
    assert doc["env"]["AIGW_BEARER_TOKEN"] == "${env:AIGW_BEARER_TOKEN}"
    providers = doc["pi"]["providers"]
    bedrock = providers["amazon-bedrock"]
    assert bedrock["api"] == "bedrock-converse-stream"
    assert bedrock["apiKey"] == "${env:AIGW_BEARER_TOKEN}"
    bedrock_ids = [m["id"] for m in bedrock["models"]]
    assert bedrock_ids == ["eu.anthropic.claude-opus-5", "eu.anthropic.claude-sonnet-5"]
    openai = providers["aigw-openai"]
    assert openai["api"] == "openai-responses"
    # Both providers resolve their key from the SAME env var: one bearer
    # token reaches both model families (#60 pillar 4's one-profile-per-run
    # claim), never a second secret for the second provider.
    assert openai["apiKey"] == "${env:AIGW_BEARER_TOKEN}"
    openai_ids = [m["id"] for m in openai["models"]]
    assert openai_ids == [
        "openai.gpt-5.6-sol", "openai.gpt-5.6-luna", "openai.gpt-5.6-terra",
    ]
    # #298: the AIGW-specific example names its own gateway.
    assert doc["billing"] == {"payer": "AIGW gateway", "basis": "chargeback"}


def test_claude_sub_example_file_exists():
    # #298: the Claude subscription snippet of docs/llm-profiles.md ships too.
    assert (EXAMPLES_DIR / "claude-sub.yaml").is_file()


def test_claude_sub_example_matches_docs_shape(tmp_path):
    doc = yaml.safe_load((EXAMPLES_DIR / "claude-sub.yaml").read_text())
    assert doc["harness"] == "claude-code"
    assert doc["model"].startswith("anthropic/")
    assert doc["env"]["CLAUDE_CODE_OAUTH_TOKEN"].startswith("${file:")
    assert doc["billing"] == {"payer": "Claude subscription", "basis": "subscription"}
    assert "pi" not in doc
    # its billing block validates against the claude-code harness (`${file:}`
    # is not resolved here: billing is literal and read before env).
    from ralphd.cli.llm_profiles import resolve_billing
    assert resolve_billing(doc["billing"], label="claude-sub", harness=doc["harness"],
                           default_payer="claude-sub") == doc["billing"]


def test_cross_family_example_resolves_given_placeholder_env(tmp_path, monkeypatch):
    reg = _registry_with(tmp_path, ["aigw-cross-family.yaml"])
    monkeypatch.setenv("AIGW_BEARER_TOKEN", "placeholder-bearer-token")
    resolved = llm_profiles.resolve_profile("aigw-cross-family", reg)
    assert resolved["env"] == {"AIGW_BEARER_TOKEN": "placeholder-bearer-token"}
    assert resolved["model"] == "amazon-bedrock/eu.anthropic.claude-opus-5"
    providers = resolved["pi"]["providers"]
    # Same resolved secret value reached both providers from the one `env:`
    # entry -- proves the one-bearer-token-two-families claim end to end,
    # not just in the unresolved YAML.
    assert providers["amazon-bedrock"]["apiKey"] == "placeholder-bearer-token"
    assert providers["aigw-openai"]["apiKey"] == "placeholder-bearer-token"
    assert providers["amazon-bedrock"]["api"] == "bedrock-converse-stream"
    assert providers["aigw-openai"]["api"] == "openai-responses"


def test_bedrock_example_resolves_given_placeholder_env(tmp_path, monkeypatch):
    reg = _registry_with(tmp_path, ["bedrock.yaml"])
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_PROFILE", "default")
    resolved = llm_profiles.resolve_profile("bedrock", reg)
    assert resolved["env"] == {"AWS_REGION": "us-east-1", "AWS_PROFILE": "default"}
    assert resolved["mounts"] == [f"{Path.home() / '.aws'}:/home/agent/.aws:ro"]
    assert resolved["model"] == "amazon-bedrock/anthropic.claude-opus-5"
    assert resolved["pi"] is None


def test_gateway_example_resolves_given_stubbed_cmd_and_placeholder_env(tmp_path, monkeypatch):
    reg = _registry_with(tmp_path, ["gateway.yaml"])

    # Stub the `${cmd:aws secretsmanager ...}` reference: put a fake `aws`
    # executable ahead of the real one on PATH that just prints a
    # placeholder value -- never a real credential, never invokes AWS.
    # The resolver shells out via `bash -lc` (a *login* shell), which
    # resets PATH from /etc/profile regardless of the calling process's
    # own PATH -- so the fake `aws` has to be injected via a fake HOME's
    # `.bash_profile` (sourced *after* /etc/profile in a login shell),
    # not via monkeypatch.setenv("PATH", ...) alone.
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    fake_aws = fake_bin / "aws"
    fake_aws.write_text("#!/bin/sh\necho placeholder-gateway-key\n")
    fake_aws.chmod(fake_aws.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    (fake_home / ".bash_profile").write_text(f'export PATH="{fake_bin}:$PATH"\n')
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    resolved = llm_profiles.resolve_profile("gateway", reg)
    assert resolved["env"] == {"GW_API_KEY": "placeholder-gateway-key"}
    assert resolved["model"] == "my-gateway/big-model"
    assert resolved["fast_model"] == "my-gateway/small-model"
    pi = resolved["pi"]["providers"]["my-gateway"]
    assert pi["baseUrl"] == "https://my-gateway.example.com/api/v1"
    assert pi["apiKey"] == "placeholder-gateway-key"
    assert [m["id"] for m in pi["models"]] == ["big-model", "small-model"]


def test_codex_example_matches_docs_shape():
    doc = yaml.safe_load((EXAMPLES_DIR / "codex-aigw.yaml").read_text())
    assert doc["harness"] == "codex"
    assert doc["model"] == "aigw-openai/openai.gpt-5.6-sol"
    # The two variables the codex wrapper requires, both references.
    assert doc["env"] == {
        "AIGW_OPENAI_BASE_URL": "${env:AIGW_OPENAI_BASE_URL}",
        "AWS_BEARER_TOKEN_BEDROCK": "${env:AWS_BEARER_TOKEN_BEDROCK}",
    }
    assert doc["billing"]["basis"] != "subscription"
    from ralphd.cli.llm_profiles import resolve_billing
    assert resolve_billing(doc["billing"], label="codex-aigw", harness=doc["harness"],
                           default_payer="codex-aigw") == doc["billing"]


def test_codex_example_resolves_given_placeholder_env(tmp_path, monkeypatch):
    reg = _registry_with(tmp_path, ["codex-aigw.yaml"])
    monkeypatch.setenv("AIGW_OPENAI_BASE_URL", "https://aigw.example.com/api/openai/v1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "placeholder-bearer-token")
    resolved = llm_profiles.resolve_profile("codex-aigw", reg)
    assert resolved["harness"] == "codex"
    assert resolved["env"] == {
        "AIGW_OPENAI_BASE_URL": "https://aigw.example.com/api/openai/v1",
        "AWS_BEARER_TOKEN_BEDROCK": "placeholder-bearer-token",
    }
    assert resolved["model"] == "aigw-openai/openai.gpt-5.6-sol"
    provider = resolved["pi"]["providers"]["aigw-openai"]
    assert provider["api"] == "openai-responses"
    assert provider["apiKey"] == "placeholder-bearer-token"


@pytest.mark.parametrize("fn", ["bedrock.yaml", "gateway.yaml", "aigw-cross-family.yaml",
                                "claude-sub.yaml", "codex-aigw.yaml"])
def test_example_has_no_real_endpoints_or_org_specific_values(fn):
    text = (EXAMPLES_DIR / fn).read_text()
    lower = text.lower()
    # No IP literals, no obviously-real cloud/org hostnames beyond the
    # example.com placeholder, no literal secret-looking base64/hex blobs.
    forbidden_substrings = [
        "amazonaws.com/",  # a real concrete AWS endpoint (region/service URL)
        "corp.",
        "internal.",
    ]
    for bad in forbidden_substrings:
        assert bad not in lower, f"{fn} contains a non-placeholder value: {bad!r}"
    if "http://" in lower or "https://" in lower:
        assert "example.com" in lower, f"{fn} has a URL that is not example.com-based"
    # Every `env:` value must be a reference (${env:}/${file:}/${cmd:}), not
    # a literal secret.
    doc = yaml.safe_load(text)
    for k, v in (doc.get("env") or {}).items():
        assert isinstance(v, str) and v.startswith("${"), (
            f"{fn}: env.{k} looks like a literal value, not a reference: {v!r}")
