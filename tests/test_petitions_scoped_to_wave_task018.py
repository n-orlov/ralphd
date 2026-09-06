"""Task 018 (requirement E.1/E.3, #150): petition discovery and exhaustion
filing are scoped to the CURRENT wave -- never an older wave's file, and
never an obligation that no longer names anything in the current plan or
contract.

Before this task, `petitions.open_petitions_in_workspace` read EVERY
`<wave>-petitions.md` file discovered under the workspace, and
`petitions.file_exhaustion_petition` (via `file_worker_petition`'s own
`files[-1]` fallback) resurrected the newest OLDER wave's file as the
filing target the moment a repo carried more than one wave's history --
exactly the shape steer 001 (#150) reported: two mid-run adjudications
re-adjudicated 14 foreign petitions filed under v0.16-v0.18 alongside this
run's own. Every test here drives the real production functions, never a
test-local shim.
"""

from __future__ import annotations

from pathlib import Path

from ralphd.engine import petitions

CURRENT_CONTRACT = """# PRD v0.19 -- a note is not a rejection

## E -- petitions belong to their wave, adjudications survive (#150, #152)

1. Scoped discovery.
"""


def _write(workspace: Path, name: str, text: str) -> Path:
    reports = workspace / "artifacts" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / name
    path.write_text(text)
    return path


def _section(obligation: str, ptype: str = "self-referential",
             evidence: str = "some evidence") -> str:
    return petitions.format_petition_section(obligation, ptype, evidence)


# ---------------------------------------------------------------------------
# 1. Scoped discovery -- only the current wave's file is read.
# ---------------------------------------------------------------------------


def test_a_foreign_older_waves_open_petition_is_not_surfaced(tmp_path):
    """v0.16/v0.17/v0.18 each carry an OPEN petition; this run's own
    contract names v0.19. None of the older waves' petitions comes back,
    even though every file is discovered on disk."""
    ws = tmp_path / "ws"
    for wave in ("v0.16", "v0.17", "v0.18"):
        _write(ws, f"{wave}-petitions.md",
               f"# {wave} petitions\n" + _section(f"{wave}'s own obligation"))
    _write(ws, "v0.19-petitions.md",
           "# v0.19 petitions\n" + _section("this run's own obligation"))

    found = petitions.open_petitions_in_workspace(ws, CURRENT_CONTRACT)

    assert len(found) == 1
    assert found[0]["obligation"] == "this run's own obligation"
    assert petitions.discover_petition_files(ws) and \
        len(petitions.discover_petition_files(ws)) == 4


def test_only_older_waves_exist_nothing_is_current(tmp_path):
    """No `v0.19-petitions.md` exists at all -- only v0.16-v0.18's history.
    Reading must never resurrect the newest OLD one as if it were current
    (the `files[-1]` bug this task fixes): the result is empty."""
    ws = tmp_path / "ws"
    for wave in ("v0.16", "v0.17", "v0.18"):
        _write(ws, f"{wave}-petitions.md",
               f"# {wave} petitions\n" + _section(f"{wave}'s own obligation"))

    found = petitions.open_petitions_in_workspace(ws, CURRENT_CONTRACT)

    assert found == []


def test_a_lone_older_waves_file_is_not_the_current_wave(tmp_path):
    """The direct case: EXACTLY ONE petitions file exists, it belongs to an
    OLDER wave (`v0.18`), and the contract names `v0.19`. One older file is
    no more current than three: nothing is surfaced, because a contract
    that names its own wave has already said which file is current."""
    ws = tmp_path / "ws"
    _write(ws, "v0.18-petitions.md",
           "# v0.18 petitions\n" + _section("v0.18's own obligation"))

    found = petitions.open_petitions_in_workspace(ws, CURRENT_CONTRACT)

    assert found == []
    # and the derivation itself agrees: no on-disk file counts as current,
    # while the path it WOULD live at is the contract's own wave.
    default_path, existing = petitions._current_wave_petitions_path(
        ws, CURRENT_CONTRACT)
    assert existing is None
    assert default_path.name == "v0.19-petitions.md"


def test_the_single_file_backcompat_fallback_still_applies(tmp_path):
    """Exactly one petitions file on disk is still treated as the current
    wave's when the contract names NO wave at all -- the common
    single-wave-repo shape every pre-existing test in this suite already
    assumes, where there is no wave token to disagree with."""
    ws = tmp_path / "ws"
    _write(ws, "run-petitions.md",
           "# run petitions\n" + _section("the lone obligation"))

    found = petitions.open_petitions_in_workspace(ws, "")

    assert len(found) == 1
    assert found[0]["obligation"] == "the lone obligation"


# ---------------------------------------------------------------------------
# 2. History filtering -- even within the current wave's own file.
# ---------------------------------------------------------------------------


def test_an_obligation_naming_a_task_no_longer_in_the_plan_is_history(tmp_path):
    ws = tmp_path / "ws"
    _write(ws, "v0.19-petitions.md",
           "# v0.19 petitions\n" +
           _section("Task 205: a task this plan no longer carries"))
    tasks = [{"id": "018", "title": "still in the plan"}]

    found = petitions.open_petitions_in_workspace(
        ws, CURRENT_CONTRACT, tasks=tasks)

    assert found == []


def test_an_obligation_naming_a_task_still_in_the_plan_stays_open(tmp_path):
    ws = tmp_path / "ws"
    _write(ws, "v0.19-petitions.md",
           "# v0.19 petitions\n" +
           _section("Task 018: scope petition discovery"))
    tasks = [{"id": "018", "title": "scope petition discovery"}]

    found = petitions.open_petitions_in_workspace(
        ws, CURRENT_CONTRACT, tasks=tasks)

    assert len(found) == 1
    assert found[0]["obligation"] == "Task 018: scope petition discovery"


def test_an_obligation_naming_a_requirement_the_contract_declares_stays_open(
        tmp_path):
    ws = tmp_path / "ws"
    _write(ws, "v0.19-petitions.md",
           "# v0.19 petitions\n" +
           _section("Requirement E, DoD predicate 1 cannot be met"))

    found = petitions.open_petitions_in_workspace(
        ws, CURRENT_CONTRACT, tasks=[{"id": "1", "title": "x"}])

    assert len(found) == 1


def test_an_obligation_naming_a_requirement_the_contract_never_declares_is_history(
        tmp_path):
    ws = tmp_path / "ws"
    _write(ws, "v0.19-petitions.md",
           "# v0.19 petitions\n" +
           _section("Requirement Z, DoD predicate 1 cannot be met"))

    found = petitions.open_petitions_in_workspace(
        ws, CURRENT_CONTRACT, tasks=[{"id": "1", "title": "x"}])

    assert found == []


def test_freeform_obligation_naming_neither_a_task_nor_a_requirement_is_history(
        tmp_path):
    """An obligation that anchors to nothing this run carries -- no task id
    from the current `tasks.json`, no requirement the contract declares -- is
    history, exactly as this task's criterion of record states ("a petition
    whose obligation names no task in the current tasks.json and no
    requirement of the contract"). Free text such as a bare criterion clause
    names neither, so nothing shows it belongs to this run's own plan or
    PRD."""
    ws = tmp_path / "ws"
    _write(ws, "v0.19-petitions.md",
           "# v0.19 petitions\n" +
           _section("the criterion can only be verified after it is met"))

    found = petitions.open_petitions_in_workspace(
        ws, CURRENT_CONTRACT, tasks=[{"id": "1", "title": "x"}])

    assert found == []


def test_an_unanchored_obligation_beside_an_anchored_one_is_the_only_one_dropped(
        tmp_path):
    """The filter is per-petition, not per-file: the current wave's file
    carries one obligation anchored to a live task and one anchored to
    nothing, and only the unanchored one is left out."""
    ws = tmp_path / "ws"
    _write(ws, "v0.19-petitions.md",
           "# v0.19 petitions\n" +
           _section("Task 018: scope petition discovery") +
           _section("no anchor of any kind in this text"))
    tasks = [{"id": "018", "title": "scope petition discovery"}]

    found = petitions.open_petitions_in_workspace(
        ws, CURRENT_CONTRACT, tasks=tasks)

    assert [p["obligation"] for p in found] == [
        "Task 018: scope petition discovery"]


def test_with_no_contract_an_unresolvable_requirement_ref_gets_the_doubt(
        tmp_path):
    """With NO contract text in hand there is nothing to resolve a
    `requirement <letter>` reference against, so that reference is left open
    -- unresolvABLE is not the same fact as unresolved. An obligation naming
    no anchor at all is still history, contract or no contract, and a live
    task reference is still an anchor."""
    ws = tmp_path / "ws"
    _write(ws, "run-petitions.md",
           "# run petitions\n" +
           _section("Requirement E, DoD predicate 1 cannot be met") +
           _section("Task 018: scope petition discovery") +
           _section("nothing here anchors to anything at all"))
    tasks = [{"id": "018", "title": "scope petition discovery"}]

    found = petitions.open_petitions_in_workspace(ws, "", tasks=tasks)

    assert [p["obligation"] for p in found] == [
        "Requirement E, DoD predicate 1 cannot be met",
        "Task 018: scope petition discovery"]


def test_an_obligation_quoting_a_live_tasks_own_criterion_clause_stays_open(
        tmp_path):
    """The third obligation form the worker contract names -- "your own
    task's criterion clause" -- names its task by quoting it, so a clause
    that is still in a current task's `successCriteria` is an anchor and
    the petition stays open, while the same shape of free text that matches
    no current criterion is history (the test above)."""
    ws = tmp_path / "ws"
    _write(ws, "v0.19-petitions.md",
           "# v0.19 petitions\n" +
           _section("the completion names the test node ids covering the "
                    "scoped discovery"))
    tasks = [{"id": "018", "title": "scope petition discovery",
              "successCriteria": (
                  "`open_petitions_in_workspace` reads only the current "
                  "wave's petitions file; the completion names the test "
                  "node ids covering the scoped discovery and the scoped "
                  "filing and they pass.")}]

    found = petitions.open_petitions_in_workspace(
        ws, CURRENT_CONTRACT, tasks=tasks)

    assert len(found) == 1


def test_a_two_word_overlap_with_some_criterion_is_not_an_anchor(tmp_path):
    """The clause anchor is bounded: an obligation too short to be a real
    clause never anchors itself to a criterion that happens to contain it."""
    ws = tmp_path / "ws"
    _write(ws, "v0.19-petitions.md", "# v0.19 petitions\n" +
           _section("they pass"))
    tasks = [{"id": "018", "title": "x",
              "successCriteria": "the node ids are named and they pass."}]

    found = petitions.open_petitions_in_workspace(
        ws, CURRENT_CONTRACT, tasks=tasks)

    assert found == []


def test_tasks_none_keeps_history_filtering_off_for_backward_compat(tmp_path):
    """`tasks=None` (every pre-existing caller) never applies the history
    filter at all, regardless of what the obligation names."""
    ws = tmp_path / "ws"
    _write(ws, "v0.19-petitions.md",
           "# v0.19 petitions\n" +
           _section("Task 205: a task this plan no longer carries"))

    found = petitions.open_petitions_in_workspace(ws, CURRENT_CONTRACT)

    assert len(found) == 1


# ---------------------------------------------------------------------------
# 3. `file_exhaustion_petition` targets the current wave's file.
# ---------------------------------------------------------------------------


def _task(tid: str = "018", title: str = "scope petition discovery") -> dict:
    return {"id": tid, "title": title, "validationNotes": "cannot be met"}


def test_exhaustion_creates_the_current_wave_file_never_reusing_an_older_one(
        tmp_path):
    ws = tmp_path / "ws"
    for wave in ("v0.16", "v0.17", "v0.18"):
        _write(ws, f"{wave}-petitions.md", f"# {wave} petitions\n")

    errors = petitions.file_exhaustion_petition(ws, _task(), CURRENT_CONTRACT)

    assert errors == []
    older = [petitions.discover_petition_files(ws)[i]
             for i, p in enumerate(petitions.discover_petition_files(ws))
             if p.name != "v0.19-petitions.md"]
    for path in older:
        assert petitions.parse_petitions(path.read_text()) == []
    current = ws / "artifacts" / "reports" / "v0.19-petitions.md"
    assert current.exists()
    filed = petitions.parse_petitions(current.read_text())
    assert len(filed) == 1
    assert filed[0]["evidence"] == "cannot be met"
    assert "018" in filed[0]["obligation"]


def test_exhaustion_reuses_an_existing_current_wave_file(tmp_path):
    ws = tmp_path / "ws"
    _write(ws, "v0.18-petitions.md", "# v0.18 petitions\n")
    current = _write(ws, "v0.19-petitions.md", "# v0.19 petitions\n")

    errors = petitions.file_exhaustion_petition(ws, _task(), CURRENT_CONTRACT)

    assert errors == []
    filed = petitions.parse_petitions(current.read_text())
    assert len(filed) == 1
    assert "018" in filed[0]["obligation"]
    # the older wave's file is untouched.
    older = ws / "artifacts" / "reports" / "v0.18-petitions.md"
    assert petitions.parse_petitions(older.read_text()) == []


def test_exhaustion_creates_the_current_wave_file_beside_a_lone_older_one(
        tmp_path):
    """The direct case for the writer: EXACTLY ONE petitions file exists and
    it is an OLDER wave's (`v0.18`). The filing lands in a newly created
    `v0.19-petitions.md`, and v0.18's bytes are unchanged -- the lone-file
    fallback must not make an older wave the filing target."""
    ws = tmp_path / "ws"
    older = _write(ws, "v0.18-petitions.md",
                   "# v0.18 petitions\n" + _section("v0.18's own obligation"))
    before = older.read_text()

    errors = petitions.file_exhaustion_petition(ws, _task(), CURRENT_CONTRACT)

    assert errors == []
    assert older.read_text() == before
    current = ws / "artifacts" / "reports" / "v0.19-petitions.md"
    assert current.exists()
    filed = petitions.parse_petitions(current.read_text())
    assert len(filed) == 1
    assert "018" in filed[0]["obligation"]
    assert filed[0]["evidence"] == "cannot be met"


def test_exhaustion_still_refuses_a_duplicate_obligation_from_any_wave(
        tmp_path):
    """The one-petition-per-obligation rule is unaffected by wave-scoping:
    a duplicate filed under an OLDER wave's file still refuses a new
    filing for the same (normalised) obligation."""
    ws = tmp_path / "ws"
    task = _task()
    obligation = petitions.task_petition_obligation(task)
    _write(ws, "v0.18-petitions.md",
           "# v0.18 petitions\n" +
           _section(obligation, evidence="filed some other way first"))

    errors = petitions.file_exhaustion_petition(ws, task, CURRENT_CONTRACT)

    assert errors
    assert "duplicate" in errors[0]
    # no v0.19 file was created for a refused filing.
    assert not (ws / "artifacts" / "reports" / "v0.19-petitions.md").exists()
