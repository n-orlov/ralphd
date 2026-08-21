"""Task 043c (#22): SPEC.md's section 10 tables *are* `ralphctl`'s real surface.

SPEC.md is the document a reader trusts for "what does this thing do", and its
section 10 states the CLI twice in table form: 10.1 lists every command, 10.2
lists every `start` flag. Both were written by hand and both had drifted -- the
command table promised an `llm set` that never existed and omitted the four
detail commands this wave added (`iteration`, `fault`, `cost`, `docs`), while
the start-flag table predated `--price-strategy`, `--base-image` and
`--dockerfile`. Prose can be reviewed; a table of names cannot be reviewed
often enough to stay true, so it is derived here instead:

  * every subcommand `cli.main.build_parser` registers has a row, and no row
    names a command argparse does not know (`logsf`, the documented pure alias,
    is the one hatch);
  * for the commands that take an action (`skills`, `creds`, `prompts`, `llm`,
    `config`), the actions the row claims are exactly that subparser's own
    choices -- which is what makes the `llm set` claim a failure rather than a
    typo nobody notices;
  * every `--flag` in the 10.2 table is a real `start` flag and every real
    `start` flag is in the table, both directions;
  * the `--sort` keys and the "N sortable columns" count in 10.6/11.2 come from
    `RUN_SORT_KEYS` and from `app.js`'s own `RUN_COLUMNS`, so the CLI, the hub
    and the spec cannot disagree about the key set or its size.

Each check is paired with a test asserting it fails on the *old* wording, and
one empirical test runs the four detail commands over a hand-written run dir
with no container at all, which is what section 10.1's "read nothing but the run
dir" and 10.6's `--json` claims mean in practice.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from ralphd.cli.main import RUN_SORT_KEYS, build_parser
from tests.conftest import RALPHCTL

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC = REPO_ROOT / "SPEC.md"
APP_JS = REPO_ROOT / "src" / "ralphd" / "cli" / "web" / "app.js"

# `logsf <id>` is a pure alias for `logs <id> -f`, rewritten by
# _preprocess_logs_argv() before argparse sees it, so it is deliberately absent
# from the parser's own choices (docs/cli.md says the same).
ALIAS_VERBS = {"logsf"}

COUNT_WORDS = {5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine",
               10: "ten", 11: "eleven", 12: "twelve"}


def _spec_section(heading: str) -> str:
    """One `### `-level SPEC section, heading line included."""
    lines = SPEC.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("#") and not lines[i].startswith("####"):
            end = i
            break
    return "\n".join(lines[start:end])


def _table_rows(section: str) -> list[list[str]]:
    """Markdown table rows as cell lists, header and separator dropped."""
    rows = []
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or set(cells[0]) <= set("-: "):
            continue
        rows.append(cells)
    return rows


# --------------------------------------------------------------------------
# the parser's own surface
# --------------------------------------------------------------------------

def _subcommands() -> dict[str, set[str] | None]:
    """{verb: its action choices, or None when it takes no action}."""
    sub = build_parser()._subparsers._group_actions[0]
    surface: dict[str, set[str] | None] = {}
    for verb, command in sub.choices.items():
        inner = [a for a in command._actions
                 if isinstance(a, argparse._SubParsersAction)]
        surface[verb] = set(inner[0].choices) if inner else None
    return surface


def _start_flags() -> set[str]:
    sub = build_parser()._subparsers._group_actions[0]
    flags = set()
    for action in sub.choices["start"]._actions:
        flags.update(action.option_strings)
    return flags - {"-h", "--help"}


# --------------------------------------------------------------------------
# 10.1: the command table
# --------------------------------------------------------------------------

def _table_verbs(section: str) -> dict[str, str]:
    """{verb: the whole row text} for section 10.1's command table."""
    found = {}
    for cells in _table_rows(section):
        m = re.match(r"`([a-z][a-z-]*)", cells[0])
        if m:
            found[m.group(1)] = " ".join(cells)
    return found


def _command_table_problems(section: str,
                            surface: dict[str, set[str] | None]) -> list[str]:
    documented = _table_verbs(section)
    problems = []
    for verb in sorted(set(surface) - set(documented)):
        problems.append(f"command table has no row for `{verb}`")
    for verb in sorted(set(documented) - set(surface) - ALIAS_VERBS):
        problems.append(f"command table names `{verb}`, which argparse rejects")
    return problems


def _action_vocabulary(surface: dict[str, set[str] | None]) -> set[str]:
    """Every word that is an action of *some* command -- the vocabulary a row
    may claim from, and the reason `set` in the `llm` row is catchable: it is a
    real action word, of `prompts` and `config`, not of `llm`."""
    words: set[str] = set()
    for actions in surface.values():
        words |= actions or set()
    return words


def _action_problems(section: str,
                     surface: dict[str, set[str] | None]) -> list[str]:
    vocabulary = _action_vocabulary(surface)
    problems = []
    for verb, row in _table_verbs(section).items():
        actions = surface.get(verb)
        if not actions:
            continue
        claimed = {word for span in re.findall(r"`([^`]*)`", row)
                   for word in re.findall(r"[a-z]+", span)}
        claimed &= vocabulary
        if claimed != actions:
            problems.append(
                f"`{verb}` row claims actions {sorted(claimed)}, "
                f"parser accepts {sorted(actions)}")
    return problems


def test_the_command_table_names_every_subcommand_and_invents_none():
    surface = _subcommands()
    problems = _command_table_problems(_spec_section("### 10.1"), surface)
    assert not problems, f"SPEC.md 10.1 disagrees with build_parser: {problems}"


def test_the_command_tables_action_lists_are_the_parsers_own_choices():
    surface = _subcommands()
    assert surface["llm"] and surface["skills"], surface
    problems = _action_problems(_spec_section("### 10.1"), surface)
    assert not problems, f"SPEC.md 10.1 misstates an action set: {problems}"


def test_the_action_check_would_catch_the_llm_set_claim():
    surface = _subcommands()
    old = ("| `llm \u2026` | `profiles`/`show`/`test` LLM profiles, `set` to "
           "rotate a live job |")
    problems = _action_problems(old, surface)
    assert problems and "llm" in problems[0], problems
    # ...and `set` really is not an `llm` action: the row was not merely worded
    # oddly, it named a command that would exit 2.
    assert "set" not in surface["llm"]
    assert "set" in _action_vocabulary(surface)  # a real word, of prompts/config
    # the check is not vacuous: the corrected row passes
    fixed = "| `llm \u2026` | `profiles`/`show`/`test` LLM profiles on the host |"
    assert not _action_problems(fixed, surface)


def test_the_command_table_check_would_catch_the_missing_detail_commands():
    surface = _subcommands()
    section = _spec_section("### 10.1")
    for verb in ("iteration", "fault", "cost", "docs"):
        without = "\n".join(line for line in section.splitlines()
                            if not line.startswith(f"| `{verb}"))
        assert _command_table_problems(without, surface), verb
    # an invented command is caught in the other direction
    invented = section + "\n| `deploy <id>` | ship it |"
    assert _command_table_problems(invented, surface)
    # the documented alias is not reported as invented
    assert not _command_table_problems(
        section + "\n| `logsf <id>` | alias for `logs -f` |", surface)


def test_the_command_table_check_is_substantive():
    surface = _subcommands()
    assert len(surface) >= 25, sorted(surface)
    assert len(_table_verbs(_spec_section("### 10.1"))) == len(surface)


# --------------------------------------------------------------------------
# 10.2: the start-flag table
# --------------------------------------------------------------------------

def _documented_start_flags(section: str) -> set[str]:
    """Every `--flag` named in the first column of 10.2's flag table."""
    flags = set()
    for cells in _table_rows(section):
        flags.update(re.findall(r"`(--[a-z][a-z-]*)", cells[0]))
    return flags


def _start_flag_problems(section: str, real: set[str]) -> list[str]:
    documented = _documented_start_flags(section)
    problems = []
    for flag in sorted(real - documented):
        problems.append(f"start flag {flag} is in no table row")
    for flag in sorted(documented - real):
        problems.append(f"table documents {flag}, which `start` does not accept")
    return problems


def test_the_start_flag_table_is_the_start_parsers_own_flag_set():
    problems = _start_flag_problems(_spec_section("### 10.2"), _start_flags())
    assert not problems, f"SPEC.md 10.2 disagrees with build_parser: {problems}"


def test_the_start_flag_check_would_catch_the_missing_v06_flags():
    real = _start_flags()
    section = _spec_section("### 10.2")
    for flag in ("--price-strategy", "--base-image", "--dockerfile"):
        assert flag in real
        without = "\n".join(line for line in section.splitlines()
                            if not line.startswith(f"| `{flag}"))
        assert _start_flag_problems(without, real), flag
    invented = section + "\n| `--retries N` | `3` | how many times |"
    assert _start_flag_problems(invented, real)


def test_the_start_flag_check_is_substantive():
    flags = _documented_start_flags(_spec_section("### 10.2"))
    assert len(flags) >= 30, sorted(flags)
    assert "--prd" in flags and "--no-detach" in flags


# --------------------------------------------------------------------------
# 10.6 / 11.2: the sort keys, and how many there are
# --------------------------------------------------------------------------

def _hub_column_keys() -> set[str]:
    block = re.search(r"const RUN_COLUMNS = \[(.*?)\n\];",
                      APP_JS.read_text(), re.DOTALL)
    assert block, "app.js no longer defines RUN_COLUMNS"
    return set(re.findall(r'key:\s*"([A-Za-z]+)"', block.group(1)))


def _documented_sort_keys(text: str) -> set[str]:
    flat = re.sub(r"\s+", "", text)
    m = re.search(r"--sort\{([A-Za-z,]+)\}", flat)
    assert m, "SPEC.md no longer shows `ralphctl runs --sort`'s key set"
    return set(m.group(1).split(","))


def _count_word_problems(text: str, expected: int) -> list[str]:
    """Every spelled count in `text` must be the real key count."""
    want = COUNT_WORDS[expected]
    problems = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for word in re.findall(r"\b([a-z]+)\b", line):
            if word in COUNT_WORDS.values() and word != want:
                problems.append(f"{lineno}: spelled count {word!r}, "
                                f"there are {expected} ({want})")
    return problems


def test_the_documented_sort_keys_are_the_cli_and_the_hubs_own_key_set():
    keys = set(RUN_SORT_KEYS)
    assert keys == _hub_column_keys(), "CLI and hub sort dialects diverged"
    section = _spec_section("### 10.6")
    assert _documented_sort_keys(section) == keys, (
        f"SPEC.md 10.6 documents {sorted(_documented_sort_keys(section))}, "
        f"RUN_SORT_KEYS has {sorted(keys)}")
    assert "tasks" in keys  # task 015 (#21): the column this wave added
    problems = _count_word_problems(
        "\n".join(line for line in section.splitlines() if "sort" in line
                  or "column" in line), len(keys))
    assert not problems, f"SPEC.md 10.6 miscounts the sort keys: {problems}"


def test_the_run_list_columns_are_the_sort_keys_in_the_hub_section():
    keys = set(RUN_SORT_KEYS)
    section = _spec_section("### 11.2")
    intro = "\n".join(section.splitlines()[:8])
    problems = _count_word_problems(intro, len(keys))
    assert not problems, f"SPEC.md 11.2 miscounts the run-list columns: {problems}"
    assert "`TASKS`" in intro, (
        "SPEC.md 11.2 lists the run-list columns, so the TASKS column belongs "
        "in the list")


def test_the_count_check_would_catch_the_seven_key_claim():
    keys = set(RUN_SORT_KEYS)
    old = ("**Those seven keys are exactly the hub's seven sortable columns, "
           "and both surfaces use one implementation**")
    assert len(keys) == 8, sorted(keys)
    assert len(_count_word_problems(old, len(keys))) == 2
    assert not _count_word_problems(old.replace("seven", "eight"), len(keys))


# --------------------------------------------------------------------------
# 10's preamble: which modules `ralphctl` actually is
# --------------------------------------------------------------------------

def _cli_modules() -> set[str]:
    cli_dir = REPO_ROOT / "src" / "ralphd" / "cli"
    return {p.name for p in cli_dir.glob("*.py") if p.name != "__init__.py"}


def _preamble() -> str:
    section = _spec_section("## 10. ralphctl")
    return section.split("### 10.1")[0]


def _module_problems(text: str, modules: set[str]) -> list[str]:
    named = set(re.findall(r"`([a-z_]+\.py)`", text))
    named |= {Path(p).name for p in re.findall(r"`(src/[A-Za-z0-9_/]+\.py)`", text)}
    problems = [f"section 10 names no module {m}" for m in sorted(modules - named)]
    problems += [f"section 10 names {m}, which the cli package does not hold"
                 for m in sorted(named - modules)]
    return problems


def test_the_cli_is_the_modules_the_cli_package_actually_holds():
    modules = _cli_modules()
    assert "main.py" in modules and "ui_server.py" in modules, sorted(modules)
    problems = _module_problems(_preamble(), modules)
    assert not problems, (
        f"SPEC.md section 10's preamble describes `ralphctl`'s implementation, "
        f"so it must name the real modules: {problems}")


def test_the_module_check_would_catch_the_single_module_claim():
    modules = _cli_modules()
    old = ("It is a single module, `src/ralphd/cli/main.py`, on the standard "
           "library alone")
    problems = _module_problems(old, modules)
    assert problems and all("names no module" in p for p in problems), problems
    assert len(problems) == len(modules) - 1  # every module but main.py


# --------------------------------------------------------------------------
# 10.1 / 10.5 / 10.6, empirically: the four detail commands need no container
# --------------------------------------------------------------------------

DETAIL_COMMANDS = [("iteration", ("1",)), ("fault", ()), ("cost", ()),
                   ("docs", ())]


@pytest.fixture
def dead_run(tmp_path):
    """A registry holding one finished run: no container, no host.json."""
    run_id = "spec-surface"
    run = tmp_path / "runs" / run_id
    (run / "iterations" / "0001").mkdir(parents=True)
    (run / "status.json").write_text(json.dumps({
        "runId": run_id, "state": "succeeded", "verdict": "verified",
        "phase": "review", "approach": 2, "maxApproaches": 3,
        "iterationsUsed": 4, "iterationsBudget": 25,
        "startedAt": "2026-08-19T09:14:02Z",
        "endedAt": "2026-08-19T10:26:02Z",
        "usage": {"totalTokens": 4200, "costUSD": 0.5, "costPriced": True,
                  "byPhase": {"worker": {"totalTokens": 4200, "costUSD": 0.5,
                                         "costPriced": True}}},
        "tasks": {"total": 2, "completed": 2},
    }))
    (run / "tasks.json").write_text(json.dumps(
        {"version": 1, "tasks": [{"id": "001", "title": "t", "status": "completed"}]}))
    (run / "notes.md").write_text("handoff notes\n")
    (run / "iterations" / "0001" / "meta.json").write_text(json.dumps({
        "number": 1, "phase": "worker", "startedAt": "2026-08-19T09:14:02Z",
        "endedAt": "2026-08-19T09:20:02Z", "exitCode": 0,
        "usage": {"totalTokens": 4200, "costUSD": 0.5, "costPriced": True},
    }))
    (run / "iterations" / "0001" / "output.jsonl").write_text(json.dumps(
        {"type": "message_end",
         "message": {"content": [{"type": "text", "text": "hello"}]}}) + "\n")
    return tmp_path, run_id


def _ctl(registry: Path, *argv: str):
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    return subprocess.run([str(RALPHCTL), *argv], env=env,
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("verb,extra", DETAIL_COMMANDS)
def test_a_detail_command_answers_from_the_run_dir_alone(dead_run, verb, extra):
    registry, run_id = dead_run
    res = _ctl(registry, verb, run_id, *extra)
    assert res.returncode == 0, res.stderr
    assert run_id in res.stdout
    # No container, no `apiUrl` anywhere -- so a command that needed the API
    # would have said so. These four never mention one (SPEC 10.1).
    assert "unreachable" not in (res.stdout + res.stderr)
    assert "snapshot" not in (res.stdout + res.stderr)


@pytest.mark.parametrize("verb,extra", DETAIL_COMMANDS)
def test_a_detail_command_honours_the_global_json_flag(dead_run, verb, extra):
    registry, run_id = dead_run
    res = _ctl(registry, "--json", verb, run_id, *extra)
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)  # raises if 10.6's claim is false
    assert isinstance(payload, (dict, list)) and payload
