"""v0.14 requirement S (docs/prds/v0.14-every-hand-on-the-plan.md):
`docs/roadmap.md`'s v0.14 block and the version bump.

**Retargeted by v0.15 task 017**, the same move v0.14's own requirement S
made to `tests/test_roadmap_v013.py`: v0.14 is no longer the declared
release (`pyproject.toml` moved to `0.15.0` at v0.15 task 015), so `SERIES`
here is **pinned** to `(0, 14)` rather than read from `pyproject.toml`, and
the "declared version has a block" half is now enforced elsewhere -- no
`tests/test_roadmap_v015.py` exists yet to take it over; adding one is out
of this task's own scope (its own criteria name this exact pytest command,
not a new module), and is left as a residual for whichever task next needs
the declared-version-has-a-block half enforced again.
`declared_version()`/`declared_series()` stay, used only to assert this
block is a *past* milestone -- if they were still used to select the block,
every check below would grade the wrong milestone against v0.14's own PRD.

Note this module's pin survived unchanged through both `0.14.1` and
`0.14.2` (same `(0, 14)` series, point releases only) -- it only went stale
now that the minor version moved to `0.15`.

Two things established by earlier waves stay true here:

* the roadmap's last milestone and `pyproject.toml`'s declared version are
  the same fact, so the block for the declared version must exist, must
  record shipped work, and nothing may claim a milestone *after* it. That
  "nothing after" half is filtered out of the two checks here that would
  otherwise fail on v0.14.2's or v0.15's own blocks simply for existing;
* a block may not claim a wave landed while omitting requirements of that
  wave. The requirement letters are parsed out of the PRD the block is about
  (`docs/prds/v0.14-every-hand-on-the-plan.md`), never retyped here.

This PRD's requirement headings are `### A — title` (an em dash, the same
shape `test_roadmap_v013.py` pinned), letters `A`-`S` with no gaps and no
split letter this time -- unlike v0.13's `F1`/`F2`, every letter here gets
its own heading. Like v0.13, **none of this PRD's headings carry an inline
`(#NN)`**, so per-letter issue coverage cannot be derived from the headings
either; this module checks a fixed, hand-verified set of issues (`PRD_ISSUES`,
read off the PRD's own "Nine issues" sentence, which is ten issues -- nine
the wave owns plus #91, which the PRD says closes as a consequence of #98
rather than by its own work) against the *whole* block, the same shape
`test_roadmap_v013.py` uses.

Beyond the coverage/citation shape every earlier module in this family
shares, this module pins three residuals the run itself produced and that
`docs/roadmap.md`'s v0.14 block is required to state plainly, the same way
the v0.9 block did for its own unfinished G:

* requirement P's test-fidelity residual and its own GitHub issue, #104;
* the finding that this run reproduced #99's own doom scenario while
  shipping #99's fix -- approach 1's task 016 went `failed` and twelve tasks
  depending on it became permanently unreachable;
* the two defects this approach found and repaired in requirement A's own
  guard mechanism (the pytest-summary-line prose false positive, task 001,
  and the discharge chain's unsatisfiable anchor, task 003).

Every check is paired with a test that mutates the *real* roadmap text (or
PRD text) into wording it replaced, so a check that stops discriminating
fails.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ROADMAP = REPO_ROOT / "docs" / "roadmap.md"
PRD = REPO_ROOT / "docs" / "prds" / "v0.14-every-hand-on-the-plan.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"
INIT = REPO_ROOT / "src" / "ralphd" / "__init__.py"

DEFERRED_HEADING = "## Later / explicitly deferred"

# The milestone this module is about. Pinned, not derived: see the module
# docstring for why deriving it from `pyproject.toml` would defang the
# version-agreement check below.
SERIES = (0, 14)

# The ten issues the PRD's own "Nine issues" sentence names -- nine the wave
# owns plus #91, which the sentence itself says closes as a consequence of
# #98 rather than by its own work. Fixed here, independent of the heading
# parse (which carries no issue numbers at all -- see the module docstring),
# so the citation check has something to check the block against.
PRD_ISSUES = {"103", "99", "98", "101", "102", "100", "95", "96", "88", "91"}

# All nineteen requirement letters this PRD uses, A-S, with no gaps and no
# split letter. Spelled out here, independent of the parse, so
# `test_the_prd_headings_parse_at_all` has something fixed to check the parse
# against.
PRD_LETTERS = set("ABCDEFGHIJKLMNOPQRS")

# The residual issue requirement P's own test-fidelity gap was filed
# against -- distinct from #95, the requirement issue P closes, and from the
# ten in PRD_ISSUES above (this run does not close #104 itself).
P_RESIDUAL_ISSUE = "104"


def declared_version() -> str:
    return tomllib.loads(PYPROJECT.read_text())["project"]["version"]


def declared_series() -> tuple[int, int]:
    major, minor = (int(p) for p in declared_version().split(".")[:2])
    return major, minor


def roadmap_text() -> str:
    return ROADMAP.read_text()


def milestone_block(text: str, series: tuple[int, int]) -> str | None:
    """The `## vX.Y ...` section body, or None when there is no such heading.

    Stops at the deferred heading as well as at the next milestone: the last
    milestone's block runs up against `## Later / explicitly deferred`, and a
    coverage check that swallowed the deferred list could be satisfied by an
    entry *deferring* the very requirement it is looking for.
    """
    heads = list(re.finditer(r"^## v(\d+)\.(\d+)\b", text, re.MULTILINE))
    for i, m in enumerate(heads):
        if (int(m.group(1)), int(m.group(2))) != series:
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[m.start(): end]
        return body.split(DEFERRED_HEADING, 1)[0]
    return None


def deferred_section(text: str) -> str:
    return text.split(DEFERRED_HEADING, 1)[1]


def prd_requirement_letters() -> set[str]:
    """`{"A", "B", ..., "S"}` from the PRD's own `### A \u2014 title` headings.

    Letters run `A`-`S` with no gaps, so the regex is pinned to that exact
    range rather than widened past it the way older modules widen past their
    own gaps.
    """
    return set(re.findall(r"^### ([A-S]) \u2014 ", PRD.read_text(), re.MULTILINE))


# ------------------------------------------------- the block matches the version


def _version_block_problems(text: str, series: tuple[int, int]) -> list[str]:
    v = f"v{series[0]}.{series[1]}"
    block = milestone_block(text, series)
    if block is None:
        return [f"docs/roadmap.md has no `## {v}` block for the declared version"]
    problems = []
    if "\u2705" not in block:
        problems.append(f"the `## {v}` block records nothing as shipped")
    heads = [(int(a), int(b)) for a, b in
             re.findall(r"^## v(\d+)\.(\d+)\b", text, re.MULTILINE)]
    ahead = sorted(h for h in heads if h > series)
    if ahead:
        problems.append(
            f"docs/roadmap.md has milestone blocks after the declared version: {ahead}")
    return problems


def _own_block_problems(text: str) -> list[str]:
    """`_version_block_problems` minus the "nothing after this milestone"
    half. v0.14.1, v0.14.2 and v0.15 all exist now, so that half reports
    later blocks unconditionally and would swamp the two checks that are
    about *this* block's own content. The "nothing after the declared
    version" rule is not weakened, only relocated: whichever module owns
    the declared version enforces it for that version, which is the only
    version it was ever a rule about."""
    return [p for p in _version_block_problems(text, SERIES)
            if "milestone blocks after" not in p]


def test_the_roadmap_has_a_block_for_this_milestone():
    """v0.14 shipped, so `## v0.14` must exist and record shipped work."""
    assert not _own_block_problems(roadmap_text())


def test_v014_is_a_past_milestone_of_the_declared_version():
    """The pin above is only honest while 0.14 is behind the release: if the
    version ever went back to 0.14.x, this module would be grading the wrong
    milestone. The two literals are also still checked to agree, since
    `test_packaging_metadata.py`'s rule and this pin only line up while they
    do."""
    assert declared_series() > SERIES, (
        f"pyproject declares {declared_series()}, so v0.14 is not history: this "
        "module's SERIES pin (v0.15 task 017) needs revisiting")
    m = re.search(r'^__version__ = "([^"]+)"', INIT.read_text(), re.MULTILINE)
    assert m and m.group(1) == declared_version()


def test_the_version_block_check_catches_a_missing_or_unshipped_block():
    text, series = roadmap_text(), SERIES
    heading = f"## v{series[0]}.{series[1]}"
    assert heading in text, "fixture assumption: the block is there to remove"
    # no block for v0.14 at all
    assert _own_block_problems(text.replace(heading, "## Not a milestone"))
    # a block that only *plans* the release records nothing shipped
    block = milestone_block(text, series)
    assert _own_block_problems(
        text.replace(block, block.replace("\u2705", "\u23f3")))
    # a version the roadmap has already moved past (v0.10, with later blocks
    # after it) -- this one is the unfiltered check, since it is exactly
    # what it reports
    assert _version_block_problems(text, (0, 10))


# --------------------------------------------------- the block covers the wave


def _coverage_problems(block: str) -> list[str]:
    return [f"requirement {letter} is not in the v0.14 block"
            for letter in sorted(PRD_LETTERS)
            if not re.search(rf"\*\*{letter}\.", block)]


def test_the_prd_headings_parse_at_all():
    """Guards the coverage check from passing because the PRD parse found
    nothing, and pins the PRD's own letter set so a future PRD edit that
    silently dropped or renumbered a requirement is caught here rather than
    only downstream in the coverage check."""
    assert prd_requirement_letters() == PRD_LETTERS


def test_every_prd_requirement_is_accounted_for_in_the_v014_block():
    block = milestone_block(roadmap_text(), SERIES)
    assert block is not None
    assert not _coverage_problems(block)


def test_every_prd_issue_is_cited_in_the_v014_block():
    """Every issue the PRD's own "Nine issues" sentence names (#103, #99,
    #98, #101, #102, #100, #95, #96, #88, and #91) is citable straight off
    the block -- the whole point of the block as an issue -> requirement
    index. Checked against the block as a whole, not per letter: this PRD's
    headings carry no inline issue number to key a per-letter check off of
    (see the module docstring)."""
    block = milestone_block(roadmap_text(), SERIES)
    assert block is not None
    for n in PRD_ISSUES:
        assert f"#{n}" in block, f"the v0.14 block does not cite #{n}"


def test_the_coverage_check_catches_a_dropped_requirement():
    block = milestone_block(roadmap_text(), SERIES)
    # dropping a requirement letter's bold marker removes it from coverage
    assert _coverage_problems(block.replace("**K.", "K."))


def test_the_issue_citation_check_catches_a_dropped_issue():
    block = milestone_block(roadmap_text(), SERIES)
    mutated = block.replace("#102", "the split-proposal issue")
    assert "102" not in re.findall(r"#(\d+)", mutated)


def test_the_coverage_check_catches_an_entirely_missing_block():
    """The coverage helper itself assumes it is handed a real block; the
    module-level checks above are what catch a missing block, but this pins
    that `_coverage_problems` still reports something (rather than silently
    passing) if ever called on an empty string, e.g. because `milestone_block`
    returned an empty match."""
    assert _coverage_problems("") == [
        f"requirement {letter} is not in the v0.14 block"
        for letter in sorted(PRD_LETTERS)
    ]


# --------------------------------------------------- the named residuals

def test_the_block_discloses_the_p_test_fidelity_residual_and_its_issue():
    """Requirement P's own test-fidelity gap is a named residual, not a
    footnote: its issue (#104, distinct from #95/#96, the requirement issues
    P and Q close) must be citable straight off the block, and the block
    must say the gap is a residual rather than merely mentioning the number
    in passing."""
    block = milestone_block(roadmap_text(), SERIES)
    assert block is not None
    assert f"#{P_RESIDUAL_ISSUE}" in block, (
        f"the v0.14 block does not cite the P test-fidelity residual's own "
        f"issue, #{P_RESIDUAL_ISSUE}")
    assert "residual" in block.lower(), (
        "the v0.14 block does not name the P gap as a residual")


def test_the_residual_check_catches_a_dropped_issue_citation():
    block = milestone_block(roadmap_text(), SERIES)
    mutated = block.replace(f"#{P_RESIDUAL_ISSUE}", "the pause test gap")
    assert P_RESIDUAL_ISSUE not in re.findall(r"#(\d+)", mutated)


def test_the_block_discloses_this_runs_own_doom_reproduction():
    """This run's approach 1 hit exactly the doom shape #99 describes: task
    016 went `failed` and twelve tasks depending on it, directly or
    transitively, became permanently unreachable. The block has to say so
    plainly, the same way the v0.9 block named its own unfinished G, rather
    than let the irony go unrecorded."""
    block = milestone_block(roadmap_text(), SERIES)
    assert block is not None
    assert "task 016" in block, (
        "the v0.14 block does not name the doomed task (task 016) from this "
        "run's own approach 1")
    assert "twelve" in block.lower(), (
        "the v0.14 block does not name how many tasks the doom made "
        "unreachable (twelve)")
    assert "failed" in block, (
        "the v0.14 block does not say task 016 went `failed`")


def test_the_doom_disclosure_check_catches_a_dropped_task_reference():
    block = milestone_block(roadmap_text(), SERIES)
    mutated = block.replace("task 016", "the paused-deadline task")
    assert "task 016" not in mutated


def test_the_block_names_the_two_requirement_a_repairs_with_true_status():
    """This approach found and fixed two defects in requirement A's own
    guard mechanism before trusting it for the wave's own closing sequence:
    the pytest-summary-line prose false positive (task 001) and the
    discharge chain's unsatisfiable anchor (task 003). Both must be named,
    by task id, and marked as fixed -- not merely mentioned as open
    problems."""
    block = milestone_block(roadmap_text(), SERIES)
    assert block is not None
    a_bullet_start = block.index("**A.")
    a_bullet_end = block.index("**B.", a_bullet_start)
    a_bullet = block[a_bullet_start:a_bullet_end]
    assert "task 001" in a_bullet, "requirement A's bullet does not name task 001"
    assert "task 003" in a_bullet, "requirement A's bullet does not name task 003"
    assert "fixed" in a_bullet.lower(), (
        "requirement A's bullet does not say the two defects were fixed")


def test_the_requirement_a_defect_check_catches_a_dropped_task_reference():
    block = milestone_block(roadmap_text(), SERIES)
    a_bullet_start = block.index("**A.")
    a_bullet_end = block.index("**B.", a_bullet_start)
    a_bullet = block[a_bullet_start:a_bullet_end]
    mutated = a_bullet.replace("task 003", "a later task")
    assert "task 003" not in mutated


# --------------------------------------------- what the block claims is not deferred


def test_the_deferred_list_is_unchanged_in_kind_by_this_wave():
    """v0.14 resolves neither standing deferred item (image publishing,
    PID namespaces) -- this wave has no deferred-list entry of its own to
    correct, so the only property to hold is that the section still exists
    and still names both."""
    section = deferred_section(roadmap_text())
    assert "PID-namespace" in section
    assert "Publishing the Docker image" in section
