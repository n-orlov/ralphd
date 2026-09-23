"""Task 039 (#170, PRD requirement I.1): no shipped hat prompt shows its
contract object inside a code fence.

The incident this closes, diagnosed in the reflection that landed as #170: a
model whose FINAL text is prose followed by a fenced ```json block carrying
the correct contract payload can end its own reply with the fence's CLOSING
line (```` ``` ````) rather than the JSON object itself -- `runner`'s "last
non-blank line" check reads the closing fence, not the payload, and the
contract is broken by construction. `tests/test_contract_reask_grounding.py`
already covers the ENGINE's own recovery once that has happened (extracting
a fenced payload from a hat's transcript); this module is the other half --
never showing the model a fenced example to imitate in the first place.

Two independent checks, over every `*.md` file actually shipped under
`src/ralphd/prompts/` (never a fixed list -- a new hat prompt is scanned too,
the same "scope is the directory, not a snapshot" convention
`tests/test_prompt_surface.py` already pins for the reflect hat's own prompt
scope):

1. **No contract object inside a fence.** A "contract object" here is any
   fenced code block whose content contains one of the top-level keys the
   shipped hats' own JSON contracts are keyed on (`"verdict"`, `"settled"`)
   -- `planning.md`'s one surviving fence (the
   `plan-proposal.json` FILE schema, pinned open by
   `tests/test_estimated_iterations.py`) carries none of those keys and is
   deliberately left alone; it is a schema for a file the planner writes,
   never the reply contract a fence's closing line could stomp on. Task 007
   (#211) retired `"report_written"` from this list along with the field
   itself -- `reflect`'s own contract no longer declares it (see
   `transitions.HAT_SCHEMAS["reflect"]`), so it is no longer one of the
   keys a fenced block could be mistaken for.
2. **The sentence.** `steer.md`, `reflect.md`, `planning.md` and `worker.md`
   each state, in prose, that a fence's closing line breaks the contract --
   the corollary that explains WHY check 1 matters, so a future edit that
   re-fences an example trips this module's own sentence check even if it
   somehow dodged the key scan.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO / "src" / "ralphd" / "prompts"

FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)

# The top-level keys that mark a fenced block's content as one of the
# shipped hats' own reply-contract objects, never a mere schema example.
CONTRACT_KEYS = ('"verdict"', '"settled"')

# Prompts that must carry the "a fence's closing line breaks the contract"
# sentence (#170 names exactly these four).
MUST_CARRY_SENTENCE = ("steer.md", "reflect.md", "planning.md", "worker.md")

# Substrings of the sentence -- the wording differs slightly per file, but
# each occurrence carries all three ideas: a fence's closing line, becoming
# the reply's own last line, and that breaking the contract.
SENTENCE_NEEDLES = (
    "closing line",
    "last non-blank line",
    "breaks the contract",
)


def _prompt_files() -> list[Path]:
    files = sorted(PROMPTS_DIR.glob("*.md"))
    assert files, f"no prompt files found under {PROMPTS_DIR}"
    return files


def _fences(text: str) -> list[str]:
    return FENCE_RE.findall(text)


def test_the_prompts_directory_is_scanned_not_a_fixed_list():
    """Pin the scan against the real directory listing, so a ninth hat
    prompt is covered automatically -- never a hard-coded filename tuple
    a future addition could silently miss."""
    names = {p.name for p in _prompt_files()}
    for expected in MUST_CARRY_SENTENCE:
        assert expected in names, f"expected shipped prompt {expected!r} missing"


def test_no_contract_object_appears_inside_a_code_fence():
    offenders = []
    for path in _prompt_files():
        for fence in _fences(path.read_text()):
            for key in CONTRACT_KEYS:
                if key in fence:
                    offenders.append((path.name, key, fence.strip()[:80]))
    assert offenders == [], (
        "a contract object is shown fenced (its closing line risks becoming "
        f"the reply's own last non-blank line): {offenders}"
    )


def test_plannings_one_surviving_fence_is_the_file_schema_not_a_contract_object():
    """The one deliberate exception, named so a future contract key added to
    that schema example is caught immediately rather than waved through by
    the general scan above."""
    text = (PROMPTS_DIR / "planning.md").read_text()
    fences = _fences(text)
    assert len(fences) == 1, "planning.md's fence count changed -- re-check the exception"
    for key in CONTRACT_KEYS:
        assert key not in fences[0], (
            f"planning.md's surviving fence now carries {key!r} -- it is no "
            "longer just the plan-proposal.json file schema"
        )


def test_each_named_prompt_carries_the_fence_closing_line_sentence():
    missing = []
    for name in MUST_CARRY_SENTENCE:
        text = (PROMPTS_DIR / name).read_text()
        for needle in SENTENCE_NEEDLES:
            if needle not in text:
                missing.append((name, needle))
    assert missing == [], (
        f"missing the 'a fence's closing line breaks the contract' sentence: {missing}"
    )


def test_steer_reports_its_outcome_bare():
    text = (PROMPTS_DIR / "steer.md").read_text()
    assert '{"settled": true}' in text
    assert '```json\n{"settled": true}' not in text


def test_reflect_reports_its_outcome_bare():
    text = (PROMPTS_DIR / "reflect.md").read_text()
    assert '{"notes": ""}' in text
    assert '```json\n{"notes"' not in text
    assert '"report_written"' not in text, (
        "reflect.md still asks the agent for report_written (task 007, #211)")
    assert '"suggestions_written"' not in text, (
        "reflect.md still asks the agent for suggestions_written (task 007, #211)")


def test_worker_shows_no_fenced_verdict_examples():
    text = (PROMPTS_DIR / "worker.md").read_text()
    assert "```" not in text, "worker.md must ship with no fenced code block at all"
    # the two examples this task un-fenced are still present, just bare
    assert '"debt_resolved": [' in text
    assert '"petition": {"obligation"' in text
