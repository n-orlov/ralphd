"""Task 035 (#7): the prompt must teach the sibling-ONLY cleanup idiom.

The job container carries `ralphd.run=<run-id>` exactly like the siblings the
agent starts -- that is how `ralphctl stop`/`rm` reap a whole run in one query.
So the obvious "tidy up my containers" one-liner filtered on the run label
alone removes the container the agent is running in: the run dies
mid-iteration, the iteration's work and transcript are lost, and the run dir is
left non-terminal (observed in a real job).

Proves the rendered prompt carries, whenever docker access is granted:
  - siblings labeled `ralphd.role=sibling` in addition to the run label,
  - the safe two-filter cleanup form (run label AND role=sibling),
  - the prohibition on the run-label-only form *with the reason*,
  - reaping being ralphctl's job, and
  - `RALPHD_SELF_CONTAINER_ID` named as the id never to touch,
and none of it when the capability is off.
"""

from __future__ import annotations

import pytest
from test_e2e import EngineProc

from ralphd.engine.loop import LoopSupervisor

HOST_ENV = {"RALPHD_HOST_WORKSPACE": "/host/path/ws",
            "RALPHD_HOST_RUN_DIR": "/host/path/run",
            "RALPHD_RUN_ID": "sib-clean",
            "RALPHD_SELF_CONTAINER_ID": "ralphd-sib-clean"}

SAFE_FILTER = ("--filter label=ralphd.run=$RALPHD_RUN_ID "
               "--filter label=ralphd.role=sibling")

# Each marker is one load-bearing half of the rule (what + why).
CLEANUP_MARKERS = [
    "--label ralphd.role=sibling",   # siblings are distinguishable...
    SAFE_FILTER,                     # ...so cleanup can exclude the job
    "ralphd.role=job",               # what THIS container is labeled
    "kills the run mid-iteration",   # the why, not just the prohibition
    "$RALPHD_SELF_CONTAINER_ID",     # the id never to touch
]


@pytest.fixture
def engine(tmp_path):
    procs: list[EngineProc] = []

    def make(stub_env: dict | None = None) -> EngineProc:
        e = EngineProc(tmp_path, {"run_id": "sib-clean", "iterations": 2,
                                  "max_approaches": 1, "on_complete": "exit"},
                       stub_env)
        procs.append(e)
        return e

    yield make
    for e in procs:
        e.stop()


def _prompts(e: EngineProc) -> list[str]:
    all_prompts = (e.run_dir / ".stub-all-prompts").read_text()
    return [p for p in all_prompts.split("===PROMPT===") if p.strip()]


def test_every_prompt_forbids_the_run_label_only_cleanup(engine):
    e = engine(stub_env=HOST_ENV)
    e.proc.wait(timeout=60)
    prompts = _prompts(e)
    assert prompts
    for p in prompts:
        assert "## Docker siblings" in p
        for marker in CLEANUP_MARKERS:
            assert marker in p, f"prompt missing cleanup marker {marker!r}"
        # the prohibition is explicit, and reaping is named as ralphctl's job
        low = p.lower()
        assert "never clean up by the run label alone" in low
        assert "ralphctl`'s job" in low or "ralphctl's job" in low


def test_sibling_run_example_carries_both_labels(engine):
    """The copy-pasteable `docker run` recipe must itself set role=sibling,
    otherwise the safe cleanup filter would never match anything."""
    e = engine(stub_env=HOST_ENV)
    e.proc.wait(timeout=60)
    for p in _prompts(e):
        run_lines = [ln for ln in p.splitlines() if "docker run --rm" in ln]
        assert run_lines
        for ln in run_lines:
            assert "--label ralphd.run=$RALPHD_RUN_ID" in ln
            assert "--label ralphd.role=sibling" in ln


def test_no_cleanup_guidance_without_docker_access(engine):
    e = engine()
    e.proc.wait(timeout=60)
    for p in _prompts(e):
        assert "Docker siblings" not in p
        assert "ralphd.role=sibling" not in p
        assert "RALPHD_SELF_CONTAINER_ID" not in p


def test_note_names_this_containers_own_id(monkeypatch):
    """Fast render: the self-container id is interpolated when known."""
    for k, v in HOST_ENV.items():
        monkeypatch.setenv(k, v)
    note = LoopSupervisor._docker_siblings_note()
    assert "`$RALPHD_SELF_CONTAINER_ID` (= `ralphd-sib-clean`)" in note
    assert SAFE_FILTER in note
    monkeypatch.delenv("RALPHD_SELF_CONTAINER_ID")
    note = LoopSupervisor._docker_siblings_note()
    assert "$RALPHD_SELF_CONTAINER_ID" in note      # still named...
    assert "ralphd-sib-clean" not in note           # ...without a stale value
