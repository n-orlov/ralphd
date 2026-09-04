"""Task 012 (PRD requirement D, #114): the first three of the plan's own
five stranding lints (`docs/prds/v0.16-a-petition-is-not-a-postmortem.md`
contract rule 3, lines 79-84) -- relational sha anchors, single-commit
demands on a deliverable whose own criteria also mandate a separate
disclosure/notification/record commit, and forward citation of a task
ordered later in the same plan. Each is folded into `lint_proposal()`
itself (`src/ralphd/engine/plan_ingestion.py:177`), reusing that function's
existing per-task loop rather than adding a fourth separately-run lint.

Every lint below is scoped to ONE clause (`_CLAUSE_SPLIT_RE`), never the
whole criteria string -- the same #86/#76 discipline
`_depends_on_own_terminal_state` already applies to
`lint_own_terminal_state` (`plan_ingestion.py:291-311`), because a criterion
long enough to be useful will otherwise trip on an unrelated mention
elsewhere in the same string. Every lint below ships at least one must-pass
sibling proving exactly that.

Task 013 (this module's own continuation, sections (d) and (e) below) adds
the remaining two: an unscoped write-time occurrence count of a string the
task's own mandated output contains, and a criterion conditioning on the
content or format of untracked run state.

Task 013's THIRD pass also closes task 012's residual gaps (see task 012's
`validationNotes`): the single-commit conflict cue was word-order sensitive,
forward-citation matching accepted numeric task ids only, and the v0.15
must-catch fixtures were reconstructions. All three are fixed here -- and
every v0.15 fixture below is now RECOVERED verbatim from a tracked file in
this repository and asserted to be a whitespace-flattened substring of it,
not reconstructed from a description.
"""

from __future__ import annotations

from pathlib import Path

from ralphd.engine import plan_ingestion as pi


def _basic_task(task_id: str, **overrides) -> dict:
    task = {
        "id": task_id,
        "title": f"do thing {task_id}",
        "successCriteria": f"thing {task_id} is verifiably done",
    }
    task.update(overrides)
    return task


REPO_ROOT = Path(__file__).resolve().parents[1]


def _flattened(rel_path: str) -> str:
    """The tracked file's text with every whitespace run collapsed, so a
    clause recovered out of a hard-wrapped record can be checked verbatim."""
    import re

    return re.sub(r"\s+", " ", (REPO_ROOT / rel_path).read_text())


# ---------------------------------------------------------------------------
# (a) relational sha anchors -- HEAD~1, HEAD^, "this commit's parent"
# ---------------------------------------------------------------------------

# The must-catch fixture named by task 012's own success criteria -- "the
# real v0.15 approach-3 task 004 `HEAD~1` clause". Task 012 shipped a
# RECONSTRUCTION of it and validation was right to call that out (task 012's
# `validationNotes`, endorsed by operator steer 002). The real clause is
# recoverable after all: it sits, word for word, in this repository's own
# tracked v0.15 records. The two tests below pin the recovery itself before
# any lint behaviour is asserted.
def test_the_v015_task_004_relational_sha_clause_is_recovered_verbatim():
    """Not a reconstruction: the fixture's operative clause is a verbatim
    substring of the tracked v0.15 notification record's approach-3 task 004
    entry, and the criterion fixture embeds it unchanged."""
    tracked = _flattened(pi.V015_TASK_004_RECOVERED_CLAUSE_SOURCE)
    assert pi.V015_TASK_004_RECOVERED_RELATIONAL_SHA_CLAUSE in tracked
    assert "## Task 004 (approach 3, requirement F)" in tracked
    assert (
        pi.V015_TASK_004_RECOVERED_RELATIONAL_SHA_CLAUSE
        in pi.TASK_004_HEAD_ANCHOR_CRITERIA
    )


def test_the_prd_itself_names_approach_3_task_004s_head_anchor_clause():
    """The PRD names this exact instance (line 204) and the generic pattern
    it instantiates (line 14), so the recovered clause above is the clause
    the contract is talking about."""
    text = (REPO_ROOT / "docs/prds/v0.16-a-petition-is-not-a-postmortem.md").read_text()
    assert "approach 3 task 004's `HEAD~1` clause" in text
    assert "verify with `HEAD~1`" in text


def test_v015_approach3_task004s_recovered_head_anchor_is_rejected():
    """The recovered real clause has to trip the lint the same way it
    stranded v0.15's task."""
    payload = {
        "tasks": [_basic_task("004", successCriteria=pi.TASK_004_HEAD_ANCHOR_CRITERIA)]
    }
    errors = pi.lint_proposal(payload)
    assert any(
        "004" in e and "relational sha" in e and "HEAD~1" in e for e in errors
    ), errors


def test_head_caret_and_this_commits_parent_are_also_rejected():
    payload = {
        "tasks": [
            _basic_task(
                "001",
                successCriteria="Verify by diffing against HEAD^ once landed.",
            ),
            _basic_task(
                "002",
                successCriteria=(
                    "The change must be verifiable by inspecting this "
                    "commit's parent for the prior value."
                ),
            ),
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("001" in e and "relational sha" in e for e in errors)
    assert any("002" in e and "relational sha" in e for e in errors)


def test_a_criterion_that_merely_mentions_a_sha_or_head_is_not_flagged():
    """Must-pass sibling: an absolute sha, or a bare mention of HEAD/a
    commit with no relational anchor, is legitimate and must not trip."""
    payload = {
        "tasks": [
            _basic_task(
                "001",
                successCriteria=(
                    "Verify against the commit sha `28f3a766` once this "
                    "task's own commit lands; HEAD's own sha at push time "
                    "is quoted in the report."
                ),
            )
        ]
    }
    assert pi.lint_proposal(payload) == []


# ---------------------------------------------------------------------------
# (b) single-commit demand vs a mandated separate disclosure/notification/
#     record commit
# ---------------------------------------------------------------------------

def test_a_single_commit_demand_with_a_separate_notification_commit_is_rejected():
    payload = {
        "tasks": [
            _basic_task(
                "005",
                successCriteria=(
                    "Ship the whole deliverable in a single commit. This "
                    "task also mandates a separate notification commit "
                    "logging the send to the operator."
                ),
            )
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any(
        "005" in e and "single commit" in e and "separate" in e for e in errors
    ), errors


def test_exactly_one_commit_with_its_own_record_commit_is_also_rejected():
    payload = {
        "tasks": [
            _basic_task(
                "006",
                successCriteria=(
                    "The fix lands in exactly one commit. The closing "
                    "record needs its own record commit disclosing the "
                    "deviation."
                ),
            )
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("006" in e and "single commit" in e for e in errors)


def test_legitimate_one_commit_mention_without_a_second_mandate_is_accepted():
    """Must-pass sibling: 'one commit'/'a single commit' with NO separate
    disclosure/notification/record commit mandated elsewhere is legitimate
    and must not trip -- this is exactly this task's own boundary clause
    shape used throughout this wave's own plan."""
    payload = {
        "tasks": [
            _basic_task(
                "007",
                successCriteria=(
                    "At least one commit lands the fix; the notification "
                    "entry may be appended in that same commit."
                ),
            )
        ]
    }
    assert pi.lint_proposal(payload) == []


def test_a_single_commit_demand_is_caught_in_either_word_order():
    """Task 012's residual gap (a), see its `validationNotes`: the cue only
    understood modifier-then-noun ("a separate notification commit"), so the
    commonest wording of all -- noun first, "The notification must land in a
    separate commit" -- returned no finding at all."""
    payload = {
        "tasks": [
            _basic_task(
                "025",
                successCriteria=(
                    "Ship the deliverable in a single commit. The "
                    "notification must land in a separate commit."
                ),
            ),
            _basic_task(
                "026",
                successCriteria=(
                    "The whole change lands in a single commit. The "
                    "disclosure of the deviation is recorded in its own "
                    "commit afterwards."
                ),
            ),
        ]
    }
    errors = pi.lint_proposal(payload)
    for tid in ("025", "026"):
        assert any(tid in e and "single commit" in e for e in errors), (tid, errors)


# ---------------------------------------------------------------------------
# (c) forward citation of a later task's commit sha
# ---------------------------------------------------------------------------

def test_citing_a_later_tasks_commit_sha_is_rejected():
    payload = {
        "tasks": [
            _basic_task(
                "010",
                successCriteria="Cite the commit sha of task 015 as evidence.",
            ),
            _basic_task("015"),
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any(
        "010" in e and "015" in e and "LATER" in e for e in errors
    ), errors


def test_citing_task_ns_own_commit_worded_the_other_way_is_also_rejected():
    payload = {
        "tasks": [
            _basic_task(
                "010",
                successCriteria="Name task 015's own commit sha in the report.",
            ),
            _basic_task("015"),
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("010" in e and "015" in e for e in errors)


def test_citing_an_earlier_tasks_commit_sha_is_accepted():
    """Must-pass sibling: citing a task ordered EARLIER in the same plan
    (already committed by the time this task runs) is order-honest and
    must not trip."""
    payload = {
        "tasks": [
            _basic_task("002"),
            _basic_task(
                "010",
                successCriteria="Cite the commit sha of task 002 as evidence.",
            ),
        ]
    }
    assert pi.lint_proposal(payload) == []


def test_a_bare_task_reference_with_no_commit_language_is_accepted():
    """Must-pass sibling: naming another task at all (no commit/sha
    language) is ordinary cross-referencing, not a forward citation."""
    payload = {
        "tasks": [
            _basic_task(
                "010",
                successCriteria="This task's scope overlaps task 015's; see its title.",
            ),
            _basic_task("015"),
        ]
    }
    assert pi.lint_proposal(payload) == []


def test_a_tasks_own_self_reference_is_never_a_forward_citation():
    """'task 010's own commit sha' inside task 010's OWN criteria names
    itself, never anything ordered later -- not a forward citation."""
    payload = {
        "tasks": [
            _basic_task(
                "010",
                successCriteria="Cite task 010's own commit sha in the report.",
            ),
        ]
    }
    assert pi.lint_proposal(payload) == []


def test_a_forward_citation_between_non_numeric_task_ids_is_rejected():
    """Task 012's residual gap (b), see its `validationNotes`: the id was
    matched as `\\d+`, so a plan whose ids are not numbers (`alpha` citing a
    later `beta`) escaped the lint entirely."""
    payload = {
        "tasks": [
            _basic_task(
                "alpha",
                successCriteria="alpha is done once it cites task beta's commit sha.",
            ),
            _basic_task("beta"),
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any(
        "alpha" in e and "beta" in e and "LATER" in e for e in errors
    ), errors


def test_a_non_numeric_backward_citation_is_still_accepted():
    """Must-pass sibling for the widened id token: a non-numeric id citing a
    task ordered EARLIER stays order-honest, and a token naming no task in
    the plan at all is never reported."""
    payload = {
        "tasks": [
            _basic_task("alpha"),
            _basic_task(
                "beta",
                successCriteria=(
                    "beta cites task alpha's commit sha, and the commit sha "
                    "of task gamma from the previous wave."
                ),
            ),
        ]
    }
    assert pi.lint_proposal(payload) == []


# ---------------------------------------------------------------------------
# (d) unscoped write-time occurrence count of a string the task's own
#     mandated output contains
# ---------------------------------------------------------------------------

def test_the_prd_itself_describes_the_write_time_occurrence_count_pattern():
    """No individual v0.15 task number survives for this pattern (unlike
    (a)'s task 004, whose real clause is recovered verbatim above) -- what
    the tracked PRD records is the count SHAPE itself, word for word, and
    `UNSCOPED_SHA_OCCURRENCE_CRITERIA` instantiates exactly that recovered
    shape."""
    normalised = _flattened(pi.V015_RECOVERED_CLAUSE_SOURCE_PRD)
    assert pi.V015_RECOVERED_WRITE_TIME_COUNT_PATTERN in normalised
    assert (
        "a write-time occurrence count of a sha that the task's own "
        "mandated additions increment"
    ) in normalised


def test_the_recovered_unscoped_occurrence_count_criteria_is_rejected():
    payload = {
        "tasks": [
            _basic_task("009", successCriteria=pi.UNSCOPED_SHA_OCCURRENCE_CRITERIA)
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any(
        "009" in e and "unscoped write-time occurrence count" in e for e in errors
    ), errors


def test_other_occurrence_count_phrasings_are_also_rejected():
    payload = {
        "tasks": [
            _basic_task(
                "011",
                successCriteria=(
                    "The notification log must show the phrase 'petition "
                    "filed' 12 instances, though this task's own commit "
                    "appends further entries containing that phrase."
                ),
            ),
            _basic_task(
                "012",
                successCriteria=(
                    "The report contains exactly 4 entries naming the "
                    "deviation, and this task's own additions grow that "
                    "count."
                ),
            ),
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("011" in e and "unscoped write-time occurrence count" in e for e in errors)
    assert any("012" in e and "unscoped write-time occurrence count" in e for e in errors)


def test_a_self_growing_count_written_with_a_spelled_out_number_is_rejected():
    """Second pass (validation finding): the first cut of this lint only
    understood digits, so `four occurrences` -- the same banned shape in
    ordinary prose -- sailed through."""
    payload = {
        "tasks": [
            _basic_task(
                "018",
                successCriteria=(
                    "The closing report must show four occurrences of the "
                    "launch sha, even though this task's own commit appends "
                    "further occurrences of it to that same report."
                ),
            ),
            _basic_task(
                "019",
                successCriteria=(
                    "The notification log shows up nineteen times the phrase "
                    "'sweep recorded'; this task's own entry increments that "
                    "count."
                ),
            ),
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("018" in e and "unscoped write-time occurrence count" in e for e in errors), errors
    assert any("019" in e and "unscoped write-time occurrence count" in e for e in errors), errors


def test_a_count_of_a_string_the_task_s_own_output_carries_is_rejected():
    """Third pass (validation finding): self-growth stated WITHOUT a growth
    verb near a counted noun -- the count clause names the counted string
    ("four occurrences of `abc123`") and a neighbouring clause says the
    task's own mandated output carries that same string -- returned no
    finding at all. It is the plainest instance of the banned shape: a count
    of a string the task's own mandated output contains."""
    payload = {
        "tasks": [
            _basic_task(
                "027",
                successCriteria=(
                    "The report must show four occurrences of `abc123`. "
                    "This task's mandated output is a new line in that "
                    "report containing `abc123`."
                ),
            ),
            _basic_task(
                "028",
                successCriteria=(
                    "The notification log shows 'sweep recorded' 3 times. "
                    "This task's own entry in that log names 'sweep "
                    "recorded' as well."
                ),
            ),
        ]
    }
    errors = pi.lint_proposal(payload)
    for tid in ("027", "028"):
        assert any(
            tid in e and "unscoped write-time occurrence count" in e for e in errors
        ), (tid, errors)


def test_a_count_beside_an_unrelated_path_mention_is_accepted():
    """Must-pass sibling for the token-identity route, and the exact false
    positive it first produced: a sha-scoped criterion that happens to name
    `artifacts/reports/` next to its count, while its ordinary notification
    clause ("this task's entry appended to artifacts/reports/...") names the
    same directory, is NOT a count of a string the task's own output grows.
    Directory prefixes are never the counted string, and only literals
    beside the count phrase are candidates at all.

    Task 013 (requirement E, #143) note: this fixture's notification clause
    names an "observed HTTP status", which the seventh lint added by that
    task now refuses outright (the PRD bans an HTTP status in a criterion
    at all). The fixture text is kept verbatim -- it is the recovered
    false positive this test exists for -- so the assertion is narrowed to
    THIS lint's own route: no write-time-count error, whatever the seventh
    lint says about the notification clause."""
    payload = {
        "tasks": [
            _basic_task(
                "029",
                successCriteria=(
                    "A tracked record under `artifacts/reports/` retires the "
                    "19 pairs measured at planning, stating the number it "
                    "actually retired sha-scoped. This task's entry appended "
                    "to `artifacts/reports/v0.16-operator-notifications.md` "
                    "carries endpoint, substance and observed HTTP status."
                ),
            )
        ]
    }
    assert [
        e for e in pi.lint_proposal(payload) if "structurally unsatisfiable" not in e
    ] == []


def test_a_static_count_the_task_does_not_grow_is_accepted():
    """Must-pass sibling, and the second pass's other validation finding: a
    count of something the task's own mandated output does NOT grow is
    static, verifiable and legitimate -- the banned shape is a *write-time*
    count, not any count."""
    payload = {
        "tasks": [
            _basic_task(
                "020",
                successCriteria="The API response fixture must contain exactly 4 entries.",
            ),
            _basic_task(
                "021",
                successCriteria=(
                    "The API fixture must contain exactly 4 entries. This "
                    "task's own commit adds the fixture."
                ),
            ),
        ]
    }
    assert pi.lint_proposal(payload) == []


def test_a_sha_scoped_figure_is_accepted():
    """Must-pass sibling: 'at `<sha>`: N' is exactly the sha-scoped form
    the standing rules mandate for every figure in a tracked record
    (PRD contract rule 2) -- it must never trip this lint."""
    payload = {
        "tasks": [
            _basic_task(
                "013",
                successCriteria=(
                    "The record states, at `28f3a766`: 19 occurrences of "
                    "the retired sha, discharged by this task's own commit."
                ),
            )
        ]
    }
    assert pi.lint_proposal(payload) == []


def test_a_bare_count_with_no_occurrence_language_is_accepted():
    """Must-pass sibling: '19 tasks remain' names a count with no
    occurrence-of-a-string language at all and must not trip."""
    payload = {
        "tasks": [
            _basic_task("014", successCriteria="19 tasks remain pending in the plan.")
        ]
    }
    assert pi.lint_proposal(payload) == []


# ---------------------------------------------------------------------------
# (e) conditioning on the content or format of untracked run state
# ---------------------------------------------------------------------------

def test_the_v015_task_001_notes_content_clause_is_recovered_verbatim():
    """Not a reconstruction: the operative clause of the real v0.15
    approach-4 task-001 incident is a verbatim substring of the tracked PRD,
    and the criterion fixture embeds it unchanged -- only the minimal frame
    around it ("This task's landed fix is not accepted while ...") is this
    repository's."""
    tracked = _flattened(pi.V015_RECOVERED_CLAUSE_SOURCE_PRD)
    assert pi.V015_TASK_001_RECOVERED_NOTES_CONTENT_CLAUSE in tracked
    assert (
        pi.V015_TASK_001_RECOVERED_NOTES_CONTENT_CLAUSE
        in pi.TASK_001_NOTES_INLINE_CRITERIA
    )


def test_the_prd_itself_names_approach_4_task_001s_notes_md_demand():
    """The PRD both describes the incident (lines 18-20, the source of the
    recovered clause above) and names this exact instance (line 204)."""
    text = (REPO_ROOT / "docs/prds/v0.16-a-petition-is-not-a-postmortem.md").read_text()
    assert "approach 4 task 001's notes.md demand" in text
    assert "pointed to*" in text
    assert "the artifact holding 19 stale-sha pairs instead of inlining them" in text


def test_the_recovered_notes_inline_demand_is_rejected():
    payload = {
        "tasks": [
            _basic_task("001", successCriteria=pi.TASK_001_NOTES_INLINE_CRITERIA)
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any(
        "001" in e and "untracked run state" in e and "notes.md" in e for e in errors
    ), errors


def test_other_untracked_state_content_demands_are_also_rejected():
    payload = {
        "tasks": [
            _basic_task(
                "015",
                successCriteria=(
                    "The handoff notes must quote the closing report's "
                    "figures verbatim before this task is considered done."
                ),
            ),
            _basic_task(
                "016",
                successCriteria=(
                    "A file outside the repository tree must contain the "
                    "same table in the exact same format as the tracked "
                    "artifact."
                ),
            ),
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("015" in e and "untracked run state" in e for e in errors)
    assert any("016" in e and "untracked run state" in e for e in errors)


def test_content_demands_on_other_out_of_tree_paths_are_also_rejected():
    """Second pass (validation finding): the first cut's path cues covered
    `notes.md`/`handoff notes` but not other concrete paths that cannot be
    inside the repository tree, and its demand cues left out the commonest
    wording of all -- `must list`."""
    payload = {
        "tasks": [
            _basic_task(
                "022",
                successCriteria="/tmp/handoff.txt must contain the full table.",
            ),
            _basic_task(
                "023",
                successCriteria=(
                    "/run/ralphd/tasks.json must record this task's status "
                    "before the closing report sends."
                ),
            ),
            _basic_task(
                "024",
                successCriteria="The handoff notes must list all 19 stale sha pairs.",
            ),
        ]
    }
    errors = pi.lint_proposal(payload)
    for tid in ("022", "023", "024"):
        assert any(tid in e and "untracked run state" in e for e in errors), (tid, errors)


def test_content_demands_on_paths_no_root_list_enumerates_are_rejected():
    """Third pass (validation finding): the fixed root list still missed
    roots it never enumerated (`/srv/...`, `/data/...`) and missed
    parent-escape relative paths (`../run-handoff.txt`) entirely -- none of
    which can name a tracked path inside this repository."""
    payload = {
        "tasks": [
            _basic_task(
                "030",
                successCriteria="/srv/ralphd-handoff.txt must contain the full stale-sha table.",
            ),
            _basic_task(
                "031",
                successCriteria="/data/run-handoff.txt must list every retired pair.",
            ),
            _basic_task(
                "032",
                successCriteria="../run-handoff.txt must show the same figures as the artifact.",
            ),
        ]
    }
    errors = pi.lint_proposal(payload)
    for tid in ("030", "031", "032"):
        assert any(tid in e and "untracked run state" in e for e in errors), (tid, errors)


def test_a_format_condition_on_scratch_state_is_rejected():
    """Third pass (validation finding): a pure FORMAT condition -- "The
    handoff notes are required to use valid JSON." -- slipped through whole,
    though contract rule 1 bans conditioning on the format of untracked run
    state as squarely as on its content."""
    payload = {
        "tasks": [
            _basic_task(
                "033",
                successCriteria="The handoff notes are required to use valid JSON.",
            ),
            _basic_task(
                "034",
                successCriteria=(
                    "/run/ralphd/notes.md must follow the same heading order "
                    "as the tracked report."
                ),
            ),
        ]
    }
    errors = pi.lint_proposal(payload)
    for tid in ("033", "034"):
        assert any(tid in e and "untracked run state" in e for e in errors), (tid, errors)


def test_a_url_path_is_never_read_as_an_out_of_tree_file_path_is_accepted():
    """Must-pass sibling for the widened path cues: an HTTP endpoint path
    (`GET /repos/n-orlov/ralphd/issues/114`, `POST .../notify`) is not a
    filesystem path, and a criterion quoting its observed response in a
    TRACKED record is exactly this wave's own closure shape -- it must never
    trip.

    Task 013 (requirement E, #143) note: as with fixture 029 above, the
    clause's "observed HTTP status" is refused by the seventh lint that
    task added, so the assertion here is narrowed to THIS lint's own route
    (no out-of-tree-path error) while the fixture text stays verbatim."""
    payload = {
        "tasks": [
            _basic_task(
                "035",
                successCriteria=(
                    "The closure is verified by a fresh "
                    "`GET /repos/n-orlov/ralphd/issues/114` whose observed "
                    "state is quoted in a tracked record, and the "
                    "notification `POST http://100.71.162.65:8090/notify` "
                    "records its observed HTTP status."
                ),
            )
        ]
    }
    assert [
        e for e in pi.lint_proposal(payload) if "structurally unsatisfiable" not in e
    ] == []


def test_an_artifact_existence_pointer_is_accepted():
    """Must-pass sibling: requiring an artifact FILE to exist, with the
    scratch-state path merely pointing at it, is exactly the pointer form
    the contract's scratch-state rule declares sufficient -- it must never
    trip."""
    payload = {
        "tasks": [
            _basic_task(
                "017",
                successCriteria=(
                    "artifacts/reports/v0.16-example.md must exist, listing "
                    "the 19 stale-sha pairs; /run/ralphd/notes.md need only "
                    "point to that artifact."
                ),
            )
        ]
    }
    assert pi.lint_proposal(payload) == []


# ---------------------------------------------------------------------------
# End to end: the refusal reaches the operator through the existing plan
# re-ask ladder, not just `lint_proposal` in isolation -- drives
# `enforce_plan_ingestion` (pointer schema -> file read -> `_parse_proposal`
# -> `lint_proposal`), same shape as
# `test_plan_ingestion.py::test_a_lint_failure_reprompts_with_the_specific_error_then_recovers`.
# ---------------------------------------------------------------------------

def _pointer(path: str) -> str:
    import json as _json
    return _json.dumps({"proposal_path": path, "summary": "a plan"})


def _write(tmp_path, name: str, doc) -> None:
    import json as _json
    text = doc if isinstance(doc, str) else _json.dumps(doc)
    (tmp_path / name).write_text(text)


def test_an_unscoped_occurrence_count_reprompts_then_recovers_end_to_end(tmp_path):
    _write(tmp_path, "plan-proposal.json",
           {"tasks": [_basic_task("009", successCriteria=pi.UNSCOPED_SHA_OCCURRENCE_CRITERIA)]})
    attempts = []

    def respond(attempt, prior_errors):
        attempts.append(prior_errors)
        if attempt == 1:
            _write(tmp_path, "plan-proposal.json", {"tasks": [_basic_task("009")]})
        return _pointer("plan-proposal.json")

    result = pi.enforce_plan_ingestion(
        respond, max_retries=2, read_proposal=pi.file_reader(tmp_path)
    )
    assert result.ok
    assert result.attempts == 2
    assert attempts[0] is None
    assert attempts[1] and any(
        "unscoped write-time occurrence count" in e for e in attempts[1]
    )


def test_a_scratch_state_content_demand_reprompts_then_recovers_end_to_end(tmp_path):
    _write(tmp_path, "plan-proposal.json",
           {"tasks": [_basic_task("001", successCriteria=pi.TASK_001_NOTES_INLINE_CRITERIA)]})
    attempts = []

    def respond(attempt, prior_errors):
        attempts.append(prior_errors)
        if attempt == 1:
            _write(tmp_path, "plan-proposal.json", {"tasks": [_basic_task("001")]})
        return _pointer("plan-proposal.json")

    result = pi.enforce_plan_ingestion(
        respond, max_retries=2, read_proposal=pi.file_reader(tmp_path)
    )
    assert result.ok
    assert result.attempts == 2
    assert attempts[0] is None
    assert attempts[1] and any(
        "untracked run state" in e for e in attempts[1]
    )


def test_the_newly_caught_shapes_also_reach_the_operator_end_to_end(tmp_path):
    """Third pass: the two shapes that used to slip through -- a count of a
    string the task's own mandated output carries, and a pure format
    condition on scratch state -- must reach the operator through the SAME
    plan re-ask ladder, not merely return a string from `lint_proposal`."""
    stranded = {
        "tasks": [
            _basic_task(
                "027",
                successCriteria=(
                    "The report must show four occurrences of `abc123`. "
                    "This task's mandated output is a new line in that "
                    "report containing `abc123`."
                ),
            ),
            _basic_task(
                "033",
                successCriteria="The handoff notes are required to use valid JSON.",
            ),
        ]
    }
    _write(tmp_path, "plan-proposal.json", stranded)
    attempts = []

    def respond(attempt, prior_errors):
        attempts.append(prior_errors)
        if attempt == 1:
            _write(tmp_path, "plan-proposal.json",
                   {"tasks": [_basic_task("027"), _basic_task("033")]})
        return _pointer("plan-proposal.json")

    result = pi.enforce_plan_ingestion(
        respond, max_retries=2, read_proposal=pi.file_reader(tmp_path)
    )
    assert result.ok
    assert result.attempts == 2
    assert attempts[1] and any(
        "027" in e and "unscoped write-time occurrence count" in e for e in attempts[1]
    ), attempts[1]
    assert any(
        "033" in e and "untracked run state" in e for e in attempts[1]
    ), attempts[1]


# ---------------------------------------------------------------------------
# Task 023 (requirement E.2, #116 section 1): the seven evasion strings
# #116 names verbatim as future fixtures for this module, recorded here
# word for word (fetched fresh from the issue body this iteration -- not
# reconstructed). Each is labelled MUST-CATCH (the lint, as shipped after
# task 013's later passes, already flags it -- #116's own parenthetical
# names the ORIGINAL gap, since closed) or DOCUMENTED-AND-ACCEPTED (still
# out of the lint's lexical reach as shipped, reason given per fixture).
# Five of the seven (1, 3, 4, 5, 6) #116 states as complete quoted
# sentences and they appear here character for character. The remaining
# two (2 and 7) #116 states as DESCRIBED shapes rather than full quoted
# sentences -- item 2 quotes only the fragment "the commit sha of task
# `beta`" inside a prose description of the id shape, and item 7 names a
# fixture in this module by reference -- so for those the issue's own
# quoted fragment / named fixture is what is reproduced verbatim, embedded
# in the minimal criteria string that exercises the described shape.
# The open-ended "reject every rephrasing" standard #116 section 3 restates
# is ruled unmeetable by a lexical lint -- these seven are the enumerated
# instances the contract actually asks for, not a claim that every future
# rephrasing of a banned shape is now caught.
# ---------------------------------------------------------------------------


def test_the_prd_itself_restates_the_reject_every_rephrasing_ruling():
    """Recovered verbatim from the tracked PRD, not reconstructed: the
    ruling this whole section labels fixtures against."""
    tracked = _flattened("docs/prds/v0.17-a-slip-is-not-a-verdict.md")
    assert (
        'documented-and-accepted where the fix is judged out of lexical '
        'reach (state which, per fixture, in the test file; "reject every '
        'rephrasing" stays ruled unmeetable).'
    ) in tracked


# --- #116 fixture 1 -- MUST-CATCH ------------------------------------------
# Word-order sensitivity was task 012's own residual gap; task 013's third
# pass fixed it (`_SEPARATE_RECORD_COMMIT_RE` now understands noun-first
# phrasing too). This exact issue string is caught today.
ISSUE_116_FIXTURE_1_SINGLE_COMMIT_WORD_ORDER = (
    "Ship the deliverable in a single commit. The notification must land "
    "in a separate commit."
)


def test_issue_116_fixture_1_single_commit_word_order_is_caught():
    payload = {
        "tasks": [
            _basic_task(
                "040", successCriteria=ISSUE_116_FIXTURE_1_SINGLE_COMMIT_WORD_ORDER
            )
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("040" in e and "single commit" in e for e in errors), errors


# --- #116 fixture 2 -- MUST-CATCH ------------------------------------------
# A non-numeric task id (`alpha`) citing "the commit sha of task `beta`"
# where `beta` is a non-numeric id ordered later. The numeric-only id
# token was task 012's residual gap (b); task 013's third pass widened
# `_TASK_ID_TOKEN` off `\d+`, so this is caught today.
# #116's quoted fragment for this item is "the commit sha of task `beta`",
# backticks included; it is embedded verbatim below.
ISSUE_116_FIXTURE_2_QUOTED_FRAGMENT = "the commit sha of task `beta`"
ISSUE_116_FIXTURE_2_NON_NUMERIC_FORWARD_CITATION = (
    "This task cannot be closed without citing "
    + ISSUE_116_FIXTURE_2_QUOTED_FRAGMENT
    + "."
)


def test_issue_116_fixture_2_non_numeric_forward_citation_is_caught():
    payload = {
        "tasks": [
            _basic_task(
                "alpha",
                successCriteria=ISSUE_116_FIXTURE_2_NON_NUMERIC_FORWARD_CITATION,
            ),
            _basic_task("beta"),
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("alpha" in e and "beta" in e and "LATER" in e for e in errors), errors
    # The issue's own quoted fragment, backticks and all, is what the
    # fixture carries -- not a paraphrase of it.
    assert (
        ISSUE_116_FIXTURE_2_QUOTED_FRAGMENT
        in ISSUE_116_FIXTURE_2_NON_NUMERIC_FORWARD_CITATION
    )


# --- #116 fixture 3 -- DOCUMENTED-AND-ACCEPTED, out of lexical reach -------
# The unscoped-occurrence-count lint's token-identity route
# (`_counted_string_tokens`) only reads a counted string out of a
# backticked or quoted literal (`_COUNTED_STRING_TOKEN_RE`). This exact
# issue fixture names the string bare, with no delimiter (`abc123`, not
# `` `abc123` ``), and no growth verb sits near the count phrase either, so
# neither of `_unscoped_occurrence_count_clause`'s two routes fires.
# Catching every bare-identifier count would mean treating any token near a
# count phrase as a candidate, reintroducing exactly the false-positive
# risk task 013's second pass (the static-count must-pass sibling) ruled
# out -- this is the "reject every rephrasing" shape, out of reach for a
# lexical lint.
ISSUE_116_FIXTURE_3_BARE_IDENTIFIER_COUNT = (
    "The report must show four occurrences of abc123. This task's "
    "mandated output is a new line in that report containing abc123."
)


def test_issue_116_fixture_3_bare_identifier_count_is_documented_and_accepted():
    payload = {
        "tasks": [
            _basic_task(
                "041", successCriteria=ISSUE_116_FIXTURE_3_BARE_IDENTIFIER_COUNT
            )
        ]
    }
    assert pi.lint_proposal(payload) == []


# --- #116 fixture 4 -- DOCUMENTED-AND-ACCEPTED, out of lexical reach -------
# The untracked-path cue (`_UNTRACKED_PATH_CUE_RE`) recognizes an absolute
# path as out-of-tree via one of two general routes: a known filesystem
# root (`run`/`tmp`/`var`/.../`srv`/`data`/...) or a dotted file
# extension. `/custom-root/handoff` is neither -- an unenumerated root with
# no extension -- so it is not recognized as a filesystem path at all.
# Enumerating every conceivable root name is the same open-ended shape
# section 3's ruling puts out of scope for a lexical lint.
ISSUE_116_FIXTURE_4_UNENUMERATED_ROOT = (
    "/custom-root/handoff must contain the complete table."
)


def test_issue_116_fixture_4_unenumerated_root_is_documented_and_accepted():
    payload = {
        "tasks": [
            _basic_task("042", successCriteria=ISSUE_116_FIXTURE_4_UNENUMERATED_ROOT)
        ]
    }
    assert pi.lint_proposal(payload) == []


# --- #116 fixture 5 -- DOCUMENTED-AND-ACCEPTED, out of lexical reach -------
# `/srv/...` IS a recognized out-of-tree path (`_UNTRACKED_PATH_CUE_RE`
# enumerates `srv`), but the content/format demand cue
# (`_CONTENT_OR_FORMAT_DEMAND_RE`) has no "identical to" phrasing -- its
# modal-verb route (`must`/`shall`/... plus a content verb) and its
# bare-verb route (`contains?`, `duplicat\w*`, `quote\w*`) both miss the
# copula construction "are identical to". Every rephrasing of
# "matches byte-for-byte" is exactly the unmeetable standard section 3
# names.
ISSUE_116_FIXTURE_5_IDENTICAL_TO_COPULA = (
    "The contents of /srv/handoff.txt are identical to the tracked report."
)


def test_issue_116_fixture_5_identical_to_copula_is_documented_and_accepted():
    payload = {
        "tasks": [
            _basic_task("043", successCriteria=ISSUE_116_FIXTURE_5_IDENTICAL_TO_COPULA)
        ]
    }
    assert pi.lint_proposal(payload) == []


# --- #116 fixture 6 -- DOCUMENTED-AND-ACCEPTED, out of lexical reach -------
# `/srv/...` is recognized the same way as fixture 5 above, but
# "has JSON syntax" trips neither the modal-verb route (bare "has" is not
# "has to") nor the explicit-phrasing route (`valid JSON`,
# `JSON-formatted`); "JSON syntax" names neither. Same unmeetable standard.
ISSUE_116_FIXTURE_6_HAS_JSON_SYNTAX = "/srv/handoff.txt has JSON syntax."


def test_issue_116_fixture_6_has_json_syntax_is_documented_and_accepted():
    payload = {
        "tasks": [
            _basic_task("044", successCriteria=ISSUE_116_FIXTURE_6_HAS_JSON_SYNTAX)
        ]
    }
    assert pi.lint_proposal(payload) == []


# --- #116 fixture 7 -- MUST-CATCH ------------------------------------------
# #116 named the self-labelled RECONSTRUCTION fixture standing in for the
# real v0.15 approach-3 task-004 relational-sha clause -- a fixture named
# by reference to THIS module rather than quoted whole, per the section
# preamble above. That gap is CLOSED, not merely documented:
# `V015_TASK_004_RECOVERED_RELATIONAL_SHA_CLAUSE` /
# `TASK_004_HEAD_ANCHOR_CRITERIA` above are RECOVERED verbatim from the
# tracked `artifacts/reports/v0.15-operator-notifications.md` record, and
# the fixture is squarely IN the lint's lexical reach: it is the exact
# clause `test_v015_approach3_task004s_recovered_head_anchor_is_rejected`
# (earlier in this module) already asserts the relational-sha lint catches
# via a `HEAD~1` anchor. Fixture 7 is therefore MUST-CATCH, not
# DOCUMENTED-AND-ACCEPTED -- unlike fixtures 3-6, there is no lexical gap
# to document here, only a recovery to pin and a catch to assert directly,
# both below, with the pre-existing recovery/containment assertions kept.
def test_issue_116_fixture_7_reconstruction_gap_is_already_closed():
    assert (
        pi.V015_TASK_004_RECOVERED_RELATIONAL_SHA_CLAUSE
        in pi.TASK_004_HEAD_ANCHOR_CRITERIA
    )
    tracked = _flattened(pi.V015_TASK_004_RECOVERED_CLAUSE_SOURCE)
    assert pi.V015_TASK_004_RECOVERED_RELATIONAL_SHA_CLAUSE in tracked
    payload = {
        "tasks": [
            _basic_task("045", successCriteria=pi.TASK_004_HEAD_ANCHOR_CRITERIA)
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any(
        "045" in e and "relational sha" in e and "HEAD~1" in e for e in errors
    ), errors

