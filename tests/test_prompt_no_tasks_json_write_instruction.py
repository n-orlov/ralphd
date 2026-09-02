"""Task 122 (requirement C, #60): a committed, mutation-tested check that no
shipped prompt under ``src/ralphd/prompts/`` tells the agent to edit, save or
otherwise write ``tasks.json``.

Tasks 118-121 moved verify/review/planning/reflect off writing the task file
directly, and task 116 already did the same for worker.md's ordinary-task-
completion path. Task 010 finished the job: worker.md's one remaining
exception -- resolving a permanently ``failed`` task by relabelling it
``skipped`` -- now rides the same `task_status` contract channel as every
other move instead of instructing a direct edit, so there is no lexical
carve-out left to grant; the scanner below accepts nothing that a plain
read would call an instruction to write ``tasks.json``.

The scanner below is deliberately simple (no real NLP): it looks for an
imperative write-ish verb ("write", "edit", "save", "modify", "update",
"overwrite", "rewrite", "touch") within a handful of words of the literal
string ``tasks.json``, then checks whether the verb is negated (preceded
close enough by "not"/"never"/"n't"/"cannot"/"without"/"no longer") -- a
negated occurrence is the desired READ-ONLY language ("you never write to
it", "do not modify tasks.json"), not an instruction. Anything left over
is a real violation.
"""

from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts"

WRITE_VERB_RE = re.compile(
    r"\b(write|edit|save|modify|update|overwrite|rewrite|touch)\b"
    r"(?:\s+\S+){0,6}?\s+tasks\.json\b",
    re.IGNORECASE,
)

NEGATION_RE = re.compile(
    r"(?:\bnot\b|\bnever\b|n't\b|\bcannot\b|\bwithout\b|\bno longer\b)\s*\S*\s*$",
    re.IGNORECASE,
)

# Task 010 removed worker.md's last lexical carve-out: resolving a
# permanently `failed` task now rides the `task_status` contract channel
# like every other move, so no accepted-exception wording is granted here
# any more -- anything the scanner flags is a real violation.
ACCEPTED_EXCEPTION_SUBSTRINGS: tuple[str, ...] = ()


def _preceding_window(text: str, start: int, width: int = 40) -> str:
    return text[max(0, start - width):start]


def find_violations(text: str) -> list[str]:
    """Return a human-readable violation string per offending match, or an
    empty list if the text is clean."""
    violations: list[str] = []
    for m in WRITE_VERB_RE.finditer(text):
        window = _preceding_window(text, m.start())
        if NEGATION_RE.search(window):
            continue
        snippet = text[max(0, m.start() - 40):m.end() + 10].replace("\n", " ")
        if any(exc in snippet for exc in ACCEPTED_EXCEPTION_SUBSTRINGS):
            continue
        line_no = text.count("\n", 0, m.start()) + 1
        violations.append(f"line {line_no}: {snippet.strip()!r}")
    return violations


def _prompt_files() -> list[Path]:
    return sorted(PROMPTS_DIR.glob("*.md"))


def test_prompts_dir_has_files():
    files = _prompt_files()
    assert files, f"expected prompt files under {PROMPTS_DIR}"


def test_no_shipped_prompt_instructs_writing_tasks_json():
    offenders: dict[str, list[str]] = {}
    for path in _prompt_files():
        found = find_violations(path.read_text())
        if found:
            offenders[path.name] = found
    assert not offenders, (
        "shipped prompts must never instruct the agent to write tasks.json "
        "(the harness owns that file; ordinary work reports through the "
        f"contract instead):\n{offenders}"
    )


def test_the_check_actually_catches_an_injected_instruction(tmp_path):
    """Mutation case: prove the scanner can fail. Copy a real, currently
    clean prompt into a tmp directory, inject an unambiguous instruction to
    write tasks.json into that tmp copy, and assert the scanner rejects it --
    while the untouched original still passes. If this test ever passes
    vacuously (mutated copy also comes back clean) the scanner has gone
    blind."""
    original_path = PROMPTS_DIR / "task-verify.md"
    original = original_path.read_text()
    assert not find_violations(original), (
        "fixture assumption broken: task-verify.md is expected to already "
        "be clean before mutation"
    )

    tmp_copy = tmp_path / original_path.name
    tmp_copy.write_text(
        original
        + "\n\nWrite the verdict directly into tasks.json before you finish.\n"
    )

    # the untouched original is unaffected by the tmp mutation
    assert not find_violations(original_path.read_text())

    violations = find_violations(tmp_copy.read_text())
    assert violations, "mutated tmp copy with an injected write-instruction must be rejected"
    assert any("tasks.json" in v for v in violations)


def test_negated_write_language_is_not_flagged():
    """Sanity check on the scanner's negation carve-out using synthetic
    text that mirrors the real prompts' actual wording, so the real-file
    test above is trusted to mean something rather than passing by luck."""
    clean_examples = [
        "Read the task state file (`tasks.json`, read-only -- you never "
        "write to it, see below).",
        "do not complete other tasks, do not modify tasks.json at all",
        "Do not touch the run state directory except tasks.json, the notes "
        "file, and the artifacts directory.",
        "`tasks.json` is read-only to you; you never edit or save it",
    ]
    for text in clean_examples:
        assert not find_violations(text), f"false positive on: {text!r}"


def test_unnegated_write_language_is_flagged():
    dirty_examples = [
        "Write your completed status back into tasks.json now.",
        "Edit tasks.json to mark the task complete.",
        "Save tasks.json with the new status before you stop.",
    ]
    for text in dirty_examples:
        assert find_violations(text), f"false negative on: {text!r}"
