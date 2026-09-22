"""`timed_out` -- the deadline hold's own state, added in three independent
spellings (v0.24 requirement B.1, task 008, #169):

* `src/ralphd/engine/state.py`'s `NONTERMINAL_STATES`/`TERMINAL_STATES`;
* `src/ralphd/engine/transitions.py`'s `ALL_STATES`/`RUN_TERMINAL_STATES`;
* `src/ralphd/cli/web/app.js`'s hardcoded degraded-check terminal tuple and
  its `RUN_STATE_ORDER` run-state ordering (mirrored on the CLI side too,
  `cli/main.py`'s own `RUN_STATE_ORDER`).

B.1 is the state alone -- B.2/B.3/C/D/F (later tasks) are what actually
drives an engine INTO `timed_out` and what a plain `resume` does once
there. This module pins only what B.1 promises: the tuple memberships, and
that `stop`/`rm`/`resume` already treat a `timed_out` run exactly like any
other non-terminal one (`running`, `starting`) with no special-casing and
no new state gate.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd.cli import main as cli_main
from ralphd.engine import state as state_mod
from ralphd.engine import transitions as tr

from test_cli_docker import Ctl, ctl  # noqa: F401 (fixture re-export)
from test_cli_resume import _seed_run
from test_cli_rm_force import HAS_CONTAINER, rm_targets
from test_cli_rm_force import ctl as rm_ctl  # noqa: F401 (fixture re-export)

__all__ = ["ctl", "rm_ctl"]

APP_JS = (Path(cli_main.__file__).resolve().parent / "web" / "app.js")


# ---------------------------------------------------------------------------
# Tuple membership -- the three spellings
# ---------------------------------------------------------------------------

def test_timed_out_is_a_nonterminal_state_in_state_py():
    assert "timed_out" in state_mod.NONTERMINAL_STATES
    assert "timed_out" not in state_mod.TERMINAL_STATES


def test_timed_out_is_in_all_states_but_not_run_terminal_in_transitions_py():
    assert "timed_out" in tr.ALL_STATES
    assert "timed_out" not in tr.RUN_TERMINAL_STATES


def test_timed_out_has_a_declared_successor_and_no_hat_of_its_own():
    # Every non-terminal state in the table must have at least one legal
    # successor (test_transitions.py's own invariant) -- pinned here too so
    # a future edit that empties this state's row is caught by name.
    assert tr.successors("timed_out", vigilant=True) != ()
    assert tr.successors("timed_out", vigilant=False) != ()
    assert tr.STATE_HAT.get("timed_out") is None


def test_app_js_hardcoded_terminal_tuple_still_excludes_timed_out():
    """The hardcoded degraded-check terminal tuple (app.js, ~L1982) mirrors
    `state.py`'s `TERMINAL_STATES` and must keep doing so byte-for-byte --
    a hub that added `timed_out` here would read the hold as terminal."""
    text = APP_JS.read_text()
    assert 'const TERMINAL_STATES = ["succeeded", "failed", "aborted"];' in text
    terminal_tuple_line = next(
        line for line in text.splitlines()
        if line.strip().startswith("const TERMINAL_STATES ="))
    assert "timed_out" not in terminal_tuple_line


def test_app_js_run_state_order_reads_timed_out_as_still_holding():
    text = APP_JS.read_text()
    order_line = text.split("RUN_STATE_ORDER = [", 1)[1].split("]")[0]
    order = [tok.strip().strip('"') for tok in order_line.split(",")]
    assert "timed_out" in order
    # still-holding: sorts alongside the other non-terminal states, strictly
    # before every terminal one -- never dropped into "unrecognised, sorts
    # last" territory past `aborted`.
    assert order.index("finishing") < order.index("timed_out")
    assert order.index("timed_out") < order.index("succeeded")
    assert order.index("timed_out") < order.index("failed")
    assert order.index("timed_out") < order.index("aborted")


def test_cli_run_state_order_matches_app_js():
    """The CLI's own `ralphctl runs --sort state` mirror (cli/main.py,
    task 055) must agree with the hub's, same lifecycle order, same
    still-holding placement for `timed_out`."""
    order = cli_main.RUN_STATE_ORDER
    assert "timed_out" in order
    assert order.index("finishing") < order.index("timed_out") \
        < order.index("succeeded")


# ---------------------------------------------------------------------------
# `stop`/`rm` treat `timed_out` exactly like any other non-terminal state
# ---------------------------------------------------------------------------

def test_plain_stop_refuses_a_timed_out_run_without_force(rm_ctl):
    rm_ctl.seed("tst-stop-timedout", state="timed_out")
    res = rm_ctl.run("stop", "tst-stop-timedout")
    assert res.returncode == 5
    assert "job still running" in res.stderr
    assert "stop --force" in res.stderr


def test_stop_force_tears_down_a_timed_out_run_like_any_nonterminal_one(rm_ctl):
    rm_ctl.seed("tst-stop-timedout-force", state="timed_out")
    res = rm_ctl.run("stop", "tst-stop-timedout-force", "--force",
                     env={"STUB_DOCKER_PS_IDS": "sib1"})
    assert res.returncode == 0, res.stderr
    rec = rm_ctl.recorded()
    assert ["rm", "-f", "ralphd-tst-stop-timedout-force"] in rec
    assert ["rm", "-f", "sib1"] in rec


def test_plain_rm_refuses_a_timed_out_job_exactly_like_running(rm_ctl):
    """Same refusal shape as `test_rm_force_refuses_a_running_job_and_
    touches_nothing`/`_a_starting_job` in test_cli_rm_force.py -- `timed_out`
    is just another `NONTERMINAL_STATES` member, no special case."""
    rm_ctl.seed("tst-rm-timedout", state="timed_out")
    res = rm_ctl.run("rm", "tst-rm-timedout", "--yes", "--force",
                     env={**HAS_CONTAINER, "STUB_DOCKER_PS_IDS": "sib1"})

    assert res.returncode == 5
    assert "job still running" in res.stderr
    assert "state: timed_out" in res.stderr
    assert "abort" in res.stderr
    assert rm_targets(rm_ctl.recorded()) == []
    assert rm_ctl.run_dir("tst-rm-timedout").exists()
    assert rm_ctl.config_dir("tst-rm-timedout").exists()


def test_rm_force_also_refuses_a_timed_out_run_same_as_running(rm_ctl):
    """Unlike `finishing` (which `--force`'s guard deliberately widens to
    accept), `timed_out` gets NO carve-out -- `rm --force` alone refuses it
    exactly as it refuses `running`/`starting`; only `stop --force` (which
    issues its own `abort` first) or an explicit `abort` then `rm --force`
    gets past it."""
    rm_ctl.seed("tst-rm-timedout-force", state="timed_out")
    res = rm_ctl.run("rm", "tst-rm-timedout-force", "--yes", "--force",
                     env={**HAS_CONTAINER, "STUB_DOCKER_PS_IDS": "sibT"})

    assert res.returncode == 5
    assert "job still running" in res.stderr
    assert rm_targets(rm_ctl.recorded()) == []
    assert rm_ctl.run_dir("tst-rm-timedout-force").exists()
    assert rm_ctl.config_dir("tst-rm-timedout-force").exists()


# ---------------------------------------------------------------------------
# `resume` is resume-eligible: no new state gate
# ---------------------------------------------------------------------------

def test_resume_accepts_a_timed_out_run_with_no_state_gate(ctl):
    """`cmd_resume`'s only state guard is that the container is not running
    (cli/main.py:2894's docstring) -- `timed_out` must reach the same
    docker-run wiring as any other resumable state, with no refusal."""
    rdir, _cdir = _seed_run(ctl, "tst-timedout-resume")
    rdir_status = rdir / "status.json"
    rdir_status.write_text(json.dumps({"state": "timed_out"}))

    res = ctl.run("resume", "tst-timedout-resume", env={
        "STUB_DOCKER_CONTAINERS": "ralphd-tst-timedout-resume",
        "STUB_DOCKER_RUNNING": "",
    })
    assert res.returncode == 0, res.stderr
    assert any(a[:2] == ["run", "-d"] for a in ctl.recorded())
