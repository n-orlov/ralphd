"""Reflect's improvement scope must be the WHOLE shipped prompt surface, not a
stale enumeration (F.1, #126).

`reflect.md:27` used to hard-code "prompts (`planning`, `worker`, `review`,
`task-verify`)" -- four names out of the eight files that actually ship under
`src/ralphd/prompts/`. Adding a ninth hat (or renaming/removing one) would
silently widen the gap between what reflect.md tells the reflection phase to
consider and what the loop actually runs, with nothing to catch the drift.

Task 026 replaces the enumeration with the full, explicit shipped set and
requires a lexical test that:

  1. asserts reflect.md currently names every prompt stem that exists in
     `src/ralphd/prompts/` right now, and
  2. DEMONSTRATES that the same comparison catches drift -- injecting an
     extra prompt file (simulating a "ninth hat" added without updating
     reflect.md) into a throwaway copy of the directory and showing the
     comparison flags it as un-named.

The comparison logic is a single small helper (`_mentioned_stems`) shared by
both tests, so there is exactly one place that decides "is this prompt named
in reflect.md's text" and both the real check and the injected-drift
demonstration exercise it.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts"

_BACKTICK_RE = re.compile(r"`([^`]+)`")


def _prompt_text(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    assert path.is_file(), f"missing prompt file {path}"
    return path.read_text()


def _shipped_stems(prompts_dir: Path) -> set[str]:
    return {p.stem for p in prompts_dir.glob("*.md")}


def _mentioned_stems(text: str, candidate_stems: set[str]) -> set[str]:
    """Return the subset of candidate_stems that text names, backtick-quoted,
    either as the bare stem (`planning`) or with the .md suffix
    (`planning.md`)."""
    mentioned: set[str] = set()
    for token in _BACKTICK_RE.findall(text):
        token = token.strip()
        if token.endswith(".md"):
            token = token[:-3]
        if token in candidate_stems:
            mentioned.add(token)
    return mentioned


# ---- 1. reflect.md's named scope must match the real shipped set today ----

def test_reflect_names_every_currently_shipped_prompt():
    stems = _shipped_stems(PROMPTS_DIR)
    assert stems, "expected at least one shipped prompt file to compare against"

    text = _prompt_text("reflect")
    mentioned = _mentioned_stems(text, stems)

    missing = stems - mentioned
    assert not missing, (
        "reflect.md's improvement scope omits shipped prompt(s) "
        f"{sorted(missing)}; step 3 must name the whole shipped prompt set, "
        "not a stale subset (F.1, #126)"
    )


def test_reflect_scope_is_not_the_old_four_prompt_subset():
    # Guard the specific regression this task fixes: the four-prompt
    # enumeration must not have simply been left in place alongside new
    # names -- the real gap (plan_review/steer/verdict_gate/reflect itself
    # being unnamed) must be closed, not papered over.
    stems = _shipped_stems(PROMPTS_DIR)
    old_subset = {"planning", "worker", "review", "task-verify"}
    assert old_subset < stems, "sanity: the old four names should still exist as files"

    text = _prompt_text("reflect")
    mentioned = _mentioned_stems(text, stems)
    assert mentioned != old_subset, (
        "reflect.md still only names the old four-prompt subset; it must "
        "name the full shipped set"
    )


# ---- 2. demonstrate the comparison catches drift: inject an extra prompt --

def test_injected_extra_prompt_is_caught_as_unnamed(tmp_path):
    shadow_dir = tmp_path / "prompts"
    shutil.copytree(PROMPTS_DIR, shadow_dir)

    extra_stem = "extra_hat_task026"
    (shadow_dir / f"{extra_stem}.md").write_text("# stub role for a hat that doesn't exist yet\n")

    # simulate: a ninth hat lands in the prompts directory, but nobody
    # touched reflect.md's text to name it.
    stems_after_addition = _shipped_stems(shadow_dir)
    assert extra_stem in stems_after_addition

    reflect_text = _prompt_text("reflect")
    mentioned = _mentioned_stems(reflect_text, stems_after_addition)

    missing = stems_after_addition - mentioned
    assert extra_stem in missing, (
        "the comparison failed to catch an injected prompt added without "
        "updating reflect.md -- this is the exact drift the lexical pin "
        "exists to catch"
    )


def test_mentioned_stems_helper_matches_exactly_the_named_set():
    # Sanity on the helper itself: bare-name and .md-suffixed backtick
    # mentions both count, unrelated backtick text does not.
    text = "See `planning` and `worker.md`, also `not-a-real-prompt`."
    candidates = {"planning", "worker", "review"}
    assert _mentioned_stems(text, candidates) == {"planning", "worker"}
