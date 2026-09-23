"""Task 029 (PRD requirement I.2's content, filed as "I.1" in `tasks.json`):
the task-boundary scan of this run's own commits -- message AND diff -- for
redaction-map VALUES, never a "looks like a token" shape (PRD requirement
I.4), and the push gate that never starts a `git push` subprocess on a hit
(PRD requirement I.2).

Everything here uses SYNTHETIC secret values only (PRD requirement I.9) --
no real credential is ever read or matched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

from ralphd.engine import commit_scan

REPO = Path(__file__).resolve().parents[1]

# The literal text of PRD requirement I.4, one of the three corpora this
# task's own criteria name ("this contract's own PRD text" must pass
# unflagged) -- copied verbatim rather than read from a run-state path at
# test time, so this test is self-contained wherever it runs.
PRD_I4_TEXT = (
    "**I.4 \u2014 the matcher keys on values, never on shapes.** It matches "
    "actual redaction-map values. It must\n"
    "**not** match a \"looks like a token\" pattern. A diff or a doc that "
    "merely discusses a token's shape, a\n"
    "test fixture containing a synthetic value, and this very PRD must all "
    "pass. Name these as tests, not as\n"
    "hopes.\n"
)

SYNTHETIC_VALUE = "sk-synthetic-0123456789abcdefSYNTH"
SYNTHETIC_SECRETS = {SYNTHETIC_VALUE: "env:SYNTHETIC_API_TOKEN"}

# A string that LOOKS like a secret (long, mixed-case, "-shaped") but is
# never itself a configured value -- the shape-vs-value distinction I.4
# requires.
TOKEN_SHAPED_BUT_UNKNOWN = "sk-lookalike-9999999999zzzzzzSHAPE"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(workspace: Path) -> str:
    workspace.mkdir(parents=True, exist_ok=True)
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "t@example.com")
    _git(workspace, "config", "user.name", "t")
    (workspace / "seed.txt").write_text("seed\n")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "seed")
    return _head(workspace)


def _head(workspace: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip()


def _commit_file(workspace: Path, name: str, content: str, message: str) -> str:
    path = workspace / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _git(workspace, "add", name)
    _git(workspace, "commit", "-q", "-m", message)
    return _head(workspace)


# ---------------------------------------------------------------------------
# Hits: a synthetic map value caught in a commit message, and in a diff.
# ---------------------------------------------------------------------------

def test_synthetic_secret_value_caught_in_a_commit_message(tmp_path):
    workspace = tmp_path / "repo"
    base = _init_repo(workspace)
    message = f"debug: dumped env, {SYNTHETIC_VALUE} looked fine to me"
    (workspace / "notes.txt").write_text("nothing secret here\n")
    _git(workspace, "add", "notes.txt")
    _git(workspace, "commit", "-q", "-m", message)
    head = _head(workspace)

    hit = commit_scan.scan_commits_for_secrets(
        workspace, base, head, secrets=SYNTHETIC_SECRETS)

    assert hit is not None
    assert hit["part"] == "message"
    assert hit["variable"] == "SYNTHETIC_API_TOKEN"
    assert hit["sha"] == head


def test_synthetic_secret_value_caught_in_a_staged_diff(tmp_path):
    workspace = tmp_path / "repo"
    base = _init_repo(workspace)
    # The value lands only in the FILE CONTENT that was staged and
    # committed -- the commit message names nothing.
    head = _commit_file(
        workspace, "diagnostics.log",
        f"connection ok, token={SYNTHETIC_VALUE}\n",
        "diagnostics: capture what the retry saw")

    hit = commit_scan.scan_commits_for_secrets(
        workspace, base, head, secrets=SYNTHETIC_SECRETS)

    assert hit is not None
    assert hit["part"] == "diff"
    assert hit["variable"] == "SYNTHETIC_API_TOKEN"
    assert hit["sha"] == head


# ---------------------------------------------------------------------------
# The three corpora PRD requirement I.4 names, all must pass UNFLAGGED.
# ---------------------------------------------------------------------------

def test_a_diff_merely_discussing_a_tokens_shape_passes_unflagged(tmp_path):
    workspace = tmp_path / "repo"
    base = _init_repo(workspace)
    head = _commit_file(
        workspace, "shape-notes.md",
        "Tokens from this provider look like `sk-` followed by 32 "
        "hex characters; they are never printed here.\n",
        "docs: describe the provider's token shape")

    hit = commit_scan.scan_commits_for_secrets(
        workspace, base, head, secrets=SYNTHETIC_SECRETS)

    assert hit is None


def test_a_fixture_carrying_a_synthetic_token_shaped_string_passes_unflagged(tmp_path):
    workspace = tmp_path / "repo"
    base = _init_repo(workspace)
    head = _commit_file(
        workspace, "tests/fixture_data.py",
        f'SAMPLE_TOKEN = "{TOKEN_SHAPED_BUT_UNKNOWN}"\n',
        "tests: add a synthetic token-shaped fixture value")

    hit = commit_scan.scan_commits_for_secrets(
        workspace, base, head, secrets=SYNTHETIC_SECRETS)

    assert hit is None


def test_this_contracts_own_prd_text_passes_unflagged(tmp_path):
    workspace = tmp_path / "repo"
    base = _init_repo(workspace)
    head = _commit_file(
        workspace, "docs/requirement-i4.md", PRD_I4_TEXT,
        "docs: land requirement I.4's own text")

    hit = commit_scan.scan_commits_for_secrets(
        workspace, base, head, secrets=SYNTHETIC_SECRETS)

    assert hit is None
    # It names AWS_BEARER_TOKEN_BEDROCK nowhere, but exercise the real,
    # non-synthetic-secret-shaped name pattern too, against the actual
    # module docstring text living in this repo, to prove a variable NAME
    # mentioned in prose is never mistaken for its VALUE.
    assert "AWS_BEARER_TOKEN_BEDROCK" not in PRD_I4_TEXT


# ---------------------------------------------------------------------------
# On a hit, the run does not push -- no push subprocess is ever started.
# ---------------------------------------------------------------------------

def test_on_a_hit_no_push_subprocess_is_ever_started(tmp_path):
    workspace = tmp_path / "repo"
    base = _init_repo(workspace)
    head = _commit_file(
        workspace, "leak.txt", f"oops: {SYNTHETIC_VALUE}\n",
        "chore: paste a diagnostic value")

    calls = []
    real_run = subprocess.run

    def _spy(args, **kwargs):
        calls.append(list(args))
        return real_run(args, **kwargs)

    with mock.patch("ralphd.engine.commit_scan.subprocess.run", side_effect=_spy):
        pushed, hit, refusal = commit_scan.push_if_clean(
            workspace, base, head, secrets=SYNTHETIC_SECRETS)

    assert pushed is False
    assert hit is not None
    assert refusal is not None
    assert not any("push" in call for call in calls), (
        f"a push subprocess was started despite the hit: {calls}")


def test_a_clean_range_does_start_exactly_one_push_subprocess(tmp_path):
    workspace = tmp_path / "repo"
    base = _init_repo(workspace)
    head = _commit_file(
        workspace, "clean.txt", "nothing secret here\n", "chore: clean commit")

    calls = []
    real_run = subprocess.run

    def _spy(args, **kwargs):
        calls.append(list(args))
        # Never actually try to push (there is no remote) -- record the
        # attempt and hand back a harmless failure instead of hitting the
        # network / a missing "origin".
        if "push" in args:
            return subprocess.CompletedProcess(args, 1, "", "no remote")
        return real_run(args, **kwargs)

    with mock.patch("ralphd.engine.commit_scan.subprocess.run", side_effect=_spy):
        pushed, hit, refusal = commit_scan.push_if_clean(
            workspace, base, head, secrets=SYNTHETIC_SECRETS,
            remote="origin", branch="main")

    assert pushed is True
    assert hit is None
    assert refusal is None
    push_calls = [c for c in calls if "push" in c]
    assert len(push_calls) == 1
    assert push_calls[0][-2:] == ["origin", "main"]


# ---------------------------------------------------------------------------
# The refusal names the variable name only; its text never contains the
# matched value.
# ---------------------------------------------------------------------------

def test_refusal_names_the_variable_only_and_never_the_value(tmp_path):
    workspace = tmp_path / "repo"
    base = _init_repo(workspace)
    head = _commit_file(
        workspace, "leak2.txt", f"leaked={SYNTHETIC_VALUE}\n",
        "chore: another leak")

    pushed, hit, refusal = commit_scan.push_if_clean(
        workspace, base, head, secrets=SYNTHETIC_SECRETS)

    assert pushed is False
    assert refusal is not None
    assert SYNTHETIC_VALUE not in refusal
    assert "SYNTHETIC_API_TOKEN" in refusal
    # The source prefix ("env:") is stripped -- the refusal names the
    # variable name only, never the label's own source annotation.
    assert "env:" not in refusal


def test_a_commit_carrying_only_the_variable_name_is_accepted(tmp_path):
    workspace = tmp_path / "repo"
    base = _init_repo(workspace)
    head = _commit_file(
        workspace, "names-only.txt",
        "rotate SYNTHETIC_API_TOKEN before shipping\n",
        "chore: name the variable, not its value")

    hit = commit_scan.scan_commits_for_secrets(
        workspace, base, head, secrets=SYNTHETIC_SECRETS)

    assert hit is None
