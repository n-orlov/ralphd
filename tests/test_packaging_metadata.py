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

import hashlib
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

# An "engine account" claim names the git sha/build a wave actually ran
# under -- a different observable from the package's own declared release
# version, and one the roadmap is required to state verbatim for a wave
# whose engine trails the repo (operator steer 001's `ralphd 0.19.0` from
# `main` `1a3b9bb` sentence). Recognised only in this exact, narrow shape:
# a backtick-quoted `ralphd X.Y.Z` immediately followed by `` from `main`
# `<sha>` `` -- so an ordinary version claim anywhere else is still caught
# in full; this is not a general carve-out for "ralphd <version>" text.
ENGINE_ACCOUNT_RE = re.compile(
    r"`ralphd \d+\.\d+\.\d+` from `main` `[0-9a-f]{7}[0-9a-f]*`"
)


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
        engine_account_spans = [m.span() for m in ENGINE_ACCOUNT_RE.finditer(text)]
        for pattern in VERSION_CLAIM_RES:
            for m in pattern.finditer(text):
                if any(start <= m.start() and m.end() <= end
                       for start, end in engine_account_spans):
                    continue  # the wave's own engine-account sentence, not a release claim
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


def test_the_engine_account_sentence_is_exempt_at_head():
    """The one sentence this exemption exists for -- docs/roadmap.md's steer-001
    engine account -- must actually be recognised, or the guard above would be
    red for the wrong reason (a doc claiming a stale version) and this passing
    for a different one (nothing left to check)."""
    text = ROADMAP.read_text()
    assert ENGINE_ACCOUNT_RE.search(text), (
        "docs/roadmap.md no longer carries the engine-account sentence "
        "ENGINE_ACCOUNT_RE targets -- the exemption may now be exempting nothing, "
        "or (if the sentence moved/changed shape) may be silently over- or "
        "under-matching"
    )


def test_mutation_an_ordinary_stale_version_claim_is_still_caught():
    """The exemption is narrow: an ordinary `ralphd X.Y.Z` claim that is NOT
    followed by the engine-account's own `` from `main` `<sha>` `` shape must
    still be flagged wrong -- proves the carve-out does not swallow a genuine
    stale-doc-version drift."""
    v = declared_version()
    stale = "0.0.1" if v != "0.0.1" else "0.0.2"
    text = f"This doc says ralphd {stale} is current.\n"
    engine_account_spans = [m.span() for m in ENGINE_ACCOUNT_RE.finditer(text)]
    wrong = []
    for pattern in VERSION_CLAIM_RES:
        for m in pattern.finditer(text):
            if any(start <= m.start() and m.end() <= end for start, end in engine_account_spans):
                continue
            if m.group(1) != v:
                wrong.append(m.group(0))
    assert wrong, "an ordinary stale ralphd-version claim must not be exempted"


def test_mutation_the_engine_account_shape_is_exempted_even_when_stale():
    """The flip side: the exact engine-account shape, even naming a version
    that differs from the declared one (as it always will, by construction --
    that is the whole point of the sentence), is exempt."""
    v = declared_version()
    other = "9.9.9" if v != "9.9.9" else "8.8.8"
    text = f"The engine running this wave is `ralphd {other}` from `main` `1a3b9bb`.\n"
    engine_account_spans = [m.span() for m in ENGINE_ACCOUNT_RE.finditer(text)]
    assert engine_account_spans, "ENGINE_ACCOUNT_RE didn't match its own target shape"
    wrong = []
    for pattern in VERSION_CLAIM_RES:
        for m in pattern.finditer(text):
            if any(start <= m.start() and m.end() <= end for start, end in engine_account_spans):
                continue
            if m.group(1) != v:
                wrong.append(m.group(0))
    assert not wrong, f"engine-account shape should have been exempted, got {wrong}"


# A milestone block records what shipped in one of two forms, and this module
# has to read both. The older blocks tick each delivered line with ✅ (and ⏳
# for a deferred one). From v0.18 on, the operator's settled terminal account
# states the shipped sha and version in one prose line instead and ticks
# nothing: `docs/roadmap.md`'s `## v0.18` block (settled at `00003a3`) carries
# zero ✅ characters while ending "Shipped sha `21652d4`, version 0.18.0,
# installed host-side by the operator". A ✅-only parse therefore reads the
# newest shipped milestone as unshipped, which both weakens the "not behind"
# assertion below (its floor drops a minor) and makes the release after it look
# "more than one minor ahead" of a roadmap that in fact records it as shipped.
# The declaration only counts for the block it sits in: its version series must
# match the heading, so a shipped line quoted from another wave proves nothing.
SHIPPED_DECLARATION_RE = re.compile(
    r"^Shipped sha\s+`?\w{7,40}`?,\s+version\s+`?(\d+)\.(\d+)\.\d+`?",
    re.MULTILINE,
)


def _records_shipped(series: tuple[int, int], body: str) -> bool:
    if "✅" in body:
        return True
    return any(
        (int(m.group(1)), int(m.group(2))) == series
        for m in SHIPPED_DECLARATION_RE.finditer(body)
    )


def _roadmap_blocks() -> list[tuple[tuple[int, int], str]]:
    """`[((major, minor), block body), ...]` per `## vX.Y` heading."""
    text = ROADMAP.read_text()
    heads = list(re.finditer(r"^## v(\d+)\.(\d+)\b", text, re.MULTILINE))
    out = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out.append(((int(m.group(1)), int(m.group(2))), text[m.end(): end]))
    return out


def _roadmap_milestones() -> list[tuple[tuple[int, int], bool]]:
    """`[((major, minor), records_something_shipped), ...]` per `## vX.Y` block."""
    return [(series, _records_shipped(series, body)) for series, body in _roadmap_blocks()]


# Every `## ` block of `docs/roadmap.md` that precedes `## v0.23`, in file order,
# with the sha256 of its body (everything after the heading line up to the next
# `## ` heading of any kind) as it stood at the pre-v0.23 baseline `7f8575d4`.
# The `## v0.22` entry is the hash of that baseline body too: the ONE permitted
# edit inside a historical block -- its shipped-sha declaration line, amendment
# 001-WAVE clauses 1 and 2 -- is stripped back out before hashing, so the
# comparison is against the pre-wave bytes for that block as well. Regenerate
# only from git, never from the working tree:
#   git show 7f8575d4:docs/roadmap.md
_PRE_V023_BASELINE_BLOCK_SHA256 = {
    "## v0.1 \u2014 the working loop (MVP)":
        "fad779bc64934d3db59d836928206550d773fbc9336e3acc06951e67559b9b92",
    "## v0.2 \u2014 durability & recovery":
        "73f67cfb77b07c992eccceb410e09cd5af40d5af4fa62ca846c7de91c32d334c",
    "## v0.3 \u2014 hub UI":
        "2746a3621cdcddbdb3f32f749f5ba06ffa12a2e910fac72f9ecb39c603caf0d5",
    "## v0.4 \u2014 quality-of-life":
        "1c63baf40bc2725bbeba43ce926f4213dc92495197760d43112c3e0d1c14be2e",
    "## v0.5 \u2014 session resilience & self-recovery":
        "9b4c15334abec91d22eb349b735c9d4226234065c51307eead24f8968a2cb35f",
    "## v0.6 \u2014 first-release polish":
        "87856cc643e45d3fd56a1c31345288d66848340ce3d02ec4812f805a4279d656",
    "## v0.7 \u2014 trustworthy unattended operation":
        "d3af970e53c37196754a211fb1cc4fa0147724f6c8b73e35602ebcccba7cfd37",
    "## v0.8 \u2014 a run a human can interrupt, audit and believe":
        "a716921b1544d3cf1b047d104e4c3673249c74a263879c049d6cb3ad8d37e835",
    "## After v0.8 \u2014 the harness stops assuming its own environment (#64, #65, #67)":
        "2f1522accb50cbb00d5cd9e0924e77f8906e8e530c9fe1653586dccef693891d",
    "## v0.9 \u2014 the loop's rules become data, and only the harness writes state":
        "346e8eddaeef4da1fde6f0a9d49a89fcb27dd7b444cf66eb3cf9c0c6ea5fa870",
    "## v0.10 \u2014 the clock is not a verdict":
        "0f78d0c59c863f77076ca6767e2db2e7a334126b66cd5f2b5fbcfd966f38bcf8",
    "## v0.12 \u2014 green on a real host":
        "5342d7816914c03020a0fe6d1e9cb95c33413254da68f4f4495b345489671735",
    "## v0.13 \u2014 a broken contract is a conversation":
        "28e463d9dff3ec4ccda83636420f38bfe7489583ac1d85ff627ca169cd81d44a",
    "## v0.14 \u2014 every hand on the plan is named":
        "5014dac68f1e3b1a836c2ce035372b7ebf66fec51fc0461826c69f686fb7d6d1",
    "## v0.14.1 \u2014 a guard that cannot see, and a rule that cannot finish":
        "d54512b3829baee6acd27d8aee6dc20633050532422e021096b799bec0591304",
    "## v0.14.2 \u2014 green where it runs":
        "90795750d0d9b6785be921ee416bef98589458cdd97f11037e8f2360a07e285b",
    "## v0.15 \u2014 impossible is an answer":
        "91a7785d49acbed7f0b49f02700978298e86dbf903625477e756bea48cf271e3",
    "## v0.16 \u2014 a petition is not a postmortem":
        "024e1b01267d04d5e33eb6e8e862aa064782da18cd4da2a127d9b87a977c5d35",
    "## v0.17 \u2014 a slip is not a verdict":
        "1e4b752518ef692058e97164e3d57a6ffefb5edc2231b6bfac0ce483403df66e",
    "## v0.18 \u2014 the run can finish":
        "f677fed1c68b6d385945ee4aa8600426c32052cbb4353be68c49cb3b852524cf",
    "## v0.19 \u2014 a note is not a rejection":
        "e9cf57fc83b1b5100185760b8093401405274e39399d148fc338f021b219f71b",
    "## v0.19.1 \u2014 a gate is not a wall":
        "9ac3405b47ea50b6420eecd33d63bf7dcf6e80c755eb04e6a5889ff0eb8eb3bf",
    "## v0.20 \u2014 a commit is progress":
        "126c67584ad00a62556c8ec3ea929ae508bb4ddb760ecce0f2fa66e61f2fdde1",
    "## v0.20.1 \u2014 inspection is not evidence":
        "baa234c114dd1b245c252ca582e46476962df560faca012cde1f392c3d1f9009",
    "## v0.20.2 \u2014 a requirement cannot be orphaned":
        "470ae9db217f9ede6d960629f1dcc872a66ee9f7193573b475780080a046318c",
    "## v0.21 \u2014 the budget belongs to the work":
        "c1432082d74bec4a07deb7c93ee5640c71f6e52f038ac60e28b34cdc106d06e9",
    "## v0.21.1 \u2014 a green suite is not a met requirement":
        "f67bd456ca15a184428310deb1cdf8e08f607a8b8d2156bf2da1aee9b9c3f47c",
    "## v0.22 \u2014 the harness owns its own state":
        "d07612aca89231b4c18d132fb705559e29b8a7f38e3014b41c819f6c01795655",
}

# The whole declaration LINE, including its trailing newline, so removing it
# restores the pre-wave text exactly.
_SHIPPED_DECLARATION_LINE_RE = re.compile(
    r"^Shipped sha\s+`?\w{7,40}`?,\s+version\s+`?(\d+)\.(\d+)\.\d+`?[^\n]*\n",
    re.MULTILINE,
)


def test_a_settled_block_stating_its_shipped_sha_counts_as_shipped():
    """The v0.18 form: a settled terminal account that ticks nothing.

    Non-vacuous by construction -- it asserts such a block exists in
    `docs/roadmap.md` -- and mutation-checked: strike the shipped-sha line out
    of the body and the same block stops counting, so this is a second reading
    of the roadmap's own record, not a blanket "assume shipped".
    """
    declared = [
        (series, body) for series, body in _roadmap_blocks()
        if "✅" not in body and SHIPPED_DECLARATION_RE.search(body)
    ]
    assert declared, (
        "no docs/roadmap.md block records a shipped sha without ✅ ticks; if the "
        "convention changed again, re-read SHIPPED_DECLARATION_RE against it"
    )
    for series, body in declared:
        assert _records_shipped(series, body), f"v{series[0]}.{series[1]} read as unshipped"
        struck = SHIPPED_DECLARATION_RE.sub("(line removed)", body)
        assert not _records_shipped(series, struck), (
            f"v{series[0]}.{series[1]} still reads as shipped with its shipped-sha "
            "line removed -- the parse is not reading that line"
        )
        assert not _records_shipped((series[0], series[1] + 1), body), (
            f"v{series[0]}.{series[1]}'s shipped-sha line counts for a milestone it "
            "does not name"
        )

    # Regression, folded in here rather than as a new collected item (task
    # cure-01-09, I.2 / amendment 001-WAVE): EVERY `## ` block preceding
    # `## v0.23` is pinned to its pre-v0.23 baseline (`7f8575d4`) bytes, and
    # the `## v0.22` block's shipped declaration is the ONE permitted edit
    # inside any of them -- exactly one such line, in that one block.
    # Blocks are extracted independently of `_roadmap_blocks()` above (which
    # matches only the `## vX.Y` heading token, so it would fold the
    # heading's own title text into the body and collide v0.19 with
    # v0.19.1) -- bounded here by the WHOLE heading line, both at the top
    # and at the next `## ` heading of any kind, so the boundary is
    # identical whether or not a later commit inserts a new section right
    # after one of them (v0.23 did). Any other edit anywhere in the
    # historical record -- a reword in v0.21, a restatement in v0.18, a
    # second declaration line in v0.22, a renamed, dropped, reordered or
    # newly inserted pre-v0.23 heading -- trips one of these assertions
    # instead of silently passing.
    full_text = ROADMAP.read_text()
    all_heads = list(re.finditer(r"^## .*$", full_text, re.MULTILINE))
    v023_index = next(
        i for i, m in enumerate(all_heads) if m.group(0).startswith("## v0.23 ")
    )
    pre_headings = [all_heads[i].group(0) for i in range(v023_index)]
    assert pre_headings == list(_PRE_V023_BASELINE_BLOCK_SHA256), (
        "the sequence of `## ` headings preceding ## v0.23 in docs/roadmap.md no longer "
        "matches the pre-v0.23 baseline (7f8575d4) -- a historical block was added, "
        "removed, renamed or reordered"
        f"\n\ngot:\n{pre_headings!r}"
    )
    for i, heading in enumerate(pre_headings):
        body_start = all_heads[i].end()
        body_end = all_heads[i + 1].start()
        section = full_text[body_start:body_end]
        if heading.startswith("## v0.22 "):
            declarations = _SHIPPED_DECLARATION_LINE_RE.findall(section)
            assert len(declarations) == 1, (
                f"the ## v0.22 section carries {len(declarations)} shipped-sha declaration "
                "lines; amendment 001-WAVE permits exactly ONE added line in that section "
                "and nothing else"
            )
            assert "\u2705" not in section, (
                "the ## v0.22 section must not carry a \u2705 tick (amendment 001-WAVE: the "
                "form is the prose declaration line, and 39c783ca's tick was reverted)"
            )
            section = _SHIPPED_DECLARATION_LINE_RE.sub("", section)
        # Every other section is hashed as it stands, declaration lines and all
        # (v0.18's own settled declaration is part of its baseline bytes), so a
        # declaration line added to one of them trips the pin below too.
        assert hashlib.sha256(section.encode()).hexdigest() == (
            _PRE_V023_BASELINE_BLOCK_SHA256[heading]
        ), (
            f"{heading} no longer matches the pre-v0.23 baseline (7f8575d4) byte-for-byte "
            "-- requirement I.2 forbids editing a historical roadmap section, and amendment "
            "001-WAVE's single permitted exception is the ## v0.22 shipped-sha line"
            f"\n\ngot:\n{section!r}"
        )


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
