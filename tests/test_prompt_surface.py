"""Feature module for the subject below (task 028, requirement D.7): the
per-task pin modules listed here are folded in whole -- every test body,
assertion and docstring verbatim, only the module they live in changed.
Sources absorbed (each was `tests/<name>`, deleted in the same commit, with
one `docs/test-suite-audit.md` § "Subsumed" row per node):

- `test_prompt_failed_task_resolution.py` -- worker.md must tell a worker how to resolve a terminally failed task (#36). A task whose validation attempts are exhausted is recorded `failed`, and a `failed` task can never satisfy the completion signal's "every task is terminal" gate. A worker that does not know what to do with it grinds until th
- `test_prompt_no_tasks_json_write_instruction.py` -- Task 122 (requirement C, #60): a committed, mutation-tested check that no shipped prompt under ``src/ralphd/prompts/`` tells the agent to edit, save or otherwise write ``tasks.json``. Tasks 118-121 moved verify/review/planning/reflect off writing the task file directly, and task 116 already did the 
- `test_reflect_prompt_scope_task026.py` -- Reflect's improvement scope must be the WHOLE shipped prompt surface, not a stale enumeration (F.1, #126). `reflect.md:27` used to hard-code "prompts (`planning`, `worker`, `review`, `task-verify`)" -- four names out of the eight files that actually ship under `src/ralphd/prompts/`. Adding a ninth h
- `test_skills_notify_preference_injection_task003.py` -- Task 003 (finding 3, PRD requirement B.3): the live `telegram-notify` skill pi actually reads is `$HOME/.pi/agent/skills/telegram-notify/ SKILL.md`, placed by `skills.place_skills()` from a host-supplied, untracked mounted (or api) source directory -- `examples/skills/ telegram-notify/SKILL.md` in t
- `test_stale_wiring_docstrings_task003.py` -- Task 003 (approach 2, review-findings fix): two docstrings had gone stale relative to the code around them -- - the supervisor's `_ask_contract_fix` method (`src/ralphd/engine/loop.py`) said "Wired to nothing yet (task 002)" long after v0.13 tasks 003-007 (`2be8b86`, `edbcb79`, `1311548`, `8599b7b`,

Renamed to resolve cross-source collisions of module-private names (no test
function was renamed):
- `PROMPTS_DIR` -> `pfr_PROMPTS_DIR` (from `test_prompt_failed_task_resolution.py`)
- `PROMPTS_DIR` -> `pnjw_PROMPTS_DIR` (from `test_prompt_no_tasks_json_write_instruction.py`)
- `PROMPTS_DIR` -> `rps026_PROMPTS_DIR` (from `test_reflect_prompt_scope_task026.py`)
"""

from __future__ import annotations


# ----------------------------------------------------------------------
# Folded from tests/test_prompt_failed_task_resolution.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import re
from pathlib import Path

pfr_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts"
WORKER = pfr_PROMPTS_DIR / "worker.md"


def _worker_text() -> str:
    assert WORKER.is_file(), f"missing prompt file {WORKER}"
    return WORKER.read_text()


def _completion_signal_section() -> str:
    """The text of worker.md's `## Completion signal` section."""
    text = _worker_text()
    m = re.search(r"^##\s+Completion signal\s*$(.*?)(?=^##\s|\Z)",
                  text, re.MULTILINE | re.DOTALL)
    assert m, "worker.md has no '## Completion signal' section"
    return m.group(1)


def _assert_all(rule: str, patterns: list[str]) -> None:
    section = _completion_signal_section()
    missing = [p for p in patterns
               if not re.search(p, section, re.IGNORECASE | re.DOTALL)]
    assert not missing, (
        "worker.md's completion-signal section is missing the "
        f"'{rule}' rule for a terminally failed task; no match for: {missing}")


def test_completion_signal_names_the_terminal_statuses():
    _assert_all("terminal statuses", [
        r"terminal status",
        r"`completed`",
        r"`skipped`",
    ])


def test_failed_task_is_stated_to_block_the_completion_signal_forever():
    _assert_all("failed blocks the signal forever", [
        r"task left `failed` blocks",
        r"forever",
        r"validation attempts are exhausted",
    ])


def test_the_danger_of_a_permanently_blocked_plan_is_spelled_out():
    _assert_all("why blocking forever is unsafe", [
        r"fail the approach and replan",
        r"from scratch",
        r"work is already finished",
        r"no review pass",
    ])


def test_resolution_a_carve_the_residual_gap_into_a_new_task():
    _assert_all("carve the gap into a new task", [
        r"residual gap",
        r"carve[^\n]*NEW task",
        r"stand as history",
    ])


def test_resolution_b_relabel_skipped_with_an_honest_justification():
    _assert_all("relabel skipped with a justification", [
        r"relabel the failed task `skipped`",
        r"justification",
        r"what its criteria required",
        r"which commits delivered",
        r"`skipped` is not a claim that the criteria were met",
    ])


def test_relabelling_skipped_leaves_validation_attempts_untouched():
    _assert_all("validationAttempts stays untouched", [
        r"[Ll]eave `validationAttempts` exactly as it stands",
    ])


def test_editing_success_criteria_to_pass_is_prohibited():
    _assert_all("no editing successCriteria", [
        r"[Nn]ever edit a task's `successCriteria` to make it pass",
    ])


def test_resolving_one_task_does_not_touch_another_task_status():
    _assert_all("do not touch other tasks", [
        r"never change another\s*\n?\s*task's status",
    ])


# ----------------------------------------------------------------------
# Folded from tests/test_prompt_no_tasks_json_write_instruction.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import re
from pathlib import Path

pnjw_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts"

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
    return sorted(pnjw_PROMPTS_DIR.glob("*.md"))


def test_prompts_dir_has_files():
    files = _prompt_files()
    assert files, f"expected prompt files under {pnjw_PROMPTS_DIR}"


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
    original_path = pnjw_PROMPTS_DIR / "task-verify.md"
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


# ----------------------------------------------------------------------
# Folded from tests/test_reflect_prompt_scope_task026.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import re
import shutil
from pathlib import Path

rps026_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts"

_BACKTICK_RE = re.compile(r"`([^`]+)`")


def _prompt_text(name: str) -> str:
    path = rps026_PROMPTS_DIR / f"{name}.md"
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
    stems = _shipped_stems(rps026_PROMPTS_DIR)
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
    stems = _shipped_stems(rps026_PROMPTS_DIR)
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
    shutil.copytree(rps026_PROMPTS_DIR, shadow_dir)

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


# ----------------------------------------------------------------------
# Folded from tests/test_skills_notify_preference_injection_task003.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

from pathlib import Path

from ralphd.engine import skills


def _mount(config_dir: Path, name: str, skill_md: str) -> Path:
    d = config_dir / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(skill_md)
    return d


def _placed_md(home: Path, name: str) -> Path:
    return home / ".pi" / "agent" / "skills" / name / "SKILL.md"


NO_INSTRUCTION_SRC = (
    "---\nname: telegram-notify\ndescription: x\n---\n\n"
    "# Telegram notifications to the operator\n\n"
    "Some body text that must survive verbatim.\n"
)


def test_telegram_notify_missing_instruction_is_injected_at_placement(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setattr(skills, "CONFIG_DIR", config_dir)
    _mount(config_dir, "telegram-notify", NO_INSTRUCTION_SRC)
    home = tmp_path / "home"
    home.mkdir()

    placed = skills.place_skills(config_dir, home=home)
    assert placed == ["telegram-notify"]

    link = home / ".pi" / "agent" / "skills" / "telegram-notify"
    assert link.is_symlink() and link.is_dir()

    text = _placed_md(home, "telegram-notify").read_text()
    assert skills.NOTIFY_PREFERENCE_MARKER in text
    assert "notify" in text.lower()
    # everything from the source is preserved, unmodified, ahead of the
    # appended instruction -- never rewritten in place.
    assert text.startswith(NO_INSTRUCTION_SRC.rstrip("\n"))
    assert text != NO_INSTRUCTION_SRC


def test_telegram_notify_source_already_carrying_it_is_placed_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setattr(skills, "CONFIG_DIR", config_dir)
    already = (
        "---\nname: telegram-notify\n---\n\n# Telegram\n\n"
        f"## {skills.NOTIFY_PREFERENCE_MARKER}\n\n"
        "Already here, verbatim.\n"
    )
    _mount(config_dir, "telegram-notify", already)
    home = tmp_path / "home"
    home.mkdir()

    skills.place_skills(config_dir, home=home)

    assert _placed_md(home, "telegram-notify").read_text() == already


def test_a_differently_named_skill_is_placed_byte_identical_to_its_source(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setattr(skills, "CONFIG_DIR", config_dir)
    src_content = "# some-other-skill\n\nDoes a thing. Contains `notify` too.\n"
    src_dir = _mount(config_dir, "some-other-skill", src_content)
    home = tmp_path / "home"
    home.mkdir()

    skills.place_skills(config_dir, home=home)

    placed_md = _placed_md(home, "some-other-skill")
    assert placed_md.read_text() == src_content
    assert placed_md.read_text() == (src_dir / "SKILL.md").read_text()
    # still a plain symlink straight at the source -- no materialization
    # for any name other than telegram-notify.
    link = home / ".pi" / "agent" / "skills" / "some-other-skill"
    assert link.is_symlink()
    assert link.resolve() == src_dir.resolve()


def test_other_files_alongside_telegram_notifys_skill_md_are_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setattr(skills, "CONFIG_DIR", config_dir)
    src_dir = _mount(config_dir, "telegram-notify", NO_INSTRUCTION_SRC)
    (src_dir / "helper.sh").write_text("#!/bin/sh\necho hi\n")
    home = tmp_path / "home"
    home.mkdir()

    skills.place_skills(config_dir, home=home)

    helper = home / ".pi" / "agent" / "skills" / "telegram-notify" / "helper.sh"
    assert helper.read_text() == "#!/bin/sh\necho hi\n"


def test_re_placement_picks_up_a_changed_source_each_time(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setattr(skills, "CONFIG_DIR", config_dir)
    src_dir = _mount(config_dir, "telegram-notify", NO_INSTRUCTION_SRC)
    home = tmp_path / "home"
    home.mkdir()

    skills.place_skills(config_dir, home=home)
    first = _placed_md(home, "telegram-notify").read_text()
    assert skills.NOTIFY_PREFERENCE_MARKER in first

    updated_src = NO_INSTRUCTION_SRC + "\nAn extra paragraph added later.\n"
    (src_dir / "SKILL.md").write_text(updated_src)

    skills.place_skills(config_dir, home=home)
    second = _placed_md(home, "telegram-notify").read_text()
    assert skills.NOTIFY_PREFERENCE_MARKER in second
    assert "An extra paragraph added later." in second
    assert second.startswith(updated_src.rstrip("\n"))


# ----------------------------------------------------------------------
# Folded from tests/test_stale_wiring_docstrings_task003.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

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
