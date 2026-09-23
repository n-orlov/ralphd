"""Task 029 (PRD requirement I.2's content, filed in `tasks.json` under the
label "I.1"): the task-boundary layer of the git-sink bound (#210).

`redact.py` scrubs known secret VALUES from everything the engine persists
or serves -- output, events, logs. A git commit the worker writes and
pushes is neither: nothing on that path ever runs a commit's message or
diff content through `redact.scrub_text`. This module is that missing
scrub, applied to "the commits this run created" at the task boundary --
i.e. the range between a `before` HEAD (read immediately before a worker
iteration is spawned, `loop.py`'s own `head_before`) and an `after` HEAD
(read immediately once it returns, `head_after`), the exact bracketing
`LoopSupervisor._step_worker` already takes for progress-witness purposes
(`_workspace_head_sha`).

Two functions matter to callers:

- `scan_commits_for_secrets` -- the pure detector. VALUE matching only
  (PRD requirement I.4): a plain substring test against a `value -> label`
  map (the same shape `redact.build_redaction_map()` returns), never a
  regex over a "looks like a token" shape. A diff or doc that merely
  discusses a token's shape, or a fixture holding a synthetic
  token-SHAPED string that is not itself a configured value, never
  matches -- only the literal value does.
- `push_if_clean` -- the gate. Scans first; only when the scan comes back
  clean does it ever start a `git push` subprocess. On a hit, no push
  subprocess is started at all -- not with a warning, not with a
  disclosure (PRD requirement I.2) -- and the returned refusal text names
  the secret's variable NAME only, never the source prefix and never the
  matched value itself (the same bound I.1's hook layer states for
  itself, task 031).

Fails OPEN on a read it cannot complete (no `git`, not a repository, an
unreadable commit) -- the same posture `loop.py`'s sibling helpers
(`_workspace_head_sha`, `_workspace_diff_paths`) already take for an
advisory git read: an unreadable scan is a defect to go fix, never a
silent reason to block every push forever. It never touches the redaction
map's own storage or the live in-memory map by itself -- callers pass
`secrets`, or leave it to default to `redact.build_redaction_map()`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import redact

_GIT_TIMEOUT_S = 30


def _run_git(workspace, *args: str, timeout: float = _GIT_TIMEOUT_S):
    try:
        return subprocess.run(
            ["git", "-C", str(workspace), *args],
            capture_output=True, text=True, timeout=timeout)
    except OSError:
        return None


def commits_between(workspace, base_sha: str | None, head_sha: str) -> list[str]:
    """Commit shas strictly after `base_sha` up to and including
    `head_sha`, oldest first. `base_sha=None` (no prior boundary -- the
    workspace's very first task) scans every ancestor of `head_sha`.
    Empty list, never `None`, on any read failure -- the caller's loop
    over an empty list is trivially a no-hit, which is the fail-open
    posture this whole module keeps."""
    if not head_sha:
        return []
    rev_range = f"{base_sha}..{head_sha}" if base_sha else head_sha
    result = _run_git(workspace, "log", "--format=%H", "--reverse", rev_range)
    if result is None or result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def commit_message(workspace, sha: str) -> str:
    result = _run_git(workspace, "log", "-1", "--format=%B", sha)
    if result is None or result.returncode != 0:
        return ""
    return result.stdout


def commit_diff(workspace, sha: str) -> str:
    """The full patch this single commit introduced -- `--no-renames` (the
    same rationale `_workspace_diff_paths` documents) so a renamed-away
    file's own old content, not only the rename pair, is still scanned."""
    result = _run_git(
        workspace, "show", "--no-color", "--format=", "--no-renames", sha)
    if result is None or result.returncode != 0:
        return ""
    return result.stdout


def _variable_name(label: str) -> str:
    """The variable NAME half of a redaction-map label. `redact.py`'s
    `value -> label` map spells labels `"env:NAME"`, `"<file>.env:NAME"`,
    `"models.json:NAME"`, etc; the refusal text names only the part after
    the last `:` -- never the source prefix, and never the value."""
    if not label:
        return label
    return label.rsplit(":", 1)[-1]


def scan_commits_for_secrets(workspace, base_sha: str | None, head_sha: str,
                             secrets: dict[str, str] | None = None) -> dict | None:
    """Scan every commit in `commits_between(base_sha, head_sha)`, message
    THEN diff, for a VALUE match against `secrets` (defaults to
    `redact.build_redaction_map()`, memory-only, never persisted).

    Returns `None` on no hit. On a hit, returns the FIRST one found
    (oldest commit first, message before that same commit's diff):
    `{"sha": <full sha>, "part": "message" | "diff", "variable": <name>}`.
    """
    if secrets is None:
        secrets = redact.build_redaction_map()
    if not secrets:
        return None
    items = sorted(secrets.items(), key=lambda kv: -len(kv[0]))
    for sha in commits_between(workspace, base_sha, head_sha):
        message = commit_message(workspace, sha)
        for value, label in items:
            if value and value in message:
                return {"sha": sha, "part": "message",
                        "variable": _variable_name(label)}
        diff = commit_diff(workspace, sha)
        for value, label in items:
            if value and value in diff:
                return {"sha": sha, "part": "diff",
                        "variable": _variable_name(label)}
    return None


def refusal_text(hit: dict) -> str:
    """The refusal's own text. Names the variable only -- never the
    matched value (PRD requirement I.1/I.2's shared bound)."""
    sha = str(hit.get("sha", ""))[:12]
    return (
        f"refusing to push: commit {sha}'s {hit.get('part')} matches a "
        f"known secret value for variable {hit.get('variable')!r}"
    )


def push_if_clean(workspace, base_sha: str | None, head_sha: str, *,
                  secrets: dict[str, str] | None = None,
                  remote: str = "origin", branch: str = "main",
                  timeout: float = _GIT_TIMEOUT_S):
    """The task-boundary gate. Scans first; a `git push` subprocess is
    started ONLY when the scan comes back clean -- on a hit, no push
    subprocess is ever started, not with a warning, not with a
    disclosure.

    Returns `(pushed: bool, hit: dict | None, refusal: str | None)`.
    """
    hit = scan_commits_for_secrets(workspace, base_sha, head_sha, secrets=secrets)
    if hit is not None:
        return False, hit, refusal_text(hit)
    subprocess.run(
        ["git", "-C", str(workspace), "push", remote, branch],
        capture_output=True, text=True, timeout=timeout)
    return True, None, None
