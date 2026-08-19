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


# --------------------------------------------------------------------------
# Task 036 (#7): the sibling-only cleanup rule, everywhere it is duplicated.
#
# The job container carries `ralphd.run=<run-id>` exactly like the siblings the
# agent starts, so a cleanup command filtered on that label alone deletes the
# container the agent is running in (run `deck-phase1` did exactly that: the
# run died mid-verify, the iteration's work and transcript were lost, the run
# dir was left non-terminal). Task 035 fixed the prompt; this guards every
# *documented* duplicate of the idiom -- docs, examples, and the rendered
# prompt -- against drifting back to the one-filter form.
# --------------------------------------------------------------------------

# Files that teach the idiom. docs/prds/ is excluded on purpose: those are
# frozen historical specs that quote the destructive command verbatim as the
# incident report ("the idiom ralphd's own prompt teaches it").
CLEANUP_DOC_FILES = [
    REPO_ROOT / "docs" / "cli.md",
    REPO_ROOT / "docs" / "architecture.md",
    REPO_ROOT / "README.md",
    REPO_ROOT / "examples" / "skills" / "toolchain-sibling" / "SKILL.md",
    REPO_ROOT / "examples" / "skills" / "toolchain-sibling" / "run.sh",
]

SIBLING_FILTER = "--filter label=ralphd.role=sibling"
# Verbs that make an occurrence of the run label a *query over containers*
# rather than a plain `--label` on something being created.
CLEANUP_VERBS = ("docker rm", "docker ps", "docker stop", "docker kill", "xargs")
# A one-filter example is allowed only where the surrounding prose marks it as
# the thing never to do.
PROHIBITION_MARKERS = (
    "never clean up by the run label alone",
    "never remove containers by",
    "run label only, deliberately",   # host-side ralphctl stop/rm, on purpose
    "filter on the run label alone on purpose",
)


def _run_label_only_cleanups(text: str) -> list[str]:
    """Lines that query containers by ralphd.run without the role filter."""
    lines = text.splitlines()
    bad = []
    for i, line in enumerate(lines):
        if "label=ralphd.run" not in line:
            continue
        if not any(v in line for v in CLEANUP_VERBS):
            continue
        # a wrapped command continues on the next line(s); the safe form must
        # carry the role filter *in the same command*, not merely nearby
        command = "\n".join(lines[i:i + 3]).lower()
        if "ralphd.role=sibling" in command:
            continue
        # a one-filter example is allowed where the prose (possibly the heading
        # of a numbered rule a few lines up) marks it as the thing never to do
        window = "\n".join(lines[max(0, i - 4):i + 3]).lower()
        if any(m in window for m in PROHIBITION_MARKERS):
            continue
        bad.append(f"{i + 1}: {line.strip()}")
    return bad


def test_docs_and_examples_teach_the_sibling_only_cleanup_filter():
    for path in CLEANUP_DOC_FILES:
        text = path.read_text()
        if "ralphd.run" not in text:
            continue
        assert "ralphd.role=sibling" in text, (
            f"{path.relative_to(REPO_ROOT)} labels siblings with the run label "
            f"but never mentions ralphd.role=sibling")
    for path in (REPO_ROOT / "docs" / "cli.md",
                 REPO_ROOT / "docs" / "architecture.md",
                 REPO_ROOT / "examples" / "skills" / "toolchain-sibling" / "SKILL.md"):
        text = path.read_text()
        assert SIBLING_FILTER.split("--filter ")[-1] in text
        low = text.lower()
        assert any(m in low for m in PROHIBITION_MARKERS[:2]), (
            f"{path.relative_to(REPO_ROOT)} must warn against the "
            f"run-label-only cleanup form, not just show the safe one")


def test_no_run_label_only_cleanup_command_in_docs_or_examples():
    offenders = {}
    for path in CLEANUP_DOC_FILES:
        bad = _run_label_only_cleanups(path.read_text())
        if bad:
            offenders[str(path.relative_to(REPO_ROOT))] = bad
    assert not offenders, (
        "cleanup commands filtered on ralphd.run alone also match the job "
        f"container (#7); add {SIBLING_FILTER}: {offenders}")


def test_rendered_prompt_has_no_run_label_only_cleanup_command(monkeypatch):
    """The prompt is the fourth copy of the idiom -- hold it to the same bar."""
    from ralphd.engine.loop import LoopSupervisor

    for k, v in {"RALPHD_HOST_WORKSPACE": "/host/ws",
                 "RALPHD_HOST_RUN_DIR": "/host/run",
                 "RALPHD_RUN_ID": "doc-check",
                 "RALPHD_SELF_CONTAINER_ID": "ralphd-doc-check"}.items():
        monkeypatch.setenv(k, v)
    note = LoopSupervisor._docker_siblings_note()
    assert SIBLING_FILTER in note
    assert not _run_label_only_cleanups(note)


def test_example_skill_run_sh_labels_siblings_with_the_role_label():
    text = (REPO_ROOT / "examples" / "skills" / "toolchain-sibling"
            / "run.sh").read_text()
    assert "ralphd.role=sibling" in text, (
        "the shipped wrapper must apply the role label, otherwise the "
        "documented sibling-only cleanup filter matches nothing")
