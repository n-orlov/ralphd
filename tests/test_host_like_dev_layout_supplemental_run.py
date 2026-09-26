"""Task cure-01-01-2 (Task 018 criterion 3): the `browser`, `docker`,
dev-layout and `nightly`-marked nodes were covered by NOTHING -- neither the
fast/smoke sweep (task 018) nor `.github/workflows/heavy.yml`'s `docker`/
`browser` jobs nor `nightly.yml`'s dev-layout cells, since every cell calls
`scripts/host_like_dev_layout.sh` with no override at all, and that script's
own default (`fast`) selection never touched them.

This module is deliberately a SIBLING of `tests/test_host_like_dev_layout_
script.py`, not an addition to it: that module's own path is one of
`tests/conftest.py`'s `_NIGHTLY_MODULES`, so anything added there is
nightly-lane no matter how it is marked, and these checks are meant to run
in `fast` (they are cheap: a stub interpreter intercepts every subprocess
this script would otherwise spawn, so no real pytest run, pip install or
network fetch ever happens here -- the same technique
`test_host_like_dev_layout_script.py`'s own PEP-668 tests already use).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "host_like_dev_layout.sh"
HEAVY_WORKFLOW = REPO / ".github" / "workflows" / "heavy.yml"

_PIP_CACHE_ENV = {
    "HOST_LIKE_DEV_LAYOUT_PIP_CACHE_DIR": "/tmp/host-like-dev-layout-pip-cache-supp-test",
}

_SUPPLEMENTAL_MARKER_EXPR = "not fast and not smoke and not docker and not browser"


def _run(args, extra_env=None, timeout=60):
    env = dict(os.environ)
    env.update(_PIP_CACHE_ENV)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


# --- stub interpreter: intercepts every subprocess this script spawns ------
# Every real pytest run, `pip install` and interpreter probe this script
# makes goes through "$PY" -- pointing PYTHON at this stub means NONE of them
# ever really execute, so a whole invocation of this script costs
# milliseconds, not the minutes a real fast-tier (let alone a real
# supplemental) pass would cost.
_STUB_INTERPRETER = r"""#!/usr/bin/env bash
printf 'HOME=%s\tPYTHONUSERBASE=%s\tPATH=%s\tARGS=%s\n' \
  "${HOME:-}" "${PYTHONUSERBASE:-<unset>}" "${PATH:-}" "$*" >> '__LOG__'
case "$*" in
  *ENABLE_USER_SITE*)   exit 0 ;;
  *EXTERNALLY-MANAGED*) printf '%s\n' '__MARKER__'; exit 0 ;;
  *getuserbase*)        printf '%s\n' "${HOME:-}/.local"; exit 0 ;;
  *"import ralphd"*)    printf '%s\n' '__REPO__/src/ralphd/__init__.py'; exit 0 ;;
  *"-m pytest"*"__FAIL_ON__"*) exit 1 ;;
  *) exit 0 ;;
esac
"""


def _stub_interpreter(tmp_path, fail_on="NEVER_MATCHES_ANYTHING_XYZ"):
    """Write the stub interpreter; return (its path, its log path)."""
    log = tmp_path / "stub-interpreter.log"
    marker = tmp_path / "no-such-marker"
    stub = tmp_path / "stub-python3"
    stub.write_text(
        _STUB_INTERPRETER.replace("__LOG__", str(log))
        .replace("__MARKER__", str(marker))
        .replace("__REPO__", str(REPO))
        .replace("__FAIL_ON__", fail_on)
    )
    stub.chmod(0o755)
    return stub, log


def _pytest_invocations(log):
    """Every logged call whose ARGS actually invoked `-m pytest`, as a list
    of field dicts, in call order."""
    out = []
    for line in log.read_text().splitlines():
        fields = dict(field.split("=", 1) for field in line.split("\t"))
        if "-m pytest" in fields.get("ARGS", ""):
            out.append(fields)
    return out


@pytest.mark.parametrize(
    "mode_env",
    [
        pytest.param(None, id="exported-default-mode"),
        pytest.param(
            {"HOST_LIKE_DEV_LAYOUT_PYTHONUSERBASE_MODE": "unset"}, id="unset-mode"
        ),
    ],
)
def test_the_true_default_invocation_runs_a_primary_and_a_supplemental_pass(
    tmp_path, mode_env
):
    """The exact shape `nightly.yml`'s dev-layout cell calls this script
    with: no HOST_LIKE_DEV_LAYOUT_TIER override, no pytest args of its own.
    Both PYTHONUSERBASE_MODE branches must add the second pass, and both
    pytest invocations must share the same $HOME, PYTHONUSERBASE handling
    and console-script PATH -- the supplemental pass reuses the primary's
    already-built environment rather than deriving its own."""
    stub, log = _stub_interpreter(tmp_path)
    extra_env = {"PYTHON": str(stub)}
    if mode_env:
        extra_env.update(mode_env)

    result = _run([], extra_env=extra_env, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "host_like_dev_layout.sh: pytest tier selection: -m fast" in result.stdout
    assert (
        "host_like_dev_layout.sh: default invocation -- also running every"
        in result.stdout
    ), result.stdout
    assert f"-m {_SUPPLEMENTAL_MARKER_EXPR}" in " ".join(result.stdout.split())

    calls = _pytest_invocations(log)
    assert len(calls) == 2, calls
    primary = [c for c in calls if "-q -m fast" in c["ARGS"]]
    supplemental = [c for c in calls if _SUPPLEMENTAL_MARKER_EXPR in c["ARGS"]]
    assert len(primary) == 1, calls
    assert len(supplemental) == 1, calls
    primary, supplemental = primary[0], supplemental[0]

    assert primary["HOME"] == supplemental["HOME"] != ""
    assert primary["PATH"] == supplemental["PATH"] != ""
    assert primary["PATH"].split(os.pathsep)[0] == supplemental["PATH"].split(
        os.pathsep
    )[0]
    if mode_env:
        assert primary["PYTHONUSERBASE"] == "<unset>", primary
        assert supplemental["PYTHONUSERBASE"] == "<unset>", supplemental
    else:
        assert primary["PYTHONUSERBASE"] == supplemental["PYTHONUSERBASE"], (
            primary,
            supplemental,
        )
        assert primary["PYTHONUSERBASE"] not in ("", "<unset>"), primary


@pytest.mark.parametrize(
    "extra_args, extra_env",
    [
        ([], {"HOST_LIKE_DEV_LAYOUT_TIER": "fast"}),
        (["some/targeted/probe.py"], {}),
        (["--collect-only"], {}),
    ],
    ids=["explicit-tier-even-if-fast", "targeted-node", "collect-only-flag"],
)
def test_explicit_tier_targeted_or_collect_only_invocations_skip_the_supplemental_pass(
    tmp_path, extra_args, extra_env
):
    """Criterion (2): naming an explicit tier, or passing any pytest argument
    of the caller's own, retains exactly the requested selection -- one
    pytest invocation, never a second whole-suite-outside-fast pass bolted
    on behind the caller's back."""
    stub, log = _stub_interpreter(tmp_path)
    env = {"PYTHON": str(stub)}
    env.update(extra_env)

    result = _run(extra_args, extra_env=env, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "default invocation -- also running every" not in result.stdout

    calls = _pytest_invocations(log)
    assert len(calls) == 1, calls


def test_a_failing_supplemental_pass_makes_the_script_exit_nonzero(tmp_path):
    """Criterion (1): "returning nonzero if either run fails" -- here the
    primary (`-q -m fast`, no extra args) trivially succeeds under the stub,
    and only the supplemental pass is made to fail."""
    stub, log = _stub_interpreter(tmp_path, fail_on="not fast and not smoke")

    result = _run([], extra_env={"PYTHON": str(stub)}, timeout=60)
    assert result.returncode != 0, result.stdout + result.stderr

    calls = _pytest_invocations(log)
    assert len(calls) == 2, calls
    # The post-run skill-survival report still runs -- STATUS is not
    # swallowed, but it also does not short-circuit the rest of the script.
    assert "SURVIVED the run" in result.stdout, result.stdout


def test_a_failing_primary_pass_still_runs_the_supplemental_pass_and_stays_nonzero(
    tmp_path,
):
    """The other direction: the primary `-m fast` run fails; the
    supplemental pass still runs (both are always attempted), and the
    overall exit stays nonzero even though the supplemental pass itself
    "succeeds" under the stub."""
    stub, log = _stub_interpreter(tmp_path, fail_on="-q -m fast")

    result = _run([], extra_env={"PYTHON": str(stub)}, timeout=60)
    assert result.returncode != 0, result.stdout + result.stderr

    calls = _pytest_invocations(log)
    assert len(calls) == 2, calls


def test_the_supplemental_marker_expression_is_the_scripts_own_constant():
    """Pin the exact string used by both the script's echoed report and its
    actual `-m` invocation to one source: `SUPPLEMENTAL_TIER_MARKER` in the
    script body, matching this module's own copy above."""
    text = SCRIPT.read_text()
    assert (
        f'SUPPLEMENTAL_TIER_MARKER="{_SUPPLEMENTAL_MARKER_EXPR}"' in text
    ), text[: text.index("set -euo pipefail")]
    assert text.count("$SUPPLEMENTAL_TIER_MARKER") >= 2, (
        "the constant must be used by both the echoed report and the actual"
        " -m invocation, not hand-copied twice"
    )
    assert '[ "$RUN_SUPPLEMENTAL_TIER" -eq 1 ]' in text
    assert 'DEV_LAYOUT_TIER_WAS_UNSET' in text


def test_the_header_documents_the_supplemental_default_run():
    """Criterion (3): the script's own documentation must accurately
    describe the supplemental run -- what triggers it, what it selects, and
    what disables it."""
    text = SCRIPT.read_text()
    header = text[: text.index("set -euo pipefail")]
    prose = " ".join(
        line.lstrip().lstrip("#").strip() for line in header.splitlines()
    )
    prose = " ".join(prose.split())
    assert "SECOND pytest" in prose, prose[-3000:]
    assert _SUPPLEMENTAL_MARKER_EXPR in prose, prose[-3000:]
    assert "nightly.yml" in prose
    assert "heavy.yml" in prose
    assert "disables the" in prose and "supplemental pass" in prose, prose[-3000:]


def test_the_supplemental_expression_selects_exactly_what_fast_docker_and_browser_leave_out():
    """Criterion (2)'s collection proof: every collected node belongs to
    exactly one of fast/docker/browser/"the supplemental leftover" (`smoke`
    is a subset of `fast`, so it adds nothing to the partition) -- so the
    unchanged `heavy.yml` (`-m docker`, `-m browser`) plus this script's own
    `-m fast` plus its new supplemental pass leaves nothing uncovered, with
    no double-count either."""

    def _collected_count(marker_expr):
        proc = subprocess.run(
            [
                "bash",
                "-c",
                f'.venv/bin/python -m pytest -q --collect-only -m "{marker_expr}"',
            ],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode in (0, 1), proc.stdout + proc.stderr
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.endswith("tests collected") or "tests collected (" in line:
                return int(line.split("/", 1)[0].split()[-1])
        raise AssertionError(f"no collected-count summary line: {proc.stdout}")

    def _total_count():
        proc = subprocess.run(
            ["bash", "-c", ".venv/bin/python -m pytest -q --collect-only"],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        last = [l for l in proc.stdout.splitlines() if l.strip()][-1]
        return int(last.split()[0])

    total = _total_count()
    fast = _collected_count("fast")
    docker = _collected_count("docker")
    browser = _collected_count("browser")
    leftover = _collected_count(_SUPPLEMENTAL_MARKER_EXPR)

    assert leftover > 0, "nothing left for the supplemental pass to cover"
    assert fast + docker + browser + leftover == total, (
        fast,
        docker,
        browser,
        leftover,
        total,
    )

    heavy_text = HEAVY_WORKFLOW.read_text()
    assert "-m docker" in heavy_text
    assert "-m browser" in heavy_text
