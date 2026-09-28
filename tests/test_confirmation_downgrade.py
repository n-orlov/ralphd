"""Task 006 (v0.27, PRD requirement B.3, #274): what may block a
CONFIRMATION review is bounded, and the engine enforces it.

On a confirmation review (the approach under review has taken at least
one GRANTED cure pass -- `LoopSupervisor._is_confirmation_review`), every
blocking finding review files must carry one of two optional fields to
stay blocking:

* `reopens` -- the cure task id of a listed finding that is still open;
* `regression: true` -- the failure is caused by a change inside the
  diff range.

A blocking finding carrying NEITHER field is downgraded to `"residual"`
before the verdict is computed (`findings.apply_confirmation_review_
downgrade`, wired into `LoopSupervisor._review_findings_channel`, so the
contract ladder that runs BEFORE `_review_verdict` already sees it), with a
`review.finding_downgraded` event naming it. Two things always stay
blocking with neither field: an exposed secret, and a requirement whose
behaviour is absent altogether -- asserted by review's own explicit,
optional `always_blocking_exception` field (`"exposed_secret"` or
`"absent_behavior"`), never inferred from incidental occurrence, or
omission, of the bare words "secret"/"absent" in the finding's own
`clause`/`evidence` text (`findings.confirmation_review_exempt`;
cure-01-03, PRD B.3, DoD 2).

Every node here is red without this task's change: before it,
`_review_verdict` never downgrades an unlinked blocking finding on a
confirmation review at all -- an unlinked finding simply stays blocking,
and no `review.finding_downgraded` event is ever emitted.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from ralphd.engine import findings
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import atomic_write_json

from tests.conftest import supervisor_factory

_supervisor = supervisor_factory(run_id="confirmation-downgrade",
                                 run_root="run", workspace="workspace")


def _git(workspace: Path, *args) -> None:
    subprocess.run(["git", "-C", str(workspace), *args], check=True,
                   capture_output=True)


def _init_git_repo(workspace: Path) -> None:
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "test")
    (workspace / "a.txt").write_text("one\n")
    _git(workspace, "add", "a.txt")
    _git(workspace, "commit", "-q", "-m", "first")


def _finding(clause: str, evidence: str, **overrides) -> dict:
    payload = {"severity": "blocking", "curable": True, "clause": clause,
               "evidence": evidence, "cured_when": "the fixture cure lands"}
    payload.update(overrides)
    return payload


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    return r


def _rejected() -> IterationResult:
    return _result(json.dumps({
        "verdict": "rejected",
        "reason": "a blocking finding is present",
        "findings_path": "review-findings.md",
    }))


def _make_confirmation_review(tmp_path, prior_finding_clause="prior C.1"):
    """Build a supervisor whose approach 1 has already taken one GRANTED
    cure pass -- `_is_confirmation_review()` is True for it from this
    point on. Returns the supervisor."""
    root = tmp_path / "run"
    sup = _supervisor(root, approach=1)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    _init_git_repo(sup.workspace)
    findings.write_findings_json(
        sup.run.findings_json_file,
        [_finding(prior_finding_clause, "prior evidence")])
    inserted = sup._take_cure_pass(1)
    assert len(inserted) == 1
    assert sup._is_confirmation_review() is True
    return sup, inserted[0]


def _verified() -> IterationResult:
    return _result(json.dumps({"verdict": "verified"}))


def _stub_contract_fix(sup) -> list:
    """Replace `_ask_contract_fix` with a recorder that keeps repeating
    the same `verified` claim, so any re-ask the ladder makes is counted."""
    calls: list = []

    async def fake_ask_contract_fix(hat, errors, **_kw):
        calls.append((hat, list(errors)))
        return json.dumps({"verdict": "verified"}), {}

    sup._ask_contract_fix = fake_ask_contract_fix  # type: ignore[method-assign]
    return calls


def _events(root: Path) -> list[dict]:
    return [json.loads(line) for line in
            (root / "events.jsonl").read_text().splitlines()]


def test_an_unlinked_blocking_finding_is_downgraded_and_the_event_emitted(
        tmp_path):
    sup, _cure_task_id = _make_confirmation_review(tmp_path)
    unlinked = _finding("requirement D.9 (unlinked)", "still observed")
    findings.write_findings_json(sup.run.findings_json_file, [unlinked])

    verified, payload, errors = sup._review_verdict(_rejected())

    assert errors == []
    # the only blocking finding was downgraded to residual -- the
    # rejection is recomputed to verified (findings.compute_verdict),
    # exactly as an all-residual set already does today.
    assert verified is True
    assert payload["verdict"] == "rejected"  # the claim is never rewritten

    downgraded_events = [e for e in _events(sup.run.root)
                         if e["type"] == "review.finding_downgraded"]
    assert len(downgraded_events) == 1
    assert downgraded_events[0]["clause"] == "requirement D.9 (unlinked)"
    assert downgraded_events[0]["evidence"] == "still observed"


def test_a_reopens_finding_stays_blocking(tmp_path):
    sup, cure_task_id = _make_confirmation_review(tmp_path)
    reopened = _finding("prior C.1", "a new example of the same rule",
                         reopens=cure_task_id)
    findings.write_findings_json(sup.run.findings_json_file, [reopened])

    verified, payload, errors = sup._review_verdict(_rejected())

    assert errors == []
    assert verified is False
    downgraded_events = [e for e in _events(sup.run.root)
                         if e["type"] == "review.finding_downgraded"]
    assert downgraded_events == []


def test_a_regression_finding_stays_blocking(tmp_path):
    sup, _cure_task_id = _make_confirmation_review(tmp_path)
    regressed = _finding("requirement D.10", "broken by the diff",
                          regression=True)
    findings.write_findings_json(sup.run.findings_json_file, [regressed])

    verified, payload, errors = sup._review_verdict(_rejected())

    assert errors == []
    assert verified is False
    downgraded_events = [e for e in _events(sup.run.root)
                         if e["type"] == "review.finding_downgraded"]
    assert downgraded_events == []


def test_the_same_unlinked_finding_stays_blocking_on_a_first_review(
        tmp_path):
    """No cure pass has ever been taken for this approach -- this is a
    FIRST review, not a confirmation review -- so the identical unlinked
    finding that the first test downgrades stays blocking here."""
    root = tmp_path / "run"
    sup = _supervisor(root, approach=1)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    _init_git_repo(sup.workspace)
    assert sup._is_confirmation_review() is False
    unlinked = _finding("requirement D.9 (unlinked)", "still observed")
    findings.write_findings_json(sup.run.findings_json_file, [unlinked])

    verified, payload, errors = sup._review_verdict(_rejected())

    assert errors == []
    assert verified is False
    downgraded_events = [e for e in _events(sup.run.root)
                         if e["type"] == "review.finding_downgraded"]
    assert downgraded_events == []


def test_an_exposed_secret_finding_stays_blocking_with_neither_field(
        tmp_path):
    sup, _cure_task_id = _make_confirmation_review(tmp_path)
    secret_finding = _finding(
        "requirement F.1", "a live API secret is printed in the log",
        always_blocking_exception="exposed_secret")
    findings.write_findings_json(sup.run.findings_json_file, [secret_finding])

    verified, payload, errors = sup._review_verdict(_rejected())

    assert errors == []
    assert verified is False
    downgraded_events = [e for e in _events(sup.run.root)
                         if e["type"] == "review.finding_downgraded"]
    assert downgraded_events == []


def test_an_absent_behaviour_finding_stays_blocking_with_neither_field(
        tmp_path):
    sup, _cure_task_id = _make_confirmation_review(tmp_path)
    absent_finding = _finding(
        "requirement F.2", "the named behaviour is absent from the code",
        always_blocking_exception="absent_behavior")
    findings.write_findings_json(sup.run.findings_json_file, [absent_finding])

    verified, payload, errors = sup._review_verdict(_rejected())

    assert errors == []
    assert verified is False
    downgraded_events = [e for e in _events(sup.run.root)
                         if e["type"] == "review.finding_downgraded"]
    assert downgraded_events == []


def test_b3_exceptions_classify_the_finding_not_incidental_words(tmp_path):
    """cure-01-03 (PRD B.3, DoD 2): the always-blocking-exception decision
    is `always_blocking_exception`, an explicit field review asserts --
    never a text match on the bare words "secret"/"absent" incidentally
    present or absent from `clause`/`evidence`. Three findings, none
    carrying `reopens`/`regression`:

    * an ORDINARY, unlinked defect whose evidence happens to use the word
      "absent" for something that is not an altogether-missing behaviour
      (an optional argument being absent) -- downgraded, exactly like any
      other unlinked finding, because the field is not set;
    * a genuine exposed-secret finding phrased with no occurrence of the
      word "secret" at all ("password ... committed in plaintext") --
      stays blocking, because review set the field explicitly;
    * a genuine altogether-absent-behaviour finding phrased with no
      occurrence of the word "absent" at all ("has not been implemented
      anywhere") -- stays blocking, because review set the field
      explicitly.
    """
    sup, _cure_task_id = _make_confirmation_review(tmp_path)
    ordinary = _finding(
        "requirement C.9",
        "The existing tail renderer drops a blank line when the optional "
        "query argument is absent. No code in the diff touches that "
        "renderer.")
    secret_no_word = _finding(
        "requirement F.3",
        "The database password is committed in plaintext to the public "
        "repository.",
        always_blocking_exception="exposed_secret")
    absent_no_word = _finding(
        "requirement F.4",
        "No handler implements PATCH /config/budget; every request "
        "returns 404. The required feature has not been implemented "
        "anywhere.",
        always_blocking_exception="absent_behavior")
    findings.write_findings_json(
        sup.run.findings_json_file,
        [ordinary, secret_no_word, absent_no_word])

    verified, payload, errors = sup._review_verdict(_rejected())

    assert errors == []
    # the two exception findings still block; only the ordinary one was
    # downgraded -- so the set is not empty and the verdict stays False.
    assert verified is False
    downgraded_clauses = {
        e["clause"] for e in _events(sup.run.root)
        if e["type"] == "review.finding_downgraded"}
    assert downgraded_clauses == {"requirement C.9"}


def test_incidental_trigger_words_alone_never_exempt_a_finding(tmp_path):
    """Control for the node above, isolated to `findings.confirmation_
    review_exempt` itself: text containing "secret"/"absent" with no
    `always_blocking_exception` field is not exempt, and the field alone
    -- with no trigger word anywhere in the text -- IS exempt."""
    assert findings.confirmation_review_exempt(
        {"clause": "c", "evidence": "a live API secret leaked"}) is False
    assert findings.confirmation_review_exempt(
        {"clause": "c", "evidence": "the behaviour is absent"}) is False
    assert findings.confirmation_review_exempt(
        {"clause": "c", "evidence": "nothing about either trigger word",
         "always_blocking_exception": "exposed_secret"}) is True
    assert findings.confirmation_review_exempt(
        {"clause": "c", "evidence": "nothing about either trigger word",
         "always_blocking_exception": "absent_behavior"}) is True
    assert findings.confirmation_review_exempt(
        {"clause": "c", "evidence": "e",
         "always_blocking_exception": "not-a-real-value"}) is False

def test_confirmation_review_downgrade_candidates_is_a_pure_unit(tmp_path):
    """Pure unit pin on `findings.apply_confirmation_review_downgrade`
    itself, isolated from `_review_verdict`'s I/O."""
    unlinked = _finding("requirement Z", "observed")
    linked = _finding("requirement Y", "observed", reopens="cure-01-01")
    new_list, downgraded = findings.apply_confirmation_review_downgrade(
        [unlinked, linked], listed_cure_task_ids={"cure-01-01"})
    assert new_list[0]["severity"] == "residual"
    assert new_list[1]["severity"] == "blocking"
    assert downgraded == [new_list[0]]
    # the input list and its dicts are never mutated.
    assert unlinked["severity"] == "blocking"


def test_confirmation_review_downgrade_candidates_default_resolves_nothing(
        tmp_path):
    """cure-01-02-2 (PRD B.3/B.5, DoD 2): a caller that omits
    `listed_cure_task_ids` gets the SAFE default -- every `reopens`
    value is unresolved, so a finding naming one is a downgrade
    candidate exactly like an unlinked finding, never the old
    any-nonblank-string exemption."""
    linked = _finding("requirement Y", "observed", reopens="cure-01-01")
    new_list, downgraded = findings.apply_confirmation_review_downgrade(
        [linked])
    assert new_list[0]["severity"] == "residual"
    assert downgraded == [new_list[0]]


def test_b3_reopens_must_name_a_listed_cure_task(tmp_path):
    """cure-01-02-2 (PRD B.3/B.5, DoD 2): the previous granted pass lists
    only `cure_task_id` (the id `_make_confirmation_review` inserted).
    Otherwise-unlinked, non-regression findings whose `reopens` names an
    unrelated string or an unresolved cure task id must NOT bypass the
    confirmation scope by retaining blocking severity -- both are
    downgraded to residual and the event fires, exactly as an unlinked
    finding is, through the live `_review_verdict` path."""
    sup, cure_task_id = _make_confirmation_review(tmp_path)
    assert cure_task_id != "not-a-cure-task"
    assert cure_task_id != "cure-99-99"
    unrelated = _finding("requirement D.11", "looks similar",
                          reopens="not-a-cure-task")
    unresolved = _finding("requirement D.12", "looks similar too",
                           reopens="cure-99-99")
    findings.write_findings_json(
        sup.run.findings_json_file, [unrelated, unresolved])

    verified, payload, errors = sup._review_verdict(_rejected())

    assert errors == []
    # both blocking findings downgraded to residual -- verdict recomputed
    # to verified even though the raw payload claimed "rejected".
    assert verified is True
    assert payload["verdict"] == "rejected"

    downgraded_clauses = {
        e["clause"] for e in _events(sup.run.root)
        if e["type"] == "review.finding_downgraded"}
    assert downgraded_clauses == {"requirement D.11", "requirement D.12"}


def test_b3_confirmation_review_listed_cure_task_ids_reads_the_lineage(
        tmp_path):
    """cure-01-02-2 (PRD B.3/B.5, DoD 2): `LoopSupervisor._confirmation_
    review_listed_cure_task_ids` reports exactly the cure task ids the
    current confirmation section lists -- the previous granted pass's own
    inserted cure task id -- and nothing else, matching what `_cure_pass_
    targeted_roots` and `_confirmation_review_context` already key on."""
    sup, cure_task_id = _make_confirmation_review(tmp_path)
    assert sup._confirmation_review_listed_cure_task_ids() == {cure_task_id}

    root = tmp_path / "run-no-approach"
    sup2 = _supervisor(root, approach=None)
    assert sup2._confirmation_review_listed_cure_task_ids() == frozenset()


def test_a_verified_claim_over_an_unlinked_finding_passes_the_ladder(
        tmp_path):
    """The live review path: `_review_contract_ladder` runs strictly BEFORE
    `_review_verdict`. On a confirmation review, a `{"verdict": "verified"}`
    reply beside an unlinked blocking finding is NOT a contradiction --
    the rule downgrades the finding before the ladder checks it -- so the
    ladder makes no re-ask, flags nothing unusable, and `_review_verdict`
    computes `verified` with exactly one `review.finding_downgraded`."""
    sup, _cure_task_id = _make_confirmation_review(tmp_path)
    sup.cfg.contract_retry_max = 1
    unlinked = _finding("requirement D.9 (unlinked)", "still observed")
    findings.write_findings_json(sup.run.findings_json_file, [unlinked])
    calls = _stub_contract_fix(sup)
    result = _verified()

    laddered = asyncio.run(sup._review_contract_ladder(result))

    assert calls == []
    assert laddered is result
    assert sup._review_findings_unusable is False
    assert sup._review_contract_unrepaired is False
    verified, payload, errors = sup._review_verdict(laddered)
    assert errors == []
    assert verified is True
    assert sup._review_success_verdict_label() == "verified-with-residuals"
    downgraded_events = [e for e in _events(sup.run.root)
                         if e["type"] == "review.finding_downgraded"]
    assert len(downgraded_events) == 1
    assert downgraded_events[0]["clause"] == "requirement D.9 (unlinked)"


def test_a_verified_claim_over_a_reopens_finding_is_still_a_contradiction(
        tmp_path):
    """Control for the node above: a `reopens` finding stays blocking, so
    the same `verified` reply IS a contradiction the ladder re-asks over
    and, unrepaired, never earns `verified`."""
    sup, cure_task_id = _make_confirmation_review(tmp_path)
    sup.cfg.contract_retry_max = 1
    reopened = _finding("prior C.1", "a new example of the same rule",
                         reopens=cure_task_id)
    findings.write_findings_json(sup.run.findings_json_file, [reopened])
    calls = _stub_contract_fix(sup)

    laddered = asyncio.run(sup._review_contract_ladder(_verified()))

    assert len(calls) == 1
    assert sup._review_findings_unusable is True
    verified, _payload, errors = sup._review_verdict(laddered)
    assert verified is False
    assert errors
    assert [e for e in _events(sup.run.root)
            if e["type"] == "review.finding_downgraded"] == []


def test_a_verified_claim_over_an_unlinked_finding_on_a_first_review_is_refused(
        tmp_path):
    """Control: with no granted cure pass the identical reply over the
    identical unlinked finding stays a contradiction on the ladder."""
    root = tmp_path / "run"
    sup = _supervisor(root, approach=1)
    sup.cfg.contract_retry_max = 1
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    _init_git_repo(sup.workspace)
    findings.write_findings_json(
        sup.run.findings_json_file,
        [_finding("requirement D.9 (unlinked)", "still observed")])
    calls = _stub_contract_fix(sup)

    laddered = asyncio.run(sup._review_contract_ladder(_verified()))

    assert len(calls) == 1
    verified, _payload, _errors = sup._review_verdict(laddered)
    assert verified is False


def test_a_mid_run_pass_never_downgrades(tmp_path):
    """The mid-run adjudication episode / mid-plan review (`mid_run=True`)
    is not a confirmation review: its findings keep their severities even
    once the approach has taken a cure pass."""
    sup, _cure_task_id = _make_confirmation_review(tmp_path)
    findings.write_findings_json(
        sup.run.findings_json_file,
        [_finding("requirement D.9 (unlinked)", "still observed")])

    listed, errors = sup._review_findings_channel(mid_run=True)

    assert errors == []
    assert [f["severity"] for f in listed] == ["blocking"]
    assert sup._review_findings(mid_run=True)[0]["severity"] == "blocking"
    assert [e for e in _events(sup.run.root)
            if e["type"] == "review.finding_downgraded"] == []


def test_the_event_is_emitted_once_per_review_pass_across_rereads(tmp_path):
    """Every reader re-reads the channel; the event names each downgraded
    finding once per pass, and a new pass (a fresh ladder entry) may
    announce it again."""
    sup, _cure_task_id = _make_confirmation_review(tmp_path)
    findings.write_findings_json(
        sup.run.findings_json_file,
        [_finding("requirement D.9 (unlinked)", "still observed")])

    asyncio.run(sup._review_contract_ladder(_verified()))
    sup._review_verdict(_verified())
    sup._review_findings_channel()
    sup._review_findings()

    def count():
        return len([e for e in _events(sup.run.root)
                    if e["type"] == "review.finding_downgraded"])

    assert count() == 1
    asyncio.run(sup._review_contract_ladder(_verified()))
    assert count() == 2
