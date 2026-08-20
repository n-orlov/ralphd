"""Packaging metadata honesty (task 040, #22).

`pyproject.toml` used to carry `version = "0.1.0.dev0"` while `docs/roadmap.md`
recorded v0.1-v0.5 as shipped, and a `cli` extra declaring `httpx` + `rich`
that **nothing under `src/ralphd/cli/` ever imported**. Both are the same kind
of defect: metadata nobody re-reads, drifting away from the code beside it.
So this module re-reads it:

* the version is a deliberate release version (no `.dev`/`rc`/`a`/`b` tail),
  spelled identically in `pyproject.toml` and `src/ralphd/__init__.py`, and
  reported by `ralphctl --version`, `ralphd-engine --version` and the
  `GET /version` handler's own source of truth;
* no doc claims a different version (the `GET /version` example in
  `docs/api.md` is a real claim, not decoration), and the roadmap cannot
  record a milestone as shipped that the version string is behind;
* every declared requirement -- runtime dependency or extra -- is used by
  something in this repo, at a location this module names. A requirement of a
  non-`dev` extra has to be *imported by `src/`*: that is exactly what the
  dead `cli` extra was not, and a test that only counted extras would have
  been satisfied by re-adding it with `httpx` alone (tests import `httpx`).

Deliberately re-reads the files rather than `importlib.metadata`: an editable
install's dist-info records the version that was current when `pip install -e`
last ran, which is precisely the stale number this module exists to catch.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
INIT = REPO_ROOT / "src" / "ralphd" / "__init__.py"
ROADMAP = REPO_ROOT / "docs" / "roadmap.md"

# Docs that describe the CURRENT release. docs/prds/ is deliberately excluded:
# a PRD is a historical snapshot of what was asked for, and must not be
# rewritten every time the version moves.
CURRENT_DOCS = [REPO_ROOT / "README.md", REPO_ROOT / "SPEC.md", *sorted(
    (REPO_ROOT / "docs").glob("*.md"))]

# A release version, not a placeholder: three numbers and nothing after them.
RELEASE_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# Version claims a reader would take literally. Each pattern's group 1 must be
# the declared version.
VERSION_CLAIM_RES = [
    re.compile(r'"ralphd":\s*"([^"]+)"'),                  # GET /version bodies
    re.compile(r"\bralphd(?:-engine|ctl)?[ =]v?(\d+\.\d+\.\d+[^\s`\"',)]*)"),
    re.compile(r'^\s*version = "([^"]+)"', re.MULTILINE),  # pyproject snippets
]


def declared_version() -> str:
    return tomllib.loads(PYPROJECT.read_text())["project"]["version"]


def _requirements() -> dict[str, list[str]]:
    """`{extra name or "dependencies": [distribution name, ...]}`."""
    project = tomllib.loads(PYPROJECT.read_text())["project"]
    out = {"dependencies": list(project.get("dependencies", []))}
    for extra, reqs in (project.get("optional-dependencies") or {}).items():
        out[extra] = list(reqs)
    return {k: [re.split(r"[<>=!~\[;\s]", r)[0].lower() for r in v] for k, v in out.items()}


# ------------------------------------------------------------------ the version


def test_version_is_a_deliberate_release_version():
    v = declared_version()
    assert RELEASE_VERSION_RE.match(v), (
        f"pyproject version {v!r} is a placeholder/pre-release string; #22 asks "
        "for a deliberate first-release version"
    )


def test_pyproject_and_package_literal_agree_on_the_version():
    m = re.search(r'^__version__ = "([^"]+)"', INIT.read_text(), re.MULTILINE)
    assert m, f"{INIT} has no __version__ literal"
    assert m.group(1) == declared_version(), (
        "src/ralphd/__init__.py and pyproject.toml disagree about the version: "
        f"{m.group(1)!r} vs {declared_version()!r}"
    )


def test_the_api_version_handler_reports_the_package_literal():
    """`GET /version`'s body is built from `ralphd.__version__` -- the same
    literal the two tests above pin -- so it cannot report a third number."""
    src = (REPO_ROOT / "src" / "ralphd" / "engine" / "api.py").read_text()
    assert '"ralphd": __version__' in src


def test_both_console_entrypoints_report_the_declared_version():
    """Run as modules, not as installed scripts: this must hold for the code in
    the checkout even when the editable install's metadata is a version behind."""
    v = declared_version()
    for module, prefix in (("ralphd.cli.main", ""), ("ralphd.engine.main", "ralphd-engine ")):
        out = subprocess.run(
            [sys.executable, "-m", module, "--version"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0, out.stderr
        printed = (out.stdout + out.stderr).strip()
        assert printed == f"{prefix}{v}", f"{module} --version printed {printed!r}, want {v!r}"


# --------------------------------------------------------------- the doc claims


def test_no_current_doc_claims_a_different_version():
    v = declared_version()
    wrong: list[str] = []
    seen = 0
    for doc in CURRENT_DOCS:
        text = doc.read_text()
        for pattern in VERSION_CLAIM_RES:
            for m in pattern.finditer(text):
                seen += 1
                if m.group(1) != v:
                    line = text[: m.start()].count("\n") + 1
                    wrong.append(f"{doc.relative_to(REPO_ROOT)}:{line}: {m.group(0)!r}")
    assert not wrong, (
        f"docs claim a version other than {v!r} (update the doc or the version):\n"
        + "\n".join(wrong)
    )
    # Non-vacuity: at least docs/api.md's `GET /version` example body is a
    # claim, so a broken pattern above cannot quietly check nothing.
    assert seen >= 1, "matched no version claim at all -- VERSION_CLAIM_RES has rotted"


def _roadmap_milestones() -> list[tuple[tuple[int, int], bool]]:
    """`[((major, minor), records_something_shipped), ...]` per `## vX.Y` block."""
    text = ROADMAP.read_text()
    heads = list(re.finditer(r"^## v(\d+)\.(\d+)\b", text, re.MULTILINE))
    out = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[m.end(): end]
        out.append(((int(m.group(1)), int(m.group(2))), "✅" in body))
    return out


def test_the_roadmap_records_shipped_milestones_at_all():
    """Guards the check below from passing because the parse found nothing."""
    shipped = [s for s, ok in _roadmap_milestones() if ok]
    assert len(shipped) >= 5, f"parsed {shipped!r} as shipped from docs/roadmap.md"


def test_the_version_is_not_behind_or_ahead_of_the_roadmap():
    v = declared_version()
    series = tuple(int(p) for p in v.split(".")[:2])
    shipped = [s for s, ok in _roadmap_milestones() if ok]
    highest = max(shipped)
    assert series >= highest, (
        f"version {v} is behind docs/roadmap.md, which records v{highest[0]}.{highest[1]} "
        "as shipped (#22: the version string contradicted the roadmap)"
    )
    assert series <= (highest[0], highest[1] + 1), (
        f"version {v} is more than one minor ahead of the highest milestone "
        f"docs/roadmap.md records as shipped (v{highest[0]}.{highest[1]})"
    )


# ------------------------------------------------------------------- the extras


class _Import:
    """Evidence: some Python file under one of `roots` imports `module`."""

    def __init__(self, module: str, roots: tuple[str, ...]):
        self.module, self.roots = module, roots

    def __str__(self) -> str:
        return f"imported by {'/'.join(self.roots)} (`import {self.module}`)"

    def found(self) -> bool:
        pattern = re.compile(
            rf"^\s*(?:import {re.escape(self.module)}\b|from {re.escape(self.module)}[. ])",
            re.MULTILINE,
        )
        for root in self.roots:
            for path in (REPO_ROOT / root).rglob("*.py"):
                if pattern.search(path.read_text()):
                    return True
        return False


class _Literal:
    """Evidence: `needle` appears verbatim in `path` (a tool, not a library)."""

    def __init__(self, path: str, needle: str):
        self.path, self.needle = path, needle

    def __str__(self) -> str:
        return f"used by {self.path} ({self.needle!r})"

    def found(self) -> bool:
        return self.needle in (REPO_ROOT / self.path).read_text()


# Where each declared requirement is actually used. Adding a dependency means
# adding its evidence here; a dependency that stops being used fails instead of
# living on in the metadata forever.
DEPENDENCY_EVIDENCE = {
    "fastapi": _Import("fastapi", ("src",)),
    "uvicorn": _Import("uvicorn", ("src",)),
    "pyyaml": _Import("yaml", ("src",)),
    "pytest": _Import("pytest", ("tests",)),
    "pytest-asyncio": _Literal("pyproject.toml", "asyncio_mode"),
    "httpx": _Import("httpx", ("tests",)),
    "ruff": _Literal("pyproject.toml", "[tool.ruff]"),
    "build": _Literal("tests/test_image_packaged_inputs.py", '"-m", "build"'),
    "hatchling": _Literal("pyproject.toml", 'build-backend = "hatchling.build"'),
}


def test_every_declared_requirement_has_recorded_evidence():
    declared = {dist for reqs in _requirements().values() for dist in reqs}
    assert declared, "parsed no requirements out of pyproject.toml"
    missing = sorted(declared - set(DEPENDENCY_EVIDENCE))
    assert not missing, (
        f"pyproject declares {missing} with no recorded use: add it to "
        "DEPENDENCY_EVIDENCE naming where it is used, or drop it"
    )
    stale = sorted(set(DEPENDENCY_EVIDENCE) - declared)
    assert not stale, f"DEPENDENCY_EVIDENCE names {stale}, which pyproject no longer declares"


def test_no_declared_requirement_is_unused():
    unused = [
        f"{dist}: not {evidence}"
        for dist, evidence in DEPENDENCY_EVIDENCE.items()
        if not evidence.found()
    ]
    assert not unused, "declared but unused requirements (#22, the dead `cli` extra):\n" + (
        "\n".join(unused)
    )


def test_a_runtime_extra_must_be_imported_by_src():
    """The rule the dead `cli` extra broke: an extra other than `dev` exists so
    an *installed* ralphd can do something more, so `src/` has to import it.
    Vacuously true while `dev` is the only extra -- it is the guard for the
    next one."""
    for extra, dists in _requirements().items():
        if extra in ("dependencies", "dev"):
            continue
        for dist in dists:
            evidence = DEPENDENCY_EVIDENCE.get(dist)
            assert isinstance(evidence, _Import) and "src" in evidence.roots, (
                f"extra {extra!r} declares {dist!r}, which nothing under src/ imports: "
                "an extra that only tests use belongs in `dev`"
            )


def test_the_dropped_cli_extra_stays_dropped():
    extras = set(_requirements()) - {"dependencies"}
    assert extras == {"dev"}, (
        f"unexpected extras {sorted(extras)}: #22 dropped `cli` (nothing under "
        "src/ralphd/cli/ imports httpx or rich)"
    )
