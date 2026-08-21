"""The worker and verifier prompts must carry the sandbox-safety rules (#35).

An iteration's shell runs inside the job's own container, as the same uid, next
to the supervisor that owns the iteration. Selecting a target by *pattern*
(`pkill -f ...`, a bare `--filter label=ralphd.run=...`) therefore matches the
loop itself and ends the run mid-iteration. Both agent-facing prompts that get
a shell (worker.md, task-verify.md) must state four rules:

  1. never signal a process by pattern -- only a PID you spawned,
  2. never signal/stop/remove a container, image, volume or shared label you
     did not create,
  3. prefer a shutdown endpoint / closing the socket / an unroutable URL over
     killing anything,
  4. scratch work in /tmp or a throwaway git worktree, never the live workspace.

Each rule gets its own parametrized test per file, so deleting any single rule
from either prompt fails a *named* test rather than a catch-all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts"

# prompts whose agent gets a shell inside the job container
SHELL_PROMPTS = ["worker", "task-verify"]


def _prompt_text(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    assert path.is_file(), f"missing prompt file {path}"
    return path.read_text()


def _assert_all(name: str, text: str, rule: str, patterns: list[str]) -> None:
    missing = [p for p in patterns
               if not re.search(p, text, re.IGNORECASE | re.DOTALL)]
    assert not missing, (
        f"{name}.md is missing the '{rule}' sandbox-safety rule; "
        f"no match for: {missing}")


@pytest.mark.parametrize("name", SHELL_PROMPTS)
def test_prompt_has_a_sandbox_safety_section(name):
    text = _prompt_text(name)
    assert re.search(r"^##\s+Sandbox safety", text, re.MULTILINE), \
        f"{name}.md has no '## Sandbox safety' heading"
    # the reason the rules exist: same container / same user as the supervisor
    _assert_all(name, text, "rationale", [
        r"container", r"same user", r"PID 1",
    ])


@pytest.mark.parametrize("name", SHELL_PROMPTS)
def test_prompt_forbids_signalling_a_process_by_pattern(name):
    _assert_all(name, _prompt_text(name), "never signal by pattern", [
        r"never signal a process by pattern",
        r"pkill",
        r"killall",
        r"pgrep[^\n]*\|[^\n]*xargs kill",
        r"pidof",
        r"only a PID you spawned",
    ])


@pytest.mark.parametrize("name", SHELL_PROMPTS)
def test_prompt_forbids_touching_containers_it_did_not_create(name):
    _assert_all(name, _prompt_text(name), "never touch what you did not create", [
        r"never signal, stop or remove a container, image or volume you did not\s*\n?\s*create",
        r"label this job's own container also",
        r"filter that can only ever match your own",
    ])


@pytest.mark.parametrize("name", SHELL_PROMPTS)
def test_prompt_prefers_a_shutdown_scope_over_killing(name):
    _assert_all(name, _prompt_text(name), "prefer a scope you own", [
        r"prefer a scope you own over killing anything",
        r"clean-shutdown endpoint",
        r"closing the socket",
        r"unroutable",
    ])


@pytest.mark.parametrize("name", SHELL_PROMPTS)
def test_prompt_keeps_scratch_work_out_of_the_live_workspace(name):
    _assert_all(name, _prompt_text(name), "scratch space, not the workspace", [
        r"scratch work in `/tmp",
        r"git worktree",
        r"never in the live\s*\n?\s*workspace tree",
    ])
