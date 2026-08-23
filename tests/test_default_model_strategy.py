"""Task 044 (#53): `balanced` is the shipped default model strategy.

Every run that never chose a strategy used to get `quality-first` -- the strong
model in all five phases -- so ralphd's out-of-the-box behaviour was its most
expensive preset, picked by nobody. The flip itself is one word; what needs
tests is the *shape* of it, because two things can rot:

* **one place.** `"quality-first"` used to be written twice as a default (the
  `JobConfig.model_strategy` field and `ralphctl start`'s
  `_TEMPLATE_SCALAR_FIELDS`), which is exactly how a half-flipped default
  happens: the engine and the host disagree about what an unconfigured run
  runs. There is now one constant, `engine/config.DEFAULT_MODEL_STRATEGY`,
  imported host-side; these tests read the AST of both sites so a later edit
  that re-types the literal fails here rather than at a confused operator.

* **two decisions, not one.** `model_for`'s fallback for an *unrecognised*
  strategy stays `quality-first` on purpose: what ships may be the cheap
  compromise, but a typo must never quietly downgrade a phase to the fast
  tier. Collapsing them into one name would make the next default flip
  silently retarget the fallback too, so the tests below pin them apart and
  assert the two presets are observably different (otherwise the pinning would
  be vacuous).

The doc half is derived from the constant, never from a second copy of the
word: the default column of `docs/cli.md`'s flag row, SPEC's `job.yaml` key
table and `start` flag table, and the `(default)` marker in all three strategy
preset tables. Each check is paired with a mutation case asserting it still
fails on the pre-flip text.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from ralphd.engine.config import (DEFAULT_MODEL_STRATEGY, MODEL_STRATEGIES,
                                  JobConfig)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PY = REPO_ROOT / "src" / "ralphd" / "engine" / "config.py"
MAIN_PY = REPO_ROOT / "src" / "ralphd" / "cli" / "main.py"
SPEC = REPO_ROOT / "SPEC.md"
CLI_DOC = REPO_ROOT / "docs" / "cli.md"
ARCH_DOC = REPO_ROOT / "docs" / "architecture.md"
API_DOC = REPO_ROOT / "docs" / "api.md"
ROADMAP = REPO_ROOT / "docs" / "roadmap.md"

# The one name `model_for` falls back to for an unrecognised strategy. Stated
# here as the *expectation* of this module (the code is read below), not as a
# convenience alias for DEFAULT_MODEL_STRATEGY.
UNKNOWN_FALLBACK = "quality-first"
PHASES = ("planning", "worker", "review", "verify", "reflect")


# ------------------------------------------------------- the constant itself


def test_the_shipped_default_is_balanced_and_is_a_real_preset():
    assert DEFAULT_MODEL_STRATEGY == "balanced"
    assert DEFAULT_MODEL_STRATEGY in MODEL_STRATEGIES
    assert DEFAULT_MODEL_STRATEGY in JobConfig.STRATEGY_TIERS
    # `config set model_strategy <default>` must be accepted, or the shipped
    # value would be one an operator cannot restate (task 043's validation).
    assert set(MODEL_STRATEGIES) == set(JobConfig.STRATEGY_TIERS)


def test_a_default_jobconfig_carries_the_constant():
    assert JobConfig().model_strategy == DEFAULT_MODEL_STRATEGY


def test_a_job_yaml_that_names_no_strategy_loads_the_constant(tmp_path):
    """The engine's own read path, not just the dataclass default."""
    (tmp_path / "job.yaml").write_text("run_id: r\niterations: 3\n")
    assert JobConfig.load(tmp_path / "job.yaml").model_strategy == \
        DEFAULT_MODEL_STRATEGY


# ------------------------------------------------- written in exactly one place


def _field_default(path: Path, class_name: str, field: str) -> ast.expr:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for stmt in node.body:
                if (isinstance(stmt, ast.AnnAssign)
                        and isinstance(stmt.target, ast.Name)
                        and stmt.target.id == field):
                    return stmt.value
    raise AssertionError(f"{class_name}.{field} is gone from {path.name}")


def test_the_engine_field_default_is_the_constant_not_a_literal():
    value = _field_default(CONFIG_PY, "JobConfig", "model_strategy")
    assert isinstance(value, ast.Name), (
        "JobConfig.model_strategy's default is a literal again -- it must be "
        "DEFAULT_MODEL_STRATEGY so engine and host cannot disagree")
    assert value.id == "DEFAULT_MODEL_STRATEGY"


def _template_scalar_value(field: str) -> ast.expr:
    tree = ast.parse(MAIN_PY.read_text())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "_TEMPLATE_SCALAR_FIELDS"
                        for t in node.targets)):
            assert isinstance(node.value, ast.Dict)
            for key, value in zip(node.value.keys, node.value.values):
                if isinstance(key, ast.Constant) and key.value == field:
                    return value
    raise AssertionError(f"_TEMPLATE_SCALAR_FIELDS[{field!r}] is gone")


def test_the_host_side_template_default_is_the_imported_constant():
    value = _template_scalar_value("model_strategy")
    assert isinstance(value, ast.Name) and value.id == "DEFAULT_MODEL_STRATEGY", (
        "ralphctl start's hardcoded rung re-types the strategy name; it must "
        "import engine/config.DEFAULT_MODEL_STRATEGY")
    assert re.search(r"from \.\.engine\.config import \(?[^)\n]*DEFAULT_MODEL_STRATEGY",
                     MAIN_PY.read_text()), "cli/main.py does not import the constant"


def test_no_strategy_name_is_written_as_a_default_on_the_host_side():
    """`ralphctl`'s source must not contain a strategy name at all: its
    choices come from MODEL_STRATEGIES (task 043) and its default from
    DEFAULT_MODEL_STRATEGY (this task), so any literal is a third copy."""
    literals = [(node.lineno, node.value)
                for node in ast.walk(ast.parse(MAIN_PY.read_text()))
                if isinstance(node, ast.Constant)
                and node.value in set(JobConfig.STRATEGY_TIERS)]
    assert not literals, f"cli/main.py hardcodes strategy names: {literals}"


def test_the_start_flag_itself_still_defaults_to_none():
    """The flag must stay `default=None`: a real default there would beat the
    `--template` and registry rungs, quietly disabling task 043's chain."""
    from ralphd.cli.main import build_parser
    sub = build_parser()._subparsers._group_actions[0]
    action = next(a for a in sub.choices["start"]._actions
                  if "--model-strategy" in a.option_strings)
    assert action.default is None
    assert list(action.choices) == list(MODEL_STRATEGIES)


# --------------------------------- the default and the garbage fallback differ


def _resolved(**kwargs) -> dict[str, str | None]:
    cfg = JobConfig(**kwargs)
    return {p: cfg.model_for(p) for p in PHASES}


def test_the_two_presets_are_observably_different():
    """Without this the pinning below would prove nothing."""
    assert JobConfig.STRATEGY_TIERS[DEFAULT_MODEL_STRATEGY] != \
        JobConfig.STRATEGY_TIERS[UNKNOWN_FALLBACK]
    assert JobConfig.STRATEGY_TIERS[DEFAULT_MODEL_STRATEGY]["worker"] == "fast"
    assert JobConfig.STRATEGY_TIERS[UNKNOWN_FALLBACK]["worker"] == "strong"


def test_the_shipped_default_routes_worker_and_verify_to_the_fast_tier():
    assert _resolved(model="S", fast_model="F") == {
        "planning": "S", "worker": "F", "review": "S", "verify": "F",
        "reflect": "S"}


def test_an_unrecognised_strategy_still_resolves_as_quality_first():
    """Not as DEFAULT_MODEL_STRATEGY: a typo must never downgrade a phase."""
    garbage = _resolved(model_strategy="qualtiy-first", model="S", fast_model="F")
    assert garbage == {p: "S" for p in PHASES}
    assert garbage != _resolved(model="S", fast_model="F"), (
        "the unknown-name fallback now follows the shipped default -- the two "
        "decisions have been collapsed into one (see DEFAULT_MODEL_STRATEGY's "
        "comment)")


def test_the_fallback_site_names_quality_first_and_not_the_default_constant():
    """The code half of the same claim, so a rewrite of `model_for` that reads
    the constant fails here even if it happens to keep the tiers today."""
    tree = ast.parse(CONFIG_PY.read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "model_for")
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    consts = {n.value for n in ast.walk(fn) if isinstance(n, ast.Constant)}
    assert UNKNOWN_FALLBACK in consts
    assert "DEFAULT_MODEL_STRATEGY" not in names


def test_a_run_with_no_fast_model_is_unaffected_by_the_flip():
    """The flip cannot change which model a job actually uses unless the
    operator configured a second one: the fast tier is `fast_model or model`."""
    assert _resolved(model="only") == {p: "only" for p in PHASES}
    assert _resolved() == {p: None for p in PHASES}
    # ...and that is true whatever strategy is shipped, which is why the flip
    # is safe to make mid-series.
    for name in MODEL_STRATEGIES:
        assert _resolved(model_strategy=name, model="only") == \
            {p: "only" for p in PHASES}


def test_a_per_phase_override_still_beats_the_shipped_default():
    assert _resolved(model="S", fast_model="F",
                     model_overrides={"worker": "W"})["worker"] == "W"


# ------------------------------------------------------------- the docs agree

# `| <flag/key> | <default cell> | ...` rows whose second cell must name the
# shipped default, keyed by file.
DEFAULT_CELL_ROWS = {
    CLI_DOC: r"^\| `--model-strategy <s>` \| ([^|]+?) \|",
    SPEC: r"^\| `--model-strategy S` \| ([^|]+?) \|",
}


def _default_cell(text: str, pattern: str) -> str:
    m = re.search(pattern, text, re.MULTILINE)
    assert m, f"the documented row is gone (pattern {pattern})"
    return m.group(1).strip().strip("`")


@pytest.mark.parametrize("path", list(DEFAULT_CELL_ROWS),
                         ids=lambda p: p.name)
def test_the_documented_flag_default_is_the_shipped_default(path):
    assert _default_cell(path.read_text(), DEFAULT_CELL_ROWS[path]) == \
        DEFAULT_MODEL_STRATEGY


@pytest.mark.parametrize("path", list(DEFAULT_CELL_ROWS),
                         ids=lambda p: p.name)
def test_the_flag_default_check_would_catch_the_pre_flip_row(path):
    old = re.sub(DEFAULT_CELL_ROWS[path],
                 lambda m: m.group(0).replace(m.group(1), UNKNOWN_FALLBACK),
                 path.read_text(), count=1, flags=re.MULTILINE)
    assert _default_cell(old, DEFAULT_CELL_ROWS[path]) == UNKNOWN_FALLBACK
    assert _default_cell(old, DEFAULT_CELL_ROWS[path]) != DEFAULT_MODEL_STRATEGY


JOB_KEY_ROW = r"^\| `model_strategy` \| string \| `([^`]+)` \|"


def test_the_spec_job_yaml_row_states_the_shipped_default():
    text = SPEC.read_text()
    m = re.search(JOB_KEY_ROW, text, re.MULTILINE)
    assert m, "SPEC's `model_strategy` job.yaml row is gone"
    assert m.group(1) == DEFAULT_MODEL_STRATEGY
    # ...while still stating the *other* decision, in the same row
    row = text[m.start():text.index("\n", m.start())]
    assert f"silently resolves as `{UNKNOWN_FALLBACK}`" in row


def test_the_job_yaml_row_check_would_catch_the_pre_flip_default():
    old = f"| `model_strategy` | string | `{UNKNOWN_FALLBACK}` |"
    m = re.search(JOB_KEY_ROW, old, re.MULTILINE)
    assert m and m.group(1) != DEFAULT_MODEL_STRATEGY


# Every strategy preset table: exactly one row carries `(default)`, and it is
# the shipped one. SPEC states the table twice (§4.1 and §6.2), the
# architecture doc once.
MARKER_RE = re.compile(r"^\| `([a-z-]+)` \(default\) \|", re.MULTILINE)
PRESET_TABLE_DOCS = {SPEC: 2, ARCH_DOC: 1}


@pytest.mark.parametrize("path", list(PRESET_TABLE_DOCS), ids=lambda p: p.name)
def test_every_preset_table_marks_the_shipped_strategy_as_the_default(path):
    marked = MARKER_RE.findall(path.read_text())
    assert len(marked) == PRESET_TABLE_DOCS[path], (
        f"{path.name} has {len(marked)} `(default)` preset rows, expected "
        f"{PRESET_TABLE_DOCS[path]}: {marked}")
    assert set(marked) == {DEFAULT_MODEL_STRATEGY}


@pytest.mark.parametrize("path", list(PRESET_TABLE_DOCS), ids=lambda p: p.name)
def test_the_marker_check_would_catch_the_pre_flip_table(path):
    old = path.read_text().replace(
        f"| `{DEFAULT_MODEL_STRATEGY}` (default) |", f"| `{DEFAULT_MODEL_STRATEGY}` |")
    old = old.replace(f"| `{UNKNOWN_FALLBACK}` |",
                      f"| `{UNKNOWN_FALLBACK}` (default) |")
    marked = set(MARKER_RE.findall(old))
    assert marked == {UNKNOWN_FALLBACK} and DEFAULT_MODEL_STRATEGY not in marked


def _spec_paragraph(anchor: str) -> str:
    """The blank-line-delimited paragraph containing `anchor`."""
    text = SPEC.read_text()
    i = text.find(anchor)
    assert i >= 0, f"SPEC no longer contains {anchor!r}"
    start = text.rfind("\n\n", 0, i) + 2
    end = text.find("\n\n", i)
    return text[start: end if end > 0 else len(text)]


def test_the_spec_names_the_single_constant_the_host_imports():
    """§6.2's rung 4: the reason the default is not duplicated host-side is that
    `ralphctl` imports it. The precedence paragraph has to name the constant it
    actually is -- the key table saying `balanced` elsewhere in §6.2 is a
    different claim, which is why this reads the paragraph, not the file."""
    import ralphd.engine.config as config_mod
    para = _spec_paragraph("only non-null keys are written")
    assert "DEFAULT_MODEL_STRATEGY" in para, \
        "SPEC §6.2's precedence paragraph no longer names where the shipped " \
        "strategy lives"
    assert "`engine/config.DEFAULT_MODEL_STRATEGY`" in para
    assert hasattr(config_mod, "DEFAULT_MODEL_STRATEGY"), \
        "the SPEC names a constant engine/config.py does not define"


def test_the_api_config_example_shows_the_shipped_strategy():
    """`GET /config`'s example body is what an unconfigured run reports."""
    m = re.search(r'"model": \{"strategy": "([^"]+)"', API_DOC.read_text())
    assert m, "docs/api.md's GET /config example lost its model block"
    assert m.group(1) == DEFAULT_MODEL_STRATEGY


ROADMAP_CLAIMS = ("**T. `balanced` is the shipped default model strategy (#53).**",
                  "DEFAULT_MODEL_STRATEGY",
                  "by operator steering rather than from the PRD")


@pytest.mark.parametrize("claim", ROADMAP_CLAIMS)
def test_the_roadmap_records_the_flip_and_its_provenance(claim):
    assert claim in ROADMAP.read_text()
