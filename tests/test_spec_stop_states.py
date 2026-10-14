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
    `timed_out`) only inside SPEC 5.2's "Loading a 0.35 run directory"
    compatibility text -- nowhere else in that scope, in any role.

Some of those tokens are still written by 0.36 code as other identifiers (an
`ending` value, an event name, a guard name, the park's routing key on
`pauseReason`, an `IterationResult` code). The compatibility text carries the
one table that spells them; every other section and doc in scope calls each by
its role ("the refusal `ending`", "review's park key", "the no-progress
guard"). So the check here is literal: any occurrence of a retired name outside
the compatibility text is a finding, whatever role the sentence gives it.
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
COMPAT_SECTION = "### 5.2 "
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

# The tokens 0.36 code still writes as another identifier; the compatibility
# text's identifier table must spell each one.
STILL_WRITTEN = ("operator_input_needed", "verdict_gate_operator_input_needed",
                 "review_contract_unrepaired", "review_findings_unusable",
                 "rate_limit_ceiling", "provider_refused", "stagnation",
                 RETIRED_STATE)


def _name(n: str) -> str:
    """The bare token, any case: `input.timed_out` and
    `rate_limit_ceiling_reached` are other identifiers (an event name each)
    that merely contain it, and are not the retired name."""
    return rf"(?i)(?<![\w.]){re.escape(n)}(?!\w)"


def retired_wording(text: str) -> list[str]:
    """Every occurrence of a retired name in `text`, as "<name>: <context>"."""
    found = []
    for name in RETIRED:
        for m in re.finditer(_name(name), text):
            ctx = " ".join(text[max(0, m.start() - 60):m.end() + 40].split())
            found.append(f"{name}: ...{ctx}...")
    return found


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
    out, skipping = [], False
    for line in text.splitlines():
        if line.startswith(COMPAT_HEADING):
            skipping = True
            continue
        if skipping and re.match(r"#{1,4} ", line):
            skipping = False
        if not skipping:
            out.append(line)
    return "\n".join(out)


def _label(heading: str) -> str:
    return f"SPEC {heading.strip('# ').strip()}"


def _spec_text() -> str:
    return SPEC.read_text()


def _scope() -> dict[str, str]:
    spec = _spec_text()
    scope = {_label(h): _section(spec, h) for h in SPEC_SECTIONS}
    for name, path in DOCS.items():
        scope[f"docs/{name}"] = path.read_text()
    return scope


def _outside_compat(scope: dict[str, str]) -> dict[str, str]:
    """Each text of the scope with only SPEC 5.2's own compatibility text cut:
    a doc, or another section, that grew a heading of the same name is not the
    compatibility text and is checked whole."""
    return {label: (_without_compat(text) if label == _label(COMPAT_SECTION) else text)
            for label, text in scope.items()}


# -- the four stop states and the three fields ---------------------------------

@pytest.mark.parametrize("label", [_label(h) for h in SPEC_SECTIONS])
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


# -- the retired reasons live only in the compatibility text -------------------

@pytest.mark.parametrize("label", sorted(_scope()))
def test_no_retired_pause_reason_appears_outside_the_compat_text(label):
    text = _outside_compat(_scope())[label]
    assert retired_wording(text) == []


def test_the_compatibility_text_is_where_the_retired_names_live():
    sec = _section(_spec_text(), COMPAT_SECTION)
    assert COMPAT_HEADING in sec
    compat = _section(sec, COMPAT_HEADING)
    assert "exhausted" in compat and "awaiting_input" in compat
    # the identifier table: every token the code still writes is spelled here,
    # each beside the role the rest of the scope calls it by
    for name in STILL_WRITTEN:
        assert re.search(rf"\| `{re.escape(name)}` \|", compat), (
            f"the compatibility text's identifier table has no row for `{name}`")
    assert re.search(_name(RETIRED_STATE), compat)


# -- the checker -----------------------------------------------------------------

@pytest.mark.parametrize("old", [
    "A `paused` run whose `pauseReason` is `stagnation` waits for `resume`.",
    "The edge `rejected_last_approach` holds the run for the operator.",
    "`rejected_incurable` is held in memory only.",
    "A repeated signature parks on `rejected_repeated_signature`.",
    "`unschedulable_dependency_cycle` pauses the run until a steer lands.",
    "`unresolvable_dependency_edge` is waited out.",
    "The run is `paused` with `pauseReason: \"operator_input_needed\"`.",
    "The park key `verdict_gate_operator_input_needed` never reaches `awaiting_input`.",
    "| `pauseReason` | `rate_limit_ceiling` -- the wait would pass the cap |",
    "A provider refusal pauses: `pauseReason: provider_refused`.",
    "A deadline hold is recorded `state: \"timed_out\"`.",
    "The run is held on `review_contract_unrepaired` until resumed.",
    "`review_findings_unusable` waits for the operator.",
    "The run stops on Stagnation and waits.",
    # each of the roles the previous attempt exempted is a finding too
    # (validation of attempt 2): confinement is literal
    "The run is `awaiting_input` with `pauseReason: operator_input_needed`.",
    "A refusal ends the run `failed` with `ending: provider_refused`.",
    "| `provider_refused` | `phase`, `iteration` | an iteration was classified `refused` |",
    "`IterationResult` ends `timed_out` or `no_traffic_timeout`.",
    "The stagnation guard archives the approach and replans.",
    "Review is retried (`review.retry`, `reason: review_contract_unrepaired`).",
])
def test_the_checker_flags_the_retired_names(old):
    assert retired_wording(old), old


@pytest.mark.parametrize("new", [
    "The run is `awaiting_input`; `pauseReason` carries review's park key.",
    "A refusal ends the run `failed` with the refusal `ending`.",
    "The wait does not start; `rate_limit_ceiling_reached` is emitted.",
    "`input.timed_out` is emitted when the input timeout fires.",
    "The no-progress guard archives the approach and replans.",
    "Review is retried (`review.retry`, its `reason` naming the guard).",
    "`--no-operator-input-needed-pause` resolves the park at once.",
])
def test_the_checker_passes_other_identifiers_and_role_names(new):
    assert retired_wording(new) == [], new


def test_only_spec_5_2_compat_text_is_cut():
    spec = "### 5.2 x\nkeep `timed_out` here\n\n#### Loading a 0.35 run directory\n" \
           "`timed_out` loads as exhausted\n\n### 5.3 y\nafter\n"
    cut = _without_compat(spec)
    assert "keep" in cut and "after" in cut and "loads as exhausted" not in cut
    doc = "#### Loading a 0.35 run directory\n`provider_refused` ends it\n"
    texts = _outside_compat({_label(COMPAT_SECTION): spec, "docs/api.md": doc})
    assert retired_wording(texts["docs/api.md"]), (
        "a doc's own heading of the same name must not hide a retired name")
    assert len(retired_wording(texts[_label(COMPAT_SECTION)])) == 1


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
    flagged = {h for h in SPEC_SECTIONS if retired_wording(_section(spec, h))}
    assert flagged >= {"### 4.2 ", "### 4.5 ", "### 4.6 ", "### 5.2 ", "### 5.4 ",
                       "### 10.4 ", "### 11.3 "}
    for name in DOCS:
        assert retired_wording(_git_show(f"docs/{name}")), name
