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


def ensure_petitions_file(workspace: Path, contract_text: str = "") -> Path | None:
    """The petitions file a filing goes into, creating an empty one when the
    workspace has none yet -- so that a contract which DOES declare the
    protocol never falls back to `failed` merely because no wave file had
    been created yet. Without this, the workspace's file inventory would
    remain a second, hidden discriminant beside the contract, which is
    exactly what requirement C.3 forbids ("the two branches are
    distinguished by the contract and nothing else").

    Called only where the contract has already been found to declare a
    protocol (`contract_declares_petition_protocol`); returns the existing
    file when there is one (the last, matching `file_worker_petition`'s own
    target choice), or None when the file cannot be created at all (a
    read-only workspace), in which case the caller keeps the `failed`
    fallback.
    """
    files = discover_petition_files(workspace)
    if files:
        return files[-1]
    match = _WAVE_RE.search(contract_text or "")
    wave = f"v{match.group(1)}" if match else DEFAULT_PETITIONS_WAVE
    path = workspace / "artifacts" / "reports" / f"{wave}-petitions.md"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# {wave} impossibility petitions\n\n"
            "Created by the engine because this run's contract declares a "
            "petition protocol (see `docs/petition-format.md`). One section "
            "per petition; no petition filed yet.\n")
    except OSError:
        return None
    return path


def open_petitions_in_workspace(workspace: Path) -> list[dict]:
    """Every OPEN petition across every petitions file found under
    `workspace` -- the input `petitions_review_context` renders. A file
    that cannot be read (permissions, a race with a concurrent write)
    costs nothing more than itself, the same posture
    `_apply_operator_status_rulings` takes for a broken amendments mount:
    skipped, not raised."""
    petitions: list[dict] = []
    for path in discover_petition_files(workspace):
        try:
            text = path.read_text()
        except OSError:
            continue
        petitions.extend(open_petitions(text))
    return petitions


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


def file_worker_petition(workspace: Path, obligation: str, ptype: str,
                          evidence: str) -> list[str]:
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

    Returns a list of error strings; empty means the petition was written.
    A non-empty return means nothing was written to disk -- the caller (an
    `extra_validate` hook, the same shape every other worker semantic
    check in this tree already uses) folds these into the ordinary
    contract re-ask ladder, so a refused declaration reaches the SAME
    worker again rather than disappearing silently.
    """
    key = _normalize_obligation(obligation)
    files = discover_petition_files(workspace)
    for path in files:
        try:
            text = path.read_text()
        except OSError:
            continue
        for p in parse_petitions(text):
            if _normalize_obligation(p["obligation"]) == key:
                return [
                    "petition: an obligation already has a filed "
                    f"petition ({p['obligation']!r}) -- a duplicate "
                    "declaration for the same obligation is refused, not "
                    "filed (docs/petition-format.md's one-petition-per-"
                    "obligation rule)"
                ]
    if not files:
        return [
            "petition: no <wave>-petitions.md file exists under "
            "artifacts/reports/ to file this declaration into"
        ]
    target = files[0] if len(files) == 1 else files[-1]
    try:
        existing = target.read_text()
    except OSError:
        existing = ""
    section = format_petition_section(obligation, ptype, evidence)
    if existing and not existing.endswith("\n"):
        section = "\n" + section
    with target.open("a", encoding="utf-8") as f:
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


def file_exhaustion_petition(workspace: Path, task: dict) -> list[str]:
    """File a petition on a task's behalf the moment its validation ladder
    is exhausted (`state.VALIDATION_ATTEMPT_LIMIT`), carrying its
    last-attempt handoff -- the text already written into
    `task['validationNotes']` by the verifier's own rejection -- as the
    petition's evidence (PRD v0.16 requirement C.1, #114: "v0.15 approach
    4's task 001 handoff was a complete, well-argued petition that had
    nowhere to go").

    A SECOND writer of `<wave>-petitions.md`, beside the worker's own typed
    declaration (`file_worker_petition`, task 004): the worker may never
    have declared impossibility explicitly (it can simply keep trying and
    losing to review), so by the time the ladder is spent there may be no
    filed petition at all for this obligation -- this is what makes sure
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
    """
    tid = task.get("id", "?")
    obligation = task_petition_obligation(task)
    evidence = (task.get("validationNotes") or "").strip() or (
        f"Task {tid} exhausted its validation ladder with no verifier "
        "reason recorded on the last attempt.")
    return file_worker_petition(workspace, obligation,
                                PETITION_TYPE_EXHAUSTION_DEFAULT, evidence)


def duplicate_petition_obligations(workspace: Path) -> list[dict]:
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
    return [
        {"obligation": first_text[key], "filings": filings}
        for key, filings in grouped.items()
        if len(filings) > 1
    ]


def duplicate_petition_findings(workspace: Path) -> list[dict]:
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
    """
    out = []
    for dup in duplicate_petition_obligations(workspace):
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
        })
    return out


def duplicate_petitions_review_context(workspace: Path) -> str:
    """The review-prompt context block naming every duplicate/re-filed
    petition the engine has already detected and turned into a blocking
    finding (`duplicate_petition_findings`) -- the prompt half of the
    engine/prompt surface task 012 puts this detection on. Tells review
    the finding is already recorded engine-side (so review does not need
    to -- and must not -- treat any of these obligations as newly
    eligible for a fresh CONFIRMED/REFUTED call) rather than leaving
    review to notice the duplicate on its own. Returns '' when none exist
    (the common case).
    """
    dupes = duplicate_petition_obligations(workspace)
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


def open_unconfirmed_petitions(workspace: Path, run=None) -> list[dict]:
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
    """
    petitions_list = open_petitions_in_workspace(workspace)
    if run is not None and petitions_list:
        confirmed, tampered = confirmed_obligations(run)
        if not tampered and confirmed:
            petitions_list = [
                p for p in petitions_list
                if not _obligation_confirmed(p["obligation"], confirmed)
            ]
    return petitions_list


def petitions_review_context(workspace: Path, run=None) -> str:
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
    """
    petitions = open_unconfirmed_petitions(workspace, run)
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
    ident = hashlib.sha256(_authority_id(run).encode("utf-8")).hexdigest()
    return store / f"{ident[:32]}.key"


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


def _read_stored_key(run) -> bytes | None:
    path = _stored_key_path(run)
    if path is None:
        return None
    try:
        material = path.read_bytes().strip()
    except OSError:
        return None
    if not material:
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
    """Persist every CONFIRMED entry in `adjudications` (review's own
    `petition_adjudications` verdict field -- a list of
    `{"obligation": str, "adjudication": "CONFIRMED"|"REFUTED", ...}`
    dicts, already schema-checked by `contracts.validate_payload`) into
    `run.petition_adjudications_file`. ONLY REFUTED->CONFIRMED entries are
    kept -- a REFUTED entry is not something a later approach inherits
    (the PRD's own words: review "states what would satisfy the
    obligation", not a permanent record), and anything not shaped like a
    valid entry is silently skipped (this function is not a second
    schema validator -- fact 16/#65 -- it is a filter over an
    already-validated payload field).

    Merges into whatever is already on disk keyed by normalised
    obligation text, rather than overwriting -- a later approach's review
    adjudicating a DIFFERENT petition must not erase an earlier approach's
    CONFIRMED entries; this is the actual inheritance mechanism together
    with `petition_adjudications_file` never being one of the paths
    `LoopSupervisor._archive_approach` copies away. A no-op (no write at
    all) when nothing new is CONFIRMED, so a review pass that adjudicates
    nothing, or only REFUTES, never touches the file's mtime.

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
    existing, tampered = read_confirmed_adjudications(run)
    if tampered:
        existing = []
    by_key = {_normalize_obligation(e["obligation"]): e for e in existing
              if isinstance(e, dict) and isinstance(e.get("obligation"), str)}
    changed = False
    for item in adjudications:
        if not isinstance(item, dict):
            continue
        if item.get("adjudication") != "CONFIRMED":
            continue
        obligation = item.get("obligation")
        if not isinstance(obligation, str) or not obligation.strip():
            continue
        key = _normalize_obligation(obligation)
        entry = {"obligation": obligation.strip(), "adjudication": "CONFIRMED"}
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


def read_confirmed_adjudications(run) -> tuple[list[dict], bool]:
    """`(entries, tampered)` read back from `run.petition_adjudications_
    file`. `tampered` is True exactly when the record does not match, in
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


def apply_refuted_petition_adjudications(tasks_doc: dict, adjudications) -> list[str]:
    """Task 010 (requirement C.2, #114): the REFUTED counterpart to
    `record_confirmed_adjudications` -- but, unlike that function, this
    one is never persisted at run scope (`docs/petition-format.md`:
    REFUTED "is not something a later approach inherits"). Instead it
    acts immediately and directly on `tasks_doc`'s own `petitioned` task
    records, mutating them IN PLACE: any `petitioned` task whose own
    engine-filed obligation (`task_petition_obligation`) fuzzy-matches a
    REFUTED entry in `adjudications` (review's `petition_adjudications`
    verdict field) moves to `failed` -- the same terminal C.2 says an
    un-rescued exhaustion always lands at -- carrying the refutation's
    own "what would satisfy it" text (its `remedy` field) into
    `validationNotes` on the task record, replacing the stale last-attempt
    handoff that got it petitioned in the first place. `failureKind` is
    set to `state.TASK_FAILURE_VALIDATION_EXHAUSTED`, matching the label
    an un-rescued exhaustion already carries (task 024/#33) -- a REFUTED
    petition does not invent a new failure vocabulary, it is the SAME
    exhaustion, just adjudicated.

    Returns the ids of every task this call moved to `failed`, in plan
    order; a no-op call (nothing REFUTED, or nothing `petitioned` to
    match against) returns `[]` and leaves `tasks_doc` untouched.

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
    for item in adjudications:
        if not isinstance(item, dict) or item.get("adjudication") != "REFUTED":
            continue
        obligation = item.get("obligation")
        if not isinstance(obligation, str) or not obligation.strip():
            continue
        norm = _normalize_obligation(obligation)
        remedy = item.get("remedy")
        remedy_text = remedy.strip() if isinstance(remedy, str) else ""
        for t in tasks:
            if (not isinstance(t, dict) or t.get("status") != "petitioned"
                    or t.get("id") in changed):
                continue
            own = _normalize_obligation(task_petition_obligation(t))
            if own and (own in norm or norm in own):
                t["status"] = "failed"
                t["failureKind"] = TASK_FAILURE_VALIDATION_EXHAUSTED
                t["validationNotes"] = remedy_text or (
                    "Petition REFUTED with no remedy stated.")
                changed.append(t.get("id"))
    return changed


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
