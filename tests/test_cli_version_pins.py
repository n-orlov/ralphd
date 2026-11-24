"""The bundled CLIs' version pins and the minimums the harness enforces (#312).

Three version spaces are in play and are never compared with one another:
the `claude-agent-sdk` package (0.2.x: the Dockerfile ARG and the dev pin),
the Claude Code CLI that SDK bundles (2.1.x: `CLAUDE_CODE_MIN_VERSION`), and
the Codex CLI (0.x: `ARG CODEX_VERSION` against `CODEX_MIN_VERSION`).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from ralphd.cli import image
from ralphd.cli.versions import compare_versions
from ralphd.engine import config
from ralphd.harness import claude_code, codex

REPO = Path(__file__).resolve().parents[1]


def sdk_pin_problem(dockerfile: str, pyproject: str) -> str | None:
    """None when the Dockerfile's SDK ARG equals the dev extra's `==` pin."""
    reqs = tomllib.loads(pyproject)["project"]["optional-dependencies"]["dev"]
    pins = [m[1] for r in reqs if (m := re.fullmatch(r"claude-agent-sdk==(\S+)", r))]
    if len(pins) != 1:
        return f"the dev extra has no single claude-agent-sdk== pin: {pins}"
    arg = image.arg_defaults(dockerfile).get("CLAUDE_AGENT_SDK_VERSION")
    if arg != pins[0]:
        return f"Dockerfile ARG {arg!r} != pyproject dev pin {pins[0]!r}"
    return None


def codex_pin_problem(dockerfile: str, minimum: str) -> str | None:
    """None when the Dockerfile's `ARG CODEX_VERSION` is at least `minimum`."""
    arg = image.arg_defaults(dockerfile).get("CODEX_VERSION")
    if compare_versions(arg, minimum) in (-1, None):
        return f"ARG CODEX_VERSION {arg!r} is below CODEX_MIN_VERSION {minimum!r}"
    return None


DOCKERFILE = (REPO / "container/Dockerfile").read_text()
PYPROJECT = (REPO / "pyproject.toml").read_text()


def test_the_image_sdk_arg_equals_the_dev_pin():
    assert sdk_pin_problem(DOCKERFILE, PYPROJECT) is None


def test_a_disagreeing_sdk_pin_is_caught():
    arg = image.arg_defaults(DOCKERFILE)["CLAUDE_AGENT_SDK_VERSION"]
    drifted = DOCKERFILE.replace(f"CLAUDE_AGENT_SDK_VERSION={arg}",
                                 "CLAUDE_AGENT_SDK_VERSION=0.2.1")
    assert sdk_pin_problem(drifted, PYPROJECT)
    assert sdk_pin_problem(DOCKERFILE, PYPROJECT.replace(
        f"claude-agent-sdk=={arg}", "claude-agent-sdk==0.2.1"))


def test_the_sdk_pin_is_an_sdk_version_not_a_cli_version():
    """0.2.x names the package; 2.1.x is the CLI it bundles. Never mixed."""
    arg = image.arg_defaults(DOCKERFILE)["CLAUDE_AGENT_SDK_VERSION"]
    assert arg.startswith("0.2.")
    assert config.CLAUDE_CODE_MIN_VERSION.startswith("2.1.")


def test_the_image_codex_arg_meets_the_codex_minimum():
    assert codex_pin_problem(DOCKERFILE, config.CODEX_MIN_VERSION) is None


def test_a_codex_arg_below_the_minimum_is_caught():
    low = re.sub(r"ARG CODEX_VERSION=\S+", "ARG CODEX_VERSION=0.1.0", DOCKERFILE)
    assert codex_pin_problem(low, config.CODEX_MIN_VERSION)
    assert codex_pin_problem(DOCKERFILE, "99.0.0")


def test_the_claude_code_minimum_is_at_least_2_1_312():
    assert compare_versions(config.CLAUDE_CODE_MIN_VERSION, "2.1.312") in (0, 1)


def test_the_installed_sdk_bundles_a_cli_that_meets_the_minimum():
    """The installed SDK is the dev pin, so its bundled CLI version file is
    what the image's `claude -v` will report; it meets the minimum."""
    import claude_agent_sdk
    bundled = Path(claude_agent_sdk.__file__).parent / "_cli_version.py"
    m = re.search(r'__cli_version__\s*=\s*"([\d.]+)"', bundled.read_text())
    assert m, "the SDK no longer records its bundled CLI version"
    assert compare_versions(m[1], config.CLAUDE_CODE_MIN_VERSION) in (0, 1)


# -- environment the CLIs must not inherit (re-check against 2.1.330) ---------

CLAUDE_2_1_330_ROUTING = [
    "ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_SERVICE_ACCOUNT_ID",
    "ANTHROPIC_PROFILE", "ANTHROPIC_CONFIG_DIR", "ANTHROPIC_UNIX_SOCKET",
    "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
    "ANTHROPIC_FOUNDRY_BASE_URL", "ANTHROPIC_FOUNDRY_RESOURCE",
    "ANTHROPIC_AWS_API_KEY", "ANTHROPIC_AWS_BASE_URL", "ANTHROPIC_AWS_WORKSPACE_ID",
    "ANTHROPIC_GOOGLE_CLOUD_BASE_URL", "ANTHROPIC_GOOGLE_CLOUD_PROJECT",
    "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_USE_GATEWAY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
]


@pytest.mark.parametrize("mod", [claude_code, codex], ids=["claude-code", "codex"])
@pytest.mark.parametrize("name", CLAUDE_2_1_330_ROUTING)
def test_the_scrub_removes_the_claude_2_1_330_routing_variables(mod, name):
    env = {name: "x", "PATH": "/bin"}
    assert mod.scrub_environ(env) == [name]
    assert env == {"PATH": "/bin"}


def test_the_claude_scrub_still_keeps_the_oauth_token():
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "t", "ANTHROPIC_PROFILE": "p"}
    claude_code.scrub_environ(env)
    assert env == {"CLAUDE_CODE_OAUTH_TOKEN": "t"}


def test_the_codex_argv_uses_flags_codex_0_171_documents():
    argv = codex.build_argv("codex", "/w")
    assert argv == ["codex", "exec", "--json", "--skip-git-repo-check", "--ephemeral",
                    "-C", "/w", "--dangerously-bypass-approvals-and-sandbox", "-"]
