"""Cure-01-05 (v0.24 requirement B.1, DoD 3, #169): the review's runtime probe
promoted into the tree.

`app.js`'s `renderInfraWait` had a hardcoded `TERMINAL_STATES` tuple
(`["succeeded", "failed", "aborted"]`) that never grew a `timed_out` entry --
so a HELD run (`state: "timed_out"`, `health: "degraded"`, `infraWait: null`,
task 008's deadline/budget hold) fell through into the generic "a retry
attempt is running, no backoff wait pending" sentence, which is exactly
backwards: nothing is running, the hold is why nothing is happening.
Membership-only tests (`test_timed_out_state.py`'s tuple checks) cannot catch
this -- the tuple itself is untouched; only the *fallback* that fires when a
state is absent from it changed shape. This module instead loads the actual
clone's `app.js`, extracts `renderInfraWait`'s real source and evaluates it
under Node, so the assertions run against the resolved JS identity rather
than a Python re-implementation of the rendering rule.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ralphd.cli import main as cli_main

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

APP_JS = Path(cli_main.__file__).resolve().parent / "web" / "app.js"
ROOT = APP_JS.parents[3]  # src/ralphd/cli/web/app.js -> repo root


def _render(status: dict, *, live: bool = False) -> tuple[int, str]:
    """Load the REAL `renderInfraWait` out of the clone's own `app.js` (its
    resolved on-disk identity, not a copy), eval it, call it with `status`,
    and return (exit_code, rendered-nodes-json). Exit 0 means the node's own
    forbidden-substring assertions (inlined into the script, one per call
    site) all held.
    """
    js = r"""
const fs = require('fs'); const path = require('path');
const filename = fs.realpathSync(process.argv[1]);
if (!filename.startsWith(fs.realpathSync(process.argv[2]) + path.sep) &&
    filename !== fs.realpathSync(process.argv[2])) throw Error('wrong identity');
const source = fs.readFileSync(filename, 'utf8');
const body = source.slice(source.indexOf('function renderInfraWait('), source.indexOf('\nfunction nextAttemptText('));
const h = (tag, props, children) => ({tag, props, children});
let retryNowCalled = false;
const addedClasses = [];
const faultBadge = () => '';
const renderRetryNow = () => { retryNowCalled = true; };
const nodes = []; const el = {classList: {add(c){ addedClasses.push(c); }}, appendChild(x){nodes.push(x);}};
eval(body);
const status = JSON.parse(process.argv[3]);
const live = process.argv[4] === '1';
renderInfraWait(el, status, 'test-run', live);
console.log(JSON.stringify({ nodes, degraded: addedClasses.indexOf('degraded') !== -1, retryNowCalled }));
"""
    result = subprocess.run(
        ["node", "-e", js, str(APP_JS), str(ROOT), json.dumps(status), "1" if live else "0"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"node script itself failed: {result.stderr}"
    return result.returncode, result.stdout.strip()


def test_hub_does_not_describe_held_degraded_run_as_retry_running():
    """B.1/DoD 3: `timed_out` + `health: degraded` + `infraWait: null` is the
    HOLD -- no retry attempt is running, no backoff wait is pending, and the
    card must say so, never the generic "a retry attempt is running" line
    the running-outage branch owns.
    """
    _, out = _render({
        "state": "timed_out", "health": "degraded", "infraWait": None,
        "reason": "deadline reached mid-iteration",
    })
    payload = json.loads(out)
    nodes_text = json.dumps(payload["nodes"])
    assert "retry attempt is running" not in nodes_text
    assert "held" in nodes_text.lower()
    assert "deadline reached mid-iteration" in nodes_text
    # health=degraded still gets the degraded treatment (the card itself)
    assert payload["degraded"] is True
    # and no active retry control is offered for a held run
    assert payload["retryNowCalled"] is False


def test_held_run_with_no_reason_still_reads_as_held_not_retrying():
    _, out = _render({"state": "timed_out", "health": "degraded", "infraWait": None})
    nodes_text = json.dumps(json.loads(out)["nodes"])
    assert "retry attempt is running" not in nodes_text
    assert "held" in nodes_text.lower()


def test_running_outage_between_attempts_keeps_its_existing_wording():
    """Unchanged behavior: a genuinely running/starting/finishing episode
    between backoff waits (infraWait null, health degraded, state running)
    still reads as a retry attempt actually in progress.
    """
    _, out = _render({"state": "running", "health": "degraded", "infraWait": None})
    nodes_text = json.dumps(json.loads(out)["nodes"])
    assert "retry attempt is running" in nodes_text


def test_terminal_degraded_run_still_quotes_its_own_terminal_reason():
    """Unchanged behavior: task 050's terminal-degraded branch (state in
    succeeded/failed/aborted) still quotes `reason` instead of either the
    running-outage sentence or the new held sentence.
    """
    _, out = _render({
        "state": "failed", "health": "degraded", "infraWait": None,
        "reason": "repeated ingestion signature",
    })
    nodes_text = json.dumps(json.loads(out)["nodes"])
    assert "repeated ingestion signature" in nodes_text
    assert "retry attempt is running" not in nodes_text
    assert "held at the deadline" not in nodes_text


def test_timed_out_stays_outside_the_terminal_tuple():
    """The membership half, kept alongside the rendering probe so a future
    edit that folds `timed_out` INTO the hardcoded terminal tuple (instead
    of giving it its own branch) is caught here too, not only in
    test_timed_out_state.py.
    """
    source = APP_JS.read_text()
    body = source[source.index("function renderInfraWait("):source.index("\nfunction nextAttemptText(")]
    tuple_line = next(line for line in body.splitlines() if "TERMINAL_STATES = [" in line)
    assert "timed_out" not in tuple_line
