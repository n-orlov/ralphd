"""Black-box tests for the per-run `auto_resume` setting (task 026, issue
#8, PRD req F).

The setting is host-side only (the engine never resumes itself): `start`
persists it with the run's other start-time wiring in the job's config dir,
where a later `ralphctl resume` -- operator-typed or, per task 027, issued
by `doctor --fix` -- still finds it after the container it was started with
is long gone.

Every assertion about the *default* goes through the single constant
`ralphd.cli.main.AUTO_RESUME_DEFAULT` (imported, never spelled out here),
which is why flipping the default to ON in v0.7 (task 027, requirement O)
was a one-line change in main.py: the only test edits it needed were the
two tests that are *about* the default (below) and the fixtures that meant
"an opted-out run" and had been relying on the default to get one -- those
now say `--no-auto-resume`.
"""

from __future__ import annotations

import json

import pytest
import yaml
from test_cli_docker import Ctl, docker_run_argv
from test_cli_resume_llm_wiring import _stop_container

from ralphd.cli.main import AUTO_RESUME_DEFAULT, _read_auto_resume_setting


@pytest.fixture
def ctl(tmp_path):
    return Ctl(tmp_path)


def _wiring_path(ctl: Ctl, run_id: str):
    return ctl.registry / "configs" / run_id / "auto-resume.json"


def _setting(ctl: Ctl, run_id: str):
    path = _wiring_path(ctl, run_id)
    assert path.is_file(), f"no auto-resume wiring for {run_id}"
    return json.loads(path.read_text())["auto_resume"]


def _start(ctl: Ctl, run_id: str, *extra: str, env: dict | None = None):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", run_id, *extra, env=env)
    assert res.returncode == 0, res.stderr
    return res


# ------------------------------------------------------------------ default
def test_default_is_on_and_lives_in_one_place(ctl):
    """A plain `start` records the single-source default, which v0.7
    (requirement O) flipped to ON: a run whose container dies unattended is
    picked back up by `doctor --fix` without anyone having had to opt in.

    Both halves are asserted -- that the recorded setting is the constant
    (so no second literal can creep into `start`'s flag layering) and that
    the constant is now True (so the flip cannot be silently reverted).
    """
    _start(ctl, "tst-ar-default")
    assert _setting(ctl, "tst-ar-default") is AUTO_RESUME_DEFAULT
    assert AUTO_RESUME_DEFAULT is True
    assert _setting(ctl, "tst-ar-default") is True


def test_default_literal_appears_exactly_once_in_the_source(ctl):
    from pathlib import Path

    src = Path(__file__).parent.parent / "src" / "ralphd"
    hits = []
    for path in sorted(src.rglob("*.py")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if line.startswith("AUTO_RESUME_DEFAULT ="):
                hits.append(f"{path}:{n}")
    assert len(hits) == 1, f"auto_resume default defined more than once: {hits}"


def test_roadmap_records_the_shipped_default_flip_and_names_the_single_place():
    """Task 027 (requirement O): the flip has happened, so the roadmap must
    record it as shipped and its deferred list must stop promising it -- a
    deferred entry for work that already landed is how the roadmap rots.
    The shipped note still has to point at the one literal a maintainer
    reverting or re-flipping the default would edit."""
    from pathlib import Path

    text = (Path(__file__).parent.parent / "docs" / "roadmap.md").read_text()
    deferred = text.split("## Later / explicitly deferred", 1)
    assert len(deferred) == 2, "docs/roadmap.md lost its deferred section"
    note = deferred[1]
    assert "auto_resume" not in note, (
        "the deferred list still promises the auto_resume default flip, which "
        "v0.7 shipped")
    # The flip is recorded as shipped where self-recovery is described, and
    # still names the single place the default lives.
    v05 = text.split("## v0.5", 1)[1].split("## Later", 1)[0]
    assert "--auto-resume" in v05
    assert "--no-auto-resume" in v05, "the shipped note must name the opt-out"
    assert "AUTO_RESUME_DEFAULT" in v05
    assert "src/ralphd/cli/main.py" in v05
    assert "v0.7" in v05 and "ON" in v05, (
        "the roadmap must say the default flipped to ON in v0.7")
    # ...and the historical claim stays honest: v0.5 itself shipped opt-in.
    assert "off by default" in v05.lower()


def test_docs_document_the_on_default_and_both_ways_to_opt_out():
    """Task 027 (requirement O): with the default ON, the documentation an
    operator reads has to say so and has to name both opt-out routes -- the
    per-run flag and the registry-wide config key -- or the flip is a change
    of behaviour nobody was told about."""
    from pathlib import Path

    cli = (Path(__file__).parent.parent / "docs" / "cli.md").read_text()
    # the `start` flag table row for this setting
    row = [ln for ln in cli.splitlines()
           if ln.startswith("| `--auto-resume`")]
    assert len(row) == 1, row
    assert "| on |" in row[0], (
        "docs/cli.md's flag table still shows the old default")
    assert "**on** since v0.7" in row[0]
    assert "start it with `--no-auto-resume`" in row[0], (
        "the row must tell an operator how to keep ONE run out of the sweep")
    assert "config set auto_resume false" in row[0]
    # ...and the sweep's own section must not still call the default off
    sweep = cli.split("#### `ralphctl doctor --fix`", 1)[1]
    assert "**default since v0.7**" in sweep
    assert "default **off**" not in sweep


def test_spec_states_the_same_default_as_the_code():
    """SPEC §8.8 spells the default literal out (`AUTO_RESUME_DEFAULT = ...`),
    which is exactly the kind of prose that rots when the one line it quotes
    changes -- so it is read off the code here rather than trusted."""
    import re
    from pathlib import Path

    spec = (Path(__file__).parent.parent / "SPEC.md").read_text()
    quoted = re.findall(r"`AUTO_RESUME_DEFAULT = (\w+)`", spec)
    assert quoted, "SPEC.md no longer states the auto-resume default literal"
    assert set(quoted) == {str(AUTO_RESUME_DEFAULT)}, quoted


# ------------------------------------------------------------------- --flag
def test_start_flag_opts_the_run_in(ctl):
    _start(ctl, "tst-ar-on", "--auto-resume")
    assert _setting(ctl, "tst-ar-on") is True


def test_no_auto_resume_flag_opts_out(ctl):
    _start(ctl, "tst-ar-off", "--no-auto-resume")
    assert _setting(ctl, "tst-ar-off") is False


def test_flags_appear_in_start_help(ctl):
    res = ctl.run("start", "--help")
    assert res.returncode == 0, res.stderr
    assert "--auto-resume" in res.stdout
    assert "--no-auto-resume" in res.stdout


def test_setting_is_not_passed_into_the_container(ctl):
    """Host-side only: nothing about it belongs in the container's env."""
    _start(ctl, "tst-ar-noenv", "--auto-resume")
    argv = docker_run_argv(ctl)
    assert not any("AUTO_RESUME" in a.upper() for a in argv)


# ------------------------------------------------------- registry / template
def test_registry_default_applies_when_the_flag_is_omitted(ctl):
    res = ctl.run("config", "set", "auto_resume", "true")
    assert res.returncode == 0, res.stderr
    # stored as a real bool, not the string "true"
    assert yaml.safe_load((ctl.registry / "config.yaml").read_text())[
        "auto_resume"] is True

    _start(ctl, "tst-ar-reg")
    assert _setting(ctl, "tst-ar-reg") is True


def test_explicit_opt_out_beats_the_registry_default(ctl):
    ctl.run("config", "set", "auto_resume", "true")
    _start(ctl, "tst-ar-regoff", "--no-auto-resume")
    assert _setting(ctl, "tst-ar-regoff") is False


def test_registry_config_set_false_is_honoured_as_false(ctl):
    """`bool("false")` is True in Python -- the string spelling must not
    silently opt every run in."""
    ctl.run("config", "set", "auto_resume", "false")
    _start(ctl, "tst-ar-regfalse")
    assert _setting(ctl, "tst-ar-regfalse") is False


def test_config_set_rejects_a_non_boolean_value(ctl):
    res = ctl.run("config", "set", "auto_resume", "maybe")
    assert res.returncode == 2, res.stdout
    assert "true" in (res.stdout + res.stderr)


def test_template_value_applies_and_the_flag_still_wins(ctl):
    tdir = ctl.registry / "templates" / "arjob"
    tdir.mkdir(parents=True)
    (tdir / "job.yaml").write_text("auto_resume: true\n")
    (tdir / "prd.md").write_text("# T\n")

    _start(ctl, "tst-ar-tpl", "--template", "arjob")
    assert _setting(ctl, "tst-ar-tpl") is True

    _start(ctl, "tst-ar-tpl2", "--template", "arjob", "--no-auto-resume")
    assert _setting(ctl, "tst-ar-tpl2") is False


# ------------------------------------------------------------------- resume
def test_setting_survives_resume(ctl):
    """The wiring the run was started with is the wiring a fresh container
    gets: `resume` replaces the container but never the config dir, so the
    opt-in is still recorded (and the reproduced `docker run` argv is the
    original run's wiring, same run dir + config dir mounts)."""
    _start(ctl, "tst-ar-resume", "--auto-resume")
    start_argv = docker_run_argv(ctl)
    _stop_container(ctl)

    res = ctl.run("resume", "tst-ar-resume", env={
        "STUB_DOCKER_CONTAINERS": "ralphd-tst-ar-resume",
        "STUB_DOCKER_RUNNING": "",
    })
    assert res.returncode == 0, res.stderr
    resume_argv = docker_run_argv(ctl)

    assert _setting(ctl, "tst-ar-resume") is True
    mounts = [resume_argv[i + 1] for i, a in enumerate(resume_argv) if a == "-v"]
    assert f"{ctl.registry / 'runs' / 'tst-ar-resume'}:/run/ralphd" in mounts
    assert f"{ctl.registry / 'configs' / 'tst-ar-resume'}:/config:ro" in mounts
    assert "ralphd.run=tst-ar-resume" in resume_argv
    assert "ralphd.run=tst-ar-resume" in start_argv


def test_reader_falls_back_to_the_default_for_a_pre_task_026_run(ctl, monkeypatch):
    """A run started before this setting existed has no wiring file at all;
    the reader `doctor --fix` (task 027) uses must treat it as the default,
    not crash and not guess."""
    monkeypatch.setenv("RALPHD_REGISTRY", str(ctl.registry))
    (ctl.registry / "configs" / "tst-ar-legacy").mkdir(parents=True)
    assert _read_auto_resume_setting("tst-ar-legacy") is AUTO_RESUME_DEFAULT
    # ... and reads back a real run's persisted opt-in
    _start(ctl, "tst-ar-read", "--auto-resume")
    assert _read_auto_resume_setting("tst-ar-read") is True


def test_opted_out_run_stays_opted_out_across_resume(ctl):
    """`resume` replaces the container, never the config dir: an explicit
    opt-out is still an opt-out afterwards (under the ON default this is the
    interesting direction -- a resume must not silently re-enrol the run)."""
    _start(ctl, "tst-ar-resume-off", "--no-auto-resume")
    _stop_container(ctl)
    res = ctl.run("resume", "tst-ar-resume-off", env={
        "STUB_DOCKER_CONTAINERS": "ralphd-tst-ar-resume-off",
        "STUB_DOCKER_RUNNING": "",
    })
    assert res.returncode == 0, res.stderr
    assert _setting(ctl, "tst-ar-resume-off") is False
