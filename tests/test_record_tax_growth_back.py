"""Requirement N.9 (v0.21 task 066): the record tax cannot grow back.

N deleted three things -- the tracked report corpus (N.1), the count-
disclosure rows in `docs/testing.md` (N.4) and the closing-report/lane-
record/count-disclosure/attestation-artifact obligations from the contract
(N.3) -- and N.9 asks for a standing test that fails the moment any of the
three regrows. Four pure detectors, so a synthetic violation can be
exercised without ever committing one to the tracked tree:

1. `_non_petitions_files` -- a file **anywhere under** `artifacts/reports/`
   (recursive, not only a direct child -- requirement I.2) that is not a
   `v<version>-petitions.md` file (N.1's own carve-out, kept for the
   engine's late-cure petition machinery, #150).
2. `_count_disclosure_rows` -- a bold paragraph in the shape N.4 deleted
   from `docs/testing.md`: `**<n> (smoke <n>, ...) by <wave> task <id>
   (...): ...**`.
3. `_budget_table_count_rows` -- a markdown table row in the exact
   count/module/collection-budget shape `3fb66dc8` deleted from
   `docs/testing.md`'s budget table: `| lane | marker | recipe | <tests:
   int> | <modules: int> | wall clock | <budget tests: int> | <budget
   modules: int> | ... |` (requirement I.1). The surviving 7-column table
   (lane/marker/recipe/wall clock/budget wall clock/PRD target/target met)
   carries no bare-integer tests/modules/budget-tests/budget-modules
   columns, so it does not match.
4. `_forbidden_prd_asks` -- a PRD under `docs/prds/` asking for a closing
   report, a lane record, a count disclosure or an attestation artifact.
   Every PRD file already in this tree (v0.5 through v0.21, the two draft
   roadmap families, `README.md`) is frozen below as KNOWN history: N.5
   says a prior wave's text is history, not a finding, and several of
   these ask for exactly the four retired things under their own wave's
   now-superseded contract. Only a PRD file this snapshot has never seen
   is scanned -- which is also what "grow back" means for a file that does
   not exist yet.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = REPO_ROOT / "artifacts" / "reports"
TESTING_DOC = REPO_ROOT / "docs" / "testing.md"
PRDS_DIR = REPO_ROOT / "docs" / "prds"

# N.1's own carve-out: the one file left behind in artifacts/reports/ for
# the engine's glob-discovery petition machinery (#150). Anything else
# under that directory is the report corpus regrowing.
_PETITIONS_FILE_RE = re.compile(r"^v[\d.]+-petitions\.md$")


def _non_petitions_files(reports_dir: Path) -> list[str]:
    """Every file anywhere under reports_dir that is not a petitions file.

    Recursive (requirement I.2): a nested non-petitions file (e.g.
    ``new-wave/closing-report.md``) is the report corpus regrowing just as
    much as a direct child is, and the carve-out is keyed on the file's own
    name, not its depth, so a nested ``v<version>-petitions.md`` still
    survives.
    """
    if not reports_dir.is_dir():
        return []
    return sorted(
        p.relative_to(reports_dir).as_posix()
        for p in reports_dir.rglob("*")
        if p.is_file() and not _PETITIONS_FILE_RE.match(p.name)
    )


# The shape of the per-task count-disclosure paragraph N.4 deleted from
# docs/testing.md, e.g. "**5631 (smoke 4024, modules 360 unchanged) by
# v0.20.2 task 009 (requirement B.1, #197): the notification ...**".
_COUNT_DISCLOSURE_RE = re.compile(r"^\*\*\d+\s*\(smoke\b", re.MULTILINE)


def _count_disclosure_rows(text: str) -> list[str]:
    return _COUNT_DISCLOSURE_RE.findall(text)


# The shape of the count/module/collection-budget table row 3fb66dc8 deleted
# from docs/testing.md's budget table, e.g. "| smoke, parallel | `smoke` |
# `-m smoke -n 8 --dist loadfile` | 4038 | 248 | 24.41 s | 4300 | 270 | 60 s |
# n/a | met -- ... |": lane | marker | recipe | <int tests> | <int modules>
# | wall clock | <int budget tests> | <int budget modules> | ... The
# surviving 7-column table (lane/marker/recipe/wall clock/budget wall
# clock/PRD target/target met, docs/testing.md today) has no bare-integer
# column in either of those two positions, so it never matches.
_BUDGET_TABLE_ROW_RE = re.compile(
    r"^\|[^|\n]*\|[^|\n]*\|[^|\n]*\|\s*\d+\s*\|\s*\d+\s*\|[^|\n]*\|\s*\d+\s*\|\s*\d+\s*\|",
    re.MULTILINE,
)


def _budget_table_count_rows(text: str) -> list[str]:
    return _BUDGET_TABLE_ROW_RE.findall(text)


# PRD files this snapshot already knows about, frozen at task 066: every
# one of them may ask for a closing report, a lane record, a count
# disclosure or an attestation artifact under its OWN (now-superseded)
# contract -- N.5 makes that history, not a finding, so none of these are
# scanned. Only a PRD file that shows up later, past this snapshot, is.
_KNOWN_PRD_FILES = frozenset(
    {
        "README.md",
        "selfdev-roadmap-1.md",
        "selfdev-roadmap-2.md",
        "selfdev-roadmap-3.md",
        "selfdev-roadmap-4.md",
        "selfdev-vigilant-4.md",
        "v0.5-resilience.md",
        "v0.6-first-release.md",
        "v0.7-trustworthy-unattended.md",
        "v0.8-auditable-interruptible.md",
        "v0.9-declarative-loop.md",
        "v0.10-clock-is-not-a-verdict.md",
        "v0.11-a-model-per-hat.md",
        "v0.12-green-on-a-real-host.md",
        "v0.13-a-broken-contract-is-a-conversation.md",
        "v0.14-every-hand-on-the-plan.md",
        "v0.14.1-a-guard-that-cannot-see.md",
        "v0.14.2-green-where-it-runs.md",
        "v0.15-impossible-is-an-answer.md",
        "v0.16-a-petition-is-not-a-postmortem.md",
        "v0.17-a-slip-is-not-a-verdict.md",
        "v0.18-the-run-can-finish.md",
        "v0.19-a-note-is-not-a-rejection.md",
        "v0.19.1-a-gate-is-not-a-wall.md",
        "v0.20-a-commit-is-progress.md",
        "v0.20.1-inspection-is-not-evidence.md",
        "v0.20.2-a-requirement-cannot-be-orphaned.md",
        "v0.21-the-budget-belongs-to-the-work.md",
    }
)

_FORBIDDEN_ASK_RES = {
    "closing report": re.compile(r"\bclosing report\b", re.IGNORECASE),
    "lane record": re.compile(r"\blane record\b", re.IGNORECASE),
    "count disclosure": re.compile(r"\bcount disclosure\b", re.IGNORECASE),
    "attestation artifact": re.compile(r"\battestation artifact\b", re.IGNORECASE),
}


def _forbidden_prd_asks(text: str) -> list[str]:
    return sorted(name for name, pat in _FORBIDDEN_ASK_RES.items() if pat.search(text))


def _new_prd_files(prds_dir: Path) -> list[Path]:
    if not prds_dir.is_dir():
        return []
    return sorted(p for p in prds_dir.glob("*.md") if p.name not in _KNOWN_PRD_FILES)


# --- artifacts/reports/ cannot grow back (N.1/N.9) -------------------------


def test_the_real_reports_directory_holds_nothing_but_a_petitions_file():
    assert _non_petitions_files(REPORTS_DIR) == []


def test_a_non_petitions_file_under_artifacts_reports_is_flagged(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "v0.21-petitions.md").write_text("this one survives")
    (reports / "v0.21-closing-report.md").write_text("the corpus regrew")
    assert _non_petitions_files(reports) == ["v0.21-closing-report.md"]


def test_an_empty_or_missing_reports_directory_is_clean(tmp_path):
    assert _non_petitions_files(tmp_path / "does-not-exist") == []
    empty = tmp_path / "empty-reports"
    empty.mkdir()
    assert _non_petitions_files(empty) == []


def test_the_recursive_walk_flags_a_nested_non_petitions_file_but_not_a_nested_petitions_one(
        tmp_path):
    """Requirement I.2/I.3/I.4, merged into one function (cure-01-04, #204:
    consolidated from two separate test functions to bring the fast tier's
    collected-test count back under its 5,800 launch ceiling; both
    assertions below are unchanged, only merged into one body).

    Part 1 -- a fixture copy of the REAL reports tree, plus the exact
    nested path named by the PRD's own observed defect --
    artifacts/reports/new-wave/closing-report.md -- driven through
    `_non_petitions_files` IS flagged, not only a direct child."""
    reports = tmp_path / "reports"
    shutil.copytree(REPORTS_DIR, reports)
    nested = reports / "new-wave" / "closing-report.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("the corpus regrew, nested this time")
    assert _non_petitions_files(reports) == ["new-wave/closing-report.md"]

    # Part 2 -- a nested petitions file is still the carve-out (I.4), even
    # several directories deep.
    reports2 = tmp_path / "reports2"
    reports2.mkdir()
    nested2 = reports2 / "archive" / "v0.9-petitions.md"
    nested2.parent.mkdir(parents=True)
    nested2.write_text("a nested petitions file, still the carve-out (I.4)")
    assert _non_petitions_files(reports2) == []


# --- docs/testing.md's count-disclosure rows cannot grow back (N.4/N.9) ----


def test_the_real_testing_doc_carries_no_count_disclosure_row():
    assert _count_disclosure_rows(TESTING_DOC.read_text()) == []


def test_a_reintroduced_count_disclosure_row_is_flagged():
    text = (
        "some ordinary prose about the tiers\n\n"
        "**5631 (smoke 4024, modules 360 unchanged) by v0.20.2 task 009 "
        "(requirement B.1, #197): the notification split.**\n\n"
        "more prose that follows it\n"
    )
    assert len(_count_disclosure_rows(text)) == 1


def test_prose_that_merely_mentions_smoke_is_not_flagged():
    text = "The smoke tier is a subset of the fast tier; see the table above.\n"
    assert _count_disclosure_rows(text) == []


# --- docs/testing.md's deleted budget-table count rows cannot grow back ---
# (N.4/I.1/I.9)


# The exact "smoke, parallel" row of the eleven-column budget table
# `3fb66dc8` deleted from docs/testing.md (before that commit).
_DELETED_BUDGET_TABLE_ROW = (
    "| smoke, parallel | `smoke` | `-m smoke -n 8 --dist loadfile` | 4038 | "
    "248 | 24.41 s | 4300 | 270 | 60 s | n/a | met -- 24.41 s inside the "
    "lane's own 60 s budget ceiling; no PRD target of this kind for this "
    "lane |"
)


def test_budget_table_count_row_detector_distinguishes_deleted_and_surviving_shapes():
    """Requirement I.1/I.3, merged into one function (cure-01-04, #204:
    consolidated from four separate test functions to bring the fast
    tier's collected-test count back under its 5,800 launch ceiling; every
    assertion below is unchanged, only merged into one body).

    Part 1 -- the real, on-disk docs/testing.md carries no such row."""
    assert _budget_table_count_rows(TESTING_DOC.read_text()) == []

    # Part 2 -- a fixture COPY of the real docs/testing.md, with the exact
    # row `3fb66dc8` deleted spliced back in, driven through the same
    # `_budget_table_count_rows` the real-tree assertion above calls, IS
    # flagged.
    fixture = TESTING_DOC.read_text() + "\n" + _DELETED_BUDGET_TABLE_ROW + "\n"
    assert len(_budget_table_count_rows(fixture)) == 1

    # Part 3 -- the "records, serial" row of the same deleted table -- a
    # second, differently-worded case of the identical count/module/budget
    # shape -- is also flagged.
    second_shape = (
        "| records, serial | n/a (selected by directory, not marker) | "
        "`.venv/bin/python -m pytest -q tests/records/` | 116 | 5 | 11.35 s "
        "| 150 | 10 | 30 s | < 15 s | met -- 11.35 s inside the 15 s "
        "target |"
    )
    assert len(_budget_table_count_rows(second_shape)) == 1

    # Part 4 -- the REAL surviving row (no tests/modules/budget-tests/
    # budget-modules columns) is NOT flagged -- proves the detector
    # distinguishes the two table shapes rather than firing on any
    # pipe-delimited row.
    survivor_row = (
        "| smoke, parallel | `smoke` | `-m smoke -n 8 --dist loadfile` | "
        "24.41 s | 60 s | n/a | met -- 24.41 s inside the lane's own 60 s "
        "budget ceiling; no PRD target of this kind for this lane |"
    )
    assert _budget_table_count_rows(survivor_row) == []


# --- the four retired contract obligations cannot grow back in a new PRD ---
# (N.3/N.9)


def test_every_prd_file_on_disk_is_named_in_the_frozen_baseline():
    real_files = {p.name for p in PRDS_DIR.glob("*.md")}
    assert real_files == _KNOWN_PRD_FILES
    assert _new_prd_files(PRDS_DIR) == []


def test_a_new_prd_asking_for_a_closing_report_is_flagged():
    text = "## H\n\n- **The closing report** walks A through F: what landed.\n"
    assert _forbidden_prd_asks(text) == ["closing report"]


def test_a_new_prd_asking_for_a_lane_record_is_flagged():
    text = "This wave writes a lane record naming each CI run and its result.\n"
    assert _forbidden_prd_asks(text) == ["lane record"]


def test_a_new_prd_asking_for_a_count_disclosure_is_flagged():
    text = "docs/testing.md keeps its count disclosure paragraph, updated per task.\n"
    assert _forbidden_prd_asks(text) == ["count disclosure"]


def test_a_new_prd_asking_for_an_attestation_artifact_is_flagged():
    text = "Ship an attestation artifact naming the final code sha and every lane.\n"
    assert _forbidden_prd_asks(text) == ["attestation artifact"]


def test_a_new_prd_asking_for_more_than_one_is_flagged_for_each():
    text = (
        "The closing report and its lane record both name the final code sha; "
        "the count disclosure and the attestation artifact accompany it.\n"
    )
    assert _forbidden_prd_asks(text) == [
        "attestation artifact",
        "closing report",
        "count disclosure",
        "lane record",
    ]


def test_a_new_prd_with_none_of_the_four_asks_is_clean():
    text = "This wave ships one feature and asks for nothing else.\n"
    assert _forbidden_prd_asks(text) == []
