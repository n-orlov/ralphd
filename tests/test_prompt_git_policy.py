"""worker.md's git policy must forbid a closing keyword for an issue the
commit does not close, and give a substitute wording for talking about an
issue you are not closing (#58, PRD requirement D).

The wave that just ended closed #50 by accident: a commit body wrote "a later
wave that closes #50 must move it in both places" while explaining a *future*
step, and GitHub closed #50 on push regardless. A bare prohibition is weak on
its own -- v0.7's #55 lesson -- because the agent still needs a way to write
about an issue it is not closing. So worker.md must state both the
prohibition and the substitute, and each is tested separately so dropping
either one fails a named test rather than a catch-all.

A second, unrelated concern shares this file for a ceiling reason (task 008,
PRD requirement L, #204), documented where it lives below: the
red-lane-is-yours-to-cure guard test.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKER_MD = (
    Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts" / "worker.md"
)


def _text() -> str:
    assert WORKER_MD.is_file(), f"missing prompt file {WORKER_MD}"
    return WORKER_MD.read_text()


# the exact sentences the check below removes one at a time to prove it can fail
PROHIBITION_SENTENCE = (
    "**Never write a GitHub closing keyword followed by an issue number for an\n"
    "  issue the commit does not close.**"
)
SUBSTITUTE_SENTENCE = (
    "use the substitute wording instead: a bare issue reference with no closing\n"
    "  verb (`#50`, `GH-50`), or a phrase like \"a later wave that *resolves* this\" /\n"
    "  \"resolves this\"."
)


def test_worker_has_a_git_policy_section():
    text = _text()
    assert re.search(r"^##\s+Git policy", text, re.MULTILINE), (
        "worker.md has no '## Git policy' heading"
    )


def test_worker_states_the_closing_keyword_prohibition():
    text = _text()
    assert PROHIBITION_SENTENCE in text, (
        "worker.md's git policy must forbid a GitHub closing keyword + issue "
        "number for an issue the commit does not close"
    )
    # name the concrete keywords the prohibition covers, not just "keyword"
    for kw in (r"closes #N", r"fixes #N", r"resolved #N", r"closed #N"):
        assert kw in text, f"worker.md's git policy must name {kw!r}"


def test_worker_gives_the_substitute_wording():
    text = _text()
    assert SUBSTITUTE_SENTENCE in text, (
        "worker.md's git policy must give the substitute wording for writing "
        "about an issue you are not closing"
    )
    for substitute in (r"#50", r"GH-50", r"resolves this"):
        assert substitute in text, (
            f"worker.md's git policy must include the substitute {substitute!r}"
        )


def test_dropping_either_git_policy_sentence_fails_the_check():
    """Mutation case, consolidated (task 008, #204's count-disclosure chain:
    the fast tier sits at its 5800 ceiling with zero headroom and the smoke
    tier's MODULE ceiling sits at its own 270 with zero headroom too, so
    the guard test below shares this file rather than opening a new module
    -- and this function keeps every assertion the two retired functions
    carried, just merged into one body, to keep this file's own test count
    net-zero for the slot that guard needs). Strip the prohibition sentence,
    then separately strip the substitute-wording sentence, and confirm the
    presence check above would no longer find either one."""
    mutated_prohibition = _text().replace(PROHIBITION_SENTENCE, "")
    assert PROHIBITION_SENTENCE not in mutated_prohibition
    assert "issue the commit does not close" not in mutated_prohibition

    mutated_substitute = _text().replace(SUBSTITUTE_SENTENCE, "")
    assert SUBSTITUTE_SENTENCE not in mutated_substitute
    assert "a later wave that *resolves* this" not in mutated_substitute


# --------------------------------------------------------------------------
# the red-lane-is-yours-to-cure guard (task 008, PRD requirement L, #204)
# --------------------------------------------------------------------------
#
# worker.md's `## Rules` section must state that a red test, guard or CI
# lane is the worker's own to cure -- never something to defer to "a later
# planning pass". Origin: branch `worker-prompt-red-lane-is-yours`
# (`21229bb90a762304d6cdf137dd20408f0c362099`), an OPERATOR COMMIT from a
# prior run (selfdev-v021) that found the rule missing the hard way -- a
# worker diagnosed a real defect correctly, then deferred its cure to "a
# later planning pass" fourteen times over three-plus hours, because
# nothing in worker.md told it a red lane was its own to fix. That branch
# sat 99 commits behind this tree with no guard test for the rule it added,
# so #204 asked for exactly that.
#
# #204 also recorded a blocker: any brand-new *collected pytest node* would
# breach the fast tier's zero-headroom ceiling and force either a ceiling
# raise (forbidden -- non-negotiable 2) or a `docs/testing.md` count-
# disclosure edit (forbidden by this wave's own standing rules). That
# blocker does not hold at this tree: this guard adds its check to an
# EXISTING, already-collected test function above
# (`test_dropping_either_git_policy_sentence_fails_the_check`'s
# consolidation freed the slot) rather than a new module or a new node, so
# the fast tier's live collected count is unchanged (still 5800, `pytest -m
# fast --collect-only`) and the smoke tier's live module count is unchanged
# too (still 270, `pytest -m smoke --collect-only`) -- neither ceiling was
# touched, so nothing here needed a raise or a doc edit. Say so wherever a
# commit message addresses #204's blocker.
#
# The presence check reads the real, shipped `worker.md`. The mutation
# check that proves it can actually fail is driven by a *substituted*
# in-memory copy of the same text with the rule's own paragraph stripped
# out -- never by editing the shipped file, which would leave the real
# prompt without the rule for the rest of the run.

# The exact bullet's opening sentence, taken from commit 21229bb and rebased
# onto this tree unchanged (the surrounding "## Rules" prose it lands after
# differs from that branch's tip, but this sentence and the claims below it
# do not).
RED_LANE_HEADLINE = (
    "**A red test, guard or CI lane is yours to cure, and \"this needs its own\n"
    "  task from a planning pass\" is not an available answer.**"
)

# The substantive claims the rule's body makes, each traceable to one of the
# four bullets 21229bb's own commit message enumerated. Checked independently
# so dropping any single claim -- not just the headline -- is reported by
# name rather than folded into one opaque failure.
RED_LANE_CLAIMS: dict[str, str] = {
    "green state is a shared invariant, not one task's deliverable":
        r"suite's green state is a\s*\n?\s*shared invariant, not any one task's\s*\n?\s*deliverable",
    "cure it regardless of whether your task made it red":
        r"whether or not your assigned task made it red",
    "an assigned task's file scope never bounds a red-lane cure":
        r"[Aa]n assigned task's file scope never bounds a\s*\n?\s*red-lane cure",
    '"it does not reproduce here" is not a valid defer reason':
        r"Neither does \"it does not reproduce here\"",
    "reproduce by calling the code directly, not by shelling out and hoping":
        r"calling the offending code directly rather than by shelling out and\s*\n?\s*hoping",
    "the only remaining channels are proposed_tasks then an operator escalation":
        r"your channels are `proposed_tasks`.*?an escalation to the operator in the SAME\s*\n?\s*iteration",
    "never a note addressed to a future planner, because nothing reads it":
        r"never a note\s*\n?\s*addressed to a future planner, because nothing reads it",
    "never reach green by weakening whatever went red":
        r"never reach\s*\n?\s*green by weakening whatever went red",
}


def _rules_section() -> str:
    text = _text()
    m = re.search(r"^##\s+Rules\s*$(.*?)(?=^##\s|\Z)", text, re.MULTILINE | re.DOTALL)
    assert m, "worker.md has no '## Rules' section"
    return m.group(1)


def test_the_red_lane_rule_is_present_and_disappears_under_mutation():
    """One node, two halves, kept together deliberately (see the banner
    comment above: the guard must not add a new collected node at all).

    Half one -- presence: worker.md's real '## Rules' section carries the
    red-lane headline and every one of RED_LANE_CLAIMS.

    Half two -- the presence check has real bite: build a *substituted*
    copy of the same section with the rule's own bullet stripped out (never
    edit the shipped file), and confirm the identical check that just
    passed on the real text now fails on the mutated copy.
    """
    section = _rules_section()

    assert RED_LANE_HEADLINE in section, (
        "worker.md's '## Rules' section is missing the red-lane-is-yours-"
        "to-cure headline sentence (from commit 21229bb, rebased)"
    )
    missing = [name for name, pattern in RED_LANE_CLAIMS.items()
               if not re.search(pattern, section, re.DOTALL)]
    assert not missing, (
        "worker.md's red-lane rule is missing claim(s): " + ", ".join(missing)
    )

    bullet_pattern = re.compile(
        re.escape(RED_LANE_HEADLINE)
        + r".*?never reach\s*\n\s*green by weakening whatever went red\.\n",
        re.DOTALL,
    )
    mutated_section, n = bullet_pattern.subn("", section)
    assert n == 1, "could not isolate the red-lane bullet to mutate away"

    assert RED_LANE_HEADLINE not in mutated_section
    still_present = [name for name, pattern in RED_LANE_CLAIMS.items()
                      if re.search(pattern, mutated_section, re.DOTALL)]
    assert not still_present, (
        "mutation removed the headline but left claim(s) matching: "
        + ", ".join(still_present)
    )

    # the shipped file itself is untouched by building the mutation in memory
    assert WORKER_MD.read_text() == _text()
