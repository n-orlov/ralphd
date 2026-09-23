"""#208: the judging hats' disposable-clone protocol is the product's, not
Python's.

`prompts/review.md` and `prompts/verdict_gate.md` require every measured
figure to come from a throwaway clone that has *proven* it is measuring
itself rather than the live workspace or the engine's own installed copy.
That invariant is right and is preserved here. What was wrong was stating it
as one ecosystem's recipe -- `pip install -e .` plus an assertion on the
installed package's `__file__` -- because a product with no `pyproject.toml`
cannot satisfy it at all. A hat honouring such a prompt legitimately measures
*nothing*, so a Go module's review executed zero tests across three
approaches and rejected `curable: false` each time, and correctly refused the
Go analogue the run had written for itself because nothing authorized
substituting it.

Two halves, both proved below:

  - The prompts state the **invariant** (a disposable clone, plus a proven
    identity assertion) and name the Python recipe as one worked example,
    so a non-Python product has a protocol it can actually satisfy.
  - `JobConfig.review_clone_protocol` names an operator-authorized script,
    rendered into every hat's `## Job context` -- authorization granted at
    launch, where it is auditable, instead of through a mid-run
    `amend --wave`.

Backward compatibility is the load-bearing default and is pinned: absent
means the key is not rendered at all, so every existing job.yaml produces
exactly the prompt it produced before.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import yaml

from test_cli_docker import Ctl, ctl, unix_sock  # noqa: F401  (fixtures)
from tests.conftest import supervisor_factory

from ralphd.engine.config import JobConfig, PROMPTS_BUILTIN

__all__ = ["ctl", "unix_sock"]

_supervisor = supervisor_factory(run_id="unit", run_root="run",
                                 workspace="workspace")

PROTOCOL = "ci/review.sh"
BLOCK_HEADING = "## Disposable-clone protocol (operator-authorized)"

# The two hats whose prompts carry the "Measuring in a disposable clone"
# section, and which therefore need the authorization to reach them.
JUDGING_PROMPTS = ("review", "verdict_gate")

# Hats reached through `build_prompt`; `verify`'s prompt file is
# `task-verify.md`, so the phase name is not a prompt name.
PHASES = ("worker", "review", "planning", "verdict_gate")


def _prompt(name: str) -> str:
    """Whitespace-normalized prompt text.

    These files are hard-wrapped at ~80 columns, so a raw `in` check against a
    phrase pins the wrap column instead of the words and breaks on any reflow.
    """
    return _norm((PROMPTS_BUILTIN / f"{name}.md").read_text())


def _norm(text: str) -> str:
    return " ".join(text.split())


# --------------------------------------------------------------------------
# JobConfig: the job.yaml key, and the default that keeps every existing run
# --------------------------------------------------------------------------


def test_the_key_defaults_to_none_so_an_existing_job_yaml_is_unaffected():
    assert JobConfig().review_clone_protocol is None


def test_job_yaml_declares_the_protocol():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "job.yaml"
        path.write_text("".join(
            f"{k}: {json.dumps(v)}\n" for k, v in
            {"run_id": "r", "review_clone_protocol": PROTOCOL}.items()))
        assert JobConfig.load(path).review_clone_protocol == PROTOCOL


# --------------------------------------------------------------------------
# The rendered `## Job context` block
# --------------------------------------------------------------------------


def test_every_hat_is_told_which_script_is_sanctioned():
    """Rendered for every hat, not just the two that measure: the worker is
    the hat that writes and maintains the script, and a worker blind to the
    path the judge will run is why the Go analogue was written somewhere the
    judge never looked."""
    with tempfile.TemporaryDirectory() as td:
        sup = _supervisor(Path(td), review_clone_protocol=PROTOCOL)
        for phase in PHASES:
            prompt = sup.build_prompt(phase)
            assert BLOCK_HEADING in prompt, phase
            assert f"`{PROTOCOL}`" in prompt, phase


def test_the_block_grants_authority_and_bounds_a_non_zero_exit():
    with tempfile.TemporaryDirectory() as td:
        prompt = _supervisor(
            Path(td), review_clone_protocol=PROTOCOL).build_prompt("review")
        block = _norm(prompt.split(BLOCK_HEADING, 1)[1])
        # Authorization is the whole point: the hat refused a perfectly good
        # protocol once because "neither a task nor a tracked document
        # authorizes replacing that prompt requirement".
        assert "no further authorization, task or amendment" in block
        assert "exit 0" in block
        # A failing protocol must not become a defect in the product -- that
        # is the `curable: false` death spiral this issue is about.
        assert "never as a defect in the work under review" in block


def test_absent_renders_nothing_at_all():
    """The conservative default, pinned: no key, no block, so a run that
    declares no protocol sees byte-for-byte the prompt it always saw."""
    with tempfile.TemporaryDirectory() as td:
        sup = _supervisor(Path(td))
        for phase in PHASES:
            prompt = sup.build_prompt(phase)
            assert BLOCK_HEADING not in prompt, phase


def test_every_hat_also_learns_the_deadline_and_remaining_wall_clock():
    """Task 019 (PRD requirement G, #169): the shared `## Job context`
    block published `Remaining iteration budget` to every hat and nothing
    about wall clock -- the review hat's own clone-and-measure protocol
    measurably costs 35-60 minutes and had no way to learn how much time it
    had left. `Deadline` and `Remaining wall clock` are rendered for every
    phase this module already exercises, independent of the clone-protocol
    key under test here."""
    with tempfile.TemporaryDirectory() as td:
        sup = _supervisor(Path(td))
        for phase in PHASES:
            prompt = sup.build_prompt(phase)
            assert "- Deadline: " in prompt, phase
            assert "- Remaining wall clock: " in prompt, phase


def test_a_whitespace_only_declaration_is_treated_as_absent():
    with tempfile.TemporaryDirectory() as td:
        prompt = _supervisor(Path(td),
                             review_clone_protocol="   ").build_prompt("review")
        assert BLOCK_HEADING not in prompt


# --------------------------------------------------------------------------
# The prompts themselves: an invariant, not one ecosystem's recipe
# --------------------------------------------------------------------------


def test_both_judging_prompts_state_the_invariant_not_a_python_mandate():
    for name in JUDGING_PROMPTS:
        text = _prompt(name)
        # The invariant survives: a throwaway clone, and proof of identity.
        assert "git clone --no-hardlinks" in text, name
        assert "**proven**" in text, name
        # The Python recipe is offered for a Python product rather than
        # demanded of every product. `pip install -e .` appearing at all is
        # fine and expected; what must not appear is the claim that ONLY
        # once that specific assertion holds is a measurement trustworthy.
        assert "For a Python product" in text, name
        assert "For any other ecosystem" in text, name
        # The sentence that made the Python assertion the ONLY gate.
        assert "Only once that assertion holds" not in text, name


def test_both_judging_prompts_honour_the_operator_authorized_protocol():
    for name in JUDGING_PROMPTS:
        text = _prompt(name)
        # The prompt points at the block the engine renders, by name.
        assert "operator-authorized disposable-clone protocol" in text, name
        assert "needs no task, tracked document or amendment" in text, name


def test_both_judging_prompts_forbid_the_curable_false_death_spiral():
    """An unsatisfiable bootstrap is a harness condition. Marking it
    `curable: false` forecloses every remaining approach against an obstacle
    that re-planning the product cannot move -- which is exactly what cost
    the reference run three approaches."""
    for name in JUDGING_PROMPTS:
        text = _prompt(name)
        assert "never a defect in the work under review" in text, name
        assert "curable" in text, name
        assert "`curable: false`" in text, name


def test_both_judging_prompts_still_forbid_manufacturing_the_evidence():
    """The hat's own instinct, kept: refusing to fake identity is why this
    surfaced as a clean rejection instead of silent fabrication."""
    for name in JUDGING_PROMPTS:
        text = _prompt(name)
        assert "do not add a stub package" in text, name
        assert "do not measure the engine's own installed copy" in text, name


# --------------------------------------------------------------------------
# `ralphctl start --review-clone-protocol`: the flag, the template key, and
# the absent case, named against the same three levels `--notifier-url` has
# (tests/test_notifier_url_flag.py) -- the flag exists so declaring this does
# not need `start` -> `docker stop` -> hand-edit `job.yaml` -> `resume`.
# --------------------------------------------------------------------------


def _job_yaml(ctl: Ctl, run_id: str) -> dict:
    return yaml.safe_load(
        (ctl.registry / "configs" / run_id / "job.yaml").read_text())


def test_start_flag_persists_into_job_yaml_and_reaches_config(ctl: Ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--review-clone-protocol", PROTOCOL,
                  "--run-id", "tst-rcp-flag")
    assert res.returncode == 0, res.stderr
    assert _job_yaml(ctl, "tst-rcp-flag")["review_clone_protocol"] == PROTOCOL
    cfg = JobConfig.load(ctl.registry / "configs" / "tst-rcp-flag" / "job.yaml")
    assert cfg.review_clone_protocol == PROTOCOL


def test_start_without_the_flag_writes_no_key(ctl: Ctl):
    """The backward-compatible default, end to end: no flag, no key, so the
    judging prompts' Python recipe stays the protocol."""
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tst-rcp-absent")
    assert res.returncode == 0, res.stderr
    assert "review_clone_protocol" not in _job_yaml(ctl, "tst-rcp-absent")
    cfg = JobConfig.load(
        ctl.registry / "configs" / "tst-rcp-absent" / "job.yaml")
    assert cfg.review_clone_protocol is None


def test_a_templates_key_reaches_the_config(ctl: Ctl):
    """`review_clone_protocol` is one of `_TEMPLATE_SCALAR_FIELDS`, which is
    the only thing that makes a template's own key reach the started run's
    `job.yaml` -- a product's review protocol belongs in the template that
    launches that product."""
    tdir = ctl.registry / "templates" / "goish"
    tdir.mkdir(parents=True)
    (tdir / "job.yaml").write_text(
        yaml.safe_dump({"review_clone_protocol": PROTOCOL}))
    (tdir / "prd.md").write_text("# T\n\nDo it.\n")
    res = ctl.run("start", "--template", "goish", "--llm", "none",
                  "--run-id", "tst-rcp-template")
    assert res.returncode == 0, res.stderr
    cfg = JobConfig.load(
        ctl.registry / "configs" / "tst-rcp-template" / "job.yaml")
    assert cfg.review_clone_protocol == PROTOCOL


# --------------------------------------------------------------------------
# #265/D.1: a command line, not merely a bare path -- verbatim end to end,
# survives `start` -> `job.yaml` -> `resume` -> the rendered prompt
# unchanged, and a bare-path value keeps rendering exactly as it did before.
# --------------------------------------------------------------------------

# #265's own worked example (PRD D.3): a Go product's `ci/review.sh`
# requires at least one argument and exits 2 with none, so the bare-path
# field alone could never name it.
GO_PROTOCOL_WITH_ARGS = "ci/review.sh --smoke"


def test_a_value_with_arguments_is_accepted_by_the_flag_and_the_key(ctl: Ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--review-clone-protocol", GO_PROTOCOL_WITH_ARGS,
                  "--run-id", "tst-rcp-args")
    assert res.returncode == 0, res.stderr
    assert (_job_yaml(ctl, "tst-rcp-args")["review_clone_protocol"]
            == GO_PROTOCOL_WITH_ARGS)
    cfg = JobConfig.load(
        ctl.registry / "configs" / "tst-rcp-args" / "job.yaml")
    assert cfg.review_clone_protocol == GO_PROTOCOL_WITH_ARGS


def test_a_value_with_arguments_survives_start_to_job_yaml_to_resume_to_rendered_prompt_unchanged(
        ctl: Ctl):
    """D.4: the whole path, in order. `resume` only ever rewrites job.yaml's
    `iterations`/`max_approaches`/`job_timeout_s`/`iteration_timeout_s` keys
    (`cmd_resume`, cli/main.py) when the matching flag is passed -- none of
    which this test passes -- so `review_clone_protocol` reaches the fresh
    container's `job.yaml` byte-for-byte, and the rendered block is the
    value with its argument intact, no shell, no argv parsing, no invented
    quoting."""
    run_id = "tst-rcp-resume"
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--review-clone-protocol", GO_PROTOCOL_WITH_ARGS,
                  "--run-id", run_id)
    assert res.returncode == 0, res.stderr
    before = _job_yaml(ctl, run_id)["review_clone_protocol"]
    assert before == GO_PROTOCOL_WITH_ARGS

    # `resume`: no prior container recorded (the same shape
    # `test_resume_without_prior_container_skips_rm` in test_cli_resume.py
    # exercises), and no budget flag -- so job.yaml is untouched by resume.
    res = ctl.run("resume", run_id)
    assert res.returncode == 0, res.stderr
    after = _job_yaml(ctl, run_id)["review_clone_protocol"]
    assert after == GO_PROTOCOL_WITH_ARGS

    # the resumed container's own engine start does exactly this load:
    cfg = JobConfig.load(ctl.registry / "configs" / run_id / "job.yaml")
    assert cfg.review_clone_protocol == GO_PROTOCOL_WITH_ARGS

    with tempfile.TemporaryDirectory() as td:
        prompt = _supervisor(
            Path(td),
            review_clone_protocol=cfg.review_clone_protocol).build_prompt(
                "review")
    # verbatim: the exact backtick-quoted command line, argument intact,
    # with nothing dropped, re-split or re-quoted.
    assert f"`{GO_PROTOCOL_WITH_ARGS}`" in prompt
    assert "`ci/review.sh`" not in prompt


def test_an_existing_bare_path_value_still_renders_exactly_as_it_does_today(
        ctl: Ctl):
    """D.4's second half: the pre-#265 shape (a bare path, no arguments) is
    unaffected -- same block heading, same value quoted verbatim, same
    authorization and exit-0 language as `test_the_block_grants_authority_
    and_bounds_a_non_zero_exit` already pins for a bare-path PROTOCOL."""
    with tempfile.TemporaryDirectory() as td:
        prompt = _supervisor(
            Path(td), review_clone_protocol=PROTOCOL).build_prompt("review")
    assert BLOCK_HEADING in prompt
    block = _norm(prompt.split(BLOCK_HEADING, 1)[1])
    assert f"`{PROTOCOL}`" in block
    assert "is the command the hat runs, verbatim, inside the" in block
    assert "no further authorization, task or amendment" in block
    assert "exit 0" in block
    assert "never as a defect in the work under review" in block
