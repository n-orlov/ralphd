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

Some of the retired names still have a live meaning that is not a pause reason:
`rate_limit_ceiling` and `provider_refused` are `ending` values and events,
`review_contract_unrepaired` / `review_findings_unusable` are the review
contract faults' guard names, `stagnation` is the worker guard's name, and
`timed_out` is an iteration result and half of the `input.timed_out` event.
`operator_input_needed` and `verdict_gate_operator_input_needed` are still the
routing key an `awaiting_input` park stamps on `pauseReason`. For those the
test forbids the retired *sense* -- the name used as the reason of a `paused`
run -- not the token. Every check is paired with a test showing the checker
rejects the 0.35 wording it replaced.
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

# PRD A.1's list of retired pause reasons. Reasons with no live meaning at all
# may not appear outside the compatibility text.
DEAD_REASONS = (
    "rejected_last_approach", "rejected_incurable", "rejected_repeated_signature",
    "unschedulable_dependency_cycle", "unresolvable_dependency_edge",
)
# The park's routing key: live on an `awaiting_input` run, retired as a reason
# of a `paused` one.
PARK_KEYS = ("verdict_gate_operator_input_needed", "operator_input_needed")
# Live under another role (ending / event / guard / worker guard), retired as
# the reason of a `paused` run.
LIVE_NAMES = ("rate_limit_ceiling", "provider_refused", "review_contract_unrepaired",
              "review_findings_unusable", "stagnation")
RETIRED_STATE = "timed_out"

_PAUSE_CONSTRUCT = (r"(?:pauseReason|pause reason|`paused` (?:run|state)|"
                    r"paused (?:on|with|for|by)|held on|pause reasons?)")


def _name(n: str) -> str:
    return rf"(?<![\w.]){re.escape(n)}(?![\w])"


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
            blocks.extend(line for line in para.splitlines() if line.strip())
        else:
            blocks.append(" ".join(para.split()))
    return blocks


def retired_wording(text: str) -> list[str]:
    """Every block of `text` that uses a retired pause reason as current."""
    found = []
    for block in _blocks(text):
        if any(re.search(_name(n), block) for n in DEAD_REASONS):
            found.append(block)
            continue
        if any(re.search(_name(n), block) for n in PARK_KEYS):
            if "awaiting_input" not in block:
                found.append(block)
                continue
        names = list(LIVE_NAMES)
        alt = "|".join(re.escape(n) for n in names)
        if re.search(_PAUSE_CONSTRUCT + r"[^.]{0,80}?(?<![\w.])(?:" + alt + r")(?!\w)",
                     block):
            found.append(block)
            continue
        # Compatibility with a 0.35 run directory is SPEC 5.2's subsection,
        # nowhere else: a block naming a live-elsewhere name beside "0.35",
        # "retired" or "legacy" is that compatibility text out of place.
        alt35 = r"(?:0\.35|retired|legacy)"
        altn = r"(?<![\w.])(?:" + "|".join(map(re.escape, names)) + r")(?!\w)"
        if re.search(alt35 + r".{0,120}?" + altn + "|" + altn + r".{0,120}?" + alt35,
                     block):
            found.append(block)
            continue
        # The retired state: only the iteration result and `input.timed_out`
        # keep the token.
        if re.search(_name(RETIRED_STATE), block) and not re.search(
                r"no_traffic_timeout|iteration_timeout_s|IterationResult", block):
            found.append(block)
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
    "`unschedulable_dependency_cycle` pauses the run until a steer lands.",
    "The run is `paused` with `pauseReason: \"operator_input_needed\"`.",
    "| `pauseReason` | `rate_limit_ceiling` -- the wait would pass the cap |",
    "A provider refusal pauses: `pauseReason: provider_refused`.",
    "A deadline hold is recorded `state: \"timed_out\"`.",
    "The run is held on `review_contract_unrepaired` until resumed.",
])
def test_the_checker_flags_the_0_35_wording(old):
    assert retired_wording(old), old


@pytest.mark.parametrize("new", [
    "The run is `awaiting_input`; `pauseReason: \"operator_input_needed\"` is the park's key.",
    "A refusal ends the run `failed` with `ending: provider_refused`.",
    "The wait does not start; `rate_limit_ceiling_reached` is emitted.",
    "`input.timed_out` is emitted when the input timeout fires.",
    "An iteration ends `timed_out` or `no_traffic_timeout`.",
    "The worker guard (stagnation) archives the approach and replans.",
])
def test_the_checker_passes_the_0_36_wording(new):
    assert retired_wording(new) == [], new


def test_the_compat_cut_hides_only_the_compat_subsection():
    text = "### 5.2 x\nkeep `timed_out` here\n\n#### Loading a 0.35 run directory\n" \
           "`timed_out` loads as exhausted\n\n### 5.3 y\nafter\n"
    cut = _without_compat(text)
    assert "keep" in cut and "after" in cut and "loads as exhausted" not in cut
