"""ralphd.engine.petitions -- surface OPEN impossibility petitions to
review (task 010, requirement C, #112).

`docs/petition-format.md` (task 009) defines the file: one markdown file
per wave, `artifacts/reports/<wave>-petitions.md`, tracked in the
WORKSPACE (the code tree a job operates on, `LoopSupervisor.workspace` --
never the run state directory, which the worker phase can write to
freely, in a way that would let a run manufacture its own adjudication).
Each petition is its own markdown section carrying at minimum an
`Obligation`, a `Type` and an `Evidence` field, and -- once review has
looked at it -- an `Adjudication` field of `CONFIRMED` or `REFUTED`
(`REFUTED` carries the remedy after the word). A petition with no
`Adjudication` field yet is OPEN: nobody has ruled on it, and this
module's whole job is finding those and handing them to review the same
way `LoopSupervisor._operator_amendment_review_context` hands review an
operator ruling -- a named context block, joined at the SAME seam
(`_review_extra_context`), so a review call site never has to know this
channel exists separately from that one.

This module only PARSES the file(s) and RENDERS the context block. It
does not decide whether a petition is CONFIRMED or REFUTED -- only review
does that, per its own prompt instructions -- and it does not turn a
duplicate/re-filed-REFUTED petition into a finding itself; that
enforcement is a later task's scope (012).

Task 011 (requirement C.3, #112) adds a second job to this module: once
review HAS adjudicated an open petition CONFIRMED (in its own verdict
payload's `petition_adjudications` field), persist that fact at RUN scope
(`RunDir.petition_adjudications_file`, `self.run.root/petition-
adjudications.json` -- deliberately not a workspace path like the
petitions file itself, and deliberately not one of the three files
`LoopSupervisor._archive_approach` copies into `approaches/NN/` on a
rejection) so approach N+1 inherits it automatically, with no explicit
carry-forward step, and so that the obligation's matching findings
become disclosed residuals rather than blocking ones for the rest of the
run (PRD requirement C.4: "a CONFIRMED petition removes its obligation
from the blocking set"). `record_confirmed_adjudications` is the ONLY
writer of that file -- called from `LoopSupervisor` itself once a review
iteration settles, never from inside a worker or review hat's own spawned
process, which is what "review writes them" (the PRD's own words) means
in a world where review's hat process runs at the exact same
dropped-to-agent uid a worker iteration does (`privsep.py`).
Tamper-evidence against the worker rests on three independent things,
because no ONE of them is enough:

1. the record is written through `privsep.write_root_owned_json` -- the
   root-owned write task 105 gave `tasks.json` -- so wherever the uid
   boundary exists (`privsep.separated()`) a hat process cannot write the
   file at all;
2. its authentication tag is a KEYED HMAC-SHA256 over a per-run secret
   that NOTHING under the run directory ever contains -- the key lives in
   the engine PROCESS's own memory for the life of an episode, and (only
   where the uid boundary exists) in a root-owned 0600 file inside a
   root-owned 0700 store OUTSIDE the run dir (`key_store_dir`), because
   the run directory itself is agent-OWNED: a hat process can replace any
   file in it, root-owned or not, so a key kept there is not a secret at
   all. An unkeyed digest stored beside the data it covers (what this
   module shipped first) is recomputable by anyone who can write the
   file, and a keyed MAC whose key sits in the same agent-owned tree is
   recomputable the same way -- both were defeated by exactly that probe;
3. every write announces its tag twice: in the engine's own in-memory
   record of the last MAC it wrote, and in an `events.jsonl` witness line
   (`ADJUDICATIONS_RECORDED_EVENT`). `read_confirmed_adjudications`
   cross-checks both, so rewriting the (agent-writable) event log to
   announce a forged MAC does not help either -- and any detection is
   itself recorded (`ADJUDICATIONS_TAMPERED_EVENT`), sticky in memory AND
   in the log for the rest of the run.

The threat model is narrow and worth stating: the property claimed is
that nothing outside `record_confirmed_adjudications` can ADD or KEEP an
exemption -- against a hat process, which is a separate PROCESS from the
engine (`runner.py` spawns the agent CLI), so it can write every file in
the run dir but cannot read the engine's address space. Where
`$RALPHD_RUN_SECRETS_DIR` points the key store somewhere the agent uid
can read (an operator's choice, and never the default), the property
holds for the episode that wrote the record -- the engine's in-memory
`last_mac` still catches a re-MAC -- but not for a LATER episode reading
that store's key; put the store where only root can read it, which is
what the `privsep.separated()` default does. Destroying the record (deleting it, replacing the key store,
truncating the event log) is always possible for whoever owns the run
directory, and it only ever REMOVES exemptions -- the safe direction.
Any mismatch, and any record that cannot be authenticated at all, means
the record is treated as EMPTY (no obligation is exempted) and -- for a
mismatch -- review is told, in its own prompt, to raise a BLOCKING
finding: fail closed, never towards trusting a record that may have been
edited to manufacture an exemption.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

# The five petition types `docs/petition-format.md` enumerates (task 004,
# requirement A.1, #114 added `self-referential` to the original four). A
# section whose `Type` field is not one of these is not treated as a
# petition at all -- this is what lets the file's own title/intro heading
# (which names none of them) pass through unnoticed.
PETITION_TYPES = ("environmental", "temporal", "contradictory-clauses",
                  "external", "self-referential")

_HEADING_RE = re.compile(r"^#{1,6}[ \t]+\S.*$", re.MULTILINE)
_FIELD_RE = re.compile(
    r"^[ \t]*(?:[-*][ \t]+)?\*\*(Obligation|Type|Evidence|Adjudication)\*\*"
    r"[ \t]*:?[ \t]*(.*)$",
    re.IGNORECASE | re.MULTILINE)


def _sections(text: str) -> list[str]:
    """Split `text` into per-heading chunks: a heading line through (but
    not including) the next heading, or end of text. The preamble before
    the first heading (the file's own title, explanatory prose) has no
    heading of its own and is dropped here -- a petition is always its
    own section, never front matter."""
    matches = list(_HEADING_RE.finditer(text))
    sections = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append(text[start:end])
    return sections


def parse_petitions(text: str) -> list[dict]:
    """Every petition section in `text` (one `<wave>-petitions.md` file's
    content), each rendered as a dict with `obligation`, `type`,
    `evidence`, `adjudication` (`None`, `"CONFIRMED"` or `"REFUTED"`) and
    `remedy` (the text stated after a `REFUTED` adjudication's marker, ''
    otherwise, and always '' for `CONFIRMED`/open). A section counts as a
    petition only if it declares a recognised `Type` -- this is what
    tells a petition section apart from the file's own title/intro
    heading, which names none of `PETITION_TYPES`.
    """
    petitions = []
    for section in _sections(text):
        fields: dict[str, str] = {}
        for m in _FIELD_RE.finditer(section):
            name = m.group(1).lower()
            fields.setdefault(name, m.group(2).strip())
        ptype = fields.get("type", "").strip().lower()
        if ptype not in PETITION_TYPES:
            continue
        heading_match = _HEADING_RE.match(section)
        heading_text = (heading_match.group(0).lstrip("#").strip()
                         if heading_match else "")
        obligation = fields.get("obligation", "").strip() or heading_text
        adjudication_raw = fields.get("adjudication", "").strip()
        adjudication = None
        remedy = ""
        if adjudication_raw:
            upper = adjudication_raw.upper()
            if upper.startswith("CONFIRMED"):
                adjudication = "CONFIRMED"
            elif upper.startswith("REFUTED"):
                adjudication = "REFUTED"
                remedy = adjudication_raw[len("REFUTED"):].lstrip(" :-\u2014").strip()
        petitions.append({
            "obligation": obligation,
            "type": ptype,
            "evidence": fields.get("evidence", "").strip(),
            "adjudication": adjudication,
            "remedy": remedy,
        })
    return petitions


def open_petitions(text: str) -> list[dict]:
    """The subset of `parse_petitions(text)` nobody has adjudicated yet --
    the ones review must actually rule on this pass."""
    return [p for p in parse_petitions(text) if p["adjudication"] is None]


def discover_petition_files(workspace: Path) -> list[Path]:
    """Every `<wave>-petitions.md` file tracked under the workspace's
    `artifacts/reports/` -- `docs/petition-format.md`'s own naming
    convention, deliberately not tied to one hardcoded wave name so a run
    spanning more than one wave still surfaces every file. Sorted for a
    deterministic render order."""
    reports_dir = workspace / "artifacts" / "reports"
    if not reports_dir.is_dir():
        return []
    return sorted(reports_dir.glob("*-petitions.md"))


# ---------------------------------------------------------------------------
# Task 065 (requirement N.6, #150): a run-scope home for OPEN petition
# filings, so the ENGINE never has to write one into the workspace tree at
# all. `#150` stays CLOSED -- this is not its glob-discovery bug fixed a
# second time, it is the tree-side file that bug's glob needs, removed for
# a run built on this code (the running engine's own petition file, from
# an OLDER build, is untouched -- see `docs/petition-format.md` and the
# module docstring above). Every reader/writer below that grows a `run`
# parameter defaults it to `None`, so every pre-existing call site (which
# knows nothing of a run handle) is byte-for-byte unaffected.
# ---------------------------------------------------------------------------

def _run_petitions_file(run) -> Path | None:
    """`run.petitions_file` when `run` is not None and exposes one, else
    None -- the one guard every run-dir-aware function below shares."""
    if run is None:
        return None
    return getattr(run, "petitions_file", None)


def _read_run_petitions_text(run) -> str:
    """The run-dir petitions file's own text, or `''` when there is none
    or it cannot be read yet -- the same fail-soft posture `filed_
    petition_for_obligation` already takes for a workspace file race."""
    path = _run_petitions_file(run)
    if path is None:
        return ""
    try:
        return path.read_text()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Task 011 (requirement C.3, #114): does the run's CONTRACT carry a petition
# protocol at all?
# ---------------------------------------------------------------------------

# A heading or a list item that names petitions -- the shape an opt-in
# actually takes in a contract (`## C -- exhaustion is a petition, not a
# failure`; `- Petitions: file one per obligation ...`). Restricted to those
# two line shapes, plus the explicit phrases below, for the same reason
# `findings.extract_materiality_rubric` matches a HEADING rather than a
# keyword: a contract that merely mentions the noun in passing ("v0.15 filed
# no petition") has not adopted a protocol, and requirement C.3's default is
# the pre-wave `failed` routing whenever the contract is silent.
_PETITION_HEADING_RE = re.compile(
    r"^[ \t]*#{1,6}[ \t]+.*\bpetitions?\b.*$", re.IGNORECASE | re.MULTILINE)
_PETITION_LIST_ITEM_RE = re.compile(
    r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+.*\bpetitions?\b.*$",
    re.IGNORECASE | re.MULTILINE)

# Phrases that declare the protocol wherever they appear, heading or prose:
# each one describes the MECHANISM (a filing, a status, a format document),
# not merely the noun.
PETITION_PROTOCOL_PHRASES = (
    "petition protocol",
    "petition-format",
    "petitioned",
    "impossibility petition",
    "impossibility declaration",
    "file a petition",
    "files a petition",
    "petition is filed",
    "petitions are filed",
)

# The explicit, quotable statement of the default this module falls back to
# when a contract declares no protocol -- the exact string
# `LoopSupervisor._verify_task` records as the reason exhaustion took the
# pre-wave route, reused verbatim by the pinning tests so the two never
# drift apart.
NO_PETITION_PROTOCOL_NOTICE = (
    "no petition protocol: the run's contract declares none, so exhaustion "
    "keeps the pre-wave 'failed' routing (requirement C.3's default-safe "
    "fallback)"
)

# Name of the file `ensure_petitions_file` creates when a contract DOES
# declare the protocol but the workspace carries no `<wave>-petitions.md`
# yet: the wave token taken from the contract's own text when it names one
# (`v0.16` -> `v0.16-petitions.md`), else a neutral `run-petitions.md`.
_WAVE_RE = re.compile(r"\bv(\d+\.\d+)(?:\.\d+)?\b")
DEFAULT_PETITIONS_WAVE = "run"


def contract_declares_petition_protocol(contract_text: str) -> bool:
    """Whether `contract_text` (the run's PRD/composite-PRD text -- "the
    contract", the same text `findings.extract_materiality_rubric` reads)
    carries a petition protocol.

    This is the ONE discriminant requirement C.3 puts between the two
    exhaustion routes (`LoopSupervisor._verify_task`): a contract that
    declares a protocol routes an exhausted task to `petitioned`; a contract
    that declares none keeps today's `failed` +
    `TASK_FAILURE_VALIDATION_EXHAUSTED` routing, exactly as before this
    wave. Deliberately NOT keyed on whether the workspace happens to carry
    an `artifacts/reports/*-petitions.md` file: a repo can carry a historical
    petitions file from an EARLIER wave (this repo does) while the contract
    the current run works from says nothing about petitions at all, and that
    run must keep the old routing -- the workspace's file inventory is not
    its contract.

    Detection is by-presence, the same default-safe pattern requirement A.3
    (#110) uses for the materiality rubric: nothing to set, nothing to
    remember, and silence means the pre-wave behaviour.
    """
    text = contract_text or ""
    lowered = text.lower()
    if any(phrase in lowered for phrase in PETITION_PROTOCOL_PHRASES):
        return True
    return bool(_PETITION_HEADING_RE.search(text)
                or _PETITION_LIST_ITEM_RE.search(text))


def _current_wave_name(contract_text: str = "") -> str:
    """The wave token this run's CONTRACT names (`v0.16` -> `"v0.16"`),
    else `DEFAULT_PETITIONS_WAVE` -- the one derivation `ensure_petitions_
    file`, `open_petitions_in_workspace` and `file_exhaustion_petition`
    now all share, so "the current wave" means the same file to a reader
    and a writer by construction rather than by convention.
    """
    match = _WAVE_RE.search(contract_text or "")
    return f"v{match.group(1)}" if match else DEFAULT_PETITIONS_WAVE


def _current_wave_petitions_path(
        workspace: Path, contract_text: str = "") -> tuple[Path, Path | None]:
    """The path THIS wave's petitions file would live at, and the existing
    on-disk file (if any) that counts as that wave's file today.

    A repo commonly carries more than one `<wave>-petitions.md` under
    `artifacts/reports/` -- an earlier wave's filing, tracked history, sits
    right beside the current one (#150: this run's own two mid-run
    adjudications re-adjudicated 14 foreign petitions filed under v0.16-
    v0.18 for exactly this reason). The match is:

    1. an EXACT filename match (`<current-wave>-petitions.md`) if one is
       discovered -- the unambiguous case;
    2. else, when the contract names NO wave at all (`_current_wave_name`
       fell back to `DEFAULT_PETITIONS_WAVE`) and exactly ONE
       `<wave>-petitions.md` file exists, THAT file -- the
       wave-less-caller case (the shape almost every pre-existing fixture
       in this test suite takes: a lone file whose name never matches the
       token an absent/minimal contract computes), where there is no wave
       to disagree with and so nothing to misfile ACROSS;
    3. else -- no existing file counts as the current wave's; a repo
       carrying only OLDER waves' files must never resurrect an OLD one as
       if it were this run's own (the `files[-1]` bug this task fixes).
       This holds for ONE older file exactly as it holds for several: a
       v0.19 contract beside a lone `v0.18-petitions.md` gets a NEW
       `v0.19-petitions.md` from `ensure_petitions_file` and reads nothing
       out of v0.18, because a contract that names its own wave has
       already said which file is current.
    """
    wave = _current_wave_name(contract_text)
    default_path = workspace / "artifacts" / "reports" / f"{wave}-petitions.md"
    files = discover_petition_files(workspace)
    for f in files:
        if f.name == default_path.name:
            return default_path, f
    if wave == DEFAULT_PETITIONS_WAVE and len(files) == 1:
        return default_path, files[0]
    return default_path, None


def ensure_petitions_file(workspace: Path, contract_text: str = "") -> Path | None:
    """The CURRENT wave's petitions file (`_current_wave_petitions_path`),
    creating an empty one when the workspace has none yet -- so that a
    contract which DOES declare the protocol never falls back to `failed`
    merely because no wave file had been created yet. Without this, the
    workspace's file inventory would remain a second, hidden discriminant
    beside the contract, which is exactly what requirement C.3 forbids
    ("the two branches are distinguished by the contract and nothing
    else").

    Called only where the contract has already been found to declare a
    protocol (`contract_declares_petition_protocol`); returns the existing
    current-wave file when there is one, or None when the file cannot be
    created at all (a read-only workspace), in which case the caller keeps
    the `failed` fallback.

    Task 018 (requirement E.1, #150): this used to return `files[-1]`
    whenever ANY petitions file existed, which resurrected the newest OLDER
    wave's file as this run's filing target the moment a repo carried more
    than one wave's history. `_current_wave_petitions_path` is the fix --
    an exact wave-name match, else (only when the contract names no wave at
    all) the lone-file fallback that keeps every pre-existing wave-less
    caller unaffected, else nothing, so this branch creates a genuinely NEW
    file for the current wave instead -- including when the workspace's ONE
    existing file belongs to an older wave.
    """
    default_path, existing = _current_wave_petitions_path(workspace, contract_text)
    if existing is not None:
        return existing
    wave = _current_wave_name(contract_text)
    try:
        default_path.parent.mkdir(parents=True, exist_ok=True)
        default_path.write_text(
            f"# {wave} impossibility petitions\n\n"
            "Created by the engine because this run's contract declares a "
            "petition protocol (see `docs/petition-format.md`). One section "
            "per petition; no petition filed yet.\n")
    except OSError:
        return None
    return default_path


_TASK_OBLIGATION_REF_RE = re.compile(
    r"\btask\s+(\S+?)(?:['\u2019]s)?\b[:\s]", re.IGNORECASE)
_REQUIREMENT_OBLIGATION_REF_RE = re.compile(
    r"\brequirement\s+([A-Za-z](?:\.\d+)*)\b", re.IGNORECASE)


# The shortest normalised obligation text that may anchor itself to a task
# by QUOTING that task's own criterion clause (anchor 3 in
# `_obligation_is_history`): short enough that a real clause qualifies, long
# enough that an incidental word or two shared with some task's criteria
# never does.
_MIN_CRITERION_CLAUSE_ANCHOR = 24


def _obligation_is_history(obligation: str, task_ids: set[str],
                            contract_text: str,
                            task_criteria: tuple[str, ...] = ()) -> bool:
    """Task 018 (requirement E.1, #150): true when `obligation` names
    neither a task that exists in the CURRENT `tasks.json` (`task_ids`) nor
    a requirement the CURRENT contract (`contract_text`) actually declares
    -- the shape a foreign wave's filing takes once its own task ids and
    requirement letters no longer resolve against this run's own plan or
    PRD (the two mid-run adjudications this task's PRD requirement cites,
    #150). A petition this function calls history is excluded from
    `open_petitions_in_workspace`'s result -- it is not re-typed or
    deleted, only left out of what review is asked to adjudicate.

    The rule is an ANCHOR test, not a token test: an obligation is current
    only while it still names something this run actually carries. It is
    history unless AT LEAST ONE of the following resolves --

    1. a task reference (`Task <id>` -- `task_petition_obligation`'s own
       format, the shape an ENGINE-filed exhaustion petition always takes)
       whose id is in `task_ids`; or
    2. a `requirement <letter>` reference the contract actually declares --
       a literal `"requirement <letter>"` phrase, or the letter's own PRD
       heading (`## <letter> --`, this PRD's own section shape); or
    3. a verbatim criterion CLAUSE of a task that is in the current plan
       (`task_criteria`, each task's `successCriteria`/`criteriaOfRecord`
       normalised) -- the third obligation form the worker contract itself
       names ("the requirement letter, DoD item, or your own task's
       criterion clause this obligation names"). Quoting a live task's own
       clause names that task as surely as writing its id does, so this is
       still the same anchor test rather than an exception to it. Bounded on
       purpose: at least `_MIN_CRITERION_CLAUSE_ANCHOR` normalised
       characters must match, so a couple of words a foreign wave's
       obligation happens to share with some current criterion is never an
       anchor.

    Everything else is history, INCLUDING an obligation that names neither
    kind of anchor at all (free text such as `the criterion can only be
    verified after it is met`): the criterion of record for this task says
    "a petition whose obligation names no task in the current tasks.json
    and no requirement of the contract" is history, and free text names
    neither. An obligation with NO anchor cannot be shown to belong to this
    run's own plan or PRD, which is exactly the property that separates a
    foreign wave's leftover filing from this wave's own (#150: the two
    mid-run adjudications that re-adjudicated 14 foreign petitions filed
    under v0.16-v0.18).

    One narrow benefit of the doubt, and only one: when `contract_text` is
    EMPTY there is no contract in hand to resolve a `requirement <letter>`
    reference against, so such a reference is left OPEN -- unresolvABLE, not
    unresolved. An obligation that names no anchor at all is history whether
    a contract was supplied or not; an anchor nobody can check is not the
    same fact as a missing anchor.

    Note the asymmetry that remains: this filter is applied ONLY when a
    caller hands `open_petitions_in_workspace` the current `tasks` list
    (`tasks=None`, every pre-existing caller, never filters at all), so the
    strict direction here can never hide a petition from a caller that did
    not ask to have the current plan taken into account. A petition called
    history is neither re-typed nor deleted -- only left out of what review
    is asked to adjudicate.
    """
    task_refs = {m.rstrip(":") for m in _TASK_OBLIGATION_REF_RE.findall(obligation)}
    if any(ref in task_ids for ref in task_refs):
        return False
    clause = _normalize_obligation(obligation)
    if len(clause) >= _MIN_CRITERION_CLAUSE_ANCHOR and any(
            clause in criteria for criteria in task_criteria):
        return False
    requirement_refs = _REQUIREMENT_OBLIGATION_REF_RE.findall(obligation)
    if requirement_refs and not contract_text:
        return False
    contract_lower = (contract_text or "").lower()
    for ref in requirement_refs:
        if re.search(rf"\brequirement\s+{re.escape(ref)}\b", contract_lower,
                     re.IGNORECASE):
            return False
        letter = ref[0]
        if re.search(rf"^#{{1,6}}[ \t]*{re.escape(letter)}\b", contract_text,
                     re.MULTILINE):
            return False
    return True


def open_petitions_in_workspace(
        workspace: Path, contract_text: str = "",
        tasks: list[dict] | None = None, run=None) -> list[dict]:
    """Every OPEN petition filed under `workspace`'s CURRENT wave's
    petitions file, PLUS (task 065, requirement N.6, #150) every OPEN
    petition filed into `run`'s own `petitions_file` -- the input
    `petitions_review_context` renders.

    Task 018 (requirement E.1, #150): this used to read EVERY
    `<wave>-petitions.md` file discovered under the workspace, which
    surfaced an earlier wave's still-unadjudicated petitions to every later
    wave's review too (the mid-run adjudication episode re-adjudicated 14
    foreign petitions from v0.16-v0.18 alongside this run's own, #150).
    Scoped to `_current_wave_petitions_path`'s single current-wave file
    (the same derivation `ensure_petitions_file` uses, so a reader and a
    writer agree on "the current wave" by construction). `contract_text`
    defaults to `""` (no wave named -> `DEFAULT_PETITIONS_WAVE`), which is
    also the only case a lone existing file is still accepted as current,
    so every pre-existing wave-less caller behaves exactly as before while
    a contract that DOES name a wave never reads an older wave's file --
    not even when that older file is the only one on disk.

    `run` (optional, default `None`, backward compatible with every
    pre-existing caller): when given, `run.petitions_file`'s own text (if
    any) is parsed the identical way and its open petitions are folded
    into the result -- this is the counterpart read to `file_worker_
    petition`/`file_exhaustion_petition`'s run-dir write path, so a
    petition this run's OWN engine filed (never into the workspace tree
    at all, per N.6) is still surfaced to review. `run=None` keeps this
    function reading the workspace's tracked file only, exactly as before
    this task.

    `tasks` (optional, default `None`, backward compatible with every
    pre-existing caller) is the CURRENT run's `tasks.json` tasks list; when
    given, a petition whose obligation names no task in it and no
    requirement of `contract_text` is also excluded as history
    (`_obligation_is_history`) -- even from within the current wave's own
    file, e.g. a leftover filing for a task the plan no longer carries at
    all, or one whose obligation anchors to nothing in this run at all.
    `tasks=None` (every pre-existing caller) applies no history filter
    whatsoever.

    A file that cannot be read (permissions, a race with a concurrent
    write) costs nothing more than itself, the same posture
    `_apply_operator_status_rulings` takes for a broken amendments mount:
    skipped, not raised."""
    _default_path, current_file = _current_wave_petitions_path(
        workspace, contract_text)
    found: list[dict] = []
    if current_file is not None:
        try:
            found.extend(open_petitions(current_file.read_text()))
        except OSError:
            pass
    run_text = _read_run_petitions_text(run)
    if run_text:
        found.extend(open_petitions(run_text))
    if tasks and found:
        task_ids = {str(t.get("id")) for t in tasks
                    if isinstance(t, dict) and t.get("id") is not None}
        task_criteria = tuple(
            _normalize_obligation(str(t.get(field) or ""))
            for t in tasks if isinstance(t, dict)
            for field in ("successCriteria", "criteriaOfRecord")
            if t.get(field))
        found = [p for p in found
                if not _obligation_is_history(
                    p["obligation"], task_ids, contract_text, task_criteria)]
    return found


def format_petition_section(obligation: str, ptype: str, evidence: str) -> str:
    """Render one petition section in `docs/petition-format.md`'s exact
    field shape -- a heading naming the obligation, then bolded
    `**Obligation**`/`**Type**`/`**Evidence**` fields, one per line, the
    same syntax `_FIELD_RE`/`parse_petitions` above already read back.
    This is the engine's own WRITER for a format this module has, until
    task 004 (requirement A.1, #114), only ever parsed.
    """
    heading = " ".join(obligation.split()).strip() or "Untitled obligation"
    return (
        f"\n## Petition: {heading}\n\n"
        f"**Obligation**: {obligation.strip()}\n"
        f"**Type**: {ptype.strip()}\n"
        f"**Evidence**: {evidence.strip()}\n"
    )


def filed_petition_for_obligation(workspace: Path, obligation: str,
                                   run=None) -> dict | None:
    """The petition already on file (in ANY wave's file under the
    workspace, PLUS `run`'s own run-dir petitions file when `run` is
    given -- task 065, requirement N.6, #150) whose obligation normalises
    to the same key as `obligation`, or None.

    Workspace-wide on purpose, and separate from "which file does a NEW
    filing land in": a re-filed obligation is a violation no matter which
    wave first filed it (`docs/petition-format.md`'s one-petition-per-
    obligation rule), while the write target is the CURRENT wave's file
    alone (`_current_wave_petitions_path`) -- or, once a caller passes
    `run`, the run dir's own file exclusively, never the workspace at
    all. Extracted (task 018, requirement E.1) so `file_exhaustion_
    petition` can ask the duplicate question BEFORE it asks `ensure_
    petitions_file` to create anything -- a refused filing must leave no
    new current-wave file behind. `run=None` (every pre-existing caller)
    scans the workspace only, exactly as before this task.
    """
    key = _normalize_obligation(obligation)
    for path in discover_petition_files(workspace):
        try:
            text = path.read_text()
        except OSError:
            continue
        for p in parse_petitions(text):
            if _normalize_obligation(p["obligation"]) == key:
                return p
    run_text = _read_run_petitions_text(run)
    if run_text:
        for p in parse_petitions(run_text):
            if _normalize_obligation(p["obligation"]) == key:
                return p
    return None


def duplicate_petition_error(existing: dict) -> str:
    """The one refusal string both writers return for an obligation that
    already carries a filed petition -- worded once, here, so the worker's
    own declaration path and the exhaustion path never drift apart."""
    return (
        "petition: an obligation already has a filed "
        f"petition ({existing['obligation']!r}) -- a duplicate "
        "declaration for the same obligation is refused, not "
        "filed (docs/petition-format.md's one-petition-per-"
        "obligation rule)"
    )


def file_worker_petition(workspace: Path, obligation: str, ptype: str,
                          evidence: str, *,
                          target: Path | None = None, run=None) -> list[str]:
    """The ENGINE's own write path for a worker's structured impossibility
    declaration (task 004, requirement A.1, #114) -- never the worker
    itself: a worker's final-line payload only ever carries the three
    plain strings a caller already ran through
    `transitions.worker_petition_report_errors` (the schema/semantic
    checks); this is the one place that turns them into a tracked filing,
    in exactly `docs/petition-format.md`'s field shape, or refuses instead
    of filing when the named obligation already has one -- the same
    one-petition-per-obligation rule `duplicate_petition_obligations`
    enforces POST-HOC for a filing that reached the file some other way;
    this is the point where an ordinary declaration never reaches that
    state at all.

    `run` (optional, default `None`, task 065, requirement N.6, #150):
    when given, the filing is written into `run.petitions_file` --
    RUN-scope, never the workspace tree -- and `target`/`discover_
    petition_files`'s own workspace-file selection is not consulted at
    all for the WRITE (the duplicate scan below still checks the
    workspace too, via `filed_petition_for_obligation(..., run=run)`, so
    a re-filed obligation is refused no matter which of the two sources
    -- workspace or run dir -- first carries it). This is the engine's
    own production write path from `LoopSupervisor._apply_worker_
    petition_declaration`/`petitions.file_exhaustion_petition`, which
    both now pass their own run handle -- #150 stays CLOSED: this is not
    its glob-discovery bug fixed again, it is the tree-side file that bug
    reads removed, for a run built on this code. `run=None` (every
    pre-existing caller) keeps writing into the workspace exactly as
    before this task.

    `target` (optional, default `None`, backward compatible with every
    pre-existing caller, ignored when `run` is given) is the exact
    workspace file to write into, when the caller has already resolved
    one -- `file_exhaustion_petition` (below) passes `ensure_petitions_
    file`'s own current-wave file when it has no run handle, so
    exhaustion never falls into this function's own `files[-1]` fallback
    (task 018, requirement E.1, #150). `target=None` keeps choosing among
    whatever `discover_petition_files` finds, exactly as before this task
    -- the worker's own typed declaration (task 004) has no wave to
    resolve from, so it is unaffected.

    The duplicate-obligation scan below is always workspace-AND-run-dir-
    wide (every discovered workspace file, every wave, plus `run`'s own
    file when given) regardless of `target`: a re-filed obligation is a
    violation no matter which wave -- or which of the two storage
    locations -- first filed it, and that is a separate concern from
    which file a NEW filing lands in.

    Returns a list of error strings; empty means the petition was written.
    A non-empty return means nothing was written to disk -- the caller (an
    `extra_validate` hook, the same shape every other worker semantic
    check in this tree already uses) folds these into the ordinary
    contract re-ask ladder, so a refused declaration reaches the SAME
    worker again rather than disappearing silently.
    """
    already = filed_petition_for_obligation(workspace, obligation, run=run)
    if already is not None:
        return [duplicate_petition_error(already)]
    if run is not None:
        run_target = _run_petitions_file(run)
        if run_target is None:
            return [
                "petition: this run exposes no run-dir petitions file to "
                "file this declaration into"
            ]
        write_target = run_target
        write_target.parent.mkdir(parents=True, exist_ok=True)
    elif target is not None:
        write_target = target
    else:
        files = discover_petition_files(workspace)
        if not files:
            return [
                "petition: no <wave>-petitions.md file exists under "
                "artifacts/reports/ to file this declaration into"
            ]
        write_target = files[0] if len(files) == 1 else files[-1]
    try:
        existing = write_target.read_text()
    except OSError:
        existing = ""
    section = format_petition_section(obligation, ptype, evidence)
    if existing and not existing.endswith("\n"):
        section = "\n" + section
    with write_target.open("a", encoding="utf-8") as f:
        f.write(section)
    return []


# Task 009 (requirement C.1, #114): the engine's own default classification
# for a petition it files itself, on EXHAUSTION, never on a worker's own
# typed claim (that is `file_worker_petition`'s `ptype` argument, supplied
# by the worker's own declaration, task 004). A spent validation ladder is,
# structurally, closest to `self-referential`'s own definition -- "a
# criterion that can only be verified using a state the criterion's own
# satisfaction would have to create" -- which is exactly what three failed
# attempts against the same criteria text demonstrate, without the engine
# ever having to guess at `environmental`/`temporal`/`external` facts it has
# no way to check on its own. A later adjudicator (mid-run or approach-end
# review) may retype or REFUTE this filing exactly like any other.
PETITION_TYPE_EXHAUSTION_DEFAULT = "self-referential"


def task_petition_obligation(task: dict) -> str:
    """The obligation string an engine-filed petition for `task` names --
    stable across every attempt on the same task, and the SAME text both
    `file_exhaustion_petition` (below) files under and
    `apply_refuted_petition_adjudications`/`_outstanding_for_completion`
    (task 010, requirement C.2, #114) later match a review adjudication
    against. Extracted so all three sites agree on the shape by
    construction rather than by convention.
    """
    tid = task.get("id", "?")
    title = (task.get("title") or "").strip()
    return f"Task {tid}: {title}" if title else f"Task {tid}"


def file_exhaustion_petition(workspace: Path, task: dict,
                              contract_text: str = "", run=None) -> list[str]:
    """File a petition on a task's behalf the moment its validation ladder
    is exhausted (`state.VALIDATION_ATTEMPT_LIMIT`), carrying its
    last-attempt handoff -- the text already written into
    `task['validationNotes']` by the verifier's own rejection -- as the
    petition's evidence (PRD v0.16 requirement C.1, #114: "v0.15 approach
    4's task 001 handoff was a complete, well-argued petition that had
    nowhere to go").

    A SECOND writer, beside the worker's own typed declaration (`file_
    worker_petition`, task 004): the worker may never have declared
    impossibility explicitly (it can simply keep trying and losing to
    review), so by the time the ladder is spent there may be no filed
    petition at all for this obligation -- this is what makes sure
    exhaustion itself always produces one. Delegates entirely to
    `file_worker_petition` for the write/duplicate-refusal logic (the same
    one-petition-per-obligation rule applies to either writer): a normalized
    obligation already on file, from either writer, is refused rather than
    re-filed, and the caller
    (`LoopSupervisor._verify_task`) folds a refusal into the ordinary
    `failed` routing rather than losing the exhaustion silently.

    The obligation is the task's own id + title (stable across the three
    attempts that share it, and normalized before comparison, so a
    worker's own earlier declaration against the SAME task -- filed with
    the same id+title text -- is treated as the one filing this obligation
    already has, not a fresh one).

    `run` (optional, default `None`, task 065, requirement N.6, #150):
    when given, forwarded straight to `filed_petition_for_obligation`/
    `file_worker_petition`, which then write into `run.petitions_file` --
    RUN-scope -- and `ensure_petitions_file`/`target` (the workspace-file
    resolution below) are never consulted at all: exhaustion's filing
    never touches the workspace tree when the caller has a run handle.
    `LoopSupervisor._verify_task` is the one production caller and always
    has one. `run=None` (every pre-existing caller, e.g. a fixture
    exercising this function directly) keeps the workspace-file behaviour
    below byte-for-byte unchanged.

    Task 018 (requirement E.1, #150): with no `run`, `target` is resolved
    through `ensure_petitions_file(workspace, contract_text)` -- the
    CURRENT wave's file, creating it if absent -- and handed to `file_
    worker_petition` explicitly, so exhaustion never lands in `file_
    worker_petition`'s own `files[-1]` fallback (an older wave's file,
    the moment the workspace carries more than one, or the moment the
    single one it carries is an older wave's). `contract_text` defaults
    to `""`, matching every pre-existing caller (no wave named ->
    `_current_wave_petitions_path`'s wave-less lone-file fallback). A
    workspace where the current-wave file cannot even be created (a
    read-only mount) refuses the filing instead of silently misfiling it
    -- the caller folds that into the ordinary `failed` route exactly
    like any other refusal from this function, and a duplicate obligation
    is refused BEFORE anything is created, so a refused filing leaves no
    empty current-wave file behind.
    """
    tid = task.get("id", "?")
    obligation = task_petition_obligation(task)
    evidence = (task.get("validationNotes") or "").strip() or (
        f"Task {tid} exhausted its validation ladder with no verifier "
        "reason recorded on the last attempt.")
    already = filed_petition_for_obligation(workspace, obligation, run=run)
    if already is not None:
        # Refused before `ensure_petitions_file` runs, so a duplicate never
        # leaves a freshly created (and permanently empty) current-wave
        # file behind (task 018, requirement E.1).
        return [duplicate_petition_error(already)]
    if run is not None:
        return file_worker_petition(
            workspace, obligation, PETITION_TYPE_EXHAUSTION_DEFAULT,
            evidence, run=run)
    target = ensure_petitions_file(workspace, contract_text)
    if target is None:
        return [
            "petition: the contract declares a petition protocol but no "
            "current-wave petitions file could be created under "
            "artifacts/reports/"
        ]
    return file_worker_petition(workspace, obligation,
                                PETITION_TYPE_EXHAUSTION_DEFAULT, evidence,
                                target=target)


def _adjudication_line(adjudication: str, remedy: str = "") -> str:
    """The exact `**Adjudication**: ...` line `write_back_petition_
    adjudication` appends -- the SAME field shape `parse_petitions`'s
    `_FIELD_RE` already reads back (task 019, requirement E.2, #150).
    `CONFIRMED` carries no remedy (`docs/petition-format.md`: a CONFIRMED
    adjudication needs no remedy text, the obligation is simply void);
    `REFUTED` always states one, even when review left it blank -- an
    empty remedy is rendered as the same "no remedy stated" text
    `apply_refuted_petition_adjudications` already writes into a task's own
    `notes` for the identical case, not a line the file silently omits.
    """
    if adjudication == "REFUTED":
        text = remedy.strip() or "Petition REFUTED with no remedy stated."
        return f"**Adjudication**: REFUTED \u2014 {text}"
    return f"**Adjudication**: {adjudication}"


def _append_adjudication_to_section(text: str, obligation_key: str,
                                     line: str) -> str | None:
    """Task 019 (requirement E.2, #150): insert `line` into the ONE
    section of `text` whose own `Obligation` field (or heading, the same
    fallback `parse_petitions` uses) normalises to `obligation_key` --
    never at the end of the whole file, since a file commonly carries more
    than one petition and the obligation being adjudicated need not be the
    last one filed. Mirrors `_sections`/`parse_petitions`'s own field
    parsing exactly, rather than re-deriving it, so a section this module
    parses as a valid petition is always the same section this function
    can find and edit.

    Returns the whole file's NEW text with the line appended just inside
    the matching section (after its own last field, before the next
    heading), or `None` when no section matches OR the matching section
    already carries an `Adjudication` field -- an adjudication already
    written to the tracked file is never overwritten or duplicated by a
    second call (idempotent by construction, not by a caller's own
    bookkeeping).
    """
    matches = list(_HEADING_RE.finditer(text))
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section = text[start:end]
        fields: dict[str, str] = {}
        for fm in _FIELD_RE.finditer(section):
            fields.setdefault(fm.group(1).lower(), fm.group(2).strip())
        ptype = fields.get("type", "").strip().lower()
        if ptype not in PETITION_TYPES:
            continue
        heading_text = m.group(0).lstrip("#").strip()
        section_obligation = fields.get("obligation", "").strip() or heading_text
        if _normalize_obligation(section_obligation) != obligation_key:
            continue
        if fields.get("adjudication"):
            return None
        stripped = section.rstrip("\n")
        trailing = section[len(stripped):]
        new_section = stripped + "\n" + line + "\n" + trailing
        return text[:start] + new_section + text[end:]
    return None


def write_back_petition_adjudication(workspace: Path, obligation: str,
                                      adjudication: str,
                                      remedy: str = "", *,
                                      run=None) -> Path | None:
    """PRD requirement E.2 (task 019, #150): when review's own
    verdict (or the mid-run adjudication episode's) CONFIRMS or REFUTES a
    petition, write that call back into the section it was filed in --
    "the engine appends `**Adjudication**: CONFIRMED|REFUTED --
    <remedy>` to the petition's own section" -- so a reader of that
    petition's own filing sees the outcome without having to
    cross-reference `events.jsonl` or the run-scope adjudications store.

    `run` (optional, default `None`, task 065, requirement N.6, #150):
    when given, ONLY `run.petitions_file` is searched/edited -- the
    workspace tree is never touched, matching `file_worker_petition`'s
    own run-dir write path -- because an obligation this engine build
    ever files now lands there, never in a tracked `<wave>-petitions.md`
    file (N.6: "the engine stops writing petitions into the tree"). An
    obligation whose only filing is a WORKSPACE file (a pre-committed
    `v0.21-petitions.md`, or one an OLDER engine build wrote during this
    same run) is a deliberate no-op here when `run` is given: the run-
    scope JSON store (`record_confirmed_adjudications`/`record_refuted_
    adjudications`) already carries that adjudication authoritatively,
    and this function's whole job is a human-readable MIRROR, never the
    record of truth -- so `None` from a run-scoped call names nothing
    broken. `run=None` (every pre-existing caller) keeps searching every
    `<wave>-petitions.md` file `discover_petition_files` finds under
    `workspace` exactly as before this task.

    Searches for the ONE section whose obligation normalises the same
    way, and appends the adjudication line inside that section
    (`_append_adjudication_to_section`). Returns the `Path` written on
    success, `None` when nothing matched, the section already carried an
    `Adjudication` field (this call is then a deliberate no-op, never a
    silent overwrite), or the file could not be re-written (a read-only
    mount).

    `adjudication` values other than `"CONFIRMED"`/`"REFUTED"` are also a
    no-op -- this function is a filter over an already-validated payload
    field exactly the way `record_confirmed_adjudications`'s own docstring
    describes itself, never a second schema validator.
    """
    if adjudication not in ("CONFIRMED", "REFUTED"):
        return None
    if not isinstance(obligation, str) or not obligation.strip():
        return None
    key = _normalize_obligation(obligation)
    line = _adjudication_line(adjudication, remedy)
    if run is not None:
        run_path = _run_petitions_file(run)
        if run_path is None:
            return None
        try:
            run_text = run_path.read_text()
        except OSError:
            return None
        updated = _append_adjudication_to_section(run_text, key, line)
        if updated is None:
            return None
        try:
            run_path.write_text(updated)
        except OSError:
            return None
        return run_path
    for path in discover_petition_files(workspace):
        try:
            text = path.read_text()
        except OSError:
            continue
        updated = _append_adjudication_to_section(text, key, line)
        if updated is None:
            continue
        try:
            path.write_text(updated)
        except OSError:
            return None
        return path
    return None


def duplicate_petition_obligations(workspace: Path, run=None) -> list[dict]:
    """Every normalised obligation with MORE THAN ONE filed petition section
    across every `<wave>-petitions.md` file under `workspace` --
    `docs/petition-format.md`'s own rule, stated once and covering both
    named cases at once: "Filing a second petition against an obligation
    that already has one -- regardless of whether the first was
    adjudicated yet -- is a duplicate" AND "[t]he same rule covers
    re-filing an obligation whose petition was already REFUTED". Both are
    the identical structural fact -- more than one petition section for
    the same obligation -- so ONE scan finds both; a caller only needs to
    look at whether any filing in the group is `REFUTED` to tell which
    sentence of the docs it is (see `duplicate_petition_findings` below).

    This is purely structural (counting sections, comparing normalised
    obligation text) -- no review judgement call is needed to know it is
    true, which is exactly why the engine can detect and enforce it
    directly (task 012, requirement C.1, #112) rather than leaving it to
    a run's own prose describing what it filed.

    `run` (optional, default `None`, task 065, requirement N.6, #150):
    when given, `run.petitions_file`'s own filed petitions are folded
    into the same scan -- a duplicate spanning the workspace's tracked
    file and the run dir's own file (e.g. an OLDER engine build's
    workspace filing re-filed by THIS build into the run dir) is caught
    exactly like one confined to either source alone. `run=None` (every
    pre-existing caller) scans the workspace only.

    Returns one dict per violated obligation: `{"obligation": <the first
    filing's own obligation text>, "filings": [every petition dict for it,
    filing order]}`. Empty when every obligation across every file has at
    most one filing -- the overwhelming common case.
    """
    grouped: dict[str, list[dict]] = {}
    first_text: dict[str, str] = {}
    for path in discover_petition_files(workspace):
        try:
            text = path.read_text()
        except OSError:
            continue
        for p in parse_petitions(text):
            key = _normalize_obligation(p["obligation"])
            grouped.setdefault(key, []).append(p)
            first_text.setdefault(key, p["obligation"])
    run_text = _read_run_petitions_text(run)
    if run_text:
        for p in parse_petitions(run_text):
            key = _normalize_obligation(p["obligation"])
            grouped.setdefault(key, []).append(p)
            first_text.setdefault(key, p["obligation"])
    return [
        {"obligation": first_text[key], "filings": filings}
        for key, filings in grouped.items()
        if len(filings) > 1
    ]


def duplicate_petition_findings(workspace: Path, run=None) -> list[dict]:
    """One `severity: "blocking"` finding, shaped exactly like
    `engine.findings.FINDING_SCHEMA` (`severity`/`curable`/`clause`/
    `evidence`), per obligation `duplicate_petition_obligations` flags.
    This is the engine's own deterministic report of the violation --
    `LoopSupervisor._review_findings_channel` appends this list to
    whatever findings review's own sidecar carries, so a duplicate or a
    re-filed-REFUTED petition is a blocking finding on the engine/prompt
    surface unconditionally, never dependent on review's own prose having
    mentioned it (task 012, requirement C.1, #112 -- the docs/petition-
    format.md rule enforced, not merely restated).

    `curable` is always `True`: a fresh worker attempt that simply removes
    the extra filing (or never files a second one) changes this finding's
    truth value on the next pass -- unlike the incurable facts requirement
    B is about, this is a self-inflicted, fully fixable procedural error.

    Task 031 (PRD requirement E.2, #171) adds `cured_when`: every curable
    blocking finding needs one now, engine-authored findings included --
    this one names the fix in the same terms `evidence` already does
    (leave exactly one filing for the obligation on disk).

    `run` (optional, default `None`, task 065, requirement N.6, #150):
    passed straight through to `duplicate_petition_obligations` so a
    duplicate spanning the run dir's own petitions file is caught too.
    """
    out = []
    for dup in duplicate_petition_obligations(workspace, run=run):
        n = len(dup["filings"])
        refuted = any(f["adjudication"] == "REFUTED" for f in dup["filings"])
        if refuted:
            evidence = (
                f"{n} petitions filed for the same obligation, and an "
                "earlier filing was already adjudicated REFUTED -- "
                "docs/petition-format.md: re-filing a REFUTED petition "
                "against the same obligation is a duplicate, a blocking "
                "finding, not a second hearing.")
        else:
            evidence = (
                f"{n} petitions filed for the same obligation -- "
                "docs/petition-format.md: a petition may be filed once "
                "per obligation; filing a second one is a duplicate, a "
                "blocking finding.")
        out.append({
            "severity": "blocking",
            "curable": True,
            "clause": dup["obligation"],
            "evidence": evidence,
            "cured_when": (
                "exactly one petition filing remains on disk for this "
                "obligation, with no re-filed REFUTED duplicate beside it"),
        })
    return out


def duplicate_petitions_review_context(workspace: Path, run=None) -> str:
    """The review-prompt context block naming every duplicate/re-filed
    petition the engine has already detected and turned into a blocking
    finding (`duplicate_petition_findings`) -- the prompt half of the
    engine/prompt surface task 012 puts this detection on. Tells review
    the finding is already recorded engine-side (so review does not need
    to -- and must not -- treat any of these obligations as newly
    eligible for a fresh CONFIRMED/REFUTED call) rather than leaving
    review to notice the duplicate on its own. Returns '' when none exist
    (the common case).

    `run` (optional, default `None`, task 065, requirement N.6, #150):
    passed straight through to `duplicate_petition_obligations`.
    """
    dupes = duplicate_petition_obligations(workspace, run=run)
    if not dupes:
        return ""
    lines = [
        "\n## Duplicate or re-filed petitions detected (task 012, "
        "requirement C.1, #112)\n",
        ("The engine has already detected the following obligation(s) with "
         "more than one filed petition under this workspace's "
         "`artifacts/reports/*-petitions.md` files, and has already "
         "recorded each as a BLOCKING finding in this run's findings "
         "channel per `docs/petition-format.md` (a petition may be filed "
         "once per obligation; re-filing one already adjudicated REFUTED "
         "is the same violation). Do not adjudicate any of these "
         "obligations as newly CONFIRMED/REFUTED -- the duplicate filing "
         "itself is the finding.\n"),
    ]
    for dup in dupes:
        lines.append(
            f"\n- **Obligation**: {dup['obligation']} "
            f"({len(dup['filings'])} filings)\n")
    return "".join(lines)


def _obligation_confirmed(obligation: str, confirmed: set[str]) -> bool:
    """Whether `obligation` (a petition's own free-text `Obligation`
    field) matches an already-run-scope-CONFIRMED obligation, using the
    same normalised substring-either-direction rule
    `downgrade_confirmed_findings` applies to a finding's `clause` -- the
    two channels (a petition's `Obligation`, a finding's `clause`) rarely
    agree on exact wording, so an exact-string comparison would silently
    fail to exclude the very obligation `record_confirmed_adjudications`
    just recorded."""
    clause = _normalize_obligation(obligation)
    return bool(clause) and any(clause in c or c in clause for c in confirmed)


def open_unconfirmed_petitions(workspace: Path, run=None, *,
                                contract_text: str = "",
                                tasks: list[dict] | None = None) -> list[dict]:
    """Every OPEN petition under `workspace` (`open_petitions_in_workspace`)
    MINUS whatever a run-scope CONFIRMED adjudication (`confirmed_
    obligations`) already excludes -- the exact filter `petitions_review_
    context` below applies before rendering its prompt block, extracted
    here so a SECOND caller can reuse it without re-deriving the rule
    (fact 16/#65): `LoopSupervisor._maybe_run_midrun_adjudication_episode`
    (task 005, requirement A.2, #114) scopes its bounded episode to
    exactly this list -- the same 'genuinely still open' set review's own
    prompt already restricts itself to, so the episode never re-asks
    review to adjudicate an obligation already CONFIRMED at run scope.

    `run=None` (every caller that existed before this task) returns
    exactly `open_petitions_in_workspace(workspace)`, unfiltered and
    byte-for-byte unchanged.

    `contract_text`/`tasks` (task 018, requirement E.1, #150) are passed
    straight through to `open_petitions_in_workspace` -- see its own
    docstring for what each scopes. Both default to the same
    backward-compatible values (`""`/`None`) that function itself
    defaults to.

    Task 065 (requirement N.6, #150): `run` is now ALSO passed through to
    `open_petitions_in_workspace`, so a petition this run's own engine
    filed into `run.petitions_file` (never the workspace tree) is
    surfaced here too, before the CONFIRMED filter below runs. `run=None`
    still means "no run-dir file to read", so the byte-for-byte claim
    above holds.
    """
    petitions_list = open_petitions_in_workspace(
        workspace, contract_text=contract_text, tasks=tasks, run=run)
    if run is not None and petitions_list:
        confirmed, tampered = confirmed_obligations(run)
        if not tampered and confirmed:
            petitions_list = [
                p for p in petitions_list
                if not _obligation_confirmed(p["obligation"], confirmed)
            ]
    return petitions_list


def petitions_review_context(workspace: Path, run=None, *,
                              contract_text: str = "",
                              tasks: list[dict] | None = None) -> str:
    """Render the review-prompt context block for every OPEN petition
    found under `workspace` -- the same treatment
    `LoopSupervisor._operator_amendment_review_context` gives operator
    rulings: a named heading, instructions on what review must do with
    each item, then one entry per petition. Returns '' when nothing is
    open (the common case -- most waves file no petitions at all), so
    review prompts are unaffected until a petition actually exists.

    `run` (optional, default `None`) is the run-scope handle whose
    `confirmed_obligations(run)` record this call consults before
    rendering: an obligation already CONFIRMED at run scope (PRD
    requirement C.4 -- 'a CONFIRMED petition removes its obligation from
    the blocking set' for the REST OF THE RUN, not just the review pass
    that confirmed it) is no longer genuinely OPEN, so re-issuing a 'MUST
    adjudicate' instruction naming it on every later review pass would
    either waste a rehearing on an already-settled fact or invite review
    to flip a CONFIRMED call back open -- neither of which requirement
    C.4 permits. Passing `run=None` (every pre-existing caller) keeps
    this function's behaviour byte-for-byte unchanged. A tampered record
    (`confirmed_obligations` returning `tampered=True`) is treated as an
    EMPTY confirmed set: fail towards surfacing the obligation again for
    a fresh adjudication, never towards trusting a record that might
    have been edited to manufacture a false exemption.

    `contract_text`/`tasks` (task 018, requirement E.1, #150), passed
    through to `open_unconfirmed_petitions`/`open_petitions_in_workspace`,
    default the same backward-compatible way.
    """
    petitions = open_unconfirmed_petitions(
        workspace, run, contract_text=contract_text, tasks=tasks)
    if not petitions:
        return ""
    lines = [
        "\n## Open impossibility petitions (task 010, requirement C, #112)\n",
        ("The following impossibility petition(s) (see "
         "`docs/petition-format.md`) are OPEN -- nobody has adjudicated "
         "them yet. For EACH one listed below, your verdict MUST "
         "adjudicate it either `CONFIRMED` (the obligation genuinely "
         "cannot be satisfied as written; it becomes a disclosed residual "
         "for the rest of the run, and every later approach inherits the "
         "adjudication) or `REFUTED` (state CONCRETELY what would satisfy "
         "the obligation -- a vague or missing remedy is not a valid "
         "REFUTED adjudication). Carry your adjudications in your verdict "
         "payload's `petition_adjudications` field, one entry per "
         "petition, each naming the obligation and your CONFIRMED/REFUTED "
         "call (and, for REFUTED, the remedy). A duplicate filing against "
         "an obligation that already has one, or a re-filed petition whose "
         "earlier filing was REFUTED, is itself a BLOCKING finding, not a "
         "second hearing.\n"),
    ]
    for p in petitions:
        lines.append(
            f"\n- **Obligation**: {p['obligation']}\n"
            f"  **Type**: {p['type']}\n"
            f"  **Evidence**: {p['evidence']}\n")
    return "".join(lines)


# ---------------------------------------------------------------------------
# Task 011 (requirement C.3, #112): run-scope persistence of CONFIRMED
# adjudications, tamper-evident against the worker.
# ---------------------------------------------------------------------------

# The `events.jsonl` event types this layer writes. Together with the
# root-owned record write (`privsep.write_root_owned_json`) and the
# engine-memory-only MAC key below, these are the independent artifacts a
# forged adjudication would have to line up.
ADJUDICATIONS_RECORDED_EVENT = "petition_adjudications_recorded"
ADJUDICATIONS_TAMPERED_EVENT = "petition_adjudications_tampered"
ADJUDICATIONS_UNAUTHENTICATED_EVENT = "petition_adjudications_unauthenticated"

# Task 007 (requirement B, #114): the REFUTED counterpart's event type.
# Unlike CONFIRMED, a REFUTED adjudication is never persisted at run scope
# as an exemption (`docs/petition-format.md`: "is not something a later
# approach inherits") -- there is nothing here for a later approach to
# read back and no MAC to authenticate, because nothing downstream ever
# TRUSTS this event the way `confirmed_obligations` is trusted to grant an
# exemption. It exists purely so "what would satisfy the obligation" is
# not lost the moment the adjudicating iteration ends, for the common case
# (task 007) where the REFUTED entry names an obligation with no matching
# `petitioned` task at all -- a worker's own mid-run declaration
# (`file_worker_petition`, task 005 PART 1) never moves the task off its
# ordinary status, so `apply_refuted_petition_adjudications`'s tasks.json
# mutation (scoped to `petitioned` tasks only, task 010/C.2) has nothing to
# touch and the remedy would otherwise vanish unrecorded.
PETITION_REFUTED_EVENT = "petition_refuted"

# Where a per-run MAC key MAY be persisted so that a record written by an
# earlier engine episode still authenticates after a restart: a directory
# OUTSIDE the (agent-owned) run dir, root-owned mode 0700, holding
# root-owned mode 0600 key files. `$RALPHD_RUN_SECRETS_DIR` overrides the
# default for an operator who wants the store on a root-only tmpfs.
RUN_SECRETS_DIR_ENV = "RALPHD_RUN_SECRETS_DIR"
DEFAULT_RUN_SECRETS_DIR = Path("/var/lib/ralphd/run-secrets")

def _normalize_obligation(obligation: str) -> str:
    """Case/whitespace-insensitive key for matching an obligation string
    against itself across the two independent free-text channels
    (a petition's own `Obligation` field, a finding's `clause`) that both
    name the same PRD requirement/DoD item in slightly different words."""
    return " ".join(obligation.split()).strip().lower()


def _canonical(entries: list[dict]) -> str:
    """Canonical JSON over `entries` (sorted keys, no incidental
    whitespace) so two semantically identical entries lists always hash
    the same regardless of dict ordering."""
    return json.dumps(entries, sort_keys=True, separators=(",", ":"))


def _adjudications_digest(entries: list[dict]) -> str:
    """An UNKEYED digest over `entries`, kept as a human/debug-readable
    field of the record. It is deliberately NOT the tamper check on its
    own: an unkeyed digest stored in the same file as the data it covers
    is recomputable by anyone who can write the file, so a worker-phase
    edit that also recomputes it would go unnoticed. The two checks that
    actually carry the property are `_adjudications_mac` (keyed, over a
    per-run secret the record does not contain) and the `events.jsonl`
    witness (`ADJUDICATIONS_RECORDED_EVENT`) -- see
    `read_confirmed_adjudications`."""
    return hashlib.sha256(_canonical(entries).encode("utf-8")).hexdigest()


@dataclass
class _Authority:
    """The engine process's own authority over one run's adjudication
    record: the MAC key, the MAC of the last write the engine itself made
    (an in-memory witness no other process can reach, let alone rewrite),
    and whether tampering has been detected in this episode (sticky)."""

    key: bytes
    last_mac: str | None = None
    tampered: bool = False


# Per-run authorities, held ONLY in this process's memory, keyed by run
# root. This is the primary home of the MAC key: a worker iteration is a
# separate process, so even at the identical uid (the documented
# single-uid degrade, which is also every test in this repo) it cannot
# read the key out of the engine's address space -- and there is no file
# under the agent-OWNED run dir for it to read the key out of either,
# which is precisely what attempt 2 got wrong.
_AUTHORITIES: dict[str, _Authority] = {}


def _authority_id(run) -> str:
    return str(Path(run.root))


def key_store_dir() -> Path | None:
    """The directory a per-run MAC key MAY additionally be persisted in so
    that a record written by an earlier engine EPISODE still authenticates
    after a restart -- or None when there is none and the key is
    memory-only for the episode.

    Never inside the run dir: the run directory is agent-owned, which is
    what makes every file in it replaceable at the agent uid whether the
    file itself is root-owned or not (the reason attempt 2's
    `run.root/petition-adjudications.key` was not a secret).
    `$RALPHD_RUN_SECRETS_DIR` names the store explicitly (an operator
    putting it on a root-only tmpfs -- a store the agent uid can read is
    the operator's own trade-off, see the module docstring); otherwise it exists exactly when the
    uid boundary does (`privsep.separated()`), where
    `/var/lib/ralphd/run-secrets` is created root-owned 0700 -- neither
    readable nor replaceable at the agent uid, because its PARENT is
    root-owned too. With no boundary and no override there is NO on-disk
    key at all: a later episode then cannot authenticate an earlier
    episode's record and treats it as empty (fail closed), which is the
    documented degrade.
    """
    override = os.environ.get(RUN_SECRETS_DIR_ENV)
    if override:
        return Path(override)
    from ralphd.engine import privsep
    if privsep.separated():
        return DEFAULT_RUN_SECRETS_DIR
    return None


def _stored_key_path(run) -> Path | None:
    """This run's key file inside `key_store_dir()`, named by a digest of
    the run root (the store names no run paths), or None when there is no
    store."""
    store = key_store_dir()
    if store is None:
        return None
    return store / stored_key_filename(_authority_id(run))


def stored_key_filename(run_root: str) -> str:
    """The file name `_stored_key_path` gives the key of a run whose root
    is `run_root` -- a digest of the root path, so the store itself names
    no run paths.

    Public because a HOST-side surface has to look for exactly this file
    without importing the private path builder: `ralphctl status` reports
    whether the run's key is actually THERE (task 020's rejection: the
    directory merely existing is not the same condition), and the host
    knows the run root only as the container path the run dir is bound at.
    """
    ident = hashlib.sha256(str(Path(run_root)).encode("utf-8")).hexdigest()
    return f"{ident[:32]}.key"


def _decode_key_material(material: bytes) -> bytes:
    """Decode what `_persist_key` wrote. The store holds the key HEX
    encoded (64 ASCII hex digits for the 32 raw bytes) precisely so that
    reading it can be whitespace-tolerant without ever being
    whitespace-DESTRUCTIVE: a raw random 32-byte key whose first or last
    byte happens to be an ASCII whitespace byte (`\\n`, `\\t`, ` `, `\\r`,
    `\\x0b`, `\\x0c` -- about a 1-in-22 chance per minted key) came back
    from `.strip()` short, so the later episode rebuilt a DIFFERENT key,
    the keyed MAC mismatched and the run's own CONFIRMED record was read
    as tampered/empty (#112, requirement C.4: the exclusion must hold for
    the rest of the run, not for 95% of runs). Bytes that are not a pure
    even-length ASCII hex string are returned unchanged, so a key file
    written by an earlier (raw-bytes) engine build still authenticates."""
    try:
        text = material.decode("ascii")
    except UnicodeDecodeError:
        return material
    if len(text) < 2 or len(text) % 2 or any(
            c not in "0123456789abcdefABCDEF" for c in text):
        return material
    try:
        return bytes.fromhex(text)
    except ValueError:  # pragma: no cover - guarded by the check above
        return material


def _read_key_material(path: Path) -> bytes | None:
    """The stripped bytes of `path`, or None when it cannot be read or is
    empty. Never raises: an unreadable key file is the fail-closed
    degrade, not an error."""
    try:
        material = path.read_bytes().strip()
    except OSError:
        return None
    return material or None


def _read_stored_key(run) -> bytes | None:
    """This run's persisted key, or None when there is no store, no key in
    it, or nothing readable there.

    Read at the engine's own effective uid FIRST -- that is the whole read
    for an operator's `$RALPHD_RUN_SECRETS_DIR` store (which the operator
    chose to make agent-readable) and for a key file an older,
    pre-boundary engine build wrote agent-owned. Under the uid boundary,
    though, the default store's key files are the ones `_persist_key`
    wrote inside `privsep.root_for_write()`: root-owned mode 0600, which
    the engine CANNOT open at its effective uid, because that uid is the
    agent's (`privsep.separate_engine_identity`: real+saved 0, effective
    agent) and that is exactly the property making the store a secret from
    every hat process. So the read is retried inside the same momentary
    raise back to root the WRITE already used -- symmetric with
    `_persist_key`, and needing no privilege the engine does not already
    hold (real and saved uid are 0 whenever `privsep.separated()` is
    True). Without that retry the key persisted by episode N was
    unreadable by episode N+1 in the ONLY configuration that persists one
    by default, so the store bought nothing: the later episode read its
    own run's CONFIRMED record as unauthenticated and fell back to empty
    (task 020's second rejection reproduced exactly this).
    """
    path = _stored_key_path(run)
    if path is None:
        return None
    material = _read_key_material(path)
    if material is None:
        from ralphd.engine import privsep
        if privsep.separated():
            with privsep.root_for_write():
                material = _read_key_material(path)
    if material is None:
        return None
    return _decode_key_material(material)


def _persist_key(run, material: bytes) -> None:
    """Best-effort copy of a freshly minted key into `key_store_dir()`:
    root-owned 0700 directory, root-owned 0600 file, written inside
    `privsep.root_for_write()`, HEX encoded (see `_decode_key_material`).
    A failure here is never fatal -- the key stays in memory, this episode
    authenticates its own writes as usual, and only cross-episode
    verification degrades (to fail-closed)."""
    path = _stored_key_path(run)
    if path is None:
        return
    from ralphd.engine import privsep
    try:
        with privsep.root_for_write():
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, 0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, material.hex().encode("ascii"))
            finally:
                os.close(fd)
            os.chmod(path, 0o600)
    except OSError:
        return


def _authority(run, create: bool = False) -> _Authority | None:
    """This run's authority: the one already held in this process's
    memory, else one rebuilt from the key store (a later episode of the
    same run), else -- only when `create` -- a freshly minted one. None
    means this process holds no key for the run, so nothing about the
    record can be authenticated at all; see
    `read_confirmed_adjudications`, which then reads it as empty."""
    ident = _authority_id(run)
    auth = _AUTHORITIES.get(ident)
    if auth is not None:
        return auth
    material = _read_stored_key(run)
    if material is None:
        if not create:
            return None
        material = secrets.token_bytes(32)
        _persist_key(run, material)
    auth = _Authority(key=material)
    _AUTHORITIES[ident] = auth
    return auth


def forget_run_authority(run) -> None:
    """Drop this run's in-memory authority -- exactly what an engine
    RESTART does to it for free. Exposed so a test can model a later
    episode honestly (the key store, if any, is all that survives)
    instead of pretending a process boundary it never crossed."""
    _AUTHORITIES.pop(_authority_id(run), None)


def _adjudications_mac(entries: list[dict], key: bytes) -> str:
    """A KEYED authentication tag over `entries` -- HMAC-SHA256 under the
    per-run key the engine's `_Authority` holds. This is what a
    worker-phase edit cannot reproduce: the key is in NO file under the
    run dir (it lives in the engine process's memory, plus -- where the
    uid boundary exists -- a root-owned store outside the run dir), so
    there is nothing in the tree a hat process can write for it to read
    the key out of."""
    return hmac.new(key, _canonical(entries).encode("utf-8"),
                     hashlib.sha256).hexdigest()


def _emit(run, type_: str, **data) -> None:
    """Best-effort `RunDir.emit` -- the witness/tamper events below are
    evidence, never load-bearing control flow, so a run object without an
    event log (a bare stub in someone else's test) costs nothing."""
    emit = getattr(run, "emit", None)
    if emit is None:
        return
    try:
        emit(type_, **data)
    except OSError:
        pass


def _witness_macs(run) -> list[str]:
    """Every MAC the engine has ANNOUNCED writing, oldest first, read off
    `events.jsonl` -- the independent witness that makes a coherent forgery
    require updating two artifacts the engine alone appends to, not one.
    The last entry is the MAC the record must currently carry."""
    from ralphd.engine.state import read_events
    macs = []
    for ev in read_events(Path(run.root), (ADJUDICATIONS_RECORDED_EVENT,)):
        mac = ev.get("mac")
        if isinstance(mac, str):
            macs.append(mac)
    return macs


def _tamper_already_witnessed(run) -> bool:
    """Whether tampering has ALREADY been detected in this run -- in this
    engine process's own memory (which nothing outside the process can
    reach) or, across episodes, in the run's event log. Detection is
    sticky by design: once the record has been edited by something other
    than its only writer, nothing later in the run can restore trust in it
    (a forger could otherwise tamper and then wait for the next legitimate
    review write to erase the evidence), and review keeps being told."""
    from ralphd.engine.state import read_events
    auth = _authority(run)
    if auth is not None and auth.tampered:
        return True
    return bool(read_events(Path(run.root), (ADJUDICATIONS_TAMPERED_EVENT,)))


def _flag_tampered(run, reason: str) -> bool:
    """Record the detection in this process's own authority (the sticky
    flag a forger cannot clear by rewriting a file) AND in `events.jsonl`
    (once -- `_tamper_already_witnessed` short-circuits every later read),
    then return True, the `tampered` half of
    `read_confirmed_adjudications`'s return."""
    auth = _authority(run)
    if auth is not None:
        auth.tampered = True
    _emit(run, ADJUDICATIONS_TAMPERED_EVENT, reason=reason)
    return True


def record_confirmed_adjudications(run, adjudications) -> None:
    """Persist every ADJUDICATED entry in `adjudications` (review's own
    `petition_adjudications` verdict field -- a list of
    `{"obligation": str, "adjudication": "CONFIRMED"|"REFUTED", ...}`
    dicts, already schema-checked by `contracts.validate_payload`) into
    `run.petition_adjudications_file` -- CONFIRMED and REFUTED alike, the
    REFUTED ones carrying review's `remedy` text, all under the ONE keyed
    MAC this module has always written. Anything not shaped like a valid
    entry (no `obligation` string, an `adjudication` that is neither
    value) is silently skipped: this function is not a second schema
    validator -- fact 16/#65 -- it is a filter over an already-validated
    payload field.

    Why REFUTED entries live here too (approach 02 task 016, cure for
    task 002, review finding 2): this store is the run's ONE record of
    what review actually decided, and `run_record.build_petitions_section`
    renders it. When only CONFIRMED entries were persisted, a REFUTED
    adjudication -- and the remedy review stated for it -- never reached
    `run-record.json`/`run-record.md` at all. Persisting it changes
    nothing about EXEMPTIONS: `read_confirmed_adjudications` (and hence
    `confirmed_obligations`, `petitioned_task_accepted`,
    `downgrade_confirmed_findings`, `open_unconfirmed_petitions`,
    `adjudicated_petitions_review_context`) filters this record down to
    `adjudication == "CONFIRMED"` entries after authenticating it, so a
    REFUTED entry in the store can never exempt an obligation, and a
    REFUTED adjudication is still "not something a later approach
    inherits" as an exemption (`docs/petition-format.md`) -- it is
    inherited only as history to render.

    Merges into whatever is already on disk keyed by normalised
    obligation text, rather than overwriting -- a later approach's review
    adjudicating a DIFFERENT petition must not erase an earlier approach's
    entries; this is the actual inheritance mechanism together
    with `petition_adjudications_file` never being one of the paths
    `LoopSupervisor._archive_approach` copies away. A no-op (no write at
    all) when nothing changes, so a review pass that adjudicates nothing
    never touches the file's mtime. An obligation already CONFIRMED in
    the store is never downgraded by a later REFUTED entry for the same
    obligation (a duplicate/stale re-adjudication): the CONFIRMED entry
    wins, which is also how `build_petitions_section` renders it.

    Callers: `LoopSupervisor` only, immediately after a review iteration's
    contract payload is parsed -- never a worker or review hat's own
    process. That is the tamper-evidence property PRD requirement C.3
    asks for: "review writes them" means the ENGINE writes them on
    review's behalf, because review's OWN spawned process runs at the
    identical dropped-to-agent uid a worker iteration does (`privsep.py`)
    and could not literally hold a privilege the worker lacks. The write
    itself goes through `privsep.write_root_owned_json` -- the same
    root-owned write task 105 gave `tasks.json`, so under the uid boundary
    the record is not writable by a hat at all -- and leaves THREE
    coherent artifacts behind: the record, its keyed MAC (over a key no
    file under the run dir contains -- `_authority`), and an
    `ADJUDICATIONS_RECORDED_EVENT` witness line in `events.jsonl`, whose
    MAC the engine also remembers in memory for the episode.

    A record found tampered is NOT merged forward: it is dropped in favour
    of the entries being written now (fail closed, never carry an
    untrusted exemption), while the sticky tamper event keeps the finding
    alive for the rest of the run.
    """
    if not adjudications:
        return
    from ralphd.engine import privsep
    existing, tampered = read_adjudications(run)
    if tampered:
        existing = []
    by_key = {_normalize_obligation(e["obligation"]): e for e in existing
              if isinstance(e, dict) and isinstance(e.get("obligation"), str)}
    changed = False
    for item in adjudications:
        if not isinstance(item, dict):
            continue
        adjudication = item.get("adjudication")
        if adjudication not in ("CONFIRMED", "REFUTED"):
            continue
        obligation = item.get("obligation")
        if not isinstance(obligation, str) or not obligation.strip():
            continue
        key = _normalize_obligation(obligation)
        entry = {"obligation": obligation.strip(),
                 "adjudication": adjudication}
        if adjudication == "REFUTED":
            remedy = item.get("remedy")
            entry["remedy"] = remedy.strip() if isinstance(remedy, str) else ""
            prior = by_key.get(key)
            if isinstance(prior, dict) and \
                    prior.get("adjudication") == "CONFIRMED":
                # A CONFIRMED exemption is never downgraded by a later or
                # duplicate REFUTED entry for the same obligation.
                continue
        if by_key.get(key) != entry:
            by_key[key] = entry
            changed = True
    if not changed:
        return
    entries = sorted(by_key.values(), key=lambda e: e["obligation"])
    auth = _authority(run, create=True)
    assert auth is not None  # create=True always yields one
    mac = _adjudications_mac(entries, auth.key)
    doc = {"entries": entries,
           "digest": _adjudications_digest(entries),
           "mac": mac}
    Path(run.root).mkdir(parents=True, exist_ok=True)
    privsep.write_root_owned_json(run.petition_adjudications_file, doc)
    auth.last_mac = mac
    _emit(run, ADJUDICATIONS_RECORDED_EVENT, mac=mac, entries=len(entries))


def read_adjudications(run) -> tuple[list[dict], bool]:
    """`(entries, tampered)` -- EVERY adjudicated entry the store holds,
    CONFIRMED and REFUTED alike (the REFUTED ones with their `remedy`),
    authenticated by exactly the same checks
    `read_confirmed_adjudications` documents below; this is that
    function's own body, and `read_confirmed_adjudications` is the
    CONFIRMED-only filter over it.

    Read this when RENDERING the run's record of what review decided
    (`run_record.build_petitions_section`). NEVER read it to decide
    whether an obligation is exempt: `confirmed_obligations` /
    `read_confirmed_adjudications` are the exemption readers, and they
    are CONFIRMED-only by construction.
    """
    return _read_adjudications_record(run)


def read_confirmed_adjudications(run) -> tuple[list[dict], bool]:
    """`(entries, tampered)` -- the CONFIRMED entries only, read back from
    `run.petition_adjudications_file`. Every entry returned here has
    `adjudication == "CONFIRMED"`; a REFUTED entry the same store carries
    for the run record's sake (see `record_confirmed_adjudications` and
    `read_adjudications`) is filtered out here, so an exemption is only
    ever granted for a CONFIRMED adjudication -- this is the single place
    that property is enforced for every exemption caller.

    `tampered` is True exactly when the record does not match, in
    every respect, what `record_confirmed_adjudications` -- its only
    writer -- leaves behind:

    1. a tamper detection already sticky for this run (in this process's
       own authority, or witnessed in `events.jsonl`): trust is never
       restored later in the run;
    2. the record missing/unreadable although the engine announced writing
       one (a deleted or truncated record);
    3. a malformed record (non-dict top level, non-list `entries`);
    4. a keyed MAC (`_adjudications_mac`) that does not match `entries`;
       this is the check an unkeyed digest cannot make, and the one that
       catches a worker-phase edit that recomputes the documented plain
       digest -- the key is in no file under the run dir, so a hat process
       has nothing to recompute it with;
    5. the unkeyed `digest` field disagreeing with `entries`;
    6. a MAC disagreeing with the last MAC THIS process wrote, when it has
       written one -- an in-memory witness no forger can rewrite, so
       replacing the record with an older (or fabricated) one is caught
       even if the event log is rewritten to match;
    7. otherwise (a record from an earlier episode, nothing written yet in
       this one) the `events.jsonl` witness's latest announced MAC
       disagreeing with the record's, or there being no witness at all --
       a record the engine never announced writing.

    Two cases are deliberately NOT tampering, because both fail in the
    safe direction (no obligation is exempted) and both have an innocent
    cause that is indistinguishable from the guilty one:

    * no record at all and no witness -- nothing has been CONFIRMED yet;
    * a record this process cannot authenticate because it holds no key
      for the run (an engine restart with no `key_store_dir()`, i.e. the
      no-uid-boundary degrade). The entries are read as EMPTY and an
      `ADJUDICATIONS_UNAUTHENTICATED_EVENT` records why; whoever owns the
      run dir can always destroy the record's trustworthiness, and doing
      so only ever REMOVES exemptions.
    """
    entries, tampered = _read_adjudications_record(run)
    return ([e for e in entries
             if isinstance(e, dict)
             and e.get("adjudication") == "CONFIRMED"], tampered)


def _read_adjudications_record(run) -> tuple[list[dict], bool]:
    """The authenticated read itself: `(entries, tampered)` with every
    entry the record holds, unfiltered. `read_adjudications` (render) and
    `read_confirmed_adjudications` (exemptions, CONFIRMED-only) are the
    two public views over this one body; the tamper/MAC semantics live
    here and are shared verbatim by both."""
    from ralphd.engine.state import read_json
    if _tamper_already_witnessed(run):
        return [], True
    witnesses = _witness_macs(run)
    auth = _authority(run)
    expected = auth.last_mac if auth is not None else None
    doc = read_json(run.petition_adjudications_file, default=None)
    if not doc:
        if witnesses or expected:
            return [], _flag_tampered(
                run, "record missing or unreadable although the engine "
                     "announced writing one")
        return [], False
    if not isinstance(doc, dict):
        return [], _flag_tampered(run, "record is not a JSON object")
    entries = doc.get("entries")
    if not isinstance(entries, list):
        return [], _flag_tampered(run, "record's entries field is not a list")
    stored_mac = doc.get("mac")
    if auth is None:
        _emit(run, ADJUDICATIONS_UNAUTHENTICATED_EVENT,
              reason="this engine episode holds no MAC key for the run, so "
                     "the record cannot be authenticated -- it is read as "
                     "empty (no obligation is exempted)")
        return [], False
    if not isinstance(stored_mac, str) or not hmac.compare_digest(
            stored_mac, _adjudications_mac(entries, auth.key)):
        return [], _flag_tampered(
            run, "keyed MAC does not authenticate the record's entries")
    if doc.get("digest") != _adjudications_digest(entries):
        return [], _flag_tampered(
            run, "unkeyed digest field does not match the record's entries")
    if expected is not None:
        if not hmac.compare_digest(expected, stored_mac):
            return [], _flag_tampered(
                run, "the record's MAC is not the one this engine process "
                     "last wrote")
    elif not witnesses or not hmac.compare_digest(witnesses[-1], stored_mac):
        return [], _flag_tampered(
            run, "events.jsonl holds no engine witness for this record's MAC")
    return entries, False


def confirmed_obligations(run) -> tuple[set[str], bool]:
    """`(obligations, tampered)` -- the normalised set of obligations
    CONFIRMED at run scope so far, plus whether the record is tampered.
    A caller MUST treat a tampered record as an EMPTY set of confirmed
    obligations (fail towards the all-blocking default non-negotiable #2
    protects, never towards trusting a record that might have been edited
    to manufacture a false exemption) -- the flag is returned precisely so
    a caller can act on it rather than silently swallow it; see
    `tamper_review_context` below for surfacing it to review itself.
    """
    entries, tampered = read_confirmed_adjudications(run)
    if tampered:
        return set(), True
    return ({_normalize_obligation(e["obligation"]) for e in entries
              if isinstance(e, dict) and isinstance(e.get("obligation"), str)},
            False)


def petitioned_task_accepted(task: dict, confirmed: set[str]) -> bool:
    """Task 010 (requirement C.2, #114): True iff `task` (a `petitioned`
    task record) has a run-scope CONFIRMED adjudication for its OWN
    engine-filed obligation (`task_petition_obligation`) -- "the task
    counts as accepted-with-residual" (PRD C.2). Uses the identical
    fuzzy substring-either-direction match `_obligation_confirmed`
    already applies to a finding's `clause` against the same `confirmed`
    set, because the two free-text channels (a filed petition's own
    `Obligation` field, this function's own constructed obligation
    string) are not guaranteed to agree on exact wording either.

    A task whose status is not `petitioned` at all is never "accepted"
    by this function -- callers gate on status themselves; this is a
    pure obligation-membership check, not a status check.
    """
    if not isinstance(task, dict):
        return False
    return _obligation_confirmed(task_petition_obligation(task), confirmed)


def _refuted_remedy_type(item: dict) -> str:
    """Task 008 (requirement A.4, #155): the REFUTED entry's own
    classification of its remedy -- `"form"` (the remedy changes no
    deliverable, only how it is produced) or the default `"substance"`
    for anything else (missing, blank, or any other value). Case-
    insensitive on the `"form"` literal, whitespace-tolerant; every
    other value -- including every REFUTED entry filed before this task
    existed, none of which ever supplied `remedy_type` at all -- is
    `"substance"`, which is exactly the point: PRD A.4's "substance
    behaves exactly as today" is this function defaulting closed, not a
    separate code path.
    """
    raw = item.get("remedy_type")
    if isinstance(raw, str) and raw.strip().lower() == "form":
        return "form"
    return "substance"


def apply_refuted_petition_adjudications(tasks_doc: dict, adjudications,
                                         form_debt: dict | None = None) -> list[str]:
    """Task 010 (requirement C.2, #114): the REFUTED counterpart to
    `record_confirmed_adjudications` -- but, unlike that function, this
    one is never persisted at run scope (`docs/petition-format.md`:
    REFUTED "is not something a later approach inherits"). Instead it
    acts immediately and directly on `tasks_doc`'s own `petitioned` task
    records, mutating them IN PLACE: any `petitioned` task whose own
    engine-filed obligation (`task_petition_obligation`) fuzzy-matches a
    REFUTED entry in `adjudications` (review's `petition_adjudications`
    verdict field) is routed by that entry's OWN classification of its
    remedy (`_refuted_remedy_type`, task 008/A.4, #155):

    - `"substance"` (the default -- see `_refuted_remedy_type`) moves the
      task to `failed`, the same terminal C.2 says an un-rescued
      exhaustion always lands at, carrying the refutation's own "what
      would satisfy it" text (its `remedy` field) into `validationNotes`
      on the task record, replacing the stale last-attempt handoff that
      got it petitioned in the first place. `failureKind` is set to
      `state.TASK_FAILURE_VALIDATION_EXHAUSTED`, matching the label an
      un-rescued exhaustion already carries (task 024/#33) -- a REFUTED
      petition does not invent a new failure vocabulary, it is the SAME
      exhaustion, just adjudicated. Behaves BYTE-FOR-BYTE as this
      function did before task 008 (every pre-existing caller, none of
      which ever supplied `remedy_type`, is unaffected).
    - `"form"` (the remedy changes no deliverable, only how it is
      produced -- PRD A.4) moves the task to `validated` instead --
      the SAME acceptance a verify pass with only a form/wording/
      process note already gives it (requirement A.2/A.3) -- and NEVER
      touches `validationNotes`: the remedy is recorded as a
      debt-shaped note (`{clause, observed, suggested}`, task 006's
      `verifyNotes` shape) appended to `form_debt[task_id]` when the
      caller supplies that dict, for the caller to hand to
      `state.record_debt_entries`/`RunDir.record_debt` -- the same one
      write path the verify-notes debt ledger already uses, never a
      second ledger writer. `form_debt` is optional and, when omitted,
      the note is simply not recorded anywhere but the task still moves
      to `validated` -- this function's own job (routing the task) never
      depends on the caller wanting the debt side effect too.

    Returns the ids of every task this call moved to `failed` (the
    `"substance"` branch only -- a `"form"`-routed task's id is never in
    this list, matching the docstring's pre-task-008 promise verbatim;
    callers that want the `"form"`-routed ids read `form_debt`'s own
    keys instead), in plan order; a no-op call (nothing REFUTED, or
    nothing `petitioned` to match against) returns `[]` and leaves
    `tasks_doc` (and `form_debt`) untouched.

    A CONFIRMED entry in `adjudications` is silently ignored here --
    that side is `record_confirmed_adjudications`'s job, at run scope,
    read back by `petitioned_task_accepted` above; this function only
    ever moves a task OFF `petitioned` on REFUTED, never accepts one.
    """
    if not adjudications:
        return []
    from ralphd.engine.state import TASK_FAILURE_VALIDATION_EXHAUSTED
    tasks = tasks_doc.get("tasks") or []
    changed: list[str] = []
    handled: set = set()
    for item in adjudications:
        if not isinstance(item, dict) or item.get("adjudication") != "REFUTED":
            continue
        obligation = item.get("obligation")
        if not isinstance(obligation, str) or not obligation.strip():
            continue
        norm = _normalize_obligation(obligation)
        remedy = item.get("remedy")
        remedy_text = remedy.strip() if isinstance(remedy, str) else ""
        remedy_type = _refuted_remedy_type(item)
        for t in tasks:
            if (not isinstance(t, dict) or t.get("status") != "petitioned"
                    or t.get("id") in handled):
                continue
            own = _normalize_obligation(task_petition_obligation(t))
            if own and (own in norm or norm in own):
                handled.add(t.get("id"))
                if remedy_type == "form":
                    t["status"] = "validated"
                    if form_debt is not None:
                        form_debt.setdefault(t.get("id"), []).append({
                            "clause": obligation,
                            "observed": (
                                "Adjudication REFUTED the petition; the "
                                "remedy is classified 'form' -- it changes "
                                "no deliverable, only how it is produced."),
                            "suggested": remedy_text or (
                                "Petition REFUTED with no remedy stated."),
                        })
                else:
                    t["status"] = "failed"
                    t["failureKind"] = TASK_FAILURE_VALIDATION_EXHAUSTED
                    t["validationNotes"] = remedy_text or (
                        "Petition REFUTED with no remedy stated.")
                    changed.append(t.get("id"))
    return changed


def apply_confirmed_petition_adjudications(tasks_doc: dict, adjudications) -> list[str]:
    """Task 015 (PRD requirement C.3, #149): the CONFIRMED counterpart to
    `apply_refuted_petition_adjudications` above, for the SAME `petitioned`
    task records -- "a CONFIRMED adjudication moves the petitioned task to
    `skipped` with the adjudication in `notes` (the obligation is void) and
    re-points every dependent's `dependsOn` from it to nothing (or to the
    replacement task the adjudication names), in the same pass that records
    the confirmation."

    Matches a CONFIRMED entry to a `petitioned` task the identical way the
    REFUTED branch does -- `task_petition_obligation(t)` fuzzy-matched
    (substring either direction, `_normalize_obligation`) against the
    entry's own `obligation` text -- because this is the SAME free-text
    channel disagreement `_refuted_remedy_type`'s sibling already lives
    with. This is deliberately a DIFFERENT match than `criteria_amendment_
    candidates` above: that one matches a CONFIRMED entry against a task's
    own `successCriteria` (the task stays in the plan, only its criteria
    text changes); this one matches against a `petitioned` task's own
    ENGINE-FILED obligation (the task itself is what the petition says is
    impossible), so the two never fire on the same entry for the same
    reason -- a task cannot simultaneously stay in the plan with amended
    criteria and be removed from it.

    For each match:

    - the task's `status` moves to `skipped` (never `failed` -- CONFIRMED
      means the obligation is void, not that the task was tried and came
      up short, the distinction REFUTED's own routing already draws
      between the two adjudications);
    - a note naming the obligation and the CONFIRMED call is appended to
      the task's own `notes` field (kept, never overwritten, exactly like
      task 011's absorption note beside it: `f"{existing}\n{note}"` when
      `notes` is already non-empty);
    - every OTHER task in `tasks_doc` whose `dependsOn` names this task's
      id has that id removed from `dependsOn`; when the SAME entry also
      carries a non-empty `replacement_task_id` string that names a task
      actually present in `tasks_doc` (the judge's own claim of which
      task inherits the dependency -- an id that does not exist in the
      CURRENT plan is never invented a dependency on), that id is added
      in its place (skipped if already present, so a dependent naming
      both the petitioned task and its replacement already never ends up
      with a duplicate entry). No `replacement_task_id`, or one naming a
      task that is not in the plan, leaves the dependent depending on
      nothing in that slot -- "re-points ... to nothing" is the default,
      exactly as the requirement's own parenthetical states it.

    Returns the ids of every task this call moved to `skipped`, in plan
    order; a no-op call (nothing CONFIRMED, or nothing `petitioned` to
    match against) returns `[]` and leaves `tasks_doc` untouched -- the
    same no-op contract `apply_refuted_petition_adjudications` keeps.

    A REFUTED entry in `adjudications` is silently ignored here -- that
    side is `apply_refuted_petition_adjudications`'s job; this function
    only ever moves a task OFF `petitioned` on CONFIRMED.
    """
    if not adjudications:
        return []
    tasks = tasks_doc.get("tasks") or []
    changed: list[str] = []
    handled: set = set()
    for item in adjudications:
        if not isinstance(item, dict) or item.get("adjudication") != "CONFIRMED":
            continue
        obligation = item.get("obligation")
        if not isinstance(obligation, str) or not obligation.strip():
            continue
        norm = _normalize_obligation(obligation)
        raw_replacement = item.get("replacement_task_id")
        replacement_task_id = (raw_replacement.strip()
                               if isinstance(raw_replacement, str) else "")
        for t in tasks:
            if (not isinstance(t, dict) or t.get("status") != "petitioned"
                    or t.get("id") in handled):
                continue
            own = _normalize_obligation(task_petition_obligation(t))
            if not (own and (own in norm or norm in own)):
                continue
            tid = t.get("id")
            handled.add(tid)
            note = (f"Petition CONFIRMED: {obligation.strip()} -- the "
                   "obligation is void; this task is skipped.")
            existing = t.get("notes")
            t["notes"] = f"{existing}\n{note}" if existing else note
            t["status"] = "skipped"
            changed.append(tid)
            valid_replacement = (
                replacement_task_id
                if replacement_task_id and any(
                    isinstance(o, dict) and o.get("id") == replacement_task_id
                    for o in tasks)
                else None)
            for other in tasks:
                if not isinstance(other, dict) or other is t:
                    continue
                deps = other.get("dependsOn")
                if not isinstance(deps, list) or tid not in deps:
                    continue
                new_deps = [d for d in deps if d != tid]
                if (valid_replacement is not None
                        and valid_replacement not in new_deps):
                    new_deps.append(valid_replacement)
                other["dependsOn"] = new_deps
    return changed


def record_refuted_adjudications(run, adjudications) -> list[str]:
    """Task 007 (requirement B, #114): emit one `PETITION_REFUTED_EVENT`
    line per REFUTED entry in `adjudications` (review's/the mid-run
    episode's own `petition_adjudications` verdict field), carrying the
    obligation and the refutation's "what would satisfy it" text (its
    `remedy` field) -- so that text is recorded on the run's own record
    (`events.jsonl`, under `run.root`) EVEN WHEN no `petitioned` task
    exists to carry it in `validationNotes`
    (`apply_refuted_petition_adjudications` above only ever mutates a
    `petitioned` task; the common mid-run-episode case is a task that
    never reached that status at all -- task 005 PART 1's worker
    declaration leaves the task's status untouched).

    Deliberately the opposite of `record_confirmed_adjudications`: no
    root-owned file, no MAC, no run-scope set anything reads back to grant
    an exemption -- `docs/petition-format.md` is explicit that REFUTED
    "is not something a later approach inherits", so this is a plain,
    unauthenticated witness line, never a trust boundary. A no-op call
    (nothing REFUTED, or an entry with a blank/non-string obligation)
    emits nothing.

    Returns the obligation text of every entry this call emitted for, in
    payload order -- used by callers/tests to confirm something was
    actually recorded rather than silently dropped.
    """
    if not adjudications:
        return []
    recorded: list[str] = []
    for item in adjudications:
        if not isinstance(item, dict) or item.get("adjudication") != "REFUTED":
            continue
        obligation = item.get("obligation")
        if not isinstance(obligation, str) or not obligation.strip():
            continue
        remedy = item.get("remedy")
        remedy_text = remedy.strip() if isinstance(remedy, str) else ""
        _emit(run, PETITION_REFUTED_EVENT, obligation=obligation,
              remedy=remedy_text)
        recorded.append(obligation)
    return recorded


# ---------------------------------------------------------------------------
# Task 006 (requirement B.1, #114): a judge-authored criteria amendment on a
# CONFIRMED petition whose SUBJECT is the affected task's own
# `successCriteria`.
# ---------------------------------------------------------------------------

# The only two petition types requirement B.1 permits a criteria amendment
# for -- `docs/petition-format.md`'s own definitions: a `contradictory-
# clauses` petition says two clauses of the SAME contract cannot both be
# satisfied, and `self-referential` says the obligation's own text makes it
# unmeetable as written. Both are, structurally, claims ABOUT THE TEXT of an
# obligation, which is exactly what makes re-scoping that text (rather than
# merely excusing the run from meeting it, `downgrade_confirmed_findings`'s
# job) a coherent remedy. The other three types (`environmental`,
# `temporal`, `external`) are claims about the WORLD, not the text -- no
# rewrite of a task's criteria changes a missing network route -- so a
# CONFIRMED entry against one of those is never eligible here, regardless of
# what a judge's `replacement_criteria` field says.
CRITERIA_AMENDMENT_TYPES = ("contradictory-clauses", "self-referential")


def criteria_amendment_candidates(adjudications, open_petitions,
                                   tasks: list[dict]) -> list[dict]:
    """Every entry in `adjudications` (a review/mid-run-adjudication
    verdict payload's own `petition_adjudications` field) that is a VALID
    judge-authored criteria amendment on a CONFIRMED petition (PRD
    requirement B.1, #114) -- a pure DECISION function, exactly the split
    `apply_refuted_petition_adjudications` already keeps between deciding
    and writing; the write (the `successCriteria` replacement, the
    `criteriaFingerprint` recompute via `_criteria_fingerprint`, the
    `validationAttempts` reset, and the `task.criteria_amended` event) is
    `LoopSupervisor._apply_criteria_amendments`'s job (`engine/loop.py`),
    never this module's -- this module has no `_criteria_fingerprint` of
    its own to call and must not grow one that could drift from the one
    the fingerprint-pinning tests already check.

    An entry counts as a valid amendment only when ALL of these hold:

    1. `item['adjudication'] == 'CONFIRMED'` -- a REFUTED entry, or one
       carrying no `adjudication` field at all, is never eligible: only a
       CONFIRMED petition may re-scope a task (the PRD's own words), and
       requirement B.2's authority boundary (task 008) starts from the
       same CONFIRMED-only gate.
    2. `item['replacement_criteria']` is a non-empty string -- the judge's
       own replacement text; nothing is amended when a CONFIRMED entry
       carries none (the ordinary case: an obligation about a PRD clause,
       never about a task's own criteria, has no reason to carry one at
       all, per B.3 -- 'no text is rewritten anywhere' for that case).
    3. `item['task_id']` names a task actually present in `tasks` -- the
       judge's own claim of WHICH task; an amendment naming a task id
       that does not exist in the CURRENT plan is silently dropped (never
       applied to some other task by guesswork).
    4. `item['obligation']` matches (by normalised obligation text) an
       entry in `open_petitions` whose own `type` field is one of
       `CRITERIA_AMENDMENT_TYPES` -- the type comes from the FILED
       petition on disk, never merely from the judge's say-so in the
       adjudication entry itself, exactly the same 'decide from the
       filed fact, not the claim about it' discipline
       `duplicate_petition_obligations` already applies structurally.
    5. The named task's OWN, CURRENT `successCriteria` text fuzzy-matches
       (`_normalize_obligation`, substring either direction -- the same
       rule `_obligation_confirmed` already applies for the identical
       reason: two free-text channels rarely agree on exact wording) the
       petition's `obligation` text -- this is the 'subject is the
       task's own successCriteria' gate the PRD's B.1 draws: a CONFIRMED,
       correctly-typed petition about something ELSE (a PRD requirement
       letter, another task's criteria) is never eligible here, however
       its `task_id`/`replacement_criteria` fields read.

    Returns one dict per valid amendment, in `adjudications` order:
    `{"task": <the matching dict from `tasks`, the SAME object -- a
    caller mutating it mutates `tasks` too>, "old_criteria": str,
    "new_criteria": str, "obligation": str, "adjudication": <a COPY of
    the full adjudication entry dict, for the event's own authorizing-
    adjudication field>}`. Empty when nothing is eligible -- the
    overwhelming common case (most CONFIRMED petitions are about PRD
    obligations, per B.3, never a task's own criteria).
    """
    if not adjudications:
        return []
    petitions_by_obligation: dict[str, dict] = {}
    for p in open_petitions or []:
        if isinstance(p, dict) and isinstance(p.get("obligation"), str):
            key = _normalize_obligation(p["obligation"])
            petitions_by_obligation.setdefault(key, p)
    tasks_by_id = {t.get("id"): t for t in tasks or [] if isinstance(t, dict)}
    out: list[dict] = []
    for item in adjudications:
        if not isinstance(item, dict) or item.get("adjudication") != "CONFIRMED":
            continue
        replacement = item.get("replacement_criteria")
        if not isinstance(replacement, str) or not replacement.strip():
            continue
        task_id = item.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            continue
        task = tasks_by_id.get(task_id)
        if task is None:
            continue
        obligation = item.get("obligation")
        if not isinstance(obligation, str) or not obligation.strip():
            continue
        petition = petitions_by_obligation.get(_normalize_obligation(obligation))
        if petition is None or petition.get("type") not in CRITERIA_AMENDMENT_TYPES:
            continue
        current = task.get("successCriteria")
        if not isinstance(current, str) or not current.strip():
            continue
        norm_current = _normalize_obligation(current)
        norm_obligation = _normalize_obligation(obligation)
        if not (norm_current in norm_obligation or norm_obligation in norm_current):
            continue
        out.append({
            "task": task,
            "old_criteria": current,
            "new_criteria": replacement.strip(),
            "obligation": obligation,
            "adjudication": dict(item),
        })
    return out


def criteria_amendment_refusals(adjudications, tasks: list[dict]) -> list[dict]:
    """Task 008 (requirement B.2, #114): the authority-boundary sibling of
    `criteria_amendment_candidates` -- every CONFIRMED entry in
    `adjudications` that carries a non-empty `replacement_criteria` (a
    genuine attempted amendment, not an ordinary PRD-obligation
    adjudication with no rewrite intent at all) whose `task_id` names
    something that is NOT an id present in the CURRENT plan's `tasks`.

    This is exactly the shape a judge amendment would take if it tried to
    rewrite the PRD text, a Definition-of-done item, or any path under
    `/config` -- none of those is ever a key in `tasks_by_id`, because
    `tasks` is the plan's own task table and nothing else. Rather than the
    silent drop `criteria_amendment_candidates` gives an ordinary
    'task exists but is not the obligation's own subject' mismatch (that
    case -- a judge naming a DIFFERENT real task's id -- stays silently
    refused, per B.2's own text; only 'not a task at all' gets a named
    refusal here), this function surfaces the OFFENDING TARGET verbatim
    (`item['task_id']`, whatever string it is -- 'PRD', 'DoD item 6',
    '/config/policy.md', or a bogus id) so the caller can record a
    refusal that NAMES what was attempted, instead of the attempt simply
    vanishing.

    Returns one dict per refused attempt, in `adjudications` order:
    `{"target": item['task_id'], "obligation": item.get('obligation'),
    "adjudication": dict(item)}`. Empty when every CONFIRMED
    replacement-bearing entry names a real task id (the common case) or
    there is nothing to refuse at all.
    """
    if not adjudications:
        return []
    task_ids = {t.get("id") for t in tasks or [] if isinstance(t, dict)}
    out: list[dict] = []
    for item in adjudications:
        if not isinstance(item, dict) or item.get("adjudication") != "CONFIRMED":
            continue
        replacement = item.get("replacement_criteria")
        if not isinstance(replacement, str) or not replacement.strip():
            continue
        task_id = item.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            continue
        if task_id in task_ids:
            continue
        out.append({
            "target": task_id,
            "obligation": item.get("obligation"),
            "adjudication": dict(item),
        })
    return out


def downgrade_confirmed_findings(findings_list: list[dict],
                                  confirmed: set[str]) -> list[dict]:
    """A NEW findings list (never mutates `findings_list` or its dicts in
    place) where every `severity: "blocking"` finding whose `clause`
    matches (normalised, substring in either direction -- the two
    channels' free text rarely agree on exact wording) a CONFIRMED
    obligation is downgraded to `severity: "residual"`. This is PRD
    requirement C.4/`docs/petition-format.md`'s own words: "CONFIRMED
    converts that obligation into a disclosed residual for the remainder
    of the run" -- the finding is never DROPPED, only recategorised, so
    `findings.compute_verdict`'s existing severity-only rule handles the
    rest without any change of its own.

    An empty `confirmed` set is a no-op that still returns a fresh list
    (never the same object) -- callers may freely reassign their own
    `findings_list` name to this return value either way.
    """
    if not confirmed:
        return list(findings_list)
    out = []
    for f in findings_list:
        if isinstance(f, dict) and f.get("severity") == "blocking":
            clause = _normalize_obligation(str(f.get("clause", "")))
            if clause and any(clause in c or c in clause for c in confirmed):
                f = dict(f)
                f["severity"] = "residual"
        out.append(f)
    return out


def tamper_review_context(tampered: bool) -> str:
    """The review-prompt context block warning review that its own
    run-scope CONFIRMED-adjudications record (`read_confirmed_
    adjudications`) no longer authenticates -- i.e. something other than
    `record_confirmed_adjudications` touched it since the engine wrote it
    (keyed MAC, unkeyed digest or `events.jsonl` witness disagreeing; a
    removed key or a deleted record counts too). Returns `''` when
    nothing is wrong (the overwhelming common case). Joined into
    `LoopSupervisor._review_extra_context` beside the open-petitions block
    -- this is what makes a worker-phase edit to the record 'documented as
    a blocking finding' (PRD requirement C.3): the instruction lives in
    review's own prompt, the same channel every other engine-detected
    contract fact already reaches review through.
    """
    if not tampered:
        return ""
    return (
        "\n## Petition adjudication record tampered (task 011, requirement C, #112)\n"
        "\nThe run-scope record of CONFIRMED petition adjudications no "
        "longer authenticates against the keyed MAC and the `events.jsonl` "
        "witness line the engine wrote it with -- something other than "
        "review's own prior adjudication has modified it (a "
        "worker-phase edit is exactly the kind of tampering this checks "
        "for). You MUST include this as a BLOCKING finding (`severity: "
        "\"blocking\"`) in your verdict's findings channel. The detection is "
        "recorded in this run's own event log and is NOT cleared by a later "
        "write: no previously CONFIRMED obligation counts as confirmed for "
        "the remainder of this run, and any petition an approach still needs "
        "must be adjudicated afresh, on its own evidence.\n")


# ---------------------------------------------------------------------------
# Task 014 (requirement F.1, #144): adjudicated petitions are CLOSED by the
# engine -- rendered to review as HISTORY, never as open or re-filed.
# ---------------------------------------------------------------------------

def adjudicated_petitions_review_context(run) -> str:
    """The review-prompt context block naming every obligation already
    CONFIRMED at run scope (`read_confirmed_adjudications(run)` --
    `petitions.record_confirmed_adjudications`'s own persisted record,
    `run.petition_adjudications_file`) -- rendered as SETTLED HISTORY, a
    fact of the run's own record, never as something still open for a
    fresh hearing.

    This is deliberately a SEPARATE block from `petitions_review_context`
    above (the OPEN petitions list): an obligation appearing here has
    already been excluded from that OPEN list (`open_unconfirmed_
    petitions` already filters on the same `confirmed_obligations(run)`
    set) -- this function's whole job is to state the settled fact
    explicitly, in-prompt, rather than leaving review to infer "not
    listed as open" as its only evidence that the obligation is closed.
    Without this, review sees no textual anchor at all for `review.md`'s
    own re-file rule ("a duplicate filing against an obligation that
    already has one ... is itself a BLOCKING finding") to be scoped
    against -- this block is that anchor: it tells review, per obligation,
    "this is closed; a NEW filing against it is the violation the rule
    describes, not this entry's own continued appearance here."

    Returns `''` when the record is empty or tampered (`read_confirmed_
    adjudications` already returns `entries=[]` for a tampered record --
    fail towards surfacing the obligation again for a fresh adjudication,
    never towards rendering an untrusted record as settled history), the
    overwhelming common case for any run that has confirmed nothing yet.
    """
    entries, tampered = read_confirmed_adjudications(run)
    if tampered or not entries:
        return ""
    lines = [
        "\n## Adjudicated petitions -- history, not open (task 014, "
        "requirement F.1, #144)\n\n"
        "The obligation(s) below are ALREADY CONFIRMED at run scope (see "
        "`docs/petition-format.md`) and are rendered here purely as "
        "SETTLED HISTORY for context -- they are NOT open, NOT awaiting a "
        "hearing, and MUST NOT be adjudicated again: do not include any "
        "of them in your verdict's `petition_adjudications` field. "
        "`review.md`'s re-file prohibition (a duplicate/re-filed petition "
        "is itself a BLOCKING finding, not a second hearing) concerns a "
        "*NEW* filing made against one of these obligations AFTER it "
        "already carries this adjudication -- never the mere fact that "
        "the obligation continues to appear here, in this history block, "
        "on every later review pass.\n",
    ]
    for entry in entries:
        obligation = entry.get("obligation", "") if isinstance(entry, dict) else ""
        lines.append(f"\n- **Obligation**: {obligation} -- CONFIRMED\n")
    return "".join(lines)
