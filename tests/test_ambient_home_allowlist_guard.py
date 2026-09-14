"""Task 007 (v0.12 PRD, #83 class): task 006 left `src/ralphd/ambient_home_
allowlist.py` as a reasoned list of the 11 ambient-`$HOME` reads that
survive under `src/ralphd/`, with a note that "a test that fails when a NEW,
unlisted ambient read appears is task 007's job" -- this module is that
test, plus its mirror image: a `path`/`line` entry the allowlist still
claims but whose file no longer actually contains that read (edited away,
or the entry was simply wrong) is exactly as much of a lie as an unlisted
read, so it fails the suite too.

The three exact shapes it scans for are the ones named in the allowlist
module's own docstring: a bare `Path.home()` call, an `os.environ` read of
the literal `HOME` name, and `os.path.expanduser` called on the bare `"~"`
sentinel -- never a `.expanduser()` call on a user-supplied path (a CLI
flag, a config value), which resolves that STRING's leading `~` rather than
reading this process's ambient home. All three regexes below require the
literal argument shape, not merely the function name, so a call like
`Path(user_dir).expanduser()` never matches (a different string, no bare
`Path.home()`/`~` sentinel in it) -- exactly the exclusion the allowlist
module's docstring asks a "future lexical guard (task 007)" to honour.

Two mutation cases below prove the guard function itself, `allowlist_
violations()`, actually fires in both directions -- entirely with in-
memory reading/allowlist lists (one of them backed by a `tmp_path` scratch
file), never by editing anything under the real `src/ralphd/` tree.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from ralphd.ambient_home_allowlist import AMBIENT_HOME_ALLOWLIST

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "ralphd"

# The three exact shapes named in ambient_home_allowlist.py's own docstring.
# Each requires the literal argument, not just the function name, so a
# `.expanduser()` call on a user-supplied path never matches.
AMBIENT_HOME_RE = re.compile(
    r"Path\.home\(\)"
    r"|os\.environ\[\s*['\"]HOME['\"]\s*\]"
    r"|os\.environ\.get\(\s*['\"]HOME['\"]"
    r"|os\.path\.expanduser\(\s*['\"]~['\"]\s*\)"
)


# The allowlist module is DATA about ambient-home reads, not a reader of one.
# Every entry now carries a `code` field quoting the source line it describes,
# so the module necessarily contains text matching the regexes below -- inside
# string literals, where it means the opposite of a read. Before the `code`
# field existed the same collision was dodged by writing "bare `Path.home`"
# without parentheses in that module's prose, which is a trick that only works
# for prose. The module contains one list literal and no executable statement,
# which is what makes excluding it safe; a read added there would be dead code.
SELF_DESCRIBING = {"ambient_home_allowlist.py"}


def find_ambient_home_reads(root: Path) -> list[tuple[str, int, str]]:
    """Every line under `root` (walked as `*.py`, recursively, except the
    self-describing allowlist module itself) that matches one of the three
    ambient-$HOME read shapes. Returns
    `(relpath, 1-based line number, raw line text)` tuples, `relpath`
    using forward slashes so it lines up with the allowlist's own `path`
    values regardless of host OS.
    """
    out: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        if path.name in SELF_DESCRIBING:
            continue
        rel = path.relative_to(root).as_posix()
        for lineno, text in enumerate(path.read_text().splitlines(), start=1):
            if AMBIENT_HOME_RE.search(text):
                out.append((rel, lineno, text))
    return out


def _norm(code: str) -> str:
    """Whitespace-insensitive form of one source line, so re-indenting a read
    (moving it into or out of a block) is not mistaken for a different read."""
    return " ".join(code.split())


def allowlist_violations(
    readings: list[tuple[str, int, str]],
    allowlist: list[dict[str, str]],
) -> list[str]:
    """Diff an ACTUAL scan (`readings`, as `find_ambient_home_reads()`
    returns) against an allowlist's claims about it.

    Keyed on `(path, normalised code)` and NOT on `(path, line)`. That is
    deliberate and is what `ambient_home_allowlist.py`'s own docstring asked
    for from the start: "line numbers drift with unrelated edits -- a future
    guard test should key off the file, not the line, precisely because of
    this". Keying on the line made this guard fail whenever an unrelated
    commit inserted a line ABOVE a listed read, reporting one unmoved read as
    both a NEW unlisted read and a STALE entry. Between 2839fa2 and 828c8d5
    on 14 Sep 2026 that cost two separate re-anchoring cure commits and left
    `gate` red for nine of eleven commits, none of which had touched an
    ambient-home read at all.

    Counted as a multiset rather than a set, so this is not weaker than the
    line-keyed version in the case that matters: `cli/main.py` legitimately
    contains `aws = Path.home() / ".aws"` twice, and a THIRD copy still has
    to be listed or the counts disagree. What is given up is only the claim
    that a listed read sits at one exact line, which was never a property
    worth asserting -- it is the property that kept breaking.

    Two kinds of mismatch, each reported with the file and the offending
    source text so a failure message can be acted on directly:

    * more occurrences of a read than the allowlist accounts for -- a NEW,
      unlisted ambient-home read (or one more copy of a listed one);
    * an allowlist entry whose code appears fewer times than claimed -- a
      STALE entry: the read was edited away, or the entry was always wrong.

    Every entry's `line` is still carried, and is still worth keeping
    current as documentation, but it is no longer load-bearing: a drifted
    line number cannot fail this test.
    """
    violations: list[str] = []

    # A new entry added without a `code` field has to say so plainly. Adding an
    # ambient read means adding an entry, and an entry that carries only the
    # old `path`/`line`/`reason` keys would otherwise raise KeyError from inside
    # a Counter comprehension -- a confusing red for a simple omission.
    undescribed = [e for e in allowlist if not e.get("code")]
    for e in undescribed:
        violations.append(
            f"allowlist entry for {e['path']}:{e.get('line', '?')} has no "
            f"`code` field: quote the source line the entry describes, since "
            f"this guard matches on the code and not on the line number."
        )

    found = Counter((p, _norm(text)) for p, _, text in readings)
    listed = Counter((e["path"], _norm(e["code"]))
                     for e in allowlist if e.get("code"))
    for key in sorted(found - listed):
        path, code = key
        n = (found - listed)[key]
        where = ", ".join(
            str(ln) for p, ln, txt in readings
            if p == path and _norm(txt) == code
        )
        violations.append(
            f"NEW unlisted ambient-$HOME read in {path} (x{n} unaccounted "
            f"for; occurrences at line(s) {where}): {code!r} -- add a "
            f"reasoned entry to AMBIENT_HOME_ALLOWLIST or remove the read."
        )
    for key in sorted(listed - found):
        path, code = key
        n = (listed - found)[key]
        violations.append(
            f"STALE allowlist entry in {path} (x{n} claimed but not found): "
            f"{code!r} -- no such ambient-$HOME read remains in that file; "
            f"remove or correct the entry."
        )
    return violations


def test_no_unlisted_or_stale_ambient_home_reads():
    """The real check: scan `src/ralphd/` for every ambient-$HOME read and
    require the allowlist to name exactly that set, no more (a new,
    unlisted read) and no less (a stale entry)."""
    readings = find_ambient_home_reads(SRC_ROOT)
    assert readings, "scan found zero ambient-home reads -- regex or SRC_ROOT drifted"
    violations = allowlist_violations(readings, AMBIENT_HOME_ALLOWLIST)
    assert not violations, "\n".join(violations)


def test_mutation_a_planted_unlisted_read_is_caught(tmp_path):
    """Proves the NEW-read direction fires: write a throwaway file (never
    under `src/ralphd/`) containing one ambient-home read that no allowlist
    entry names, scan it with the SAME `find_ambient_home_reads()` the real
    test uses, and check `allowlist_violations()` reports it by exact
    file:line."""
    planted = tmp_path / "engine" / "fake_module.py"
    planted.parent.mkdir(parents=True)
    planted.write_text(
        "def f():\n"
        "    x = 1\n"
        "    home = Path.home()  # planted, deliberately unlisted\n"
    )

    readings = find_ambient_home_reads(tmp_path)
    assert readings == [("engine/fake_module.py", 3, readings[0][2])]

    violations = allowlist_violations(readings, allowlist=[])
    assert len(violations) == 1
    assert "NEW" in violations[0]
    assert "engine/fake_module.py" in violations[0]
    assert "Path.home()" in violations[0]


def test_mutation_b_stale_allowlist_entry_is_caught(tmp_path):
    """Proves the STALE-entry direction fires: write a throwaway file with
    NO ambient-home read at all, but hand `allowlist_violations()` an entry
    claiming one exists at a specific line in it, and check the mismatch is
    reported by that entry's exact file:line."""
    clean = tmp_path / "engine" / "clean_module.py"
    clean.parent.mkdir(parents=True)
    clean.write_text(
        "def f():\n"
        "    return 42  # no ambient-home read anywhere in this file\n"
    )

    readings = find_ambient_home_reads(tmp_path)
    assert readings == []

    stale_allowlist = [{
        "path": "engine/clean_module.py",
        "line": "2",
        "code": "home = Path.home()",
        "reason": "this entry is deliberately wrong for the mutation test",
    }]
    violations = allowlist_violations(readings, stale_allowlist)
    assert len(violations) == 1
    assert "STALE" in violations[0]
    assert "engine/clean_module.py" in violations[0]
    assert "Path.home()" in violations[0]


def test_mutation_c_a_read_that_only_moved_line_is_not_a_violation(tmp_path):
    """The regression this keying exists for. Same file, same single read,
    shifted down by an unrelated insertion above it. The old `(path, line)`
    keying reported that as TWO violations (a NEW read at the new line and a
    STALE entry at the old one); keying on the code text reports none."""
    module = tmp_path / "engine" / "shifting_module.py"
    module.parent.mkdir(parents=True)
    entry = {
        "path": "engine/shifting_module.py",
        "line": "2",
        "code": 'home = Path.home() / ".ralphd"',
        "reason": "listed when the read sat on line 2",
    }

    module.write_text('def f():\n    home = Path.home() / ".ralphd"\n')
    before = find_ambient_home_reads(tmp_path)
    assert [(p, ln) for p, ln, _ in before] == [("engine/shifting_module.py", 2)]
    assert allowlist_violations(before, [entry]) == []

    # An unrelated import lands above it: same read, now on line 4.
    module.write_text(
        "import os\nimport sys\n"
        'def f():\n    home = Path.home() / ".ralphd"\n'
    )
    after = find_ambient_home_reads(tmp_path)
    assert [(p, ln) for p, ln, _ in after] == [("engine/shifting_module.py", 4)]
    assert allowlist_violations(after, [entry]) == []


def test_an_entry_with_no_code_field_is_reported_not_a_keyerror(tmp_path):
    """The omission a future entry is most likely to make. Adding an ambient
    read means adding an entry, and an entry written with only the pre-`code`
    keys must produce a readable violation rather than a KeyError."""
    module = tmp_path / "engine" / "m.py"
    module.parent.mkdir(parents=True)
    module.write_text("def f():\n    return Path.home()\n")

    legacy = [{"path": "engine/m.py", "line": "2", "reason": "no code field"}]
    violations = allowlist_violations(find_ambient_home_reads(tmp_path), legacy)
    assert any("has no `code` field" in v for v in violations), violations
    assert any("NEW unlisted" in v for v in violations), violations


def test_a_third_copy_of_a_twice_listed_read_is_still_caught(tmp_path):
    """Occurrence counting, not set membership: `cli/main.py` really does
    contain `aws = Path.home() / ".aws"` twice, so the multiset has to allow
    exactly two and refuse a third."""
    module = tmp_path / "cli" / "dup_module.py"
    module.parent.mkdir(parents=True)
    code = 'aws = Path.home() / ".aws"'
    twice = [{"path": "cli/dup_module.py", "line": str(n), "code": code,
              "reason": "one of two legitimate copies"} for n in (2, 3)]

    module.write_text(f"def f():\n    {code}\n    {code}\n")
    assert allowlist_violations(find_ambient_home_reads(tmp_path), twice) == []

    module.write_text(f"def f():\n    {code}\n    {code}\n    {code}\n")
    violations = allowlist_violations(find_ambient_home_reads(tmp_path), twice)
    assert len(violations) == 1
    assert "NEW" in violations[0] and "x1 unaccounted" in violations[0]
