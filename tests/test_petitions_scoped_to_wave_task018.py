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


def test_the_single_file_backcompat_fallback_still_applies(tmp_path):
    """Exactly one petitions file on disk, whatever it is named, is still
    treated as the current wave's -- the common single-wave-repo shape
    every pre-existing test in this suite already assumes."""
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


def test_freeform_obligation_with_no_task_or_requirement_token_stays_open(
        tmp_path):
    """An obligation that names neither kind of token at all (an ordinary
    worker declaration) is never excluded merely because it cannot be
    checked -- only a NAMED, unresolved reference triggers the exclusion."""
    ws = tmp_path / "ws"
    _write(ws, "v0.19-petitions.md",
           "# v0.19 petitions\n" +
           _section("the criterion can only be verified after it is met"))

    found = petitions.open_petitions_in_workspace(
        ws, CURRENT_CONTRACT, tasks=[{"id": "1", "title": "x"}])

    assert len(found) == 1


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
