"""`exhausted` -- the deadline hold's own state, added in three independent
spellings (v0.24 requirement B.1, task 008, #169):

* `src/ralphd/engine/state.py`'s `NONTERMINAL_STATES`/`TERMINAL_STATES`;
* `src/ralphd/engine/transitions.py`'s `ALL_STATES`/`RUN_TERMINAL_STATES`;
* `src/ralphd/cli/web/app.js`'s hardcoded degraded-check terminal tuple and
  its `RUN_STATE_ORDER` run-state ordering (mirrored on the CLI side too,
  `cli/main.py`'s own `RUN_STATE_ORDER`).

B.1 is the state alone -- B.2/B.3/C/D/F (later tasks) are what actually
drives an engine INTO `exhausted` and what a plain `resume` does once
there. This module pins only what B.1 promises: the tuple memberships, the
still-holding ordering, that `stop` and `rm` accept a `exhausted` run
exactly as they accept a stopped one (the hold has no iteration in flight,
so neither command demands an `abort` first), and that it stays
resume-eligible with no new state gate.
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

def test_exhausted_is_a_nonterminal_state_in_state_py():
    assert "exhausted" in state_mod.NONTERMINAL_STATES
    assert "exhausted" not in state_mod.TERMINAL_STATES


def test_exhausted_is_in_all_states_but_not_run_terminal_in_transitions_py():
    assert "exhausted" in tr.ALL_STATES
    assert "exhausted" not in tr.RUN_TERMINAL_STATES


def test_exhausted_has_a_declared_successor_and_no_hat_of_its_own():
    # Every non-terminal state in the table must have at least one legal
    # successor (test_transitions.py's own invariant) -- pinned here too so
    # a future edit that empties this state's row is caught by name.
    assert tr.successors("exhausted", vigilant=True) != ()
    assert tr.successors("exhausted", vigilant=False) != ()
    assert tr.STATE_HAT.get("exhausted") is None


def test_app_js_hardcoded_terminal_tuple_still_excludes_exhausted():
    """The hardcoded degraded-check terminal tuple (app.js, ~L1982) mirrors
    `state.py`'s `TERMINAL_STATES` and must keep doing so byte-for-byte --
    a hub that added `exhausted` here would read the hold as terminal."""
    text = APP_JS.read_text()
    assert 'const TERMINAL_STATES = ["succeeded", "failed", "aborted"];' in text
    terminal_tuple_line = next(
        line for line in text.splitlines()
        if line.strip().startswith("const TERMINAL_STATES ="))
    assert "exhausted" not in terminal_tuple_line


def test_app_js_run_state_order_reads_exhausted_as_still_holding():
    text = APP_JS.read_text()
    order_line = text.split("RUN_STATE_ORDER = [", 1)[1].split("]")[0]
    order = [tok.strip().strip('"') for tok in order_line.split(",")]
    assert "exhausted" in order
    # still-holding: sorts alongside the other non-terminal states, strictly
    # before every terminal one -- never dropped into "unrecognised, sorts
    # last" territory past `aborted`.
    assert order.index("finishing") < order.index("exhausted")
    assert order.index("exhausted") < order.index("succeeded")
    assert order.index("exhausted") < order.index("failed")
    assert order.index("exhausted") < order.index("aborted")


def test_cli_run_state_order_matches_app_js():
    """The CLI's own `ralphctl runs --sort state` mirror (cli/main.py,
    task 055) must agree with the hub's, same lifecycle order, same
    still-holding placement for `exhausted`."""
    order = cli_main.RUN_STATE_ORDER
    assert "exhausted" in order
    assert order.index("finishing") < order.index("exhausted") \
        < order.index("succeeded")


# ---------------------------------------------------------------------------
# `stop`/`rm` accept a `exhausted` run exactly as they accept a stopped one
# ---------------------------------------------------------------------------

def test_plain_stop_accepts_a_exhausted_run_with_no_force(rm_ctl):
    """Requirement B.1: `stop` treats the hold as it treats a stopped run.
    A held run has no iteration in flight, so plain `stop` tears it down
    instead of exiting 5 to demand `abort`/`--force` the way it still does
    for `running` (the control node below)."""
    rm_ctl.seed("tst-stop-exhausted", state="exhausted")
    res = rm_ctl.run("stop", "tst-stop-exhausted",
                     env={"STUB_DOCKER_PS_IDS": "sibS"})

    assert res.returncode == 0, res.stderr
    assert "job still running" not in res.stderr
    rec = rm_ctl.recorded()
    assert ["rm", "-f", "ralphd-tst-stop-exhausted"] in rec
    assert ["rm", "-f", "sibS"] in rec
    # `stop` never deletes the run dir, held or not.
    assert rm_ctl.run_dir("tst-stop-exhausted").exists()


def test_plain_stop_still_refuses_a_running_run_without_force(rm_ctl):
    """The control: widening the gate for `exhausted` must not have widened
    it for a genuinely live job."""
    rm_ctl.seed("tst-stop-running", state="running")
    res = rm_ctl.run("stop", "tst-stop-running")

    assert res.returncode == 5
    assert "job still running" in res.stderr
    assert "stop --force" in res.stderr
    assert rm_targets(rm_ctl.recorded()) == []


def test_stop_force_tears_down_a_exhausted_run_too(rm_ctl):
    rm_ctl.seed("tst-stop-exhausted-force", state="exhausted")
    res = rm_ctl.run("stop", "tst-stop-exhausted-force", "--force",
                     env={"STUB_DOCKER_PS_IDS": "sib1"})
    assert res.returncode == 0, res.stderr
    rec = rm_ctl.recorded()
    assert ["rm", "-f", "ralphd-tst-stop-exhausted-force"] in rec
    assert ["rm", "-f", "sib1"] in rec


def test_rm_force_deletes_a_exhausted_run_like_a_stopped_one(rm_ctl):
    """`--force`'s recorded-state guard accepts `exhausted` exactly as it
    accepts a terminal (stopped) run: container torn down through `stop`'s
    own sequence, then both directories deleted."""
    rm_ctl.seed("tst-rm-exhausted-force", state="exhausted")
    res = rm_ctl.run("rm", "tst-rm-exhausted-force", "--yes", "--force",
                     env={**HAS_CONTAINER, "STUB_DOCKER_PS_IDS": "sibT"})

    assert res.returncode == 0, res.stderr
    rec = rm_ctl.recorded()
    assert ["rm", "-f", "ralphd-tst-rm-exhausted-force"] in rec
    assert ["rm", "-f", "sibT"] in rec
    assert not rm_ctl.run_dir("tst-rm-exhausted-force").exists()
    assert not rm_ctl.config_dir("tst-rm-exhausted-force").exists()


def test_rm_force_deletes_a_exhausted_run_with_no_container(rm_ctl):
    rm_ctl.seed("tst-rm-exhausted-nocont", state="exhausted")
    res = rm_ctl.run("rm", "tst-rm-exhausted-nocont", "--yes", "--force")

    assert res.returncode == 0, res.stderr
    assert not rm_ctl.run_dir("tst-rm-exhausted-nocont").exists()
    assert not rm_ctl.config_dir("tst-rm-exhausted-nocont").exists()


def test_plain_rm_of_a_exhausted_run_has_no_state_refusal_of_its_own(rm_ctl):
    """Unlike `finishing` (which draws its own dedicated plain-`rm` refusal
    naming the unwind), the hold draws none: with no container record left,
    plain `rm` deletes a `exhausted` run exactly as it deletes a stopped
    one."""
    rm_ctl.seed("tst-rm-exhausted-plain", state="exhausted")
    res = rm_ctl.run("rm", "tst-rm-exhausted-plain", "--yes")

    assert res.returncode == 0, res.stderr
    assert not rm_ctl.run_dir("tst-rm-exhausted-plain").exists()
    assert not rm_ctl.config_dir("tst-rm-exhausted-plain").exists()


def test_plain_rm_of_a_exhausted_run_refuses_only_for_the_container(rm_ctl):
    """The one refusal a `exhausted` run can still draw is the generic
    container-still-exists one -- word for word the refusal a stopped
    (`succeeded`) run with a leftover container draws, and never the
    `job still running` wording reserved for live work."""
    rm_ctl.seed("tst-rm-exhausted", state="exhausted")
    res = rm_ctl.run("rm", "tst-rm-exhausted", "--yes", env=HAS_CONTAINER)

    assert res.returncode == 5
    assert "container still exists" in res.stderr
    assert "job still running" not in res.stderr
    assert "reflect unwind" not in res.stderr
    assert rm_targets(rm_ctl.recorded()) == []
    assert rm_ctl.run_dir("tst-rm-exhausted").exists()
    assert rm_ctl.config_dir("tst-rm-exhausted").exists()

    # ... byte-for-byte the stopped run's own refusal.
    rm_ctl.seed("tst-rm-succeeded", state="succeeded")
    stopped = rm_ctl.run("rm", "tst-rm-succeeded", "--yes", env=HAS_CONTAINER)
    assert stopped.returncode == res.returncode
    assert stopped.stderr == res.stderr


# ---------------------------------------------------------------------------
# `resume` is resume-eligible: no new state gate
# ---------------------------------------------------------------------------

def test_resume_accepts_a_exhausted_run_with_no_state_gate(ctl):
    """`cmd_resume`'s only state guard is that the container is not running
    (cli/main.py:2894's docstring) -- `exhausted` must reach the same
    docker-run wiring as any other resumable state, with no refusal."""
    rdir, _cdir = _seed_run(ctl, "tst-exhausted-resume")
    rdir_status = rdir / "status.json"
    rdir_status.write_text(json.dumps({"state": "exhausted"}))

    res = ctl.run("resume", "tst-exhausted-resume", env={
        "STUB_DOCKER_CONTAINERS": "ralphd-tst-exhausted-resume",
        "STUB_DOCKER_RUNNING": "",
    })
    assert res.returncode == 0, res.stderr
    assert any(a[:2] == ["run", "-d"] for a in ctl.recorded())
