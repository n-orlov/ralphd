"""Task 043d (#22): SPEC.md's state, API, hub and deferred sections *are* the code.

Task 043c derived SPEC's section 10 tables from `cli.main.build_parser`. This is
the same treatment for everything else this wave changed, section by section:

  * 3.5's module map lists every module the package actually ships (the old one
    knew nothing of `cli/image.py` or `engine/pricing_aws.py`);
  * 5.1's run-dir tree holds every file `RunDir` puts in a run dir -- which now
    includes the last-good tasks cache;
  * 5.2's `status.json` table documents every field the engine writes into
    `status.json`, `model`/`modelRaw` included;
  * 5.3's read contract is the reader's own: the four `tasksSource` values come
    from four real reads of a real run dir, and the cache file name, the re-read
    budget and the `{tasksStale,tasksSource}` field names come from the module;
  * 6.2/6.7/7.1's key lists are `ralphctl`'s own (`_CONFIG_KEYS`,
    `_TEMPLATE_SCALAR_FIELDS`, `resolve_profile`);
  * 8.6's strategy values and rate-table facts come from `config.PRICE_STRATEGIES`
    and `engine/pricing_aws.py`, and every `cost*` marker the runner writes has to
    be documented somewhere in the spec;
  * 9.2's endpoint table is the FastAPI app's own route set, both directions;
  * 11's hub sections name every `/api/runs/<id>/<sub>` the hub serves;
  * 15 defers nothing the code now does.

Every check is paired with a test showing it fails on the wording it replaced, so
none of them is a tautology over prose somebody may later reflow.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC = REPO_ROOT / "SPEC.md"
SRC = REPO_ROOT / "src" / "ralphd"
ENGINE_DIR = SRC / "engine"
CLI_DIR = SRC / "cli"

COUNT_WORDS = {5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine",
               10: "ten"}


def _spec_section(heading: str) -> str:
    """One SPEC section, heading line included; `####` blocks stay inside it."""
    lines = SPEC.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("#") and not lines[i].startswith("####"):
            end = i
            break
    return "\n".join(lines[start:end])


def _table_rows(section: str) -> list[list[str]]:
    rows = []
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or set(cells[0]) <= set("-: "):
            continue
        rows.append(cells)
    return rows


def _without(section: str, needle: str) -> str:
    """The section with every line mentioning `needle` removed -- the shape of
    the wording that predates a correction. Case-insensitive, so dropping
    "steering" drops a "Steering form" heading too."""
    low = needle.lower()
    return "\n".join(line for line in section.splitlines()
                     if low not in line.lower())


# --------------------------------------------------------------------------
# 3.5: the module map is the package's own module list
# --------------------------------------------------------------------------

def _shipped_modules() -> set[str]:
    """Every shipped source file, as the map spells them (`engine/state.py`)."""
    names = set()
    for path in sorted(SRC.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix not in (".py", ".js", ".css", ".html", ".md"):
            continue
        names.add(str(path.relative_to(SRC)))
    return names


def _module_map_problems(section: str, modules: set[str]) -> list[str]:
    documented = set()
    for cells in _table_rows(section):
        for path in re.findall(r"`src/ralphd/([A-Za-z0-9_/{},.-]+)`", cells[0]):
            # `{cli,engine}/__init__.py` is one row for two real files
            brace = re.match(r"\{([A-Za-z,]+)\}/(.+)", path)
            if brace:
                documented |= {f"{part}/{brace.group(2)}"
                               for part in brace.group(1).split(",")}
            else:
                documented.add(path)
    problems = [f"module map has no row for {m}" for m in sorted(modules - documented)]
    problems += [f"module map names {m}, which the package does not ship"
                 for m in sorted(documented - modules)]
    return problems


def test_the_module_map_is_the_packages_own_module_list():
    modules = _shipped_modules()
    assert "engine/state.py" in modules and "cli/web/app.js" in modules
    problems = _module_map_problems(_spec_section("### 3.5"), modules)
    assert not problems, f"SPEC.md 3.5 disagrees with src/ralphd: {problems}"


def test_the_module_map_check_would_catch_the_missing_v06_modules():
    modules = _shipped_modules()
    section = _spec_section("### 3.5")
    for module in ("cli/image.py", "engine/pricing_aws.py"):
        assert module in modules
        assert _module_map_problems(_without(section, module), modules), module
    invented = section + "\n| `src/ralphd/engine/scheduler.py` | queues jobs |"
    assert _module_map_problems(invented, modules)


# --------------------------------------------------------------------------
# 5.1: the run-dir tree is the run dir
# --------------------------------------------------------------------------

def _tree_names(section: str) -> set[str]:
    block = re.search(r"```\n~/\.ralphd/runs/<run-id>/(.*?)```", section, re.DOTALL)
    assert block, "SPEC.md 5.1 no longer draws the run-dir tree"
    names = set()
    for line in block.group(1).splitlines():
        entry = re.match(r"^[\u2502\u251c\u2514\u2500\s]+([^\s#]+)", line)
        if not entry:
            continue
        name = entry.group(1).split("#")[0]
        if name and not name.endswith("/"):
            names.add(name)
    return names


def _run_dir_files(root: Path) -> set[str]:
    from ralphd.engine.state import (
        HOST_FILE,
        OPERATOR_TERMINATION_FILE,
        TASKS_LAST_GOOD_NAME,
        RunDir,
    )
    run = RunDir(root=root)
    names = set()
    for attr in dir(type(run)):
        if attr.startswith("_") or not isinstance(
                getattr(type(run), attr, None), property):
            continue
        value = getattr(run, attr)
        if isinstance(value, Path) and value.parent == run.root and value.suffix:
            names.add(value.name)
    return names | {HOST_FILE, OPERATOR_TERMINATION_FILE, TASKS_LAST_GOOD_NAME,
                    "events.jsonl"}


def test_the_spec_run_dir_tree_lists_every_file_the_run_dir_holds(tmp_path):
    documented = _tree_names(_spec_section("### 5.1"))
    missing = sorted(_run_dir_files(tmp_path) - documented)
    assert not missing, (
        f"SPEC.md 5.1's run-dir tree omits files the engine writes: {missing}")


def test_the_run_dir_tree_check_would_catch_the_missing_tasks_cache(tmp_path):
    from ralphd.engine.state import TASKS_LAST_GOOD_NAME
    old = _without(_spec_section("### 5.1"), TASKS_LAST_GOOD_NAME)
    assert TASKS_LAST_GOOD_NAME in _run_dir_files(tmp_path) - _tree_names(old)


# --------------------------------------------------------------------------
# 5.2: every status.json field the engine writes
# --------------------------------------------------------------------------

def _status_fields_written() -> set[str]:
    """Field names the engine puts into `status.json`.

    Two spellings, because the engine uses both: keyword arguments to
    `update_status()`, and a `patch` dict built up and splatted into it (which is
    how `model`/`modelRaw` are written -- an ast walk over keywords alone misses
    exactly this wave's fields).
    """
    fields: set[str] = set()
    for path in sorted(ENGINE_DIR.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "update_status"):
                fields |= {kw.arg for kw in node.keywords if kw.arg}
                for arg in node.args:
                    if isinstance(arg, ast.Dict):
                        fields |= {k.value for k in arg.keys
                                   if isinstance(k, ast.Constant)}
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (isinstance(target, ast.Subscript)
                            and isinstance(target.value, ast.Name)
                            and target.value.id.endswith("patch")
                            and isinstance(target.slice, ast.Constant)
                            and isinstance(target.slice.value, str)):
                        fields.add(target.slice.value)
    return fields


def _undocumented_status_fields(section: str, fields: set[str]) -> list[str]:
    return sorted(f for f in fields
                  if f"`{f}`" not in section and f'"{f}"' not in section)


def test_every_status_field_the_engine_writes_is_in_the_spec_table():
    fields = _status_fields_written()
    assert len(fields) >= 25, sorted(fields)
    problems = _undocumented_status_fields(_spec_section("### 5.2"), fields)
    assert not problems, (
        f"SPEC.md 5.2 omits status.json fields the engine writes: {problems}")


def test_the_status_field_check_would_catch_the_missing_model_fields():
    fields = _status_fields_written()
    section = _spec_section("### 5.2")
    for field in ("model", "modelRaw", "maxApproaches"):
        assert field in fields, sorted(fields)
        # both spellings, since the section carries a worked JSON example too
        assert _undocumented_status_fields(
            _without(_without(section, f"`{field}`"), f'"{field}"'), fields)


# --------------------------------------------------------------------------
# 5.3: the hardened tasks read, described by the reader itself
# --------------------------------------------------------------------------

@pytest.fixture
def four_reads(tmp_path):
    """`{tasksSource: TasksRead}` for all four cases, from real run dirs."""
    from ralphd.engine.state import read_tasks_doc
    reads = {}
    absent = tmp_path / "absent"
    absent.mkdir()
    reads["absent"] = read_tasks_doc(absent, attempts=1, delay=0)

    good = tmp_path / "good"
    good.mkdir()
    plan = {"version": 1, "tasks": [{"id": "001", "status": "completed"}]}
    (good / "tasks.json").write_text(json.dumps(plan))
    reads["file"] = read_tasks_doc(good, attempts=1, delay=0)
    (good / "tasks.json").write_text('{"version": 1, "tas')
    reads["last-good"] = read_tasks_doc(good, attempts=1, delay=0)

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "tasks.json").write_text("{ nope")
    reads["unreadable"] = read_tasks_doc(broken, attempts=1, delay=0)
    return reads


def _read_contract_problems(section: str, reads: dict) -> list[str]:
    from ralphd.engine.state import TASKS_LAST_GOOD_NAME, TASKS_READ_ATTEMPTS
    problems = []
    for source, read in reads.items():
        if f"`{source}`" not in section:
            problems.append(f"5.3 does not name the tasksSource `{source}`")
        row = [cells for cells in _table_rows(section)
               if cells[0] == f"`{source}`"]
        if not row:
            problems.append(f"5.3 has no row for `{source}`")
            continue
        claimed = row[0][-1].strip("` ")
        if claimed != str(read.stale).lower():
            problems.append(
                f"5.3 says `{source}` is stale={claimed}, the reader says "
                f"{str(read.stale).lower()}")
    for name in ("tasksStale", "tasksSource"):
        if f"`{name}`" not in section:
            problems.append(f"5.3 does not name the contract field `{name}`")
    if TASKS_LAST_GOOD_NAME not in section:
        problems.append(f"5.3 does not name the cache file {TASKS_LAST_GOOD_NAME}")
    if "`TASKS_READ_ATTEMPTS`" not in section:
        problems.append("5.3 does not point at the re-read budget constant")
    assert TASKS_READ_ATTEMPTS >= 2  # a bounded re-read is the whole mechanism
    return problems


def test_the_tasks_read_section_is_the_readers_own_contract(four_reads):
    assert set(four_reads) == {"absent", "file", "last-good", "unreadable"}
    assert [r.source for r in four_reads.values()] == list(four_reads)
    problems = _read_contract_problems(_spec_section("### 5.3"), four_reads)
    assert not problems, f"SPEC.md 5.3 misstates the tasks read: {problems}"


def test_the_tasks_read_check_would_catch_the_old_silence(four_reads):
    """Before this wave 5.3 described the file and not the read at all."""
    section = _spec_section("### 5.3")
    old = section.split("**Reading it")[0]
    problems = _read_contract_problems(old, four_reads)
    assert len(problems) >= 6, problems
    # and it is not vacuous: a single wrong staleness claim is caught too
    flipped = section.replace("| `last-good` | it would not parse; this is the "
                              "last payload that did | `true` |",
                              "| `last-good` | it would not parse; this is the "
                              "last payload that did | `false` |")
    assert _read_contract_problems(flipped, four_reads)


def test_the_last_good_cache_is_never_written_on_the_happy_path(tmp_path):
    """5.3's load-bearing claim, checked rather than trusted."""
    from ralphd.engine.state import TASKS_LAST_GOOD_NAME, read_tasks_doc
    (tmp_path / "tasks.json").write_text('{"version": 1, "tasks": []}')
    read_tasks_doc(tmp_path, attempts=1, delay=0)
    assert not (tmp_path / TASKS_LAST_GOOD_NAME).exists()
    (tmp_path / "tasks.json").write_text("{ half")
    read_tasks_doc(tmp_path, attempts=1, delay=0)
    assert (tmp_path / TASKS_LAST_GOOD_NAME).exists()


# --------------------------------------------------------------------------
# 6.2 / 6.7 / 7.1: the key lists are the CLI's own
# --------------------------------------------------------------------------

def _registry_keys() -> set[str]:
    from ralphd.cli.main import _CONFIG_KEYS
    return set(_CONFIG_KEYS)


def _registry_layer_problems(text: str, keys: set[str]) -> list[str]:
    problems = [f"the registry-layer key list omits `{k}`"
                for k in sorted(keys) if f"`{k}`" not in text]
    want = COUNT_WORDS[len(keys)]
    for word in re.findall(r"\b([a-z]+)\b", text):
        if word in COUNT_WORDS.values() and word != want:
            problems.append(f"spelled count {word!r}, there are {len(keys)}")
    return problems


@pytest.mark.parametrize("heading", ["### 6.2", "### 10.2"])
def test_the_registry_layer_lists_config_sets_own_keys(heading):
    keys = _registry_keys()
    section = _spec_section(heading)
    para = [p for p in section.split("\n\n")
            if "registry" in p and ("config.yaml" in p or "config set" in p)]
    assert para, f"{heading} no longer describes the registry layer"
    problems = []
    for block in para:
        if all(f"`{k}`" in block for k in ("on_complete", "network")):
            problems += _registry_layer_problems(block, keys)
    assert not problems, f"SPEC.md {heading}: {problems}"


def test_the_registry_layer_check_would_catch_the_six_key_claim():
    keys = _registry_keys()
    old = ("only for the six keys that have one: `image`, `on_complete`, "
           "`network`, `auto_resume`, `default_llm_profile` and "
           "`price_strategy`")
    problems = _registry_layer_problems(old, keys)
    assert "the registry-layer key list omits `base_image`" in problems
    assert "the registry-layer key list omits `dockerfile`" in problems
    assert any("spelled count 'six'" in p for p in problems), problems


def test_the_template_scalar_list_is_the_clis_own_field_set():
    from ralphd.cli.main import _TEMPLATE_SCALAR_FIELDS, IMAGE_SUPPLY_KEYS
    keys = set(_TEMPLATE_SCALAR_FIELDS) | set(IMAGE_SUPPLY_KEYS)
    section = _spec_section("### 6.7")
    missing = sorted(k for k in keys if f"`{k}`" not in section)
    assert not missing, (
        f"SPEC.md 6.7's template `job.yaml` key list omits: {missing}")
    # the check is substantive: it would have caught this wave's additions
    for key in ("price_strategy", "base_image", "dockerfile"):
        assert key in keys
        assert f"`{key}`" not in _without(section, f"`{key}`")


def test_the_profile_key_table_is_resolve_profiles_own_keys(tmp_path):
    from ralphd.cli.llm_profiles import resolve_profile
    reg = tmp_path / "reg"
    (reg / "llm-profiles").mkdir(parents=True)
    (reg / "llm-profiles" / "p.yaml").write_text(
        "description: d\nmodel: m\nprice_strategy: aws\n")
    resolved = set(resolve_profile("p", reg, host_env={}))
    section = _spec_section("### 7.1")
    documented = {cells[0].strip("`") for cells in _table_rows(section)
                  if re.fullmatch(r"`[a-z_]+`", cells[0])}
    assert documented == resolved, (
        f"SPEC.md 7.1's key table is {sorted(documented)}, a resolved profile "
        f"is {sorted(resolved)}")
    assert "price_strategy" in resolved  # this wave's field


# --------------------------------------------------------------------------
# 8.6: the cost markers, the strategy values and the shipped table
# --------------------------------------------------------------------------

def _cost_markers() -> set[str]:
    """Every `cost*` key the runner writes into an iteration's usage."""
    src = (ENGINE_DIR / "runner.py").read_text()
    return set(re.findall(r'usage\["(cost[A-Za-z]+)"\]', src))


def test_every_cost_marker_the_runner_writes_is_documented():
    markers = _cost_markers()
    assert {"costZeroQuoted", "costFree"} <= markers, sorted(markers)
    spec = SPEC.read_text()
    missing = sorted(m for m in markers if f"`{m}`" not in spec)
    assert not missing, f"SPEC.md documents no cost marker {missing}"


def _price_strategy_problems(section: str) -> list[str]:
    from ralphd.engine import pricing_aws
    from ralphd.engine.config import DEFAULT_PRICE_STRATEGY, PRICE_STRATEGIES
    from ralphd.engine.pricing import NO_TABLE, OPERATOR_TABLE
    problems = []
    if "`price_strategy`" not in section:
        problems.append("8.6 does not name the price_strategy knob")
    for value in PRICE_STRATEGIES:
        if f'"{value}"' not in section and f"`{value}`" not in section:
            problems.append(f"8.6 does not name the strategy {value!r}")
    if f"`{DEFAULT_PRICE_STRATEGY}`" not in section:
        problems.append("8.6 does not name the default strategy")
    for name in ("TABLE_NAME", "STALE_AFTER_DAYS", "AS_OF",
                 "staleness()", "pricing_map()", "PricingChain",
                 "resolve_pricing()", "is_zero_quote()"):
        if name not in section:
            problems.append(f"8.6 does not point at `{name}`")
    if pricing_aws.AS_OF in section:
        problems.append("8.6 repeats the as-of date instead of naming AS_OF")
    if OPERATOR_TABLE not in section or NO_TABLE not in section:
        problems.append("8.6 does not word the price-table layers as the code does")
    return problems


def test_the_cost_section_describes_the_pricing_code_it_documents():
    problems = _price_strategy_problems(_spec_section("### 8.6"))
    assert not problems, f"SPEC.md 8.6: {problems}"


def test_the_cost_section_check_would_catch_the_pre_v06_wording():
    section = _spec_section("### 8.6")
    old = section
    for gone in ("price_strategy", "pricing_aws", "AS_OF", "PricingChain",
                 "resolve_pricing", "zero"):
        old = _without(old, gone)
    problems = _price_strategy_problems(old)
    assert len(problems) >= 5, problems
    # and a date pasted in instead of a pointer at the constant is caught
    from ralphd.engine import pricing_aws
    assert _price_strategy_problems(section + f"\nas of {pricing_aws.AS_OF}.")


def test_the_implausible_zero_rule_is_the_codes_own():
    """8.6's zero-quote rows, checked against `is_zero_quote` itself."""
    from ralphd.engine.state import cost_status, is_zero_quote
    quoted_zero = {"input": 32, "output": 18320, "cacheRead": 438945,
                   "cacheWrite": 48331, "totalTokens": 505628,
                   "costUSD": 0, "costPriced": True}
    assert is_zero_quote(quoted_zero)
    assert cost_status(quoted_zero) == "unknown"
    assert not is_zero_quote({"totalTokens": 0, "costUSD": 0})
    assert not is_zero_quote({**quoted_zero, "costFree": True})
    section = _spec_section("### 8.6")
    assert "declared" in section and "costFree" in section


# --------------------------------------------------------------------------
# 9.2: the endpoint table is the app's own route set
# --------------------------------------------------------------------------

def _api_routes() -> set[tuple[str, str]]:
    src = (ENGINE_DIR / "api.py").read_text()
    return {(m.group(1).upper(), re.sub(r"\{[^}]*\}", "{}", m.group(2)))
            for m in re.finditer(r'@app\.(get|post|put|patch|delete)\("([^"]+)"',
                                 src)}


def _documented_routes(section: str) -> set[tuple[str, str]]:
    routes = set()
    for cells in _table_rows(section):
        method = re.fullmatch(r"`([A-Z]+)`", cells[0])
        if not method or len(cells) < 2:
            continue
        for path in re.findall(r"`([^`]+)`", cells[1]):
            routes.add((method.group(1), re.sub(r"\{[^}]*\}", "{}", path)))
    return routes


def test_the_endpoint_table_is_the_apis_own_route_set():
    real, documented = _api_routes(), _documented_routes(_spec_section("### 9.2"))
    assert len(real) >= 30, sorted(real)
    assert not sorted(real - documented), (
        f"SPEC.md 9.2 omits routes the engine serves: {sorted(real - documented)}")
    assert not sorted(documented - real), (
        f"SPEC.md 9.2 documents routes that do not exist: "
        f"{sorted(documented - real)}")


def test_the_endpoint_table_check_is_substantive():
    real = _api_routes()
    section = _spec_section("### 9.2")
    assert _api_routes() - _documented_routes(_without(section, "/steering"))
    invented = section + "\n| `GET` | `/plan` | the plan |"
    assert _documented_routes(invented) - real


def test_the_status_route_guarantees_name_this_waves_defaults():
    """9.3's "absence is never a third case" list must cover the new fields."""
    section = _spec_section("### 9.3")
    for field in ("maxApproaches", "model", "modelRaw", "tasksStale"):
        assert f"`{field}`" in section, field


# --------------------------------------------------------------------------
# 11: the hub sections name every view the hub serves
# --------------------------------------------------------------------------

def _hub_sub_resources() -> set[str]:
    src = (CLI_DIR / "ui_server.py").read_text()
    return set(re.findall(r'segs\[3\] == "([a-z]+)"', src))


def _hub_section() -> str:
    return "\n".join(_spec_section(h) for h in
                     ("### 11.1", "### 11.2", "### 11.3", "### 11.4", "### 11.5"))


def _hub_problems(text: str, subs: set[str]) -> list[str]:
    lowered = text.lower()
    return [f"section 11 never mentions the {sub} view" for sub in sorted(subs)
            if sub not in lowered]


def test_section_11_mentions_every_view_the_hub_serves():
    subs = _hub_sub_resources()
    assert {"documents", "artifacts", "fault", "cost", "iterations",
            "steering"} <= subs, sorted(subs)
    problems = _hub_problems(_hub_section(), subs)
    assert not problems, f"SPEC.md section 11: {problems}"


@pytest.mark.parametrize("sub", ["documents", "artifacts", "fault", "cost",
                                 "steering"])
def test_the_hub_view_check_would_catch_an_undocumented_view(sub):
    assert _hub_problems(_without(_hub_section(), sub), {sub})


def test_the_run_detail_payload_shape_is_the_servers_own(tmp_path):
    """11.3 states the run payload's keys; `run_detail` decides them."""
    from ralphd.cli.ui_server import run_detail
    run = tmp_path / "runs" / "shape"
    run.mkdir(parents=True)
    (run / "status.json").write_text(json.dumps(
        {"runId": "shape", "state": "succeeded"}))
    payload = set(run_detail(tmp_path, "shape"))
    section = _spec_section("### 11.3")
    documented = set(re.findall(r"[a-zA-Z]+",
                                re.search(r"`\{(runId[^}]*)\}`", section).group(1)))
    assert documented == payload, (
        f"SPEC.md 11.3 states the payload as {sorted(documented)}, run_detail "
        f"returns {sorted(payload)}")
    assert "deletable" in payload  # task 031 (#19)


def test_the_hub_delete_route_exists_and_is_described():
    from ralphd.cli import ui_server
    assert hasattr(ui_server, "delete_run") and hasattr(ui_server, "deletion_refusal")
    section = _spec_section("### 11.3")
    assert "`DELETE /api/runs/<id>`" in section
    assert "typed back" in section or "type the run id" in section.lower()


# --------------------------------------------------------------------------
# 15: the deferred list defers nothing the code now does
# --------------------------------------------------------------------------

def _deferred_bullets() -> dict[str, str]:
    """{bolded title: whole bullet text} for section 15."""
    section = _spec_section("## 15. Deferred")
    bullets: dict[str, str] = {}
    current = None
    for line in section.splitlines():
        start = re.match(r"- \*\*(.+?)\*\*", line)
        if start:
            current = start.group(1)
            bullets[current] = line
        elif current and line.startswith("  "):
            bullets[current] += "\n" + line
        elif not line.strip():
            current = None
    return bullets


def test_the_deferred_list_is_parseable_and_substantive():
    bullets = _deferred_bullets()
    assert len(bullets) >= 6, sorted(bullets)


def test_the_deferred_auto_resume_default_matches_the_literal():
    """The one flip the entry promises is a single literal; when it flips, this
    entry has to go, and this test is what says so."""
    from ralphd.cli.main import AUTO_RESUME_DEFAULT
    entry = [t for t in _deferred_bullets() if "auto_resume" in t]
    if AUTO_RESUME_DEFAULT:
        assert not entry, ("`auto_resume` now defaults ON, so SPEC.md 15 must "
                           "stop deferring it")
    else:
        assert entry, "SPEC.md 15 no longer explains the opt-in default"


OLD_IMAGE_ENTRY = (
    "- **A published Docker image and `pipx` packaging.** The image builds and "
    "runs\n  locally — the `-m docker` tier proves it — and the wheel builds, but "
    "there is\n  no publishing pipeline to push either to a registry or to PyPI, "
    "so `--image`\n  points at a locally built tag and installation is "
    "`pip install -e .`.")


def _delivered_capabilities() -> list[tuple[bool, str]]:
    """(the code fact, the phrase a deferred *title* may therefore not claim).

    A title is the claim a reader skims; the body may still mention `pipx` as an
    install shape, which is why only titles are checked here.
    """
    from ralphd.cli import image, ui_server
    from ralphd.engine import pricing_aws
    from ralphd.engine.config import PRICE_STRATEGIES
    return [
        (bool(image.PACKAGED_FILES), "pipx packaging"),
        (bool(image.IMAGE_INPUTS), "content-hashed image"),
        ("aws" in PRICE_STRATEGIES and bool(pricing_aws.RATES), "rate table"),
        (hasattr(ui_server, "delete_run"), "one-command delete"),
        (hasattr(ui_server, "iteration_view"), "run self-explanation"),
    ]


def _deferred_problems(titles: list[str]) -> list[str]:
    problems = []
    for holds, phrase in _delivered_capabilities():
        assert holds, phrase  # the probe itself must be true of the code
        for title in titles:
            plain = title.replace("`", "").lower()
            if phrase in plain:
                problems.append(f"deferred entry {title!r} defers {phrase!r}, "
                                f"which v0.6 ships")
    return problems


def test_nothing_deferred_is_something_the_code_now_does():
    problems = _deferred_problems(list(_deferred_bullets()))
    assert not problems, f"SPEC.md 15: {problems}"


def test_the_deferred_check_would_catch_the_old_image_entry():
    title = re.match(r"- \*\*(.+?)\*\*", OLD_IMAGE_ENTRY).group(1)
    problems = _deferred_problems([title])
    assert problems and "pipx packaging" in problems[0], problems


def test_the_image_entry_defers_publishing_and_not_the_build():
    from ralphd.cli import image
    entry = [b for t, b in _deferred_bullets().items() if "image" in t.lower()]
    assert len(entry) == 1, [t for t in _deferred_bullets()]
    body = entry[0]
    assert "publish" in body.lower(), body
    assert bool(image.PACKAGED_FILES)
    # the old wording paired the unbuilt publishing pipeline with `pipx`
    # packaging, which this wave decided and implemented (task 038, #20 H4).
    assert "packaging" not in body.split("What is deferred")[0]
    assert "packaging" in OLD_IMAGE_ENTRY.split("What is deferred")[0]
