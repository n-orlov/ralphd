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
"""

from __future__ import annotations

import re
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
