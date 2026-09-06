"""Task 011 (requirement C.3/C.4, #112): CONFIRMED petition adjudications
persist at RUN scope and survive `LoopSupervisor._archive_approach` --
approach N+1 inherits every CONFIRMED adjudication, and the obligation it
names is removed from the blocking set (a CONFIRMED finding is downgraded
to `severity: "residual"`, never dropped).

`engine.petitions` gains the write/read/digest layer
(`record_confirmed_adjudications`/`read_confirmed_adjudications`/
`confirmed_obligations`/`downgrade_confirmed_findings`/
`tamper_review_context`); `RunDir.petition_adjudications_file` is the new
run-scope path (`run.root/petition-adjudications.json`, deliberately NOT
one of the three paths `_archive_approach` copies into `approaches/NN/`);
`LoopSupervisor._review_findings_channel` applies the downgrade for every
caller (`_review_verdict`, `_review_contract_ladder`,
`_review_incurable_rejection`) in one place, and `_review_verdict` itself
persists CONFIRMED entries as a side effect of parsing review's own
contract payload -- never from a worker or review hat's own process, which
is the tamper-evidence property: only `LoopSupervisor`/`engine.petitions`
ever write the file. A worker-phase hand-edit is detected by
`confirmed_obligations`'s digest check and surfaced to review's own prompt
(`_petition_adjudications_tamper_context`) as a mandatory BLOCKING finding.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ralphd.engine import findings, petitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, read_json
from tests.conftest import supervisor_factory


def _finding(severity: str, **overrides) -> dict:
    payload = {
        "severity": severity,
        "curable": True,
        "clause": "Requirement B, DoD predicate 9",
        "evidence": "fixture evidence",
    }
    payload.update(overrides)
    return payload


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    return r


# The shared harness (tests/conftest.py, G.1); `status` is the resumed-run
# shape: the state is on disk before the supervisor exists.
_supervisor = supervisor_factory(run_id="task011-petition-adjudications",
                                status={"state": "starting"})



def test_record_confirmed_adjudications_writes_the_run_scope_file(tmp_path):
    run = RunDir(root=tmp_path / "run")
    petitions.record_confirmed_adjudications(run, [
        {"obligation": "Requirement B, DoD predicate 9",
         "adjudication": "CONFIRMED"},
    ])
    assert run.petition_adjudications_file.exists()
    assert run.petition_adjudications_file.parent == run.root
    entries, tampered = petitions.read_confirmed_adjudications(run)
    assert tampered is False
    assert entries == [{"obligation": "Requirement B, DoD predicate 9",
                         "adjudication": "CONFIRMED"}]


def test_refuted_entries_are_persisted_but_never_exempt(tmp_path):
    """Task 016 (approach 02, review finding 2) changed the product here:
    `record_confirmed_adjudications` now persists REFUTED entries too --
    same store, same keyed MAC -- because that store is what
    `run_record.build_petitions_section` renders, and a REFUTED
    adjudication and its remedy were otherwise lost to the run record.
    What must NOT change (and is pinned here) is the EXEMPTION rule: the
    exemption readers (`read_confirmed_adjudications`,
    `confirmed_obligations`) still see CONFIRMED entries only, so a
    REFUTED entry in the store exempts nothing. This test previously
    asserted the file was never written at all; that expectation was the
    render bug, not the property."""
    run = RunDir(root=tmp_path / "run")
    petitions.record_confirmed_adjudications(run, [
        {"obligation": "Some other requirement", "adjudication": "REFUTED",
         "remedy": "do X instead"},
    ])
    assert run.petition_adjudications_file.exists()
    stored, tampered = petitions.read_adjudications(run)
    assert tampered is False
    assert stored == [{"obligation": "Some other requirement",
                       "adjudication": "REFUTED", "remedy": "do X instead"}]
    entries, tampered = petitions.read_confirmed_adjudications(run)
    assert entries == []
    assert tampered is False
    obligations, tampered = petitions.confirmed_obligations(run)
    assert obligations == set()
    assert tampered is False


def test_record_merges_rather_than_overwrites_across_calls(tmp_path):
    run = RunDir(root=tmp_path / "run")
    petitions.record_confirmed_adjudications(
        run, [{"obligation": "Obligation One", "adjudication": "CONFIRMED"}])
    petitions.record_confirmed_adjudications(
        run, [{"obligation": "Obligation Two", "adjudication": "CONFIRMED"}])
    obligations, tampered = petitions.confirmed_obligations(run)
    assert tampered is False
    assert obligations == {"obligation one", "obligation two"}


def test_downgrade_confirmed_findings_converts_blocking_to_residual():
    findings_list = [
        _finding("blocking", clause="Requirement B, DoD predicate 9"),
        _finding("blocking", clause="An unrelated clause", curable=False),
    ]
    confirmed = {"requirement b, dod predicate 9"}
    out = petitions.downgrade_confirmed_findings(findings_list, confirmed)
    # never mutates the input list or its dicts.
    assert findings_list[0]["severity"] == "blocking"
    assert out[0]["severity"] == "residual"
    assert out[1]["severity"] == "blocking"
    assert findings.compute_verdict(out) == "rejected"  # the OTHER finding still blocks


def test_downgrade_with_no_confirmed_obligations_is_a_no_op_copy():
    findings_list = [_finding("blocking")]
    out = petitions.downgrade_confirmed_findings(findings_list, set())
    assert out == findings_list
    assert out is not findings_list


# ---------------------------------------------------------------------------
# Survives `_archive_approach`: approach N+1 inherits every CONFIRMED
# adjudication (requirement C.3).
# ---------------------------------------------------------------------------

def test_confirmed_adjudication_survives_archive_approach(tmp_path):
    sup = _supervisor(tmp_path / "run")
    petitions.record_confirmed_adjudications(sup.run, [
        {"obligation": "Requirement B, DoD predicate 9",
         "adjudication": "CONFIRMED"},
    ])
    # populate the three files `_archive_approach` DOES move, to prove the
    # adjudication file is untouched by the same call that resets those.
    sup.run.tasks_file.write_text('{"tasks": []}')
    sup.run.notes_file.write_text("approach 1 notes\n")
    sup.run.findings_file.write_text("approach 1 findings\n")

    sup._archive_approach(1)

    assert (sup.run.root / "approaches" / "01" / "tasks.json").exists()
    # the adjudication record was never archived away -- it never lived
    # under approaches/NN/ in the first place.
    assert not (sup.run.root / "approaches" / "01" /
                "petition-adjudications.json").exists()
    obligations, tampered = petitions.confirmed_obligations(sup.run)
    assert tampered is False
    assert obligations == {"requirement b, dod predicate 9"}


def test_approach_n_plus_1_review_no_longer_sees_the_obligation_as_blocking(
        tmp_path):
    """The end-to-end proof requirement C.4 asks for: approach 1's review
    CONFIRMS a petition against an obligation; `_archive_approach` runs (the
    ordinary rejection path); approach 2's review, seeing the SAME clause
    as a fresh blocking finding, has it downgraded to residual because the
    run-scope record survived -- the obligation is out of the blocking set
    without review re-litigating it."""
    sup = _supervisor(tmp_path / "run")

    # Approach 1: review confirms the petition (verdict payload carries
    # `petition_adjudications`), rejecting on the SAME clause it just
    # confirmed is unachievable (an overcautious hat, or one that reports
    # the finding and the adjudication in the same pass).
    findings.write_findings_json(sup.run.findings_json_file, [
        _finding("blocking", clause="Requirement B, DoD predicate 9",
                  curable=False),
    ])
    result_1 = _result(json.dumps({
        "verdict": "rejected",
        "reason": "the obligation cannot be met",
        "findings_path": "review-findings.md",
        "petition_adjudications": [
            {"obligation": "Requirement B, DoD predicate 9",
             "adjudication": "CONFIRMED"},
        ],
    }))
    verified_1, payload_1, errors_1 = sup._review_verdict(result_1)
    assert errors_1 == []
    assert payload_1["petition_adjudications"][0]["adjudication"] == "CONFIRMED"

    # the CONFIRMED entry landed at run scope, and the SAME review's own
    # findings channel already reads it back downgraded -- the blocking
    # set that fed `verified_1` was already residual-only, so this
    # particular rejection self-corrects to verified (predicate 3's own
    # honest-recomputation rule); the interaction this test actually pins
    # is the NEXT approach's independent findings channel below.
    assert verified_1 is True

    sup.run.notes_file.write_text("approach 1 notes\n")
    sup.run.findings_file.write_text("approach 1 findings\n")
    sup._archive_approach(1)

    # Approach 2: a FRESH review, on a fresh findings channel, reports the
    # SAME clause as blocking again (it never re-litigates the petition --
    # it doesn't even know one was ever filed). The run-scope record from
    # approach 1 is what removes it from the blocking set this time.
    findings.write_findings_json(sup.run.findings_json_file, [
        _finding("blocking", clause="Requirement B, DoD predicate 9",
                  curable=False),
        _finding("blocking", clause="A second, unrelated finding",
                  curable=False),
    ])
    result_2 = _result(json.dumps({
        "verdict": "rejected",
        "reason": "two findings this time",
        "findings_path": "review-findings.md",
    }))
    verified_2, payload_2, errors_2 = sup._review_verdict(result_2)
    assert errors_2 == []
    # still rejected -- the SECOND, unrelated finding is still blocking --
    # but the confirmed obligation itself is out of the blocking set: this
    # is what `_review_findings_channel` (the one place the downgrade
    # happens) proves.
    assert verified_2 is False
    live_findings, live_errors = sup._review_findings_channel()
    assert live_errors == []
    by_clause = {f["clause"]: f["severity"] for f in live_findings}
    assert by_clause["Requirement B, DoD predicate 9"] == "residual"
    assert by_clause["A second, unrelated finding"] == "blocking"


# ---------------------------------------------------------------------------
# Tamper-evidence against the worker (requirement C.3): review writes the
# record (via `LoopSupervisor`/`engine.petitions` only); a worker-phase
# hand-edit is detectable and surfaced as a mandatory blocking finding.
# ---------------------------------------------------------------------------

def test_a_worker_phase_edit_is_detected_as_tampered(tmp_path):
    run = RunDir(root=tmp_path / "run")
    petitions.record_confirmed_adjudications(run, [
        {"obligation": "Requirement B, DoD predicate 9",
         "adjudication": "CONFIRMED"},
    ])
    # simulate a worker-phase hand-edit: add a fabricated CONFIRMED entry
    # directly on disk, WITHOUT going through `record_confirmed_
    # adjudications` (the one function that keeps entries+digest
    # consistent) -- exactly what a worker iteration with filesystem
    # access to the run dir could do.
    doc = read_json(run.petition_adjudications_file)
    doc["entries"].append({"obligation": "A fabricated exemption",
                            "adjudication": "CONFIRMED"})
    run.petition_adjudications_file.write_text(json.dumps(doc))

    obligations, tampered = petitions.confirmed_obligations(run)
    assert tampered is True
    # fails towards the all-blocking default: nothing is trusted as
    # confirmed once tampering is detected, not even the genuine entry.
    assert obligations == set()


def test_tamper_review_context_demands_a_blocking_finding():
    assert petitions.tamper_review_context(False) == ""
    ctx = petitions.tamper_review_context(True)
    assert "tampered" in ctx.lower()
    assert "BLOCKING" in ctx


def test_review_extra_context_surfaces_tamper_to_the_prompt(tmp_path):
    sup = _supervisor(tmp_path / "run")
    petitions.record_confirmed_adjudications(sup.run, [
        {"obligation": "Requirement B, DoD predicate 9",
         "adjudication": "CONFIRMED"},
    ])
    doc = read_json(sup.run.petition_adjudications_file)
    doc["digest"] = "not-the-real-digest"
    sup.run.petition_adjudications_file.write_text(json.dumps(doc))

    ctx = sup._review_extra_context()
    assert "tampered" in ctx.lower()
    assert "BLOCKING" in ctx

    prompt = sup.build_prompt("review", extra=ctx)
    assert "tampered" in prompt.lower()


def test_untampered_run_has_no_tamper_context(tmp_path):
    sup = _supervisor(tmp_path / "run")
    assert sup._petition_adjudications_tamper_context() == ""
    petitions.record_confirmed_adjudications(sup.run, [
        {"obligation": "Requirement B, DoD predicate 9",
         "adjudication": "CONFIRMED"},
    ])
    assert sup._petition_adjudications_tamper_context() == ""


# ---------------------------------------------------------------------------
# `_review_verdict` persists CONFIRMED entries as a side effect (never a
# worker/review hat process writing the file directly).
# ---------------------------------------------------------------------------

def test_review_verdict_persists_confirmed_petition_adjudications(tmp_path):
    sup = _supervisor(tmp_path / "run")
    result = _result(json.dumps({
        "verdict": "verified",
        "petition_adjudications": [
            {"obligation": "Requirement B, DoD predicate 9",
             "adjudication": "CONFIRMED"},
            {"obligation": "Some other requirement",
             "adjudication": "REFUTED", "remedy": "do X"},
        ],
    }))
    sup._review_verdict(result)
    obligations, tampered = petitions.confirmed_obligations(sup.run)
    assert tampered is False
    assert obligations == {"requirement b, dod predicate 9"}


def test_review_verdict_with_no_petition_adjudications_writes_nothing(tmp_path):
    sup = _supervisor(tmp_path / "run")
    result = _result(json.dumps({"verdict": "verified"}))
    sup._review_verdict(result)
    assert not sup.run.petition_adjudications_file.exists()


# ---------------------------------------------------------------------------
# Tamper-evidence, hardened (task 011 attempt 2): an UNKEYED digest stored
# in the same worker-writable file proves nothing -- a worker that edits the
# entries and recomputes the documented digest defeats it (this is exactly
# the probe that rejected attempt 1). The property now rests on three
# independent artifacts: a root-owned write (`privsep.write_root_owned_json`,
# unwritable by a hat wherever the uid boundary exists), a KEYED HMAC over a
# per-run secret the record does not contain, and an append-only
# `events.jsonl` witness line per write, with any detection itself recorded
# and sticky for the rest of the run.
# ---------------------------------------------------------------------------

def _recorded(run):
    """Every CONFIRMED entry the record currently holds, straight off disk,
    without going through the verification layer."""
    return read_json(run.petition_adjudications_file)["entries"]


def _confirm(run, obligation="Requirement B, DoD predicate 9"):
    petitions.record_confirmed_adjudications(
        run, [{"obligation": obligation, "adjudication": "CONFIRMED"}])


def test_recomputing_the_documented_unkeyed_digest_is_still_detected(tmp_path):
    """The probe that rejected attempt 1, verbatim in shape: append a
    fabricated CONFIRMED obligation, recompute the unkeyed SHA-256 digest
    with the module's own documented canonical-JSON algorithm, and the
    record must STILL read as tampered -- the fabricated exemption must not
    reach the blocking set."""
    run = RunDir(root=tmp_path / "run")
    _confirm(run)
    doc = read_json(run.petition_adjudications_file)
    doc["entries"].append({"obligation": "A fabricated exemption",
                            "adjudication": "CONFIRMED"})
    doc["digest"] = petitions._adjudications_digest(doc["entries"])
    run.petition_adjudications_file.write_text(json.dumps(doc))

    obligations, tampered = petitions.confirmed_obligations(run)
    assert tampered is True
    assert obligations == set()
    still_blocking = petitions.downgrade_confirmed_findings(
        [_finding("blocking", clause="A fabricated exemption", curable=False)],
        obligations)
    assert still_blocking[0]["severity"] == "blocking"


def test_a_forger_holding_the_key_still_fails_the_in_memory_witness(tmp_path):
    """Hand a forger the key itself -- the one thing a real hat process
    cannot get, since the key exists only in the engine process's memory --
    and let it also rewrite the agent-writable `events.jsonl` witness (the
    exact combination that defeated attempt 2). The engine's OWN in-memory
    memory of the last MAC it wrote, which lives in no file and so cannot
    be rewritten from outside the process, still catches it."""
    from ralphd.engine.state import read_events

    run = RunDir(root=tmp_path / "run")
    _confirm(run)
    key = petitions._authority(run).key  # a real hat has no way to do this
    entries = _recorded(run) + [{"obligation": "A fabricated exemption",
                                  "adjudication": "CONFIRMED"}]
    forged_mac = petitions._adjudications_mac(entries, key)
    run.petition_adjudications_file.write_text(json.dumps({
        "entries": entries,
        "digest": petitions._adjudications_digest(entries),
        "mac": forged_mac,
    }))
    # ... and fabricate the engine witness line for it, appending to the
    # agent-writable event log exactly as the attempt-2 probe did.
    with (run.root / "events.jsonl").open("a") as fh:
        fh.write(json.dumps({"ts": "2026-08-31T00:00:00Z",
                              "type": petitions.ADJUDICATIONS_RECORDED_EVENT,
                              "mac": forged_mac, "entries": len(entries)}) + "\n")
    assert [ev["mac"] for ev in read_events(
        run.root, (petitions.ADJUDICATIONS_RECORDED_EVENT,))][-1] == forged_mac

    obligations, tampered = petitions.confirmed_obligations(run)
    assert tampered is True
    assert obligations == set()


def test_no_file_under_the_run_dir_carries_the_key(tmp_path):
    """The property attempt 2 lacked: there is nothing under the (agent-
    OWNED) run directory for a hat process to read the key out of -- not a
    dedicated key file, not the record, not the event log. Searched
    byte-wise across every file in the tree, so a future key file added
    under any name fails this test."""
    run = RunDir(root=tmp_path / "run")
    _confirm(run, "Obligation One")
    _confirm(run, "Obligation Two")
    key = petitions._authority(run).key
    for path in sorted(run.root.rglob("*")):
        if not path.is_file():
            continue
        blob = path.read_bytes()
        assert key not in blob, path
        assert key.hex().encode("ascii") not in blob, path


def test_a_separate_process_cannot_forge_an_exemption(tmp_path):
    """The threat model as it actually stands: a worker iteration is a
    SEPARATE PROCESS from the engine (`runner.py` spawns the agent CLI), so
    it can write every file in the run dir but cannot read the engine's
    address space. This test runs the attempt-2 probe's full program in a
    genuine subprocess -- edit the record, hunt the run dir for key
    material, recompute both the documented unkeyed digest and a keyed MAC
    under every candidate key it can find, append a matching
    `petition_adjudications_recorded` witness line -- and the engine
    process then reads the result as TAMPERED with no exemption granted."""
    import subprocess
    import sys

    run = RunDir(root=tmp_path / "run")
    _confirm(run)
    probe = tmp_path / "forge.py"
    probe.write_text(
        "import hashlib, hmac, json, sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, %r)\n"
        "from ralphd.engine import petitions\n"
        "root = Path(sys.argv[1])\n"
        "record = root / 'petition-adjudications.json'\n"
        "doc = json.loads(record.read_text())\n"
        "doc['entries'].append({'obligation': 'A fabricated exemption',\n"
        "                       'adjudication': 'CONFIRMED'})\n"
        "entries = doc['entries']\n"
        "doc['digest'] = petitions._adjudications_digest(entries)\n"
        "candidates = []\n"
        "for path in sorted(root.rglob('*')):\n"
        "    if path.is_file():\n"
        "        blob = path.read_bytes()\n"
        "        candidates += [blob, blob.strip()]\n"
        "macs = [petitions._adjudications_mac(entries, c) for c in candidates]\n"
        "doc['mac'] = macs[0] if macs else doc['digest']\n"
        "record.write_text(json.dumps(doc))\n"
        "with (root / 'events.jsonl').open('a') as fh:\n"
        "    fh.write(json.dumps({'ts': '2026-08-31T00:00:00Z',\n"
        "                         'type': petitions.ADJUDICATIONS_RECORDED_EVENT,\n"
        "                         'mac': doc['mac'],\n"
        "                         'entries': len(entries)}) + '\\n')\n"
        "print('forged', len(candidates))\n"
        % str(Path(petitions.__file__).parents[3]))
    proc = subprocess.run([sys.executable, str(probe), str(run.root)],
                           capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("forged")
    assert {"obligation": "A fabricated exemption",
            "adjudication": "CONFIRMED"} in _recorded(run)  # the edit landed

    obligations, tampered = petitions.confirmed_obligations(run)
    assert tampered is True
    assert obligations == set()


def test_a_record_the_engine_never_keyed_grants_no_exemption(tmp_path):
    """A record fabricated from scratch in a run where the engine never
    wrote one: this process holds no key for the run, so the record cannot
    be authenticated at all -- it is read as EMPTY (fail closed, no
    obligation exempted) and the reason is recorded in the event log."""
    from ralphd.engine.state import read_events

    run = RunDir(root=tmp_path / "run")
    run.root.mkdir(parents=True, exist_ok=True)
    entries = [{"obligation": "A fabricated exemption",
                 "adjudication": "CONFIRMED"}]
    run.petition_adjudications_file.write_text(json.dumps({
        "entries": entries,
        "digest": petitions._adjudications_digest(entries),
        "mac": petitions._adjudications_digest(entries),
    }))
    obligations, tampered = petitions.confirmed_obligations(run)
    assert obligations == set()
    assert tampered is False  # unauthenticatable, not provably edited
    assert read_events(run.root,
                       (petitions.ADJUDICATIONS_UNAUTHENTICATED_EVENT,))


def test_deleting_the_record_after_a_witnessed_write_is_detected(tmp_path):
    run = RunDir(root=tmp_path / "run")
    _confirm(run)
    run.petition_adjudications_file.unlink()
    obligations, tampered = petitions.confirmed_obligations(run)
    assert tampered is True
    assert obligations == set()


def test_a_later_episode_with_no_key_store_reads_the_record_as_empty(tmp_path):
    """The documented no-uid-boundary degrade: with no `key_store_dir()`
    the key is memory-only, so an engine RESTART (modelled honestly by
    `forget_run_authority`, which is exactly what a new process starts
    with) can no longer authenticate the earlier episode's record. It fails
    CLOSED -- the confirmed set is empty, the obligation goes back to
    blocking -- and that is not called tampering."""
    run = RunDir(root=tmp_path / "run")
    _confirm(run)
    assert petitions.key_store_dir() is None  # no boundary, no override
    petitions.forget_run_authority(run)
    obligations, tampered = petitions.confirmed_obligations(run)
    assert obligations == set()
    assert tampered is False


def test_a_later_episode_with_a_key_store_still_authenticates(tmp_path,
                                                              monkeypatch):
    """With a key store OUTSIDE the run dir (`$RALPHD_RUN_SECRETS_DIR`, or
    the root-owned default wherever the uid boundary exists), a record
    written by an earlier engine episode still authenticates after a
    restart -- and the store's key file is 0600 inside a 0700 directory,
    with the run dir itself still carrying no key material."""
    import stat

    store = tmp_path / "secrets"
    monkeypatch.setenv(petitions.RUN_SECRETS_DIR_ENV, str(store))
    run = RunDir(root=tmp_path / "run")
    _confirm(run)
    keys = list(store.glob("*.key"))
    assert len(keys) == 1
    assert stat.S_IMODE(keys[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(store.stat().st_mode) == 0o700
    assert store not in run.root.parents and not str(store).startswith(
        str(run.root))

    petitions.forget_run_authority(run)  # the restart
    obligations, tampered = petitions.confirmed_obligations(run)
    assert tampered is False
    assert obligations == {"requirement b, dod predicate 9"}


def test_tamper_detection_is_recorded_in_the_event_log_and_sticky(tmp_path):
    """Detection is evidence, and it is not reversible: restoring the file
    byte-for-byte, or a later legitimate review write, does not buy back
    trust in the record for the rest of the run."""
    from ralphd.engine.state import read_events

    run = RunDir(root=tmp_path / "run")
    _confirm(run)
    good = run.petition_adjudications_file.read_text()
    doc = json.loads(good)
    doc["entries"].append({"obligation": "A fabricated exemption",
                            "adjudication": "CONFIRMED"})
    doc["digest"] = petitions._adjudications_digest(doc["entries"])
    run.petition_adjudications_file.write_text(json.dumps(doc))

    assert petitions.confirmed_obligations(run)[1] is True
    detections = read_events(run.root, (petitions.ADJUDICATIONS_TAMPERED_EVENT,))
    assert len(detections) == 1
    assert detections[0]["reason"]

    run.petition_adjudications_file.write_text(good)  # "put it back"
    assert petitions.confirmed_obligations(run) == (set(), True)
    _confirm(run, "Some later, genuine obligation")   # a real review write
    obligations, tampered = petitions.confirmed_obligations(run)
    assert tampered is True
    assert obligations == set()
    # and the detection was recorded once, not once per read.
    assert len(read_events(
        run.root, (petitions.ADJUDICATIONS_TAMPERED_EVENT,))) == 1


def test_every_write_is_witnessed_in_the_event_log(tmp_path):
    from ralphd.engine.state import read_events

    run = RunDir(root=tmp_path / "run")
    _confirm(run, "Obligation One")
    _confirm(run, "Obligation Two")
    macs = [ev["mac"] for ev in
            read_events(run.root, (petitions.ADJUDICATIONS_RECORDED_EVENT,))]
    assert len(macs) == 2
    assert macs[-1] == read_json(run.petition_adjudications_file)["mac"]
    assert petitions.confirmed_obligations(run) == (
        {"obligation one", "obligation two"}, False)


def test_the_record_write_goes_through_the_root_owned_write_path(tmp_path,
                                                                 monkeypatch):
    """Layer 1 is load-bearing: the record is written by
    `privsep.write_root_owned_json` (root-owned, unwritable by the agent uid
    wherever `privsep.separated()`), not by a plain agent-owned write."""
    from ralphd.engine import privsep

    seen = []
    real = privsep.write_root_owned_json

    def spy(path, obj):
        seen.append(Path(path))
        return real(path, obj)

    monkeypatch.setattr(privsep, "write_root_owned_json", spy)
    run = RunDir(root=tmp_path / "run")
    _confirm(run)
    assert seen == [run.petition_adjudications_file]


def test_the_key_store_is_never_archived(tmp_path, monkeypatch):
    store = tmp_path / "secrets"
    monkeypatch.setenv(petitions.RUN_SECRETS_DIR_ENV, str(store))
    sup = _supervisor(tmp_path / "run")
    _confirm(sup.run)
    sup._archive_approach(1)
    archived = sup.run.root / "approaches" / "01"
    assert archived.is_dir()
    assert not list(archived.rglob("*.key"))
    assert not list(sup.run.root.rglob("*.key"))  # the store is outside it
    assert petitions.confirmed_obligations(sup.run)[1] is False


def test_review_sees_a_digest_recomputing_edit_as_a_blocking_finding(tmp_path):
    """End to end through the supervisor: the same probe that defeated
    attempt 1, run against a live review pass -- the fabricated obligation
    stays BLOCKING in review's findings channel, and review's own prompt
    carries the mandatory blocking-finding instruction."""
    sup = _supervisor(tmp_path / "run")
    _confirm(sup.run)
    doc = read_json(sup.run.petition_adjudications_file)
    doc["entries"].append({"obligation": "A fabricated exemption",
                            "adjudication": "CONFIRMED"})
    doc["digest"] = petitions._adjudications_digest(doc["entries"])
    sup.run.petition_adjudications_file.write_text(json.dumps(doc))

    findings.write_findings_json(sup.run.findings_json_file, [
        _finding("blocking", clause="A fabricated exemption", curable=False),
        _finding("blocking", clause="Requirement B, DoD predicate 9",
                  curable=False),
    ])
    live_findings, errors = sup._review_findings_channel()
    assert errors == []
    assert {f["clause"]: f["severity"] for f in live_findings} == {
        "A fabricated exemption": "blocking",
        "Requirement B, DoD predicate 9": "blocking",
    }
    assert findings.compute_verdict(live_findings) == "rejected"

    ctx = sup._review_extra_context()
    assert "tampered" in ctx.lower()
    assert "BLOCKING" in ctx
    assert "tampered" in sup.build_prompt("review", extra=ctx).lower()
