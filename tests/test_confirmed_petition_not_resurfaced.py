"""Task 001 (requirement C.4, #112): an obligation already ADJUDICATED at
RUN scope (`petitions.settled_obligations`/`record_confirmed_
adjudications`, task 011) must not be re-surfaced to review as an OPEN
petition just because its own `<wave>-petitions.md` filing's
`Adjudication` field is still blank -- a per-file, workspace fact that
task 011's run-scope record has already overtaken. PRD requirement C.4:
"a CONFIRMED petition removes its obligation from the blocking set" for
the REST OF THE RUN, not only the review pass that confirmed it, so a
'MUST adjudicate' instruction naming an already-settled obligation on
every later review pass would either waste a rehearing or invite review
to flip a CONFIRMED call back open.

#209 §5 generalises this module's subject from "already CONFIRMED" to
"already ADJUDICATED, CONFIRMED or REFUTED". A REFUTED obligation was
never suppressed here, because the filter read `confirmed_obligations`
(CONFIRMED-only by construction) -- and a WORKSPACE filing can never close
its own `Adjudication` field either (`write_back_petition_adjudication`
annotates `run.petitions_file` only, a documented no-op for a tracked
workspace file, task 008/requirement H). So a settled REFUTED obligation
was re-presented to every later review pass and re-argued from scratch:
13 `petition_refuted` events for 3 ledger entries across 6 review passes
in the `deck-phase4` run. Suppression is not exemption: a REFUTED entry
still exempts nothing, and nothing is deleted -- the ledger row, its
remedy, its `petition_refuted` event and `run-record.md`'s rendering of
all three are unchanged.

Wiring lives engine-side, not test-side:
`LoopSupervisor._petitions_review_context` (`src/ralphd/engine/loop.py`)
passes `self.run` through to `engine.petitions.petitions_review_context`,
which is the one place the exclusion is applied
(`_obligation_confirmed`/`settled_obligations`).

Six directions pinned:
1. a CONFIRMED obligation is dropped from the rendered context while a
   second, unadjudicated obligation in the SAME file still renders;
2. a tampered record (`confirmed_obligations`/`settled_obligations`
   returning `tampered=True`) surfaces the obligation again, for EITHER
   verdict -- fail towards adjudication, never towards a manufactured
   exemption and never towards a settlement nobody can authenticate;
3. `run=None` (every pre-existing caller) is unaffected -- behaviour is
   byte-for-byte the same as before this task.
4. (#209 §5) a REFUTED obligation settled at run scope is likewise not
   re-presented as OPEN, while the unadjudicated one in the same file is;
5. (#209 §5) a settled obligation supplied in a TRUNCATED wording retires
   the full filing too, and the mirror direction as well, with an
   unrelated obligation in the same file still rendering -- the matching
   rule is bounded, not "matches everything";
6. (#209 §5) the mid-run adjudication episode's own scoping call excludes
   it too, so no bounded off-budget episode is ever spent re-arguing it.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine import petitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir, read_json
from tests.conftest import supervisor_factory

TWO_OPEN_PETITIONS_MD = """# v0.15 petitions

## Requirement B, DoD predicate 9 cannot be met

**Obligation**: Requirement B, DoD predicate 9 (verbatim: "every finding is
included in the escalation").
**Type**: contradictory-clauses
**Evidence**: requirement A's own residual-finding carve-out means a
residual finding never appears in a BLOCKING escalation; the two clauses
cannot both hold.

## A second, unrelated obligation

**Obligation**: A second, wholly unrelated obligation nobody has ruled on.
**Type**: environmental
**Evidence**: unrelated evidence for the unrelated obligation.
"""


# The shared harness (tests/conftest.py, G.1): this module's run dir and
# workspace sit side by side under the test's own tmp_path.
_supervisor = supervisor_factory(run_id="task001-confirmed-not-resurfaced", run_root="run",
                                workspace="ws")



def _write_two_open_petitions(tmp_path: Path) -> None:
    reports = tmp_path / "ws" / "artifacts" / "reports"
    reports.mkdir(parents=True)
    (reports / "v0.15-petitions.md").write_text(TWO_OPEN_PETITIONS_MD)


# #209 §5: the truncation half needs its own body and its own workspace,
# because the geometry under test is "the ledger holds a SHORTER wording of
# the same obligation than the filing does" -- the real defect's shape,
# where a hard-wrapped `**Obligation**:` field reached the ledger cut at its
# first line (`_FIELD_RE` is single-line by design) while the filing carried
# the whole sentence.
def _write_petitions_body(ws: Path, body: str) -> None:
    reports = ws / "artifacts" / "reports"
    reports.mkdir(parents=True)
    (reports / "v0.15-petitions.md").write_text(body)


# ---------------------------------------------------------------------------
# Direction 1: a CONFIRMED obligation is excluded; the unconfirmed one stays.
# ---------------------------------------------------------------------------

def test_confirmed_obligation_is_not_resurfaced_but_the_other_one_still_is(
        tmp_path):
    _write_two_open_petitions(tmp_path)
    sup = _supervisor(tmp_path)
    petitions.record_confirmed_adjudications(sup.run, [
        {"obligation": "Requirement B, DoD predicate 9",
         "adjudication": "CONFIRMED"},
    ])
    ctx = sup._petitions_review_context()
    assert "DoD predicate 9" not in ctx
    assert "wholly unrelated obligation" in ctx
    assert "MUST" in ctx  # the surviving instruction is still present


def test_engine_petitions_function_itself_honours_the_run_argument(tmp_path):
    """The same exclusion, called directly at the `engine.petitions`
    layer (no supervisor involved) -- pins that the wiring lives in
    `petitions_review_context` itself, not merely in the loop.py call
    site."""
    _write_two_open_petitions(tmp_path)
    run = RunDir(root=tmp_path / "run")
    petitions.record_confirmed_adjudications(run, [
        {"obligation": "Requirement B, DoD predicate 9",
         "adjudication": "CONFIRMED"},
    ])
    ctx = petitions.petitions_review_context(tmp_path / "ws", run)
    assert "DoD predicate 9" not in ctx
    assert "wholly unrelated obligation" in ctx


def test_with_no_confirmed_obligations_both_petitions_still_render(tmp_path):
    _write_two_open_petitions(tmp_path)
    sup = _supervisor(tmp_path)
    ctx = sup._petitions_review_context()
    assert "DoD predicate 9" in ctx
    assert "wholly unrelated obligation" in ctx


# ---------------------------------------------------------------------------
# Direction 2: a tampered record fails towards adjudication, not exemption.
# ---------------------------------------------------------------------------

def test_a_tampered_record_surfaces_the_obligation_again(tmp_path):
    """#209 §5 widened this node from the CONFIRMED half to both verdicts:
    the record now carries a CONFIRMED entry for the first obligation AND a
    REFUTED entry for the second, so the tamper posture is pinned for the
    SETTLED reader (`settled_obligations`) as well as the exemption reader
    (`confirmed_obligations`). Both must read empty and both petitions must
    render again. Nothing is relaxed -- every assertion the CONFIRMED-only
    version made is still made verbatim; the REFUTED entry and the
    `settled_obligations` assertion are additions. The direction matters
    for the new reader in a way it did not for the old one: an unreadable
    record must not be allowed to look like a settlement, or a hand-edit
    could keep a genuinely open obligation out of review's prompt
    altogether."""
    _write_two_open_petitions(tmp_path)
    sup = _supervisor(tmp_path)
    petitions.record_confirmed_adjudications(sup.run, [
        {"obligation": "Requirement B, DoD predicate 9",
         "adjudication": "CONFIRMED"},
        {"obligation": "A second, wholly unrelated obligation nobody has "
                       "ruled on.",
         "adjudication": "REFUTED", "remedy": "do X instead"},
    ])
    # simulate a worker-phase hand-edit of the run-scope record, exactly
    # as tests/test_petition_adjudications_run_scope.py's own tamper
    # probes do -- append a fabricated entry, recompute the unkeyed
    # digest, and write it back WITHOUT going through
    # `record_confirmed_adjudications`.
    doc = read_json(sup.run.petition_adjudications_file)
    doc["entries"].append({"obligation": "A fabricated exemption",
                            "adjudication": "CONFIRMED"})
    doc["digest"] = petitions._adjudications_digest(doc["entries"])
    sup.run.petition_adjudications_file.write_text(json.dumps(doc))

    confirmed, tampered = petitions.confirmed_obligations(sup.run)
    assert tampered is True
    assert confirmed == set()

    settled, settled_tampered = petitions.settled_obligations(sup.run)
    assert settled_tampered is True
    assert settled == set()

    ctx = sup._petitions_review_context()
    assert "DoD predicate 9" in ctx  # surfaced again, not exempted
    assert "wholly unrelated obligation" in ctx  # surfaced again, not retired


# ---------------------------------------------------------------------------
# Direction 3: `run=None` (every pre-existing caller) is unaffected.
# ---------------------------------------------------------------------------

def test_run_none_keeps_prior_behaviour_unchanged(tmp_path):
    _write_two_open_petitions(tmp_path)
    without_run = petitions.petitions_review_context(tmp_path / "ws")
    assert "DoD predicate 9" in without_run
    assert "wholly unrelated obligation" in without_run


# ---------------------------------------------------------------------------
# Direction 4 (#209 §5): a REFUTED obligation is retired too, not just a
# CONFIRMED one -- the node that goes red without the read-site filter switch.
# ---------------------------------------------------------------------------

def test_a_refuted_obligation_is_not_re_presented_as_open(tmp_path):
    """The `deck-phase4` shape exactly: a WORKSPACE filing whose own
    `Adjudication` field the engine can never write back (task
    008/requirement H), adjudicated REFUTED at run scope. Before #209 §5 the
    read-site filter consulted `confirmed_obligations`, so this obligation
    was rendered as OPEN with a 'MUST adjudicate' instruction on every later
    review pass -- 13 `petition_refuted` events for 3 ledger entries over 6
    passes. The remedy has already been delivered once (the ledger entry,
    the `petition_refuted` event, the run record); a second hearing is the
    waste, and `docs/petition-format.md` already calls a re-filing of a
    REFUTED obligation a blocking finding rather than a second hearing."""
    _write_two_open_petitions(tmp_path)
    sup = _supervisor(tmp_path)
    petitions.record_confirmed_adjudications(sup.run, [
        {"obligation": "Requirement B, DoD predicate 9",
         "adjudication": "REFUTED",
         "remedy": "drop requirement A's residual carve-out"},
    ])
    ctx = sup._petitions_review_context()
    assert "DoD predicate 9" not in ctx
    assert "wholly unrelated obligation" in ctx
    assert "MUST" in ctx  # the surviving instruction is still present

    # Retired, never deleted: the ledger row and its remedy are intact, and
    # REFUTED still exempts nothing.
    stored, tampered = petitions.read_adjudications(sup.run)
    assert tampered is False
    assert stored[0]["adjudication"] == "REFUTED"
    assert stored[0]["remedy"] == "drop requirement A's residual carve-out"
    assert petitions.confirmed_obligations(sup.run) == (set(), False)


# ---------------------------------------------------------------------------
# Direction 5 (#209 §5): the truncated-wording half, both directions, with a
# negative control that keeps the substring rule bounded.
# ---------------------------------------------------------------------------

_FULL_OBLIGATION = ('Requirement B, DoD predicate 9 (verbatim: "every '
                    'finding is included in the escalation") cannot hold '
                    "alongside requirement A's residual carve-out.")

_ONE_LINE_PETITIONS_MD = """# v0.15 petitions

## Requirement B, DoD predicate 9 cannot be met

**Obligation**: {obligation}
**Type**: contradictory-clauses
**Evidence**: the two clauses cannot both hold.

## A second, unrelated obligation

**Obligation**: A second, wholly unrelated obligation nobody has ruled on.
**Type**: environmental
**Evidence**: unrelated evidence for the unrelated obligation.
"""


def test_a_settled_obligation_supplied_truncated_retires_the_full_filing_too(
        tmp_path):
    """`_obligation_confirmed`'s normalised substring-either-direction rule
    is reused verbatim for the SETTLED set, which is what makes the real
    defect's two wordings of one obligation retire together: `deck-phase4`'s
    ledger held the same obligation twice, once cut at 56 characters because
    its `**Obligation**:` field was hard-wrapped and `_FIELD_RE` captures a
    single line. Both directions are pinned (ledger shorter than filing, and
    filing shorter than ledger) because either can happen depending on which
    channel wrapped. The negative control in the same node keeps the rule
    bounded: an unrelated obligation in the same file still renders, so this
    pins fuzzy matching, not "matches everything"."""
    truncated = _FULL_OBLIGATION[:56]
    assert truncated in _FULL_OBLIGATION  # the geometry the node claims

    # Direction A: the filing carries the full text, the ledger the cut one.
    ws_a = tmp_path / "ws-a"
    _write_petitions_body(ws_a, _ONE_LINE_PETITIONS_MD.format(
        obligation=_FULL_OBLIGATION))
    run_a = RunDir(root=tmp_path / "run-a")
    petitions.record_confirmed_adjudications(run_a, [
        {"obligation": truncated, "adjudication": "REFUTED",
         "remedy": "drop the carve-out"},
    ])
    ctx_a = petitions.petitions_review_context(ws_a, run_a)
    assert "DoD predicate 9" not in ctx_a
    assert "wholly unrelated obligation" in ctx_a

    # Direction B, the mirror: the ledger carries the full text, the filing
    # the cut one.
    ws_b = tmp_path / "ws-b"
    _write_petitions_body(ws_b, _ONE_LINE_PETITIONS_MD.format(
        obligation=truncated))
    run_b = RunDir(root=tmp_path / "run-b")
    petitions.record_confirmed_adjudications(run_b, [
        {"obligation": _FULL_OBLIGATION, "adjudication": "REFUTED",
         "remedy": "drop the carve-out"},
    ])
    ctx_b = petitions.petitions_review_context(ws_b, run_b)
    assert "DoD predicate 9" not in ctx_b
    assert "wholly unrelated obligation" in ctx_b


# ---------------------------------------------------------------------------
# Direction 6 (#209 §5): the mid-run episode's own scoping call.
# ---------------------------------------------------------------------------

def test_the_midrun_episode_scoping_call_also_excludes_a_settled_obligation(
        tmp_path):
    """`LoopSupervisor._maybe_run_midrun_adjudication_episode` scopes its
    bounded, off-budget episode to `open_unconfirmed_petitions(workspace,
    run, contract_text=..., tasks=...)` and returns immediately when that
    list is empty. Called with the episode's own argument shape (the
    `tasks` list is what engages `_obligation_is_history`, which is left
    exactly as it is -- #212 says it is already too aggressive), so a
    settled obligation costs no further episode either. The before/after
    pair keeps this non-vacuous: the obligation IS open until it settles."""
    _write_two_open_petitions(tmp_path)
    ws = tmp_path / "ws"
    run = RunDir(root=tmp_path / "run")
    tasks = [{"id": "010", "title": "t", "status": "petitioned"}]

    before = petitions.open_unconfirmed_petitions(
        ws, run, contract_text="", tasks=tasks)
    assert len(before) == 1
    assert "DoD predicate 9" in before[0]["obligation"]

    petitions.record_confirmed_adjudications(run, [
        {"obligation": "Requirement B, DoD predicate 9",
         "adjudication": "REFUTED", "remedy": "drop the carve-out"},
    ])
    after = petitions.open_unconfirmed_petitions(
        ws, run, contract_text="", tasks=tasks)
    assert after == []
