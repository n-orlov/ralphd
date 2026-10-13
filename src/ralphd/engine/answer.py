"""Answering an `awaiting_input` findings park (v0.36 requirement C).

`ralphctl answer`, `POST /answer` and the hub panel are one path with one
writer: `LoopSupervisor.answer`. This module is its pure half -- it takes the
parked `inputQuestion` and the operator's choices and names the outcome, or
refuses with a message naming what does apply. Nothing here touches disk.

The outcomes, in order (C.3):

1. every blocking finding waived          -> `verified` on that judge's
   verdict, and on to the peer gate or finish;
2. `K >= 1` cure rounds                    -> `cure`: K rounds are added to
   the current approach's allowance and the run cures the accepted findings
   now (the answer is the round's authorisation, so it does not re-park);
3. `K = 0`, an approach left (or granted)  -> `next_approach`: archive and
   replan on approach + 1, keeping the workspace tree;
4. `K = 0`, none left, none granted        -> refused, naming
   `--cure-rounds K>=1` and `--approaches +N`;
5. `K >= 1` with an accepted finding that is `curable: false` -> refused:
   cure rounds cannot fix it. Waive it, or take `K = 0`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import ladder

DEFAULT_CURE_ROUNDS = 1

OUTCOME_VERIFIED = "verified"
OUTCOME_CURE = "cure"
OUTCOME_NEXT_APPROACH = "next_approach"


class AnswerRefused(Exception):
    """The answer cannot be applied; `status` is the HTTP status the API
    answers with (409: wrong state or an option that cannot apply; 422: a
    malformed answer), `message` names what does apply."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class AnswerPlan:
    outcome: str
    waived: list[dict] = field(default_factory=list)
    accepted: list[dict] = field(default_factory=list)
    cure_rounds: int = 0
    approaches_granted: int = 0


def _id_list(value: object, what: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise AnswerRefused(f"`{what}` must be a list of finding ids", 422)
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def _count(value: object, what: str, default: int | None) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool):
        raise AnswerRefused(f"`{what}` must be a whole number", 422)
    try:
        number = int(str(value).strip().lstrip("+"))
    except (TypeError, ValueError):
        raise AnswerRefused(f"`{what}` must be a whole number", 422) from None
    if number < 0:
        raise AnswerRefused(f"`{what}` must not be negative", 422)
    return number


def plan_answer(
    question: dict | None, *, waive: object = None, accept: object = None,
    cure_rounds: object = None, approaches: object = None,
    stored_waivers: list[dict] | None = None, approach: int,
    max_approaches: int,
) -> AnswerPlan:
    """Validate one answer against the parked `question` and name its
    outcome. A finding not named is accepted. `approaches` is the number of
    approaches an answer with `cure_rounds = 0` grants when none is left."""
    if not isinstance(question, dict) or question.get("kind") != "findings":
        raise AnswerRefused(
            "this run has no findings question to answer (it is not "
            "`awaiting_input` on findings)")
    findings = [f for f in question.get("findings") or []
                if isinstance(f, dict)]
    by_id = {str(f.get("id")): f for f in findings}
    waive_ids = _id_list(waive, "waive")
    accept_ids = _id_list(accept, "accept")
    for fid in waive_ids + accept_ids:
        if fid not in by_id:
            raise AnswerRefused(
                f"unknown finding id {fid!r}; the question carries "
                f"{', '.join(by_id) or 'no findings'}", 422)
    both = [fid for fid in waive_ids if fid in accept_ids]
    if both:
        raise AnswerRefused(
            f"finding {', '.join(both)} is named in both --waive and "
            "--accept", 422)
    granted = _count(approaches, "approaches", 0) or 0
    # `--approaches +N` alone means "no more cure rounds, grant N approaches
    # and replan"; K defaults to 1 only when nothing grants an approach.
    rounds = _count(cure_rounds, "cure rounds",
                    0 if granted else DEFAULT_CURE_ROUNDS)

    already = ladder.waived_clause_keys(stored_waivers or [])
    waived = [f for f in findings
              if str(f.get("id")) in waive_ids
              or ladder.clause_key(f.get("clause")) in already]
    waived_ids = {str(f.get("id")) for f in waived}
    accepted = [f for f in findings if str(f.get("id")) not in waived_ids]
    explicit_waived = [by_id[fid] for fid in waive_ids]

    if not accepted:
        return AnswerPlan(OUTCOME_VERIFIED, waived=explicit_waived)

    if granted and rounds:
        raise AnswerRefused(
            "--approaches grants approaches for `--cure-rounds 0` (the next "
            "approach); with --cure-rounds K>=1 the run cures this approach "
            "and no approach is added", 422)
    if rounds >= 1:
        stuck = [str(f.get("id")) for f in accepted
                 if f.get("curable") is not True]
        if stuck:
            raise AnswerRefused(
                f"finding {', '.join(stuck)} is accepted but not curable "
                "(`curable: false`), so cure rounds cannot fix it: waive it "
                "(--waive), or answer --cure-rounds 0 for the next approach "
                "(or --approaches +N when none is left)")
        return AnswerPlan(OUTCOME_CURE, waived=explicit_waived,
                          accepted=accepted, cure_rounds=rounds)
    if approach >= max_approaches and not granted:
        raise AnswerRefused(
            f"no approach is left ({approach} of {max_approaches}): answer "
            "with `--cure-rounds K` (K>=1) to cure this approach, or "
            "`--approaches +N` to grant N more approaches and replan")
    return AnswerPlan(OUTCOME_NEXT_APPROACH, waived=explicit_waived,
                      accepted=accepted, cure_rounds=0,
                      approaches_granted=granted)
