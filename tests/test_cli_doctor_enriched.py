"""Black-box tests for the enriched `ralphctl doctor` checks (PRD req 20):
default LLM profile resolution, registry schema, and dangling-container
checks in both directions. Each test invokes the real `ralphctl` executable
with RALPHD_DOCKER pointing at the recording stub (tests/stub-docker/docker)
and RALPHD_REGISTRY at a tmp dir. No CLI internals are imported.
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


class Ctl:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.registry = tmp / "registry"
        self.registry.mkdir()
        self.log = tmp / "docker-argv.jsonl"

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

    def doctor(self, env: dict | None = None) -> dict:
        res = self.run("--json", "doctor", env=env)
        return json.loads(res.stdout)


@pytest.fixture
def ctl(tmp_path):
    return Ctl(tmp_path)


def _base_env() -> dict:
    """Makes the docker/image/registry/pi_host_config checks pass so a test
    can isolate the new checks without noise (image inspect always exits 0
    for the stub; pi_host_config only depends on $HOME which pytest's tmp
    HOME already lacks -- fine, we don't assert on it)."""
    return {"STUB_DOCKER_INSPECT_OK": "1"}


def test_doctor_default_profile_host_resolves_trivially(ctl):
    """No config.yaml at all -> default profile is the builtin 'host',
    which always 'resolves' (nothing to look up)."""
    doc = ctl.doctor(env=_base_env())
    assert doc["checks"]["default_llm_profile"] is True
    assert doc["defaultLlmProfile"] == "host"
    assert doc["defaultLlmProfileError"] is None


def test_doctor_unresolvable_default_profile_fails_check(ctl):
    (ctl.registry / "config.yaml").write_text(
        yaml.safe_dump({"default_llm_profile": "broken"}))
    pdir = ctl.registry / "llm-profiles"
    pdir.mkdir()
    (pdir / "broken.yaml").write_text(yaml.safe_dump(
        {"env": {"SECRET": "${env:TOTALLY_UNSET_VAR_XYZ}"}}))
    doc = ctl.doctor(env=_base_env())
    assert doc["checks"]["default_llm_profile"] is False
    assert doc["defaultLlmProfile"] == "broken"
    assert "TOTALLY_UNSET_VAR_XYZ" in doc["defaultLlmProfileError"]
    assert doc["ok"] is False


def test_doctor_resolvable_named_default_profile_passes(ctl):
    (ctl.registry / "config.yaml").write_text(
        yaml.safe_dump({"default_llm_profile": "fine"}))
    pdir = ctl.registry / "llm-profiles"
    pdir.mkdir()
    (pdir / "fine.yaml").write_text(yaml.safe_dump(
        {"model": "some-model", "env": {"LITERAL": "value"}}))
    doc = ctl.doctor(env=_base_env())
    assert doc["checks"]["default_llm_profile"] is True
    assert doc["defaultLlmProfileError"] is None


def test_doctor_malformed_llm_profile_fails_registry_schema(ctl):
    pdir = ctl.registry / "llm-profiles"
    pdir.mkdir()
    (pdir / "corrupt.yaml").write_text("not: valid: yaml: [[[")
    doc = ctl.doctor(env=_base_env())
    assert doc["checks"]["registry_schema"] is False
    assert any("corrupt.yaml" in issue for issue in doc["registryIssues"])
    assert doc["ok"] is False


def test_doctor_schema_version_too_new_fails_registry_schema(ctl):
    rdir = ctl.registry / "runs" / "future-run"
    rdir.mkdir(parents=True)
    (rdir / "status.json").write_text(json.dumps({"schemaVersion": 999}))
    doc = ctl.doctor(env=_base_env())
    assert doc["checks"]["registry_schema"] is False
    assert any("future-run" in issue and "999" in issue
               for issue in doc["registryIssues"])


def test_doctor_clean_registry_passes_schema_check(ctl):
    rdir = ctl.registry / "runs" / "ok-run"
    rdir.mkdir(parents=True)
    (rdir / "status.json").write_text(json.dumps({"schemaVersion": 1, "state": "succeeded"}))
    doc = ctl.doctor(env=_base_env())
    assert doc["checks"]["registry_schema"] is True
    assert doc["registryIssues"] == []


def test_doctor_stray_container_reported_non_fatal(ctl):
    """Direction 1 (pre-existing): a labeled container with no run dir at
    all. Kept here to prove it still coexists with the new checks and stays
    report-only."""
    (ctl.registry / "runs" / "live-run").mkdir(parents=True)
    labels = {"c-live": "live-run", "c-stray": "gone-run"}
    env = {**_base_env(), "STUB_DOCKER_PS_IDS": "c-live,c-stray",
           "STUB_DOCKER_INSPECT_LABELS": json.dumps(labels)}
    doc = ctl.doctor(env=env)
    assert doc["strayContainers"] == [{"id": "c-stray", "runId": "gone-run"}]
    assert doc["ok"] == all(doc["checks"].values())


def test_doctor_dangling_registry_entry_reported_non_fatal(ctl):
    """Direction 2 (new): a run dir recorded as `state: running` whose
    container no longer exists at all -- the reverse of a stray container.
    Never affects `ok`."""
    rdir = ctl.registry / "runs" / "orphaned-run"
    rdir.mkdir(parents=True)
    (rdir / "status.json").write_text(json.dumps({"state": "running", "schemaVersion": 1}))
    # STUB_DOCKER_CONTAINERS deliberately omits ralphd-orphaned-run, so
    # `docker inspect --format {{.State.Running}}` on it exits nonzero.
    env = {**_base_env(), "STUB_DOCKER_CONTAINERS": "some-other-container"}
    doc = ctl.doctor(env=env)
    assert doc["danglingRegistryEntries"] == [
        {"runId": "orphaned-run", "container": "ralphd-orphaned-run"}]
    assert doc["ok"] == all(doc["checks"].values())


def test_doctor_running_registry_entry_with_live_container_not_dangling(ctl):
    rdir = ctl.registry / "runs" / "alive-run"
    rdir.mkdir(parents=True)
    (rdir / "status.json").write_text(json.dumps({"state": "running", "schemaVersion": 1}))
    env = {**_base_env(), "STUB_DOCKER_CONTAINERS": "ralphd-alive-run",
           "STUB_DOCKER_RUNNING": "ralphd-alive-run"}
    doc = ctl.doctor(env=env)
    assert doc["danglingRegistryEntries"] == []


def test_doctor_json_shape_stable(ctl):
    doc = ctl.doctor(env=_base_env())
    for key in ("ok", "checks", "strayContainers", "danglingRegistryEntries",
                "registryIssues", "defaultLlmProfile", "defaultLlmProfileError"):
        assert key in doc
    for key in ("docker", "image", "registry", "pi_host_config",
                "default_llm_profile", "registry_schema"):
        assert key in doc["checks"]
