"""D.6 -- the transition-edge re-pins, folded into one module (task 027,
requirement D.6). Absorbs, whole, the eight `tests/test_*.py` modules whose
subject is the declared run-state transition table itself, or one specific
edge in it, proven both declaratively (`transitions.is_legal`/`successors`/
`guard_names`/`table_for`) and, where the source module also drove a real
engine, behaviourally over a real `LoopSupervisor`:

* `tests/test_transition_table.py` (task 002/#72, task 008): the base
  declared-table test -- every state's successors are enumerable, terminal
  states have none, the two `vigilant`/non-`vigilant` variants differ only
  by the declared delta, the executor refuses an undeclared move.
* `tests/test_transition_table_docs.py` (task 016, requirement B
  follow-through): `docs/architecture.md`'s generated transition-table
  block matches `scripts/render_transition_table.py`'s live output.
* `tests/test_transition_diagram_docs.py` (task 005, requirement C.1,
  finding 4): the same byte-for-byte proof for the generated FSM diagram
  block and `scripts/render_transition_diagram.py`.
* `tests/test_transition_executor_cutover.py` (task 014): the table
  executor cutover end to end -- illegal transitions refused at runtime,
  the two variants differ only by selection, no legacy dual path.
* `tests/test_rejected_incurable_edge.py` (task 006, requirement B, #111):
  the `review -> review` self-loop (`rejected_incurable`) -- an
  all-incurable rejection pauses and escalates instead of replanning.
* `tests/test_steer_hat_table_entry.py` (task 009 v0.14, requirement G,
  #98, #60 pillar 4): `steer` as a pure table entry -- state, schema,
  prompt, `MODEL_HATS` entry, tier rungs -- and nothing else.
* `tests/test_doomed_plan_fault.py` (task 007 v0.14, requirement D, #99):
  the `worker -> review` `doomed_plan_unverifiable` edge -- a wholly doomed
  remainder faults on the first pass instead of stagnating silently.
* `tests/test_plan_review_resume_issue161.py` (task 046, #161): the two
  edges the #161 fix declares (`planning -> worker` under
  `planning_skipped_plan_already_reviewed`, `plan_review -> worker` under
  `plan_review_revision_refused_work_recorded`) -- NOT live in the engine
  running this run (operator steer 003); these tests are its whole
  evidence, moved here verbatim, still never claimed as exercised by this
  run.

Six module-local helper names collided across the absorbed modules
(`_ok`, `_supervisor`, `_ScriptedAgent`, `_plan`, `TASK_1`, `TASK_2`) --
each later occurrence survives here renamed with its own section's
suffix (`_cutover`, `_rejected_incurable`, `_doomed`, `_161`); two
`_embedded_block` helpers (table-docs/diagram-docs) survive renamed the
same way. `docs/test-suite-audit.md` § "Subsumed" records every renamed
node. `REPO_ROOT`/`ARCHITECTURE_DOC` are declared once, shared by every
section that used them.
"""

from __future__ import annotations

import ast
import asyncio
import json
import shlex
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "src"))

import render_transition_diagram as rtd  # noqa: E402
import render_transition_table as rtt  # noqa: E402

from ralphd.engine import contracts  # noqa: E402
from ralphd.engine import findings  # noqa: E402
from ralphd.engine import tiers as T  # noqa: E402
from ralphd.engine import transitions  # noqa: E402
from ralphd.engine import transitions as tr  # noqa: E402
from ralphd.engine.config import (DEFAULT_HAT_MODEL_REF, MODEL_HATS,  # noqa: E402
                                  UNRECOGNISED_HAT_FALLBACK, JobConfig)
from ralphd.engine.loop import LoopSupervisor  # noqa: E402
from ralphd.engine.runner import IterationResult  # noqa: E402
from ralphd.engine.state import (RunDir, atomic_write_json,  # noqa: E402
                                 read_events, task_blocked_by)
from tests.conftest import (make_supervisor, scripted_supervisor,  # noqa: E402
                            supervisor_factory, supervisor_with_runner)

ARCHITECTURE_DOC = REPO_ROOT / "docs" / "architecture.md"


# ===========================================================================
# From tests/test_transition_table.py (task 002/#72, task 008 (the declared table itself))
# ===========================================================================

MODULE_PATH = REPO_ROOT / "src" / "ralphd" / "engine" / "transitions.py"


# ---------------------------------------------------------------------------
# Every state's successors are enumerable
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vigilant", [True, False])
def test_every_declared_state_has_enumerable_successors(vigilant):
    for state in tr.ALL_STATES:
        succ = tr.successors(state, vigilant)
        assert isinstance(succ, tuple)
        for edge in succ:
            assert isinstance(edge, tr.Transition)
            assert edge.target in tr.ALL_STATES
            assert isinstance(edge.guard, str) and edge.guard


def test_terminal_states_have_no_successors_and_nothing_else_does():
    # `verify` is the one declared exception: it is a real hat-bearing
    # state in both variants (STATE_HAT/HATS), but it is only *reachable*
    # -- and only has successors -- under vigilant (VIGILANT_DELTA). It is
    # not a terminal state; it is simply unreachable, non-terminal state
    # under non-vigilant, which is exactly what the delta declares.
    #
    # `plan_review` (task 018, requirement K, #101) is the SAME shape on a
    # DIFFERENT axis: it is a real hat-bearing state either way, but this
    # test never turns `peer_plan_review` on (it only parametrizes
    # `vigilant`), so its successors are `()` regardless of `vigilant`
    # here -- reachability is gated by `JobConfig.peer_plan_review`
    # (default OFF), never by `vigilant` at all.
    for vigilant in (True, False):
        for state in tr.ALL_STATES:
            succ = tr.successors(state, vigilant)
            if state in tr.RUN_TERMINAL_STATES:
                assert succ == (), f"{state} ({vigilant=}) must be terminal"
            elif state == "verify" and not vigilant:
                assert succ == (), "verify is unreachable, not terminal, non-vigilant"
            elif state == "plan_review":
                assert succ == (), (
                    "plan_review is unreachable, not terminal, when "
                    "peer_plan_review is off (this test's default)")
            elif state == "verdict_gate":
                assert succ == (), (
                    "verdict_gate is unreachable, not terminal, when "
                    "peer_verdict_gate is off (this test's default) -- "
                    "task 020, requirement L, #101, the SAME shape as "
                    "plan_review just above, on its own axis")
            else:
                assert succ != (), (
                    f"{state} ({vigilant=}) is not terminal but has no "
                    "successors")


def test_entry_state_is_planning():
    assert tr.ENTRY_STATE == "planning"
    assert tr.ENTRY_STATE in tr.ALL_STATES


# ---------------------------------------------------------------------------
# The two variants differ ONLY by the declared delta
# ---------------------------------------------------------------------------

def test_vigilant_and_non_vigilant_differ_only_by_the_declared_delta():
    vigilant_table = tr.table_for(True)
    plain_table = tr.table_for(False)
    differing_keys = {
        k for k in tr.ALL_STATES
        if vigilant_table.get(k, ()) != plain_table.get(k, ())
    }
    assert differing_keys == set(tr.VIGILANT_DELTA), (
        "the only states allowed to differ between variants are exactly "
        "the ones named in VIGILANT_DELTA")
    for key, edges in tr.VIGILANT_DELTA.items():
        assert vigilant_table[key] == edges


def test_non_vigilant_never_reaches_verify():
    plain_table = tr.table_for(False)
    assert plain_table["verify"] == ()
    for state, edges in plain_table.items():
        assert all(edge.target != "verify" for edge in edges), (
            f"{state} must not target verify in the non-vigilant variant")


def test_vigilant_worker_can_reach_verify():
    vigilant_table = tr.table_for(True)
    assert any(edge.target == "verify" for edge in vigilant_table["worker"])


def test_there_is_exactly_one_function_that_produces_a_variant_table():
    """`table_for` is the only producer of a variant of the STEP-state
    table (`TABLE`/`VIGILANT_DELTA`) -- guards against a second,
    hand-written non-vigilant table creeping in later.

    Task 019 (requirement F) adds a SECOND declared table --
    `TASK_STATUS_TABLE`/`TASK_STATUS_VIGILANT_DELTA`, a different concern
    (one task's `status` field, not the run's own FSM position) -- with
    its own analogous producer, `task_status_table_for`. The invariant this
    test guards is now "exactly one producer PER declared table", not
    "exactly one producer in the whole module": it still fails if either
    table grows a second, duplicate producer.
    """
    tree = ast.parse(MODULE_PATH.read_text())
    producers = [
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.endswith("table_for")
    ]
    assert sorted(producers) == ["table_for", "task_status_table_for"]


# ---------------------------------------------------------------------------
# The specific transitions requirement B names, at minimum
# ---------------------------------------------------------------------------

def test_planning_to_worker():
    assert tr.is_legal("planning", "worker", vigilant=True)
    assert tr.is_legal("planning", "worker", vigilant=False)


def test_worker_to_worker_or_verify_when_vigilant():
    assert tr.is_legal("worker", "worker", vigilant=True)
    assert tr.is_legal("worker", "verify", vigilant=True)


def test_worker_to_worker_or_review_when_non_vigilant():
    assert tr.is_legal("worker", "worker", vigilant=False)
    assert tr.is_legal("worker", "review", vigilant=False)
    assert not tr.is_legal("worker", "verify", vigilant=False)


def test_verify_to_worker_both_directions():
    guards = tr.guard_names("verify", vigilant=True)
    assert "verify_failed_retry_same_task" in guards
    assert "verify_passed_next_task" in guards
    for edge in tr.successors("verify", vigilant=True):
        assert edge.target == "worker"


def test_review_to_worker_replan_finishing_or_review():
    # requirement A, #72: `review_interrupted` self-loops back to `review`,
    # `review_unjudged_at_budget_end` lands on `finishing` (already a
    # target via `rejected_no_approach_left`) -- so `review` joins the
    # target set alongside the four pre-existing targets. Task 004
    # (finding 4): `rejected_curable_pass_available` now targets `cure`
    # (not `worker` directly) and `rejected_last_approach` now targets
    # `paused` (not a `review -> review` self-loop) -- both new step
    # states join the target set too.
    targets = {edge.target for edge in tr.successors("review", vigilant=True)}
    assert targets == {"worker", "replan", "finishing", "review", "cure", "paused"}
    targets_plain = {edge.target for edge in tr.successors("review", vigilant=False)}
    assert targets_plain == {"worker", "replan", "finishing", "review", "cure", "paused"}


def test_review_interrupted_edges_declared_in_both_variants_requirement_a_72():
    """Requirement A (#72): the two new interrupted-review edges must be
    named in `guard_names("review", vigilant)` for BOTH variants, and the
    four pre-existing guard names/targets must be byte-for-byte unchanged.
    """
    pre_existing = {
        "steering_pending_at_verified": "worker",
        "verified": "finishing",
        "rejected_approach_left": "replan",
        "rejected_no_approach_left": "finishing",
    }
    for vigilant in (True, False):
        guards = tr.guard_names("review", vigilant=vigilant)
        assert "review_interrupted" in guards
        assert "review_unjudged_at_budget_end" in guards
        by_guard = {edge.guard: edge.target for edge in tr.successors("review", vigilant=vigilant)}
        for guard, target in pre_existing.items():
            assert guard in by_guard, f"{guard} missing for vigilant={vigilant}"
            assert by_guard[guard] == target, (
                f"{guard} target changed for vigilant={vigilant}")
        # the new edges' own meanings: interrupted stays in review, budget
        # exhaustion at the same interrupted condition ends in finishing.
        assert by_guard["review_interrupted"] == "review"
        assert by_guard["review_unjudged_at_budget_end"] == "finishing"


def test_review_cure_pending_guard_declared_in_both_variants_and_under_the_gate_requirement_d1():
    """Task 018 (PRD requirement D.1, #220): the third `_terminal_success_
    blockers` blocker gets its own declared guard, `review_cure_pending_
    at_verified`, DISTINCT from `steering_pending_at_verified` -- reusing
    that name would lie in the event log (the PRD's own words) -- present
    in `transitions.TABLE["review"]` for both variants and, byte-for-byte,
    under `PEER_VERDICT_GATE_DELTA["review"]`, targeting `worker` exactly
    like the steering guard does."""
    for vigilant in (True, False):
        by_guard = {edge.guard: edge.target
                    for edge in tr.successors("review", vigilant=vigilant)}
        assert "review_cure_pending_at_verified" in by_guard
        assert by_guard["review_cure_pending_at_verified"] == "worker"
        assert by_guard["review_cure_pending_at_verified"] != "replan"
    assert "review_cure_pending_at_verified" != "steering_pending_at_verified"
    gated = {edge.guard: edge.target
            for edge in tr.successors("review", vigilant=False,
                                       peer_verdict_gate=True)}
    assert gated["review_cure_pending_at_verified"] == "worker"


def test_third_terminal_success_blocker_fires_for_a_pending_review_cure_task_and_clears_once_terminal(
        tmp_path):
    """Task 018 (PRD requirement D.1, #220): `_terminal_success_blockers`
    names a non-terminal `review-cure`-origin task (`state.is_review_cure_
    origin`) as its third blocker, silent once the plan holds none --
    exactly the same "settled is not done" shape the pre-existing steering-
    origin blocker already proves, one level over."""
    from ralphd.engine.state import RunDir

    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [
        {"id": "001", "status": "completed"},
        {"id": "002", "status": "pending", "title": "cure-inserted",
         "origin": "review-cure"},
    ]})
    sup = make_supervisor(root)

    blockers = sup._terminal_success_blockers()
    assert len(blockers) == 1, blockers
    assert "002" in blockers[0]
    assert "not terminal" in blockers[0]

    doc = sup.run.read_tasks()
    for t in doc["tasks"]:
        if t["id"] == "002":
            t["status"] = "completed"
    atomic_write_json(sup.run.tasks_file, doc)
    assert sup._terminal_success_blockers() == []


def test_approach_boundary_is_a_transition_with_a_guard():
    assert "replan" in tr.ALL_STATES
    edges = tr.successors("replan", vigilant=True)
    assert edges, "replan must have declared successors"
    for edge in edges:
        assert edge.guard  # every approach-boundary edge is named/gated
    assert tr.is_legal("replan", "planning", vigilant=True)
    assert tr.is_legal("replan", "finishing", vigilant=True)


def test_finishing_is_non_terminal():
    assert "finishing" not in tr.RUN_TERMINAL_STATES
    assert tr.successors("finishing", vigilant=True) != ()
    assert tr.successors("finishing", vigilant=False) != ()


def test_reflect_unwind_follows_finishing():
    assert tr.is_legal("finishing", "reflect", vigilant=True)
    reflect_targets = {edge.target for edge in tr.successors("reflect", vigilant=True)}
    assert reflect_targets == set(tr.RUN_TERMINAL_STATES)


def test_finishing_can_also_go_straight_to_a_terminal_outcome_without_reflect():
    targets = {edge.target for edge in tr.successors("finishing", vigilant=True)}
    assert targets == {"reflect"} | set(tr.RUN_TERMINAL_STATES)


# ---------------------------------------------------------------------------
# Per-hat entries
# ---------------------------------------------------------------------------

def test_every_hat_has_a_table_entry_and_every_step_state_with_a_hat_is_in_hats():
    for hat in tr.HATS:
        assert hat in tr.STATE_HAT.values() or hat in tr.STATE_HAT
    hat_bearing_states = {s for s, h in tr.STATE_HAT.items() if h is not None}
    assert hat_bearing_states == set(tr.HATS)


def test_deterministic_states_have_no_hat():
    assert tr.STATE_HAT["replan"] is None
    assert tr.STATE_HAT["finishing"] is None
    for terminal in tr.RUN_TERMINAL_STATES:
        assert tr.STATE_HAT[terminal] is None


# ---------------------------------------------------------------------------
# The engine cutover (task 014)
# ---------------------------------------------------------------------------

def test_loop_py_now_imports_the_table():
    """Task 008 was declaration only; task 014 is the cutover -- loop.py now
    drives this table instead of a hand-written imperative flow. See
    tests/test_transition_executor_cutover.py for the behavioural proof
    (illegal transition refused, variants differ only by selection)."""
    loop_src = (REPO_ROOT / "src" / "ralphd" / "engine" / "loop.py").read_text()
    assert ("from . import transitions" in loop_src
            or "from ralphd.engine import transitions" in loop_src
            or "engine.transitions" in loop_src)

# ===========================================================================
# From tests/test_transition_table_docs.py (task 016, requirement B follow-through)
# ===========================================================================

def _embedded_block_table_docs() -> str:
    text = ARCHITECTURE_DOC.read_text()
    start = text.index(rtt.BEGIN_MARKER)
    end = text.index(rtt.END_MARKER) + len(rtt.END_MARKER)
    return text[start:end]


def test_markers_present_exactly_once():
    text = ARCHITECTURE_DOC.read_text()
    assert text.count(rtt.BEGIN_MARKER) == 1
    assert text.count(rtt.END_MARKER) == 1
    assert text.index(rtt.BEGIN_MARKER) < text.index(rtt.END_MARKER)


def test_doc_block_matches_the_generator_right_now():
    """The doc is not a hand copy: what's embedded must equal what the
    generator produces from the code, byte for byte."""
    generated = rtt.render().rstrip("\n")
    embedded = _embedded_block_table_docs()
    assert embedded == generated, (
        "docs/architecture.md's transition table block has drifted from "
        "src/ralphd/engine/transitions.py -- regenerate it with "
        "`python scripts/render_transition_table.py --write`"
    )


def test_generator_output_is_derived_from_the_table_not_a_constant():
    """Mutation case: prove the generator's output tracks the table's
    actual content rather than being a fixed string that happens to equal
    the doc. Render once against the real table, then render again after
    swapping one edge's guard name for a decoy and assert the two renders
    differ -- if they didn't, the "generated from code" claim above would
    be checking nothing.
    """
    baseline = rtt.render()

    real_table = transitions.TABLE
    mutated_worker = real_table["worker"][:-1] + (
        transitions.T("replan", "totally_decoy_guard_name_not_real"),
    )
    mutated_table = dict(real_table)
    mutated_table["worker"] = mutated_worker

    original_successors = transitions.successors

    def _mutated_successors(state, vigilant, peer_plan_review=False,
                             peer_verdict_gate=False):  # noqa: ANN001
        if state != "worker":
            return original_successors(state, vigilant, peer_plan_review,
                                       peer_verdict_gate)
        table = mutated_table if not vigilant else {
            **mutated_table,
            "worker": mutated_table["worker"] + (
                transitions.T("verify", "completed_task_pending_verification"),
            ),
        }
        return table[state]

    transitions.successors = _mutated_successors
    try:
        mutated_render = rtt.render()
    finally:
        transitions.successors = original_successors

    assert mutated_render != baseline, (
        "mutating a table edge produced an identical render -- the "
        "generator is not actually reading transitions.py's data"
    )
    assert "totally_decoy_guard_name_not_real" in mutated_render
    assert "totally_decoy_guard_name_not_real" not in baseline
    # And the doc, unmutated, must still equal the (unmutated) baseline --
    # confirming the earlier "matches right now" check isn't vacuously
    # true because both sides always agree regardless of content.
    assert _embedded_block_table_docs() == baseline.rstrip("\n")


def test_every_all_states_entry_appears_in_the_render():
    """Every state the table declares shows up in the rendered block, so a
    state added to ALL_STATES without a render row would be caught."""
    rendered = rtt.render()
    for state in transitions.ALL_STATES:
        assert f"`{state}`" in rendered, (
            f"state {state!r} is in transitions.ALL_STATES but missing "
            f"from the rendered transition table"
        )


def test_render_is_idempotent():
    assert rtt.render() == rtt.render()


# ---------------------------------------------------------------------------
# The task-status mini-FSM's own generated table and diagram (v0.20
# requirement B.1/F, #156, task 019) -- same discipline, second pair of
# blocks: `superseded` is a real transition TARGET the engine writes, so the
# declared table names it and the docs render it from that declaration
# instead of describing it in hand-written prose.
# ---------------------------------------------------------------------------

def _embedded_task_status_table_block() -> str:
    text = ARCHITECTURE_DOC.read_text()
    start = text.index(rtt.TASK_STATUS_BEGIN_MARKER)
    end = text.index(rtt.TASK_STATUS_END_MARKER) + len(rtt.TASK_STATUS_END_MARKER)
    return text[start:end]


def _embedded_task_status_diagram_block() -> str:
    text = ARCHITECTURE_DOC.read_text()
    start = text.index(rtd.TASK_STATUS_BEGIN_MARKER)
    end = text.index(rtd.TASK_STATUS_END_MARKER) + len(rtd.TASK_STATUS_END_MARKER)
    return text[start:end]


def test_task_status_block_markers_present_exactly_once():
    text = ARCHITECTURE_DOC.read_text()
    for marker in (rtt.TASK_STATUS_BEGIN_MARKER, rtt.TASK_STATUS_END_MARKER,
                   rtd.TASK_STATUS_BEGIN_MARKER, rtd.TASK_STATUS_END_MARKER):
        assert text.count(marker) == 1, marker
    assert (text.index(rtt.TASK_STATUS_BEGIN_MARKER)
            < text.index(rtt.TASK_STATUS_END_MARKER)
            < text.index(rtd.TASK_STATUS_BEGIN_MARKER)
            < text.index(rtd.TASK_STATUS_END_MARKER))


def test_task_status_table_block_matches_the_generator_right_now():
    assert _embedded_task_status_table_block() == \
        rtt.render_task_status_table().rstrip("\n"), (
        "docs/architecture.md's task-status table block has drifted from "
        "src/ralphd/engine/transitions.py -- regenerate it with "
        "`python scripts/render_transition_table.py --write`")


def test_task_status_diagram_block_matches_the_generator_right_now():
    block = _embedded_task_status_diagram_block()
    assert block == rtd.render_task_status_diagram().rstrip("\n"), (
        "docs/architecture.md's task-status diagram block has drifted from "
        "src/ralphd/engine/transitions.py -- regenerate it with "
        "`python scripts/render_transition_diagram.py --write`")
    assert block.count("```mermaid") == 1
    assert "stateDiagram-v2" in block


def test_both_task_status_renders_are_idempotent():
    assert rtt.render_task_status_table() == rtt.render_task_status_table()
    assert rtd.render_task_status_diagram() == rtd.render_task_status_diagram()


def test_every_declared_task_status_appears_in_both_renders():
    table = rtt.render_task_status_table()
    diagram = rtd.render_task_status_diagram()
    for status in transitions.TASK_STATUSES:
        assert f"`{status}`" in table, status
        assert f'"{status}"' in diagram, status


def test_superseded_is_rendered_as_a_declared_edge_in_both_blocks():
    """The deliverable the prose alone could not carry: the GENERATED table
    and mermaid say `in-progress -> superseded`, from the declaration."""
    table = _embedded_task_status_table_block()
    diagram = _embedded_task_status_diagram_block()
    assert "| `in-progress` | both | `superseded` |" in table
    assert "| `superseded` | — | `— (terminal)` |" in table
    assert "in_progress --> superseded" in diagram


def test_the_one_non_vigilant_only_claim_edge_is_labelled_in_both_blocks():
    """`in-progress -> completed` is the only edge the vigilant delta drops
    (`awaiting-validation` replaces `completed` as a vigilant worker's claim
    state), so the table calls it `non-vigilant only` and the diagram labels
    the arrow `non_vigilant_only` instead of drawing it as if both variants
    had it."""
    assert "| `in-progress` | non-vigilant only | `completed` |" in \
        _embedded_task_status_table_block()
    assert "in_progress --> completed : non_vigilant_only" in \
        _embedded_task_status_diagram_block()


def test_every_declared_task_status_edge_appears_in_the_diagram():
    diagram = rtd.render_task_status_diagram()
    for vigilant in (False, True):
        table = transitions.task_status_table_for(vigilant)
        for status, targets in table.items():
            for target in targets:
                line = (f"{status.replace('-', '_')} --> "
                        f"{target.replace('-', '_')}")
                assert line in diagram, (line, vigilant)


def test_task_status_renders_track_the_declaration_not_a_constant():
    """Mutation case, mirroring the step-table one above: drop
    `superseded` from the composed table and both renders must change."""
    baseline_table = rtt.render_task_status_table()
    baseline_diagram = rtd.render_task_status_diagram()
    original = transitions.task_status_table_for

    def _without_superseded(vigilant):  # noqa: ANN001
        table = dict(original(vigilant))
        table["in-progress"] = tuple(
            t for t in table["in-progress"] if t != "superseded")
        return table

    transitions.task_status_table_for = _without_superseded
    try:
        mutated_table = rtt.render_task_status_table()
        mutated_diagram = rtd.render_task_status_diagram()
    finally:
        transitions.task_status_table_for = original

    assert "| `in-progress` | both | `superseded` |" not in mutated_table
    assert "in_progress --> superseded" not in mutated_diagram
    assert mutated_table != baseline_table
    assert mutated_diagram != baseline_diagram


def test_the_widened_stagnation_guard_condition_is_declared_and_rendered():
    """The every-approach pause edge: the row's guard name is still
    `stagnation_last_approach`, so the widened CONDITION is declared as data
    (`GUARD_CONDITION_NOTES`) and rendered into the same generated block
    rather than left to a reader's inference (v0.20 requirement B.3, #156,
    tasks 018/019)."""
    note = transitions.guard_condition_note("stagnation_last_approach")
    assert note and "EVERY approach that progressed" in note
    block = _embedded_block_table_docs()
    assert "| `worker` | worker | both | `paused` | `stagnation_last_approach` |" in block
    assert "Guards whose declared condition is wider than their name" in block
    assert note in block
    # A guard whose name already states its condition gets no note and no
    # bullet -- the section is not a running commentary on every guard.
    assert transitions.guard_condition_note("stagnation_exhausted") is None
    assert "- `stagnation_exhausted`" not in block


def test_a_declared_guard_note_actually_reaches_the_render():
    """Mutation case for the note section: a note declared for another
    guard shows up, so the containment check above is not vacuous."""
    original = dict(transitions.GUARD_CONDITION_NOTES)
    transitions.GUARD_CONDITION_NOTES["verified"] = "decoy note for a test"
    try:
        rendered = rtt.render()
    finally:
        transitions.GUARD_CONDITION_NOTES.clear()
        transitions.GUARD_CONDITION_NOTES.update(original)
    assert "- `verified` — decoy note for a test" in rendered
    assert "decoy note for a test" not in rtt.render()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))

# ===========================================================================
# From tests/test_transition_diagram_docs.py (task 005, requirement C.1, finding 4)
# ===========================================================================

def _embedded_block_diagram_docs() -> str:
    text = ARCHITECTURE_DOC.read_text()
    start = text.index(rtd.BEGIN_MARKER)
    end = text.index(rtd.END_MARKER) + len(rtd.END_MARKER)
    return text[start:end]


def _required_edge_lines() -> list[str]:
    table = transitions.table_for(vigilant=False)
    lines = []
    for state in transitions.ALL_STATES:
        for edge in table.get(state, ()):
            lines.append(f"{state} --> {edge.target} : {edge.guard}")
    return lines


def test_markers_present_exactly_once_in_the_diagram():
    text = ARCHITECTURE_DOC.read_text()
    assert text.count(rtd.BEGIN_MARKER) == 1
    assert text.count(rtd.END_MARKER) == 1
    assert text.index(rtd.BEGIN_MARKER) < text.index(rtd.END_MARKER)


def test_block_contains_a_mermaid_fence():
    block = _embedded_block_diagram_docs()
    assert "```mermaid" in block
    assert "stateDiagram-v2" in block
    assert block.count("```mermaid") == 1
    assert block.rstrip().endswith(rtd.END_MARKER)


def test_diagram_block_matches_the_generator_right_now():
    """The doc is not a hand copy: what's embedded must equal what the
    generator produces from the code, byte for byte."""
    generated = rtd.render().rstrip("\n")
    embedded = _embedded_block_diagram_docs()
    assert embedded == generated, (
        "docs/architecture.md's transition diagram block has drifted from "
        "src/ralphd/engine/transitions.py -- regenerate it with "
        "`python scripts/render_transition_diagram.py --write`"
    )


def test_diagram_render_is_idempotent():
    """Re-running the generator against the unchanged table leaves the
    diagram block byte-identical (criterion 2)."""
    first = rtd.render()
    second = rtd.render()
    assert first == second


def test_every_step_state_appears_in_the_diagram():
    rendered = rtd.render()
    for state in transitions.STEP_STATES:
        assert state in rendered, (
            f"state {state!r} is in transitions.STEP_STATES but missing "
            f"from the rendered FSM diagram"
        )


def test_every_non_vigilant_edge_appears_in_the_diagram():
    rendered = rtd.render()
    required = _required_edge_lines()
    assert required, "sanity: the non-vigilant table declared no edges at all"
    for line in required:
        assert line in rendered, (
            f"edge {line!r} is a declared non-vigilant transition but is "
            f"missing from the rendered FSM diagram"
        )


def test_review_cure_and_review_paused_edges_appear():
    """Named explicitly by the criterion: the two edges task 004 added for
    finding 4 must show up in the diagram, not just be table entries."""
    rendered = rtd.render()
    assert "review --> cure : rejected_curable_pass_available" in rendered
    assert "review --> paused : operator_input_needed" in rendered
    assert "rejected_last_approach" not in rendered


def test_staleness_is_caught_when_a_required_edge_is_missing():
    """Mutation case: a diagram block missing one required edge line must
    fail the same containment check that passes against the real,
    up-to-date doc -- otherwise the check above would be vacuous."""
    rendered = rtd.render()
    required = _required_edge_lines()
    victim = "review --> cure : rejected_curable_pass_available"
    assert victim in required
    assert victim in rendered

    stale = rendered.replace(victim, "")
    assert victim not in stale, "sanity: the victim line was not actually removed"

    with pytest.raises(AssertionError):
        for line in required:
            assert line in stale


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))

# ===========================================================================
# From tests/test_transition_executor_cutover.py (task 014)
# ===========================================================================

LOOP_PATH = REPO_ROOT / "src" / "ralphd" / "engine" / "loop.py"


# ---------------------------------------------------------------------------
# scaffolding (same shape as tests/test_worker_hat.py)
# ---------------------------------------------------------------------------

def _ok_cutover(text: str = "working") -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 30.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


class _ScriptedAgent_cutover:
    def __init__(self, sup: LoopSupervisor, handlers: dict):
        self.sup = sup
        self.handlers = handlers
        self.phases: list[str] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        phase = self.sup.run.read_status().get("phase")
        self.phases.append(phase)
        self.running = True
        try:
            return self.handlers[phase](self.sup, prompt)
        finally:
            self.running = False


_supervisor_cutover = supervisor_with_runner(
    lambda sup, handlers: _ScriptedAgent_cutover(sup, handlers),
    run_id="task014-cutover", iterations=15, max_approaches=2, vigilant=True, on_complete="exit",
    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
    infra_outage_budget_s=1000.0)


def _run(root: Path, handlers: dict, **cfg_kw) -> tuple[LoopSupervisor, str]:
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor_cutover(root, handlers, **cfg_kw)
    return sup, asyncio.run(sup._run_job_core())


def _plan_cutover(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "001", "title": "do the thing",
         "successCriteria": "the thing is done"},
    ]})
    return _ok_cutover("planned\n" +
              json.dumps({"proposal_path": "plan-proposal.json", "summary": "planned"}))


def _complete_task(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    # Task 015 (requirement O, #100): report the harness-owned `complete`
    # verdict for the task the scheduler assigned, instead of writing
    # tasks.json directly and ending with the now-retired
    # `<promise>COMPLETE</promise>` whole-plan marker. With only one task in
    # this fixture's plan, `_apply_worker_completion_report` applying this
    # single move already leaves `_outstanding_for_completion` empty, so
    # the SAME iteration's `applied_this_iteration` branch (task 116) emits
    # `signal: "COMPLETE"` and ends the worker loop -- no separate
    # `plan_complete` iteration is needed for a one-task plan.
    doc = sup.run.read_tasks()
    task_id = doc["tasks"][0]["id"]
    return _ok_cutover("done\n" + json.dumps({"verdict": "complete", "task_id": task_id}))


def _verify_pass(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    tid = sup.run.read_tasks()["tasks"][0]["id"]
    r = _ok_cutover(json.dumps({"task_id": tid, "verdict": "verified"}))
    return r


def _review_verified(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    # Task 119 (#60 pillar 3, fact 9): review reports through the `review`
    # JSON contract, not the retired `<promise>VERIFIED</promise>` sentinel.
    return _ok_cutover("looks right\n" + json.dumps({"verdict": "verified"}))


def _happy_handlers() -> dict:
    return {"planning": _plan_cutover, "worker": _complete_task,
            "verify": _verify_pass, "review": _review_verified}


# ---------------------------------------------------------------------------
# 1. illegal transition refused BY THE EXECUTOR, not by reading the table back
# ---------------------------------------------------------------------------

def test_executor_refuses_an_edge_the_table_does_not_list():
    executor = tr.TransitionExecutor(vigilant=True)
    # "worker" has no successor named "reflect" in either variant.
    with pytest.raises(tr.IllegalTransition):
        executor.advance("worker", "reflect", "made_up_guard")
    # A legal edge is unaffected.
    assert executor.advance("planning", "worker", "planning_produced_tasks") == "worker"


def test_a_buggy_step_handler_proposing_an_illegal_target_fails_the_run_loudly(tmp_path):
    """Not a unit test of the checker function -- this drives the REAL
    dispatch loop in `_run_job_core` with one step handler patched to
    propose an edge the table forbids, and observes the refusal happen at
    runtime, the same way any other engine bug is contained."""
    sup, _ = None, None
    root = tmp_path / "run"
    root.mkdir()
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor_cutover(root, _happy_handlers())

    async def bogus_step_worker():
        # "aborted" is a RUN_TERMINAL_STATE; nothing in the table lists it
        # as a successor of "worker".
        return ("advance", "aborted", "not_a_real_guard")

    sup._step_worker = bogus_step_worker  # type: ignore[method-assign]
    state = asyncio.run(sup._run_job_core())
    assert state == "failed"
    status = sup.run.read_status()
    assert "illegal transition" in (status.get("reason") or "")
    assert "'worker' -> 'aborted'" in (status.get("reason") or "")


# ---------------------------------------------------------------------------
# 2. the two modes differ ONLY by which table variant the executor uses
# ---------------------------------------------------------------------------

def test_loopsupervisor_builds_its_executor_from_cfg_vigilant_alone(tmp_path):
    root = tmp_path / "run"
    run = RunDir(root=root)
    vig = LoopSupervisor(JobConfig(run_id="v", iterations=1, vigilant=True,
                                   infra_retry_backoff_s=[0.0],
                                   infra_retry_backoff_max_s=0.0), run, root)
    plain = LoopSupervisor(JobConfig(run_id="p", iterations=1, vigilant=False,
                                     infra_retry_backoff_s=[0.0],
                                     infra_retry_backoff_max_s=0.0),
                           run, root)
    assert vig._executor.vigilant is True
    assert plain._executor.vigilant is False
    # Same class, same module-level table -- not two hand-written executors.
    assert type(vig._executor) is type(plain._executor) is tr.TransitionExecutor


def test_step_worker_reads_cfg_vigilant_exactly_once():
    """The only place `_step_worker` is allowed to branch on `self.cfg.vigilant`
    is the one guard deciding whether to attempt the verify sub-cycle at all
    (verify is unreachable in the non-vigilant table anyway -- attempting the
    edge there would simply be refused by the executor). If a second,
    hand-written vigilant/non-vigilant fork creeps in, this fails."""
    tree = ast.parse(LOOP_PATH.read_text())
    step_worker = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_step_worker")
    hits = [
        node for node in ast.walk(step_worker)
        if isinstance(node, ast.Attribute) and node.attr == "vigilant"
    ]
    assert len(hits) == 1, (
        f"expected exactly one `.vigilant` read in _step_worker, found "
        f"{len(hits)}")


@pytest.mark.parametrize("vigilant", [True, False])
def test_a_full_run_takes_the_declared_edges_for_its_variant(tmp_path, vigilant):
    handlers = _happy_handlers()
    if not vigilant:
        del handlers["verify"]
    root = tmp_path / f"run-{vigilant}"
    sup, state = _run(root, handlers, vigilant=vigilant, max_approaches=1)
    assert state == "succeeded"
    agent = sup.runner
    if vigilant:
        assert "verify" in agent.phases
    else:
        assert "verify" not in agent.phases


# ---------------------------------------------------------------------------
# 3. no legacy dual path: the old imperative shape is gone from the source
# ---------------------------------------------------------------------------

def test_run_job_core_no_longer_contains_the_old_imperative_shape():
    tree = ast.parse(LOOP_PATH.read_text())
    run_job_core = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_job_core")
    # The pre-cutover body was a `for approach in range(...)` loop with
    # nested `while` phase loops inside it. Post-cutover it is a plain
    # `while True:` dispatch over `step`, calling out to `_step_*` handlers.
    for_loops = [n for n in ast.walk(run_job_core) if isinstance(n, ast.For)]
    assert for_loops == [], (
        "_run_job_core still contains a `for` loop -- the approach "
        "boundary must be a table transition (_step_replan), not a loop "
        "construct here")
    while_loops = [n for n in ast.walk(run_job_core) if isinstance(n, ast.While)]
    assert len(while_loops) == 1, (
        f"_run_job_core should hold exactly one dispatch `while True:`, "
        f"found {len(while_loops)}")


def test_run_job_core_dispatches_on_every_step_state():
    src = LOOP_PATH.read_text()
    core_start = src.index("async def _run_job_core")
    core_end = src.index("\n    def _advance(")
    body = src[core_start:core_end]
    for state in ("planning", "replan", "worker", "review"):
        assert f'step == "{state}"' in body, (
            f"_run_job_core's dispatch loop no longer names {state!r}")


def test_the_executor_is_the_only_thing_that_moves_state_in_run_job_core():
    src = LOOP_PATH.read_text()
    core_start = src.index("async def _run_job_core")
    core_end = src.index("\n    def _advance(")
    body = src[core_start:core_end]
    # The only assignment to `step` after the initial ENTRY_STATE seed is
    # `step = self._advance(...)`.
    assignments = [
        line.strip() for line in body.splitlines()
        if line.strip().startswith("step = ") or line.strip().startswith("step: ")
    ]
    non_entry = [a for a in assignments if "ENTRY_STATE" not in a]
    assert non_entry, "expected at least one non-entry assignment to `step`"
    assert all(a == "step = self._advance(step, target, guard)" for a in non_entry), (
        f"a `step` assignment bypasses `self._advance()`: {non_entry}")


def test_loop_line_count_delta_is_recorded_honestly():
    """Not a pass/fail gate on the number itself (the PRD asks that the
    delta be RECORDED in the commit message, not that it shrink) -- this
    just pins the before/after figures so the commit message's claim is
    checkable against the tree it describes."""
    before = 2886  # git show 866a671:src/ralphd/engine/loop.py | wc -l
    after = len(LOOP_PATH.read_text().splitlines())
    assert after > 0
    # Sanity: the file actually changed size (it did -- more, smaller
    # methods with their own docstrings replaced one large one).
    assert after != before

# ===========================================================================
# From tests/test_rejected_incurable_edge.py (task 006, requirement B, #111)
# ===========================================================================

def _finding(severity: str, curable: bool, clause: str, evidence: str) -> dict:
    return {"severity": severity, "curable": curable, "clause": clause,
            "evidence": evidence}


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


# Task 014 (PRD requirement A.1, #224): `cfg_defaults=False` here means
# the bare `JobConfig` default applies, and that default is now `True` for
# `operator_input_needed_pause` -- explicitly off, so this module's own
# subject (the `rejected_incurable`/`rejected_no_approach_left` edges, not
# task 014's gate) still reaches exactly the ordinary archive/replan/tail
# outcomes it always did; task 014's own edge has its own module
# (`tests/test_operator_input_needed_pause.py`).
_supervisor_rejected_incurable = supervisor_factory(run_id="task006-rejected-incurable", cfg_defaults=False,
                                 status={"state": "running"},
                                 operator_input_needed_pause=False)


class _StubNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, text: str) -> None:
        self.messages.append(text)


# ---------------------------------------------------------------------------
# 1. The declared edge itself.
# ---------------------------------------------------------------------------

def test_the_edge_is_declared_in_both_variants():
    """v0.36 requirement B: an incurable rejection no longer has an edge of
    its own (`review -> review` on `rejected_incurable`); it is a trigger of
    the one ladder park, `review -> paused` on `operator_input_needed`.
    Covering node for the park itself: `tests/test_rejection_ladder.py::
    test_step_4a_an_incurable_blocking_finding_parks`."""
    for vigilant in (True, False):
        guards = transitions.guard_names("review", vigilant)
        assert "rejected_incurable" not in guards
        by_guard = {e.guard: e.target for e in
                   transitions.successors("review", vigilant)}
        assert by_guard["operator_input_needed"] == "paused"


def test_the_edge_is_also_declared_under_the_peer_verdict_gate_delta():
    edges = transitions.successors("review", vigilant=False,
                                   peer_verdict_gate=True)
    by_guard = {e.guard: e.target for e in edges}
    assert "rejected_incurable" not in by_guard
    assert by_guard["operator_input_needed"] == "paused"


def test_the_review_contract_unrepaired_edge_is_declared_in_both_variants_and_under_the_gate():
    """GH #209 defect 2's hold edge, in the same two-assertion shape the
    two nodes above use for `rejected_incurable`: an exhausted review
    contract ladder that never got a readable verdict claim out of the hat
    holds on `review -> paused` rather than scoring a rejection, and the
    edge is declared in BOTH table variants and identically under the peer
    verdict gate delta (the check runs on the way to the rejection branch,
    entirely before the VERIFIED-only gate is ever consulted)."""
    for vigilant in (True, False):
        by_guard = {e.guard: e.target for e in
                   transitions.successors("review", vigilant)}
        assert by_guard["review_contract_unrepaired"] == "paused"
    gated = {e.guard: e.target for e in
            transitions.successors("review", vigilant=False,
                                   peer_verdict_gate=True)}
    assert gated["review_contract_unrepaired"] == "paused"


# ---------------------------------------------------------------------------
# 2-4. Driving a real LoopSupervisor to the edge.
# ---------------------------------------------------------------------------

def test_all_incurable_blocking_findings_pause_and_escalate(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor_rejected_incurable(root, max_approaches=3,
                                         operator_input_needed_pause=True)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    all_incurable = [
        _finding("blocking", False, "requirement B (#111)",
                "the endpoint has no docker.sock in this container"),
        _finding("blocking", False, "requirement D (#99)",
                "the deadline already passed before this approach started"),
    ]
    findings.write_findings_json(sup.run.findings_json_file, all_incurable)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected",
            "reason": "both blocking findings are environment/temporal "
                      "facts a fresh approach cannot change",
            "findings_path": "review-findings.md",
        }))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    assert sup._pause.is_set() is True  # not paused yet

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "paused", "operator_input_needed")
    # the run is paused through the EXISTING pause machinery -- the same
    # asyncio.Event POST /pause's loop.pause() clears.
    assert sup._pause.is_set() is False

    # the operator_escalation event's own `escalation` field keeps the
    # FULL, un-digested text -- every finding's clause AND evidence, plus
    # the exact ralphctl amend invocation for each.
    escalations = [e for e in _events(root)
                  if e.get("type") == "operator_escalation"]
    assert len(escalations) == 1
    full_text = escalations[0]["escalation"]
    for f in all_incurable:
        assert f["clause"] in full_text
        assert f["evidence"] in full_text

    # The exact invocations, spelled out here as literals (NOT derived from
    # `loop._amend_invocation_for_finding`, which would make this test a
    # tautology): `ralphctl amend <run_id> <clause-slug> --waive <reason>`,
    # where the slug lowercases the clause and collapses every run of
    # non-alphanumerics to a single `-`, and the reason (task 002, #269
    # follow-on) is the shell-quoted `"F<index>: <clause>"` pair -- the
    # finding's own id (matching the `Finding <index> (...)` header above
    # it) plus its clause, never the evidence (already shown once on the
    # finding's own `evidence:` line, not repeated here).
    expected_commands = [
        "ralphctl amend task006-rejected-incurable requirement-b-111 "
        "--waive 'F1: requirement B (#111)'",
        "ralphctl amend task006-rejected-incurable requirement-d-99 "
        "--waive 'F2: requirement D (#99)'",
    ]
    # Each command appears verbatim, exactly once, on its own
    # `resolve with:` line -- and the set of such lines is EXACTLY these
    # two, in finding order, so an extra/misordered/malformed invocation
    # fails here too.
    resolve_lines = [line for line in full_text.splitlines()
                    if line.strip().startswith("resolve with:")]
    assert resolve_lines == [f"  resolve with: {c}" for c in expected_commands]
    for command in expected_commands:
        assert full_text.count(command) == 1
    # No other `ralphctl amend` invocation hides anywhere in the body.
    assert full_text.count("ralphctl amend") == len(expected_commands)

    # Argument-by-argument, as a shell would parse it: subcommand, run id,
    # clause slug, the `--waive` flag, then ONE reason argument carrying
    # the finding's id and its clause, unquoted -- and NEVER its evidence.
    for index, (command, finding) in enumerate(
            zip(expected_commands, all_incurable), start=1):
        argv = shlex.split(command)
        assert argv[:2] == ["ralphctl", "amend"]
        assert argv[2] == "task006-rejected-incurable"
        assert argv[3] == finding["clause"].lower().replace(
            " ", "-").replace("(#", "").replace(")", "").replace("#", "")
        assert argv[4] == "--waive"
        assert argv[5] == f"F{index}: {finding['clause']}"
        assert finding["evidence"] not in argv[5]
        assert len(argv) == 6

    # never archived, never composited into a fresh approach's PRD -- the
    # SAME approach is what review is asked about again on resume.
    assert not (root / "approaches" / "01").exists()
    assert sup._approach == 1

    # Cure task cure-01-02 (PRD requirement A.1/A.2, #269): the notifier
    # itself only ever gets the bounded, EVIDENCE-FREE digest -- each
    # finding's clause and id, never its evidence, and no `resolve with:`
    # line at all (the digest never carries one) -- plus both live-run
    # levers, since this pause waits on an operator ruling.
    assert len(notifier.messages) == 1
    digest_text = notifier.messages[0]
    assert "blocking: 2" in digest_text and "residual: 0" in digest_text
    for f in all_incurable:
        assert f["clause"] in digest_text
        assert f["evidence"] not in digest_text
    assert "resolve with:" not in digest_text
    assert "ralphctl amend" not in digest_text
    assert "ralphctl answer task006-rejected-incurable" in digest_text
    assert "ralphctl unpause task006-rejected-incurable" not in digest_text


def test_the_escalation_is_also_emitted_to_events_jsonl_with_no_notifier(tmp_path):
    """Even with no notifier wired in (production's default), the
    escalation is never silently lost -- it lands in events.jsonl too."""
    root = tmp_path / "run"
    sup = _supervisor_rejected_incurable(root, max_approaches=3,
                                         operator_input_needed_pause=True)
    sup._approach = 1
    assert sup.notifier is None

    incurable = [_finding("blocking", False, "requirement B (#111)",
                          "temporal miss")]
    findings.write_findings_json(sup.run.findings_json_file, incurable)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())
    assert outcome == ("advance", "paused", "operator_input_needed")
    assert sup._pause.is_set() is False

    events = [json.loads(l) for l in
             (root / "events.jsonl").read_text().splitlines()]
    escalations = [e for e in events if e.get("type") == "operator_escalation"]
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "operator_input_needed"
    assert escalations[0]["findings"][0]["clause"] == "requirement B (#111)"


# ---------------------------------------------------------------------------
# 5. A curable blocking finding still replans; approach exhaustion still
#    wins over an all-incurable rejection.
# ---------------------------------------------------------------------------

def test_a_curable_blocking_finding_still_takes_the_ordinary_replan_edge(tmp_path):
    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    (root / "prd.md").write_text("# test PRD\n\nDo the thing.\n")
    sup = _supervisor_rejected_incurable(root, max_approaches=3)
    sup._approach = 1
    sup.notifier = _StubNotifier()

    mixed_curability = [
        _finding("blocking", False, "requirement B (#111)", "incurable one"),
        {**_finding("blocking", True, "requirement D (#99)", "curable one"),
         "cured_when": "the curable finding is repaired"},
    ]
    findings.write_findings_json(sup.run.findings_json_file, mixed_curability)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "mixed"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "replan", "rejected_approach_left")
    assert sup._pause.is_set() is True  # never paused
    assert sup.notifier.messages == []  # never escalated
    assert (root / "approaches" / "01").exists()  # archived as usual
    # DoD predicate 5 (#111): the ordinary replan edge also writes the
    # composite PRD for the next approach to work from -- a curable
    # blocking finding must not short-circuit that half either.
    assert sup.run.composite_prd_file.exists()
    assert "Do the thing." in sup.run.composite_prd_file.read_text()


def test_rejected_no_approach_left_still_wins_when_approaches_are_exhausted(tmp_path):
    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    (root / "prd.md").write_text("# test PRD\n\nDo the thing.\n")
    sup = _supervisor_rejected_incurable(root, max_approaches=1)  # nothing left after this one
    sup._approach = 1
    sup.notifier = _StubNotifier()

    all_incurable = [_finding("blocking", False, "requirement B (#111)",
                              "incurable finding")]
    findings.write_findings_json(sup.run.findings_json_file, all_incurable)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())

    # the approach budget is spent -- `rejected_no_approach_left` wins over
    # `rejected_incurable` even though every blocking finding is incurable.
    assert outcome == ("tail",)
    assert sup._pause.is_set() is True  # never paused
    assert sup.notifier.messages == []  # never escalated
    status = sup.run.read_status()
    assert status.get("state") != "succeeded"

# ===========================================================================
# From tests/test_steer_hat_table_entry.py (task 009 (v0.14), requirement G, #98, #60 pillar 4)
# ===========================================================================

STEER_PROMPT = REPO_ROOT / "src" / "ralphd" / "prompts" / "steer.md"


# ---------------------------------------------------------------------------
# 1. `steer` is a declared table entry, reachable only through the declared
#    edge, and an undeclared target is refused like every other state.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vigilant", [True, False])
def test_steer_is_in_the_state_inventory(vigilant):
    assert "steer" in tr.ALL_STATES
    succ = tr.successors("steer", vigilant)
    assert succ != (), "steer must not be a dead end -- it must return control"
    assert all(edge.target in tr.ALL_STATES for edge in succ)


@pytest.mark.parametrize("vigilant", [True, False])
def test_worker_has_a_declared_edge_into_steer(vigilant):
    assert tr.is_legal("worker", "steer", vigilant)
    edges = tr.successors("worker", vigilant)
    steer_edges = [e for e in edges if e.target == "steer"]
    assert len(steer_edges) == 1
    assert steer_edges[0].guard == "steering_pending"


@pytest.mark.parametrize("vigilant", [True, False])
def test_steer_returns_control_to_worker_via_a_declared_edge(vigilant):
    assert tr.is_legal("steer", "worker", vigilant)
    edges = tr.successors("steer", vigilant)
    assert edges == (tr.T("worker", "steer_settled"),)


@pytest.mark.parametrize("vigilant", [True, False])
def test_an_undeclared_target_out_of_steer_is_refused(vigilant):
    assert not tr.is_legal("steer", "bogus_undeclared_target", vigilant)
    with pytest.raises(tr.IllegalTransition):
        tr.TransitionExecutor(vigilant).advance(
            "steer", "bogus_undeclared_target", "steer_settled")


@pytest.mark.parametrize("vigilant", [True, False])
def test_no_other_state_declares_an_edge_into_steer_except_worker(vigilant):
    """`steer` is reachable ONLY through the one declared `worker -> steer`
    edge -- proves no other state's table entry accidentally targets it."""
    table = tr.table_for(vigilant)
    sources = {state for state, edges in table.items()
               if any(edge.target == "steer" for edge in edges)}
    assert sources == {"worker"}


# ---------------------------------------------------------------------------
# 2. STATE_HAT / HATS / HAT_SCHEMAS entries
# ---------------------------------------------------------------------------

def test_state_hat_entry():
    assert tr.STATE_HAT["steer"] == "steer"


def test_steer_is_in_hats():
    assert "steer" in tr.HATS


def test_hat_schemas_entry_shape():
    schema = tr.HAT_SCHEMAS["steer"]
    assert schema["type"] == "object"
    assert schema["required"] == ["settled"]
    assert schema["properties"]["settled"]["type"] == "boolean"
    assert schema["additionalProperties"] is False


def test_every_hat_including_steer_has_exactly_one_schema():
    assert contracts.hats_with_schemas() == frozenset(tr.HATS)
    assert "steer" in contracts.hats_with_schemas()


def test_a_settled_true_payload_validates():
    errors = contracts.validate_payload("steer", {"settled": True})
    assert errors == []


def test_a_settled_false_payload_with_notes_validates():
    errors = contracts.validate_payload(
        "steer", {"settled": False, "notes": "could not parse the message"})
    assert errors == []


def test_a_payload_missing_settled_is_rejected():
    errors = contracts.validate_payload("steer", {"notes": "no settled field"})
    assert errors


def test_an_extra_property_is_rejected():
    errors = contracts.validate_payload(
        "steer", {"settled": True, "task_status": "completed"})
    assert errors


# ---------------------------------------------------------------------------
# 3. The prompt exists and says the hat reshapes state and writes no code.
# ---------------------------------------------------------------------------

def test_steer_prompt_exists():
    assert STEER_PROMPT.is_file(), f"missing {STEER_PROMPT}"


def test_steer_prompt_says_it_reshapes_state_and_writes_no_code():
    text = STEER_PROMPT.read_text()
    lowered = text.lower()
    assert "reshape" in lowered
    assert "write no code" in lowered or "you write no code" in lowered
    # never claims task/verify/review-owned statuses for itself either
    assert "you write no code" in lowered or "writes no code" in lowered


def test_steer_prompt_declares_the_settled_contract():
    text = STEER_PROMPT.read_text()
    assert '"settled"' in text
    assert "done" in text.lower()


# ---------------------------------------------------------------------------
# 4. `config.MODEL_HATS` gains a REAL `steer` entry with a shipped default
#    -- never resolving through `UNRECOGNISED_HAT_FALLBACK` -- and tier
#    rungs via `tiers.all_hat_tier_pairs()`.
# ---------------------------------------------------------------------------

def test_steer_is_a_real_model_hats_entry():
    assert "steer" in MODEL_HATS
    assert "steer" != UNRECOGNISED_HAT_FALLBACK


def test_steer_shipped_default_matches_every_other_known_hat():
    cfg = JobConfig()
    assert cfg.model_for("steer") is DEFAULT_HAT_MODEL_REF is None
    cfg2 = JobConfig(model="shipped-model")
    assert cfg2.model_for("steer") == "shipped-model"


def test_steer_never_resolves_through_the_unrecognised_hat_fallback():
    """A `planning`-only configured table must NOT leak onto `steer` the
    way it would for a genuinely unrecognised (typo'd) hat name -- `steer`
    is one of `model_for`'s own known hats, so it falls through to the
    bare `model` field, never to `planning`'s entry."""
    cfg = JobConfig(model="strong-default", models={"planning": "opus"})
    # the anti-downgrade path a TYPO would take:
    assert cfg.model_for("totally-bogus-hat") == "opus"
    # steer is real, so it does NOT take that path:
    assert cfg.model_for("steer") == "strong-default"


def test_steer_has_tier_rungs_via_all_hat_tier_pairs():
    pairs = T.all_hat_tier_pairs()
    steer_pairs = {(h, t) for h, t in pairs if h == "steer"}
    assert steer_pairs == {("steer", tier) for tier in T.TIERS}


def test_steer_host_map_widens_cleanly_with_nothing_configured():
    cfg = JobConfig(model="shipped-model")
    host_map = T.default_host_map(cfg)
    assert "steer" in host_map
    for tier in T.TIERS:
        assert host_map["steer"][tier].model == "shipped-model"


def test_steer_resolves_through_resolve_bundles_pure_signature():
    """`resolve_bundle` is the single pure resolver task 016 pins to one
    call site in `loop.py` (`tests/test_tier_dispatch.py::
    test_resolver_has_exactly_one_call_site_in_src`) -- this only proves
    `steer` participates in that SAME function, not a second pathway."""
    cfg = JobConfig(model="shipped-model")
    host_map = T.default_host_map(cfg)
    bundle = T.resolve_bundle("steer", "normal", host_map)
    assert bundle.model == "shipped-model"


def test_steer_configured_tier_rung_resolves_through_the_same_resolver():
    cfg = JobConfig(model_tiers={"steer": {"hard": {"model": "steer-hard-ref"}}})
    host_map = T.default_host_map(cfg)
    bundle = T.resolve_bundle("steer", "hard", host_map)
    assert bundle.model == "steer-hard-ref"

# ===========================================================================
# From tests/test_doomed_plan_fault.py (task 007 (v0.14), requirement D, #99)
# ===========================================================================

TASK_1_DOOMED = "001"
TASK_2_DOOMED = "002"
TASK_3 = "003"


# -- scaffolding (mirrors tests/test_unsatisfiable_criteria.py) -----------


def _ok_doomed(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _no_progress(sup, prompt="") -> IterationResult:
    return _ok_doomed("I looked at this and made no progress")


def _review_rejects(sup, prompt="") -> IterationResult:
    # GH #209 defect 2: this used to reply with the bare prose "this plan
    # cannot be verified; rejecting", which reached the ordinary rejection
    # path only because an exhausted review contract ladder was SCORED as a
    # rejection. It no longer is (the run holds on `review_contract_
    # unrepaired` instead), and this section's subject is the doomed-plan
    # fault, not review's contract handling -- so the reply is now the
    # compliant `review` payload it always meant, and every assertion in
    # both nodes that use it is unchanged.
    return _ok_doomed(json.dumps(
        {"verdict": "rejected", "reason": "this plan cannot be verified"}))



_supervisor_doomed = scripted_supervisor(run_id="v014-doomed-plan", iterations=15,
                                  max_approaches=1, vigilant=False,
                                  on_complete="exit",
                                  infra_outage_budget_s=1000.0)



def _run_resumed(root: Path, handlers: dict, tasks: list[dict],
                 **cfg_kw) -> tuple[LoopSupervisor, str]:
    """These seeded tasks bypass planning entirely (a real
    plan-proposal.json contract can never carry a task's own `status`), the
    same resumed-run shape `tests/test_unsatisfiable_criteria.py` uses.
    `handlers` must have no `"planning"` key."""
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor_doomed(root, handlers, **cfg_kw)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})
    sup.iterations_used = 1
    return sup, asyncio.run(sup._run_job_core())


async def _drive_until_paused(sup: LoopSupervisor) -> None:
    """Task 012 (requirement B.3, #153): a `_run_job_core()` that hits the
    stagnation guard on the LAST approach now pauses and escalates instead
    of ending the run `failed` -- and stays parked at `_gate()` forever
    without an operator's `resume()`, which none of these scripted-runner
    tests ever send. Starts `_run_job_core()` as a background task, polls
    `status.json` for `state == "paused"`, then cancels the still-pending
    task so the test's own event loop can close cleanly."""
    task = asyncio.ensure_future(sup._run_job_core())
    for _ in range(500):
        if sup.run.read_status().get("state") == "paused":
            break
        if task.done():
            break
        await asyncio.sleep(0.01)
    else:
        task.cancel()
        raise AssertionError("run never reached `paused`")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _events(root: Path) -> list[dict]:
    p = root / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _fault_events(root: Path) -> list[dict]:
    return [ev for ev in _events(root)
            if ev.get("type") == "log" and "doomedTaskIds" in ev]


def _phases_recorded(root: Path) -> list[str]:
    return [ev.get("phase") for ev in _events(root) if ev.get("type") == "phase"]


# -- the table declares the new edge ---------------------------------------


def test_the_table_declares_the_doomed_plan_edge():
    guards = transitions.guard_names("worker", vigilant=False)
    assert "doomed_plan_unverifiable" in guards
    assert transitions.is_legal("worker", "review", vigilant=False)
    # the vigilant variant inherits the same edge (VIGILANT_DELTA composes
    # on top of TABLE["worker"], never replaces it).
    assert "doomed_plan_unverifiable" in transitions.guard_names(
        "worker", vigilant=True)


# -- the pure helper in isolation -------------------------------------------


def _bare_sup(tmp_path: Path) -> LoopSupervisor:
    root = tmp_path / "bare"
    return _supervisor_doomed(root, {})


def test_helper_returns_none_for_a_doomed_subset_with_actionable_work(tmp_path):
    sup = _bare_sup(tmp_path)
    tasks = [
        {"id": TASK_1_DOOMED, "status": "pending"},
        {"id": TASK_2_DOOMED, "status": "failed"},
        {"id": TASK_3, "status": "pending", "dependsOn": [TASK_2_DOOMED]},
    ]
    blocked_by = task_blocked_by(tasks)
    assert blocked_by == {TASK_3: [TASK_2_DOOMED]}
    assert sup._doomed_plan_fault({"tasks": tasks}, blocked_by) is None


def test_helper_fires_when_the_whole_remainder_is_doomed_and_names_dead_ancestors(
        tmp_path):
    sup = _bare_sup(tmp_path)
    tasks = [
        {"id": TASK_2_DOOMED, "status": "failed"},
        {"id": TASK_3, "status": "pending", "dependsOn": [TASK_2_DOOMED]},
    ]
    blocked_by = task_blocked_by(tasks)
    fault = sup._doomed_plan_fault({"tasks": tasks}, blocked_by)
    assert fault == {
        "doomedTaskIds": [TASK_3],
        "deadAncestorIds": [TASK_2_DOOMED],
        "planUnverifiable": True,
    }


def test_helper_returns_none_with_no_doomed_tasks_at_all(tmp_path):
    sup = _bare_sup(tmp_path)
    tasks = [{"id": TASK_1_DOOMED, "status": "pending"}]
    assert sup._doomed_plan_fault({"tasks": tasks}, {}) is None


# -- the whole run, both directions -----------------------------------------


def test_a_doomed_subset_with_actionable_work_left_behaves_exactly_as_today(
        tmp_path):
    """Task 001 is genuinely actionable (no `dependsOn`) and never finishes;
    task 003 is a doomed SUBSET (blocked on failed task 002). This must
    behave byte-for-byte like the pre-existing stagnation path: the fault
    never fires, the worker hat is actually called (three times, the
    ordinary stagnation count), and the run pauses through the ordinary
    last-approach stagnation guard (task 012, requirement B.3), not
    through the new doomed-plan guard."""
    root = tmp_path / "run"
    tasks = [
        {"id": TASK_1_DOOMED, "status": "pending", "title": "keep going"},
        {"id": TASK_2_DOOMED, "status": "failed", "title": "already dead"},
        {"id": TASK_3, "status": "pending", "title": "doomed",
         "dependsOn": [TASK_2_DOOMED]},
    ]

    sup = _supervisor_doomed(root, {
        "worker": [_no_progress],
        "review": [_review_rejects],
    })
    RunDir(root=root).update_status(state="starting")
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})
    sup.iterations_used = 1
    asyncio.run(_drive_until_paused(sup))

    status = sup.run.read_status()
    assert status.get("state") == "paused", status
    assert status.get("pauseReason") == "stagnation"
    assert sup.runner.calls["worker"] == 3        # type: ignore[attr-defined]
    assert not _fault_events(root), \
        "a doomed SUBSET with actionable work left must never raise the fault"
    assert "review" not in _phases_recorded(root), \
        "stagnation on the last approach pauses, it never routes to review"

    doc = sup.run.read_tasks()
    by_id = {t["id"]: t for t in doc["tasks"]}
    # task 001 was claimed `in-progress` on the first pass (the harness's
    # own pre-spawn bet, task 116) and stays there -- it never finished,
    # but it WAS worked on, unlike task 003, which was never even claimed.
    assert by_id[TASK_1_DOOMED]["status"] == "in-progress"
    assert by_id[TASK_3]["status"] == "pending"

    # the pre-existing per-task blocked-notes mechanism (task 021/#60) is
    # untouched by this task: task 003's doom is still noted, exactly as
    # before -- this task only adds a NEW behaviour on top for the wholly
    # doomed case, it does not remove the old one for the subset case.
    # (Not asserted strictly here since task 001 is always selectable and
    # `selected` therefore never reads `None` in this fixture -- the point
    # this test pins is that nothing NEW happens, not that the notes path
    # specifically fires.)


def test_a_wholly_doomed_remainder_raises_the_fault_instead_of_stagnating(
        tmp_path):
    """The only task left is doomed: the fault fires on the very FIRST
    pass, before the worker hat is ever dispatched, and routes straight to
    review -- not three silent no-progress iterations and a stagnation
    verdict."""
    root = tmp_path / "run"
    tasks = [
        {"id": TASK_2_DOOMED, "status": "failed", "title": "already dead"},
        {"id": TASK_3, "status": "pending", "title": "doomed",
         "dependsOn": [TASK_2_DOOMED]},
    ]

    sup, state = _run_resumed(root, {
        "review": [_review_rejects],
    }, tasks)

    assert sup.runner.calls.get("worker", 0) == 0, \
        "no worker iteration could ever change anything here -- none must run"
    fault_events = _fault_events(root)
    assert len(fault_events) == 1
    fault = fault_events[0]
    assert fault["doomedTaskIds"] == [TASK_3]
    assert fault["deadAncestorIds"] == [TASK_2_DOOMED]
    assert fault["planUnverifiable"] is True
    assert "review" in _phases_recorded(root)
    assert sup.runner.calls.get("review", 0) >= 1  # type: ignore[attr-defined]

    doc = sup.run.read_tasks()
    task = next(t for t in doc["tasks"] if t["id"] == TASK_3)
    assert task["status"] == "pending", \
        "the doomed task's own record is untouched by the fault"

    # rejected with no approach left -> the run ends, not stuck retrying.
    assert state in ("failed", "aborted"), sup.run.read_status()

# ===========================================================================
# From tests/test_plan_review_resume_issue161.py (task 046, #161)
# ===========================================================================

TASK_1_161 = "001"
TASK_2_161 = "002"


# ---------------------------------------------------------------------------
# scripted-agent fixtures (same shape as tests/test_peer_plan_review_gate.py)
# ---------------------------------------------------------------------------


def _ok_161(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _interrupted() -> IterationResult:
    r = IterationResult(exit_code=-2)
    r.interrupted = True
    r.final_text = ""
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 0, "totalTokens": 1}
    return r


def _plan_161(*task_ids: str):
    ids = task_ids or (TASK_1_161,)

    def handler(sup, prompt=""):
        atomic_write_json(sup.run.root / "plan-proposal.json", {
            "version": 1,
            "tasks": [{"id": t, "title": f"t{t}", "successCriteria": "c"}
                      for t in ids],
        })
        return _ok_161(json.dumps(
            {"proposal_path": "plan-proposal.json", "summary": "planned"}))
    return handler


def _plan_review_approves(sup, prompt=""):
    return _ok_161(json.dumps({"verdict": "approved"}))


def _plan_review_interrupted(sup, prompt=""):
    return _interrupted()


def _plan_review_objects(reason: str):
    def handler(sup, prompt=""):
        return _ok_161(json.dumps({"verdict": "objected", "reason": reason}))
    return handler


def _worker_completes(task_id: str):
    def handler(sup, prompt=""):
        return _ok_161(json.dumps({"verdict": "complete", "task_id": task_id}))
    return handler


def _worker_completes_assigned(sup, prompt=""):
    """Complete the task the harness hands out (first `in-progress`, else
    first `pending`, in list order) -- the same order `_step_worker`
    assigns by, so the reported id never trips the assigned-vs-reported
    mismatch guard."""
    tasks = sup.run.read_tasks()["tasks"]
    for wanted in ("in-progress", "pending"):
        for t in tasks:
            if t.get("status") == wanted:
                return _ok_161(json.dumps({"verdict": "complete",
                                       "task_id": t["id"]}))
    raise AssertionError("worker asked to run with nothing assignable")


def _review_verifies(sup, prompt=""):
    return _ok_161(json.dumps({"verdict": "verified"}))


class _ScriptedAgent_161:
    def __init__(self, handlers: dict):
        self.handlers = handlers
        self.calls: dict[str, int] = {}
        self.phases_seen: list[str] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        phase = self.sup.run.read_status().get("phase")
        self.phases_seen.append(phase)
        n = self.calls.get(phase, 0)
        self.calls[phase] = n + 1
        steps = self.handlers[phase]   # KeyError == "this phase must not run"
        self.running = True
        try:
            return steps[min(n, len(steps) - 1)](self.sup, prompt)
        finally:
            self.running = False


def _supervisor_161(root: Path, handlers: dict, **cfg):
    return make_supervisor(
        root, runner_factory=lambda sup: _ScriptedAgent_161(handlers),
        **{"run_id": "issue161-plan-review-resume", "iterations": 15,
           "max_approaches": 1, "vigilant": False, "on_complete": "exit",
           "infra_retry_backoff_s": [0.0], "infra_retry_backoff_max_s": 0.0,
           "infra_outage_budget_s": 1000.0, "peer_plan_review": True, **cfg})


def _run_fresh(root: Path, handlers: dict, **cfg):
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor_161(root, handlers, **cfg)
    return sup, asyncio.run(sup._run_job_core())


def _seed_resumable_run_dir(root: Path, *, task1_status: str,
                            approved_marker: bool) -> RunDir:
    """A run dir a prior episode left behind: a two-task plan, one
    completed iteration on disk (so `_resuming_existing_run_dir()` is
    true), status `aborted` at `worker`."""
    run = RunDir(root=root)
    root.mkdir(parents=True, exist_ok=True)
    run.write_tasks({
        "version": 1, "goal": "g", "scope": {}, "repositories": [],
        "tasks": [
            {"id": TASK_1_161, "title": "t001", "successCriteria": "c",
             "status": task1_status},
            {"id": TASK_2_161, "title": "t002", "successCriteria": "c",
             "status": "pending"},
        ],
        "discovered": {},
    })
    it = root / "iterations" / "0001"
    it.mkdir(parents=True)
    atomic_write_json(it / "meta.json", {
        "number": 1, "phase": "planning", "approach": 1,
        "startedAt": "2026-09-07T07:00:00Z", "endedAt": "2026-09-07T07:05:00Z",
        "exitCode": 0})
    status = {"state": "aborted", "approach": 1, "phase": "worker"}
    if approved_marker:
        status["planReviewApproved"] = {"1": True}
    run.update_status(**status)
    return run


def _statuses(run: RunDir) -> dict[str, str]:
    return {t["id"]: t["status"] for t in run.read_tasks()["tasks"]}


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vigilant", [True, False])
def test_the_two_161_edges_exist_only_with_the_gate_on(vigilant):
    on = tr.TransitionExecutor(vigilant, peer_plan_review=True)
    assert on.advance("planning", "worker",
                      "planning_skipped_plan_already_reviewed") == "worker"
    assert on.advance("plan_review", "worker",
                      "plan_review_revision_refused_work_recorded") == "worker"
    off_guards = {e.guard for e in tr.successors("planning", vigilant,
                                                  peer_plan_review=False)}
    assert "planning_skipped_plan_already_reviewed" not in off_guards
    assert tr.successors("plan_review", vigilant, peer_plan_review=False) == ()


# ---------------------------------------------------------------------------
# 1. resume over a worked / approved plan goes straight to worker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("task1_status,approved_marker", [
    ("validated", True),    # the ordinary post-fix case
    ("validated", False),   # a run dir from before the marker existed
    ("in-progress", False),
    ("pending", True),      # approved, killed before any work: still no re-ask
])
def test_resume_never_reenters_plan_review_over_a_settled_plan(
        tmp_path, task1_status, approved_marker):
    root = tmp_path / "run"
    run = _seed_resumable_run_dir(root, task1_status=task1_status,
                                  approved_marker=approved_marker)
    before = _statuses(run)
    handlers = {"worker": [_worker_completes_assigned],
                "review": [_review_verifies]}
    # NO "planning" and NO "plan_review" handler: reaching either phase
    # raises KeyError inside the scripted agent and fails the test.
    sup = _supervisor_161(root, handlers)
    final_state = asyncio.run(sup._run_job_core())

    agent = sup.runner
    assert "plan_review" not in agent.phases_seen
    assert "planning" not in agent.phases_seen
    assert agent.phases_seen[0] == "worker"
    assert final_state == "succeeded", sup.run.read_status()

    after = _statuses(run)
    if task1_status == "validated":
        assert after[TASK_1_161] == "validated", (before, after)
    assert after[TASK_2_161] != "pending"
    logs = [e for e in read_events(sup.run.root)
            if e.get("type") == "log" and "#161" in (e.get("message") or "")]
    assert logs, "the skip is logged so an operator can see why the peer was not re-asked"


# ---------------------------------------------------------------------------
# 2. resume over an all-pending, never-reviewed plan still runs the gate
# ---------------------------------------------------------------------------


def test_resume_over_an_unreviewed_all_pending_plan_still_runs_plan_review(
        tmp_path):
    root = tmp_path / "run"
    _seed_resumable_run_dir(root, task1_status="pending", approved_marker=False)
    handlers = {"plan_review": [_plan_review_approves],
                "worker": [_worker_completes_assigned],
                "review": [_review_verifies]}
    sup = _supervisor_161(root, handlers)
    final_state = asyncio.run(sup._run_job_core())

    agent = sup.runner
    assert agent.phases_seen[0] == "plan_review", agent.phases_seen
    assert "planning" not in agent.phases_seen
    assert final_state == "succeeded", sup.run.read_status()
    assert sup.run.read_status().get("planReviewApproved") == {"1": True}


# ---------------------------------------------------------------------------
# 3. an interrupted plan_review is re-asked, not read as an objection
# ---------------------------------------------------------------------------


def test_an_interrupted_plan_review_is_reasked_in_place_and_spends_no_revision(
        tmp_path):
    root = tmp_path / "run"
    sup, final_state = _run_fresh(
        root,
        {"planning": [_plan_161(TASK_1_161)],
         "plan_review": [_plan_review_interrupted, _plan_review_approves],
         "worker": [_worker_completes(TASK_1_161)],
         "review": [_review_verifies]})

    agent = sup.runner
    assert agent.phases_seen == [
        "planning", "plan_review", "plan_review", "worker", "review"]
    assert agent.calls["planning"] == 1, "no revision re-ran planning"
    assert final_state == "succeeded", sup.run.read_status()
    status = sup.run.read_status()
    assert status.get("planReviewRevisions") is None
    assert status.get("planReviewApproved") == {"1": True}


def test_a_twice_interrupted_plan_review_over_a_fresh_plan_falls_to_the_old_policy(
        tmp_path):
    """Two interruptions in a row on a plan with NO recorded work: the
    existing undecidable policy still applies (spend a revision, re-plan)
    -- #161 changes nothing there, since nothing can be wiped."""
    root = tmp_path / "run"
    sup, final_state = _run_fresh(
        root,
        {"planning": [_plan_161(TASK_1_161), _plan_161(TASK_1_161)],
         "plan_review": [_plan_review_interrupted, _plan_review_interrupted,
                         _plan_review_approves],
         "worker": [_worker_completes(TASK_1_161)],
         "review": [_review_verifies]})

    agent = sup.runner
    assert agent.calls["planning"] == 2
    assert agent.calls["plan_review"] == 3
    assert final_state == "succeeded", sup.run.read_status()
    assert sup.run.read_status().get("planReviewRevisions") == {"1": 1}


# ---------------------------------------------------------------------------
# 3b. v0.25 task 028c (PRD requirement K.6, #267): `--now` against a RUNNING
#     judging-hat iteration (planning/plan_review/review) keeps EXACTLY
#     today's behaviour and today's cost -- K introduces no new interrupt
#     semantics for these three hats. Driven through the REAL
#     `LoopSupervisor.interrupt_or_wake()` (not the hand-rolled
#     `IterationResult.interrupted=True` stand-in `_interrupted()` above
#     produces on its own -- the `test_steer.py` idiom,
#     `test_steer_now_forces_the_next_transition_to_steer_not_worker`),
#     so the claim is proven against the real dispatcher rather than
#     asserted in prose. `_ScriptedAgent_161.interrupt()` (already defined
#     above) reports `self.running`, which is True for the whole span of
#     the handler call below -- exactly the state a real in-flight `pi`
#     process would be in.
# ---------------------------------------------------------------------------


def _interrupted_by_now_161() -> IterationResult:
    """Calls the REAL `interrupt_or_wake()` on the supervisor handling this
    very call -- proving a running planning/plan_review/review iteration is
    SIGNALLED exactly like any other running iteration, never routed to the
    idle-wake path K.3/K.5 added for a genuinely idle loop -- then hands
    back the same interrupted-result shape `_interrupted()` above does, so
    the rest of the phase's own pre-existing policy runs completely
    unchanged."""
    def handler(sup, prompt=""):
        outcome = sup.interrupt_or_wake()
        assert outcome == {"target": "iteration", "signalled": True,
                          "woken": False}, outcome
        return _interrupted()
    return handler


def test_steer_now_against_a_running_plan_review_iteration_keeps_todays_161_cost(
        tmp_path):
    """A single `--now` mid-plan_review is signalled (never the idle-wake
    shape) and re-asked in place with NO revision spent -- byte-for-byte
    `test_an_interrupted_plan_review_is_reasked_in_place_and_spends_no_
    revision` above, with the trigger swapped for the real dispatcher."""
    root = tmp_path / "run"
    sup, final_state = _run_fresh(
        root,
        {"planning": [_plan_161(TASK_1_161)],
         "plan_review": [_interrupted_by_now_161(), _plan_review_approves],
         "worker": [_worker_completes(TASK_1_161)],
         "review": [_review_verifies]})

    agent = sup.runner
    assert agent.phases_seen == [
        "planning", "plan_review", "plan_review", "worker", "review"]
    assert agent.calls["planning"] == 1, "no revision re-ran planning"
    assert final_state == "succeeded", sup.run.read_status()
    status = sup.run.read_status()
    assert status.get("planReviewRevisions") is None, (
        "K.6: a single --now signal must not spend a plan_review revision")


def test_steer_now_twice_against_a_running_plan_review_still_spends_a_revision(
        tmp_path):
    """Two `--now` signals in a row over a fresh, unworked plan still hit
    the pre-existing undecidable policy: a revision is spent and `planning`
    re-runs (which re-ingests every task `pending` off the fresh proposal)
    -- #161's own cost, byte-for-byte unchanged by K, proven through the
    real dispatcher instead of the synthetic stand-in."""
    root = tmp_path / "run"
    sup, final_state = _run_fresh(
        root,
        {"planning": [_plan_161(TASK_1_161), _plan_161(TASK_1_161)],
         "plan_review": [_interrupted_by_now_161(), _interrupted_by_now_161(),
                         _plan_review_approves],
         "worker": [_worker_completes(TASK_1_161)],
         "review": [_review_verifies]})

    agent = sup.runner
    assert agent.calls["planning"] == 2
    assert agent.calls["plan_review"] == 3
    assert final_state == "succeeded", sup.run.read_status()
    assert sup.run.read_status().get("planReviewRevisions") == {"1": 1}


def test_steer_now_against_a_running_planning_iteration_keeps_todays_retry_in_place(
        tmp_path):
    """K.6 for `planning` itself: a single `--now` mid-`planning` is
    signalled (never woken as idle) and retried IN PLACE by
    `_one_planning_attempt`'s own inner loop -- no state change, no cost
    -- exactly `test_4_one_planning_attempt_retries_in_place_when_
    interrupted_with_budget_left` (`tests/test_worker_hat.py`) proves at
    the unit level, here end to end through the real dispatcher with peer
    plan review off (irrelevant to this phase's own contract)."""
    root = tmp_path / "run"
    sup, final_state = _run_fresh(
        root,
        {"planning": [_interrupted_by_now_161(), _plan_161(TASK_1_161)],
         "worker": [_worker_completes(TASK_1_161)],
         "review": [_review_verifies]},
        peer_plan_review=False)

    agent = sup.runner
    assert agent.calls["planning"] == 2, (
        "the interrupted attempt must be retried in place, not skipped")
    assert agent.phases_seen[:2] == ["planning", "planning"], agent.phases_seen
    assert final_state == "succeeded", sup.run.read_status()


def test_steer_now_against_a_running_review_iteration_keeps_todays_self_loop(
        tmp_path):
    """K.6 for `review` itself: a single `--now` mid-`review` is signalled
    (never woken as idle) and self-loops via the pre-existing
    `review_interrupted` guard (requirement A, #72) -- no new edge, no new
    cost, unaffected by K."""
    root = tmp_path / "run"
    sup, final_state = _run_fresh(
        root,
        {"planning": [_plan_161(TASK_1_161)],
         "worker": [_worker_completes(TASK_1_161)],
         "review": [_interrupted_by_now_161(), _review_verifies]},
        peer_plan_review=False)

    agent = sup.runner
    assert agent.calls["review"] == 2, (
        "the interrupted review attempt must self-loop back into review")
    assert final_state == "succeeded", sup.run.read_status()


# ---------------------------------------------------------------------------
# 4. an objection over a worked plan is refused; tasks.json is untouched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reply", ["objected", "interrupted-twice", "garbage"])
def test_a_revision_over_a_worked_plan_is_refused_and_wipes_nothing(
        tmp_path, reply):
    root = tmp_path / "run"
    run = _seed_resumable_run_dir(root, task1_status="validated",
                                  approved_marker=False)
    before = _statuses(run)
    if reply == "objected":
        peer = [_plan_review_objects("re-cut the plan")]
    elif reply == "interrupted-twice":
        peer = [_plan_review_interrupted, _plan_review_interrupted]
    else:
        peer = [lambda sup, prompt="": _ok_161("not json at all")]
    sup = _supervisor_161(root, {"plan_review": peer})
    sup._approach = 1
    outcome = asyncio.run(sup._step_plan_review())

    assert outcome == ("advance", "worker",
                       "plan_review_revision_refused_work_recorded"), outcome
    assert _statuses(run) == before, "tasks.json must not change"
    status = sup.run.read_status()
    assert status.get("planReviewRevisions") is None, "no revision was spent"
    assert status.get("planReviewApproved") == {"1": True}
    warnings = [e for e in read_events(sup.run.root)
                if e.get("type") == "log" and "refused" in (e.get("message") or "")]
    assert warnings, "the refusal is logged with the peer's objection"
    assert sup._pending_plan_review_objection is None


# ---------------------------------------------------------------------------
# the marker: bound-reached also records that the plan proceeded
# ---------------------------------------------------------------------------


def test_bound_reached_records_the_plan_as_settled(tmp_path):
    root = tmp_path / "run"
    sup, final_state = _run_fresh(
        root,
        {"planning": [_plan_161(TASK_1_161), _plan_161(TASK_1_161)],
         "plan_review": [_plan_review_objects("first"),
                         _plan_review_objects("second, bound spent")],
         "worker": [_worker_completes(TASK_1_161)],
         "review": [_review_verifies]},
        peer_plan_review_max_revisions=1)
    assert final_state == "succeeded", sup.run.read_status()
    status = sup.run.read_status()
    assert status.get("planReviewRevisions") == {"1": 1}
    assert status.get("planReviewApproved") == {"1": True}
