"""Automates the manual spot-check performed when docs/tutorial.md was
written (task 042): every `ralphctl <verb>` subcommand referenced in the
tutorial must genuinely exist in `ralphctl --help`'s subcommand list, and the
tutorial must cover its documented steps (install, doctor, profile, start
with skills+creds, watch/logs, steer, artifacts, resume, ui) in that order.
Keeps the "copy-pasteable / no stale commands" claim durable against drift
as the CLI evolves.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TUTORIAL = REPO_ROOT / "docs" / "tutorial.md"

REQUIRED_STEPS_IN_ORDER = [
    "install",
    "doctor",
    "profile",
    "start",
    "watch",
    "steer",
    "resum",  # "resume" / "Resume"
    "hub",  # web hub / ui
]


# `logsf <id>` is a documented pure alias for `logs <id> -f`, rewritten by
# _preprocess_logs_argv() before argparse ever sees it (src/ralphd/cli/main.py)
# -- it deliberately never appears in argparse's own subcommand choices list.
ALIAS_VERBS = {"logsf"}


def _ralphctl_help_subcommands() -> set[str]:
    out = subprocess.run(
        [sys.executable, "-m", "ralphd.cli.main", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    # argparse prints "{a,b,c,...}" as the metavar for the subcommand choices
    m = re.search(r"\{([a-z_,\-]+)\}", out.stdout)
    assert m, f"could not find subcommand list in --help output:\n{out.stdout}"
    return set(m.group(1).split(",")) | ALIAS_VERBS


def test_tutorial_exists_and_covers_required_steps_in_order():
    text = TUTORIAL.read_text()
    headers = re.findall(r"^## .*$", text, re.MULTILINE)
    assert headers, "docs/tutorial.md has no ## section headers"
    lowered = [h.lower() for h in headers]
    positions = []
    for step in REQUIRED_STEPS_IN_ORDER:
        idx = next((i for i, h in enumerate(lowered) if step in h), None)
        assert idx is not None, f"tutorial missing a step covering {step!r}; headers={headers}"
        positions.append(idx)
    assert positions == sorted(positions), (
        f"tutorial steps out of order: expected {REQUIRED_STEPS_IN_ORDER} to appear "
        f"in that relative order among headers={headers}"
    )


def test_tutorial_skills_and_creds_step_present():
    text = TUTORIAL.read_text().lower()
    assert "skill" in text and "cred" in text, (
        "tutorial must cover starting a job with skills and credentials"
    )


def test_tutorial_artifacts_step_present():
    text = TUTORIAL.read_text().lower()
    assert "artifact" in text, "tutorial must cover collecting artifacts"


def test_every_ralphctl_command_in_tutorial_exists_in_help():
    subcommands = _ralphctl_help_subcommands()
    text = TUTORIAL.read_text()
    referenced = set(re.findall(r"ralphctl\s+([a-z][a-z_-]*)", text))
    assert referenced, "no `ralphctl <verb>` commands found in tutorial to spot-check"
    unknown = {v for v in referenced if v not in subcommands}
    assert not unknown, (
        f"docs/tutorial.md references ralphctl verbs that don't exist in "
        f"--help: {sorted(unknown)}; known verbs: {sorted(subcommands)}"
    )
    # Sanity: the tutorial must reference a reasonably large subset of real
    # verbs, not just one or two (proves the walkthrough is substantive).
    assert len(referenced & subcommands) >= 8
