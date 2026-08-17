"""Black-box tests for the "toolchain in a sibling" capability guidance.

The engine image is deliberately thin (Python/Node) and the agent is non-root,
so any job needing a toolchain the image lacks (Go, Rust, a JDK, tmux, a
database) must run that work in a SIBLING container with the HOST workspace
bind-mounted. That is a prompt-level capability, not something each PRD should
have to explain.

Proves:
  - every phase prompt of a job started with `--allow-docker` (i.e. with the
    RALPHD_HOST_* env vars set) carries the recipe: build from a repo-committed
    `ci/Dockerfile`, run `--rm --user 1000:1000` siblings against the HOST
    workspace path, cache in a named volume, bridge network / no credentials,
  - none of it appears when the capability is off (no docker socket granted),
  - the guidance steers away from the run-id-locked cache volume anti-pattern
    (a cache volume named/gated per run breaks every subsequent run),
  - the shipped `examples/skills/toolchain-sibling/` skill is a valid mountable
    skill whose `run.sh` embodies the same rules,
  - the docs name the pattern and its failure modes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from test_e2e import EngineProc

REPO = Path(__file__).parent.parent
SKILL_DIR = REPO / "examples" / "skills" / "toolchain-sibling"

HOST_ENV = {"RALPHD_HOST_WORKSPACE": "/host/path/ws",
            "RALPHD_HOST_RUN_DIR": "/host/path/run",
            "RALPHD_RUN_ID": "tc-sib"}

# Each marker is one load-bearing fact of the proven pattern.
RECIPE_MARKERS = [
    "ci/Dockerfile",          # sibling image defined in the TARGET repo
    "ci/run.sh",              # thin wrapper, also in the target repo
    "--user 1000:1000",       # or the workspace fills up with root-owned files
    "docker run --rm",        # throwaway siblings
    "$RALPHD_HOST_WORKSPACE",  # HOST path, not this container's /workspace
    "named volume",           # cache, or every run re-downloads deps
    "bridge",                 # siblings' network, whatever the job's is
    "tmux",                   # proven toolchain examples
    "pty",
]


@pytest.fixture
def engine(tmp_path):
    procs: list[EngineProc] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> EngineProc:
        defaults = {"run_id": "tc-sib", "iterations": 3, "max_approaches": 1,
                    "on_complete": "exit"}
        e = EngineProc(tmp_path, {**defaults, **(job or {})}, stub_env)
        procs.append(e)
        return e

    yield make
    for e in procs:
        e.stop()


def _prompts(e: EngineProc) -> list[str]:
    all_prompts = (e.run_dir / ".stub-all-prompts").read_text()
    return [p for p in all_prompts.split("===PROMPT===") if p.strip()]


def test_every_prompt_carries_the_sibling_toolchain_recipe(engine):
    e = engine(stub_env=HOST_ENV)
    e.proc.wait(timeout=60)
    prompts = _prompts(e)
    assert prompts
    for p in prompts:
        assert "## Docker siblings" in p
        for marker in RECIPE_MARKERS:
            assert marker in p, f"prompt missing recipe marker {marker!r}"
        # the mount example must use the host path, never the container's view
        assert "-v $RALPHD_HOST_WORKSPACE:/workspace" in p


def test_no_sibling_toolchain_recipe_without_docker_access(engine):
    """Capability off (no --allow-docker) -> none of the guidance is spent."""
    e = engine()
    e.proc.wait(timeout=60)
    for p in _prompts(e):
        assert "Docker siblings" not in p
        assert "--user 1000:1000" not in p
        assert "ci/run.sh" not in p


def test_prompt_warns_against_run_id_locked_cache_volume(engine):
    e = engine(stub_env=HOST_ENV)
    e.proc.wait(timeout=60)
    for p in _prompts(e):
        low = p.lower()
        assert "conditional on `$ralphd_run_id`" in low or "run label off it" in low
        # the sanctioned alternative is stated, not just the prohibition
        assert "docker volume rm" in low


def test_example_skill_is_a_valid_mountable_skill():
    assert (SKILL_DIR / "SKILL.md").is_file()
    run_sh = SKILL_DIR / "run.sh"
    assert run_sh.is_file()
    assert run_sh.stat().st_mode & 0o111, "run.sh must be executable"
    text = (SKILL_DIR / "SKILL.md").read_text()
    assert text.startswith("---"), "SKILL.md needs name/description frontmatter"
    assert "--allow-docker" in text
    for marker in ("--user 1000:1000", "$RALPHD_HOST_WORKSPACE", "ci/Dockerfile"):
        assert marker in text, f"SKILL.md missing {marker!r}"


def test_example_run_sh_cache_volume_is_shared_not_run_scoped():
    text = (SKILL_DIR / "run.sh").read_text()
    assert "--user 1000:1000" in text
    assert "RALPHD_HOST_WORKSPACE" in text
    cache_lines = [ln for ln in text.splitlines()
                   if re.match(r"\s*CACHE_VOL=", ln)]
    assert cache_lines, "run.sh must define a cache volume name"
    for ln in cache_lines:
        assert "RALPHD_RUN_ID" not in ln, (
            "the cache volume must not be scoped to one run: "
            f"{ln!r}")
    # the volume must not be labeled with the run either (labels get reaped)
    create = [ln for ln in text.splitlines() if "volume create" in ln]
    assert create and all("--label" not in ln for ln in create)


def test_docs_name_the_pattern_and_its_failure_modes():
    arch = (REPO / "docs" / "architecture.md").read_text()
    assert "### Toolchain in a sibling" in arch
    section = arch.split("### Toolchain in a sibling", 1)[1]
    section = section.split("\n## ", 1)[0].lower()
    for marker in ("--user 1000:1000", "empty", "named cache volume",
                   "bridge", "tmux", "pty"):
        assert marker in section, f"architecture.md section missing {marker!r}"
    cli = (REPO / "docs" / "cli.md").read_text()
    assert "Toolchain in a sibling" in cli
    assert "examples/skills/toolchain-sibling" in cli
