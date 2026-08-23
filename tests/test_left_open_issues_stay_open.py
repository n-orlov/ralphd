"""Requirement D's second check (#58): the closure record's own
`left_open_issues()` names issues this run deliberately did NOT close, but
that claim is only as good as GitHub's *current* state -- the local record
cannot see an issue someone (or something) else closed after the record was
written. This module re-reads each of those issues over the live GitHub API
and fails if any of them is no longer open.

It lives beside the other roadmap/closure consistency checks
(`tests/test_roadmap_v07.py`), reusing that module's `left_open_issues()`
rather than retyping the set, and it skips cleanly -- with a reason naming
exactly what is missing -- when no GITHUB_TOKEN or no network is available,
so the fast lane stays green on a developer host with no ralphd env vars and
no `/config/creds` mount. The skip path itself is tested directly (without
actually skipping the module) by calling the same prerequisite check the real
test uses and asserting the reason it returns.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

from tests.test_roadmap_v07 import left_open_issues

REPO = "n-orlov/ralphd"
ISSUE_URL_TEMPLATE = "https://api.github.com/repos/{repo}/issues/{number}"
PROBE_URL = "https://api.github.com"
TIMEOUT_S = 10


def _missing_prerequisites() -> str | None:
    """`None` if a live, authenticated GitHub GET is possible right now;
    otherwise a reason string naming exactly what is missing."""
    if not os.environ.get("GITHUB_TOKEN"):
        return "no GITHUB_TOKEN in the environment -- cannot authenticate to GitHub"
    try:
        urllib.request.urlopen(PROBE_URL, timeout=TIMEOUT_S)
    except (urllib.error.URLError, OSError) as exc:
        return f"no network reachable to {PROBE_URL} ({exc})"
    return None


def _fetch_issue_state(number: int, token: str) -> str:
    """The `state` GitHub answers on a fresh GET, never a cached/echoed one."""
    request = urllib.request.Request(
        ISSUE_URL_TEMPLATE.format(repo=REPO, number=number),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "ralphd-selfdev-test",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        body = json.loads(response.read())
    return body["state"]


def test_the_skip_condition_fires_without_a_token(monkeypatch):
    """The skip path, exercised directly rather than by actually skipping:
    remove the token and check the reason names it."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    reason = _missing_prerequisites()
    assert reason is not None
    assert "GITHUB_TOKEN" in reason


def test_the_skip_condition_fires_without_network(monkeypatch):
    """Same path, but the token is present and the network call is the thing
    that fails -- the reason must name what actually failed, not the token."""
    monkeypatch.setenv("GITHUB_TOKEN", "placeholder-token-for-this-test-only")

    def _simulated_no_route(*_args, **_kwargs):
        raise urllib.error.URLError("simulated: no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", _simulated_no_route)
    reason = _missing_prerequisites()
    assert reason is not None
    assert "api.github.com" in reason
    assert "GITHUB_TOKEN" not in reason


def test_every_deliberately_left_open_issue_is_still_open():
    """The real check: re-read each issue `left_open_issues()` names over the
    live GitHub API and fail if any of them is no longer open. Skips cleanly,
    with a named reason, when no token/network is available."""
    reason = _missing_prerequisites()
    if reason:
        pytest.skip(reason)
    token = os.environ["GITHUB_TOKEN"]
    unexpectedly_not_open = []
    for number in sorted(left_open_issues()):
        state = _fetch_issue_state(number, token)
        if state != "open":
            unexpectedly_not_open.append((number, state))
    assert not unexpectedly_not_open, (
        "issues the closure record names as deliberately left open, but "
        f"that a fresh GitHub GET now reports otherwise: {unexpectedly_not_open}"
    )
