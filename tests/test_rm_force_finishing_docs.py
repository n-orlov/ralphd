"""Task 014 (#56): `ralphctl rm`'s treatment of the `finishing` window.

`docs/cli.md`'s `## ralphctl rm <run-id> [--force]` section must say two
things about the reflect unwind's `finishing` state (task 012's
`NONTERMINAL_STATES` addition):

1. plain `rm` refuses it with its own wording (not the generic
   container-still-exists or job-still-running phrasing), and
2. `--force`'s terminal-state guard is *deliberately* widened to accept it --
   i.e. what `--force` costs during the unwind is nothing beyond what it
   already costs for an ordinary terminal run (the container gets stopped via
   `stop`'s teardown sequence, same as always).

Both claims are paired here with a mutation case (the doc text is checked
against the exact phrases the task 014 commit introduced, so reverting the
doc edit -- independent of the code edit -- is caught) and a code-derived
behaviour check tying the doc claim to `cmd_rm`'s actual refusal/allow
decisions (the black-box coverage lives in tests/test_cli_rm_force.py; this
file pins the same two decisions at the source-inspection level so a revert
of the code without touching the tests below is still caught by SOMETHING in
this file even if test_cli_rm_force.py were, hypothetically, not run).
"""

from __future__ import annotations

import inspect
from pathlib import Path

from ralphd.cli import main as cli_main

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_DOC = REPO_ROOT / "docs" / "cli.md"

# The exact phrases the task 014 doc edit introduced.
FINISHING_REFUSAL_PHRASE = (
    "run is finishing (reflect unwind in progress) \u2014 wait for it to "
    "reach a terminal state, or use `rm --force`")
FORCE_WIDENED_FRAGMENT = "or `finishing`** (task 014, #56)"


def test_docs_state_the_finishing_specific_plain_rm_refusal():
    text = " ".join(CLI_DOC.read_text(encoding="utf-8").split())
    assert FINISHING_REFUSAL_PHRASE in text, (
        "docs/cli.md must quote the finishing-specific refusal `cmd_rm` "
        "actually raises, naming the unwind rather than the generic "
        "container-still-exists/job-still-running wording")


def test_docs_state_force_is_widened_to_accept_finishing():
    text = CLI_DOC.read_text(encoding="utf-8")
    assert FORCE_WIDENED_FRAGMENT in text, (
        "docs/cli.md must say --force's terminal-state guard was widened "
        "to accept `finishing` deliberately (task 014), not merely list it "
        "among today's terminal states by coincidence")


def test_mutation_the_finishing_phrase_is_not_a_typo_match():
    """The phrase above really is the string `cmd_rm` dies with -- so the
    doc check above is checking the doc against the code's own wording, not
    against a string nobody emits."""
    source = inspect.getsource(cli_main.cmd_rm)
    assert 'die(5, "run is finishing' in source
    assert "reach a terminal state" in source


def test_mutation_force_widening_is_really_in_the_guard():
    """The terminal-state guard `cmd_rm` uses really does carve out
    `finishing` alongside `TERMINAL_STATES` -- if this exclusion were
    removed, `--force` would refuse a finishing run again and this
    assertion (grep-equivalent over the live source) would fail."""
    source = inspect.getsource(cli_main.cmd_rm)
    assert 'state != "finishing"' in source
    assert 'state == "finishing" and not args.force' in source


# --------------------------------------------------------------------------
# Behaviour side: exercise the two decisions `cmd_rm` makes directly.
# --------------------------------------------------------------------------
import json
import os
import subprocess

from tests.conftest import RALPHCTL

STUB_DOCKER = REPO_ROOT / "tests" / "stub-docker" / "docker"


def _run_rm(tmp_path, run_id, *extra_argv, container_exists=False):
    registry = tmp_path / "registry"
    rdir = registry / "runs" / run_id
    rdir.mkdir(parents=True)
    (rdir / "iterations").mkdir()
    (rdir / "status.json").write_text(json.dumps({"state": "finishing"}))
    cdir = registry / "configs" / run_id
    cdir.mkdir(parents=True)
    (cdir / "job.yaml").write_text("iterations: 3\n")
    env = {**os.environ, "RALPHD_DOCKER": str(STUB_DOCKER),
           "RALPHD_REGISTRY": str(registry)}
    if container_exists:
        env["STUB_DOCKER_INSPECT_OK"] = "1"
    return subprocess.run(
        [str(RALPHCTL), "rm", run_id, "--yes", *extra_argv],
        env=env, capture_output=True, text=True, timeout=60), rdir


def test_plain_rm_refuses_a_finishing_run_regardless_of_container(tmp_path):
    res, rdir = _run_rm(tmp_path, "doc-fin-1", container_exists=True)
    assert res.returncode == 5
    assert "finishing" in res.stderr
    assert rdir.exists()


def test_force_deletes_a_finishing_run(tmp_path):
    res, rdir = _run_rm(tmp_path, "doc-fin-2", "--force", container_exists=True)
    assert res.returncode == 0, res.stderr
    assert not rdir.exists()
