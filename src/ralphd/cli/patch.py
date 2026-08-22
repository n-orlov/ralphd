"""Reading a reflection diff, and saying what it would touch (task 028, req P).

Every run's reflect phase may leave `artifacts/reflection/suggestions.diff`: a
unified diff proposing edits to the loop's own material (`src/ralphd/prompts/`,
skills). Nothing has ever applied it -- SPEC §16's open question -- so the
loop's self-improvement depended entirely on an operator reading a diff by hand
and retyping it. v0.6's diff *was* read by hand, and became eight issues.

This module is the half of the review-and-apply verb that decides **what a diff
says, whether it applies, and exactly which files it would change** -- and that
is all it does until an operator says otherwise:

* `parse_patch(text)` -> `tuple[FilePatch, ...]`, or `PatchError` naming the
  offending line. A malformed proposal is refused *as text*, before any tree is
  touched, because the diff is agent-authored and the failure has to be legible.
* `plan_patch(root, patches)` -> `PatchPlan`: the relative paths that would
  change, the new content computed in memory, and one `HunkFailure` per hunk
  that cannot apply (with its `@@` header and why).
* `apply_plan(root, plan)` writes -- and refuses outright unless every hunk of
  every file applied. **All-or-nothing** (see below).

Nothing here prints, prompts, reads a run directory or runs `git`. The verb
that does those things is `cli/main.py`'s; a pure planner is what lets a test
assert "this diff would touch exactly these two files" without a filesystem
race, and what lets the verb show the operator the plan before writing.

Why not shell out to `git apply` / `patch`
------------------------------------------
Both are absent from plenty of hosts that can perfectly well run `ralphctl`
(the `pipx install ralphd` case has no checkout, let alone a toolchain), and
both report a rejection in their own vocabulary on stderr -- which this verb
would then have to re-parse to say *which* hunk of *which* file failed. The
requirement is that the refusal names the file and the hunk, so the matcher is
here, ~200 lines of it, and the failure objects are structured from the start.
The trade is deliberate: no fuzz, no rename detection, no binary hunks. A diff
this cannot apply is refused, never guessed at.

All-or-nothing, and why partial application is not a flag here
--------------------------------------------------------------
A plan with any failing hunk writes nothing at all. A prompts diff is a set of
edits an agent reasoned about together; applying the four hunks that landed and
leaving the fifth rejected produces a tree neither the agent nor the operator
ever proposed -- and (fact 2) the very next `start` bakes that tree into the job
image by content hash. *Selecting* a subset up front is a different, legitimate
operation (requirement P's partial-application question, task 030): it is a
choice made before the plan is built, not a half-finished write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# The `\ No newline at end of file` marker: not a content line, but the record
# of whether the side it follows ends in a newline. Dropped from the hunk's
# lines and remembered as a flag, so a file that never ended in a newline is
# not silently given one (which would be a real content change, invisible in
# the diff the operator was shown).
NO_NEWLINE_MARKER = "\\ No newline at end of file"

# `--- /dev/null` / `+++ /dev/null`: the file is created / deleted.
DEV_NULL = "/dev/null"

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# What a `FilePatch` does to its path. Spelled out rather than inferred at each
# use site, so a surface can say "creates" without re-deriving it from
# `/dev/null`.
CHANGE_MODIFY = "modify"
CHANGE_CREATE = "create"
CHANGE_DELETE = "delete"

# Why a hunk could not be applied. One constant per reason, because these
# strings are what an operator reads when the verb exits non-zero, and a test
# asserting the reason must not be asserting a re-typed copy of it.
REASON_MISSING = "the file does not exist in the target tree"
REASON_EXISTS = "the file already exists in the target tree"
REASON_NOT_FOUND = "no such context anywhere in the file"
REASON_AMBIGUOUS = "the context appears {count} times, so its position is ambiguous"
REASON_ALREADY_APPLIED = "the change is already present in the file"
REASON_DELETE_MISMATCH = "the file's content is not what the diff removes"


class PatchError(Exception):
    """The diff is not a diff: it could not be parsed at all.

    Distinct from a *rejection* (`HunkFailure`), which is a well-formed diff
    that does not fit this tree. One means the proposal is unreadable, the
    other that it is out of date -- an operator does different things about
    them, so they are never collapsed into one error.
    """


@dataclass(frozen=True)
class Hunk:
    """One `@@` block: where it applies and the two sides of its text.

    `old_lines`/`new_lines` are the content *without* the leading ` `/`-`/`+`
    marker and without line terminators; `lines` keeps the raw markers so the
    hunk can be shown back exactly as proposed.
    """

    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[str, ...]
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]
    # Does the side end without a trailing newline? Only meaningful for a hunk
    # that reaches the end of the file.
    old_no_newline: bool = False
    new_no_newline: bool = False

    @property
    def text(self) -> str:
        """The hunk as it appeared in the diff (header included)."""
        return "\n".join([self.header, *self.lines])


@dataclass(frozen=True)
class FilePatch:
    """Every hunk the diff proposes for one path, plus what it does to it."""

    path: str
    old_path: str
    new_path: str
    change: str
    hunks: tuple[Hunk, ...]

    @property
    def creates(self) -> bool:
        return self.change == CHANGE_CREATE

    @property
    def deletes(self) -> bool:
        return self.change == CHANGE_DELETE


@dataclass(frozen=True)
class HunkFailure:
    """One rejected hunk: the file, the `@@` header, and why.

    This is the object the verb's non-zero exit prints. `header` is empty only
    for a whole-file reason (a missing file, an existing one being created),
    where naming a hunk would be misleading -- nothing about the hunk is wrong.
    """

    path: str
    header: str
    reason: str

    def __str__(self) -> str:
        where = f"hunk {self.header}" if self.header else "the whole file"
        return f"{self.path}: {where} does not apply ({self.reason})"


@dataclass(frozen=True)
class FileChange:
    """What would happen to one file, computed in memory.

    `text` is the complete new content (None for a deletion). `offsets` records
    the line shift each hunk needed to match, so a plan can say a hunk applied
    at a different position than the diff claimed instead of hiding it.
    """

    path: str
    change: str
    text: str | None
    offsets: tuple[int, ...] = ()

    @property
    def relocated(self) -> bool:
        return any(self.offsets)


@dataclass(frozen=True)
class PatchPlan:
    """The whole diff against one tree: what it would do, or why it cannot.

    A plan is inert. It holds the new content of every file it would write and
    has written nothing; `apply_plan` is the only thing in this module that
    touches the tree, and it refuses a plan that is not `ok`.
    """

    root: Path
    changes: tuple[FileChange, ...] = ()
    failures: tuple[HunkFailure, ...] = ()
    patches: tuple[FilePatch, ...] = ()

    @property
    def ok(self) -> bool:
        """Would every hunk of every file apply? (An empty diff plans nothing
        and applies cleanly -- there is nothing wrong with proposing nothing.)"""
        return not self.failures

    @property
    def paths(self) -> tuple[str, ...]:
        """Every relative path this plan would write or remove, in diff order.

        The list an operator is shown *before* anything happens. Includes the
        files of a failing plan too: "what it would have touched" is exactly
        what makes a rejection understandable."""
        return tuple(dict.fromkeys([p.path for p in self.patches]))


# ---------------------------------------------------------------- parsing

def strip_path_prefix(path: str) -> str:
    """`a/src/x.md` -> `src/x.md`; `/dev/null` and bare names pass through.

    Only the conventional single leading component `git diff` writes (`-p1`),
    and only `a/`/`b/`: a diff whose paths carry some other prefix is not
    silently re-rooted, because guessing the strip depth is how a patch tool
    writes a file the operator never saw named.
    """
    text = str(path or "").strip()
    # A `--- a/x.md\t2024-01-01` timestamp column is part of the format.
    text = text.split("\t", 1)[0].strip()
    if text == DEV_NULL:
        return text
    for prefix in ("a/", "b/"):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def _int(value: str | None, default: int) -> int:
    return default if value in (None, "") else int(value)


def _finish_hunk(state: dict, path: str) -> Hunk:
    """Turn the accumulated hunk state into a `Hunk`, checking its counts."""
    old = tuple(state["old"])
    new = tuple(state["new"])
    if len(old) != state["old_count"] or len(new) != state["new_count"]:
        raise PatchError(
            f"{path}: hunk {state['header']} claims "
            f"-{state['old_count']} +{state['new_count']} lines but carries "
            f"-{len(old)} +{len(new)}")
    return Hunk(header=state["header"], old_start=state["old_start"],
                old_count=state["old_count"], new_start=state["new_start"],
                new_count=state["new_count"], lines=tuple(state["raw"]),
                old_lines=old, new_lines=new,
                old_no_newline=state["old_no_newline"],
                new_no_newline=state["new_no_newline"])


def parse_patch(text: str) -> tuple[FilePatch, ...]:
    """Parse a unified diff, or raise `PatchError` naming the offending line.

    Accepts what a reflect phase actually writes: optional `diff --git` and
    mode/index preamble lines, `--- `/`+++ ` pairs, `@@` hunks, and
    `\\ No newline at end of file`. Everything before the first `---` is
    ignored (agents like to narrate), and so is a `diff --git` line's own copy
    of the paths -- the `---`/`+++` pair is the authority, since that is what
    the hunks belong to.
    """
    files: list[FilePatch] = []
    hunks: list[Hunk] = []
    old_path = new_path = ""
    hunk: dict | None = None

    def close_hunk() -> None:
        nonlocal hunk
        if hunk is not None:
            hunks.append(_finish_hunk(hunk, new_path or old_path or "?"))
            hunk = None

    def close_file() -> None:
        nonlocal old_path, new_path, hunks
        close_hunk()
        if old_path or new_path:
            files.append(_build_file_patch(old_path, new_path, tuple(hunks)))
        old_path = new_path = ""
        hunks = []

    for lineno, raw in enumerate(str(text or "").splitlines(), start=1):
        if raw.startswith("--- "):
            close_file()
            old_path = strip_path_prefix(raw[4:])
            continue
        if raw.startswith("+++ "):
            if not old_path:
                raise PatchError(f"line {lineno}: `+++` with no `---` before it")
            close_hunk()
            new_path = strip_path_prefix(raw[4:])
            continue
        if raw.startswith("@@"):
            if not new_path:
                raise PatchError(
                    f"line {lineno}: hunk before any `---`/`+++` file header")
            close_hunk()
            m = _HUNK_RE.match(raw)
            if m is None:
                raise PatchError(f"line {lineno}: unreadable hunk header: {raw}")
            hunk = {"header": raw, "old_start": int(m.group(1)),
                    "old_count": _int(m.group(2), 1),
                    "new_start": int(m.group(3)),
                    "new_count": _int(m.group(4), 1),
                    "old": [], "new": [], "raw": [],
                    "old_no_newline": False, "new_no_newline": False}
            continue
        if hunk is None:
            # Preamble (`diff --git`, `index`, mode lines, narration): ignored
            # on purpose -- see the docstring.
            continue
        if raw.startswith(NO_NEWLINE_MARKER):
            last = hunk["raw"][-1] if hunk["raw"] else " "
            if last.startswith(("-", " ")):
                hunk["old_no_newline"] = True
            if last.startswith(("+", " ")):
                hunk["new_no_newline"] = True
            hunk["raw"].append(raw)
            continue
        body, marker = raw[1:], raw[:1]
        if marker == " " or raw == "":
            # A wholly empty line is a context line whose single space some
            # editors and mail paths strip; treating it as the end of the hunk
            # would reject diffs that are otherwise perfectly applicable.
            hunk["old"].append(body if raw else "")
            hunk["new"].append(body if raw else "")
        elif marker == "-":
            hunk["old"].append(body)
        elif marker == "+":
            hunk["new"].append(body)
        else:
            raise PatchError(
                f"line {lineno}: line in hunk {hunk['header']} starts with "
                f"{marker!r}, not one of ' ', '-', '+'")
        hunk["raw"].append(raw)
    close_file()
    if not files:
        raise PatchError("no file headers (`--- `/`+++ `) in the diff")
    return tuple(files)


def _build_file_patch(old_path: str, new_path: str,
                      hunks: tuple[Hunk, ...]) -> FilePatch:
    if not hunks:
        raise PatchError(f"{new_path or old_path}: file header with no hunks")
    if old_path == DEV_NULL and new_path == DEV_NULL:
        raise PatchError("a file header naming /dev/null on both sides")
    if old_path == DEV_NULL:
        return FilePatch(path=new_path, old_path=old_path, new_path=new_path,
                         change=CHANGE_CREATE, hunks=hunks)
    if new_path == DEV_NULL:
        return FilePatch(path=old_path, old_path=old_path, new_path=new_path,
                         change=CHANGE_DELETE, hunks=hunks)
    if not new_path:
        raise PatchError(f"{old_path}: `---` with no `+++` after it")
    if old_path != new_path:
        # A rename is a real thing a diff can express and this module does not
        # implement it. Refusing beats writing the new path and leaving the old
        # one in place, which is what a naive applier would silently do.
        raise PatchError(
            f"{old_path} -> {new_path}: renames are not supported; apply the "
            "move by hand and re-run the verb on the remaining hunks")
    return FilePatch(path=new_path, old_path=old_path, new_path=new_path,
                     change=CHANGE_MODIFY, hunks=hunks)


# ---------------------------------------------------------------- matching

def _split(text: str) -> tuple[list[str], bool]:
    """File text -> (lines without terminators, ends-with-newline)."""
    if text == "":
        return [], True
    ends = text.endswith("\n")
    return text.split("\n")[:-1] if ends else text.split("\n"), ends


def _join(lines: list[str], trailing_newline: bool) -> str:
    """The inverse of `_split`. An empty file is empty text, never a lone
    newline: a diff that removes every line must not leave a blank line
    behind."""
    if not lines:
        return ""
    return "\n".join(lines) + ("\n" if trailing_newline else "")


def _match_at(lines: list[str], want: tuple[str, ...], at: int) -> bool:
    return at >= 0 and lines[at:at + len(want)] == list(want)


def _find(lines: list[str], want: tuple[str, ...],
          hint: int) -> tuple[int, list[int]]:
    """Where does `want` sit in `lines`? Returns (index, all_matches).

    The diff's own position is tried first, so an unchanged file matches at
    offset 0 and no search happens. Otherwise every occurrence is collected:
    exactly one is a relocation (recorded, applied), several are ambiguous and
    rejected. Never a fuzzy match -- see the module docstring.
    """
    if not want:
        # A pure insertion has no context to find: the position is the diff's.
        return max(0, min(hint, len(lines))), []
    if _match_at(lines, want, hint):
        return hint, [hint]
    found = [i for i in range(0, len(lines) - len(want) + 1)
             if lines[i:i + len(want)] == list(want)]
    return (found[0] if len(found) == 1 else -1), found


def _plan_file(root: Path, patch: FilePatch) -> tuple[FileChange | None,
                                                      list[HunkFailure]]:
    """Compute one file's new content, or the failures that stop it."""
    target = root / patch.path
    exists = target.is_file()
    if patch.creates and exists:
        return None, [HunkFailure(patch.path, "", REASON_EXISTS)]
    if not patch.creates and not exists:
        return None, [HunkFailure(patch.path, "", REASON_MISSING)]
    original = target.read_text() if exists else ""
    lines, trailing = _split(original)

    if patch.deletes:
        wanted = list(patch.hunks[0].old_lines)
        if len(patch.hunks) == 1 and lines == wanted:
            return FileChange(patch.path, CHANGE_DELETE, None), []
        return None, [HunkFailure(patch.path, patch.hunks[0].header,
                                  REASON_DELETE_MISMATCH)]

    failures: list[HunkFailure] = []
    offsets: list[int] = []
    # How far the lines still to come have already moved: every earlier hunk
    # that matched somewhere other than its stated position, plus every line it
    # added or removed. Without this a diff of several hunks would look
    # "relocated" from the second hunk on.
    shift = 0
    for hunk in patch.hunks:
        hint = hunk.old_start - 1 + shift
        at, found = _find(lines, hunk.old_lines, hint)
        if at < 0:
            reason = REASON_NOT_FOUND if not found else \
                REASON_AMBIGUOUS.format(count=len(found))
            if not found and hunk.new_lines and _find(
                    lines, hunk.new_lines, hunk.new_start - 1)[0] >= 0:
                # The tree already contains what the hunk proposes: an operator
                # re-running the verb, or a suggestion somebody typed in by
                # hand. That is a different fact from "this does not fit".
                reason = REASON_ALREADY_APPLIED
            failures.append(HunkFailure(patch.path, hunk.header, reason))
            continue
        offsets.append(at - hint)
        lines[at:at + len(hunk.old_lines)] = list(hunk.new_lines)
        shift += (at - hint) + len(hunk.new_lines) - len(hunk.old_lines)
        if hunk.new_no_newline:
            trailing = False
        elif hunk.old_no_newline:
            trailing = True
    if failures:
        return None, failures
    return FileChange(patch.path, patch.change, _join(lines, trailing),
                      tuple(offsets)), []


def plan_patch(root: Path | str, patches: tuple[FilePatch, ...]) -> PatchPlan:
    """What this diff would do to this tree -- computed, not done.

    Every file is planned even after one fails, so the operator sees the whole
    picture in one refusal instead of peeling failures off one run at a time.
    """
    base = Path(root)
    changes: list[FileChange] = []
    failures: list[HunkFailure] = []
    for patch in patches:
        change, bad = _plan_file(base, patch)
        failures.extend(bad)
        if change is not None:
            changes.append(change)
    return PatchPlan(root=base, changes=tuple(changes),
                     failures=tuple(failures), patches=tuple(patches))


def plan_text(root: Path | str, text: str) -> PatchPlan:
    """`parse_patch` + `plan_patch`: the one call a surface needs to answer
    "what would this diff do here" (and the one that raises `PatchError`)."""
    return plan_patch(root, parse_patch(text))


# ---------------------------------------------------------------- applying

def apply_plan(plan: PatchPlan) -> tuple[str, ...]:
    """Write a clean plan; refuse anything else. Returns the paths written.

    The only function in this module that touches the tree, and it is a
    separate call from `plan_patch` on purpose: the verb shows the plan, gets
    the operator's explicit act, and only then calls this. A plan with any
    failure raises `PatchError` rather than writing its clean half (see the
    module docstring on all-or-nothing).
    """
    if not plan.ok:
        raise PatchError("refusing to write a diff that does not apply: "
                         + "; ".join(str(f) for f in plan.failures))
    written: list[str] = []
    for change in plan.changes:
        target = plan.root / change.path
        if change.change == CHANGE_DELETE:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.text or "")
        written.append(change.path)
    return tuple(written)


def apply_text(root: Path | str, text: str) -> tuple[str, ...]:
    """Parse, plan and (only if every hunk applies) write. Convenience for a
    caller that has already shown the operator a plan of the same text."""
    return apply_plan(plan_text(root, text))


def format_plan(plan: PatchPlan) -> list[str]:
    """The plan as lines, for whichever surface shows it (`ralphctl`, the hub).

    Shaped here rather than in the CLI for the reason `state.py` shapes its
    own summaries: two surfaces must not describe the same plan differently.
    """
    lines: list[str] = []
    for patch in plan.patches:
        change = {p.path: p for p in plan.changes}.get(patch.path)
        verb = {CHANGE_CREATE: "create", CHANGE_DELETE: "delete",
                CHANGE_MODIFY: "modify"}[patch.change]
        note = "" if change is not None else "  <- does not apply"
        hunks = f"{len(patch.hunks)} hunk" + ("s" if len(patch.hunks) != 1 else "")
        relocated = ("  (relocated)" if change is not None and change.relocated
                     else "")
        lines.append(f"{verb:>6}  {patch.path}  ({hunks}){relocated}{note}")
    for failure in plan.failures:
        lines.append(f"reject  {failure}")
    return lines
