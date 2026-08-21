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

import pytest

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


# --------------------------------------------------------------------------
# Task 042 (#22): a documented-but-nonexistent CLI flag or API field must
# fail the suite.
#
# The reference docs are read as a promise, not as prose: every `--flag`
# docs/cli.md shows is checked against the real argparse tree
# (`cli.main.build_parser`, walked including every sub-subparser), and every
# route/field docs/api.md documents is checked against the code that serves
# it. This is deliberately mechanical -- the semantic pass over the same
# files is a human/agent job (task 043) that this check outlives.
#
# Found by writing it, all of them documented and none of them real: the
# global `--registry`/`--quiet` (SPEC.md already said so: there are two global
# flags), `start --prompt-override`, `start --model-<phase>` (a `job.yaml` key,
# no flag), a `--detach` spelling of `--no-detach`, and `ralphctl llm set`
# (mid-run rotation is `PUT /config/llm`, API-only in v0.6).
# --------------------------------------------------------------------------

CLI_DOC = REPO_ROOT / "docs" / "cli.md"
API_DOC = REPO_ROOT / "docs" / "api.md"
ENGINE_API = REPO_ROOT / "src" / "ralphd" / "engine" / "api.py"
HUB_SERVER = REPO_ROOT / "src" / "ralphd" / "cli" / "ui_server.py"

# Programs other than ralphctl whose flags legitimately appear in the docs.
# A code span / command line naming one of these (and not `ralphctl`) is
# somebody else's flag vocabulary -- `docker run --rm --label`, `pi --mode
# json`, `pipx install --force`, `python tools/refresh_bedrock_rates.py
# --check` -- and is not ours to validate.
FOREIGN_PROGRAMS = (
    "docker", "podman", "pi", "pipx", "uvx", "uv", "pip", "python", "python3",
    "pytest", "git", "curl", "xargs", "tar", "ssh", "sudo", "systemctl",
    "ralphd-engine", "tools/refresh_bedrock_rates.py",
)
FLAG_RE = re.compile(r"--[A-Za-z][A-Za-z0-9-]*")
# Flags of other programs that the docs also mention *bare*, with no command
# around them to give them away (`--label ralphd.role=sibling` as a rule the
# agent must follow, `--rm` for short-lived siblings, `pi --mode json`). Only
# flags ralphctl itself does not and should not have belong here.
FOREIGN_FLAGS = {
    "--label", "--rm", "--user", "--filter", "--entrypoint", "--volumes",
    "--mode", "--no-session", "--privileged", "--check",
}
# Prose that *denies* a flag's existence names it without claiming it ("there
# is no global `--registry`") -- the same escape hatch the sibling-cleanup
# checks above give a deliberately-wrong example. Deliberately narrow: only
# these spellings, and only on the flag's own line or the one before it.
NEGATION_MARKERS = (
    "there is no global", "there is no start-time", "is not a flag",
    "never existed", "no longer exists", "there is no `ralphctl",
)
SECTION_RE = re.compile(r"^### `ralphctl(?: ([a-z][a-z-]*))?")
FENCE_RE = re.compile(r"^ *```")


def _cli_parser_flags() -> dict[tuple[str, ...], set[str]]:
    """Every command path in the real parser tree -> its own option strings."""
    import argparse

    from ralphd.cli.main import build_parser

    tree: dict[tuple[str, ...], set[str]] = {}

    def walk(parser, path: tuple[str, ...]) -> None:
        tree[path] = {opt for a in parser._actions for opt in a.option_strings}
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    walk(sub, path + (name,))

    walk(build_parser(), ())
    return tree


def _flags_under(tree: dict[tuple[str, ...], set[str]],
                 path: tuple[str, ...]) -> set[str]:
    """A command's own flags plus its subcommands' plus the global ones."""
    flags = set(tree.get((), set()))
    for known, opts in tree.items():
        if known[:len(path)] == path:
            flags |= opts
    return flags


def _is_foreign(command_text: str) -> bool:
    """True for a command line/span that belongs to another program."""
    if "ralphctl" in command_text:
        return False
    words = re.findall(r"[A-Za-z0-9_./-]+", command_text)
    return any(w in FOREIGN_PROGRAMS for w in words)


def _code_spans(text: str) -> list[tuple[str, int, str | None]]:
    """Every code span in a doc, as (span text, line number, section verb).

    Only code spans count as commands: flags and invocations always appear in
    backticks or in a fenced block in these docs, and prose ("resolved on the
    host by ralphctl at ...") would otherwise be read as one. A fenced block
    is read line by line so a `docker`/`pi` example can be told apart from a
    `ralphctl` one (a wrapped continuation line inherits the program of the
    line that started the command); an inline span is read whole, because the
    docs wrap long commands across source lines.
    """
    lines = text.splitlines()
    spans: list[tuple[str, int, str | None]] = []
    verbs: dict[int, str | None] = {}
    verb: str | None = None
    prose: list[str] = []          # lines outside fenced blocks, fences blanked
    in_fence = False
    fence_program = ""
    for lineno, line in enumerate(lines, start=1):
        if m := SECTION_RE.match(line):
            verb = m.group(1)
        verbs[lineno] = verb
        if FENCE_RE.match(line):
            in_fence = not in_fence
            fence_program = ""
            prose.append("")
            continue
        if in_fence:
            prose.append("")
            stripped = line.strip().lstrip("$").strip()
            head = stripped.split(" ", 1)[0] if stripped else ""
            if head and not head.startswith("-"):
                fence_program = stripped
            command = fence_program if head.startswith("-") else stripped
            if command and not _is_foreign(command):
                spans.append((line.strip(), lineno, verb))
            continue
        prose.append(line)

    # inline spans, across line breaks: odd chunks of a backtick split
    body = "\n".join(prose)
    offset = 0
    for i, chunk in enumerate(body.split("`")):
        if i % 2 == 1:
            lineno = body.count("\n", 0, offset) + 1
            span = re.sub(r"\s+", " ", chunk).strip()
            if span and not _is_foreign(span):
                spans.append((span, lineno, verbs.get(lineno)))
        offset += len(chunk) + 1
    return spans


def _doc_flag_claims(text: str) -> list[tuple[str, str | None, bool, int]]:
    """Extract (flag, section-verb, is_option_table_row, line-no) claims."""
    lines = text.splitlines()
    claims: list[tuple[str, str | None, bool, int]] = []
    for span, lineno, verb in _code_spans(text):
        row = re.match(r"^\| *`(--[A-Za-z][A-Za-z0-9-]*)", lines[lineno - 1])
        for flag in FLAG_RE.findall(span):
            claims.append((flag, verb, bool(row) and flag == row.group(1),
                           lineno))
    return claims


def _denied(lines: list[str], lineno: int) -> bool:
    window = " ".join(lines[max(0, lineno - 2):lineno]).lower()
    return any(m in window for m in NEGATION_MARKERS)


def _flag_problems(text: str) -> list[str]:
    """Documented flags that no command in the real parser tree accepts."""
    tree = _cli_parser_flags()
    everything = _flags_under(tree, ())
    lines = text.splitlines()
    problems = []
    for flag, verb, _row, lineno in _doc_flag_claims(text):
        if flag in FOREIGN_FLAGS or _denied(lines, lineno):
            continue
        if flag not in everything:
            problems.append(f"{lineno}: {flag} (documented under "
                            f"{'ralphctl ' + verb if verb else 'global flags'})")
    return problems


def _flag_attribution_problems(text: str) -> list[str]:
    """Option-table rows claiming a flag the section's own command rejects."""
    tree = _cli_parser_flags()
    problems = []
    for flag, verb, row, lineno in _doc_flag_claims(text):
        if not row or verb is None:
            continue
        if flag not in _flags_under(tree, (verb,)):
            problems.append(f"{lineno}: `ralphctl {verb}` has no {flag}")
    return problems


def test_every_flag_documented_in_cli_md_exists_in_the_parser():
    problems = _flag_problems(CLI_DOC.read_text())
    assert not problems, (
        "docs/cli.md documents flags no ralphctl command accepts "
        f"(docs/cli.md:{'; docs/cli.md:'.join(problems)})")


@pytest.mark.parametrize("relpath", ["README.md", "docs/tutorial.md"])
def test_every_flag_in_the_other_command_teaching_docs_exists(relpath):
    """The reference is not the only place someone copy-pastes a command
    from."""
    problems = _flag_problems((REPO_ROOT / relpath).read_text())
    assert not problems, f"{relpath} documents flags ralphctl rejects: {problems}"


# A subcommand invocation, as the docs write it: `ralphctl llm set <run-id>
# --profile <p>` (which never existed -- rotation is API-only) has to be as
# checkable as a flag. Only verbs that really have sub-actions are checked,
# and only when the doc names further words at all.
INVOCATION_RE = re.compile(r"ralphctl +(?:--json +)?([a-z][a-z-]*)((?: +[^`|\n]+)?)")
WORD_RE = re.compile(r"^[a-z][a-z-]*$")


def _command_problems(text: str) -> list[str]:
    """Documented `ralphctl <verb> <action>` invocations nothing implements."""
    tree = _cli_parser_flags()
    verbs = {path[0] for path in tree if len(path) == 1} | ALIAS_VERBS
    lines = text.splitlines()
    problems = []
    for span, lineno, _verb in _code_spans(text):
        if _denied(lines, lineno):
            continue
        for verb, rest in INVOCATION_RE.findall(span):
            if verb not in verbs:
                problems.append(f"{lineno}: ralphctl {verb}")
                continue
            actions = {path[1] for path in tree
                       if len(path) == 2 and path[0] == verb}
            if not actions:
                continue
            words = [w for w in rest.split() if WORD_RE.match(w)]
            # `ralphctl llm` on its own is a heading, not an invocation; a
            # word list naming none of the real actions is a claim about an
            # action that does not exist (`llm set`).
            if words and not (actions & set(words)):
                problems.append(f"{lineno}: ralphctl {verb} {' '.join(words)}")
    return problems


@pytest.mark.parametrize("relpath", ["docs/cli.md", "docs/api.md",
                                    "docs/llm-profiles.md", "docs/tutorial.md",
                                    "README.md"])
def test_every_documented_subcommand_invocation_exists(relpath):
    problems = _command_problems((REPO_ROOT / relpath).read_text())
    assert not problems, (
        f"{relpath} documents ralphctl invocations nothing implements: "
        f"{problems}")


def test_a_fake_subcommand_in_the_docs_is_reported():
    assert _command_problems("rotate with `ralphctl llm set <run> --profile p`")
    assert not _command_problems("inspect with `ralphctl llm show <profile>`")
    assert not _command_problems("`ralphctl llm` manages profiles")
    assert not _command_problems("`ralphctl artifacts brisk-otter-1408 ls`")
    assert _command_problems("`ralphctl nosuchverb <run-id>`")


def test_every_option_table_row_names_a_flag_of_its_own_command():
    problems = _flag_attribution_problems(CLI_DOC.read_text())
    assert not problems, (
        "an option table row is a promise about that command: "
        f"{'; '.join(problems)}")


def test_the_cli_flag_check_is_substantive():
    """Guards the extractor against silently matching nothing (which would
    make the two checks above vacuous)."""
    claims = _doc_flag_claims(CLI_DOC.read_text())
    flags = {flag for flag, _v, _r, _l in claims}
    rows = {(flag, verb) for flag, verb, row, _l in claims if row}
    verbs = {verb for _f, verb, _r, _l in claims if verb}
    assert len(flags) >= 50, sorted(flags)
    assert len(rows) >= 40, sorted(rows)
    assert len(verbs) >= 15, sorted(verbs)


def test_a_fake_cli_flag_in_the_docs_is_reported():
    """The demonstration, kept: a flag nobody implements must be caught, and
    a real flag documented under the wrong command must be caught too."""
    fake = ("### `ralphctl start`\n\n"
            "| Option | Default | Meaning |\n"
            "|--------|---------|---------|\n"
            "| `--prd <file>` | required | the PRD |\n"
            "| `--no-such-flag <x>` | — | invented |\n")
    assert any("--no-such-flag" in p for p in _flag_problems(fake))
    assert not _flag_problems(fake.replace("--no-such-flag <x>", "--reflect"))
    # real flag, wrong command: caught by the attribution check only
    wrong = fake.replace("--no-such-flag <x>", "--tail <n>")
    assert not _flag_problems(wrong)
    assert any("--tail" in p for p in _flag_attribution_problems(wrong))


def test_another_programs_flags_are_not_read_as_ralphctl_flags():
    """`docker run --rm`/`pi --mode json` in an example is not a claim about
    ralphctl -- but a ralphctl line in the same block still is."""
    foreign = ("### `ralphctl llm`\n\n"
               "```bash\n"
               "docker run --rm --label ralphd.llm-test=x \\\n"
               "  --entrypoint pi <image> -p --mode json --no-session\n"
               "ralphctl llm test host --no-ping\n"
               "```\n"
               "Prose: `docker rm -f --volumes` and `pipx install --force`.\n")
    assert not _flag_problems(foreign)
    assert any("--no-such-flag" in p for p in _flag_problems(
        foreign.replace("--no-ping", "--no-such-flag")))


def test_the_negation_escape_hatch_stays_narrow():
    """Prose may *deny* a flag ("there is no global `--registry`") without
    claiming it -- but only in those words, and only for the flags the docs
    actually deny today. Generic prose that happens to contain a negation is
    still a claim.
    """
    text = CLI_DOC.read_text()
    lines = text.splitlines()
    denied = {flag for flag, _v, _r, lineno in _doc_flag_claims(text)
              if _denied(lines, lineno)}
    assert denied <= {"--registry", "--quiet", "--yes", "--prompt-override"}, denied
    generic = ("with `host` there is no port publishing -- the engine "
               "itself listens on `--port`")
    assert not _denied([generic], 1)
    assert _denied(["There is no global `--no-such-flag`."], 1)


def test_a_denied_flag_is_still_checked_where_it_is_claimed():
    """The escape hatch is per-mention, not per-flag: denying a flag in one
    sentence does not license documenting it as real in the next."""
    doc = ("There is no global `--no-such-flag`.\n\n"
           "### `ralphctl start`\n\n"
           "| Option | Default | Meaning |\n"
           "|--------|---------|---------|\n"
           "| `--no-such-flag` | — | invented |\n")
    assert any("--no-such-flag" in p for p in _flag_problems(doc))


# ---- docs/api.md: routes and response fields --------------------------------

ROUTE_RE = re.compile(r"`((?:GET|POST|PUT|PATCH|DELETE)"
                      r"(?:\s+(?:GET|POST|PUT|PATCH|DELETE))*)"
                      r"\s+(/[A-Za-z0-9_{}<>:/.*-]*)")
JSON_BLOCK_RE = re.compile(r"```json\n(.*?)```", re.DOTALL)
JSON_KEY_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:')
# `tasksSource`, `infraWaitTotalS`, `usage.byPhase` -- a backticked camelCase
# token in the API reference is a field name, not prose.
CAMEL_FIELD_RE = re.compile(r"`([a-z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*)`")
# Field names the code never spells literally, with the reason:
GENERATED_FIELDS = {
    # ui_server._with_local_times() appends "Local" to each of its fields
    "startedAtLocal", "endedAtLocal", "updatedAtLocal",
    # pi's own model-config keys, quoted in docs/llm-profiles.md's shape
    "baseUrl", "apiKey",
}


def _normalize_route(path: str) -> str:
    """`/iterations/{n}` and `/iterations/<n>` are the same route."""
    path = re.sub(r"\{[^}]*\}|<[^>]*>", "*", path)
    return "/" + path.strip("/")


def _engine_routes() -> set[tuple[str, str]]:
    source = ENGINE_API.read_text()
    return {(m.upper(), _normalize_route(p)) for m, p in
            re.findall(r'@app\.(get|post|put|patch|delete)\("([^"]+)"', source)}


def _hub_routes() -> set[str]:
    """The hub dispatches on path segments, so its vocabulary is the set of
    segment literals compared against `segs` in ui_server.Handler."""
    source = HUB_SERVER.read_text()
    leaves = set()
    for line in source.splitlines():
        if "segs" not in line:
            continue
        leaves |= set(re.findall(r'"([a-z][a-z-]*)"', line))
    return leaves


def _documented_routes(text: str) -> set[tuple[str, str]]:
    routes = set()
    flat = re.sub(r"\s+", " ", text)
    for methods, path in ROUTE_RE.findall(flat):
        for method in methods.split():
            routes.add((method, _normalize_route(path)))
    return routes


def _route_problems(text: str) -> list[str]:
    engine = _engine_routes()
    hub = _hub_routes()
    problems = []
    for method, path in sorted(_documented_routes(text)):
        if (method, path) in engine:
            continue
        segs = [s for s in path.split("/") if s]
        if segs[:2] == ["api", "runs"]:
            # hub route: every non-wildcard segment must be one the hub's
            # dispatcher compares against
            unknown = [s for s in segs if s != "*" and s not in hub]
            if not unknown:
                continue
            problems.append(f"{method} {path} (hub knows no {unknown})")
            continue
        problems.append(f"{method} {path}")
    return problems


def _field_claims(text: str) -> set[str]:
    fields = set()
    for block in JSON_BLOCK_RE.findall(text):
        fields |= set(JSON_KEY_RE.findall(block))
    fields |= set(CAMEL_FIELD_RE.findall(text))
    return fields


def _served_field_names() -> str:
    """Everything the shipped code could spell a field name in."""
    parts = []
    for pattern in ("*.py", "*.js"):
        for path in sorted((REPO_ROOT / "src").rglob(pattern)):
            parts.append(path.read_text())
    return "\n".join(parts)


def _field_problems(text: str) -> list[str]:
    source = _served_field_names()
    problems = []
    for field in sorted(_field_claims(text)):
        if field in GENERATED_FIELDS:
            continue
        spellings = (f'"{field}"', f"'{field}'", f"{field}=", f".{field}",
                     f"{field}:")
        if not any(s in source for s in spellings):
            problems.append(field)
    return problems


def test_every_route_documented_in_api_md_is_served():
    problems = _route_problems(API_DOC.read_text())
    assert not problems, (
        f"docs/api.md documents routes nothing serves: {problems}")


def test_every_route_documented_in_cli_md_is_served():
    problems = _route_problems(CLI_DOC.read_text())
    assert not problems, (
        f"docs/cli.md documents routes nothing serves: {problems}")


def test_every_field_documented_in_api_md_exists_in_the_code():
    problems = _field_problems(API_DOC.read_text())
    assert not problems, (
        "docs/api.md documents response fields the code never produces: "
        f"{problems}")


def test_the_api_doc_checks_are_substantive():
    text = API_DOC.read_text()
    routes = _documented_routes(text)
    fields = _field_claims(text)
    assert len(routes) >= 25, sorted(routes)
    assert len(fields) >= 80, len(fields)
    # the extractor must be reading the real serving code, not an empty set
    assert len(_engine_routes()) >= 30
    assert "iterations" in _hub_routes()


def test_a_fake_api_route_or_field_in_the_docs_is_reported():
    """The other half of the demonstration, kept."""
    assert _route_problems("See `GET /no-such-route` for details.")
    assert not _route_problems("See `GET /status` and `GET /iterations/{n}`.")
    assert _route_problems("The hub serves `GET /api/runs/<id>/nonsense`.")
    assert not _route_problems("The hub serves `GET /api/runs/<id>/cost`.")
    fake = '```json\n{"state": "running", "noSuchField": 3}\n```\n'
    assert _field_problems(fake) == ["noSuchField"]
    assert _field_problems(fake.replace("noSuchField", "tasksSource")) == []
    assert _field_problems("prose about `noSuchField` counts too") == [
        "noSuchField"]
