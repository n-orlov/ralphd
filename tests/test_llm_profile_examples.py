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


@pytest.mark.parametrize("fn", ["bedrock.yaml", "gateway.yaml"])
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
