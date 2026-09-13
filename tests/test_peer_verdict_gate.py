"""Task 020 (PRD requirement L, #101): the opt-in, genuinely BLIND peer
verdict gate.

`peer_verdict_gate: false` (the default) must reproduce today's engine
BYTE-FOR-BYTE: the composed transition table, the dispatch loop's
behaviour, and the shipped `MODEL_HATS`/`HAT_SCHEMAS` inventory all gain a
real `verdict_gate` entry (exactly like `plan_review` gained one in task
018, and `verify` has always had one for the vigilant axis), but nothing
about a gate-off run's OWN reachable table changes.

`peer_verdict_gate: true` inserts one hat between `review`'s `verified`
edge and `finishing`: a SECOND, INDEPENDENT peer model either agrees
(`finishing`, the run's real success edge now sitting here) or rejects,
folding its own findings into a FRESH approach (`replan`) -- the decided
rejection semantics (a NEW approach, never a targeted worker fix-up;
`review` is re-run automatically by that fresh approach's own ordinary
`worker -> review` pass, no bespoke re-invocation needed), recorded in
this task's own commit message and
`artifacts/reports/task-020-requirement-l-rejection-semantics.md`. Once the
approach budget is spent the gate fails OPEN (`finishing` anyway), never a
deadlocked run.

*Blind* is the requirement's teeth, and this file pins it at every level:
Part 6 drives a real scripted `review` reply carrying a distinctive marker
in its `reason` field and asserts that marker is ABSENT from the peer
verdict gate's own rendered prompt (`_step_verdict_gate` never passes
review's payload as `extra` context, unlike `_run_review_iteration`
itself), and Part 7b forbids the shipped prompt TEXT from attributing a
verdict value to the review hat at all -- the first attempt at this task
opened the prompt with "review has already emitted a `verified` verdict",
which handed the peer review's conclusion however clean the `extra`
plumbing was.

Part 8 pins the other half of "before the run may reach terminal success":
`_maybe_grace_review` (task 002's off-budget grace review) is the only
other site that writes `succeeded`/`verified`, so with the gate on it gets
its own single off-budget, equally blind `verdict_gate` call before any
terminal write -- and with the gate off it behaves exactly as it always
did.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from ralphd.engine import contracts, findings
from ralphd.engine import tiers as T
from ralphd.engine import transitions as tr
from ralphd.engine.config import (DEFAULT_HAT_MODEL_REF, MODEL_HATS,
                                  UNRECOGNISED_HAT_FALLBACK, JobConfig)
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import scripted_supervisor

REPO_ROOT = Path(__file__).resolve().parents[1]
VERDICT_GATE_PROMPT = REPO_ROOT / "src" / "ralphd" / "prompts" / "verdict_gate.md"

TASK_1 = "001"


# ---------------------------------------------------------------------------
# 1. off by default: the composed table is byte-identical to today's
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vigilant", [True, False])
def test_gate_off_by_default_table_is_byte_identical_to_today(vigilant):
    """`table_for(vigilant)` (the pre-task-020 call shape, no extra
    arguments) and `table_for(vigilant, peer_verdict_gate=False)` -- the
    ONLY two ways every pre-existing caller invokes it -- must produce the
    exact same dict. A `verdict_gate` key silently appearing (even mapped
    to `()`) would NOT be byte-identical to a dict that lacks the key
    entirely, and `review`'s own edges must not have moved a single
    character."""
    today = tr.table_for(vigilant)
    off = tr.table_for(vigilant, peer_verdict_gate=False)
    assert today == off
    assert "verdict_gate" not in today
    assert "verdict_gate" not in off
    assert today["review"] == (
        tr.T("worker", "steering_pending_at_verified"),
        tr.T("finishing", "verified"),
        tr.T("replan", "rejected_approach_left"),
        # Task 006 (PRD requirement B.2, #111): the incurable-rejection
        # edge, added to the base table beside `rejected_approach_left` --
        # present here too since gate-off is byte-identical to TODAY's
        # table (now including this edge), not to a pre-task-006 snapshot
        # of it.
        tr.T("review", "rejected_incurable"),
        # Task 009 (PRD requirement C.1, #141): the cure-pass edge, added
        # to the base table beside `rejected_incurable` -- present here
        # too for the same reason: gate-off is byte-identical to TODAY's
        # table (now including this edge), not to a pre-task-009 snapshot
        # of it.
        tr.T("cure", "rejected_curable_pass_available"),
        # Task 011 (PRD requirement D.1, #142): the last-approach pause
        # edge, added to the base table beside `rejected_curable_pass_
        # available` -- present here too for the same reason: gate-off is
        # byte-identical to TODAY's table (now including this edge), not
        # to a pre-task-011 snapshot of it. Task 004 (finding 4)
        # retargeted this at `paused`, and the cure-pass edge above at
        # `cure`, in the base table -- both carried over here unchanged.
        tr.T("paused", "rejected_last_approach"),
        # Task 016 (PRD requirement E.5, #190): the repeated-rejection-
        # signature pause edge, added to the base table beside `rejected_
        # last_approach` -- present here too for the same reason: gate-off
        # is byte-identical to TODAY's table (now including this edge),
        # not to a pre-task-016 snapshot of it.
        tr.T("paused", "rejected_repeated_signature"),
        tr.T("finishing", "rejected_no_approach_left"),
        tr.T("review", "review_interrupted"),
        tr.T("finishing", "review_unjudged_at_budget_end"),
    )


def test_gate_off_by_default_on_a_default_jobconfig():
    assert JobConfig().peer_verdict_gate is False


def test_both_peer_gates_can_be_off_together_byte_identical_to_today():
    """Composing all three flags at their defaults (vigilant either way,
    both peer gates off) is still byte-identical to the original two-arg
    `table_for(vigilant)` -- no interaction between the axes changes
    anything when neither opt-in gate is on."""
    for vigilant in (True, False):
        assert tr.table_for(vigilant) == tr.table_for(
            vigilant, peer_plan_review=False, peer_verdict_gate=False)


def test_gate_off_transition_executor_matches_the_two_arg_call():
    executor_default = tr.TransitionExecutor(vigilant=False)
    executor_explicit_off = tr.TransitionExecutor(vigilant=False, peer_verdict_gate=False)
    assert executor_default.advance("review", "finishing", "verified") == "finishing"
    assert executor_explicit_off.advance("review", "finishing", "verified") == "finishing"
    with pytest.raises(tr.IllegalTransition):
        executor_default.advance("review", "verdict_gate",
                                 "verified_peer_verdict_gate_enabled")


# ---------------------------------------------------------------------------
# 2. on: the declared edges, and nothing undeclared
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vigilant", [True, False])
def test_verdict_gate_is_in_the_state_inventory(vigilant):
    assert "verdict_gate" in tr.ALL_STATES
    assert tr.successors("verdict_gate", vigilant, peer_verdict_gate=False) == ()
    succ = tr.successors("verdict_gate", vigilant, peer_verdict_gate=True)
    assert succ != (), "verdict_gate must not be a dead end when the gate is on"
    assert all(edge.target in tr.ALL_STATES for edge in succ)


@pytest.mark.parametrize("vigilant", [True, False])
def test_review_verified_routes_to_verdict_gate_only_when_the_gate_is_on(vigilant):
    assert not tr.is_legal("review", "verdict_gate", vigilant, peer_verdict_gate=False)
    assert tr.is_legal("review", "verdict_gate", vigilant, peer_verdict_gate=True)
    edges = tr.successors("review", vigilant, peer_verdict_gate=True)
    peer_edges = [e for e in edges if e.target == "verdict_gate"]
    assert len(peer_edges) == 1
    assert peer_edges[0].guard == "verified_peer_verdict_gate_enabled"
    # `review -> finishing` under the OLD `verified` guard is gone once
    # the gate is on -- the two guards must never both be legal at once.
    guards = tr.guard_names("review", vigilant, peer_verdict_gate=True)
    assert "verified" not in guards


@pytest.mark.parametrize("vigilant", [True, False])
def test_review_s_other_edges_are_unchanged_by_the_gate(vigilant):
    with_gate = {(e.target, e.guard) for e in
                tr.successors("review", vigilant, peer_verdict_gate=True)}
    without_gate = {(e.target, e.guard) for e in
                   tr.successors("review", vigilant, peer_verdict_gate=False)}
    # every edge except `verified`'s target changes; the rest carry over.
    without_gate_minus_verified = {
        (t, g) for t, g in without_gate if g != "verified"}
    with_gate_minus_new = {
        (t, g) for t, g in with_gate
        if g != "verified_peer_verdict_gate_enabled"}
    assert without_gate_minus_verified == with_gate_minus_new


@pytest.mark.parametrize("vigilant", [True, False])
def test_verdict_gate_has_exactly_the_three_declared_edges(vigilant):
    """Task 001 (PRD requirement A.1, #192) adds a FOURTH declared edge
    (`verdict_gate_rejected_curable_pass_available` -> `cure`) beside the
    three this test originally pinned -- kept under its original name
    (this module's own node-id-in-commit-message discipline) with its
    assertion widened to match."""
    edges = tr.successors("verdict_gate", vigilant, peer_verdict_gate=True)
    assert edges == (
        tr.T("finishing", "verdict_gate_approved"),
        tr.T("cure", "verdict_gate_rejected_curable_pass_available"),
        tr.T("replan", "verdict_gate_rejected_approach_left"),
        tr.T("finishing", "verdict_gate_rejected_no_approach_left"),
    )


@pytest.mark.parametrize("vigilant", [True, False])
def test_an_undeclared_target_out_of_verdict_gate_is_refused(vigilant):
    assert not tr.is_legal("verdict_gate", "bogus_undeclared_target", vigilant,
                           peer_verdict_gate=True)
    with pytest.raises(tr.IllegalTransition):
        tr.TransitionExecutor(vigilant, peer_verdict_gate=True).advance(
            "verdict_gate", "bogus_undeclared_target", "verdict_gate_approved")


@pytest.mark.parametrize("vigilant", [True, False])
def test_no_other_state_declares_an_edge_into_verdict_gate_except_review(vigilant):
    table = tr.table_for(vigilant, peer_verdict_gate=True)
    sources = {state for state, edges in table.items()
              if any(edge.target == "verdict_gate" for edge in edges)}
    assert sources == {"review"}


def test_the_two_peer_gates_compose_without_interaction():
    """A vigilant run with BOTH opt-in gates on: `planning`/`plan_review`
    and `review`/`verdict_gate` each carry their own delta, and neither
    axis touches the other's states."""
    table = tr.table_for(True, peer_plan_review=True, peer_verdict_gate=True)
    assert any(e.target == "plan_review" for e in table["planning"])
    assert any(e.target == "verdict_gate" for e in table["review"])
    # worker/verify (the vigilant axis) are untouched by either peer delta.
    assert table["verify"] == tr.VIGILANT_DELTA["verify"]


# ---------------------------------------------------------------------------
# 3. STATE_HAT / HATS / HAT_SCHEMAS entries
# ---------------------------------------------------------------------------


def test_state_hat_entry():
    assert tr.STATE_HAT["verdict_gate"] == "verdict_gate"


def test_verdict_gate_is_in_hats():
    assert "verdict_gate" in tr.HATS


def test_hat_schemas_entry_shape():
    schema = tr.HAT_SCHEMAS["verdict_gate"]
    assert schema["type"] == "object"
    assert schema["required"] == ["verdict"]
    assert schema["properties"]["verdict"]["enum"] == ["verified", "rejected"]
    assert schema["additionalProperties"] is False


def test_every_hat_including_verdict_gate_has_exactly_one_schema():
    assert contracts.hats_with_schemas() == frozenset(tr.HATS)
    assert "verdict_gate" in contracts.hats_with_schemas()


def test_a_verified_payload_validates():
    assert contracts.validate_payload("verdict_gate", {"verdict": "verified"}) == []


def test_a_rejected_payload_with_a_reason_validates():
    errors = contracts.validate_payload(
        "verdict_gate", {"verdict": "rejected", "reason": "missing a requirement",
                        "findings_path": "review-findings.md"})
    assert errors == []


def test_a_rejected_payload_with_no_reason_is_a_semantic_violation():
    payload = {"verdict": "rejected"}
    assert contracts.validate_payload("verdict_gate", payload) == []
    assert tr.verdict_gate_report_errors(payload) != []


def test_a_rejected_payload_with_a_blank_reason_is_a_semantic_violation():
    payload = {"verdict": "rejected", "reason": "   "}
    assert tr.verdict_gate_report_errors(payload) != []


def test_a_verified_payload_has_no_reason_requirement():
    assert tr.verdict_gate_report_errors({"verdict": "verified"}) == []


def test_a_payload_missing_verdict_is_rejected():
    errors = contracts.validate_payload("verdict_gate", {"reason": "x"})
    assert errors


def test_an_extra_property_is_rejected():
    errors = contracts.validate_payload(
        "verdict_gate", {"verdict": "verified", "task_status": "completed"})
    assert errors


def test_an_unrecognised_verdict_value_is_rejected():
    errors = contracts.validate_payload("verdict_gate", {"verdict": "approved"})
    assert errors


# ---------------------------------------------------------------------------
# 4. the prompt exists and states the blind contract
# ---------------------------------------------------------------------------


def test_verdict_gate_prompt_exists():
    assert VERDICT_GATE_PROMPT.is_file(), f"missing {VERDICT_GATE_PROMPT}"


def test_verdict_gate_prompt_declares_the_verdict_contract():
    text = VERDICT_GATE_PROMPT.read_text()
    assert '"verified"' in text
    assert '"rejected"' in text
    assert "reason" in text


def test_verdict_gate_prompt_says_what_it_does_and_does_not_receive():
    """Requirement L's own teeth: the prompt must say, in words, both
    halves of the blindness contract."""
    text = VERDICT_GATE_PROMPT.read_text().lower()
    assert "blind" in text
    # does NOT receive review's verdict/reasoning
    assert "review" in text and (
        "not receive" in text or "do not receive" in text or "never receive" in text)
    assert "reason" in text
    # DOES receive the PRD / tasks.json / workspace
    assert "prd" in text
    assert "tasks.json" in text


# ---------------------------------------------------------------------------
# 5. config.MODEL_HATS gains a REAL entry, tier rungs included
# ---------------------------------------------------------------------------


def test_verdict_gate_is_a_real_model_hats_entry():
    assert "verdict_gate" in MODEL_HATS
    assert "verdict_gate" != UNRECOGNISED_HAT_FALLBACK


def test_verdict_gate_shipped_default_matches_every_other_known_hat():
    cfg = JobConfig()
    assert cfg.model_for("verdict_gate") is DEFAULT_HAT_MODEL_REF is None
    cfg2 = JobConfig(model="shipped-model")
    assert cfg2.model_for("verdict_gate") == "shipped-model"


def test_verdict_gate_has_tier_rungs_via_all_hat_tier_pairs():
    pairs = T.all_hat_tier_pairs()
    peer_pairs = {(h, t) for h, t in pairs if h == "verdict_gate"}
    assert peer_pairs == {("verdict_gate", tier) for tier in T.TIERS}


# ---------------------------------------------------------------------------
# 6. a real LoopSupervisor, driven end to end
# ---------------------------------------------------------------------------


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _plan(task_id: str = TASK_1):
    def handler(sup, prompt=""):
        atomic_write_json(sup.run.root / "plan-proposal.json", {
            "version": 1,
            "tasks": [{"id": task_id, "title": "t", "successCriteria": "c"}],
        })
        return _ok(json.dumps(
            {"proposal_path": "plan-proposal.json", "summary": "planned"}))
    return handler


def _worker_completes(task_id: str):
    def handler(sup, prompt=""):
        return _ok(json.dumps({"verdict": "complete", "task_id": task_id}))
    return handler


def _review_verifies(reason: str | None = None):
    def handler(sup, prompt=""):
        payload = {"verdict": "verified"}
        if reason is not None:
            payload["reason"] = reason
        return _ok(json.dumps(payload))
    return handler


def _verdict_gate_approves(sup, prompt=""):
    return _ok(json.dumps({"verdict": "verified"}))


def _verdict_gate_rejects(reason: str):
    def handler(sup, prompt=""):
        return _ok(json.dumps({"verdict": "rejected", "reason": reason}))
    return handler



_supervisor = scripted_supervisor(run_id="task020-verdict-gate", iterations=20,
                                  max_approaches=1, vigilant=False,
                                  on_complete="exit",
                                  infra_outage_budget_s=1000.0)



def _run_job(root: Path, handlers: dict, **cfg_kw) -> tuple[LoopSupervisor, str]:
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, handlers, **cfg_kw)
    return sup, asyncio.run(sup._run_job_core())


def test_gate_off_never_runs_the_verdict_gate_phase_at_all(tmp_path):
    root = tmp_path / "run"
    sup, final_state = _run_job(
        root,
        {"planning": [_plan()], "worker": [_worker_completes(TASK_1)],
         "review": [_review_verifies()]},
        peer_verdict_gate=False)

    agent = sup.runner
    assert "verdict_gate" not in agent.phases_seen
    assert final_state == "succeeded", sup.run.read_status()
    assert agent.phases_seen == ["planning", "worker", "review"]


def test_gate_on_approval_path_reaches_terminal_success(tmp_path):
    root = tmp_path / "run"
    sup, final_state = _run_job(
        root,
        {"planning": [_plan()], "worker": [_worker_completes(TASK_1)],
         "review": [_review_verifies()],
         "verdict_gate": [_verdict_gate_approves]},
        peer_verdict_gate=True)

    agent = sup.runner
    assert agent.phases_seen == ["planning", "worker", "review", "verdict_gate"]
    assert agent.calls == {"planning": 1, "worker": 1, "review": 1,
                           "verdict_gate": 1}
    assert final_state == "succeeded", sup.run.read_status()


def test_gate_on_rejection_with_an_approach_left_starts_a_fresh_approach(tmp_path):
    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    (root / "prd.md").write_text("# test PRD\n\nDo the thing.\n")
    sup, final_state = _run_job(
        root,
        {"planning": [_plan(), _plan()],
         "worker": [_worker_completes(TASK_1), _worker_completes(TASK_1)],
         "review": [_review_verifies(), _review_verifies()],
         "verdict_gate": [_verdict_gate_rejects("the peer found an unmet "
                                                "requirement review missed"),
                          _verdict_gate_approves]},
        peer_verdict_gate=True, max_approaches=2)

    agent = sup.runner
    assert agent.phases_seen == [
        "planning", "worker", "review", "verdict_gate",
        "planning", "worker", "review", "verdict_gate"]
    assert final_state == "succeeded", sup.run.read_status()
    # the rejection's findings landed on disk for the next approach to read.
    findings = root / "approaches" / "01" / "review-findings.md"
    # `_archive_approach` copies whatever exists at `self.run.findings_file`
    # -- the peer hat writes it itself per prompts/verdict_gate.md; this
    # scripted reply doesn't actually write the file, so assert the
    # ARCHIVE mechanism ran (the directory exists) and the composite PRD
    # folds review-findings in when present, rather than asserting content
    # the scripted handler never wrote.
    assert (root / "approaches" / "01").is_dir()
    composite = root / "composite-prd.md"
    assert composite.exists()


def test_gate_on_rejection_with_no_approach_left_fails_open_to_finishing(tmp_path):
    root = tmp_path / "run"
    sup, final_state = _run_job(
        root,
        {"planning": [_plan()], "worker": [_worker_completes(TASK_1)],
         "review": [_review_verifies()],
         "verdict_gate": [_verdict_gate_rejects("no budget left to try again")]},
        peer_verdict_gate=True, max_approaches=1)

    agent = sup.runner
    assert agent.phases_seen == ["planning", "worker", "review", "verdict_gate"]
    # a peer rejection is never a terminal SUCCESS, budget or not.
    assert final_state == "failed", sup.run.read_status()
    assert sup.run.read_status().get("verdict") == "unverified"


def test_gate_on_a_malformed_reply_defaults_to_the_safe_rejected_choice(tmp_path):
    """An absent/malformed verdict_gate payload is never a run-ending
    contract violation -- it defaults to the safe choice (not verified),
    exactly like an unparseable `review` reply already does, and (with no
    approach left) ends the run failed rather than hanging or crashing."""
    def _garbage(sup, prompt=""):
        return _ok("not json at all")

    root = tmp_path / "run"
    sup, final_state = _run_job(
        root,
        {"planning": [_plan()], "worker": [_worker_completes(TASK_1)],
         "review": [_review_verifies()], "verdict_gate": [_garbage]},
        peer_verdict_gate=True, max_approaches=1)

    assert final_state == "failed", sup.run.read_status()


def test_verdict_gate_is_charged_like_plan_review_not_refunded_like_steer(tmp_path):
    """Decision recorded in this task's commit message and
    `artifacts/reports/task-020-requirement-l-rejection-semantics.md`: the
    verdict gate does real reasoning work, so its iteration counts against
    the run's ordinary iteration budget exactly like `plan_review`'s does
    -- never refunded the way `steer`'s uncharged iterations are."""
    root = tmp_path / "run"
    sup, final_state = _run_job(
        root,
        {"planning": [_plan()], "worker": [_worker_completes(TASK_1)],
         "review": [_review_verifies()],
         "verdict_gate": [_verdict_gate_approves]},
        peer_verdict_gate=True)
    assert final_state == "succeeded", sup.run.read_status()
    status = sup.run.read_status()
    # 4 real iterations (planning, worker, review, verdict_gate), none
    # refunded.
    assert status.get("iterationsUsed") == 4


# ---------------------------------------------------------------------------
# 7. genuinely BLIND: the reviewer's reasoning/verdict never reaches the
#    peer verdict gate's own rendered prompt
# ---------------------------------------------------------------------------


REVIEW_SECRET_REASON = "TOPSECRET-REVIEW-REASON-MARKER-4f9c2"


def test_verdict_gate_prompt_never_carries_reviews_own_reasoning_or_verdict(tmp_path):
    """The teeth of requirement L: `review`'s own contract payload (its
    stated verdict AND its `reason` text, planted here with a marker no
    ordinary prompt text would ever contain) must be genuinely absent from
    `verdict_gate`'s rendered prompt -- not merely unused, ABSENT, because
    `_step_verdict_gate` never assembles review's payload into `extra`
    context the way `_run_review_iteration` does for review's OWN prompt.
    """
    root = tmp_path / "run"
    sup, final_state = _run_job(
        root,
        {"planning": [_plan()], "worker": [_worker_completes(TASK_1)],
         "review": [_review_verifies(REVIEW_SECRET_REASON)],
         "verdict_gate": [_verdict_gate_approves]},
        peer_verdict_gate=True)

    assert final_state == "succeeded", sup.run.read_status()
    agent = sup.runner
    assert agent.phases_seen == ["planning", "worker", "review", "verdict_gate"]

    verdict_gate_prompt = agent.prompts[agent.phases_seen.index("verdict_gate")]
    review_prompt = agent.prompts[agent.phases_seen.index("review")]

    # the marker really was in review's OWN contract payload (sanity check
    # that this test would actually catch a leak).
    assert REVIEW_SECRET_REASON not in review_prompt  # it's review's OUTPUT, not its prompt

    # ... and it must be absent from the peer gate's rendered prompt.
    assert REVIEW_SECRET_REASON not in verdict_gate_prompt

    # the peer gate's prompt states its own blindness contract in words.
    assert "blind" in verdict_gate_prompt.lower()


def test_verdict_gate_run_iteration_call_site_passes_no_extra_context(monkeypatch, tmp_path):
    """A narrower, unit-level pin of the same fact: `_step_verdict_gate`
    calls `run_iteration('verdict_gate', ...)` with NO `extra` keyword
    (or an empty one) -- unlike `_run_review_iteration`, which always
    passes `extra=self._review_extra_context()`. Spies on the real
    `LoopSupervisor.run_iteration` call rather than reading source, so a
    future refactor that reintroduces review-shaped context would fail
    this test even if it renamed the parameter.
    """
    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    RunDir(root=root).update_status(state="starting")
    sup = LoopSupervisor(
        JobConfig(run_id="task020-unit", iterations=5, max_approaches=1,
                 vigilant=False, peer_verdict_gate=True,
                 infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0),
        run, root)
    sup._approach = 1

    calls = []
    orig = LoopSupervisor.run_iteration

    async def spying_run_iteration(self, phase, extra="", *a, **kw):
        calls.append((phase, extra))
        r = IterationResult(exit_code=0)
        r.final_text = json.dumps({"verdict": "verified"})
        return r

    monkeypatch.setattr(LoopSupervisor, "run_iteration", spying_run_iteration)
    outcome = asyncio.run(sup._step_verdict_gate())
    monkeypatch.setattr(LoopSupervisor, "run_iteration", orig)

    assert outcome == ("done", "succeeded")
    assert len(calls) == 1
    phase, extra = calls[0]
    assert phase == "verdict_gate"
    assert extra == ""


# ---------------------------------------------------------------------------
# 7b. blind means the prompt itself never states what review CONCLUDED
#     (task 020 rework: the earlier prompt said "review has already emitted
#     a `verified` verdict" in its own opening sentence -- the reviewer's
#     verdict leaking through the prompt TEXT rather than through `extra`)
# ---------------------------------------------------------------------------


# every value a judge's verdict can take, as whole words. `unverifiable`,
# `verifiably`, `Rejecting` and `verdict` are deliberately NOT matches: the
# leak this rule forbids is a statement of what verdict some OTHER judge
# reached, not the peer's own contract vocabulary.
VERDICT_VALUE_RE = re.compile(
    r"\b(verified|unverified|rejected|approved|passed)\b", re.IGNORECASE)
# the run-dir FILENAME both judges write is not a statement about either
# judge's conclusion, so it is masked out before the proximity rule runs.
FINDINGS_FILENAME = "review-findings.md"
REVIEW_TOKEN_RE = re.compile(r"\breview(er|s|ed)?\b", re.IGNORECASE)
PROXIMITY_CHARS = 400


def _review_verdict_attributions(text: str) -> list[str]:
    """Every place in `text` where a verdict VALUE sits within
    `PROXIMITY_CHARS` of a word naming the `review` hat -- i.e. every place
    the text could be read as telling this blind peer what review decided.
    Proximity rather than sentence-splitting on purpose: a leak spread over
    two sentences ("review has already run. It verified the work.") is the
    same leak, and sentence-splitting on `.` would also split
    `review-findings.md` into fragments and hide it.
    """
    masked = text.replace(FINDINGS_FILENAME, "F" * len(FINDINGS_FILENAME))
    hits = []
    for m in VERDICT_VALUE_RE.finditer(masked):
        lo = max(0, m.start() - PROXIMITY_CHARS)
        window = masked[lo:m.end() + PROXIMITY_CHARS]
        if REVIEW_TOKEN_RE.search(window):
            hits.append(masked[max(0, m.start() - 120):m.end() + 120])
    return hits


def test_the_proximity_rule_itself_catches_the_leak_it_exists_to_catch():
    """The rule above is only worth asserting if it fails on the exact
    sentence the first attempt at this task shipped."""
    leak = ("You run right after the independent `review` phase has already "
            "emitted a `verified` verdict, and right before the run may "
            "reach terminal success.")
    assert _review_verdict_attributions(leak), (
        "the proximity rule must flag a prompt that states review's verdict")
    # ... and it must not flag the peer's own reporting contract, which
    # legitimately contains both verdict values.
    clean = ('End with `{"verdict": "verified"}` or `{"verdict": '
             '"rejected", "reason": "..."}`; write your findings to '
             '`review-findings.md`.')
    assert _review_verdict_attributions(clean) == []


def test_verdict_gate_prompt_text_never_states_what_review_concluded():
    """Requirement L, literally: "its prompt states what it does and does
    not receive". A prompt whose own opening sentence says review already
    emitted `verified` HANDS the peer review's verdict, however blind the
    `extra` plumbing is -- so the shipped prompt text may not attribute a
    verdict value to the review hat anywhere.
    """
    hits = _review_verdict_attributions(VERDICT_GATE_PROMPT.read_text())
    assert hits == [], (
        "prompts/verdict_gate.md states review's own verdict to the blind "
        f"peer: {hits}")
    # the withholding is still stated positively, in words.
    lowered = VERDICT_GATE_PROMPT.read_text().lower()
    assert "blind" in lowered
    assert "what you do not receive" in lowered
    assert "what you do receive" in lowered


def test_the_rendered_verdict_gate_prompt_never_states_what_review_concluded(tmp_path):
    """The same rule applied to what the model is ACTUALLY handed at
    runtime (prompt file + every scrap `build_prompt` composes around it),
    on a run where review really did reply `verified` with a planted
    `reason` -- so this covers both leak channels at once: `extra` context
    and the prompt text itself.
    """
    root = tmp_path / "run"
    sup, final_state = _run_job(
        root,
        {"planning": [_plan()], "worker": [_worker_completes(TASK_1)],
         "review": [_review_verifies(REVIEW_SECRET_REASON)],
         "verdict_gate": [_verdict_gate_approves]},
        peer_verdict_gate=True)
    assert final_state == "succeeded", sup.run.read_status()

    agent = sup.runner
    rendered = agent.prompts[agent.phases_seen.index("verdict_gate")]
    assert REVIEW_SECRET_REASON not in rendered
    hits = _review_verdict_attributions(rendered)
    assert hits == [], f"the rendered peer prompt states review's verdict: {hits}"


# ---------------------------------------------------------------------------
# 8. the gate guards EVERY terminal-success path, including the off-budget
#    grace review (task 002) -- task 020 rework
# ---------------------------------------------------------------------------


def _seed_all_completed(root: Path) -> RunDir:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file,
                      {"version": 1, "tasks": [{"id": TASK_1, "title": "t",
                                                "successCriteria": "c",
                                                "status": "completed"}]})
    return run


def test_grace_review_alone_can_no_longer_reach_terminal_success_with_the_gate_on(tmp_path):
    """The hole this rework closes: `_maybe_grace_review` is the SECOND
    site that can write `succeeded`/`verified`, and with
    `peer_verdict_gate=True` it used to do so with `phases_seen ==
    ['review']` and zero verdict_gate calls -- terminal success without the
    peer gate ever running. Now the grace review's VERIFIED verdict only
    stands if an off-budget, equally blind peer agrees.
    """
    root = tmp_path / "run"
    _seed_all_completed(root)
    sup = _supervisor(
        root,
        {"review": [_review_verifies(REVIEW_SECRET_REASON)],
         "verdict_gate": [_verdict_gate_approves]},
        peer_verdict_gate=True)
    sup._approach = 1

    assert asyncio.run(sup._maybe_grace_review(1)) is True
    agent = sup.runner
    assert agent.phases_seen == ["review", "verdict_gate"]
    assert agent.calls == {"review": 1, "verdict_gate": 1}
    status = sup.run.read_status()
    assert status.get("state") == "succeeded"
    assert status.get("verdict") == "verified"
    # blind on this path too: the grace review's own reason is in hand at
    # the call site and still never reaches the peer.
    assert REVIEW_SECRET_REASON not in agent.prompts[1]
    assert _review_verdict_attributions(agent.prompts[1]) == []


def test_a_grace_review_peer_rejection_never_reaches_terminal_success(tmp_path):
    """Fail closed on success, open on deadlock: the peer's rejection on
    the grace path leaves the ordinary failed/unverified terminal write to
    the caller (the budget is already spent, so there is no approach to
    replan into) -- never `succeeded`, never a hang."""
    root = tmp_path / "run"
    _seed_all_completed(root)
    sup = _supervisor(
        root,
        {"review": [_review_verifies()],
         "verdict_gate": [_verdict_gate_rejects("the peer disagrees")]},
        peer_verdict_gate=True)
    sup._approach = 1

    assert asyncio.run(sup._maybe_grace_review(1)) is False
    assert sup.run.read_status().get("state") != "succeeded"
    note = sup._terminal_reason_note or ""
    assert "grace" in note and "peer verdict gate" in note, note


def test_a_malformed_grace_peer_reply_takes_the_same_safe_default(tmp_path):
    """One parser, one safe default, both sites: an unusable
    `verdict_gate` reply on the grace path is not verified either."""
    root = tmp_path / "run"
    _seed_all_completed(root)
    sup = _supervisor(
        root,
        {"review": [_review_verifies()],
         "verdict_gate": [lambda sup, prompt="": _ok("not json at all")]},
        peer_verdict_gate=True)
    sup._approach = 1

    assert asyncio.run(sup._maybe_grace_review(1)) is False
    assert sup.run.read_status().get("state") != "succeeded"


def test_the_grace_path_is_untouched_when_the_gate_is_off(tmp_path):
    """Off by default stays off HERE too: a gate-off grace review reaches
    terminal success exactly as it did before this task, with no
    verdict_gate phase and no extra iteration."""
    root = tmp_path / "run"
    _seed_all_completed(root)
    sup = _supervisor(root, {"review": [_review_verifies()]},
                     peer_verdict_gate=False)
    sup._approach = 1

    assert asyncio.run(sup._maybe_grace_review(1)) is True
    assert sup.runner.phases_seen == ["review"]
    status = sup.run.read_status()
    assert status.get("state") == "succeeded"
    assert status.get("graceReview") is True


def test_the_grace_verdict_gate_iteration_is_off_budget(tmp_path):
    """The grace review is off-budget (task 002) because the budget is
    already exhausted when it runs; its peer gate is granted on the same
    terms -- charging an iteration against a spent budget would be
    incoherent. Both refunds are recorded as `grace`."""
    root = tmp_path / "run"
    _seed_all_completed(root)
    sup = _supervisor(
        root, {"review": [_review_verifies()],
               "verdict_gate": [_verdict_gate_approves]},
        peer_verdict_gate=True)
    sup._approach = 1

    assert asyncio.run(sup._maybe_grace_review(1)) is True
    # two off-budget attempts ran, and BOTH are credited back through the
    # same `grace` channel the grace review has always used (the channel
    # `budget_left()` subtracts). `iterationsUsed` stays the raw attempt
    # count by long-standing design -- only infra/steer refunds are netted
    # out of the published figure, and this task changes neither.
    status = sup.run.read_status()
    assert (status.get("iterationsRefunded") or {}).get("grace") == 2, status

    # gate OFF on the same scenario: exactly one grace refund, unchanged.
    root2 = tmp_path / "run-off"
    _seed_all_completed(root2)
    sup2 = _supervisor(root2, {"review": [_review_verifies()]},
                      peer_verdict_gate=False)
    sup2._approach = 1
    assert asyncio.run(sup2._maybe_grace_review(1)) is True
    assert (sup2.run.read_status().get("iterationsRefunded")
            or {}).get("grace") == 1


def test_only_one_off_budget_peer_gate_per_approach(tmp_path):
    """Bounded by construction, exactly as the grace review itself is: the
    peer call lives inside the `_grace_review_granted`-guarded block, so a
    second `_maybe_grace_review(1)` runs neither phase again."""
    root = tmp_path / "run"
    _seed_all_completed(root)
    sup = _supervisor(
        root, {"review": [_review_verifies()],
               "verdict_gate": [_verdict_gate_rejects("no")]},
        peer_verdict_gate=True)
    sup._approach = 1

    assert asyncio.run(sup._maybe_grace_review(1)) is False
    assert asyncio.run(sup._maybe_grace_review(1)) is False
    assert sup.runner.calls == {"review": 1, "verdict_gate": 1}


# ---------------------------------------------------------------------------
# 9. Task 001 (PRD requirement A.1, #192): a rejection whose blocking
#    findings are ALL curable schedules a cure pass instead of ending the
#    run -- driven against the SAME `_progressing_approach_supervisor`-style
#    fixture the rest of this file already uses (a real `LoopSupervisor`
#    over a fresh `RunDir`), never against this run's own engine (0.20.2,
#    which has no such edge at all).
# ---------------------------------------------------------------------------


def _all_curable_gate_findings() -> list[dict]:
    return [
        {"severity": "blocking", "curable": True,
         "clause": "requirement A.1 (#192)",
         "evidence": "the peer gate found an unmet requirement review missed",
         "cured_when": "a cure task adds the missing behaviour"},
        {"severity": "blocking", "curable": True,
         "clause": "requirement A.1 second clause (#192)",
         "evidence": "the peer gate found a second, independent gap",
         "cured_when": "a cure task adds that behaviour too"},
    ]


def test_gate_rejection_all_curable_takes_a_cure_pass_not_a_replan(tmp_path):
    """The exact gap #192 names: review returned `verified`, the peer gate
    returned blocking findings it had already marked curable, and v0.20.2's
    engine had no cure path -- only the ordinary `replan`/`finishing` edges,
    which either spends a fresh approach's replan or ends the run holding
    cures it never spent. This drives `_step_verdict_gate` directly on a
    fresh `LoopSupervisor`/`RunDir` fixture (never this run's own engine)
    with `max_approaches=1` -- the LAST approach, so a wrongly-taken
    `verdict_gate_rejected_no_approach_left` edge would end the run right
    here, and a wrongly-taken `verdict_gate_rejected_approach_left` edge
    would be illegal outright (there is no approach left to spend).
    """
    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": []})
    cfg = JobConfig(run_id="task001-gate-cure-pass", iterations=5,
                    max_approaches=1, vigilant=False, peer_verdict_gate=True,
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0)
    sup = LoopSupervisor(cfg, run, root)
    sup._approach = 1

    curable = _all_curable_gate_findings()
    findings.write_findings_json(run.verdict_gate_findings_json_file, curable)

    async def _gate_rejects(self, phase, extra="", *a, **kw):
        r = IterationResult(exit_code=0)
        r.final_text = json.dumps({
            "verdict": "rejected",
            "reason": "both blocking findings are curable defects",
        })
        return r

    orig = LoopSupervisor.run_iteration
    LoopSupervisor.run_iteration = _gate_rejects
    try:
        outcome = asyncio.run(sup._step_verdict_gate())
    finally:
        LoopSupervisor.run_iteration = orig

    # the cure-pass edge, not a replan and not a terminal finish -- even
    # though this IS the last approach.
    assert outcome == ("advance", "cure",
                       "verdict_gate_rejected_curable_pass_available")

    # the approach number is unchanged, and it was never archived.
    assert sup._approach == 1
    assert not (root / "approaches" / "01").exists()
    assert not (root / "composite-prd.md").exists()

    # the gate's own cure-pass counter, persisted on status.json.
    status = run.read_status()
    assert status.get("gateCurePasses") == {"1": 1}
    # Task 004 (PRD requirement A.4): the terminal-record ending field
    # names the cure path as taken, not merits or budget-exhausted.
    assert status.get("gateRejectionEnding") == "rejected_cure_taken"

    # one cure task per blocking finding, appended to THIS approach's
    # tasks.json, in finding order.
    tasks = run.read_tasks()["tasks"]
    cure_tasks = [t for t in tasks if t.get("origin") == "review-cure"]
    assert len(cure_tasks) == 2
    for task, finding_ in zip(cure_tasks, curable):
        assert task["title"] == finding_["clause"]
        assert task["status"] == "pending"
        assert task["successCriteria"] == finding_["cured_when"]

    # the `cure` state's own successor takes the run straight back to
    # `worker` in the same dispatch pass, over the SAME appended tasks.
    cure_outcome = asyncio.run(sup._step_cure())
    assert cure_outcome == ("advance", "worker", "cure_pass_appended")


def _mixed_gate_findings() -> list[dict]:
    return [
        {"severity": "blocking", "curable": True,
         "clause": "requirement A.3 curable clause (#192)",
         "evidence": "the peer gate found a fixable gap",
         "cured_when": "a cure task adds the missing behaviour"},
        {"severity": "blocking", "curable": False,
         "clause": "requirement A.3 non-curable clause (#192)",
         "evidence": "an environment fact no cure task or fresh approach "
                    "can change"},
    ]


def test_a_mix_of_curable_and_incurable_gate_findings_still_ends_the_run(
        tmp_path):
    """Task 003 (PRD requirement A.3): the complement `_gate_cure_pass_
    available`'s own docstring names -- a rejection carrying even one
    non-curable blocking finding beside an otherwise-curable one never
    takes the cure-pass edge (`findings.all_blocking_findings_curable`
    requires the WHOLE blocking set, review's own mirror is
    `test_a_mix_of_curable_and_incurable_findings_never_takes_the_cure_
    pass` in `tests/test_cure_pass.py`), so this falls straight through to
    the ordinary archive/replan-or-finish edges below, ending the run
    exactly as it always has -- A.1 is never a claim that the gate can no
    longer fail a run. Driven end to end through `_run_job_core()` (never
    `_step_verdict_gate` in isolation) on the `test020-verdict-gate`
    fixture (`_supervisor` above) with `max_approaches=1` -- the LAST
    approach -- so a wrongly-taken cure pass would be the only thing
    standing between this mixed rejection and a terminal `failed` state."""
    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    findings.write_findings_json(run.verdict_gate_findings_json_file,
                                 _mixed_gate_findings())
    sup, final_state = _run_job(
        root,
        {"planning": [_plan()], "worker": [_worker_completes(TASK_1)],
         "review": [_review_verifies()],
         "verdict_gate": [_verdict_gate_rejects(
             "one blocking finding is curable, the other is not")]},
        peer_verdict_gate=True, max_approaches=1)

    agent = sup.runner
    assert agent.phases_seen == ["planning", "worker", "review", "verdict_gate"]
    # a mixed rejection never takes the cure-pass edge -- it ends the run
    # exactly like an all-incurable one does (`test_gate_on_rejection_
    # with_no_approach_left_fails_open_to_finishing` above).
    assert final_state == "failed", sup.run.read_status()
    assert sup.run.read_status().get("verdict") == "unverified"
    assert sup.run.read_status().get("gateCurePasses") is None
    # Task 004 (PRD requirement A.4): a mixed rejection is "on the merits",
    # never mistaken for a cure taken or a budget-exhausted cure.
    assert (sup.run.read_status().get("gateRejectionEnding")
            == "rejected_on_the_merits")
    tasks = sup.run.read_tasks()["tasks"]
    cure_tasks = [t for t in tasks if t.get("origin") == "review-cure"]
    assert cure_tasks == []


def test_the_gate_cure_pass_edge_is_declared_only_under_the_peer_delta():
    """The new edge exists ONLY when `peer_verdict_gate=True` -- `verdict_
    gate` has no reachable table entry at all when the gate is off, so
    there is nothing to compare a byte-identical snapshot against, but the
    edge itself must never leak into `TABLE`/`VIGILANT_DELTA` (a `verdict_
    gate` guard that fires even though the state is unreachable)."""
    for vigilant in (True, False):
        assert "verdict_gate" not in tr.table_for(vigilant, peer_verdict_gate=False)
        edges = tr.successors("verdict_gate", vigilant, peer_verdict_gate=True)
        by_guard = {e.guard: e.target for e in edges}
        assert by_guard["verdict_gate_rejected_curable_pass_available"] == "cure"
        assert by_guard["verdict_gate_approved"] == "finishing"
        assert by_guard["verdict_gate_rejected_approach_left"] == "replan"
        assert by_guard["verdict_gate_rejected_no_approach_left"] == "finishing"
        assert len(edges) == 4
