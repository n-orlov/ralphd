"""Task 003 (approach 2, review-findings fix): two docstrings had gone
stale relative to the code around them --

- the supervisor's `_ask_contract_fix` method (`src/ralphd/engine/loop.py`) said
  "Wired to nothing yet (task 002)" long after v0.13 tasks 003-007
  (`2be8b86`, `edbcb79`, `1311548`, `8599b7b`, `aa0a7b1`) gave it four live
  callers via `contract_enforcement.enforce_contract`'s `respond` hook;
- the `contract_enforcement` module docstring said "Nothing in
  `_run_job_core` calls this yet" for the same reason.

Both were corrected in this task's commit to name their live callers by
sha instead. This guard makes the same drift detectable mechanically
instead of by prose review alone: it fails if either docstring contains a
"not wired yet" / "nothing calls this yet" claim WHILE a live caller of
the thing it describes still exists in `loop.py` -- exactly the
contradiction that made the original claims false. It stays green as long
as either the claim is absent, or the callers really are gone too (a
future revert that removes the cutover AND restores the disclaimer is not
a violation).

Red-run proof (not re-run here on purpose -- see the last test's
docstring for why): with the two removed phrases pasted back verbatim
into the real files' docstrings, both guard tests below failed; the
quoted failure text is in this commit's message.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOOP_PY = REPO_ROOT / "src" / "ralphd" / "engine" / "loop.py"
CONTRACT_ENFORCEMENT_PY = (
    REPO_ROOT / "src" / "ralphd" / "engine" / "contract_enforcement.py")

# Loose enough to catch a re-worded relapse ("not yet wired", "wired to
# nothing", "nothing ... calls this yet", "no caller ... yet") without
# tripping on unrelated prose.
STALE_WIRING_RE = re.compile(
    r"wired to nothing yet|not wired yet|nothing.*calls this yet"
    r"|no caller.*yet",
    re.IGNORECASE,
)


def _ask_contract_fix_docstring(text: str) -> str:
    marker = "async def _ask_contract_fix("
    start = text.index(marker)
    doc_start = text.index('"""', start)
    doc_end = text.index('"""', doc_start + 3)
    return text[doc_start:doc_end + 3]


def _module_docstring(text: str) -> str:
    assert text.startswith('"""')
    end = text.index('"""', 3)
    return text[:end + 3]


def _live_ask_contract_fix_callers(loop_text: str) -> int:
    # Callers of `_ask_contract_fix`, not its own definition line.
    return len(re.findall(r"self\._ask_contract_fix\(", loop_text))


def _live_enforce_contract_callers(loop_text: str) -> int:
    return len(re.findall(r"contract_enforcement\.enforce_contract\s*,",
                           loop_text))


def test_ask_contract_fix_docstring_is_not_stale_while_it_has_live_callers():
    loop_text = LOOP_PY.read_text()
    doc = _ask_contract_fix_docstring(loop_text)
    callers = _live_ask_contract_fix_callers(loop_text)
    assert callers >= 1, "expected _ask_contract_fix to have live callers"
    assert not STALE_WIRING_RE.search(doc), (
        f"_ask_contract_fix's docstring re-acquired a 'not wired yet' style "
        f"claim while {callers} live caller(s) of _ask_contract_fix exist "
        f"in loop.py -- name the callers instead:\n{doc}")


def test_contract_enforcement_module_docstring_is_not_stale_while_enforce_contract_has_live_callers():
    ce_text = CONTRACT_ENFORCEMENT_PY.read_text()
    doc = _module_docstring(ce_text)
    loop_text = LOOP_PY.read_text()
    callers = _live_enforce_contract_callers(loop_text)
    assert callers >= 1, "expected enforce_contract to have live callers in loop.py"
    assert not STALE_WIRING_RE.search(doc), (
        f"contract_enforcement's module docstring re-acquired a 'nothing "
        f"calls this yet' style claim while {callers} live caller(s) of "
        f"enforce_contract exist in loop.py -- name the callers "
        f"instead:\n{doc}")


def test_the_stale_wiring_regex_matches_both_original_phrasings():
    """The guard's regex is not vacuous: it must match the EXACT two
    phrasings this task removed, so a verbatim relapse of either one is
    caught, not just a hypothetical reword. (A live re-insertion of these
    exact phrases into the real files, proving the two tests above go red
    on it, was run by hand for this commit -- see the commit message for
    the quoted failure output; it is not repeated here because leaving the
    relapse IN the tree, even temporarily inside a test, would itself be
    exactly the stale claim this guard exists to catch.)
    """
    original_ask_contract_fix_phrase = (
        "Wired to nothing yet (task 002): no caller passes this to")
    original_contract_enforcement_phrase = (
        "Nothing in `_run_job_core` calls this yet.")
    assert STALE_WIRING_RE.search(original_ask_contract_fix_phrase)
    assert STALE_WIRING_RE.search(original_contract_enforcement_phrase)
