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
   (`run.root/petition-adjudications.key`, minted root-owned mode 0600 by
   the engine) that the record itself does not contain, so recomputing
   the documented hash of hand-edited entries does not produce a valid
   tag -- an unkeyed digest stored beside the data it covers, which is
   what this module shipped first, is recomputable by anyone who can
   write the file and therefore proves nothing;
3. every write announces its tag in an append-only `events.jsonl` witness
   line (`ADJUDICATIONS_RECORDED_EVENT`) that `read_confirmed_
   adjudications` cross-checks, so even a forger who read the key in an
   unprivileged single-uid container (the documented degrade: no boundary
   to raise into) must also fabricate an engine witness line -- and any
   detection is itself recorded (`ADJUDICATIONS_TAMPERED_EVENT`) and
   sticky for the rest of the run.

Any mismatch means the record is treated as EMPTY (no obligation is
exempted) and review is told, in its own prompt, to raise a BLOCKING
finding -- fail closed, never towards trusting a record that may have
been edited to manufacture an exemption.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from pathlib import Path

# The four petition types `docs/petition-format.md` enumerates. A section
# whose `Type` field is not one of these is not treated as a petition at
# all -- this is what lets the file's own title/intro heading (which
# names none of them) pass through unnoticed.
PETITION_TYPES = ("environmental", "temporal", "contradictory-clauses",
                  "external")

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


def petitions_review_context(workspace: Path) -> str:
    """Render the review-prompt context block for every OPEN petition
    found under `workspace` -- the same treatment
    `LoopSupervisor._operator_amendment_review_context` gives operator
    rulings: a named heading, instructions on what review must do with
    each item, then one entry per petition. Returns '' when nothing is
    open (the common case -- most waves file no petitions at all), so
    review prompts are unaffected until a petition actually exists.
    """
    petitions = open_petitions_in_workspace(workspace)
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

# The per-run MAC key file (`run.root/petition-adjudications.key`) and the
# two `events.jsonl` witness event types. Together with the root-owned
# record write (`privsep.write_root_owned_json`) these are the three
# independent artifacts a forged adjudication would have to line up.
ADJUDICATIONS_KEY_FILENAME = "petition-adjudications.key"
ADJUDICATIONS_RECORDED_EVENT = "petition_adjudications_recorded"
ADJUDICATIONS_TAMPERED_EVENT = "petition_adjudications_tampered"

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


def _key_path(run) -> Path:
    """The per-run MAC key backing `_adjudications_mac`, at
    `run.root/petition-adjudications.key` (documented in SPEC 5.1's
    run-dir tree and `docs/architecture.md`). Written ONCE, by the engine,
    root-owned mode 0600 when `privsep.separated()` -- i.e. unreadable by
    the dropped-to-agent uid every hat process (worker AND review) runs
    at, which is what makes forging a MAC impossible rather than merely
    detectable. Lives at run scope, beside the record it authenticates,
    so it survives an engine restart mid-run (the record must still verify
    in a later episode) and is never copied into `approaches/NN/`."""
    return Path(run.root) / ADJUDICATIONS_KEY_FILENAME


def _read_key(run) -> bytes | None:
    """The run's MAC key, or None when there is none to read (no record
    has ever been written -- or the key was removed, which is itself
    treated as tampering by `read_confirmed_adjudications` whenever a
    record file does exist)."""
    try:
        material = _key_path(run).read_bytes().strip()
    except OSError:
        return None
    return material or None


def _load_or_create_key(run) -> bytes:
    """The run's MAC key, minted on first use. Created inside
    `privsep.root_for_write()` with mode 0600 so that, wherever the uid
    boundary exists, the key is root-owned and no hat process can read it;
    with no boundary (`privsep.separated()` is False -- the test suite, an
    unprivileged container) it degrades to an agent-owned 0600 file, the
    same documented degrade `privsep.write_root_owned_json` takes, and
    detection then rests on the `events.jsonl` witness below."""
    existing = _read_key(run)
    if existing is not None:
        return existing
    from ralphd.engine import privsep
    path = _key_path(run)
    path.parent.mkdir(parents=True, exist_ok=True)
    material = secrets.token_hex(32).encode("ascii")
    tmp = path.with_suffix(path.suffix + ".tmp")
    with privsep.root_for_write():
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, material)
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    return _read_key(run) or material


def _adjudications_mac(entries: list[dict], key: bytes) -> str:
    """A KEYED authentication tag over `entries` -- HMAC-SHA256 under the
    per-run key `_key_path` holds. This is what a worker-phase edit cannot
    reproduce: the key is not in the record, and under the uid boundary it
    is not even readable by the uid a hat runs at."""
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
    """Whether tampering has ALREADY been detected and recorded in this
    run's event log. Detection is sticky by design: once the record has
    been edited by something other than its only writer, nothing later in
    the run can restore trust in it (a forger could otherwise tamper and
    then wait for the next legitimate review write to erase the
    evidence), and review keeps being told about it."""
    from ralphd.engine.state import read_events
    return bool(read_events(Path(run.root), (ADJUDICATIONS_TAMPERED_EVENT,)))


def _flag_tampered(run, reason: str) -> bool:
    """Record the detection in `events.jsonl` (once -- `_tamper_already_
    witnessed` short-circuits every later read) and return True, the
    `tampered` half of `read_confirmed_adjudications`'s return."""
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
    coherent artifacts behind: the record, its keyed MAC (over a key the
    record does not contain, `_key_path`), and an
    `ADJUDICATIONS_RECORDED_EVENT` witness line in `events.jsonl`.

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
    key = _load_or_create_key(run)
    mac = _adjudications_mac(entries, key)
    doc = {"entries": entries,
           "digest": _adjudications_digest(entries),
           "mac": mac}
    Path(run.root).mkdir(parents=True, exist_ok=True)
    privsep.write_root_owned_json(run.petition_adjudications_file, doc)
    _emit(run, ADJUDICATIONS_RECORDED_EVENT, mac=mac, entries=len(entries))


def read_confirmed_adjudications(run) -> tuple[list[dict], bool]:
    """`(entries, tampered)` read back from `run.petition_adjudications_
    file`. `tampered` is True exactly when the record does not match, in
    every respect, what `record_confirmed_adjudications` -- its only
    writer -- leaves behind:

    1. a tamper detection already witnessed in `events.jsonl` (sticky:
       trust is never restored later in the run);
    2. the record missing/unreadable although the engine announced
       writing one (a deleted or truncated record);
    3. a malformed record (non-dict top level, non-list `entries`);
    4. no MAC key on disk although a record exists -- a record the engine
       never wrote;
    5. a keyed MAC (`_adjudications_mac`) that does not match `entries`;
       this is the check an unkeyed digest cannot make, and the one that
       catches a worker-phase edit that recomputes the documented plain
       digest;
    6. the unkeyed `digest` field disagreeing with `entries`;
    7. the `events.jsonl` witness's latest announced MAC disagreeing with
       the record's -- so a forger would have to hold the key AND append a
       matching engine witness line, not just rewrite one file.

    A missing file with no witness is `([], False)` -- no record at all is
    not tampering, just nothing having been CONFIRMED yet.
    """
    from ralphd.engine.state import read_json
    if _tamper_already_witnessed(run):
        return [], True
    witnesses = _witness_macs(run)
    doc = read_json(run.petition_adjudications_file, default=None)
    if not doc:
        if witnesses:
            return [], _flag_tampered(
                run, "record missing or unreadable although the engine "
                     "announced writing one")
        return [], False
    if not isinstance(doc, dict):
        return [], _flag_tampered(run, "record is not a JSON object")
    entries = doc.get("entries")
    if not isinstance(entries, list):
        return [], _flag_tampered(run, "record's entries field is not a list")
    key = _read_key(run)
    if key is None:
        return [], _flag_tampered(
            run, "a record exists but the run holds no MAC key -- the "
                 "engine never wrote this record")
    stored_mac = doc.get("mac")
    if not isinstance(stored_mac, str) or not hmac.compare_digest(
            stored_mac, _adjudications_mac(entries, key)):
        return [], _flag_tampered(
            run, "keyed MAC does not authenticate the record's entries")
    if doc.get("digest") != _adjudications_digest(entries):
        return [], _flag_tampered(
            run, "unkeyed digest field does not match the record's entries")
    if not witnesses or not hmac.compare_digest(witnesses[-1], stored_mac):
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
