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
  `docs/api.md` is a real claim, not decoration), and `docs/roadmap.md` has a
  Shipped line for the declared version;
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


def test_the_declared_version_is_at_least_0_29_0():
    """PRD v0.29 F.3 bumps the release version to 0.29.0 (#279-#283). Parsed as
    a tuple so a future bump (0.30.0, 1.0.0, ...) keeps passing instead of
    pinning the suite to this one release forever."""
    v = tuple(int(part) for part in declared_version().split("."))
    assert v >= (0, 29, 0), (
        f"declared version {declared_version()!r} is below 0.29.0 -- PRD v0.29 "
        "F.3 asks for the release bump that ships #279-#283"
    )


def test_the_declared_version_is_at_least_0_32_3():
    """The release that ships #299 and #300 is 0.32.3. A tuple comparison, so a
    later bump keeps passing; the sites that must move with it are held equal
    by the neighbouring pyproject/package-literal and doc-claim tests."""
    v = tuple(int(part) for part in declared_version().split("."))
    assert v >= (0, 32, 3), (
        f"declared version {declared_version()!r} is below 0.32.3 -- the release "
        "that ships #299 and #300 is 0.32.3"
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


def test_mutation_an_ordinary_stale_version_claim_is_still_caught():
    """A doc naming a release other than the declared one is flagged."""
    v = declared_version()
    stale = "0.0.1" if v != "0.0.1" else "0.0.2"
    text = f"This doc says ralphd {stale} is current.\n"
    wrong = [
        m.group(0)
        for pattern in VERSION_CLAIM_RES
        for m in pattern.finditer(text)
        if m.group(1) != v
    ]
    assert wrong, "an ordinary stale ralphd-version claim must be flagged"


# The roadmap's Shipped section carries one line per version, shaped
# "- vX.Y — title — sentence" (a `.0` patch release is written `vX.Y`).
SHIPPED_HEADING = "## Shipped"
SHIPPED_LINE_RE = re.compile(r"^- v(\d+\.\d+(?:\.\d+)?) \u2014 ", re.MULTILINE)


def _shipped_versions(text: str) -> list[str]:
    """The version labels of every Shipped line, normalised to X.Y.Z."""
    parts = text.split(SHIPPED_HEADING, 1)
    assert len(parts) == 2, f"docs/roadmap.md has no {SHIPPED_HEADING!r} section"
    section = parts[1].split("\n## ", 1)[0]
    return [
        label if label.count(".") == 2 else f"{label}.0"
        for label in SHIPPED_LINE_RE.findall(section)
    ]


def test_the_roadmap_has_a_shipped_line_for_the_declared_version():
    v = declared_version()
    shipped = _shipped_versions(ROADMAP.read_text())
    assert v in shipped, (
        f"docs/roadmap.md's Shipped section has no line for version {v} "
        f"(found {shipped!r})"
    )


def test_mutation_a_roadmap_without_the_declared_version_is_caught():
    """The parse reads the Shipped section, not the whole file: a version named
    only under Planned does not count, and a `.0` release matches `vX.Y`."""
    text = (
        "## Planned\n\n- v9.9 \u2014 later \u2014 not yet.\n\n"
        f"{SHIPPED_HEADING}\n\n- v1.2.3 \u2014 a \u2014 b.\n- v1.2 \u2014 c \u2014 d.\n"
    )
    assert _shipped_versions(text) == ["1.2.3", "1.2.0"]


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
    "pytest-xdist": _Literal("scripts/host_like_pytest.sh", "-n 8 --dist loadfile"),
    # #203: the per-test bound. `timeout_method` rather than `timeout` is the
    # literal checked for, because it is the half that is easy to lose in a
    # merge and useless to lose -- a `signal`-method timeout never fires while
    # the main thread is blocked in `ep_poll`, which is the exact signature of
    # every hang measured on 14 Sep 2026.
    "pytest-timeout": _Literal("pyproject.toml", "timeout_method"),
    "httpx": _Import("httpx", ("tests",)),
    "ruff": _Literal("pyproject.toml", "[tool.ruff]"),
    "build": _Literal("tests/test_image_packaged_inputs.py", '"-m", "build"'),
    "hatchling": _Literal("pyproject.toml", 'build-backend = "hatchling.build"'),
    "claude-agent-sdk": _Import("claude_agent_sdk", ("src", "tests")),
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


def test_dev_sdk_pin_equals_image_pin():
    """The SDK version is pinned in `pyproject.toml`'s dev extra, and it must
    equal the pin in `container/Dockerfile` (requirement J.1).
    """
    from ralphd.cli import image

    # Get the SDK requirement from the raw pyproject.toml (before version stripping)
    project = tomllib.loads(PYPROJECT.read_text())["project"]
    dev_reqs = project.get("optional-dependencies", {}).get("dev", [])
    dev_sdk_reqs = [r for r in dev_reqs if "claude-agent-sdk" in r]
    assert dev_sdk_reqs, "claude-agent-sdk not found in dev requirements"

    dev_sdk_req = dev_sdk_reqs[0]
    # Extract version from "claude-agent-sdk==0.2.163"
    match = re.match(r"claude-agent-sdk==(.+)$", dev_sdk_req)
    assert match, f"dev requirement {dev_sdk_req!r} does not use == pinning"
    dev_version = match.group(1)

    # Get the image pin from container/Dockerfile
    dockerfile_text = (REPO_ROOT / "container/Dockerfile").read_text()
    args = image.arg_defaults(dockerfile_text)
    image_version = args.get("CLAUDE_AGENT_SDK_VERSION")
    assert image_version, "container/Dockerfile has no CLAUDE_AGENT_SDK_VERSION ARG"

    # They must match
    assert dev_version == image_version, (
        f"dev SDK version {dev_version!r} does not match image pin "
        f"{image_version!r} in container/Dockerfile"
    )

    # The installed SDK's version must match too
    import claude_agent_sdk
    assert claude_agent_sdk.__version__ == dev_version, (
        f"installed claude-agent-sdk version {claude_agent_sdk.__version__!r} does not match "
        f"the pinned requirement {dev_version!r}"
    )
