"""v0.36 (#304): the spec's stop-state sections are in step with the code.

0.36 leaves a run in exactly four non-terminal stop states, each with one
owner -- `paused` (the operator), `awaiting_input` (the engine asking),
`exhausted` (a spent budget) and `running` with `health: degraded` (an infra
outage) -- and retires every pause reason 0.35's engine used to stop a run on.
The sections that describe stop behaviour (SPEC.md 4.1, 4.2, 4.5, 4.6, 5.2,
5.4, 10.4, 11.3 and 12) and the three user docs that restate it
(`docs/cli.md`, `docs/api.md`, `docs/solution-tech-spec.md`) therefore have to:

  * name the four stop states and the three status fields that carry them
    (`inputQuestion`, `exhaustedBudget`, `pausedBy`);
  * mention a retired pause reason (PRD A.1's list, plus the retired state
    `timed_out`) nowhere but SPEC 5.2's "Loading a 0.35 run directory" text.

Five of the names (the three `rejected_*` edges and the two dependency-graph
pauses) have no meaning left and are banned outright. The others are tokens the
0.36 code still uses as a *different identifier* that is not a pause reason:
`rate_limit_ceiling` / `provider_refused` are `ending` values (PRD F.4/F.5 name
them so) and `provider_refused` an event; `review_contract_unrepaired` /
`review_findings_unusable` are the review contract fault's guard names (F.3);
`stagnation` is the worker guard (F.1); `timed_out` is an `IterationResult`
code; `operator_input_needed` / `verdict_gate_operator_input_needed` are the
routing key an `awaiting_input` park stamps on `pauseReason`. Outside the
compatibility text each occurrence must therefore stand in one of the roles
enumerated in `ROLES`, and its own clause must carry no pause or hold wording,
no 0.35/retired/legacy wording, and -- beside a park key -- no denial of
`awaiting_input`. Every check is paired with a test showing the checker
rejects the 0.35 wording it replaced, and the 0.35.0 release text of the same
scope is flagged.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC = REPO_ROOT / "SPEC.md"
DOCS = {name: REPO_ROOT / "docs" / name
        for name in ("cli.md", "api.md", "solution-tech-spec.md")}

SPEC_SECTIONS = ("### 4.1 ", "### 4.2 ", "### 4.5 ", "### 4.6 ", "### 5.2 ",
                 "### 5.4 ", "### 10.4 ", "### 11.3 ", "## 12. ")

COMPAT_HEADING = "#### Loading a 0.35 run directory"

STOP_STATES = ("paused", "awaiting_input", "exhausted", "degraded")
STATUS_FIELDS = ("inputQuestion", "exhaustedBudget", "pausedBy")

# PRD A.1's list of retired pause reasons (the two without a token of their
# own -- "judge no-verdict" and "commit-secret" -- are prose, not names), plus
# the retired state `timed_out`.
A1_REASONS = (
    "operator_input_needed", "verdict_gate_operator_input_needed",
    "rejected_last_approach", "rejected_incurable", "rejected_repeated_signature",
    "stagnation", "unschedulable_dependency_cycle", "unresolvable_dependency_edge",
    "review_contract_unrepaired", "review_findings_unusable",
    "rate_limit_ceiling", "provider_refused",
)
RETIRED_STATE = "timed_out"
RETIRED = (*A1_REASONS, RETIRED_STATE)

PARK_KEYS = ("verdict_gate_operator_input_needed", "operator_input_needed")
REVIEW_GUARDS = ("review_contract_unrepaired", "review_findings_unusable")
ENDINGS = ("rate_limit_ceiling", "provider_refused")


def _name(n: str) -> str:
    """The bare token: `input.timed_out` and `rate_limit_ceiling_reached` are
    other identifiers (an event name each), not the retired name."""
    return rf"(?<![\w.]){re.escape(n)}(?![\w])"


# Outside SPEC 5.2's compatibility text a retired token may stand only in one
# of these enumerated roles -- each a different identifier that happens to
# share the token, none of them a reason a run stops on. A name with no entry
# here (the three `rejected_*` edges and the two dependency-graph pauses) may
# not appear at all. Each role is a predicate over (block, clause, prefix,
# suffix) of one occurrence.
def _role_park_key(block, clause, pre, post):
    # The park's routing key: written to `pauseReason` / carried by
    # `operator_escalation.reason` on an `awaiting_input` run.
    return "awaiting_input" in block


def _role_ending(block, clause, pre, post):
    # An `ending` value of a run that ended `failed` (PRD F.4/F.5), or the
    # `provider_refused` event of the same name.
    return bool(re.search(r"ending:\s*[\"`]?$|(?i:end failed):\s*$", pre)
                or (block.startswith("| `ending` |") and pre.endswith('"'))
                or block.startswith("| `provider_refused` |")
                or post.startswith("` is emitted"))


def _role_review_guard(block, clause, pre, post):
    # The review contract fault's guard name (PRD F.3): retried, then ended
    # `unreviewed`, never held.
    return any(k in block for k in ("unreviewed", "review.contract_fault",
                                    "review.retry", "judge_retry_max",
                                    "ask review again"))


def _role_stagnation(block, clause, pre, post):
    # The worker guard and its counter, witness and blocker signature (PRD
    # F.1: it archives the approach and replans).
    return bool(re.match(r"`?\W{0,2}(?:guard|counter|witness|formula)\b|-exhaustion", post)
                or re.search(r"(?:worker-|fails on )$", pre)
                or (pre.endswith('"') and "blockerSignature" in block)
                or block.startswith("| `log` |")
                or "archive" in clause)


def _role_iteration_result(block, clause, pre, post):
    # An iteration's own result code, beside its siblings.
    return bool(re.search(r"IterationResult|no_traffic_timeout", block))


ROLES = {
    **{n: _role_park_key for n in PARK_KEYS},
    **{n: _role_review_guard for n in REVIEW_GUARDS},
    **{n: _role_ending for n in ENDINGS},
    "stagnation": _role_stagnation,
    RETIRED_STATE: _role_iteration_result,
}

# A stop that is a pause or a hold: the retired sense of every name above.
_PAUSE_SENSE = re.compile(
    r"(?<![\w-])(?:paused|pauses|pausing|pause)(?![\w-])|"
    r"pauseDetail|(?<![\w-])(?:held|holds?) (?:on|the run|it)\b|until (?:resumed|a steer)")
# A clause denying the state the park key belongs to.
_DENIES_AWAITING = re.compile(
    r"(?:never|not|no longer|instead of|rather than)\W+(?:\w+\W+){0,4}?`?awaiting_input")

_CLAUSE_END = re.compile(r"(?:(?<=[.;!?])|(?<=[.;!?]\*\*))\s+|\s+--\s+|\|")


def _clause(block: str, start: int, end: int) -> str:
    left = 0
    for m in _CLAUSE_END.finditer(block, 0, start):
        left = m.end()
    m = _CLAUSE_END.search(block, end)
    return block[left:m.start() if m else len(block)]


def _section(text: str, heading: str) -> str:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    level = len(heading) - len(heading.lstrip("#"))
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(rf"#{{1,{level}}} ", lines[i]):
            end = i
            break
    return "\n".join(lines[start:end])


def _without_compat(text: str) -> str:
    """The text with SPEC 5.2's compatibility subsection cut out."""
    lines = text.splitlines()
    out, skipping = [], False
    for line in lines:
        if line.startswith(COMPAT_HEADING):
            skipping = True
            continue
        if skipping and re.match(r"#{1,4} ", line):
            skipping = False
        if not skipping:
            out.append(line)
    return "\n".join(out)


def _blocks(text: str) -> list[str]:
    """Paragraphs, with every table row a block of its own."""
    blocks: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        if para.lstrip().startswith("|"):
            blocks.extend(line.strip() for line in para.splitlines() if line.strip())
        else:
            blocks.append(" ".join(para.split()))
    return blocks


def retired_wording(text: str) -> list[str]:
    """Every occurrence of a retired name in `text` outside its enumerated
    non-pause roles, as "<name>: <clause>"."""
    found = []
    for block in _blocks(text):
        for name in RETIRED:
            for m in re.finditer(_name(name), block):
                clause = _clause(block, m.start(), m.end())
                pre, post = block[max(0, m.start() - 40):m.start()], block[m.end():m.end() + 40]
                role = ROLES.get(name)
                if (role is None or not role(block, clause, pre, post)
                        or _PAUSE_SENSE.search(clause)
                        or (name in PARK_KEYS and _DENIES_AWAITING.search(clause))
                        or re.search(r"0\.35|retired|legacy", clause)):
                    found.append(f"{name}: {clause}")
    return found


def _spec_text() -> str:
    return SPEC.read_text()


def _scope() -> dict[str, str]:
    spec = _spec_text()
    scope = {}
    for heading in SPEC_SECTIONS:
        sec = _section(spec, heading)
        scope[f"SPEC {heading.strip('# ').strip()}"] = sec
    for name, path in DOCS.items():
        scope[f"docs/{name}"] = path.read_text()
    return scope


# -- the four stop states and the three fields ---------------------------------

@pytest.mark.parametrize("label", [f"SPEC {h.strip('# ').strip()}" for h in SPEC_SECTIONS])
def test_each_spec_section_names_the_engine_stop_states(label):
    text = _scope()[label]
    missing = [s for s in ("awaiting_input", "exhausted") if s not in text]
    assert not missing, f"{label} never names {missing}"


def test_the_spec_sections_together_name_the_four_stop_states_and_the_fields():
    joined = "\n".join(v for k, v in _scope().items() if k.startswith("SPEC"))
    for token in (*STOP_STATES, *STATUS_FIELDS):
        assert token in joined, f"no stop-state section of SPEC.md names `{token}`"
    assert "four" in _section(_spec_text(), "### 4.1 "), (
        "SPEC 4.1 no longer says there are four stop states")


@pytest.mark.parametrize("name", sorted(DOCS))
def test_each_user_doc_names_the_four_stop_states_and_the_fields(name):
    text = DOCS[name].read_text()
    for token in (*STOP_STATES, *STATUS_FIELDS):
        assert token in text, f"docs/{name} never names `{token}`"


# -- the retired reasons --------------------------------------------------------

@pytest.mark.parametrize("label", sorted(_scope()))
def test_no_retired_pause_reason_is_current_behaviour(label):
    text = _without_compat(_scope()[label])
    assert retired_wording(text) == []


def test_the_compatibility_text_is_where_the_retired_state_lives():
    sec = _section(_spec_text(), "### 5.2 ")
    assert COMPAT_HEADING in sec
    compat = _section(sec, COMPAT_HEADING)
    assert re.search(_name(RETIRED_STATE), compat)
    assert "exhausted" in compat and "awaiting_input" in compat
    assert not re.search(_name(RETIRED_STATE), _without_compat(sec))


# -- the checker rejects the 0.35 wording --------------------------------------

@pytest.mark.parametrize("old", [
    "A `paused` run whose `pauseReason` is `stagnation` waits for `resume`.",
    "The edge `rejected_last_approach` holds the run for the operator.",
    "`rejected_incurable` is held in memory only.",
    "A repeated signature parks on `rejected_repeated_signature`.",
    "`unschedulable_dependency_cycle` pauses the run until a steer lands.",
    "`unresolvable_dependency_edge` is waited out.",
    "The run is `paused` with `pauseReason: \"operator_input_needed\"`.",
    # the denial of the park's own state, beside it (validation of attempt 1)
    "The run is `paused` with `pauseReason: \"operator_input_needed\"`; it never "
    "enters `awaiting_input`.",
    "The park key `verdict_gate_operator_input_needed` never reaches `awaiting_input`.",
    "| `pauseReason` | `rate_limit_ceiling` -- the wait would pass the cap |",
    "A provider refusal pauses: `pauseReason: provider_refused`.",
    "`provider_refused` stops the run for the operator.",
    "`rate_limit_ceiling` is written to `pauseDetail.reason`.",
    "A deadline hold is recorded `state: \"timed_out\"`.",
    "The run holds `timed_out` until `ralphctl resume`.",
    "The run is held on `review_contract_unrepaired` until resumed.",
    "`review_findings_unusable` waits for the operator.",
    "The run stops on stagnation and waits.",
    "A 0.35 run directory with `ending: provider_refused` loads as before.",
])
def test_the_checker_flags_the_0_35_wording(old):
    assert retired_wording(old), old


# Each enumerated non-pause role, once: the token as another identifier.
@pytest.mark.parametrize("new", [
    "The run is `awaiting_input`; `pauseReason: \"operator_input_needed\"` is the park's key.",
    "A refusal ends the run `failed` with `ending: provider_refused`.",
    "| `provider_refused` | `phase`, `iteration` | an iteration was classified `refused` |",
    "The wait does not start; `rate_limit_ceiling_reached` is emitted.",
    "`input.timed_out` is emitted when the input timeout fires.",
    "`IterationResult` ends `timed_out` or `no_traffic_timeout`.",
    "The stagnation guard archives the approach and replans.",
    "Review is retried (`review.retry`, `reason: review_contract_unrepaired`).",
])
def test_the_checker_passes_the_0_36_wording(new):
    assert retired_wording(new) == [], new


def test_every_retired_name_has_a_role_or_is_banned_outright():
    """The only names allowed outside the compatibility text at all are the
    ones still alive as another identifier; the rest are banned everywhere."""
    assert set(RETIRED) == set(A1_REASONS) | {RETIRED_STATE}
    banned = set(RETIRED) - set(ROLES)
    assert banned == {"rejected_last_approach", "rejected_incurable",
                      "rejected_repeated_signature", "unschedulable_dependency_cycle",
                      "unresolvable_dependency_edge"}
    for name in banned:
        assert retired_wording(f"The run ends `failed`; `{name}` is gone.")


# The 0.35.0 release commit ("bump to 0.35.0", #303).
RELEASE_035 = "49e7954d1d8a3f280fd0fd7013e9169a0f99f30d"


def _git_show(path: str) -> str:
    import subprocess
    old = subprocess.run(["git", "-C", str(REPO_ROOT), "show", f"{RELEASE_035}:{path}"],
                         capture_output=True, text=True)
    if old.returncode != 0:
        pytest.skip("the 0.35.0 release commit is not in this checkout (shallow clone)")
    return old.stdout


def test_the_checker_fails_on_the_0_35_spec():
    """The 0.35.0 text of the same scope is flagged in every section and doc
    that described an engine pause."""
    spec = _git_show("SPEC.md")
    flagged = {h for h in SPEC_SECTIONS
               if retired_wording(_without_compat(_section(spec, h)))}
    assert flagged >= {"### 4.2 ", "### 4.5 ", "### 4.6 ", "### 5.2 ", "### 5.4 ",
                       "### 10.4 ", "### 11.3 "}
    for name in DOCS:
        assert retired_wording(_git_show(f"docs/{name}")), name


def test_the_compat_cut_hides_only_the_compat_subsection():
    text = "### 5.2 x\nkeep `timed_out` here\n\n#### Loading a 0.35 run directory\n" \
           "`timed_out` loads as exhausted\n\n### 5.3 y\nafter\n"
    cut = _without_compat(text)
    assert "keep" in cut and "after" in cut and "loads as exhausted" not in cut
