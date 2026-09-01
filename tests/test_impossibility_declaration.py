"""Task 004 (requirement A.1, #114): the worker contract's structured
impossibility declaration -- a worker names the obligation (a requirement
letter, a DoD item, or its own task's criterion clause), the petition TYPE
(`petitions.PETITION_TYPES`) and the EVIDENCE, and the ENGINE -- never the
worker -- writes/validates the filing into the wave's own
`artifacts/reports/<wave>-petitions.md`, in `docs/petition-format.md`'s
field shape, refusing a second declaration against an obligation already
filed instead of filing it.

Three behaviours, pinned as separate tests, each driven through the SAME
`contract_enforcement.enforce_contract` re-ask ladder every other worker
semantic channel in this tree already uses (`transitions.
worker_unsatisfiable_report_errors`'s own sibling, `transitions.
worker_petition_report_errors`, plus the write path itself,
`petitions.file_worker_petition`):

- a valid declaration is written to the petitions file and parses back
  through `petitions.parse_petitions` with the declared type;
- a malformed declaration (a blank `obligation`) is refused through the
  ladder rather than filed -- the ladder exhausts, `agent_fault` is True,
  and the petitions file gains no new section;
- a duplicate declaration against an obligation that already has one is
  refused the same way, and the original filing is left untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine import contract_enforcement as ce
from ralphd.engine import contracts
from ralphd.engine import petitions
from ralphd.engine import transitions


def _make_petitions_file(workspace: Path, wave: str = "v0.16") -> Path:
    reports = workspace / "artifacts" / "reports"
    reports.mkdir(parents=True)
    path = reports / f"{wave}-petitions.md"
    path.write_text(
        f"# {wave} petitions\n\nNo petition filed yet in this wave.\n")
    return path


def _extra_validate(workspace: Path):
    """The same shape every sibling worker channel's `extra_validate`
    hook already uses (`_apply_unsatisfiable_report`,
    `_apply_worker_completion_report`, ...): the schema/semantic check
    first (`transitions.worker_petition_report_errors`), then the
    engine's own write path (`petitions.file_worker_petition`) -- never a
    second schema validator (fact 16/#65), only ever called once the
    payload is already schema-valid.
    """

    def check(payload: dict) -> list[str]:
        errors = transitions.worker_petition_report_errors(payload)
        if errors:
            return errors
        petition = payload.get("petition")
        if not isinstance(petition, dict):
            return []
        return petitions.file_worker_petition(
            workspace, petition["obligation"], petition["type"],
            petition["evidence"])

    return check


# ---------------------------------------------------------------------------
# `self-referential` joins the type table (requirement A.1's own text)
# ---------------------------------------------------------------------------


def test_self_referential_is_a_member_of_petition_types():
    assert "self-referential" in petitions.PETITION_TYPES
    assert len(petitions.PETITION_TYPES) == 5


def test_worker_schema_accepts_a_self_referential_petition():
    payload = {
        "verdict": "no_verdict",
        "petition": {
            "obligation": "Task 900's own criterion clause",
            "type": "self-referential",
            "evidence": "the criterion names its own not-yet-existing sha",
        },
    }
    assert contracts.validate_payload("worker", payload) == []


def test_petition_format_doc_names_five_types_including_self_referential():
    doc = Path("docs/petition-format.md").read_text()
    assert "## The five types" in doc
    assert "## The four types" not in doc
    assert "self-referential" in doc


# ---------------------------------------------------------------------------
# A valid declaration: filed, then parses back with its declared type
# ---------------------------------------------------------------------------


def test_a_valid_declaration_is_written_and_parses_back_with_its_type(tmp_path):
    petitions_file = _make_petitions_file(tmp_path)
    obligation = "Requirement Z: an obligation this test invents for coverage"
    raw = json.dumps({
        "verdict": "no_verdict",
        "petition": {
            "obligation": obligation,
            "type": "environmental",
            "evidence": "no such capability exists in this sandbox",
        },
    })

    def respond(attempt: int, prior_errors: list[str] | None) -> str:
        return raw

    result = ce.enforce_contract(
        "worker", respond, max_retries=2,
        extra_validate=_extra_validate(tmp_path))

    assert result.ok, result.errors
    parsed = petitions.parse_petitions(petitions_file.read_text())
    assert len(parsed) == 1
    assert parsed[0]["obligation"] == obligation
    assert parsed[0]["type"] == "environmental"
    assert parsed[0]["adjudication"] is None


# ---------------------------------------------------------------------------
# A malformed declaration: refused through the re-ask ladder, never filed
# ---------------------------------------------------------------------------


def test_a_malformed_declaration_is_refused_through_the_reask_ladder(tmp_path):
    petitions_file = _make_petitions_file(tmp_path)
    # A blank obligation passes the schema's own "type": "string" check
    # (the empty string is still a string) but is exactly what
    # `worker_petition_report_errors` -- the semantic half -- catches.
    raw = json.dumps({
        "verdict": "no_verdict",
        "petition": {
            "obligation": "   ",
            "type": "environmental",
            "evidence": "some evidence",
        },
    })
    attempts_seen: list[int] = []

    def respond(attempt: int, prior_errors: list[str] | None) -> str:
        attempts_seen.append(attempt)
        return raw

    result = ce.enforce_contract(
        "worker", respond, max_retries=2,
        extra_validate=_extra_validate(tmp_path))

    assert not result.ok
    assert result.agent_fault
    assert any("obligation" in e for e in result.errors)
    # The ladder actually re-asked -- max_retries + 1 attempts, not a
    # single silent drop.
    assert attempts_seen == [0, 1, 2]
    # Nothing was ever written: the malformed declaration never reached
    # the write path at all.
    assert petitions.parse_petitions(petitions_file.read_text()) == []


# ---------------------------------------------------------------------------
# A duplicate declaration against an already-filed obligation: refused
# ---------------------------------------------------------------------------


def test_a_duplicate_declaration_for_the_same_obligation_is_refused(tmp_path):
    petitions_file = _make_petitions_file(tmp_path)
    obligation = "Requirement Y: exact same obligation text every time"

    first_errors = petitions.file_worker_petition(
        tmp_path, obligation, "environmental", "the first filing's evidence")
    assert first_errors == []

    raw = json.dumps({
        "verdict": "no_verdict",
        "petition": {
            "obligation": obligation,
            "type": "temporal",
            "evidence": "a second attempt at the same obligation",
        },
    })

    def respond(attempt: int, prior_errors: list[str] | None) -> str:
        return raw

    result = ce.enforce_contract(
        "worker", respond, max_retries=2,
        extra_validate=_extra_validate(tmp_path))

    assert not result.ok
    assert result.agent_fault
    assert any("already" in e for e in result.errors)
    parsed = petitions.parse_petitions(petitions_file.read_text())
    # Still exactly the one, original filing -- the duplicate never landed.
    assert len(parsed) == 1
    assert parsed[0]["type"] == "environmental"
    assert parsed[0]["evidence"] == "the first filing's evidence"
