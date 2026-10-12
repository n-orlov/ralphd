"""Guard for `.github/workflows/ci.yml` (v0.35, requirement A): the triggers,
the two concurrency groups, the runner-label rule and the trust gate.

The workflow is parsed with PyYAML and its expressions are EVALUATED, not
pattern-matched: a small evaluator for the subset of the GitHub Actions
expression language ci.yml uses (`==`, `!=`, `&&`, `||`, `!`, `format()`,
`always()` and the status functions, dotted contexts) decides, for each of four
events -- a same-repository pull request, a fork pull request, a push to `main`
and a manual dispatch -- which jobs would be scheduled, propagating a skipped
dependency the way Actions does (a job whose `needs` did not succeed is skipped
unless its own `if:` names a status function).

Every assertion has a mutated-fixture node: a deep copy edited back to the
broken shape, dumped under `tmp_path` (never under `.github/`), which the same
check must reject -- the discipline `tests/test_ci_dev_layout_matrix_guard.py`
uses.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"

TRUST_EXPR = (
    "github.event_name != 'pull_request' || "
    "github.event.pull_request.head.repo.full_name == github.repository"
)

POOL_LABELS = ["self-hosted", "linux", "x64", "ci-pool"]
CAPABILITY_LABELS = {"docker-host", "browser-runner"}
# The single named exception to the runner-label rule: the verdict job runs on
# a GitHub-hosted runner (no checkout, no secrets) so that a fork pull request
# schedules nothing on a self-hosted one. It does not exist until the task that
# adds it; when it does, it must be exactly this and nothing else may be.
HOSTED_EXCEPTION_JOB = "required"
HOSTED_RUNNER = "ubuntu-latest"
FORBIDDEN_LABEL_RE = re.compile(r"int21h|fast-lane|ralphd-vps")

TRUST_GATED_JOBS = ("lint", "fast", "docker", "browser")

WORKFLOW_DIR = CI_PATH.parent
BROWSER_IMAGE_PATH = WORKFLOW_DIR / "browser-image.yml"
HEAVY_PATH = WORKFLOW_DIR / "heavy.yml"
BROWSER_IMAGE_DIGEST = (
    "ghcr.io/n-orlov/ralphd-browser-ci"
    "@sha256:2311f2f375f35762e57de5c564c43b30cd5fe7cf7dae9e991370a05100596b7c"
)
# The predecessor lanes' bounds (heavy.yml's docker-tier and browser-tier);
# a moved job may never raise them.
MAX_TIMEOUT_MINUTES = {"docker": 120, "browser": 60}
RUN_BOUNDED_RE = re.compile(r"scripts/run_bounded\.sh\s+(\d+)\b")
MAX_RUN_BOUNDED_TOTAL = {"docker": 6600, "browser": 3000}
IMAGE_BUILD_RE = re.compile(r"docker\s+(build|push|buildx)|build-push-action|docker/login-action")

REPO = "n-orlov/ralphd"
SHA = "0123456789abcdef0123456789abcdef01234567"


# --- a small GitHub Actions expression evaluator -----------------------------

_TOKEN_RE = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<num>\d+(?:\.\d+)?)|(?P<op>==|!=|&&|\|\||[!(),])"
    r"|(?P<name>[A-Za-z_][\w-]*(?:\.[A-Za-z_*][\w-]*)*))"
)


def _tokens(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if m is None or m.end() == pos:
            raise ValueError(f"cannot tokenize {text[pos:]!r} in {text!r}")
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


def _norm(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return value
    return value.lower() if isinstance(value, str) else value


def _truthy(value) -> bool:
    return bool(value) and value != "0"


class _Parser:
    def __init__(self, text: str, context: dict, status: dict):
        self.toks = _tokens(text)
        self.i = 0
        self.ctx = context
        self.status = status

    def _peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def _take(self):
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def parse(self):
        value = self._or()
        if self.i != len(self.toks):
            raise ValueError(f"trailing tokens {self.toks[self.i:]!r}")
        return value

    def _or(self):
        value = self._and()
        while self._peek() == ("op", "||"):
            self._take()
            rhs = self._and()
            value = value if _truthy(value) else rhs
        return value

    def _and(self):
        value = self._eq()
        while self._peek() == ("op", "&&"):
            self._take()
            rhs = self._eq()
            value = rhs if _truthy(value) else value
        return value

    def _eq(self):
        value = self._unary()
        while self._peek() in (("op", "=="), ("op", "!=")):
            op = self._take()[1]
            rhs = self._unary()
            same = _norm(value) == _norm(rhs)
            value = same if op == "==" else not same
        return value

    def _unary(self):
        if self._peek() == ("op", "!"):
            self._take()
            return not _truthy(self._unary())
        return self._atom()

    def _atom(self):
        kind, text = self._take()
        if kind == "str":
            return text[1:-1].replace("''", "'")
        if kind == "num":
            return float(text) if "." in text else int(text)
        if (kind, text) == ("op", "("):
            value = self._or()
            assert self._take() == ("op", ")")
            return value
        if kind != "name":
            raise ValueError(f"unexpected token {text!r}")
        if self._peek() == ("op", "("):
            self._take()
            args = []
            if self._peek() != ("op", ")"):
                args.append(self._or())
                while self._peek() == ("op", ","):
                    self._take()
                    args.append(self._or())
            assert self._take() == ("op", ")")
            return self._call(text, args)
        if text in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[text]
        node = self.ctx
        for part in text.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        return node

    def _call(self, name, args):
        if name == "format":
            template = args[0]
            for index, arg in enumerate(args[1:]):
                template = template.replace("{%d}" % index, "" if arg is None else str(arg))
            return template
        if name == "always":
            return True
        if name == "success":
            return self.status["success"]
        if name == "failure":
            return self.status["failure"]
        if name == "cancelled":
            return False
        raise ValueError(f"unsupported function {name}()")


def evaluate(expression, context: dict, status: dict | None = None):
    """Evaluate a workflow expression (bare, or wrapped in `${{ }}`)."""
    if isinstance(expression, bool) or expression is None:
        return expression
    text = str(expression).strip()
    m = re.fullmatch(r"\$\{\{((?:(?!\}\}).)*)\}\}", text, re.S)
    if m:
        text = m.group(1)
    elif "${{" in text:
        # interpolation inside a literal string: `ci-${{ github.ref }}`
        return re.sub(
            r"\$\{\{((?:(?!\}\}).)*)\}\}",
            lambda mm: str(evaluate(mm.group(1), context, status)),
            text,
        )
    status = status or {"success": True, "failure": False}
    return _Parser(text, context, status).parse()


_STATUS_FN_RE = re.compile(r"\b(always|success|failure|cancelled)\s*\(")


# --- scenarios ---------------------------------------------------------------


def _github(event_name: str, *, fork: bool = False) -> dict:
    github = {
        "event_name": event_name,
        "repository": REPO,
        "sha": SHA,
        "ref": "refs/heads/main",
        "event": {},
    }
    if event_name == "pull_request":
        github["ref"] = "refs/pull/7/merge"
        github["event"] = {
            "pull_request": {
                "head": {"repo": {"full_name": "someone/ralphd-fork" if fork else REPO}}
            }
        }
    return github


SCENARIOS = {
    "same-repo PR": _github("pull_request"),
    "fork PR": _github("pull_request", fork=True),
    "push to main": _github("push"),
    "dispatch": _github("workflow_dispatch"),
}


def _step_outputs(job: dict) -> dict:
    """Outputs a scheduled job would publish: every `echo "k=v" >> $GITHUB_OUTPUT`
    line in a step with an `id`, keyed by step id."""
    steps: dict[str, dict] = {}
    for step in job.get("steps") or []:
        if not step.get("id"):
            continue
        values = dict(
            re.findall(r'echo\s+"(\w+)=([^"]*)"\s*>>\s*"?\$GITHUB_OUTPUT"?', step.get("run") or "")
        )
        steps[step["id"]] = {"outputs": values}
    return steps


def schedule(workflow: dict, github: dict) -> dict[str, str]:
    """Return {job: 'success' | 'skipped'} for `github`, a job being scheduled
    (and assumed to succeed) when its `if:` holds and, unless that `if:` names
    a status function, every job it needs succeeded."""
    jobs = workflow.get("jobs") or {}
    result: dict[str, str] = {}
    outputs: dict[str, dict] = {}

    def run(name: str) -> str:
        if name in result:
            return result[name]
        job = jobs[name]
        needs = job.get("needs") or []
        needs = [needs] if isinstance(needs, str) else list(needs)
        for dep in needs:
            run(dep)
        needs_ctx = {
            dep: {"result": result[dep], "outputs": outputs.get(dep, {})} for dep in needs
        }
        all_ok = all(result[dep] == "success" for dep in needs)
        condition = job.get("if")
        ctx = {"github": github, "needs": needs_ctx}
        if condition is None:
            scheduled = all_ok
        elif _STATUS_FN_RE.search(str(condition)):
            scheduled = _truthy(
                evaluate(condition, ctx, {"success": all_ok, "failure": not all_ok})
            )
        else:
            scheduled = all_ok and _truthy(evaluate(condition, ctx))
        result[name] = "success" if scheduled else "skipped"
        if scheduled:
            steps_ctx = _step_outputs(job)
            outputs[name] = {
                key: evaluate(expr, {"steps": steps_ctx})
                for key, expr in (job.get("outputs") or {}).items()
            }
        return result[name]

    for name in jobs:
        run(name)
    return result


def _runs_on(job: dict) -> list[str]:
    value = job.get("runs-on")
    return [value] if isinstance(value, str) else list(value or [])


# --- the checks --------------------------------------------------------------


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # PyYAML parses the bare key `on` as the boolean True.
    return workflow.get("on", workflow.get(True)) or {}


def check_triggers(workflow: dict) -> list[str]:
    problems: list[str] = []
    triggers = _triggers(workflow)
    if set(triggers) != {"push", "pull_request", "workflow_dispatch"}:
        problems.append(f"triggers are {sorted(map(str, triggers))}, not push, pull_request, workflow_dispatch")
    push = triggers.get("push") or {}
    if push.get("branches") != ["main"]:
        problems.append(f"push is not limited to branches: [main]: {push!r}")
    if "branches-ignore" in push or "tags" in push or "paths" in push:
        problems.append(f"push carries a filter besides branches: [main]: {push!r}")
    pr = triggers.get("pull_request") or {}
    if pr.get("branches") or pr.get("branches-ignore"):
        problems.append(f"pull_request is limited to some bases (any base is required): {pr!r}")
    return problems


def check_concurrency(workflow: dict) -> list[str]:
    problems: list[str] = []
    conc = workflow.get("concurrency") or {}
    expect = {
        "same-repo PR": ("ci-refs/pull/7/merge", True),
        "fork PR": ("ci-refs/pull/7/merge", True),
        "push to main": (f"ci-main-{SHA}", False),
        "dispatch": (f"ci-main-{SHA}", False),
    }
    for scenario, (group, cancel) in expect.items():
        ctx = {"github": SCENARIOS[scenario]}
        got_group = evaluate(conc.get("group"), ctx)
        got_cancel = _truthy(evaluate(conc.get("cancel-in-progress"), ctx))
        if got_group != group:
            problems.append(f"{scenario}: concurrency group is {got_group!r}, not {group!r}")
        if got_cancel != cancel:
            problems.append(f"{scenario}: cancel-in-progress is {got_cancel}, not {cancel}")
    return problems


def check_runner_labels(workflow: dict) -> list[str]:
    problems: list[str] = []
    for name, job in (workflow.get("jobs") or {}).items():
        labels = _runs_on(job)
        if name == HOSTED_EXCEPTION_JOB:
            if labels != [HOSTED_RUNNER]:
                problems.append(f"{name} must run on exactly {HOSTED_RUNNER!r}: {labels!r}")
            continue
        if labels[: len(POOL_LABELS)] != POOL_LABELS:
            problems.append(f"{name} does not run on {POOL_LABELS}: {labels!r}")
        extra = set(labels[len(POOL_LABELS):])
        if not extra <= CAPABILITY_LABELS:
            problems.append(
                f"{name} carries labels outside {sorted(CAPABILITY_LABELS)}: {sorted(extra - CAPABILITY_LABELS)}"
            )
        if any(FORBIDDEN_LABEL_RE.search(str(label)) for label in labels):
            problems.append(f"{name} names a standing-runner label: {labels!r}")
    return problems


def check_trust_gate(workflow: dict) -> list[str]:
    problems: list[str] = []
    jobs = workflow.get("jobs") or {}
    trusted = jobs.get("trusted")
    if trusted is None:
        return ["no job named 'trusted'"]
    condition = re.sub(r"\s+", " ", str(trusted.get("if") or "")).strip()
    condition = re.sub(r"^\$\{\{\s*|\s*\}\}$", "", condition)
    if condition != TRUST_EXPR:
        problems.append(f"trusted's own job-level if: is {condition!r}, not the trust expression")
    if trusted.get("needs"):
        problems.append("trusted needs another job")
    for step in trusted.get("steps") or []:
        if "uses" in step:
            problems.append(f"trusted has a uses: step ({step['uses']!r}); it must check out nothing")
    outputs = trusted.get("outputs") or {}
    if list(outputs) != ["trusted"]:
        problems.append(f"trusted exposes {sorted(outputs)}, not exactly one output named 'trusted'")
    for name in TRUST_GATED_JOBS:
        job = jobs.get(name)
        if job is None:
            problems.append(f"no job named {name!r}")
            continue
        needs = job.get("needs") or []
        needs = [needs] if isinstance(needs, str) else needs
        if "trusted" not in needs:
            problems.append(f"{name} does not need trusted")
        if "needs.trusted.outputs.trusted" not in str(job.get("if") or ""):
            problems.append(f"{name} is not conditioned on trusted's output")
    return problems


EXPECTED_SCHEDULE = {
    "same-repo PR": True,
    "fork PR": False,
    "push to main": True,
    "dispatch": True,
}


def check_scheduling(workflow: dict) -> list[str]:
    problems: list[str] = []
    jobs = workflow.get("jobs") or {}
    for scenario, github in SCENARIOS.items():
        result = schedule(workflow, github)
        want = EXPECTED_SCHEDULE[scenario]
        for name in ("trusted", *TRUST_GATED_JOBS):
            got = result.get(name) == "success"
            if got != want:
                problems.append(
                    f"{scenario}: {name} is {'scheduled' if got else 'not scheduled'}, "
                    f"expected {'scheduled' if want else 'not scheduled'}"
                )
        if not want:
            for name, outcome in result.items():
                if outcome == "success" and _runs_on(jobs[name]) != [HOSTED_RUNNER]:
                    problems.append(f"{scenario}: {name} is scheduled on a self-hosted runner")
    return problems


def _grants(job_or_workflow: dict) -> dict:
    perms = job_or_workflow.get("permissions")
    return perms if isinstance(perms, dict) else {}


def check_docker_and_browser_jobs(workflow: dict) -> list[str]:
    problems: list[str] = []
    jobs = workflow.get("jobs") or {}
    for name in ("docker", "browser"):
        job = jobs.get(name)
        if job is None:
            problems.append(f"no job named {name!r}")
            continue
        labels = _runs_on(job)
        want = POOL_LABELS + ["docker-host"] + (["browser-runner"] if name == "browser" else [])
        if labels != want:
            problems.append(f"{name} runs on {labels!r}, not {want!r}")
        minutes = job.get("timeout-minutes")
        if not isinstance(minutes, int) or minutes > MAX_TIMEOUT_MINUTES[name]:
            problems.append(f"{name} timeout-minutes is {minutes!r}, over {MAX_TIMEOUT_MINUTES[name]}")
        bounds = [
            int(m)
            for step in job.get("steps") or []
            for m in RUN_BOUNDED_RE.findall(step.get("run") or "")
        ]
        if not bounds:
            problems.append(f"{name} has no scripts/run_bounded.sh bound")
        if sum(bounds) > MAX_RUN_BOUNDED_TOTAL[name]:
            problems.append(
                f"{name}'s run_bounded bounds sum to {sum(bounds)}s, over {MAX_RUN_BOUNDED_TOTAL[name]}s"
            )
        for step in job.get("steps") or []:
            if not step.get("run") and not step.get("uses"):
                problems.append(f"{name} step {step.get('name')!r} has neither run: nor uses:")
            if IMAGE_BUILD_RE.search(step.get("run") or "") or IMAGE_BUILD_RE.search(str(step.get("uses") or "")):
                problems.append(f"{name} step {step.get('name')!r} builds or pushes an image")
    for name, job in jobs.items():
        for step in job.get("steps") or []:
            if re.search(r"docker\s+(build|push)\b", step.get("run") or ""):
                problems.append(f"ci.yml job {name} step {step.get('name')!r} builds or pushes an image")
    if "packages" in _grants(workflow) or _grants(workflow).get("packages") == "write":
        problems.append("ci.yml grants packages at the workflow level")
    for name, job in jobs.items():
        granted = _grants(job).get("packages")
        want_grant = "read" if name == "browser" else None
        if granted != want_grant:
            problems.append(f"{name} grants packages: {granted!r}, expected {want_grant!r}")
    browser = jobs.get("browser") or {}
    container = browser.get("container")
    if not isinstance(container, dict):
        problems.append("browser has no container: mapping")
    else:
        if container.get("image") != BROWSER_IMAGE_DIGEST:
            problems.append(f"browser container.image is {container.get('image')!r}, not the pinned digest")
        credentials = container.get("credentials") or {}
        if credentials.get("password") != "${{ secrets.GITHUB_TOKEN }}" or not credentials.get("username"):
            problems.append("browser container.credentials are not the job's own GITHUB_TOKEN login")
    if "container" in (jobs.get("docker") or {}):
        problems.append("docker declares a container: (the tier drives the runner's own docker socket)")
    return problems


def check_browser_image_workflow(workflow: dict, others: dict[str, dict]) -> list[str]:
    problems: list[str] = []
    triggers = _triggers(workflow)
    if set(triggers) != {"workflow_dispatch"}:
        problems.append(f"browser-image.yml triggers are {sorted(map(str, triggers))}, not workflow_dispatch only")
    jobs = workflow.get("jobs") or {}
    if list(jobs) != ["browser-image"]:
        problems.append(f"browser-image.yml jobs are {list(jobs)}, not exactly ['browser-image']")
    elif _grants(jobs["browser-image"]).get("packages") != "write":
        problems.append("the browser-image job does not hold packages: write")
    if workflow.get("permissions") not in ({"contents": "read"},):
        problems.append(f"browser-image.yml workflow-level permissions are {workflow.get('permissions')!r}")
    for fname, other in others.items():
        grants = [_grants(other)] + [_grants(j) for j in (other.get("jobs") or {}).values()]
        if any(g.get("packages") == "write" for g in grants):
            problems.append(f"{fname} holds a packages: write grant")
        if "browser-image" in (other.get("jobs") or {}):
            problems.append(f"{fname} still carries a browser-image job")
        if "publish_browser_image" in str(_triggers(other)):
            problems.append(f"{fname} still carries the publish_browser_image input")
    return problems


def _other_workflows() -> dict[str, dict]:
    return {
        path.name: _load(path)
        for path in sorted(WORKFLOW_DIR.glob("*.yml"))
        if path.name != "browser-image.yml"
    }


def _fixture(tmp_path: Path, name: str, workflow: dict) -> dict:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")
    return _load(path)


def _mutate(tmp_path, name, edit) -> dict:
    broken = copy.deepcopy(_load(CI_PATH))
    edit(broken)
    return _fixture(tmp_path, name, broken)


# --- tests: the real file ----------------------------------------------------


def test_ci_triggers_are_push_to_main_pull_request_and_dispatch():
    assert check_triggers(_load(CI_PATH)) == []


def test_ci_concurrency_groups_and_cancel_rule():
    assert check_concurrency(_load(CI_PATH)) == []


def test_ci_runner_labels_stay_on_the_ci_pool():
    assert check_runner_labels(_load(CI_PATH)) == []


def test_ci_trusted_job_is_gated_by_its_own_if_and_lint_and_fast_follow_it():
    assert check_trust_gate(_load(CI_PATH)) == []


def test_the_trust_expression_is_true_false_true_true_for_the_four_events():
    got = {
        scenario: _truthy(evaluate(TRUST_EXPR, {"github": github}))
        for scenario, github in SCENARIOS.items()
    }
    assert got == EXPECTED_SCHEDULE


def test_trusted_lint_and_fast_are_scheduled_except_for_a_fork_pr():
    assert check_scheduling(_load(CI_PATH)) == []


def test_a_fork_pr_schedules_nothing_on_a_self_hosted_runner():
    workflow = _load(CI_PATH)
    result = schedule(workflow, SCENARIOS["fork PR"])
    on_pool = [n for n, r in result.items() if r == "success" and _runs_on(workflow["jobs"][n]) != [HOSTED_RUNNER]]
    assert on_pool == []


# --- tests: the evaluator itself ----------------------------------------------


def test_the_expression_evaluator_handles_the_operators_ci_yml_uses():
    ctx = {"github": {"a": "Push", "b": None}, "needs": {"t": {"outputs": {"x": "true"}}}}
    assert evaluate("github.a == 'push'", ctx) is True
    assert evaluate("github.a != 'push'", ctx) is False
    assert evaluate("github.b == ''", ctx) is True
    assert evaluate("needs.t.outputs.x == 'true' && !(github.a == 'x')", ctx) is True
    assert evaluate("${{ github.a == 'x' || format('ci-{0}', github.a) }}", ctx) == "ci-Push"
    assert evaluate("needs.gone.outputs.x == 'true'", ctx) is False
    assert evaluate("always()", ctx) is True
    with pytest.raises(ValueError):
        evaluate("hashFiles('x')", ctx)


def test_skipped_dependencies_propagate_unless_the_if_names_a_status_function():
    workflow = {
        "jobs": {
            "a": {"runs-on": "x", "if": "github.event_name == 'push'"},
            "b": {"runs-on": "x", "needs": "a"},
            "c": {"runs-on": "x", "needs": "a", "if": "always()"},
            "d": {"runs-on": "x", "needs": "a", "if": "success()"},
        }
    }
    assert schedule(workflow, _github("pull_request")) == {
        "a": "skipped", "b": "skipped", "c": "success", "d": "skipped"
    }
    assert schedule(workflow, _github("push")) == {
        "a": "success", "b": "success", "c": "success", "d": "success"
    }


# --- tests: each assertion fails without what it asserts ----------------------


def test_the_trigger_check_fails_for_each_broken_trigger_set(tmp_path):
    def any_branch(wf):
        _triggers(wf)["push"] = None

    def other_branch(wf):
        _triggers(wf)["push"]["branches"] = ["main", "release"]

    def no_dispatch(wf):
        del _triggers(wf)["workflow_dispatch"]

    def base_filter(wf):
        _triggers(wf)["pull_request"] = {"branches": ["main"]}

    def schedule_added(wf):
        _triggers(wf)["schedule"] = [{"cron": "0 2 * * *"}]

    for name, edit in {
        "push-any-branch": any_branch,
        "push-two-branches": other_branch,
        "no-dispatch": no_dispatch,
        "pr-base-filter": base_filter,
        "extra-trigger": schedule_added,
    }.items():
        assert check_triggers(_mutate(tmp_path, f"{name}.yml", edit)), name


def test_the_concurrency_check_fails_for_each_broken_group(tmp_path):
    def no_cancel(wf):
        wf["concurrency"]["cancel-in-progress"] = False

    def always_cancel(wf):
        wf["concurrency"]["cancel-in-progress"] = True

    def ref_group(wf):
        wf["concurrency"]["group"] = "ci-${{ github.ref }}"

    def sha_group(wf):
        wf["concurrency"]["group"] = "ci-main-${{ github.sha }}"

    for name, edit in {
        "pr-not-cancelled": no_cancel,
        "main-cancelled": always_cancel,
        "main-shares-ref-group": ref_group,
        "pr-uses-sha-group": sha_group,
    }.items():
        assert check_concurrency(_mutate(tmp_path, f"{name}.yml", edit)), name


def test_the_runner_label_check_fails_for_each_broken_label_set(tmp_path):
    def standing_slot(wf):
        wf["jobs"]["lint"]["runs-on"] = ["self-hosted", "linux", "x64", "int21h-ws-1"]

    def fast_lane(wf):
        wf["jobs"]["fast"]["runs-on"] = ["self-hosted", "linux", "x64", "ci-pool", "fast-lane"]

    def hosted(wf):
        wf["jobs"]["lint"]["runs-on"] = "ubuntu-latest"

    def required_on_pool(wf):
        wf["jobs"]["required"] = {"runs-on": POOL_LABELS, "steps": [{"run": "true"}]}

    for name, edit in {
        "standing-slot": standing_slot,
        "fast-lane": fast_lane,
        "hosted-lint": hosted,
        "required-on-pool": required_on_pool,
    }.items():
        assert check_runner_labels(_mutate(tmp_path, f"{name}.yml", edit)), name

    def required_hosted(wf):
        wf["jobs"]["required"] = {"runs-on": HOSTED_RUNNER, "steps": [{"run": "true"}]}

    assert check_runner_labels(_mutate(tmp_path, "required-hosted.yml", required_hosted)) == []


def test_dropping_trusteds_own_if_schedules_it_for_a_fork_pr(tmp_path):
    def drop_if(wf):
        del wf["jobs"]["trusted"]["if"]

    broken = _mutate(tmp_path, "trusted-no-if.yml", drop_if)
    assert any("trusted's own job-level if" in p for p in check_trust_gate(broken))
    problems = check_scheduling(broken)
    assert any("fork PR: trusted is scheduled" in p for p in problems), problems
    assert any("fork PR: lint is scheduled" in p for p in problems), problems
    assert any("scheduled on a self-hosted runner" in p for p in problems), problems


def test_a_weakened_trust_expression_is_rejected(tmp_path):
    def weaken(wf):
        wf["jobs"]["trusted"]["if"] = "github.event_name != 'pull_request'"

    broken = _mutate(tmp_path, "trusted-weak-if.yml", weaken)
    assert any("not the trust expression" in p for p in check_trust_gate(broken))
    assert any("same-repo PR: trusted is not scheduled" in p for p in check_scheduling(broken))

    def always_true(wf):
        wf["jobs"]["trusted"]["if"] = "always()"

    broken = _mutate(tmp_path, "trusted-always.yml", always_true)
    assert any("fork PR: trusted is scheduled" in p for p in check_scheduling(broken))


def test_lint_and_fast_that_ignore_trusted_are_rejected(tmp_path):
    def free_lint(wf):
        del wf["jobs"]["lint"]["needs"]
        del wf["jobs"]["lint"]["if"]

    broken = _mutate(tmp_path, "lint-free.yml", free_lint)
    problems = check_trust_gate(broken)
    assert any("lint does not need trusted" in p for p in problems), problems
    assert any("lint is not conditioned" in p for p in problems), problems
    assert any("fork PR: lint is scheduled" in p for p in check_scheduling(broken))

    def unconditioned_fast(wf):
        del wf["jobs"]["fast"]["if"]

    broken = _mutate(tmp_path, "fast-no-if.yml", unconditioned_fast)
    assert any("fast is not conditioned on trusted's output" in p for p in check_trust_gate(broken))

    def not_needed(wf):
        wf["jobs"]["fast"]["needs"] = []

    broken = _mutate(tmp_path, "fast-no-needs.yml", not_needed)
    assert any("fast does not need trusted" in p for p in check_trust_gate(broken))


def test_a_trusted_job_that_checks_out_or_exposes_two_outputs_is_rejected(tmp_path):
    def checkout(wf):
        wf["jobs"]["trusted"]["steps"].insert(0, {"uses": "actions/checkout@v7"})

    def two_outputs(wf):
        wf["jobs"]["trusted"]["outputs"]["other"] = "${{ steps.gate.outputs.trusted }}"

    problems = check_trust_gate(_mutate(tmp_path, "trusted-checkout.yml", checkout))
    assert any("check out nothing" in p for p in problems), problems
    problems = check_trust_gate(_mutate(tmp_path, "trusted-two.yml", two_outputs))
    assert any("not exactly one output" in p for p in problems), problems


def test_a_trusted_output_that_is_false_skips_lint_and_fast_for_every_event(tmp_path):
    def distrust(wf):
        wf["jobs"]["trusted"]["steps"][0]["run"] = 'echo "trusted=false" >> "$GITHUB_OUTPUT"'

    broken = _mutate(tmp_path, "trusted-false.yml", distrust)
    problems = check_scheduling(broken)
    assert any("same-repo PR: lint is not scheduled" in p for p in problems), problems
    assert any("push to main: fast is not scheduled" in p for p in problems), problems


# --- tests: the docker and browser jobs, and browser-image.yml ------------------


def test_ci_docker_and_browser_jobs_keep_their_lanes_labels_bounds_digest_and_grants():
    assert check_docker_and_browser_jobs(_load(CI_PATH)) == []


def test_docker_and_browser_need_trusted_and_are_not_scheduled_for_a_fork_pr():
    workflow = _load(CI_PATH)
    for name in ("docker", "browser"):
        job = workflow["jobs"][name]
        needs = [job["needs"]] if isinstance(job["needs"], str) else job["needs"]
        assert "trusted" in needs, name
        assert "needs.trusted.outputs.trusted" in str(job["if"]), name
    fork = schedule(workflow, SCENARIOS["fork PR"])
    assert (fork["docker"], fork["browser"]) == ("skipped", "skipped")
    for scenario in ("same-repo PR", "push to main", "dispatch"):
        result = schedule(workflow, SCENARIOS[scenario])
        assert (result["docker"], result["browser"]) == ("success", "success"), scenario
    # They wait on `trusted` alone, so they start beside lint and fast.
    for name in ("docker", "browser"):
        assert workflow["jobs"][name]["needs"] in ("trusted", ["trusted"])


def test_browser_image_workflow_is_dispatch_only_and_the_only_packages_write_holder():
    others = _other_workflows()
    assert "ci.yml" in others and "heavy.yml" in others
    assert check_browser_image_workflow(_load(BROWSER_IMAGE_PATH), others) == []


def test_heavy_yml_no_longer_publishes_the_browser_image():
    heavy = _load(HEAVY_PATH)
    assert "browser-image" not in heavy["jobs"]
    assert "publish_browser_image" not in str(_triggers(heavy))
    assert "packages" not in str(heavy.get("permissions"))


def test_the_docker_and_browser_check_fails_for_each_broken_job(tmp_path):
    def wrong_label(wf):
        wf["jobs"]["docker"]["runs-on"] = ["self-hosted", "linux", "x64", "ci-pool"]

    def browser_without_runner(wf):
        wf["jobs"]["browser"]["runs-on"] = POOL_LABELS + ["docker-host"]

    def raised_docker_timeout(wf):
        wf["jobs"]["docker"]["timeout-minutes"] = 121

    def raised_browser_timeout(wf):
        wf["jobs"]["browser"]["timeout-minutes"] = 61

    def raised_run_bounded(wf):
        step = next(s for s in wf["jobs"]["browser"]["steps"] if s.get("name") == "Browser tier")
        step["run"] = step["run"].replace("run_bounded.sh 2400", "run_bounded.sh 2700")

    def unpinned_image(wf):
        wf["jobs"]["browser"]["container"]["image"] = "ghcr.io/n-orlov/ralphd-browser-ci:latest"

    def other_digest(wf):
        wf["jobs"]["browser"]["container"]["image"] = BROWSER_IMAGE_DIGEST[:-1] + "0"

    def no_credentials(wf):
        del wf["jobs"]["browser"]["container"]["credentials"]

    def packages_write(wf):
        wf["jobs"]["browser"]["permissions"]["packages"] = "write"

    def no_packages_read(wf):
        del wf["jobs"]["browser"]["permissions"]["packages"]

    def docker_gets_packages(wf):
        wf["jobs"]["docker"]["permissions"] = {"contents": "read", "packages": "read"}

    def ci_builds_the_image(wf):
        wf["jobs"]["browser"]["steps"].insert(
            0, {"name": "Build", "run": "docker build -t x ci/browser"}
        )

    def step_without_run(wf):
        del wf["jobs"]["docker"]["steps"][-1]["run"]

    def docker_in_container(wf):
        wf["jobs"]["docker"]["container"] = {"image": "python:3.12"}

    for name, edit in {
        "docker-label": wrong_label,
        "browser-label": browser_without_runner,
        "docker-timeout": raised_docker_timeout,
        "browser-timeout": raised_browser_timeout,
        "browser-run-bounded": raised_run_bounded,
        "tag-not-digest": unpinned_image,
        "other-digest": other_digest,
        "no-credentials": no_credentials,
        "packages-write": packages_write,
        "no-packages-read": no_packages_read,
        "docker-packages": docker_gets_packages,
        "ci-builds-image": ci_builds_the_image,
        "docker-container": docker_in_container,
        "step-without-run": step_without_run,
    }.items():
        assert check_docker_and_browser_jobs(_mutate(tmp_path, f"{name}.yml", edit)), name


def test_dropping_a_trust_condition_from_docker_or_browser_is_rejected(tmp_path):
    for name in ("docker", "browser"):
        def free(wf, name=name):
            del wf["jobs"][name]["needs"]
            del wf["jobs"][name]["if"]

        broken = _mutate(tmp_path, f"{name}-free.yml", free)
        assert any(f"{name} does not need trusted" in p for p in check_trust_gate(broken))
        assert any(f"fork PR: {name} is scheduled" in p for p in check_scheduling(broken))


def test_the_browser_image_check_fails_for_each_broken_workflow(tmp_path):
    def load(edit):
        broken = copy.deepcopy(_load(BROWSER_IMAGE_PATH))
        edit(broken)
        return _fixture(tmp_path, "browser-image-broken.yml", broken)

    others = _other_workflows()

    def add_push(wf):
        _triggers(wf)["push"] = {"branches": ["main"]}

    def add_pr(wf):
        _triggers(wf)["pull_request"] = None

    def drop_write(wf):
        wf["jobs"]["browser-image"]["permissions"] = {"contents": "read"}

    def extra_job(wf):
        wf["jobs"]["other"] = {"runs-on": "x", "steps": [{"run": "true"}]}

    def workflow_write(wf):
        wf["permissions"] = {"contents": "read", "packages": "write"}

    for name, edit in {
        "push-trigger": add_push,
        "pr-trigger": add_pr,
        "no-write-grant": drop_write,
        "second-job": extra_job,
        "workflow-level-write": workflow_write,
    }.items():
        assert check_browser_image_workflow(load(edit), others), name

    # A second packages: write holder among the other workflows is rejected.
    ci_with_write = copy.deepcopy(others["ci.yml"])
    ci_with_write["jobs"]["browser"]["permissions"]["packages"] = "write"
    assert check_browser_image_workflow(
        _load(BROWSER_IMAGE_PATH), {**others, "ci.yml": ci_with_write}
    )
    # heavy.yml carrying the job or the input again is rejected.
    heavy_with_job = copy.deepcopy(others["heavy.yml"])
    heavy_with_job["jobs"]["browser-image"] = {"runs-on": "x", "steps": []}
    assert check_browser_image_workflow(
        _load(BROWSER_IMAGE_PATH), {**others, "heavy.yml": heavy_with_job}
    )
    heavy_with_input = copy.deepcopy(others["heavy.yml"])
    _triggers(heavy_with_input)["workflow_dispatch"] = {
        "inputs": {"publish_browser_image": {"type": "boolean"}}
    }
    assert check_browser_image_workflow(
        _load(BROWSER_IMAGE_PATH), {**others, "heavy.yml": heavy_with_input}
    )
