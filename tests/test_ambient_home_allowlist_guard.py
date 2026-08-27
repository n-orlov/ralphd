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


def find_ambient_home_reads(root: Path) -> list[tuple[str, int, str]]:
    """Every line under `root` (walked as `*.py`, recursively) that matches
    one of the three ambient-$HOME read shapes. Returns
    `(relpath, 1-based line number, raw line text)` tuples, `relpath`
    using forward slashes so it lines up with the allowlist's own `path`
    values regardless of host OS.
    """
    out: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        for lineno, text in enumerate(path.read_text().splitlines(), start=1):
            if AMBIENT_HOME_RE.search(text):
                out.append((rel, lineno, text))
    return out


def allowlist_violations(
    readings: list[tuple[str, int, str]],
    allowlist: list[dict[str, str]],
) -> list[str]:
    """Diff an ACTUAL scan (`readings`, as `find_ambient_home_reads()`
    returns) against an allowlist's claims about it. Two kinds of mismatch,
    each reported with its offending file:line so a failure message can be
    acted on directly:

    * a reading with no matching `(path, line)` allowlist entry -- a NEW,
      unlisted ambient-home read;
    * an allowlist entry whose `(path, line)` matches no actual reading --
      a STALE entry: the file no longer contains that read there (edited
      away, or the entry was simply wrong).
    """
    reading_keys = {(p, l) for p, l, _ in readings}
    allow_keys = {(e["path"], int(e["line"])) for e in allowlist}

    violations: list[str] = []
    for p, l, text in readings:
        if (p, l) not in allow_keys:
            violations.append(
                f"NEW unlisted ambient-$HOME read at {p}:{l}: {text.strip()!r} "
                f"-- add a reasoned entry to AMBIENT_HOME_ALLOWLIST or remove "
                f"the read."
            )
    for e in allowlist:
        key = (e["path"], int(e["line"]))
        if key not in reading_keys:
            violations.append(
                f"STALE allowlist entry at {e['path']}:{e['line']}: no "
                f"ambient-$HOME read found there anymore -- remove or "
                f"correct the entry."
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
    assert "engine/fake_module.py:3" in violations[0]


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
        "reason": "this entry is deliberately wrong for the mutation test",
    }]
    violations = allowlist_violations(readings, stale_allowlist)
    assert len(violations) == 1
    assert "STALE" in violations[0]
    assert "engine/clean_module.py:2" in violations[0]
