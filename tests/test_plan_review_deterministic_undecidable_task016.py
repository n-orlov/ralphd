"""Task 016 (PRD requirement D.2, #117 half B): `plan_review`'s undecidable
branch (an absent/malformed/schema-invalid reply, or an `objected` verdict
with no reason) gets a declared DETERMINISTIC policy instead of asking
`engine.router.decide()`.

Task 019 (requirement M) had made this the router's one production call
site: a reply naming no usable verdict was routed by a live
`router.decide()` call over `plan_review`'s real edges. This task reverses
that -- `payload is None and revision_available` now spends the revision
directly, the exact same move an `objected`-with-no-reason verdict already
took. Three properties, each pinned below:

1. no router call happens for this branch any more -- `sup.router_prompts`
   and `artifacts/router/decisions.jsonl` both stay empty;
2. the revision re-prompt that reaches the next `planning` attempt carries
   the peer's own raw reply and the concrete parse errors, not task 019's
   boilerplate ("the router routed the plan back");
3. once the revision bound is spent, `plan_review_revision_bound_reached`
   still bounds the loop -- the plan proceeds to `worker` regardless,
   never a deadlock, never a fault.

`engine.router` itself and its fixture tests (`tests/test_router.py`) are
untouched and still pass -- the router is retained for the next genuinely
ambiguous state, per the PRD's explicit "do not delete the router".

Reuses `tests/test_peer_plan_review_gate.py`'s own scripted-agent harness
(`_supervisor`, `_run_job`, `_router_decisions`, the handler builders) --
the same cross-module import shape `tests/test_absolute_timestamps.py`
already uses for `test_cli_docker`/`test_cli_ui`.
"""

from __future__ import annotations

import asyncio

from ralphd.engine.state import RunDir

from test_peer_plan_review_gate import (  # noqa: F401 (re-exported fixtures)
    TASK_1,
    _ok,
    _plan,
    _plan_review_approves,
    _review_verifies,
    _router_decisions,
    _run_job,
    _supervisor,
    _worker_completes,
)


def _garbage(sup, prompt=""):
    return _ok("not valid json at all, no verdict anywhere in sight")


def test_an_undecidable_reply_spends_the_revision_directly_no_router_asked(tmp_path):
    """`payload is None and revision_available` -> the revision is spent
    right here, in `_step_plan_review` itself -- no call into
    `_route_with_router`/`engine.router.decide()` at all. A test that
    declared no `router_answer=...` would fail loudly on any attempted
    router call (`_supervisor`'s `fake_ask_router` asserts `answers` is
    non-empty before it ever answers), so a passing run here IS the
    zero-call proof, reinforced by the two direct assertions below."""
    root = tmp_path / "run"
    sup, final_state = _run_job(
        root,
        {"planning": [_plan(), _plan()],
         "plan_review": [_garbage, _plan_review_approves],
         "worker": [_worker_completes(TASK_1)], "review": [_review_verifies]},
        peer_plan_review=True)

    agent = sup.runner
    assert agent.phases_seen == [
        "planning", "plan_review", "planning", "plan_review", "worker",
        "review"]
    assert final_state == "succeeded", sup.run.read_status()

    # the revision was spent exactly as an `objected`-with-no-reason
    # verdict already would have -- same accounting as before task 016.
    revisions = sup.run.read_status().get("planReviewRevisions")
    assert revisions == {"1": 1}

    # no router call happened for this branch: neither the model channel
    # nor the decisions ledger saw anything.
    assert sup.router_prompts == []
    assert _router_decisions(root) == []


def test_the_revision_reprompt_carries_the_raw_reply_and_the_parse_errors(tmp_path):
    """The next `planning` attempt's prompt must contain the peer's own
    raw reply text and a concrete parse-error message -- not the old
    boilerplate ("the router routed the plan back for one revision")."""
    raw_garbage = "not valid json at all, no verdict anywhere in sight"

    def _garbage_reply(sup, prompt=""):
        return _ok(raw_garbage)

    root = tmp_path / "run"
    sup, final_state = _run_job(
        root,
        {"planning": [_plan(), _plan()],
         "plan_review": [_garbage_reply, _plan_review_approves],
         "worker": [_worker_completes(TASK_1)], "review": [_review_verifies]},
        peer_plan_review=True)

    assert final_state == "succeeded", sup.run.read_status()
    agent = sup.runner
    second_planning_prompt = agent.prompts[agent.phases_seen.index(
        "planning", 1)]

    # the peer's raw reply text reaches the next planning attempt verbatim.
    assert raw_garbage in second_planning_prompt
    # a concrete parse error accompanies it, not vague boilerplate.
    assert "could not parse a JSON object" in second_planning_prompt
    assert "the router routed the plan back" not in second_planning_prompt


def test_a_schema_invalid_reply_reprompt_carries_its_own_contract_errors(tmp_path):
    """A reply that DOES parse as JSON but fails `plan_review`'s own
    contract (schema violation, or `objected` with no/blank reason) is
    just as undecidable -- its re-prompt carries those contract errors,
    not a generic parse-failure message, since the JSON parsed fine."""
    def _schema_invalid(sup, prompt=""):
        return _ok('{"verdict": "objected"}')  # no "reason" -> semantic error

    root = tmp_path / "run"
    sup, final_state = _run_job(
        root,
        {"planning": [_plan(), _plan()],
         "plan_review": [_schema_invalid, _plan_review_approves],
         "worker": [_worker_completes(TASK_1)], "review": [_review_verifies]},
        peer_plan_review=True)

    assert final_state == "succeeded", sup.run.read_status()
    agent = sup.runner
    second_planning_prompt = agent.prompts[agent.phases_seen.index(
        "planning", 1)]
    assert '{"verdict": "objected"}' in second_planning_prompt
    assert sup.router_prompts == []
    assert _router_decisions(root) == []


def test_the_revision_bound_reached_fallback_still_bounds_the_loop(tmp_path):
    """With the revision bound already spent, an undecidable reply still
    falls through to `plan_review_revision_bound_reached` -> `worker` --
    fail OPEN on a bounded gate, never a deadlock, and (task 016) never a
    router call either: `planning` is not a legal move here regardless of
    what the reply says, decidable or not."""
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting",
                                    planReviewRevisions={"1": 1})
    sup = _supervisor(
        root,
        {"planning": [_plan()], "plan_review": [_garbage],
         "worker": [_worker_completes(TASK_1)], "review": [_review_verifies]},
        peer_plan_review=True, peer_plan_review_max_revisions=1)
    final_state = asyncio.run(sup._run_job_core())

    assert sup.runner.phases_seen == ["planning", "plan_review", "worker",
                                      "review"]
    assert final_state == "succeeded", sup.run.read_status()
    # the loop proceeded to worker rather than looping or faulting.
    assert sup.run.read_status().get("planReviewRevisions") == {"1": 1}
    # bounded and deterministic: no router call, no decision recorded.
    assert sup.router_prompts == []
    assert _router_decisions(root) == []


def test_the_bound_reached_fallback_is_a_real_legal_table_edge(tmp_path):
    """`plan_review_revision_bound_reached` is a declared edge in the
    table, not an ad hoc string -- guards against the fallback drifting
    out of sync with `transitions.py` while this module only exercises
    the loop-level behaviour above."""
    from ralphd.engine import transitions as tr

    assert tr.fallback_guard("plan_review") == \
        "plan_review_revision_bound_reached"
    edges = tr.successors("plan_review", vigilant=False,
                          peer_plan_review=True)
    assert any(e.guard == "plan_review_revision_bound_reached"
               and e.target == "worker" for e in edges)
