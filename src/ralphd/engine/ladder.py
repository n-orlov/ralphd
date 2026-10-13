"""The one rejection ladder (v0.36 requirement B).

Review and the peer verdict gate are two judges feeding ONE ladder. A
rejection is evaluated in this order and the first match wins:

1. the budget (iterations or time) is spent -> `exhausted`;
2. (a `verified` verdict never reaches this module: it goes to the peer gate
   or finishes);
3. the rejection carries blocking findings and every one of them falls under a
   clause an operator already waived -> treated as verified;
4. a park -> `awaiting_input`, when a blocking finding is `curable: false`
   (or the rejection carries no structured findings to cure), the reviewer set
   `raise_operator_gate`, or the current approach's cure rounds are spent;
5. otherwise a cure: insert the cure tasks, count one round against the
   current approach, go to the worker.

`decide` is the pure half: it takes the facts and names the step, so the two
judges cannot disagree about the order. `LoopSupervisor._run_rejection_ladder`
gathers the facts for either judge and carries the step out.
"""

from __future__ import annotations

from dataclasses import dataclass

STEP_EXHAUSTED = "exhausted"
STEP_WAIVED = "waived"
STEP_PARK = "park"
STEP_CURE = "cure"

JUDGE_REVIEW = "review"
JUDGE_VERDICT_GATE = "verdict_gate"
JUDGES = (JUDGE_REVIEW, JUDGE_VERDICT_GATE)

TRIGGER_INCURABLE = "incurable"
TRIGGER_REVIEWER_ESCALATION = "reviewer_escalation"
TRIGGER_CURE_ROUNDS_SPENT = "cure_rounds_spent"


@dataclass(frozen=True)
class Decision:
    """The ladder's verdict on one rejection: `step` is one of the `STEP_*`
    names; `trigger` is the `inputQuestion.trigger` for a park, else None."""

    step: str
    trigger: str | None = None


def clause_key(clause: object) -> str:
    """A clause compared the way a waiver is bound to it: whitespace-trimmed
    and case-folded."""
    return " ".join(str(clause or "").split()).casefold()


def waived_clause_keys(waivers: list[dict]) -> set[str]:
    return {clause_key(w.get("clause")) for w in waivers or []
            if clause_key(w.get("clause"))}


def every_blocking_finding_waived(blocking: list[dict],
                                  waivers: list[dict]) -> bool:
    """True when the judge raised at least one blocking finding and none of
    them sits outside the clauses already waived."""
    if not blocking:
        return False
    waived = waived_clause_keys(waivers)
    return all(clause_key(f.get("clause")) in waived for f in blocking)


def park_trigger(*, incurable: bool, reviewer_escalation: bool,
                 rounds_spent: bool) -> str | None:
    """Why this rejection parks, or None when it does not. The reviewer's own
    escalation flag is named first, then an incurable finding, else the cure
    rounds."""
    if reviewer_escalation:
        return TRIGGER_REVIEWER_ESCALATION
    if incurable:
        return TRIGGER_INCURABLE
    if rounds_spent:
        return TRIGGER_CURE_ROUNDS_SPENT
    return None


def decide(*, budget_left: bool, blocking: list[dict], waivers: list[dict],
           incurable: bool, reviewer_escalation: bool,
           rounds_spent: bool) -> Decision:
    """Run the ladder over one rejection."""
    if not budget_left:
        return Decision(STEP_EXHAUSTED)
    if every_blocking_finding_waived(blocking, waivers):
        return Decision(STEP_WAIVED)
    trigger = park_trigger(incurable=incurable,
                           reviewer_escalation=reviewer_escalation,
                           rounds_spent=rounds_spent)
    if trigger is not None:
        return Decision(STEP_PARK, trigger)
    return Decision(STEP_CURE)
