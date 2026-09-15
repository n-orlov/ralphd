"""Flow 5 of the twelve (task 005, requirement G.5): **petition ->
adjudication REFUTED/CONFIRMED -> history**, over a real `ralphd-engine`
subprocess with the stub `pi` binary on PATH.

The path, end to end, with the engine doing every write:

1. a worker iteration DECLARES a structured impossibility petition on the
   contract's `petition` field alongside its ordinary `verdict: "complete"`
   (`STUB_WORKER_PETITION`) -- the worker never writes the filing itself;
2. the engine turns those three declared strings into a tracked filing in
   the RUN dir's own `petitions.md` (task 065, requirement N.6, #150) --
   never a workspace file under `artifacts/reports/` -- in `docs/petition-
   format.md`'s field shape;
3. review adjudicates the obligation through its own optional
   `petition_adjudications` field (`STUB_REVIEW_ADJUDICATIONS`) -- CONFIRMED,
   or REFUTED with a remedy;
4. the engine records the ruling at run scope in `petition-adjudications.json`
   under its keyed MAC, emits the `petition_adjudications_recorded` witness to
   `events.jsonl`, and writes the ruling BACK into the petition's own section
   of that SAME run-dir `petitions.md`, so a reader of it sees the outcome
   without cross-referencing anything -- the workspace tree gains nothing at
   any step (requirement N.6's own claim, proved directly below over a real
   `ralphd-engine` subprocess -- an installed clone, never this run's own
   `/run/ralphd`).

Both rulings are separate tests because the two are not symmetrical: a
CONFIRMED obligation is a run-scope exemption a later approach inherits, while
a REFUTED one is history plus a remedy and exempts nothing (the run-scope
record still carries it, so `run-record` can render what review decided).
Neither ruling is invented here: the ADJUDICATION comes from the review hat's
own payload, and every assertion below reads what the ENGINE wrote.
"""

from __future__ import annotations

import json

from tests.flows.flowlib import (assert_step_sequence, events_of, flow_steps,
                                 status)

OBLIGATION = "requirement Z.9: mint a token from the sealed vault"
PETITION = {
    "obligation": OBLIGATION,
    "type": "environmental",
    "evidence": "no vault socket exists in this sandbox; /run/vault is absent",
}
REMEDY = "mock the vault client and assert the call shape instead"


def _petition_run(engine_factory, run_id: str, adjudications: list[dict]):
    return engine_factory(
        job={"run_id": run_id, "on_complete": "exit", "max_approaches": 1,
             "iterations": 30},
        stub_env={"STUB_WORKER_PETITION": json.dumps(PETITION),
                  "STUB_REVIEW_ADJUDICATIONS": json.dumps(adjudications)})


def _petition_file_text(run) -> str:
    """Task 065 (requirement N.6, #150): the filing lands in the RUN dir's
    own `petitions.md`, never a workspace `<wave>-petitions.md` -- asserted
    below alongside reading it, so this same helper both fetches the text a
    caller needs AND proves the negative every caller here relies on.

    The stub `pi` itself pre-creates an EMPTY wave file under the
    workspace (`tests/stub-pi/pi`'s own `_declare_worker_petition`,
    mimicking the pre-created file a real wave's planning phase places) --
    that file existing is not the engine's own doing and is not what N.6
    forbids; what N.6 forbids is the ENGINE ever filing INTO it. So the
    proof below is that no workspace `*-petitions.md` file parses back ANY
    petition section at all, not that no such file exists.
    """
    reports = run.workspace / "artifacts" / "reports"
    from ralphd.engine import petitions as _petitions
    workspace_filed = [p for path in sorted(reports.glob("*-petitions.md"))
                       for p in _petitions.parse_petitions(path.read_text())]
    assert workspace_filed == [], (
        f"flow petition: requirement N.6 -- the engine must file into the "
        f"run dir, never a tracked workspace file, but a workspace file "
        f"under {reports} carries a filed petition: {workspace_filed}")
    path = run.run_dir / "petitions.md"
    assert path.exists(), (
        f"flow petition: no {path} was filed -- the engine never turned the "
        f"worker's declaration into a tracked run-dir filing")
    return path.read_text()


def _recorded_entries(run) -> list[dict]:
    f = run.run_dir / "petition-adjudications.json"
    assert f.exists(), (
        "flow petition: the run-scope record petition-adjudications.json was "
        "never written, so review's ruling reached nothing durable")
    doc = json.loads(f.read_text())
    assert doc.get("mac") and doc.get("digest"), (
        f"flow petition: the run-scope record must carry its keyed MAC and "
        f"digest, got keys {sorted(doc)}")
    return doc.get("entries") or []


# The path both rulings walk: one plan, two worker iterations (the first one
# declares), one review pass whose payload carries the adjudication -- the
# `adjudications_recorded` step lands BEFORE the verdict is scored, because
# the engine persists a valid ruling before it ever computes contract errors
# on the rest of the same payload.
PETITION_PATH_STEPS = [
    "state:running",
    "phase:planning@1",
    "iteration:planning",
    "iteration:worker",
    "iteration:worker",
    "signal:COMPLETE",
    "phase:review@1",
    "iteration:review",
    "adjudications_recorded",
    "verdict:review:verified@1",
    "signal:VERIFIED",
    "state:succeeded",
]


def test_a_declared_petition_is_filed_and_a_refuted_ruling_is_written_back(
        engine_factory):
    run = _petition_run(engine_factory, "flow-petition-refuted", [
        {"obligation": OBLIGATION, "adjudication": "REFUTED", "remedy": REMEDY}])
    assert run.proc.wait(timeout=60) == 0, (
        "flow petition REFUTED: the engine did not exit 0")

    assert_step_sequence(flow_steps(run), PETITION_PATH_STEPS,
                         flow="flow petition REFUTED")

    text = _petition_file_text(run)
    for field, value in (("Obligation", OBLIGATION),
                         ("Type", PETITION["type"]),
                         ("Evidence", PETITION["evidence"])):
        assert f"**{field}**: {value}" in text, (
            f"flow petition REFUTED: the filing must carry the declared "
            f"{field} verbatim; filed text was:\n{text}")
    assert f"**Adjudication**: REFUTED \u2014 {REMEDY}" in text, (
        f"flow petition REFUTED: the ruling must be written back into the "
        f"petition's own section with its remedy; filed text was:\n{text}")
    assert text.count("**Adjudication**") == 1, (
        f"flow petition REFUTED: the ruling must be written back exactly once, "
        f"filed text was:\n{text}")

    entries = _recorded_entries(run)
    assert [(e.get("obligation"), e.get("adjudication"), e.get("remedy"))
            for e in entries] == [(OBLIGATION, "REFUTED", REMEDY)], (
        f"flow petition REFUTED: the run-scope record must carry exactly the "
        f"refutation and its remedy, got {entries}")

    witnesses = events_of(run, "petition_adjudications_recorded")
    assert len(witnesses) == 1 and witnesses[0].get("entries") == 1, (
        f"flow petition REFUTED: events.jsonl must witness the one recorded "
        f"ruling, got {witnesses}")

    st = status(run)
    assert st.get("state") == "succeeded", (
        f"flow petition REFUTED: a refuted petition is not a run failure -- the "
        f"run must still end on review's own verdict, got {st.get('state')!r}")


def test_a_declared_petition_confirmed_becomes_a_run_scope_exemption(
        engine_factory):
    run = _petition_run(engine_factory, "flow-petition-confirmed", [
        {"obligation": OBLIGATION, "adjudication": "CONFIRMED"}])
    assert run.proc.wait(timeout=60) == 0, (
        "flow petition CONFIRMED: the engine did not exit 0")

    assert_step_sequence(flow_steps(run), PETITION_PATH_STEPS,
                         flow="flow petition CONFIRMED")

    text = _petition_file_text(run)
    assert "**Adjudication**: CONFIRMED" in text, (
        f"flow petition CONFIRMED: the ruling must be written back into the "
        f"petition's own section; filed text was:\n{text}")
    assert "REFUTED" not in text, (
        f"flow petition CONFIRMED: a confirmation must never render a remedy "
        f"line; filed text was:\n{text}")

    entries = _recorded_entries(run)
    assert [(e.get("obligation"), e.get("adjudication")) for e in entries] == [
        (OBLIGATION, "CONFIRMED")], (
        f"flow petition CONFIRMED: the run-scope record must carry exactly the "
        f"confirmation, got {entries}")
    assert not entries[0].get("remedy"), (
        f"flow petition CONFIRMED: a confirmation carries no remedy, got "
        f"{entries[0]!r}")

    witnesses = events_of(run, "petition_adjudications_recorded")
    assert len(witnesses) == 1, (
        f"flow petition CONFIRMED: events.jsonl must witness the recorded "
        f"ruling exactly once, got {witnesses}")
    assert witnesses[0].get("mac") == json.loads(
        (run.run_dir / "petition-adjudications.json").read_text()).get("mac"), (
        "flow petition CONFIRMED: the witness event's MAC must match the record "
        "it witnesses -- otherwise a later tamper check has nothing to compare")

    st = status(run)
    assert st.get("state") == "succeeded", (
        f"flow petition CONFIRMED: the run must end succeeded, got "
        f"{st.get('state')!r}")
    assert st.get("verdict") in ("verified", "verified-with-residuals"), (
        f"flow petition CONFIRMED: the ending verdict must be a verified label "
        f"(a confirmed obligation is a disclosed residual, not a rejection), got "
        f"{st.get('verdict')!r}")
