"""The runner scaler's systemd user unit, its sample config and the operator
runbook in `docs/ci.md` (C.2, C.3).

The unit must parse and restart always; the sample config must load and pass the
CPU oversubscription check; a config holding an inline token is an error; and
every command and config key the runbook names must exist in
`ci/runner_scaler.py`, so a runbook naming an invented one fails here.
"""

from __future__ import annotations

import configparser
import importlib.util
import re
import shlex
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCALER = ROOT / "ci" / "runner_scaler.py"
UNIT = ROOT / "ci" / "runner" / "ralphd-ci-scaler.service"
SAMPLE = ROOT / "ci" / "runner" / "scaler.sample.yaml"
DOC = ROOT / "docs" / "ci.md"
RUNBOOK_HEADING = "## Adopting the elastic pool"


@pytest.fixture(scope="module")
def scaler():
    spec = importlib.util.spec_from_file_location("runner_scaler_runbook_under_test", SCALER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- the unit ----------------------------------------------------------------

def _unit() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str  # systemd keys are case-sensitive
    parser.read_string(UNIT.read_text())
    return parser


def test_the_unit_parses_and_restarts_always():
    unit = _unit()
    assert unit["Service"]["Restart"] == "always"
    assert "WantedBy" in unit["Install"]


def test_the_unit_runs_the_run_command_with_the_default_config_path(scaler):
    exec_start = shlex.split(_unit()["Service"]["ExecStart"])
    script = next(i for i, word in enumerate(exec_start) if word.endswith("ci/runner_scaler.py"))
    assert exec_start[-1] == "run"
    assert "run" in scaler.build_parser()._subparsers._group_actions[0].choices
    assert exec_start[script + 1] == "--config"
    assert exec_start[script + 2].endswith(scaler.DEFAULT_CONFIG_PATH.removeprefix("~"))


# ---- the sample config and the inline-token refusal ---------------------------

def test_the_sample_config_loads_and_passes_the_cpu_oversubscription_check(scaler):
    cfg = scaler.load_config(SAMPLE)
    largest = max(float(p["cpus"]) for p in scaler.all_profiles(cfg))
    assert cfg["max_concurrency"] * largest <= cfg["host_cpus"] - scaler.RESERVED_CORES


def test_the_sample_config_names_the_token_by_file_path_and_holds_no_token_value():
    keys = re.findall(r"^([A-Za-z_]+):", SAMPLE.read_text(), re.M)
    assert "token_file" in keys
    assert not [k for k in keys if k == "token" or k.endswith("_token")]


def test_the_sample_config_oversubscribed_is_refused(scaler, tmp_path):
    path = tmp_path / "scaler.yaml"
    path.write_text(SAMPLE.read_text().replace("max_concurrency: 2", "max_concurrency: 4"))
    with pytest.raises(scaler.ConfigError, match="max_concurrency"):
        scaler.load_config(path)


@pytest.mark.parametrize("key", ["token", "github_token"])
def test_a_config_holding_an_inline_token_is_an_error(scaler, tmp_path, key):
    path = tmp_path / "scaler.yaml"
    path.write_text(SAMPLE.read_text() + f"{key}: not-a-real-token-value\n")
    with pytest.raises(scaler.ConfigError, match="inline token"):
        scaler.load_config(path)


# ---- the runbook against the code ---------------------------------------------

def _runbook(text: str) -> str:
    start = text.index(RUNBOOK_HEADING)
    end = text.find("\n## ", start + 1)
    return text[start:end if end != -1 else len(text)]


def _blocks(section: str, lang: str) -> list[str]:
    return [m.group(1) for m in re.finditer(
        rf"^\s*```{lang}\n(.*?)^\s*```", section, re.M | re.S)]


def _dedent(block: str) -> str:
    lines = block.splitlines()
    pad = min(len(l) - len(l.lstrip()) for l in lines if l.strip())
    return "\n".join(l[pad:] for l in lines) + "\n"


def runbook_problems(scaler, section: str) -> list[str]:
    """Every command, flag and config key the runbook names that the scaler lacks."""
    problems: list[str] = []
    parser = scaler.build_parser()
    commands = parser._subparsers._group_actions[0].choices
    top_flags = {s for a in parser._actions for s in a.option_strings}
    for block in _blocks(section, "sh"):
        for line in block.splitlines():
            if "runner_scaler.py" not in line:
                continue
            words = shlex.split(line)
            rest = words[words.index(next(w for w in words if w.endswith("runner_scaler.py"))) + 1:]
            while rest and rest[0].startswith("-"):
                flag = rest.pop(0)
                if flag not in top_flags:
                    problems.append(f"unknown flag {flag}")
                elif flag == "--config" and rest:
                    rest.pop(0)
            if not rest:
                problems.append(f"no command in {line!r}")
                continue
            command, flags = rest[0], rest[1:]
            if command not in commands:
                problems.append(f"unknown command {command}")
                continue
            known = {s for a in commands[command]._actions for s in a.option_strings}
            problems += [f"unknown flag {f} for {command}" for f in flags if f not in known]
    allowed = set(scaler.DEFAULTS) | set(scaler.REQUIRED)
    for block in _blocks(section, "yaml"):
        cfg = scaler.parse_config_text(_dedent(block))
        for key, value in cfg.items():
            if key not in allowed:
                problems.append(f"unknown config key {key}")
            if key == "profiles":
                for label, entry in value.items():
                    problems += [f"unknown profile key {k}" for k in entry
                                 if k not in scaler.PROFILE_KEYS]
    source = SCALER.read_text()
    for word in re.findall(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`", section):
        if word not in allowed and word not in scaler.PROFILE_KEYS:
            problems.append(f"unknown name `{word}`")
        elif f'"{word}"' not in source:
            problems.append(f"`{word}` is not in ci/runner_scaler.py")
    return problems


def test_the_runbook_section_exists_and_orders_the_steps():
    section = _runbook(DOC.read_text())
    order = ["**Build the image.**", "**Write the config.**", "once --dry-run",
             "**Enable the unit.**", "`status`", "**Retire the standing runners one at a time.**"]
    positions = [section.index(marker) for marker in order]
    assert positions == sorted(positions)
    retire = section[section.index("**Retire the standing runners"):]
    assert retire.index("custom labels first") < retire.index("next runner")


def test_the_runbook_lists_the_nic_fault_and_the_per_job_cost():
    section = _runbook(DOC.read_text())
    assert "#303" in section and "NIC" in section and "precondition" in section
    assert "4\nCPUs" in section or "4 CPUs" in section
    assert "12g" in section


def test_every_command_and_config_key_the_runbook_names_exists_in_the_scaler(scaler):
    section = _runbook(DOC.read_text())
    assert runbook_problems(scaler, section) == []
    named = {w for b in _blocks(section, "sh") for w in b.split()}
    assert {"once", "status", "prune-images", "--dry-run"} <= named


def test_the_runbook_config_block_matches_the_sample_config(scaler):
    block = _dedent(_blocks(_runbook(DOC.read_text()), "yaml")[0])
    shown = scaler.parse_config_text(block)
    sample = scaler.parse_config_text(SAMPLE.read_text())
    assert shown == sample


@pytest.mark.parametrize("old,new,expect", [
    ("once --dry-run", "reconcile --dry-run", "unknown command reconcile"),
    ("once --dry-run", "once --simulate", "unknown flag --simulate for once"),
    ("\n   max_concurrency: 2", "\n   max_workers: 2", "unknown config key max_workers"),
    ("memory: 16g", "mem_limit: 16g", "unknown profile key mem_limit"),
    ("`max_concurrency` times", "`worker_cap` times", "unknown name `worker_cap`"),
])
def test_a_runbook_naming_an_invented_command_flag_or_key_fails(scaler, old, new, expect):
    section = _runbook(DOC.read_text())
    assert old in section
    assert expect in runbook_problems(scaler, section.replace(old, new, 1))
