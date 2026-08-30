"""v0.14.1 requirement C (`docs/prds/v0.14.1-a-guard-that-cannot-see.md`, task
009): a terminal wave's `docs/roadmap.md` block must not still say part of
its own wave is outstanding.

Every wave's block is written *while the run is still going* and updated as
work lands; the standing template for the still-outstanding tail was
originally the literal phrase **"not yet landed as of this entry"**
(case-insensitive, tolerant of the soft line-wraps markdown inserts inside
it). v0.14.1's own task 009 pinned that literal template and then went
`failed` on exactly the residual its own guard left open: the match caught
only that one opening sentence and missed every rephrasing of the same
live claim (a bare "not yet landed" with a different tail, or the same
words in a different order around it) — v0.14.2's requirement C (this
module) closes that gap. The check now flags the **bare phrase "not yet
landed"** anywhere inside a terminal block, in any surrounding wording,
*except* when it is a past-tense quoted reference to the phrase itself —
wrapped immediately in quote marks on both sides, narrating that an earlier
sentence used and was corrected, not asserting anything is currently
outstanding (see `_is_quoted_reference` below; the three passages this
file's own docstring names below are exactly that shape). Once the run the
block is about reaches a terminal state, nothing in `docs/prds/README.md`
is still updating that block for it, so a live copy of the claim — in
whatever wording — is now simply wrong: either the outstanding item landed
later and the sentence was never revisited, or it never landed at all and
the record needs to say so as a settled historical fact instead of an open
item. Either way, the fix is a rewrite of that block, not a future task
nobody owns.

This module derives *terminal* from `docs/prds/README.md`'s own Outcome
column for the run the block's own "Full PRD:" link names — never from a
hand list of which versions are "done": a table row's cell reads `running`
or `launching` while the run is still live (both observed at HEAD, e.g. the
v0.14 and v0.14.1 rows themselves) and something else — `succeeded`,
`**failed**`, `**aborted**` — once it is not (see `_is_terminal`). A block
whose PRD link cannot be found in the table at all (the pre-PRD-tracking
v0.1-v0.4 headings, and `## After v0.8 ...`, which is not a version block)
is skipped rather than guessed at.

The template match is deliberately narrower than the bare phrase `"not yet
landed"`: three other places in this same file use those two words quoted as
a *reference* to the phrase itself while narrating, in the past tense, that
the sentence was corrected (`docs/roadmap.md`'s own corrected `## v0.14`
paragraph, twice, and this PRD's own `## v0.14.1` block once, describing this
very test). None of those three follow the words with "as of this entry", so
the narrower template does not re-flag them — pinned by
`test_the_check_does_not_flag_a_past_tense_quoted_reference` below, using the
exact wording those three passages use.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ROADMAP = REPO_ROOT / "docs" / "roadmap.md"
README = REPO_ROOT / "docs" / "prds" / "README.md"

# The live, still-outstanding claim, in ANY phrasing: the bare phrase "not
# yet landed" (case-insensitive, tolerant of soft line-wraps markdown
# inserts inside it — both docs/roadmap.md's own v0.12 and v0.13 blocks wrap
# right inside the original "...as of this entry" template, and bold
# markers can sit right up against the phrase too, e.g. the pre-task-008
# v0.14 paragraph). Deliberately wider than the original template so that a
# validator paraphrasing around the same three words — moving the tail
# before it, dropping "as of this entry" for "as of today", swapping the
# colon for "is", wrapping it in em-dashes — still trips this check; see
# `_is_quoted_reference` for the one carve-out.
_BARE_PHRASE_RE = re.compile(r"not\s+yet\s+landed", re.IGNORECASE)

# Quote characters (straight and curly) this repo's prose uses to quote the
# phrase itself as a past-tense reference rather than assert it live.
_QUOTE_CHARS = "\"'\u201c\u201d\u2018\u2019"


def _is_quoted_reference(body: str, match: "re.Match[str]") -> bool:
    """True if `match` sits immediately inside quote marks on both sides —
    a reference to the phrase (narrating that an earlier sentence used it
    and was corrected), not a live claim that something is outstanding."""
    before = body[:match.start()]
    after = body[match.end():]
    return (
        bool(before) and before[-1] in _QUOTE_CHARS
        and bool(after) and after[0] in _QUOTE_CHARS
    )


def _live_claim_present(body: str) -> bool:
    return any(
        not _is_quoted_reference(body, m)
        for m in _BARE_PHRASE_RE.finditer(body)
    )

# Non-terminal run states, read off docs/prds/README.md's own vocabulary
# (the two seen at HEAD: v0.14 reads `running`, v0.14.1 reads `launching`).
# Anything else the Outcome cell's first word names (`succeeded`, `failed`,
# `aborted`, bold or not) is terminal.
_NON_TERMINAL = {"running", "launching"}

_README_ROW_RE = re.compile(
    r"^\|\s*`[^`]+`\s*\|\s*\[[^\]]*\]\(([^)]+)\)\s*\|\s*([^|]+)\|",
    re.MULTILINE,
)

_PRD_LINK_RE = re.compile(r"\[`docs/prds/([^`]+)`\]\(prds/[^)]+\)")

_HEADING_RE = re.compile(r"^## (\S.*)$", re.MULTILINE)
_VERSION_HEADING_RE = re.compile(r"^v(\d+(?:\.\d+)+)\b")


def readme_outcomes(readme_text: str) -> dict[str, str]:
    """`{filename: outcome_cell_text}` for every run row in the table."""
    return {
        filename: outcome.strip()
        for filename, outcome in _README_ROW_RE.findall(readme_text)
    }


def is_terminal(outcome: str) -> bool:
    m = re.match(r"\*{0,2}([A-Za-z]+)", outcome)
    assert m, f"outcome cell has no leading word to classify: {outcome!r}"
    return m.group(1).lower() not in _NON_TERMINAL


def roadmap_version_blocks(roadmap_text: str) -> list[tuple[str, str]]:
    """`[(heading_text, block_body)]` for every `## ...` section, in order.

    `block_body` runs from just after its own heading line to the start of
    the next `## ` heading (or end of file) — the same "stop at the next
    top-level heading" shape `tests/test_roadmap_v014.py`'s `milestone_block`
    uses for a single pinned version, generalised here to every block.
    """
    heads = list(_HEADING_RE.finditer(roadmap_text))
    blocks = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(roadmap_text)
        blocks.append((m.group(1), roadmap_text[m.end():end]))
    return blocks


def block_prd_filename(block_body: str) -> str | None:
    m = _PRD_LINK_RE.search(block_body)
    return m.group(1) if m else None


def unresolved_claim_problems(roadmap_text: str, readme_text: str) -> list[str]:
    outcomes = readme_outcomes(readme_text)
    problems = []
    for heading, body in roadmap_version_blocks(roadmap_text):
        if not _VERSION_HEADING_RE.match(heading):
            continue  # not a version block at all (e.g. "After v0.8 ...")
        filename = block_prd_filename(body)
        if filename is None or filename not in outcomes:
            continue  # no README row to derive terminal-ness from
        outcome = outcomes[filename]
        if not is_terminal(outcome):
            continue
        if _live_claim_present(body):
            problems.append(
                f"## {heading} is terminal (docs/prds/README.md outcome "
                f"{outcome!r}) but still carries a live 'not yet landed as "
                f"of this entry' claim")
    return problems


# ------------------------------------------------------------- the real files


def test_no_terminal_roadmap_block_carries_a_live_not_yet_landed_claim():
    problems = unresolved_claim_problems(ROADMAP.read_text(), README.read_text())
    assert not problems, "\n".join(problems)


# ------------------------------------------------------ the check is proven to bite

_SYNTHETIC_README = (
    "| `selfdev-vX` | [vX.md](vX.md) | succeeded / verified | 1 | 1M | $1 |\n"
    "| `selfdev-vY` | [vY.md](vY.md) | running — still going | 1 | 1M | $1 |\n"
)


def _synthetic_block(heading: str, filename: str, tail: str) -> str:
    return (
        f"## {heading}\n\n"
        f"Full PRD: [`docs/prds/{filename}`]({'prds/' + filename}).\n\n"
        f"{tail}\n"
    )


def test_the_check_fails_against_a_synthetic_terminal_block_claiming_outstanding_work():
    block = _synthetic_block(
        "v9.9 — a made-up wave", "vX.md",
        "Not yet landed as of this entry: a report nobody wrote yet.",
    )
    problems = unresolved_claim_problems(block, _SYNTHETIC_README)
    assert problems, "the check must flag a terminal block's live claim"
    assert "v9.9" in problems[0]


def test_the_check_fails_against_the_pre_task_008_v014_paragraph():
    """The exact wording `docs/roadmap.md`'s own `## v0.14` block carried
    before task 008 rewrote it (commit `d748a97`'s diff, reproduced here as
    a fixture rather than re-fetched from history) — proof this check would
    have bitten on the record this wave was opened to correct, not just on
    a hand-built synthetic string."""
    tail = (
        "**Not yet landed as of this entry**, tracked as this run's own "
        "remaining tasks and not claimed\n  here: a fresh fast/docker/"
        "browser-tier sweep."
    )
    block = _synthetic_block("v9.9 — every hand on the plan is named", "vX.md", tail)
    problems = unresolved_claim_problems(block, _SYNTHETIC_README)
    assert problems


# The four evading rephrasings v0.14.1's own task 009 left uncaught (#108):
# same three words, none of them followed by "as of this entry", each
# pinned by its own assertion so a future narrowing cannot silently drop one.


def test_the_check_fails_against_a_colon_led_rephrasing():
    block = _synthetic_block(
        "v9.9 — a made-up wave", "vX.md",
        "Not yet landed: the closing report.",
    )
    problems = unresolved_claim_problems(block, _SYNTHETIC_README)
    assert problems, "the check must flag 'Not yet landed: X.'"


def test_the_check_fails_against_a_subject_led_rephrasing():
    block = _synthetic_block(
        "v9.9 — a made-up wave", "vX.md",
        "The closing report is not yet landed.",
    )
    problems = unresolved_claim_problems(block, _SYNTHETIC_README)
    assert problems, "the check must flag 'X is not yet landed.'"


def test_the_check_fails_against_an_as_of_today_rephrasing():
    block = _synthetic_block(
        "v9.9 — a made-up wave", "vX.md",
        "Not yet landed as of today: the closing report.",
    )
    problems = unresolved_claim_problems(block, _SYNTHETIC_README)
    assert problems, "the check must flag 'Not yet landed as of today: X.'"


def test_the_check_fails_against_an_em_dash_wrapped_rephrasing():
    block = _synthetic_block(
        "v9.9 — a made-up wave", "vX.md",
        "Outstanding work — not yet landed — includes the closing report.",
    )
    problems = unresolved_claim_problems(block, _SYNTHETIC_README)
    assert problems, "the check must flag 'Outstanding work — not yet landed — includes X.'"


def test_the_check_skips_a_non_terminal_runs_block():
    block = _synthetic_block(
        "v8.8 — still running", "vY.md",
        "Not yet landed as of this entry: everything, it just started.",
    )
    assert not unresolved_claim_problems(block, _SYNTHETIC_README)


def test_the_check_does_not_flag_a_past_tense_quoted_reference():
    """The three passages this file's own docstring names -- quoting "not
    yet landed" without "as of this entry" right after it, to narrate that
    an earlier sentence was corrected -- must not re-trip the check."""
    for tail in (
        'Two claims this block\'s own first draft made under "not yet '
        'landed" were wrong the moment the tree already held the work.',
        'this is what actually happened, not the live "not yet landed" '
        "list this entry originally carried.",
        'a fast-tier test pinning that no terminal wave\'s block may carry '
        'a live "not yet landed" claim follows.',
    ):
        block = _synthetic_block("v9.9 — a made-up wave", "vX.md", tail)
        assert not unresolved_claim_problems(block, _SYNTHETIC_README), tail


def test_terminal_classification_reads_the_readme_vocabulary_not_a_hand_list():
    assert is_terminal("succeeded / verified")
    assert is_terminal("**failed** / unverified — some note")
    assert is_terminal("**aborted** / unverified")
    assert not is_terminal("running — approach 2 in progress")
    assert not is_terminal("launching — placeholder row")
