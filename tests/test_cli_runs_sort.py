"""Task 055 (#9): `ralphctl runs` defaults newest-first and sorts on the same
keys as the hub's run-list columns (task 054, `RUN_COLUMNS` in
cli/web/app.js), with `--reverse`, `--json` in the *same* order as the human
table, and composition with `--state`.

Black-box: the real `ralphctl` executable over an on-disk registry fixture
(no docker, no engine) via the stub-docker `Ctl` harness. The run-id order
of the fixture is deliberately NOT the started-at order, so a test that
passes by accident on alphabetical order cannot exist.
"""

from __future__ import annotations

import json

from test_cli_docker import Ctl, ctl, unix_sock

from ralphd.cli.main import RUN_SORT_KEYS, sort_run_rows

__all__ = ["ctl", "unix_sock"]


# runId               state       verdict      phase     appr  iters  startedAt
_FIXTURE = [
    ("aaa-old", "succeeded", "verified", "reflect", 1, 30, "2024-01-01T00:00:00Z"),
    ("bbb-running", "running", None, "worker", 1, 2, "2024-04-01T00:00:00Z"),
    ("mmm-new", "running", None, "worker", 3, 5, "2024-03-01T00:00:00Z"),
    # no startedAt yet: the "missing value" case the hub's cmpValues handles
    ("nnn-nostart", "starting", None, "planning", None, 0, None),
    ("zzz-mid", "failed", "unverified", "verify", 2, 100, "2024-02-01T00:00:00Z"),
]


def _seed(ctl: Ctl) -> None:
    for run_id, state, verdict, phase, approach, iters, started in _FIXTURE:
        rdir = ctl.registry / "runs" / run_id
        rdir.mkdir(parents=True)
        doc = {"runId": run_id, "state": state, "verdict": verdict,
               "phase": phase, "approach": approach, "iterationsUsed": iters,
               "iterationsBudget": 250, "schemaVersion": 1}
        if started is not None:
            doc["startedAt"] = started
        (rdir / "status.json").write_text(json.dumps(doc))


def _human_ids(res) -> list[str]:
    assert res.returncode == 0, res.stderr
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    assert lines[0].split()[0] == "RUN", lines[:1]
    return [ln.split()[0] for ln in lines[1:]]


def _json_ids(res) -> list[str]:
    assert res.returncode == 0, res.stderr
    return [r["runId"] for r in json.loads(res.stdout)]


# --------------------------------------------------------------------------
def test_default_order_is_newest_first(ctl):
    """#9's actual complaint: the list came out run-id alphabetical, so the
    run you just started was wherever the alphabet put it."""
    _seed(ctl)
    ids = _human_ids(ctl.run("runs"))
    # missing startedAt sorts first under a descending key (exactly what the
    # hub's cmpValues * dir does) -- a just-started run has nothing to date.
    assert ids == ["nnn-nostart", "bbb-running", "mmm-new", "zzz-mid", "aaa-old"]
    assert ids != sorted(ids), "alphabetical order would be a false pass"


def test_reverse_flips_the_default(ctl):
    _seed(ctl)
    assert _human_ids(ctl.run("runs", "--reverse")) == [
        "aaa-old", "zzz-mid", "mmm-new", "bbb-running", "nnn-nostart"]


def test_sort_run_id_is_alphabetical(ctl):
    _seed(ctl)
    assert _human_ids(ctl.run("runs", "--sort", "runId")) == [
        "aaa-old", "bbb-running", "mmm-new", "nnn-nostart", "zzz-mid"]
    assert _human_ids(ctl.run("runs", "--sort", "runId", "--reverse")) == [
        "zzz-mid", "nnn-nostart", "mmm-new", "bbb-running", "aaa-old"]


def test_sort_state_and_verdict_use_lifecycle_order(ctl):
    """Not alphabetical: `aborted` is not the first thing that happens to a
    run, and `unverified` is not "after" `verified`."""
    _seed(ctl)
    assert _human_ids(ctl.run("runs", "--sort", "state")) == [
        "nnn-nostart", "bbb-running", "mmm-new", "aaa-old", "zzz-mid"]
    assert _human_ids(ctl.run("runs", "--sort", "verdict")) == [
        "bbb-running", "mmm-new", "nnn-nostart", "zzz-mid", "aaa-old"]


def test_sort_phase(ctl):
    _seed(ctl)
    assert _human_ids(ctl.run("runs", "--sort", "phase")) == [
        "nnn-nostart", "aaa-old", "zzz-mid", "bbb-running", "mmm-new"]


def test_sort_approach_biggest_first(ctl):
    _seed(ctl)
    assert _human_ids(ctl.run("runs", "--sort", "approach")) == [
        "nnn-nostart", "mmm-new", "zzz-mid", "aaa-old", "bbb-running"]


def test_sort_iterations_is_numeric_not_the_rendered_cell(ctl):
    """The cell reads `100/250`; sorting its text would put 100 between 0 and
    2 (string order), which is the trap task 054 called out for the hub."""
    _seed(ctl)
    ids = _human_ids(ctl.run("runs", "--sort", "iterationsUsed"))
    assert ids == ["zzz-mid", "aaa-old", "mmm-new", "bbb-running", "nnn-nostart"]


def test_sort_started_at_matches_the_default(ctl):
    _seed(ctl)
    assert _human_ids(ctl.run("runs", "--sort", "startedAt")) == _human_ids(
        ctl.run("runs"))


def test_json_order_matches_human_order_for_every_key(ctl):
    """A script reading --json and a human reading the table must not
    disagree about which run is first."""
    _seed(ctl)
    for key in sorted(RUN_SORT_KEYS):
        for extra in ([], ["--reverse"]):
            human = _human_ids(ctl.run("runs", "--sort", key, *extra))
            payload = _json_ids(ctl.run("--json", "runs", "--sort", key, *extra))
            assert human == payload, (key, extra, human, payload)


def test_json_keeps_raw_iso_and_numeric_fields(ctl):
    _seed(ctl)
    rows = json.loads(ctl.run("--json", "runs").stdout)
    by_id = {r["runId"]: r for r in rows}
    assert by_id["aaa-old"]["startedAt"] == "2024-01-01T00:00:00Z"
    assert by_id["zzz-mid"]["iterationsUsed"] == 100
    assert by_id["zzz-mid"]["iterations"] == "100/250"


def test_sort_composes_with_state_filter(ctl):
    _seed(ctl)
    assert _human_ids(ctl.run("runs", "--state", "running")) == [
        "bbb-running", "mmm-new"]
    assert _human_ids(ctl.run("runs", "--state", "running", "--reverse")) == [
        "mmm-new", "bbb-running"]
    assert _human_ids(
        ctl.run("runs", "--state", "running", "--sort", "iterationsUsed")) == [
        "mmm-new", "bbb-running"]
    assert _json_ids(ctl.run("--json", "runs", "--state", "succeeded")) == [
        "aaa-old"]


def test_unknown_sort_key_is_a_usage_error(ctl):
    _seed(ctl)
    res = ctl.run("runs", "--sort", "cost")
    assert res.returncode == 2, res.stdout
    assert "--sort" in res.stderr


def test_sort_keys_mirror_the_hub_columns():
    """One dialect, not two: the CLI keys ARE the hub's `RUN_COLUMNS` keys."""
    app_js = (
        __import__("pathlib").Path(__file__).parent.parent
        / "src" / "ralphd" / "cli" / "web" / "app.js").read_text()
    block = app_js.split("const RUN_COLUMNS = [")[1].split("];")[0]
    hub_keys = set(__import__("re").findall(r'key:\s*"([^"]+)"', block))
    assert hub_keys == set(RUN_SORT_KEYS), hub_keys ^ set(RUN_SORT_KEYS)


def test_sort_run_rows_is_stable_on_ties():
    rows = [{"runId": "b", "state": "running"}, {"runId": "a", "state": "running"}]
    assert [r["runId"] for r in sort_run_rows(rows, "state")] == ["a", "b"]
    # tie-break is NOT flipped by --reverse (same rule as the hub)
    assert [r["runId"] for r in sort_run_rows(rows, "state", True)] == ["a", "b"]
